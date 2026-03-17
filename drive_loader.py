"""Load documents from Google Drive folder (PDFs and DOCX files)."""

import io
import json
import os
import logging
import tempfile
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PyPDF2 import PdfReader
from docx import Document

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

SUPPORTED_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    # Google Docs will be exported as DOCX
    "application/vnd.google-apps.document": "gdoc",
}


def _get_drive_service(service_account_info: str):
    """Create authenticated Google Drive service."""
    # service_account_info can be a file path or JSON string
    if os.path.isfile(service_account_info):
        creds = service_account.Credentials.from_service_account_file(
            service_account_info, scopes=SCOPES
        )
    else:
        info = json.loads(service_account_info)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    return build("drive", "v3", credentials=creds)


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from PDF bytes."""
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            pages.append(f"--- Strana {i + 1} ---\n{text}")
    return "\n\n".join(pages)


def _extract_docx_text(file_bytes: bytes) -> str:
    """Extract text from DOCX bytes."""
    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def _download_file(service, file_id: str, mime_type: str) -> bytes:
    """Download a file from Google Drive."""
    if mime_type == "application/vnd.google-apps.document":
        # Export Google Doc as DOCX
        request = service.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    else:
        request = service.files().get_media(fileId=file_id)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def load_documents(
    folder_id: Optional[str] = None,
    service_account_path: Optional[str] = None,
) -> list[dict]:
    """
    Load all supported documents from a Google Drive folder.

    Returns a list of dicts: [{"name": "filename", "content": "extracted text"}, ...]
    """
    folder_id = folder_id or os.environ["GOOGLE_DRIVE_FOLDER_ID"]
    from secret_manager import get_secret
    service_account_path = service_account_path or get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")

    service = _get_drive_service(service_account_path)

    # List files in the folder
    query = f"'{folder_id}' in parents and trashed = false"
    results = (
        service.files()
        .list(q=query, fields="files(id, name, mimeType)", pageSize=100)
        .execute()
    )
    files = results.get("files", [])

    documents = []
    for f in files:
        mime = f["mimeType"]
        if mime not in SUPPORTED_MIME_TYPES:
            logger.info(f"Skipping unsupported file: {f['name']} ({mime})")
            continue

        logger.info(f"Loading: {f['name']}")
        try:
            file_bytes = _download_file(service, f["id"], mime)

            if mime == "application/pdf":
                text = _extract_pdf_text(file_bytes)
            else:
                # DOCX or Google Doc exported as DOCX
                text = _extract_docx_text(file_bytes)

            if text.strip():
                documents.append({"name": f["name"], "content": text})
                logger.info(f"  -> {len(text)} characters extracted")
            else:
                logger.warning(f"  -> No text extracted from {f['name']}")
        except Exception as e:
            logger.error(f"  -> Error loading {f['name']}: {e}")

    logger.info(f"Loaded {len(documents)} documents total")
    return documents
