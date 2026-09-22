from pathlib import Path

import pytest

from doc2wiki.ingestion.pdf import Source
from doc2wiki.llm.models import Analysis, Generation, PageDraft, PagePlan
from doc2wiki.pipeline import prepare_pages, validate_plan


@pytest.fixture
def source():
    return Source(Path("report.pdf"), "report.pdf", "report-123", "hash", ["text"] * 10)


@pytest.mark.parametrize("path", ["../../escape.md", "concepts/../../escape.md", "C:/escape.md"])
def test_model_cannot_choose_paths_outside_wiki(source, path):
    analysis = Analysis(
        findings="Evidence",
        pages=[
            PagePlan(path=source.summary_path, title="Report", instructions="Summarize"),
            PagePlan(path=path, title="Escape", instructions="Invalid"),
        ],
    )
    with pytest.raises(ValueError, match="Invalid planned page"):
        validate_plan(analysis, source, {})


@pytest.mark.parametrize(
    "citation",
    [
        "[PDF](../pdfs/report-123.pdf#page=11)",
        "[PDF](../pdfs/missing.pdf#page=1)",
        "No citation at all.",
    ],
)
def test_generation_rejects_missing_or_invalid_page_citations(source, citation):
    analysis = Analysis(
        findings="Evidence",
        pages=[
            PagePlan(path=source.summary_path, title="Report", instructions="Summarize"),
        ],
    )
    generation = Generation(
        pages=[
            PageDraft(path=source.summary_path, summary="Report.", body=f"# Report\n\n{citation}"),
        ]
    )
    with pytest.raises(ValueError, match="citation"):
        prepare_pages(generation, analysis, source, {}, {source.id: {"page_count": 10}})


def test_generation_cannot_silently_omit_a_planned_page(source):
    analysis = Analysis(
        findings="Evidence",
        pages=[
            PagePlan(path=source.summary_path, title="Report", instructions="Summarize"),
        ],
    )
    with pytest.raises(ValueError, match="exactly the planned pages"):
        prepare_pages(Generation(pages=[]), analysis, source, {}, {})
