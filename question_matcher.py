
 
import fitz
import json
import re
import os
import time
import math
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from PIL import Image
 
# ==================== CONFIGURE GEMINI ====================
 
genai.configure(api_key='Your-Gemini-api-key')
model = genai.GenerativeModel("gemini-3.5-flash")
 
TARGET_CALLS = 5
REQUEST_DELAY = 5
 
 
# ==================== RETRY WRAPPER ====================
 
def call_gemini_with_retry(content, max_retries=4):
    for attempt in range(max_retries):
        try:
            response = model.generate_content(content)
            return response
        except ResourceExhausted as e:
            wait = 15 * (2 ** attempt)
            retry_match = re.search(r'retry in (\d+)', str(e))
            if retry_match:
                wait = int(retry_match.group(1)) + 2
            if attempt < max_retries - 1:
                print(f"  ⏳ Rate limited. Waiting {wait}s (retry {attempt+1}/{max_retries-1})...")
                time.sleep(wait)
            else:
                print("  ❌ Max retries reached. Skipping batch.")
                raise
        except Exception as e:
            print(f"  ❌ Gemini error: {e}")
            raise
 
 
# ==================== TEXT EXTRACTION ====================
 
def is_watermark(text, x, page_width):
    """Detect RCC** watermark spans."""
    return "RCC" in text or text.strip() in ["R", "C", "RC"]
 
 
def is_solution(text):
    """Detect inline solution lines like 'Sol. (2):'"""
    return bool(re.match(r'^Sol\.?\s*\(', text.strip()))
 
 
def extract_solution_key(text):
    """Extract answer key number from 'Sol. (2):' → 'A' (mapped to letter)"""
    match = re.search(r'Sol\.?\s*\((\d)\)', text)
    if match:
        return NEET_TO_LETTER.get(match.group(1), match.group(1))
    return None
 
 
# NEET option number → schema letter key
NEET_TO_LETTER = {"1": "A", "2": "B", "3": "C", "4": "D"}
 
 
def classify_question_type(question_text, options):
    """Infer questionType from text content."""
    text = question_text.lower()
    if re.search(r'match the|column[\s\-]*i\b', text):
        return "MATRIX_MATCH"
    if "assertion" in text and "reason" in text:
        return "ASSERTION_REASON"
    if re.search(r'\btrue\b.*\bfalse\b|\bfalse\b.*\btrue\b', text) and not options:
        return "TRUE_FALSE"
    if not options:
        return "NUMERICAL_INTEGER"
    return "MCQ_SINGLE"
 
 
def is_subject_header(text):
    """Detect subject section headers like 'Dual Nature of Radiation & Matter'"""
    # These appear at x≈67-80, are title-case, and don't start with a number
    return (
        len(text) > 10
        and not re.match(r'^\d', text)
        and text[0].isupper()
        and not re.match(r'^(Match|Which|What|How|Find|Calculate|A |B |C |D |The |In |An |If )', text)
    )
 
 
