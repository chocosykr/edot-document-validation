import requests
from config import OCR_API_URL


def extract_document(file_path: str) -> dict:
    """
    Send a document to the OCR API and return its JSON response.
    """

    with open(file_path, "rb") as file:
        response = requests.post(
            OCR_API_URL,
            files={"file": file},
            timeout=120
        )

    response.raise_for_status()

    return response.json()