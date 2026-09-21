import os
import json
import requests
from google import genai
from dotenv import load_dotenv

load_dotenv()

OCR_API = "http://192.168.1.34:8007/api/extract"

DOCUMENT = "INDOS (20).pdf"

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)


def extract_document(file_path):
    with open(file_path, "rb") as f:
        response = requests.post(
            OCR_API,
            files={"file": f}
        )

    response.raise_for_status()
    return response.json()


def send_to_gemini(ocr_result):
    prompt = """
You are a part of a seaman' document validation system, you are the part that discovers the validation sources for some given document. 

Analyze the OCR output below.

Extract and organize:
1. Document type
2. Issuing organization
3. Country
4. Document holder name
5. Date of birth
6. Document number
7. Issue date
8. Expiry date, if present
9. Other important identifying fields
10. Any suspicious, missing, or inconsistent information

Do not invent information.
If a field is missing, return null.

OCR OUTPUT:
""" + json.dumps(ocr_result, ensure_ascii=False, indent=2)

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )

    return response.text


def main():
    print("Running OCR...")

    ocr_result = extract_document(DOCUMENT)

    print("OCR completed.")
    print("Sending OCR result to Gemini...")

    analysis = send_to_gemini(ocr_result)

    print("\n--- GEMINI OUTPUT ---\n")
    print(analysis)


if __name__ == "__main__":
    main()