"""
image_store.py
==============
Saves cropped images and returns a public URL.

Local (default):
  - Images saved to ./output_images/<job_id>/
  - Served via FastAPI static files
  - URL: http://localhost:8000/images/<job_id>/<filename>

Cloud (optional):
  - Set STORAGE_BACKEND=s3 in .env
  - URL: https://<bucket>.s3.<region>.amazonaws.com/<job_id>/<filename>
"""

import os
import shutil
from pathlib import Path

STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
LOCAL_IMAGE_DIR = os.environ.get("LOCAL_IMAGE_DIR", "./output_images")
BASE_URL        = os.environ.get("BASE_URL", "http://localhost:8000/images")


# ── Local backend ────────────────────────────────────────────────────────────

def _save_local(src_path: str, job_id: str, filename: str) -> str:
    dest_dir = Path(LOCAL_IMAGE_DIR) / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest_dir / filename)
    return f"{BASE_URL}/{job_id}/{filename}"


# ── S3 backend (swap in when ready) ─────────────────────────────────────────

def _save_s3(src_path: str, job_id: str, filename: str) -> str:
    import boto3
    bucket = os.environ["AWS_BUCKET"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    key    = f"{job_id}/{filename}"
    boto3.client("s3").upload_file(
        src_path, bucket, key, ExtraArgs={"ACL": "public-read"}
    )
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


# ── Public API ───────────────────────────────────────────────────────────────

def store_image(src_path: str, job_id: str) -> str:
    """Store one cropped image, return its public URL."""
    filename = Path(src_path).name
    if STORAGE_BACKEND == "s3":
        return _save_s3(src_path, job_id, filename)
    return _save_local(src_path, job_id, filename)


def store_all_images(image_metadata: list, job_id: str) -> list:
    """
    Store every cropped image from digital_parser output.
    Adds a 'url' key to each metadata dict — this URL goes into the JSON output.
    """
    for img in image_metadata:
        src = img.get("path", "")
        img["url"] = store_image(src, job_id) if os.path.exists(src) else None
    return image_metadata
