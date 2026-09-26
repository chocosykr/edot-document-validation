import json
import requests
import io
from pypdf import PdfReader, PdfWriter

OCR_API = "http://192.168.1.34:8007/api/extract"
DOCUMENT = "InputFiles/2O - HEIN HTET (AIO).pdf"

def extract_page(page_bytes, page_num):
    # Send the in-memory PDF page to the OCR API
    # We provide a dummy filename so the API recognizes it as a PDF
    files = {
        "file": (f"page_{page_num}.pdf", page_bytes, "application/pdf")
    }
    
    response = requests.post(OCR_API, files=files)
    response.raise_for_status()
    
    return response.json()

def main():
    print(f"Opening '{DOCUMENT}'...")
    
    try:
        reader = PdfReader(DOCUMENT)
        num_pages = len(reader.pages)
        print(f"Document has {num_pages} page(s).\n")
        
        all_ocr_results = []
        
        for i in range(num_pages):
            page_num = i + 1
            print(f"Processing page {page_num}/{num_pages}...")
            
            # Create a new PDF containing only the current page
            writer = PdfWriter()
            writer.add_page(reader.pages[i])
            
            # Save the single page to an in-memory bytes buffer
            page_bytes = io.BytesIO()
            writer.write(page_bytes)
            page_bytes.seek(0) # Reset buffer pointer to the beginning
            
            # Send the single page to the OCR API
            ocr_result = extract_page(page_bytes, page_num)
            
            # Store the result with its corresponding page number
            all_ocr_results.append({
                "page": page_num,
                "data": ocr_result
            })
            
        print("\n--- ALL PAGES PROCESSED ---\n")
        print(json.dumps(all_ocr_results, ensure_ascii=False, indent=2))
        
    except FileNotFoundError:
        print(f"Error: The file '{DOCUMENT}' was not found.")
    except requests.exceptions.RequestException as e:
        print(f"Network error during OCR API call: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()