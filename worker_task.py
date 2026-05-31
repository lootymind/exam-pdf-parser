"""
worker_task.py
==============
The function that RQ workers execute for each PDF job.
Runs the full pipeline: extract images → store → match questions → output JSON.
"""

import os
import json
import uuid
import traceback
from pathlib import Path

import fitz

# Import your existing modules (adjust paths as needed)
import sys
sys.path.append(str(Path(__file__).parent.parent))

from digital_parser import render_and_save_images, filter_duplicate_images
from question_matcher_gemini import (
    get_cleaned_text_blocks,
    detect_questions,
    match_images_to_questions,
    generate_json,
)
from storage.image_store import store_all_images

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output_jobs")


def process_pdf(job_id: str, pdf_path: str) -> dict:
    """
    Full pipeline for one PDF.
    Called by RQ worker — runs in background.

    Returns result dict with output JSON path and summary stats.
    """
    print(f"[{job_id}] Starting: {pdf_path}")
    job_output_dir = Path(OUTPUT_DIR) / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)

        # ── Step 1: Extract & crop all images ───────────────────────────────
        temp_img_dir = str(job_output_dir / "temp_images")
        images = render_and_save_images(
            doc, output_folder=temp_img_dir, dpi=200
        )
        images = filter_duplicate_images(images)
        print(f"[{job_id}] Extracted {len(images)} images")
        doc.close()

        # ── Step 2: Store images → get real URLs ────────────────────────────
        images = store_all_images(images, job_id)
        print(f"[{job_id}] Images stored, URLs assigned")

        # Save image metadata for reference
        meta_path = job_output_dir / "image_metadata.json"
        meta_path.write_text(json.dumps(images, indent=2))

        # ── Step 3: Extract text & detect questions ──────────────────────────
        text_blocks = get_cleaned_text_blocks(pdf_path)
        questions   = detect_questions(text_blocks)
        print(f"[{job_id}] Detected {len(questions)} questions")

        # ── Step 4: Gemini vision places images ──────────────────────────────
        questions = match_images_to_questions(pdf_path, questions, images)

        # ── Step 5: Generate schema-conformant JSON ──────────────────────────
        output_json_path = str(job_output_dir / "result.json")
        result = generate_json(questions, output_path=output_json_path)

        summary = {
            "job_id":           job_id,
            "status":           "done",
            "pdf":              pdf_path,
            "total_questions":  result["total_questions"],
            "stem_images":      sum(len(q.get("imageDetails", [])) for q in result["questions"]),
            "option_images":    sum(
                1 for q in result["questions"]
                for o in q.get("options", []) if o.get("imageDetails")
            ),
            "output_json":      output_json_path,
        }
        print(f"[{job_id}] Done ✅ — {summary['total_questions']} questions")
        return summary

    except Exception as e:
        error_info = {
            "job_id": job_id,
            "status": "failed",
            "pdf":    pdf_path,
            "error":  str(e),
            "trace":  traceback.format_exc(),
        }
        print(f"[{job_id}] FAILED ❌ — {e}")
        # Write error to disk so it's inspectable
        (job_output_dir / "error.json").write_text(
            json.dumps(error_info, indent=2)
        )
        raise   # Re-raise so RQ marks job as failed
