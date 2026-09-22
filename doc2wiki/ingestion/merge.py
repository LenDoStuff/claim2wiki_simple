"""Merge each existing page separately, without letting the model change identity."""

import json
from pathlib import Path

from ..llm.client import MAX_OUTPUT_TOKENS
from ..llm.models import PageMerge
from ..llm.prompts import MERGE, instructions
from ..wiki.markdown import Page, pdf_citations, validate_links
from ..wiki.state import write_json


def merge_pages(
    updates: dict[str, Page],
    pages: dict[str, Page],
    context: dict,
    purpose: str,
    schema: str,
    llm,
    state: Path,
) -> list:
    reviews = []
    known_paths = set(pages) | set(updates)
    for path, incoming in updates.items():
        old = pages.get(path)
        if old is None or old.body.strip() == incoming.body.strip():
            continue
        print(f"  Merge: {path}", flush=True)
        result = llm.ask(
            instructions(MERGE, purpose, schema),
            json.dumps(
                {
                    "settings": context["settings"],
                    "source": context["source"],
                    "catalog": context["catalog"],
                    "planned_pages": context["analysis"]["pages"],
                    "available_sources": context["available_sources"],
                    "existing_page": old.markdown(),
                    "incoming_page": incoming.markdown(),
                    "path": path,
                },
                ensure_ascii=False,
            ),
            PageMerge,
            MAX_OUTPUT_TOKENS,
        )
        write_json(
            state / "merges" / context["source"]["id"] / Path(path).with_suffix(".json"),
            result.model_dump(mode="json"),
        )
        if not result.body.startswith("# ") or not result.summary.strip():
            raise ValueError(f"{path}: merge is missing a heading or summary.")
        if len(result.body) < max(len(old.body), len(incoming.body)) * 0.70:
            raise ValueError(f"{path}: merge lost too much content; existing page was not changed.")
        merged = Page(
            path, {**incoming.metadata, "summary": " ".join(result.summary.split())}, result.body
        )
        validate_links(merged, known_paths, context["available_sources"])
        required = pdf_citations(old) | pdf_citations(incoming)
        if not required.issubset(pdf_citations(merged)):
            raise ValueError(
                f"{path}: merge dropped PDF page citations; existing page was not changed."
            )
        updates[path] = merged
        reviews.extend(result.reviews)
    return reviews
