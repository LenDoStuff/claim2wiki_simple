"""Markdown response parsing, including exact code and incomplete block recovery."""

import json

import pytest
import yaml
from test_pipeline import ScriptedLLM, make_pdf

from doc2wiki.models import (
    Analysis,
    ChunkAnalysis,
    Generation,
    IncompleteResponse,
    PageMerge,
    ReviewSuggestions,
)
from doc2wiki.pipeline import build, collect_reviews, generate_pages
from doc2wiki.responses import parse_generation, parse_response
from doc2wiki.wiki import load_pages

PAGE = """---
type: concept
title: Measurements
summary: Exact measurement definitions.
created: 2026-09-22
updated: 2026-09-22
tags: [measurement]
related: []
sources: [report-123]
author: Example author
---

# Measurements

Evidence with [[readings]] and [Report, p. 2](../pdfs/report-123.pdf#page=2).

```sql
CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    value DECIMAL(10, 3) NOT NULL
);
```

| Name | Value |
| --- | --- |
| A | 1.25 |
"""

REVIEW = """---REVIEW: contradiction | Conflicting measurements---
The source uses a different population. Keep both results attributed.
OPTIONS: Create Page | Skip
PAGES: wiki/concepts/measurements.md, wiki/sources/report-123.md
---END REVIEW---"""


def file_block(path="concepts/measurements.md", page=PAGE):
    return f"---FILE: wiki/{path}---\n{page.rstrip()}\n---END FILE---"


def test_original_file_and_review_syntax_preserves_markdown_and_metadata():
    result = parse_generation(file_block() + "\n\n" + REVIEW)
    page = result.pages[0]
    assert page.path == "concepts/measurements.md"
    assert page.body == PAGE.split("\n---\n", 1)[1].strip()
    assert page.metadata["created"] == "2026-09-22"
    assert page.metadata["author"] == "Example author"
    assert page.tags == ["measurement"]
    assert result.reviews[0].kind == "contradiction"
    assert result.reviews[0].pages == ["concepts/measurements.md", "sources/report-123.md"]
    assert "OPTIONS:" not in result.reviews[0].description


def test_markers_in_code_blocks_are_literal_not_control_blocks():
    page = (
        PAGE
        + "\n````markdown\n---FILE: wiki/concepts/literal.md---\n```sql\nSELECT 1;\n```\n---END FILE---\n````\n"
    )
    result = parse_generation(file_block(page=page))
    assert len(result.pages) == 1
    assert "---FILE: wiki/concepts/literal.md---" in result.pages[0].body
    assert "SELECT 1;" in result.pages[0].body


@pytest.mark.parametrize(
    "text",
    [
        '{"pages": []}',
        "Here are the files:\n" + file_block(),
        file_block(page="# No frontmatter"),
        file_block(page=PAGE.replace("tags: [measurement]", "tags: measurement")),
        file_block(page=PAGE.replace("type: concept", "type: [concept]")),
        file_block(page=PAGE.replace("title: Measurements", "title: [unclosed")),
        file_block(path="../outside.md"),
        file_block(path="concepts/../../outside.md"),
    ],
)
def test_invalid_formats_metadata_and_paths_fail(text):
    with pytest.raises(ValueError):
        parse_generation(text)


def test_merge_uses_a_plain_markdown_page_with_optional_review_blocks():
    result = parse_response(PAGE + "\n" + REVIEW, PageMerge)
    assert result.body == PAGE.split("\n---\n", 1)[1].strip()
    assert result.metadata["author"] == "Example author"
    assert len(result.reviews) == 1
    with pytest.raises(ValueError, match="does not accept FILE"):
        parse_response(file_block(), PageMerge)


def test_plain_analysis_plan_and_chunk_headings():
    analysis = """## Findings
The document supports two concepts.

## Page Plan
### wiki/concepts/measurements.md | Measurements
Preserve the SQL schema and measurements.

### wiki/concepts/calibration.md | Calibration
Explain the limitations.
"""
    result = parse_response(analysis, Analysis)
    assert result.findings == analysis.strip()
    assert [p.path for p in result.pages] == ["concepts/measurements.md", "concepts/calibration.md"]
    assert result.pages[0].instructions == "Preserve the SQL schema and measurements."
    chunk = parse_response(
        "## Chunk Analysis\n\nExact evidence, PDF page 10.\n\n"
        "## Updated Global Digest\n\nCumulative findings.",
        ChunkAnalysis,
    )
    assert chunk.analysis == "Exact evidence, PDF page 10."
    assert chunk.digest == "Cumulative findings."


def test_reviews_accept_no_items_and_search_suggestions():
    assert parse_response("", ReviewSuggestions).reviews == []
    review = REVIEW.replace("contradiction", "suggestion").replace(
        "---END REVIEW---",
        "SEARCH: measurement uncertainty methods | calibration bias studies\n---END REVIEW---",
    )
    result = parse_response(review, ReviewSuggestions)
    assert result.reviews[0].search_queries == [
        "measurement uncertainty methods",
        "calibration bias studies",
    ]