def get_cleaned_text_blocks(pdf_path, top_margin=55, bottom_margin=55):
    """
    Extract text spans from all pages.
    Filters: watermarks, solutions, page headers/footers.
    Preserves: question numbers, question text, option numbers, table content.
    """
    doc = fitz.open(pdf_path)
    blocks = []
 
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_height = page.rect.height
        page_width = page.rect.width
        text_dict = page.get_text("dict")
 
        for block in text_dict["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    y0 = span["bbox"][1]
                    x0 = span["bbox"][0]
 
                    # Skip headers/footers
                    if y0 < top_margin or y0 > page_height - bottom_margin:
                        continue
                    # Skip empty or single chars
                    if len(text) <= 1:
                        continue
                    # Skip watermarks
                    if is_watermark(text, x0, page_width):
                        continue
                    # Capture solution key then skip the span
                    if is_solution(text):
                        blocks.append({
                            "page": page_num,
                            "text": "",
                            "y": y0,
                            "x": x0,
                            "bbox": list(span["bbox"]),
                            "font_size": span["size"],
                            "page_width": page_width,
                            "page_height": page_height,
                            "_solution_key": extract_solution_key(text),
                        })
                        continue
                    # Skip unicode garbage (math symbols rendered as private use area)
                    if all(ord(c) > 0xF000 for c in text):
                        continue
 
                    blocks.append({
                        "page": page_num,
                        "text": text,
                        "y": y0,
                        "x": x0,
                        "bbox": list(span["bbox"]),
                        "font_size": span["size"],
                        "page_width": page_width,
                        "page_height": page_height,
                    })
 
    doc.close()
    return blocks
 
 
# ==================== QUESTION DETECTION ====================
 
def detect_questions(text_blocks):
    """
    NEET-specific question detection.
 
    Key observations from text dump:
      - Question numbers: "7." "8." alone, x≈31
      - Question text: starts at x≈54, same Y as the number
      - Options: "1)" "2)" "3)" "4)" at x≈54
      - Option text: at x≈68, same Y as option number
      - Tables: multiple spans at same Y — grouped into rows
    """
    questions = []
    current_q = None
 
    # NEET question number: "7." alone OR "7." followed by text, at left margin (x < 50)
    Q_NUM_PATTERN = re.compile(r'^(\d{1,3})\.$')          # "7." alone
    Q_NUM_INLINE  = re.compile(r'^(\d{1,3})\.\s+\S')      # "7. Match..." inline
 
    # NEET options: "1)" "2)" "3)" "4)"
    OPT_PATTERN = re.compile(r'^([1-4])\)\s*')
 
    # Group spans by (page, y_bucket) to reconstruct table rows
    Y_TOLERANCE = 8   # spans within 8pt of each other are on the same "line"
 
    def flush_question():
        if current_q:
            questions.append(current_q)
 
    for block in text_blocks:
        text = block["text"]
        y    = block["y"]
        x    = block["x"]
        page = block["page"]
 
        # --- Detect question number ---
        q_match = Q_NUM_PATTERN.match(text) if x < 50 else None
        q_inline = Q_NUM_INLINE.match(text)  if x < 50 else None
 
        if q_match or q_inline:
            pattern = q_match or q_inline
            q_num = int(pattern.group(1))
            last_num = questions[-1]["number"] if questions else 0
 
            # Accept sequential questions (allow gaps up to 10 for subject changes)
            if q_num > last_num and q_num <= last_num + 10:
                flush_question()
                # Inline: number + text in same span
                q_text = text if q_inline else ""
                current_q = {
                    "number": q_num,
                    "text": q_text,
                    "page": page,
                    "y": y,
                    "x": x,
                    "bbox": block["bbox"],
                    "options": [],
                    "images": [],
                    "table_images": [],
                    "_last_y": y,
                }
                continue
 
        # --- Capture solution key from Sol. block ---
        if block.get("_solution_key") and current_q:
            current_q["answer_key"] = block["_solution_key"]
            continue
 
        if not block.get("_solution_key") and not current_q:
            continue
        if not current_q:
            continue
        # --- Detect NEET options "1)" "2)" "3)" "4)" ---
        opt_match = OPT_PATTERN.match(text)
        if opt_match and x < 70:
            opt_num = opt_match.group(1)
            opt_text = text[len(opt_match.group(0)):].strip()
            existing = [o["key"] for o in current_q["options"]]
            if opt_num not in existing:
                current_q["options"].append({
                    "key": opt_num,
                    "text": opt_text,
                    "y": y,
                    "x": x,
                    "bbox": block["bbox"],
                    "image": None,
                })
                current_q["_last_y"] = y
                continue
 
        # --- Append option continuation text ---
        # If x≈68 and Y matches last option's Y → this is option text continuation
        if current_q["options"]:
            last_opt = current_q["options"][-1]
            if abs(y - last_opt["y"]) < Y_TOLERANCE and x > 60:
                last_opt["text"] += " " + text
                continue
 
        # --- Append to question text ---
        current_q["text"] += " " + text
        current_q["_last_y"] = y
 
    flush_question()
 
    # Clean up internal keys
    for q in questions:
        q.pop("_last_y", None)
 
    return questions
 
 
# ==================== BATCH PAGE RENDERING ====================
 
def render_page_pil(pdf_path, page_num, dpi=120):
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img
 
 
def build_page_batches(pages_with_images, target_calls=TARGET_CALLS):
    if not pages_with_images:
        return []
    pages = sorted(pages_with_images)
    per_batch = max(1, math.ceil(len(pages) / target_calls))
    return [pages[i:i+per_batch] for i in range(0, len(pages), per_batch)]
 
 
# ==================== GEMINI VISION PLACEMENT ====================
 
def gemini_place_images_batch(pdf_path, batch_pages, questions, images):
    batch_images   = {p: [img for img in images   if img["page"] == p] for p in batch_pages}
    batch_questions= {p: [q   for q   in questions if q["page"]   == p] for p in batch_pages}
 
    total_imgs = sum(len(v) for v in batch_images.values())
    if total_imgs == 0:
        return []
 
    content = []
    img_index_map = {}
    prompt_sections = []
 
    for page_num in batch_pages:
        pil_img   = render_page_pil(pdf_path, page_num)
        content.append(pil_img)
 
        page_imgs = batch_images.get(page_num, [])
        page_qs   = batch_questions.get(page_num, [])
 
        q_lines  = [f"  Q{q['number']} (options: {[o['key'] for o in q.get('options',[])]})"
                    for q in page_qs]
        img_lines = []
        for i, img in enumerate(page_imgs):
            key = f"P{page_num}_IMG_{i}"
            img_index_map[key] = img
            img_lines.append(f"  {key}: bbox={img['bbox']}")
 
        if page_imgs:
            section  = f"=== PAGE {page_num} ===\n"
            section += ("Questions:\n" + "\n".join(q_lines) + "\n") if q_lines else ""
            section += "Images:\n" + "\n".join(img_lines)
            prompt_sections.append(section)
 
    prompt = f"""You are analyzing pages from a NEET exam PDF.
Options in NEET are numbered 1) 2) 3) 4) (not A B C D).
Each PIL image above corresponds to a page in order.
 
{chr(10).join(prompt_sections)}
 
For EVERY image, decide placement:
- 'stem'       → part of the question stem / figure referenced in question text
- 'option'     → illustrates a specific numbered option (1/2/3/4)
- 'table_cell' → inside a table within a question
- 'skip'       → decorative, watermark, logo
 
Return ONLY valid JSON, no markdown:
{{
  "placements": [
    {{"image_id": "P0_IMG_0", "placement": "stem", "question_number": 7, "option_key": null, "table_info": null}},
    {{"image_id": "P0_IMG_1", "placement": "option", "question_number": 8, "option_key": "3", "table_info": null}}
  ]
}}"""
 
    content.append(prompt)
    response = call_gemini_with_retry(content)
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
 
    try:
        data = json.loads(raw)
        placements = data.get("placements", [])
    except json.JSONDecodeError:
        print(f"  ⚠️  Invalid JSON from Gemini for batch {batch_pages}")
        return []
 
    results = []
    for p in placements:
        img = img_index_map.get(p.get("image_id", ""))
        if not img:
            continue
        results.append({
            "image_path": img["path"],
            "norm_bbox":  img.get("norm_bbox"),
            "placement":  p.get("placement", "skip"),
            "question_number": p.get("question_number"),
            "option_key": p.get("option_key"),      # "1"/"2"/"3"/"4" for NEET
            "table_info": p.get("table_info"),
        })
 
    return results
 
 
def match_images_to_questions(pdf_path, questions, images):
    pages_with_images = list(set(img["page"] for img in images))
    batches = build_page_batches(pages_with_images, TARGET_CALLS)
 
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()
 
    print(f"  {total_pages} pages total, {len(pages_with_images)} have images")
    print(f"  → {len(batches)} Gemini calls (target: {TARGET_CALLS})")
 
    q_by_num = {q["number"]: q for q in questions}
 
    for i, batch_pages in enumerate(batches):
        print(f"\n  Batch {i+1}/{len(batches)}: pages {batch_pages} → Gemini...")
        try:
            placements = gemini_place_images_batch(pdf_path, batch_pages, questions, images)
        except Exception:
            print(f"  ⚠️  Batch {i+1} failed, continuing...")
            continue
 
        for p in placements:
            placement = p["placement"]
            q_num     = p["question_number"]
            opt_key   = p["option_key"]
 
            if placement == "skip" or q_num is None:
                continue
            target_q = q_by_num.get(q_num)
            if not target_q:
                continue
 
            image_entry = {
                "url": p["image_path"],
                "norm_bbox": p["norm_bbox"],
                "mappingImageName": f"{{{{IMAGE:img_q{q_num}_{placement}}}}}",
            }
 
            if placement == "stem":
                target_q["images"].append(image_entry)
                print(f"    ✅ IMG → Q{q_num} stem")
            elif placement == "option" and opt_key:
                for opt in target_q["options"]:
                    if str(opt["key"]) == str(opt_key):
                        opt["image"] = image_entry
                        print(f"    ✅ IMG → Q{q_num} option {opt_key}")
                        break
            elif placement == "table_cell":
                target_q["table_images"].append({**image_entry, "table_info": p.get("table_info")})
                print(f"    ✅ IMG → Q{q_num} table cell")
 
        if i < len(batches) - 1:
            time.sleep(REQUEST_DELAY)
 
    return questions
 
 
# ==================== JSON OUTPUT ====================
 
def generate_json(questions, output_path="output/result.json"):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    output = {"total_questions": len(questions), "questions": []}
 
    for q in questions:
        stem_images  = q.get("images", [])
        table_images = q.get("table_images", [])
        options_raw  = q.get("options", [])
        answer_key   = q.get("answer_key")   # e.g. "B" (already mapped to letter)
 
        q_json = {
            # --- Core schema fields ---
            "questionText":   q["text"].strip(),
            "questionType":   classify_question_type(q["text"], options_raw),
            "hasImage":       len(stem_images) > 0,
            "answer": {
                "key":         answer_key,
                "explanation": ""          # not available in this PDF
            },
            # --- Images ---
            "imageDetails": [{
                "url":              img["url"],
                "altText":          None,
                "imageType":        "question",
                "mappingImageName": img.get("mappingImageName", ""),
                "figure_bbox":      img.get("norm_bbox"),
            } for img in stem_images],
            # --- Tables (schema extension, documented in README) ---
            "tableImages": [{
                "url":              img["url"],
                "altText":          None,
                "imageType":        "table_cell",
                "tableInfo":        img.get("table_info"),
                "mappingImageName": img.get("mappingImageName", ""),
                "figure_bbox":      img.get("norm_bbox"),
            } for img in table_images],
            # --- Options ---
            "options": [],
            # --- Optional fields ---
            "page": q["page"],
        }
 
        for opt in options_raw:
            img        = opt.get("image")
            letter_key = NEET_TO_LETTER.get(str(opt["key"]), opt["key"])  # 1→A, 2→B etc.
            q_json["options"].append({
                "key":        letter_key,
                "optionType": "text_and_image" if img else "text",
                "text":       opt["text"] or None,
                "imageDetails": {
                    "url":              img["url"],
                    "altText":          None,
                    "imageType":        "option",
                    "mappingImageName": img.get("mappingImageName", ""),
                    "figure_bbox":      img.get("norm_bbox"),
                } if img else None,
            })
 
        output["questions"].append(q_json)
 
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
 
    return output
 
 
# ==================== MAIN ====================
 
if __name__ == "__main__":
    PDF_PATH = "test_pdf.pdf"
 
    print("NEET PDF → Structured JSON (Gemini Batched)")
    print("=" * 60)
 
    try:
        with open("image_metadata.json", "r") as f:
            images = json.load(f)
        print(f"Loaded {len(images)} images")
    except FileNotFoundError:
        print("❌ No image_metadata.json found. Run digital_parser.py first.")
        exit(1)
 
    text_blocks = get_cleaned_text_blocks(PDF_PATH)
    print(f"Extracted {len(text_blocks)} text blocks")
 
    questions = detect_questions(text_blocks)
    print(f"Found {len(questions)} questions")
    for q in questions[:5]:
        print(f"  Q{q['number']}: {q['text'][:70]}...")
        print(f"    Options: {[o['key'] for o in q['options']]}")
 
    print("\nMatching images via Gemini Vision (batched)...")
    questions = match_images_to_questions(PDF_PATH, questions, images)
 
    result = generate_json(questions)
 
    stem_total = sum(len(q.get("images", [])) for q in questions)
    opt_total  = sum(1 for q in questions for o in q.get("options", []) if o.get("image"))
    tbl_total  = sum(len(q.get("table_images", [])) for q in questions)
 
    print(f"\n✅ JSON saved to output/result.json")
    print(f"\nSummary:")
    print(f"  Total questions   : {len(questions)}")
    print(f"  Stem images       : {stem_total}")
    print(f"  Option images     : {opt_total}")
    print(f"  Table-cell images : {tbl_total}")
    print(f"  Gemini API calls  : ~{TARGET_CALLS} (batched)")