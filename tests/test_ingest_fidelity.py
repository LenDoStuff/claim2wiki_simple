"""Exercise the ingestion features restored from the reference workflow."""

import json
from pathlib import Path

import pytest
from test_pipeline import ScriptedLLM, make_pdf

from doc2wiki.config import read_config, schema_folders
from doc2wiki.long_source import semantic_chunks, source_context, split_page
from doc2wiki.models import (
    Analysis,
    ChunkAnalysis,
    Generation,
    IncompleteResponse,
    PageDraft,
    PageMerge,
    PagePlan,
    Review,
    ReviewSuggestions,
)
from doc2wiki.pdf import Source
from doc2wiki.pipeline import build, generate_pages, validate_plan
from doc2wiki.render import render_markdown
from doc2wiki.wiki import Page, load_pages, validate_links


def test_custom_purpose_schema_and_review_are_used_and_exported(tmp_path):
    purpose = tmp_path / "purpose.md"
    purpose.write_text(
        "# Goal\nEvaluate winter reliability.\n# Thesis\nDo not assume cold-weather parity."
    )
    schema_file = tmp_path / "schema.md"
    schema_file.write_text(
        read_config(None, "schema.md")
        + "\n| finding | wiki/findings/ | Measured field results. |\n"
    )
    review = Review(
        kind="contradiction",
        title="Different operating conditions",
        description="The two measurements use different temperatures; do not combine them.",
        pages=["findings/heat-pumps.md"],
    )

    class Model(ScriptedLLM):
        def ask(self, system, user, schema, output_tokens):
            context = json.loads(user)
            if schema is ReviewSuggestions:
                assert "Evaluate winter reliability" in system
                assert context["existing_reviews"][0]["title"] == review.title
                return ReviewSuggestions(reviews=[review])  # Deduplicated against generation.
            result = super().ask(system, user, schema, output_tokens)
            for page in result.pages:
                page.path = page.path.replace("concepts/", "findings/")
            if schema is Generation:
                result.reviews.append(review)
            return result

    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "report.pdf")
    llm = Model()
    build(inputs, output, llm=llm, purpose_file=purpose, schema_file=schema_file)
    assert all(
        "Evaluate winter reliability" in system and "wiki/findings/" in system
        for system in llm.systems
    )
    page = load_pages(output / "wiki")["findings/heat-pumps.md"]
    assert page.metadata["type"] == "finding"
    assert "Different operating conditions" in (output / "site/reviews.html").read_text()
    assert (
        'href="../findings/heat-pumps.html"'
        in next((output / "site/sources").glob("*.html")).read_text()
    )
    manifest = json.loads((output / ".state/manifest.json").read_text())
    assert len(manifest["reviews"]) == 1
    assert len(manifest["log"]) == 1
    assert (output / "wiki/log.md").is_file()


def test_separate_merge_locks_metadata_unions_arrays_and_preserves_page_citations(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "first.pdf")
    build(inputs, output, llm=ScriptedLLM())
    page = load_pages(output / "wiki")["concepts/heat-pumps.md"]
    page.metadata.update(
        title="Established title",
        type="concept",
        created="2020-01-02",
        tags=["existing-tag"],
        related=["heat-pumps"],
    )
    old_source = page.metadata["sources"][0]
    page.body += f"\n\nEarlier evidence. [Earlier, p. 2](../pdfs/{old_source}.pdf#page=2)"
    (output / "wiki" / page.path).write_text(page.markdown(), encoding="utf-8")
    make_pdf(inputs / "second.pdf")
    build(inputs, output, llm=ScriptedLLM())
    merged = load_pages(output / "wiki")[page.path]
    assert merged.metadata["title"] == "Established title"
    assert merged.metadata["created"] == "2020-01-02"
    assert merged.metadata["tags"] == ["existing-tag", "measurements"]
    assert merged.metadata["related"] == ["heat-pumps"]
    assert len(merged.metadata["sources"]) == 2
    assert "#page=2" in merged.body and "#page=10" in merged.body


@pytest.mark.parametrize("failure", ["short", "citation", "api"])
def test_bad_merge_keeps_previous_wiki_and_manifest(tmp_path, failure):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "first.pdf")
    build(inputs, output, llm=ScriptedLLM())
    old = (output / "wiki/concepts/heat-pumps.md").read_bytes()
    make_pdf(inputs / "second.pdf")

    class Model(ScriptedLLM):
        def ask(self, system, user, schema, output_tokens):
            result = super().ask(system, user, schema, output_tokens)
            if schema is PageMerge:
                if failure == "api":
                    raise ValueError("Simulated API failure")
                if failure == "short":
                    result.body = "# Heat pumps\n\nToo short."
                else:
                    # Same length and source identity, but a distinct old page citation is lost.
                    first_id = next(iter(json.loads(user)["available_sources"]))
                    result.body = result.body.replace(
                        f"{first_id}.pdf#page=10", f"{first_id}.pdf#page=9"
                    )
            return result

    with pytest.raises(ValueError):
        build(inputs, output, llm=Model())
    assert (output / "wiki/concepts/heat-pumps.md").read_bytes() == old
    assert len(json.loads((output / ".state/manifest.json").read_text())["sources"]) == 1


