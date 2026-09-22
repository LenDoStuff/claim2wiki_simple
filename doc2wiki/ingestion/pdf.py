"""Read the complete text of each PDF, keeping physical page numbers."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass
class Source:
    path: Path
    name: str
    id: str
    sha256: str
    pages: list[str]

    @property
    def text(self) -> str:
        return "\n\n".join(
            f"--- PDF page {number} ---\n{text}" for number, text in enumerate(self.pages, 1)
        )

    @property
    def summary_path(self) -> str:
        return f"sources/{self.id}.md"


def read_pdf(path: Path, name: str) -> Source:
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError(f"{name}: password-protected PDFs are not supported.")
    pages = [page.extract_text(extraction_mode="layout").strip() for page in reader.pages]
    if not pages or not any(pages):
        raise ValueError(f"{name}: no extractable text. OCR the PDF before importing it.")
    empty = [str(i) for i, text in enumerate(pages, 1) if not text]
    if empty:
        raise ValueError(
            f"{name}: pages {', '.join(empty)} have no extractable text. "
            "OCR scanned pages or remove genuinely blank pages before importing."
        )
    slug = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:60].rstrip("-") or "document"
    # Include the relative path so subdirectories can contain the same filename.
    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return Source(
        path, name, f"{slug}-{suffix}", hashlib.sha256(path.read_bytes()).hexdigest(), pages
    )
