import fitz
import json

def view_cleaned_text(pdf_path, top_margin=70, bottom_margin=70):
    """
    Extract and display cleaned text (no watermarks, no headers/footers).
    """
    doc = fitz.open(pdf_path)
    
    all_text = []
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_height = page.rect.height
        
        text_dict = page.get_text("dict")
        
        page_text = []
        
        for block in text_dict["blocks"]:
            if block["type"] == 0:  # text block
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        y0 = span["bbox"][1]
                        
                        # Skip header/footer
                        if y0 < top_margin or y0 > page_height - bottom_margin:
                            continue
                        
                        # Skip RCC watermark (if still present)
                        if "RCC" in text :
                            continue
                        
                        if text:
                            page_text.append(text)
        
        if page_text:
            all_text.append({
                "page": page_num + 1,
                "text": " ".join(page_text)
            })
    
    doc.close()
    
    # Print output
    print("\n" + "="*60)
    print("CLEANED TEXT EXTRACTION")
    print("="*60)
    
    for page in all_text:
        print(f"\n--- PAGE {page['page']} ---")
        print(page['text'][:500])  # First 500 chars
        if len(page['text']) > 500:
            print("  ... (truncated)")
    
    return all_text

# Run it
result = view_cleaned_text("test_pdf.pdf", top_margin=70, bottom_margin=70)