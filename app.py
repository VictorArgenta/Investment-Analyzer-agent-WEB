"""
Investment Analyzer Web — Flask + Anthropic API
Análisis institucional de inversión vía agente con búsqueda web.
"""
import json
import os
import queue
import threading
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)

# Configuración del modelo y la herramienta
MODEL_NAME = "claude-sonnet-4-20250514"
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 15,
}
MAX_TOKENS = 16000
MAX_ITERATIONS = 25

# Ruta del system prompt
PROMPT_PATH = Path(__file__).parent / "prompts" / "investment_agent.md"

# Estado por sesión — cada sesión tiene su cola de eventos SSE
sessions: dict[str, queue.Queue] = {}
sessions_lock = threading.Lock()


def load_system_prompt() -> str:
    """Lee el system prompt desde el fichero en prompts/."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def emit(q: queue.Queue, event_type: str, **payload) -> None:
    """Envía un evento a la cola SSE de la sesión."""
    q.put({"type": event_type, **payload})


def run_agent(ticker: str, q: queue.Queue) -> None:
    """
    Bucle agentico completo.
    Ejecuta la herramienta web_search mientras el modelo lo requiera
    y emite los pasos por la cola SSE.
    """
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            emit(q, "error", message="Falta ANTHROPIC_API_KEY en el entorno")
            return

        client = Anthropic(api_key=api_key)
        system_prompt = load_system_prompt()

        emit(q, "step", message=f"Iniciando análisis institucional de {ticker}")
        emit(q, "step", message="Cargando system prompt de Equity Research")

        user_message = (
            f"Realiza un Equity Research institucional completo sobre la empresa cotizada "
            f"con ticker {ticker}. Sigue rigurosamente los 8 pasos del proceso de investigación "
            f"usando web_search para obtener datos actualizados y fuentes primarias "
            f"(SEC filings, Investor Relations, earnings calls). "
            f"Cuando hayas completado TODOS los pasos, redacta el informe final "
            f"con el formato institucional especificado."
        )

        messages = [{"role": "user", "content": user_message}]

        iteration = 0
        final_response = None

        while iteration < MAX_ITERATIONS:
            iteration += 1
            emit(q, "step", message=f"Llamada al modelo #{iteration}")

            response = client.messages.create(
                model=MODEL_NAME,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            )
            final_response = response

            # Procesar los bloques de contenido y emitir eventos de progreso
            for block in response.content:
                btype = getattr(block, "type", None)

                if btype == "text":
                    text = getattr(block, "text", "") or ""
                    if text.strip():
                        preview = text.strip()
                        if len(preview) > 280:
                            preview = preview[:280] + "…"
                        emit(q, "thinking", message=preview)

                elif btype == "server_tool_use":
                    tool_name = getattr(block, "name", "tool")
                    tool_input = getattr(block, "input", {}) or {}
                    query = tool_input.get("query") if isinstance(tool_input, dict) else None
                    if query:
                        emit(q, "tool_use", message=f"web_search · {query}")
                    else:
                        emit(q, "tool_use", message=f"Invocando {tool_name}")

                elif btype == "web_search_tool_result":
                    content = getattr(block, "content", None)
                    count = len(content) if isinstance(content, list) else 0
                    emit(q, "tool_result", message=f"Resultados recibidos ({count} fuentes)")

                elif btype == "tool_use":
                    # Fallback para posibles herramientas client-side
                    emit(q, "tool_use", message=f"Herramienta local: {getattr(block, 'name', '')}")

            # Añadir la respuesta del asistente al historial
            messages.append({"role": "assistant", "content": response.content})

            stop_reason = response.stop_reason
            emit(q, "step", message=f"stop_reason = {stop_reason}")

            if stop_reason == "tool_use":
                # Algún tool client-side quedó pendiente. Con web_search (server-side)
                # esto no debería ocurrir, pero lo manejamos defensivamente.
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Herramienta no implementada en este entorno.",
                            "is_error": True,
                        })
                if not tool_results:
                    # No hay tool_use real → salimos
                    break
                messages.append({"role": "user", "content": tool_results})
                continue

            if stop_reason == "pause_turn":
                # El modelo ha pausado para continuar (turnos largos con server tools)
                emit(q, "step", message="Pausa de turno — continuando")
                continue

            # end_turn, max_tokens, stop_sequence, etc. → fin del bucle
            break

        # Extraer el texto final del último mensaje del asistente
        report_text = ""
        if final_response is not None:
            report_text = "\n\n".join(
                getattr(b, "text", "") for b in final_response.content
                if getattr(b, "type", None) == "text"
            ).strip()

        if not report_text:
            report_text = "El agente terminó sin producir un informe de texto."

        emit(q, "done", report=report_text, iterations=iteration)

    except Exception as exc:  # noqa: BLE001
        emit(q, "error", message=f"{type(exc).__name__}: {exc}")


@app.route("/")
def index():
    """Formulario para introducir el ticker."""
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Lanza el agente en un hilo y devuelve el session_id
    que el cliente usará para suscribirse al stream SSE.
    """
    data = request.get_json(silent=True) or request.form
    ticker = (data.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "El ticker es obligatorio"}), 400

    session_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    with sessions_lock:
        sessions[session_id] = q

    thread = threading.Thread(target=run_agent, args=(ticker, q), daemon=True)
    thread.start()

    return jsonify({"session_id": session_id, "ticker": ticker})


@app.route("/stream/<session_id>")
def stream(session_id: str):
    """Stream SSE con los eventos del agente para la sesión indicada."""

    def generate():
        with sessions_lock:
            q = sessions.get(session_id)
        if q is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Sesión no encontrada'})}\n\n"
            return

        try:
            while True:
                try:
                    event = q.get(timeout=300)
                except queue.Empty:
                    # Heartbeat para mantener viva la conexión
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                if event.get("type") in ("done", "error"):
                    break
        finally:
            with sessions_lock:
                sessions.pop(session_id, None)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(generate(), headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
