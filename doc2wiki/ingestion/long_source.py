"""Semantic chunks and a resumable rolling digest for unusually large PDFs."""

import hashlib
import json
import re
from pathlib import Path

from ..llm.models import ChunkAnalysis
from ..llm.prompts import CHUNK, instructions
from ..wiki.state import write_json
from .pdf import Source

# Upstream caps single-pass input at 300k characters and semantic chunks at 60k.
# Normal ten-page PDFs stay whole. The token guard in llm/client.py checks EVERY call.
SINGLE_PASS_CHARS = 300_000
CHUNK_CHARS = 60_000


def split_page(text: str, target: int) -> list[str]:
    """Prefer paragraph, then line, then sentence boundaries; retain all text."""
    pieces = []
    while len(text) > target:
        window = text[:target]
        boundary = window.rfind("\n\n")
        if boundary < target // 2:
            boundary = window.rfind("\n")
        if boundary < target // 2:
            matches = list(re.finditer(r"[.!?]\s+", window))
            boundary = matches[-1].end() if matches else target
        if boundary < target // 2:
            boundary = target
        pieces.append(text[:boundary])
        text = text[boundary:]
    if text:
        pieces.append(text)
    return pieces


def semantic_chunks(source: Source, target: int) -> list[str]:
    chunks, current = [], ""
    for page_number, page in enumerate(source.pages, 1):
        for piece in split_page(page, target):
            marked = f"--- PDF page {page_number} ---\n{piece}"
            if current and len(current) + len(marked) + 2 > target:
                chunks.append(current)
                current = ""
            current += ("\n\n" if current else "") + marked
    if current:
        chunks.append(current)
    return chunks


def source_context(
    source: Source, context: dict, purpose: str, schema: str, llm, state: Path
) -> str:
    if len(source.text) <= SINGLE_PASS_CHARS:
        return source.text
    chunks = semantic_chunks(source, CHUNK_CHARS)
    system = instructions(CHUNK, purpose, schema)
    key = hashlib.sha256(
        json.dumps(
            {
                "source": source.sha256,
                "chunks": chunks,
                "system": system,
                "context": context,
                "deployment": getattr(llm, "deployment", "test"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    checkpoint = state / "chunks" / f"{source.id}.json"
    saved = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    results = saved.get("results", []) if saved.get("key") == key else []
    for index in range(len(results), len(chunks)):
        print(f"  Analyze chunk {index + 1}/{len(chunks)}", flush=True)
        overlap_size = min(3_000, max(800, int(CHUNK_CHARS * 0.08)))
        result = llm.ask(
            system,
            json.dumps(
                {
                    **context,
                    "chunk_number": index + 1,
                    "chunk_count": len(chunks),
                    "chunk_text": chunks[index],
                    "overlap": chunks[index - 1][-overlap_size:] if index else "",
                    "previous_digest": results[-1]["digest"] if results else "",
                },
                ensure_ascii=False,
            ),
            ChunkAnalysis,
            8_192,
        )
        if not result.analysis.strip() or not result.digest.strip():
            raise ValueError("Long-source analysis returned empty notes or digest.")
        results.append(result.model_dump())
        write_json(checkpoint, {"key": key, "results": results})
    # Retain EVERY chunk's detailed evidence rather than silently trimming old notes.
    notes = "\n\n".join(f"## Chunk {i + 1}\n{r['analysis']}" for i, r in enumerate(results))
    return f"# Long-source digest\n{results[-1]['digest']}\n\n# Detailed evidence\n{notes}"
