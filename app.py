"""
Investment Analyzer Web — Flask + Anthropic API
Análisis institucional de inversión vía agente con búsqueda web.
"""
import io
import json
import os
import queue
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file

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

# Estado por sesión — cola para SSE + informe final para descarga
sessions: dict[str, queue.Queue] = {}
reports: dict[str, dict] = {}
state_lock = threading.Lock()


def load_system_prompt() -> str:
    """Lee el system prompt desde el fichero en prompts/."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def emit(q: queue.Queue, event_type: str, **payload) -> None:
    """Envía un evento a la cola SSE de la sesión."""
    q.put({"type": event_type, **payload})


def run_agent(ticker: str, session_id: str, q: queue.Queue) -> None:
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
                    break
                messages.append({"role": "user", "content": tool_results})
                continue

            if stop_reason == "pause_turn":
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

        # Guardar el informe para permitir la descarga posterior
        with state_lock:
            reports[session_id] = {
                "ticker": ticker,
                "report": report_text,
                "generated_at": datetime.utcnow().isoformat() + "Z",
            }

        emit(q, "done", report=report_text, iterations=iteration, session_id=session_id)

    except Exception as exc:  # noqa: BLE001
        emit(q, "error", message=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Construcción del documento Word
# ---------------------------------------------------------------------------

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_rich_paragraph(doc: Document, text: str, *, style: str | None = None) -> None:
    """Añade un párrafo con parseo muy simple de negritas markdown (**…**)."""
    paragraph = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    cursor = 0
    for match in _MD_BOLD.finditer(text):
        if match.start() > cursor:
            paragraph.add_run(text[cursor:match.start()])
        bold_run = paragraph.add_run(match.group(1))
        bold_run.bold = True
        cursor = match.end()
    if cursor < len(text):
        paragraph.add_run(text[cursor:])


def _strip_md(text: str) -> str:
    """Limpia marcas markdown inline básicas."""
    text = _MD_BOLD.sub(r"\1", text)
    text = text.replace("`", "")
    return text


def _parse_table(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """Parsea una tabla markdown empezando en `start`. Devuelve (filas, índice_siguiente)."""
    rows: list[list[str]] = []
    i = start
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        raw = lines[i].strip()
        # Saltar separador tipo |---|---|
        if re.match(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", raw):
            i += 1
            continue
        cells = [c.strip() for c in raw.strip("|").split("|")]
        rows.append(cells)
        i += 1
    return rows, i


def build_docx(ticker: str, report_text: str, generated_at: str) -> io.BytesIO:
    """
    Convierte el informe en markdown a un .docx con formato institucional.
    Soporta encabezados, listas, tablas markdown y negritas.
    """
    doc = Document()

    # Estilo base
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    # Cabecera del documento
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = title.add_run(f"Investment Research Report — {ticker}")
    run.bold = True
    run.font.size = Pt(22)
    run.font.color.rgb = RGBColor(0x0B, 0x3D, 0x5F)

    meta = doc.add_paragraph()
    meta_run = meta.add_run(f"Generado: {generated_at}  ·  Equity Research AI")
    meta_run.italic = True
    meta_run.font.size = Pt(9)
    meta_run.font.color.rgb = RGBColor(0x6B, 0x6B, 0x6B)

    doc.add_paragraph()  # separador

    lines = report_text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # Línea en blanco
        if not line.strip():
            i += 1
            continue

        stripped = line.lstrip()

        # Tabla markdown (dos filas al menos + separador)
        if stripped.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
            rows, next_i = _parse_table(lines, i)
            if rows:
                ncols = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=ncols)
                table.style = "Light Grid Accent 1"
                for r_idx, row in enumerate(rows):
                    for c_idx in range(ncols):
                        cell_text = _strip_md(row[c_idx]) if c_idx < len(row) else ""
                        cell = table.rows[r_idx].cells[c_idx]
                        cell.text = ""  # reset
                        para = cell.paragraphs[0]
                        run = para.add_run(cell_text)
                        if r_idx == 0:
                            run.bold = True
                doc.add_paragraph()
            i = next_i
            continue

        # Encabezados
        if stripped.startswith("#### "):
            doc.add_heading(_strip_md(stripped[5:]), level=4)
        elif stripped.startswith("### "):
            doc.add_heading(_strip_md(stripped[4:]), level=3)
        elif stripped.startswith("## "):
            doc.add_heading(_strip_md(stripped[3:]), level=2)
        elif stripped.startswith("# "):
            doc.add_heading(_strip_md(stripped[2:]), level=1)
        # Listas con viñetas
        elif stripped.startswith(("- ", "* ", "• ")):
            _add_rich_paragraph(doc, stripped[2:].lstrip(), style="List Bullet")
        # Listas numeradas (1. 2. 3. …)
        elif re.match(r"^\d+[.\)]\s+", stripped):
            content = re.sub(r"^\d+[.\)]\s+", "", stripped)
            _add_rich_paragraph(doc, content, style="List Number")
        # Línea separadora
        elif stripped in ("---", "***", "___"):
            doc.add_paragraph("_" * 40)
        else:
            _add_rich_paragraph(doc, stripped)

        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


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
    with state_lock:
        sessions[session_id] = q

    thread = threading.Thread(target=run_agent, args=(ticker, session_id, q), daemon=True)
    thread.start()

    return jsonify({"session_id": session_id, "ticker": ticker})


@app.route("/stream/<session_id>")
def stream(session_id: str):
    """Stream SSE con los eventos del agente para la sesión indicada."""

    def generate():
        with state_lock:
            q = sessions.get(session_id)
        if q is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Sesión no encontrada'})}\n\n"
            return

        try:
            while True:
                try:
                    event = q.get(timeout=300)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                if event.get("type") in ("done", "error"):
                    break
        finally:
            # Limpiamos sólo la cola; el informe se mantiene para la descarga.
            with state_lock:
                sessions.pop(session_id, None)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(generate(), headers=headers)


@app.route("/download/<session_id>")
def download(session_id: str):
    """Devuelve el informe como documento Word (.docx)."""
    with state_lock:
        data = reports.get(session_id)
    if not data:
        return jsonify({"error": "Informe no disponible o sesión expirada"}), 404

    buf = build_docx(data["ticker"], data["report"], data["generated_at"])
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    filename = f"research_{data['ticker']}_{stamp}.docx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
