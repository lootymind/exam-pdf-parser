import fitz  # PyMuPDF
import os
import hashlib
import json

# ==================== HYBRID VISUAL EXTRACTION ====================

def get_all_visual_bboxes(page):
    """
    Find bounding boxes for embedded images AND vector graphics.
    Filters out decorative lines, borders, and tiny elements.
    """
    all_bboxes = []
    page_width = page.rect.width
    page_height = page.rect.height

    # --- Method 1: Embedded raster images ---
    image_list = page.get_images(full=True)
    for img in image_list:
        try:
            bbox = page.get_image_bbox(img)
            if bbox and not bbox.is_empty:
                all_bboxes.append({
                    "type": "embedded",
                    "bbox": bbox,
                    "xref": img[0]
                })
        except Exception:
            # Fallback: look for image blocks in text dict
            for block in page.get_text("dict")["blocks"]:
                if block["type"] == 1:  # image block
                    all_bboxes.append({
                        "type": "embedded",
                        "bbox": fitz.Rect(block["bbox"]),
                        "xref": img[0]
                    })
                    break

    # --- Method 2: Vector drawings (graphs, diagrams, geometric figures) ---
    try:
        drawings = page.get_cdrawings()
        for drawing in drawings:
            rect = drawing.get("rect")
            if not rect or rect.is_empty:
                continue

            w, h = rect.width, rect.height
            area = w * h
            aspect = w / h if h > 0 else 0

            # Skip:
            # - tiny elements (lines, underlines, borders)
            # - full-page-width elements (likely decorative dividers)
            # - extreme aspect ratios (horizontal/vertical lines)
            if area < 5000:
                continue
            if w > page_width * 0.90:
                continue
            if not (0.15 < aspect < 10):
                continue

            all_bboxes.append({
                "type": "vector",
                "bbox": rect,
                "xref": None
            })
    except Exception:
        pass

    return all_bboxes


def merge_overlapping_bboxes(bboxes, tolerance=5):
    """
    Merge bboxes that overlap or are very close together.
    Prevents saving multiple crops for the same logical figure.
    """
    if not bboxes:
        return []

    merged = []
    used = [False] * len(bboxes)

    for i, a in enumerate(bboxes):
        if used[i]:
            continue
        current = fitz.Rect(a["bbox"])
        current_type = a["type"]
        used[i] = True

        for j, b in enumerate(bboxes):
            if used[j] or i == j:
                continue
            expanded = fitz.Rect(
                current.x0 - tolerance,
                current.y0 - tolerance,
                current.x1 + tolerance,
                current.y1 + tolerance
            )
            if expanded.intersects(b["bbox"]):
                current = current | b["bbox"]  # union
                used[j] = True

        merged.append({
            "type": current_type,
            "bbox": current,
            "xref": a.get("xref")
        })

    return merged


def render_and_save_images(doc, output_folder="cleaned_images", top_margin=70, bottom_margin=70, dpi=200):
    """
    Render all visual areas and save as PNGs.
    Uses 200 DPI for sharp crops (important for graphs with fine labels).
    Adds small padding around each crop so nothing gets clipped.
    """
    os.makedirs(output_folder, exist_ok=True)

    saved_images = []
    image_counter = 0
    zoom = dpi / 72
    PADDING = 4  # points of padding around each crop

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_rect = page.rect
        page_height = page_rect.height

        visual_bboxes = get_all_visual_bboxes(page)
        visual_bboxes = merge_overlapping_bboxes(visual_bboxes)

        for visual in visual_bboxes:
            bbox = visual["bbox"]

            # Skip headers and footers
            if bbox.y0 < top_margin or bbox.y1 > page_height - bottom_margin:
                continue

            # Skip tiny elements
            if bbox.width < 40 or bbox.height < 40:
                continue

            # Add padding, clamped to page bounds
            padded = fitz.Rect(
                max(bbox.x0 - PADDING, page_rect.x0),
                max(bbox.y0 - PADDING, page_rect.y0),
                min(bbox.x1 + PADDING, page_rect.x1),
                min(bbox.y1 + PADDING, page_rect.y1),
            )

            try:
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, clip=padded)

                image_path = os.path.join(output_folder, f"page{page_num}_img{image_counter}.png")
                pix.save(image_path)
                pix = None

                # Store bbox in 0–1000 normalised grid (as schema requires)
                norm_bbox = [
                    round(bbox.x0 / page_rect.width * 1000, 1),
                    round(bbox.y0 / page_rect.height * 1000, 1),
                    round(bbox.x1 / page_rect.width * 1000, 1),
                    round(bbox.y1 / page_rect.height * 1000, 1),
                ]

                saved_images.append({
                    "page": page_num,
                    "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                    "norm_bbox": norm_bbox,         # 0-1000 grid for schema
                    "page_width": page_rect.width,
                    "page_height": page_rect.height,
                    "path": image_path,
                    "type": visual["type"]
                })
                image_counter += 1

            except Exception as e:
                print(f"  Crop error page {page_num}: {e}")

    return saved_images


def filter_duplicate_images(images):
    """
    Remove duplicate images by content hash.
    Only deduplicates within the same page to avoid
    deleting legitimately repeated figures on different pages.
    """
    unique = []
    # key = (page, hash) so same image on different pages is kept
    seen = {}

    for img in images:
        try:
            with open(img["path"], "rb") as f:
                img_hash = hashlib.md5(f.read()).hexdigest()

            key = (img["page"], img_hash)
            if key not in seen:
                seen[key] = True
                unique.append(img)
            else:
                os.remove(img["path"])
        except Exception:
            unique.append(img)

    return unique


# ==================== MAIN ====================

if __name__ == "__main__":
    PDF_PATH = "test_pdf.pdf"
    TOP_MARGIN = 70
    BOTTOM_MARGIN = 70
    IMAGE_OUTPUT = "cleaned_images"

    print(f"Opening: {PDF_PATH}")
    print("=" * 60)

    doc = fitz.open(PDF_PATH)

    saved_images = render_and_save_images(doc, IMAGE_OUTPUT, TOP_MARGIN, BOTTOM_MARGIN, dpi=200)
    print(f"\nImages rendered: {len(saved_images)}")

    saved_images = filter_duplicate_images(saved_images)
    print(f"After duplicate removal: {len(saved_images)}")

    with open("image_metadata.json", "w") as f:
        json.dump(saved_images, f, indent=2)

    print(f"\n✅ Images saved to: {IMAGE_OUTPUT}/")
    print(f"✅ Metadata saved to: image_metadata.json")

    doc.close()
