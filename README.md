# PupilTree — Exam PDF → Structured JSON Pipeline

An end-to-end pipeline that ingests exam PDFs and outputs structured JSON conforming to the PupilTree schema. Extracts every question, classifies its type, preserves LaTeX and tables, and detects, crops, stores, and correctly places every image at the right level of the schema (question stem, option, or table cell).

---

## Table of Contents

- [Approach](#approach)
- [Project Structure](#project-structure)
- [Tools & Models Used](#tools--models-used)
- [Setup & Installation](#setup--installation)
- [Running the Pipeline](#running-the-pipeline)
- [Queue Design & Scaling Plan](#queue-design--scaling-plan)
- [Schema & Extensions](#schema--extensions)
- [Cost Per PDF](#cost-per-pdf)
- [Latency](#latency)
- [Known Limitations](#known-limitations)

---

## Approach

The pipeline runs in two stages:

**Stage 1 — PyMuPDF (structural extraction)**
- Opens the PDF and detects all embedded raster images (`get_images()`) and vector drawings (`get_cdrawings()`)
- Crops each image tightly with 200 DPI resolution, adds 4pt padding to avoid clipping
- Merges overlapping bounding boxes so composite figures aren't split into fragments
- Filters headers, footers, watermarks, and decorative lines using area + aspect ratio thresholds
- Normalises bounding boxes to a 0–1000 grid as required by the schema
- Saves each crop to disk and assigns it a public URL via `image_store.py`

**Stage 2 — Gemini 3.5 Flash (vision-based placement)**
- Renders each page to PNG at 120 DPI
- Groups pages into batches (~5 Gemini calls per PDF) to stay within free tier limits
- Sends each batch of page images + a structured prompt to Gemini
- Gemini reads the actual visual layout and decides for each image:
  - `stem` → image belongs to the question text
  - `option` → image illustrates a specific answer option
  - `table_cell` → image lives inside a table within a question
  - `skip` → decorative or unplaceable
- Pure coordinate heuristics are not used for placement because they break on two-column layouts, images above their question stem, and options formatted as `(A)` vs `A.` vs `1)`

**Why this hybrid approach?**
PyMuPDF is fast and precise for cropping. Gemini is used only for placement decisions — the one task where visual understanding genuinely outperforms coordinate math. This keeps cost low while maintaining accuracy.

---

## Project Structure

```
├── digital_parser.py           # Stage 1: image extraction & cropping
├── question_matcher_gemini.py  # Stage 2: question detection + Gemini placement
├── run.py                      # Single-PDF runner (no Redis needed)
│
├── storage/
│   └── image_store.py          # Save images locally or to S3, return URL
│
├── queue/
│   └── worker_task.py          # RQ worker function (full pipeline for one PDF)
│
├── api/
│   └── main.py                 # FastAPI: submit PDFs, poll status, get results
│
├── requirements.txt
├── .env.example
└── README.md
```

---

## Tools & Models Used

| Tool | Purpose |
|---|---|
| `PyMuPDF (fitz)` | PDF parsing, image extraction, page rendering, bounding boxes |
| `Gemini 1.5 Flash` | Vision-based image placement decisions |
| `FastAPI` | REST API for job submission and status polling |
| `Redis + RQ` | Job queue for handling 100+ PDFs concurrently |
| `Pillow` | PIL image handling for Gemini input |
| `boto3` | Optional S3 image storage backend |

---

## Setup & Installation

**1. Clone the repository**
```bash
git clone https://github.com/yourusername/pupiltree-pipeline.git
cd pupiltree-pipeline
```

**2. Create a virtual environment**
```bash
python -m venv .venv
source .venv/bin/activate      # Mac/Linux
.venv\Scripts\activate         # Windows
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Set up environment variables**
```bash
cp .env.example .env
# Open .env and add your GEMINI_API_KEY
```

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com) — no credit card required.

---

## Running the Pipeline

### Option A — Single PDF (no Redis needed)

Create `run.py` in the project root:

```python
import json
from queue.worker_task import process_pdf

result = process_pdf(job_id="test-job-001", pdf_path="your_exam.pdf")
print(json.dumps(result, indent=2))
```

```bash
python run.py
```

Output appears in `./output_jobs/test-job-001/result.json`.
Cropped images appear in `./output_images/test-job-001/`.

---

### Option B — Full Queue (100+ PDFs)

**Start Redis**
```bash
# Mac
brew install redis && redis-server

# Ubuntu / WSL
sudo apt install redis-server && redis-server
```

**Start the API**
```bash
uvicorn api.main:app --reload --port 8000
```

**Start workers** (one terminal per worker; more workers = more concurrency)
```bash
rq worker pdf-queue --url redis://localhost:6379
rq worker pdf-queue --url redis://localhost:6379   # worker 2
rq worker pdf-queue --url redis://localhost:6379   # worker 3
```

**Submit PDFs**
```bash
# Single PDF
curl -X POST http://localhost:8000/submit \
  -F "file=@exam.pdf"
# → {"job_id": "abc-123", "poll_url": "/status/abc-123"}

# Batch of PDFs
curl -X POST http://localhost:8000/submit-batch \
  -F "files=@exam1.pdf" \
  -F "files=@exam2.pdf" \
  -F "files=@exam3.pdf"

# Poll status
curl http://localhost:8000/status/abc-123
# → {"status": "done", "total_questions": 30, ...}

# Download result JSON
curl http://localhost:8000/result/abc-123 -o result.json

# Check queue health
curl http://localhost:8000/health
curl http://localhost:8000/jobs
```

---

## Queue Design & Scaling Plan

### How the Queue Works

```
Client (HTTP)
    │
    ▼
FastAPI  ──────────────────────────────────────────────────────
POST /submit      saves PDF to disk → enqueues job → returns job_id immediately
GET  /status/{id} fetches job state from Redis → queued | started | done | failed
GET  /result/{id} serves output JSON from disk
GET  /images/...  serves cropped images via static files
    │
    ▼
Redis Queue  (pdf-queue)
    │
    ├── Worker 1  →  process_pdf(job_id, pdf_path)
    ├── Worker 2  →  process_pdf(job_id, pdf_path)
    └── Worker N  →  process_pdf(job_id, pdf_path)
```

Each worker processes one PDF at a time. Jobs exceeding 5 minutes are automatically killed and marked failed. Failed jobs are retried once automatically by RQ.

### Scaling for 100 PDFs

| Scenario | Setup |
|---|---|
| Dev / testing | 1 worker, local Redis |
| 10–20 concurrent PDFs | 5 workers, local or managed Redis |
| 100+ PDFs | 20+ workers across multiple machines, Redis Cloud or ElastiCache |

**Backpressure:** Redis holds the queue in memory. If all workers are busy, new jobs wait in the queue — no jobs are dropped. You can inspect queue depth at `GET /health`.

**Idempotency:** Each job gets a UUID. Resubmitting the same PDF creates a new UUID and new job — no deduplication by default (add file hash check to `POST /submit` if needed).

**Retries:** RQ retries failed jobs once automatically. Add `retry=Retry(max=3)` to `enqueue()` for more aggressive retry logic.

**Monitoring:** Run `rq-dashboard` for a live web UI showing queue depth, worker status, and job history:
```bash
pip install rq-dashboard
rq-dashboard --redis-url redis://localhost:6379
# Open http://localhost:9181
```

---

## Schema & Extensions

The output JSON follows the PupilTree target schema with two extensions:

**1. `tableImages[]` on each question (extension)**

The reference schema has no explicit table field. We represent tables with images inside cells as:

```json
{
  "questionText": "Study the table and answer:",
  "tableImages": [
    {
      "url": "http://localhost:8000/images/job-id/page2_img1.png",
      "altText": null,
      "imageType": "table_cell",
      "tableInfo": "row 1, col 2",
      "mappingImageName": "{{IMAGE:img_q7_table_cell}}",
      "figure_bbox": [114.6, 264.9, 300.9, 352.1]
    }
  ]
}
```

**2. `questionType` auto-classification**

Classified from question text using keyword matching:

| Detected pattern | `questionType` |
|---|---|
| "Match the list" / "Column-I" | `MATRIX_MATCH` |
| "Assertion" + "Reason" | `ASSERTION_REASON` |
| "True" + "False", no options | `TRUE_FALSE` |
| No options | `NUMERICAL_INTEGER` |
| Default (has options) | `MCQ_SINGLE` |

**Option keys** are always output as `A | B | C | D` per the schema, even though NEET PDFs use `1) 2) 3) 4)` internally.

**Answer key** is captured from inline `Sol. (2):` annotations and mapped to letter keys (`2 → B`).

---

## Cost Per PDF

Using **Gemini 1.5 Flash** free tier: **$0.00 per PDF**.

Free tier allows 20 requests/day and 1,500 requests/day (varies by account). The pipeline uses ~5 Gemini calls per PDF (pages batched into 5 groups), so the free tier supports ~4 PDFs/day without any cost.

If you upgrade to a paid Gemini key:

| Model | Price | Cost per PDF (~5 calls, ~10 pages) |
|---|---|---|
| Gemini 1.5 Flash | $0.075 / 1M tokens | ~$0.001–0.003 |
| Gemini 1.5 Pro | $1.25 / 1M tokens | ~$0.02–0.05 |

Flash is recommended — accuracy is comparable for structured extraction tasks.

---

## Latency

Measured on a standard laptop with Gemini 1.5 Flash:

| Stage | Time |
|---|---|
| PDF open + image extraction (PyMuPDF) | 3–8s |
| Image storage | 1–2s |
| Text extraction + question detection | 1–3s |
| Gemini Vision (5 batched calls) | 20–45s |
| JSON generation | <1s |
| **Total** | **~30–60 seconds** |

Well within the 1.5 minute requirement. Gemini call time dominates — reducing `TARGET_CALLS` from 5 to 3 cuts this further at a small accuracy trade-off.

---

## Known Limitations

**NEET-specific tuning**
The question detection regex (`x < 50` margin, `1)/2)/3)/4)` options) is tuned to NEET exam PDFs. Other exam formats (JEE, UPSC, SAT) may need regex adjustments — the architecture is general, the patterns are not.

**Math / LaTeX rendering**
LaTeX embedded as vector text (e.g. fractions rendered with `\uf0e6` private-use glyphs) is filtered as unicode garbage. True LaTeX preservation requires a separate MathML/LaTeX OCR model. Currently such expressions are cropped as images where possible.

**Scanned PDFs**
Scanned PDFs are handled by Gemini Vision (it reads the page image directly), but text extraction from `get_text()` will return nothing — question detection falls back entirely to Gemini, increasing API calls and cost.

**Table structure**
Text-based tables (grid lines + text cells) are not parsed into structured `rows[]`/`cells[]` — they appear as concatenated question text. Only images embedded inside tables are captured via `tableImages[]`. Full table parsing would require a dedicated table extraction model.

**Gemini free tier rate limits**
20 requests/day on the free tier limits throughput to ~4 PDFs/day. Use a paid key for production.

**Image deduplication is page-scoped**
The same image appearing on two different pages is kept (intentional). The same image appearing twice on the same page is deduplicated by MD5 hash.
