import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline import run_pipeline

BASE_DIR = Path(__file__).resolve().parent.parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="DNAssociates POD Matcher")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict[str, dict] = {}


def _save_uploads(files: list[UploadFile], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        safe_name = Path(f.filename).name
        out = dest / safe_name
        with open(out, "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        paths.append(out)
    return paths


def _serialize_record(r: dict) -> dict:
    return {k: v for k, v in r.items()}


def _run_job(job_id: str, pod_paths, po_paths, xlsx_path, tmpdir):
    job = JOBS[job_id]

    def progress_cb(stage, current, total):
        job["stage"] = stage
        job["current"] = current
        job["total"] = total

    try:
        job["status"] = "processing"
        out_pdf = JOBS_DIR / job_id / "merged_output.pdf"
        out_csv = JOBS_DIR / job_id / "match_report.csv"
        result = run_pipeline(
            pod_paths, po_paths, xlsx_path, Path(tmpdir), out_pdf, out_csv, progress_cb
        )

        matched = [
            {
                "patient_name": po["patient_name"],
                "order_no": po["order_no"],
                "invoice_no": pod["invoice_no"],
                "match_method": method,
                "verification": verification,
                "pod_source": pod["source_pdf"],
                "pod_page": pod["page_index"] + 1,
                "po_source": po["source_pdf"],
                "po_page": po["page_index"] + 1,
            }
            for po, pod, method, verification in result.matched
        ]
        job["result"] = {
            "matched": sorted(matched, key=lambda m: (m["patient_name"] or "")),
            "unmatched_po": [_serialize_record(r) for r in result.unmatched_po],
            "unmatched_pod": [_serialize_record(r) for r in result.unmatched_pod],
            "xlsx_meta": result.xlsx_meta,
            "has_output": out_pdf.exists(),
        }
        job["status"] = "done"
    except Exception as e:  # surface errors to the UI instead of hanging
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/jobs")
async def create_job(
    pod_files: list[UploadFile] = File(...),
    po_files: list[UploadFile] = File(...),
    xlsx_file: Optional[UploadFile] = File(None),
):
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = job_dir / "uploads"

    pod_paths = _save_uploads(pod_files, upload_dir / "pod")
    po_paths = _save_uploads(po_files, upload_dir / "po")
    xlsx_path = None
    if xlsx_file is not None and xlsx_file.filename:
        xlsx_path = _save_uploads([xlsx_file], upload_dir / "xlsx")[0]

    tmpdir = tempfile.mkdtemp(prefix=f"pod_matcher_{job_id}_")

    JOBS[job_id] = {
        "status": "queued",
        "stage": "Queued",
        "current": 0,
        "total": 0,
        "created": time.time(),
    }

    t = threading.Thread(
        target=_run_job, args=(job_id, pod_paths, po_paths, xlsx_path, tmpdir), daemon=True
    )
    t.start()

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return job


@app.get("/api/jobs/{job_id}/download/merged.pdf")
async def download_merged(job_id: str):
    path = JOBS_DIR / job_id / "merged_output.pdf"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename="merged_output.pdf")


@app.get("/api/jobs/{job_id}/download/report.csv")
async def download_report(job_id: str):
    path = JOBS_DIR / job_id / "match_report.csv"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="text/csv", filename="match_report.csv")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