@pytest.mark.parametrize("mode", ["missing", "truncated"])
def test_generation_repairs_only_missing_pages_and_has_no_twelve_page_cap(mode):
    source = Source(Path("report.pdf"), "report.pdf", "report-123", "hash", ["page"])
    plans = [
        PagePlan(path=f"concepts/topic-{i}.md", title=f"Topic {i}", instructions="Explain")
        for i in range(15)
    ]
    analysis = Analysis(findings="Evidence", pages=plans)
    validate_plan(analysis, source, {})  # Inserts the mandatory source summary.
    assert len(analysis.pages) == 16
    calls = []

    class Model:
        def ask(self, system, user, schema, output_tokens):
            context = json.loads(user)
            requested = context["requested_pages"]
            calls.append([p["path"] for p in requested])
            if len(calls) == 1:
                if mode == "truncated":
                    raise IncompleteResponse("length")
                requested = requested[:-2]
            return Generation(
                pages=[
                    PageDraft(path=p["path"], summary="Summary", body="# Title") for p in requested
                ]
            )

    result = generate_pages(
        {
            "analysis": analysis.model_dump(),
            "source": {"id": source.id, "summary_path": source.summary_path},
        },
        "purpose",
        "schema",
        Model(),
    )
    assert {p.path for p in result.pages} == {p.path for p in analysis.pages}
    assert len(calls) == (3 if mode == "missing" else 17)
    assert all(len(paths) == 1 for paths in calls[1:])


def test_source_summary_fallback_is_visible_and_flagged():
    context = {
        "analysis": {
            "findings": "Evidence from the whole source.",
            "pages": [{"path": "sources/a.md", "title": "Report", "instructions": "Summarize"}],
        },
        "source": {"id": "a", "summary_path": "sources/a.md"},
    }

    class Model:
        def ask(self, *args):
            return Generation(pages=[])

    result = generate_pages(context, "purpose", "schema", Model())
    assert "fallback" in result.pages[0].body
    assert "Evidence from the whole source" in result.pages[0].body
    assert len(result.reviews) == 1


def test_long_source_resumes_chunks_and_retains_all_notes(tmp_path, monkeypatch):
    monkeypatch.setattr("doc2wiki.long_source.SINGLE_PASS_CHARS", 100)
    monkeypatch.setattr("doc2wiki.long_source.CHUNK_CHARS", 250)
    pages = [f"Evidence on page {i}. " * 12 for i in range(1, 5)]
    source = Source(Path("report.pdf"), "report.pdf", "report-123", "hash", pages)
    chunks = semantic_chunks(source, 250)
    calls = []

    class Model:
        fail = True

        def ask(self, system, user, schema, output_tokens):
            context = json.loads(user)
            number = context["chunk_number"]
            calls.append(number)
            assert schema is ChunkAnalysis
            assert "PDF page" in context["chunk_text"]
            if number == 3 and self.fail:
                raise ValueError("Interrupted")
            if number > 1:
                assert context["overlap"] and context["previous_digest"] == f"Digest {number - 1}"
            return ChunkAnalysis(
                analysis=f"Unique finding {number} with exact data.", digest=f"Digest {number}"
            )

    model = Model()
    with pytest.raises(ValueError, match="Interrupted"):
        source_context(source, {}, "purpose", "schema", model, tmp_path)
    model.fail = False
    text = source_context(source, {}, "purpose", "schema", model, tmp_path)
    assert calls[:4] == [1, 2, 3, 3]  # Successful chunks are not billed again.
    assert len(calls) == len(chunks) + 1
    assert all(f"Unique finding {n} with exact data." in text for n in range(1, len(chunks) + 1))
    count = len(calls)
    source_context(source, {}, "purpose", "schema", model, tmp_path)
    assert len(calls) == count
    source_context(source, {}, "changed purpose", "schema", model, tmp_path)
    assert len(calls) == count + len(chunks)  # A changed purpose invalidates the checkpoint.


def test_semantic_split_does_not_discard_structured_text():
    text = "CREATE TABLE readings (id INT PRIMARY KEY, value DECIMAL);\n\n" * 40 + "Final caveat."
    assert "".join(split_page(text, 250)) == text


def test_wikilinks_resolve_aliases_ambiguity_and_leave_code_untouched():
    paths = {"concepts/pump.md", "entities/pump.md"}
    body = "See [[concepts/pump|Pump concept]]. `[[not-a-link]]`\n\n```\n[[literal]]\n```\n\n\\[\\[escaped]]"
    rendered = render_markdown(body, "sources/report.md", paths)
    assert 'href="../concepts/pump.html">Pump concept</a>' in rendered
    assert "<code>[[not-a-link]]</code>" in rendered
    assert "[[literal]]" in rendered and "[[escaped]]" in rendered
    page = Page("sources/report.md", {}, "# Report\n\n[[pump]]")
    with pytest.raises(ValueError, match="ambiguous"):
        validate_links(page, paths, {})


@pytest.mark.parametrize(
    "row",
    [
        "| custom | wiki/../ | no |",
        "| custom | wiki/a/b/ | no |",
        "| source | wiki/other/ | duplicate |",
    ],
)
def test_schema_rejects_unsafe_or_duplicate_routing(row):
    with pytest.raises(ValueError):
        schema_folders(read_config(None, "schema.md") + "\n" + row)
