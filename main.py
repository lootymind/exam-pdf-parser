"""
api/main.py
===========
FastAPI app — submit PDFs, poll status, download results.

Endpoints:
  POST /submit        — upload a PDF → returns job_id
  GET  /status/{id}   — check job status (queued / started / done / failed)
  GET  /result/{id}   — download the output JSON
  GET  /images/{id}/{filename} — serve cropped image files
  GET  /jobs          — list all jobs

Run:
  uvicorn api.main:app --reload --port 8000

Start worker (separate terminal):
  rq worker pdf-queue --url redis://localhost:6379
"""

import os
import json
import uuid
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
from rq import Queue
from rq.job import Job, NoSuchJobError

from queue.worker_task import process_pdf

# ── Config ───────────────────────────────────────────────────────────────────

REDIS_URL      = os.environ.get("REDIS_URL", "redis://localhost:6379")
UPLOAD_DIR     = os.environ.get("UPLOAD_DIR",  "./uploaded_pdfs")
OUTPUT_DIR     = os.environ.get("OUTPUT_DIR",  "./output_jobs")
LOCAL_IMG_DIR  = os.environ.get("LOCAL_IMAGE_DIR", "./output_images")

Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(LOCAL_IMG_DIR).mkdir(parents=True, exist_ok=True)

# ── Redis + RQ setup ─────────────────────────────────────────────────────────

redis_conn = Redis.from_url(REDIS_URL)
pdf_queue  = Queue("pdf-queue", connection=redis_conn)

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="PupilTree PDF Pipeline", version="1.0")

# Serve cropped images as static files at /images/<job_id>/<filename>
app.mount("/images", StaticFiles(directory=LOCAL_IMG_DIR), name="images")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/submit")
async def submit_pdf(file: UploadFile = File(...)):
    """
    Upload a PDF. Returns a job_id to poll with GET /status/{job_id}.
    Enqueues processing — returns immediately.
    Timeout: 5 minutes per PDF (well within 1.5min target for most PDFs).
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files accepted")

    job_id   = str(uuid.uuid4())
    pdf_path = Path(UPLOAD_DIR) / f"{job_id}.pdf"

    # Save uploaded file to disk
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Enqueue the job — RQ worker picks it up asynchronously
    job = pdf_queue.enqueue(
        process_pdf,
        job_id=job_id,
        pdf_path=str(pdf_path),
        job_id=job_id,           # RQ job ID = our job ID for easy lookup
        job_timeout=300,         # 5 min max per PDF
        result_ttl=86400,        # keep result for 24 hours
        failure_ttl=86400,
    )

    return {
        "job_id":   job_id,
        "status":   "queued",
        "filename": file.filename,
        "poll_url": f"/status/{job_id}",
    }


@app.post("/submit-batch")
async def submit_batch(files: list[UploadFile] = File(...)):
    """
    Upload multiple PDFs at once.
    Each gets its own job_id and is processed independently by workers.
    Returns list of job submissions.
    """
    results = []
    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            results.append({"filename": file.filename, "error": "Not a PDF"})
            continue

        job_id   = str(uuid.uuid4())
        pdf_path = Path(UPLOAD_DIR) / f"{job_id}.pdf"

        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        pdf_queue.enqueue(
            process_pdf,
            job_id=job_id,
            pdf_path=str(pdf_path),
            job_id=job_id,
            job_timeout=300,
            result_ttl=86400,
            failure_ttl=86400,
        )

        results.append({
            "job_id":   job_id,
            "filename": file.filename,
            "status":   "queued",
            "poll_url": f"/status/{job_id}",
        })

    return {"submitted": len(results), "jobs": results}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    """
    Poll job status.
    Returns: queued | started | done | failed
    When done, includes output_json and image URLs.
    """
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job.get_status()

    if status == "finished":
        result = job.result or {}
        return {
            "job_id": job_id,
            "status": "done",
            **result,
        }

    if status == "failed":
        error_file = Path(OUTPUT_DIR) / job_id / "error.json"
        error_info = {}
        if error_file.exists():
            error_info = json.loads(error_file.read_text())
        return JSONResponse(
            status_code=500,
            content={"job_id": job_id, "status": "failed", **error_info},
        )

    # queued or started
    position = pdf_queue.job_ids.index(job_id) + 1 if job_id in pdf_queue.job_ids else "?"
    return {
        "job_id":   job_id,
        "status":   str(status),
        "position": position,
    }


@app.get("/result/{job_id}")
def get_result(job_id: str):
    """Download the output JSON for a completed job."""
    result_path = Path(OUTPUT_DIR) / job_id / "result.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Result not ready yet")
    return FileResponse(result_path, media_type="application/json")


@app.get("/jobs")
def list_jobs():
    """List all queued and recently completed jobs."""
    queued  = pdf_queue.job_ids
    started = [j.id for j in pdf_queue.started_job_registry.get_job_ids()]
    failed  = [j for j in pdf_queue.failed_job_registry.get_job_ids()]
    done    = [j for j in pdf_queue.finished_job_registry.get_job_ids()]

    return {
        "queued":  queued,
        "started": started,
        "done":    done,
        "failed":  failed,
        "total":   len(queued) + len(started) + len(done) + len(failed),
    }


@app.get("/health")
def health():
    """Health check — confirms API and Redis are reachable."""
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "api":        "ok",
        "redis":      "ok" if redis_ok else "unreachable",
        "queue_size": len(pdf_queue),
    }
