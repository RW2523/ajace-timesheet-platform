"""HTTP API + static UI.

Endpoints:
    GET  /                     -> calendar UI (static)
    POST /api/process          -> run pipeline {folder, month, year}
    GET  /api/report           -> latest report JSON
    GET  /api/evidence?file=&page=  -> source evidence (rendered page or original file)
    GET  /api/health
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..pipeline import process_folder
from ..settings import get_settings

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

app = FastAPI(title="Timesheet Intelligence", version="1.0.0")

# the Next.js product calls this engine cross-origin
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    """When TSE_API_KEY is configured, every /api/* call (except health) must
    carry a matching X-API-Key header. This makes it safe to expose the engine
    over a public tunnel — only the app holding the secret can reach it."""
    required = get_settings().api_key
    if required:
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            if request.headers.get("x-api-key") != required:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)

# in-memory handle to the last run (also persisted to output/latest_report.json)
STATE: dict = {"report": None, "folder": None}
_PROCESS_LOCK = threading.Lock()


def _allowed_files(report: Optional[dict]) -> set[str]:
    """Relative source paths the engine actually touched -- the ONLY files the
    evidence endpoint may serve. Prevents the folder= / + file=etc/passwd read."""
    allowed: set[str] = set()
    if not report:
        return allowed
    for e in report.get("employees", []):
        for f in e.get("source_files", []) or []:
            allowed.add(f)
        for d in e.get("days", []) or []:
            for s in d.get("sources", []) or []:
                if s.get("file"):
                    allowed.add(s["file"])
        for iss in e.get("issues", []) or []:
            for s in iss.get("sources", []) or []:
                if s.get("file"):
                    allowed.add(s["file"])
    for u in report.get("unprocessed", []) or []:
        if u.get("file"):
            allowed.add(u["file"])
    return allowed


class ProcessRequest(BaseModel):
    folder: str
    month: int
    year: int


def _load_latest_from_disk() -> Optional[dict]:
    p = get_settings().output_path / "latest_report.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


@app.get("/api/health")
def health():
    s = get_settings()
    return {"ok": True, "llm_enabled": s.llm_enabled,
            "models": {t: s.models_for(t)[:1] for t in
                       ("classify", "vision", "table", "normalize", "validate")}}


@app.post("/api/process")
def api_process(req: ProcessRequest):
    folder = Path(req.folder).expanduser()
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(404, f"folder not found: {folder}")
    if not (1 <= req.month <= 12):
        raise HTTPException(400, "month must be 1-12")
    with _PROCESS_LOCK:                      # serialize: STATE + file write are shared
        report = process_folder(folder, req.month, req.year)
        s = get_settings()
        try:
            (s.output_path / "latest_report.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            pass                            # don't 500 after an expensive run
        STATE["report"] = json.loads(report.model_dump_json())
        STATE["folder"] = str(folder.resolve())
    return STATE["report"]


@app.post("/api/process-upload")
async def api_process_upload(
    file: UploadFile = File(...),
    month: int = Form(...),
    year: int = Form(...),
):
    """Process a SINGLE uploaded timesheet (the product's per-employee flow).

    Saves the upload to a scratch folder, runs the full extraction+LLM pipeline
    on just that file, and returns the first employee record it found -- the
    populated calendar + identity fields the web app renders for review/edit.
    """
    if not (1 <= month <= 12):
        raise HTTPException(400, "month must be 1-12")
    name = Path(file.filename or "upload").name           # strip any path
    tmp = Path(tempfile.mkdtemp(prefix="ts_upload_"))
    try:
        dest = tmp / name
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        report = process_folder(tmp, month, year)
        data = json.loads(report.model_dump_json())
        emps = data.get("employees", [])
        return {
            "ok": True,
            "employee": emps[0] if emps else None,
            "employee_count": len(emps),
            "employees": emps,                            # >1 if the file held several
            "unprocessed": data.get("unprocessed", []),
            "llm_used": data.get("llm_used", False),
            "stats": report.stats(),
            "file_name": name,
        }
    except Exception as exc:                              # never leak a stack to the web app
        raise HTTPException(500, f"processing failed: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/preview-upload")
async def api_preview_upload(file: UploadFile = File(...)):
    """Convert an uploaded file (any format) to scrollable page images.

    Fast (no LLM): used by the web app to preview the source the moment it's
    selected. Returns base64 data URLs so the browser renders every format.
    """
    import base64

    name = Path(file.filename or "upload").name
    tmp = Path(tempfile.mkdtemp(prefix="ts_prev_"))
    try:
        dest = tmp / name
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        from ..preview import to_pdf

        pdf = to_pdf(dest, get_settings())
        if not pdf:
            raise HTTPException(415, f"cannot preview {name}")
        pages: list[str] = []
        try:
            import fitz

            with fitz.open(pdf) as doc:
                for i, page in enumerate(doc):
                    if i >= 30:
                        break
                    png = page.get_pixmap(dpi=120).tobytes("png")
                    pages.append("data:image/png;base64," + base64.b64encode(png).decode())
        except Exception as exc:
            raise HTTPException(500, f"render failed: {exc}")
        return {"ok": True, "pages": pages, "file_name": name}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/report")
def api_report():
    rep = STATE["report"] or _load_latest_from_disk()
    if not rep:
        raise HTTPException(404, "no report yet; POST /api/process first")
    STATE["report"] = rep
    if not STATE["folder"]:
        STATE["folder"] = rep.get("folder")
    return rep


def _safe_under(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


@app.get("/api/evidence")
def api_evidence(file: str = Query(...), page: Optional[int] = None):
    """Serve the best available evidence for a source reference.

    Prefers a rendered page PNG under output/_evidence; otherwise returns the
    original source file. Restricted to the processed folder and output dir.
    """
    s = get_settings()
    # Hard input validation: never accept absolute paths or parent traversal,
    # and only serve files the engine actually referenced in the current report.
    if Path(file).is_absolute() or ".." in Path(file).parts:
        raise HTTPException(400, "invalid file reference")
    report = STATE["report"] or _load_latest_from_disk()
    allowed = _allowed_files(report)
    if allowed and file not in allowed:
        raise HTTPException(403, "file is not part of the current report")

    stem = Path(file).stem
    # 1) rendered evidence image (scanned PDFs, docx media)
    ev_dir = s.output_path / "_evidence" / stem
    if ev_dir.exists():
        candidates = []
        if page:
            candidates.append(ev_dir / f"{stem}_p{page}.png")
        candidates += sorted(ev_dir.glob("*.png"))
        for c in candidates:
            if c.exists() and _safe_under(s.output_path / "_evidence", c):
                return FileResponse(str(c))
    # 2) original source file (constrained to the processed folder)
    folder = STATE.get("folder")
    if folder:
        base = Path(folder).resolve()
        src = (base / file).resolve()
        if src.exists() and not src.is_symlink() and _safe_under(base, src):
            return FileResponse(str(src), filename=Path(file).name)
    raise HTTPException(404, f"no evidence found for {file}")


@app.get("/api/preview")
def api_preview(file: str = Query(...)):
    """Return ANY source file as a previewable PDF (converts on demand, cached).

    Excel/CSV/DOCX are rendered via LibreOffice, images wrapped into a PDF, and
    PDFs served as-is -- so the UI can preview every format uniformly.
    """
    from ..preview import to_pdf

    s = get_settings()
    if Path(file).is_absolute() or ".." in Path(file).parts:
        raise HTTPException(400, "invalid file reference")
    report = STATE["report"] or _load_latest_from_disk()
    allowed = _allowed_files(report)
    if allowed and file not in allowed:
        raise HTTPException(403, "file is not part of the current report")
    folder = STATE.get("folder") or (report or {}).get("folder")
    if not folder:
        raise HTTPException(404, "no processed folder")
    base = Path(folder).resolve()
    src = (base / file).resolve()
    if not (src.exists() and not src.is_symlink() and _safe_under(base, src)):
        raise HTTPException(404, f"source not found: {file}")
    pdf = to_pdf(src, s)
    if not pdf or not Path(pdf).exists():
        raise HTTPException(415, f"cannot render preview for {file}")
    return FileResponse(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{Path(file).stem}.pdf"'})


def _resolve_source(file: str):
    """Validated absolute path of a report source file, or raise."""
    if Path(file).is_absolute() or ".." in Path(file).parts:
        raise HTTPException(400, "invalid file reference")
    report = STATE["report"] or _load_latest_from_disk()
    allowed = _allowed_files(report)
    if allowed and file not in allowed:
        raise HTTPException(403, "file is not part of the current report")
    folder = STATE.get("folder") or (report or {}).get("folder")
    if not folder:
        raise HTTPException(404, "no processed folder")
    base = Path(folder).resolve()
    src = (base / file).resolve()
    if not (src.exists() and not src.is_symlink() and _safe_under(base, src)):
        raise HTTPException(404, f"source not found: {file}")
    return src


@app.get("/api/preview_pages")
def api_preview_pages(file: str = Query(...)):
    """Render the source's PDF to page images; returns the page count + URLs."""
    from ..preview import pdf_page_images

    src = _resolve_source(file)
    imgs = pdf_page_images(src, get_settings())
    if not imgs:
        raise HTTPException(415, f"cannot render preview for {file}")
    from urllib.parse import quote
    return {"pages": len(imgs),
            "urls": [f"/api/preview_img?file={quote(file)}&page={i + 1}"
                     for i in range(len(imgs))]}


@app.get("/api/preview_img")
def api_preview_img(file: str = Query(...), page: int = Query(1)):
    """Serve one rendered page image of the source's PDF preview."""
    from ..preview import pdf_page_images

    src = _resolve_source(file)
    imgs = pdf_page_images(src, get_settings())
    if not (1 <= page <= len(imgs)):
        raise HTTPException(404, "page out of range")
    return FileResponse(imgs[page - 1], media_type="image/png")


# static UI (mounted last so /api/* wins)
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


def serve(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    print(f"\n  Timesheet UI  ->  http://{host}:{port}/\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
