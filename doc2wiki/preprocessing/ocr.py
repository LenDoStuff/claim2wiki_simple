"""Save Azure Read's searchable PDF, resolving its service through a Foundry project."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeOutputOption
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
from pypdf import PdfReader

from ..wiki.state import write_json


def resource_endpoint(project_endpoint: str, credential) -> str:
    """Resolve the named project connection without fetching any stored API keys."""
    url = urlsplit(project_endpoint.strip())
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not re.fullmatch(r"/api/projects/[^/]+/?", url.path)
    ):
        raise ValueError("Set FOUNDRY_PROJECT_ENDPOINT to https://RESOURCE/api/projects/PROJECT.")
    name = os.getenv("FOUNDRY_DOCUMENT_INTELLIGENCE_CONNECTION", "").strip()
    if not name:
        raise ValueError(
            "Set FOUNDRY_DOCUMENT_INTELLIGENCE_CONNECTION to your OCR connection name."
        )
    with AIProjectClient(endpoint=project_endpoint, credential=credential) as project:
        connection = project.connections.get(name, include_credentials=False)
    target = urlsplit(connection.target)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.path not in {"", "/"}
        or target.query
        or target.fragment
    ):
        raise ValueError(
            "The OCR connection target must be the Document Intelligence resource URL."
        )
    return connection.target.rstrip("/")


def searchable_pdf(source: Path, output: Path) -> Path:
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("OCR input must be an existing PDF file.")
    if source == output or output.suffix.lower() != ".pdf":
        raise ValueError("Choose a separate .pdf output; the original PDF is kept intact.")
    if source.stat().st_size > 500_000_000:
        raise ValueError("Azure Read accepts at most 500 MB per PDF. Split this file first.")
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    state_path = output.with_suffix(".ocr.json")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["source_sha256"] != source_hash or state["model"] != "prebuilt-read":
            raise ValueError("OCR input changed. Choose a new output PDF.")
        if (
            not output.is_file()
            or hashlib.sha256(output.read_bytes()).hexdigest() != state["output_sha256"]
        ):
            raise ValueError("The saved OCR PDF is missing or changed. Choose a new output PDF.")
        print(f"Already OCR processed: {output}", flush=True)
        return output
    if output.exists():
        raise ValueError(f"Output already exists without an OCR record: {output}")
    reader = PdfReader(source)
    if reader.is_encrypted:
        raise ValueError("Supply an unlocked PDF before running OCR.")
    page_count = len(reader.pages)
    if not 1 <= page_count <= 2_000:
        raise ValueError("Azure Read accepts between 1 and 2,000 PDF pages per request.")
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"OCR: {source.name}, {page_count} pages using prebuilt-read", flush=True)
    with (
        AzureCliCredential() as credential,
        DocumentIntelligenceClient(
            endpoint=resource_endpoint(os.getenv("FOUNDRY_PROJECT_ENDPOINT", ""), credential),
            credential=credential,
            api_version="2024-11-30",
            retry_total=0,
        ) as client,
    ):
        with source.open("rb") as stream:
            poller = client.begin_analyze_document(
                "prebuilt-read",
                body=stream,
                output=[AnalyzeOutputOption.PDF],
            )
        result = poller.result()
        returned_pages = [page.page_number for page in result.pages or []]
        if returned_pages != list(range(1, page_count + 1)):
            raise ValueError(
                "Azure did not return all pages. Use the paid tier; the free tier reads two pages."
            )
        operation_id = poller.details["operation_id"]
        temporary = output.with_suffix(".pdf.tmp")
        with temporary.open("wb") as stream:
            stream.writelines(
                client.get_analyze_result_pdf(model_id="prebuilt-read", result_id=operation_id)
            )
    # Do not publish a truncated or unusable download as the completed result.
    downloaded = PdfReader(temporary)
    if len(downloaded.pages) != page_count:
        raise ValueError("Searchable PDF page count differs from the source PDF.")
    empty_pages = [
        i for i, page in enumerate(downloaded.pages, 1) if not page.extract_text().strip()
    ]
    if len(empty_pages) == page_count:
        raise ValueError("Azure's PDF has no extractable text; inspect the source scan.")
    temporary.replace(output)
    write_json(
        state_path,
        {
            "source": str(source),
            "source_sha256": source_hash,
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "model": "prebuilt-read",
            "api_version": "2024-11-30",
            "operation_id": operation_id,
            "page_count": page_count,
            "empty_text_pages": empty_pages,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    print(f"Searchable PDF: {output}", flush=True)
    if empty_pages:
        print(f"Review pages with no extracted text: {empty_pages}", flush=True)
    return output
