import shutil
import tempfile
import threading
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from parse_exam_pdf import parse_pdf


APP_ROOT = Path(__file__).resolve().parent
JOB_STORAGE_ROOT = APP_ROOT / "api_jobs"
UPLOAD_ROOT = JOB_STORAGE_ROOT / "uploads"
DEFAULT_WORKERS = 2


# docs_url/redoc/openapi bağlıdır: API strukturu kənardan görünməsin
app = FastAPI(title="PDF Quiz Parser API", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)


class ParsePathRequest(BaseModel):
    pdf_path: str
    output_dir: str


class JobRecord(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    source_type: Literal["path", "upload"]
    source_pdf: Optional[str] = None
    output_dir: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    question_count: Optional[int] = None
    json_path: Optional[str] = None
    images_dir: Optional[str] = None
    error: Optional[str] = None


class ExamRecord(BaseModel):
    id: str
    title: str
    question_count: int
    source_pdf: str
    json_path: str
    updated_at: str


JOBS: Dict[str, JobRecord] = {}
FUTURES: Dict[str, Future] = {}
JOB_LOCK = threading.Lock()
EXECUTOR: Optional[ProcessPoolExecutor] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_directory(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_job_storage() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def build_result_payload(parsed: dict, output_dir: Path) -> dict:
    source_pdf = Path(parsed["source_pdf"])
    json_path = output_dir / source_pdf.stem / f"{source_pdf.stem}.json"
    return {
        "source_pdf": parsed["source_pdf"],
        "question_count": parsed["question_count"],
        "json_path": json_path.as_posix(),
        "images_dir": (output_dir / source_pdf.stem / "images").as_posix(),
    }


def make_exam_id(json_path: Path) -> str:
    try:
        relative = json_path.resolve().relative_to(APP_ROOT)
        parts = relative.parts
    except ValueError:
        parts = json_path.resolve().parts[-3:]
    return "__".join(part.replace(".", "_") for part in parts)


def iter_exam_json_files() -> List[Path]:
    candidates: List[Path] = []
    for path in APP_ROOT.rglob("*.json"):
        if any(part in {"api_jobs", "__pycache__"} for part in path.parts):
            continue
        candidates.append(path)
    return sorted(candidates)


def discover_exams() -> List[Dict[str, Any]]:
    exams: List[Dict[str, Any]] = []
    for json_path in iter_exam_json_files():
        try:
            import json

            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(data, dict):
            continue
        if "questions" not in data or "question_count" not in data or "source_pdf" not in data:
            continue

        source_pdf = str(data.get("source_pdf", ""))
        title = Path(source_pdf).stem or json_path.stem
        exams.append(
            {
                "id": make_exam_id(json_path),
                "title": title,
                "question_count": int(data.get("question_count", 0)),
                "source_pdf": source_pdf,
                "json_path": json_path.as_posix(),
                "updated_at": datetime.fromtimestamp(json_path.stat().st_mtime, timezone.utc).isoformat(),
            }
        )
    exams.sort(key=lambda item: item["updated_at"], reverse=True)
    return exams


def get_exam_file_or_404(exam_id: str) -> Path:
    for exam in discover_exams():
        if exam["id"] == exam_id:
            return Path(exam["json_path"]).resolve()
    raise HTTPException(status_code=404, detail="Exam not found.")


def rewrite_exam_images(value: Any, exam_id: str, exam_root: Path) -> Any:
    if isinstance(value, list):
        return [rewrite_exam_images(item, exam_id, exam_root) for item in value]
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == "images" and isinstance(item, list):
                rewritten[key] = [build_asset_url(path_text, exam_id, exam_root) for path_text in item]
            else:
                rewritten[key] = rewrite_exam_images(item, exam_id, exam_root)
        return rewritten
    return value


def build_asset_url(path_text: str, exam_id: str, exam_root: Path) -> str:
    raw_path = Path(str(path_text).replace("\\", "/"))
    if raw_path.is_absolute():
        asset_path = raw_path.resolve()
    else:
        asset_path = (APP_ROOT / raw_path).resolve()
        if not asset_path.exists():
            asset_path = (exam_root / raw_path).resolve()

    try:
        relative = asset_path.relative_to(exam_root)
    except ValueError:
        return ""
    return f"/exams/{exam_id}/assets/{relative.as_posix()}"


def parse_pdf_job(pdf_path_text: str, output_dir_text: str) -> dict:
    pdf_path = Path(pdf_path_text).expanduser().resolve()
    output_dir = Path(output_dir_text).expanduser().resolve()
    parsed = parse_pdf(pdf_path, output_dir)
    return build_result_payload(parsed, output_dir)


def get_executor() -> ProcessPoolExecutor:
    global EXECUTOR
    if EXECUTOR is None:
        EXECUTOR = ProcessPoolExecutor(max_workers=DEFAULT_WORKERS)
    return EXECUTOR


def get_job_or_404(job_id: str) -> JobRecord:
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def update_job(job_id: str, **changes: object) -> None:
    with JOB_LOCK:
        current = JOBS[job_id]
        JOBS[job_id] = current.model_copy(update=changes)


def attach_future(job_id: str, future: Future) -> None:
    with JOB_LOCK:
        FUTURES[job_id] = future

    def on_done(done_future: Future) -> None:
        try:
            result = done_future.result()
            update_job(
                job_id,
                status="completed",
                finished_at=utc_now_iso(),
                source_pdf=result["source_pdf"],
                question_count=result["question_count"],
                json_path=result["json_path"],
                images_dir=result["images_dir"],
                error=None,
            )
        except Exception as exc:
            update_job(
                job_id,
                status="failed",
                finished_at=utc_now_iso(),
                error=str(exc),
            )

    future.add_done_callback(on_done)


def submit_job(source_type: Literal["path", "upload"], pdf_path: Path, output_dir: Path) -> JobRecord:
    job_id = uuid.uuid4().hex
    job = JobRecord(
        id=job_id,
        status="queued",
        source_type=source_type,
        source_pdf=str(pdf_path),
        output_dir=output_dir.as_posix(),
        created_at=utc_now_iso(),
    )

    with JOB_LOCK:
        JOBS[job_id] = job

    future = get_executor().submit(parse_pdf_job, pdf_path.as_posix(), output_dir.as_posix())
    update_job(job_id, status="running", started_at=utc_now_iso())
    attach_future(job_id, future)
    return JOBS[job_id]


@app.on_event("startup")
def on_startup() -> None:
    ensure_job_storage()
    get_executor()


@app.on_event("shutdown")
def on_shutdown() -> None:
    global EXECUTOR
    if EXECUTOR is not None:
        EXECUTOR.shutdown(wait=False, cancel_futures=False)
        EXECUTOR = None


@app.get("/health")
def health() -> dict:
    with JOB_LOCK:
        jobs = list(JOBS.values())

    return {
        "ok": True,
        "worker_count": DEFAULT_WORKERS,
        "job_counts": {
            "queued": sum(1 for job in jobs if job.status == "queued"),
            "running": sum(1 for job in jobs if job.status == "running"),
            "completed": sum(1 for job in jobs if job.status == "completed"),
            "failed": sum(1 for job in jobs if job.status == "failed"),
        },
    }


@app.get("/exams", response_model=List[ExamRecord])
def list_exams() -> List[ExamRecord]:
    return [ExamRecord(**exam) for exam in discover_exams()]


@app.get("/exams/{exam_id}")
def get_exam(exam_id: str) -> dict:
    import json

    json_path = get_exam_file_or_404(exam_id)
    exam_root = json_path.parent.resolve()
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read exam JSON: {exc}") from exc

    return rewrite_exam_images(data, exam_id, exam_root)


@app.get("/exams/{exam_id}/assets/{asset_path:path}")
def get_exam_asset(exam_id: str, asset_path: str) -> FileResponse:
    json_path = get_exam_file_or_404(exam_id)
    exam_root = json_path.parent.resolve()
    file_path = (exam_root / asset_path).resolve()
    try:
        file_path.relative_to(exam_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid asset path.") from exc

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found.")
    return FileResponse(file_path)


@app.post("/parse-path")
def parse_from_path(payload: ParsePathRequest) -> dict:
    pdf_path = Path(payload.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found.")
    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    output_dir = ensure_directory(payload.output_dir)
    parsed = parse_pdf(pdf_path, output_dir)
    return {"ok": True, **build_result_payload(parsed, output_dir)}


@app.post("/parse-upload")
async def parse_from_upload(
    file: UploadFile = File(...),
    output_dir: str = Form(...),
    filename: Optional[str] = Form(None),
) -> dict:
    original_name = filename or file.filename or "uploaded.pdf"
    safe_name = Path(original_name).name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    output_path = ensure_directory(output_dir)

    with tempfile.TemporaryDirectory(prefix="pdf_parser_") as tmp_dir:
        temp_pdf = Path(tmp_dir) / safe_name
        with temp_pdf.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        parsed = parse_pdf(temp_pdf, output_path)
        return {"ok": True, **build_result_payload(parsed, output_path)}


@app.post("/jobs/parse-path", response_model=JobRecord)
def enqueue_parse_from_path(payload: ParsePathRequest) -> JobRecord:
    pdf_path = Path(payload.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found.")
    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    output_dir = ensure_directory(payload.output_dir)
    return submit_job("path", pdf_path, output_dir)


@app.post("/jobs/parse-upload", response_model=JobRecord)
async def enqueue_parse_from_upload(
    file: UploadFile = File(...),
    output_dir: str = Form(...),
    filename: Optional[str] = Form(None),
) -> JobRecord:
    ensure_job_storage()

    original_name = filename or file.filename or "uploaded.pdf"
    safe_name = Path(original_name).name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    upload_id = uuid.uuid4().hex
    upload_dir = UPLOAD_ROOT / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_pdf = upload_dir / safe_name

    with temp_pdf.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    output_path = ensure_directory(output_dir)
    return submit_job("upload", temp_pdf, output_path)


@app.get("/jobs", response_model=List[JobRecord])
def list_jobs(limit: int = 100) -> List[JobRecord]:
    limit = max(1, min(limit, 500))
    with JOB_LOCK:
        jobs = sorted(JOBS.values(), key=lambda item: item.created_at, reverse=True)
    return jobs[:limit]


@app.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    return get_job_or_404(job_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("parse_api:app", host="127.0.0.1", port=8001, reload=False)