def test_missing_summary_in_upstream_frontmatter_gets_a_local_catalog_label():
    result = parse_generation(
        file_block(page=PAGE.replace("summary: Exact measurement definitions.\n", ""))
    )
    assert result.pages[0].summary.startswith("Evidence with")


def test_truncated_generation_preserves_complete_files_and_repairs_only_unfinished_page():
    text = file_block() + "\n\n---FILE: wiki/concepts/calibration.md---\n---\ntitle: Calibration"
    with pytest.raises(IncompleteResponse):
        parse_generation(text)
    requests = []

    class Model:
        def ask(self, system, user, result_type, output_tokens):
            context = json.loads(user)
            requests.append(context["requested_pages"])
            if len(requests) == 1:
                raise IncompleteResponse("Output limit", text)
            return parse_generation(file_block("concepts/calibration.md"))

    context = {
        "analysis": {
            "pages": [
                {
                    "path": "concepts/measurements.md",
                    "title": "Measurements",
                    "instructions": "Explain",
                },
                {
                    "path": "concepts/calibration.md",
                    "title": "Calibration",
                    "instructions": "Explain",
                },
            ]
        }
    }
    result = generate_pages(context, "purpose", "schema", Model())
    assert [p.path for p in result.pages] == ["concepts/measurements.md", "concepts/calibration.md"]
    assert len(requests) == 2
    assert [p["path"] for p in requests[1]] == ["concepts/calibration.md"]


def test_cut_off_review_is_recovered_even_when_all_files_are_complete():
    calls = []

    class Model:
        def ask(self, system, user, result_type, output_tokens):
            calls.append(result_type)
            if result_type is Generation:
                raise IncompleteResponse(
                    "Output limit", file_block() + "\n" + REVIEW.removesuffix("---END REVIEW---")
                )
            return parse_response(REVIEW, ReviewSuggestions)

    context = {
        "analysis": {
            "pages": [
                {
                    "path": "concepts/measurements.md",
                    "title": "Measurements",
                    "instructions": "Explain",
                }
            ]
        }
    }
    model = Model()
    generation = generate_pages(context, "purpose", "schema", model)
    reviews = collect_reviews(generation, {}, context, "purpose", "schema", model)
    assert calls == [Generation, ReviewSuggestions]
    assert len(generation.pages) == 1 and len(reviews) == 1
    assert reviews[0].kind == "contradiction"


def test_markdown_responses_through_incremental_ingestion_and_html(tmp_path):
    """Replace only the model's text generation; run every Markdown parser and stage."""

    class MarkdownModel(ScriptedLLM):
        def ask(self, system, user, result_type, output_tokens):
            result = super().ask(system, user, result_type, output_tokens)
            if result_type is Analysis:
                text = (
                    result.findings
                    + "\n\n## Page Plan\n"
                    + "\n\n".join(
                        f"### wiki/{p.path} | {p.title}\n{p.instructions}" for p in result.pages
                    )
                )
            elif result_type is Generation:
                blocks = []
                context = json.loads(user)
                for page in result.pages:
                    title = next(
                        p["title"] for p in context["requested_pages"] if p["path"] == page.path
                    )
                    metadata = {
                        "title": title,
                        "type": "source" if page.path.startswith("sources/") else "concept",
                        "summary": page.summary,
                        "tags": page.tags,
                        "related": page.related,
                        "sources": [context["source"]["id"]],
                        "author": "Preserved author",
                    }
                    markdown = "---\n" + yaml.safe_dump(metadata) + "---\n\n" + page.body
                    blocks.append(file_block(page.path, markdown))
                text = "\n\n".join(blocks)
            elif result_type is PageMerge:
                context = json.loads(user)
                metadata = yaml.safe_load(context["existing_page"].split("\n---\n", 1)[0][4:])
                metadata["summary"] = result.summary
                text = "---\n" + yaml.safe_dump(metadata) + "---\n\n" + result.body
            else:
                text = ""  # No review suggestions needed for these short fixtures.
            return parse_response(text, result_type)

    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "first.pdf")
    build(inputs, output, llm=MarkdownModel())
    make_pdf(inputs / "second.pdf")
    build(inputs, output, llm=MarkdownModel())
    page = load_pages(output / "wiki")["concepts/heat-pumps.md"]
    assert len(page.metadata["sources"]) == 2
    assert page.metadata["author"] == "Preserved author"
    assert "first.pdf" in page.body and "second.pdf" in page.body
    assert "---FILE:" not in (output / "wiki/concepts/heat-pumps.md").read_text()
    assert "second.pdf" in (output / "site/concepts/heat-pumps.html").read_text()
