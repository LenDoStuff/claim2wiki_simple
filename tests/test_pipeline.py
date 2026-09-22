import json
import zipfile
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from doc2wiki.models import Analysis, Generation, PageMerge, ReviewSuggestions
from doc2wiki.pdf import read_pdf
from doc2wiki.pipeline import build
from doc2wiki.render import export_html
from doc2wiki.wiki import load_pages


def make_pdf(path: Path, pages: int = 10, subject: str = "Heat pumps") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(path))
    for number in range(1, pages + 1):
        document.drawString(60, 790, f"{subject}: field notes, page {number}")
        document.drawString(60, 760, f"Measured output on page {number}: {number * 2} kWh.")
        document.drawString(60, 730, "These are synthetic test data, not real research findings.")
        document.showPage()
    document.save()


class ScriptedLLM:
    """Only the remote model is replaced; the entire local pipeline runs normally."""

    def __init__(self, bad_link=False):
        self.requests = []
        self.usage = []
        self.bad_link = bad_link
        self.systems = []

    def ask(self, system, user, schema, output_tokens):
        context = json.loads(user)
        self.requests.append(context)
        self.systems.append(system)
        source = context["source"]
        concept = "concepts/heat-pumps.md"
        if schema is Analysis:
            return Analysis.model_validate(
                {
                    "findings": "The synthetic field notes cover heat pump output through page 10.",
                    "pages": [
                        {
                            "path": source["summary_path"],
                            "title": source["name"],
                            "instructions": "Summarize all pages.",
                        },
                        {
                            "path": concept,
                            "title": "Heat pumps",
                            "instructions": "Merge the findings.",
                        },
                    ],
                }
            )
        if schema is PageMerge:
            old = context["existing_page"].split("\n---\n", 1)[1].strip()
            incoming = context["incoming_page"].split("\n---\n", 1)[1].strip()
            return PageMerge(
                summary="Integrated measurements and limitations.",
                body=old + "\n\n" + incoming.split("\n", 1)[1].strip(),
            )
        if schema is ReviewSuggestions:
            return ReviewSuggestions(reviews=[])
        assert schema is Generation
        citation = f"[{source['name']}, p. 10](../pdfs/{source['id']}.pdf#page=10)"
        destination = "missing" if self.bad_link else "heat-pumps"
        return Generation.model_validate(
            {
                "pages": [
                    {
                        "path": source["summary_path"],
                        "summary": "A synthetic ten-page field report.",
                        "body": f"# {source['name']}\n\nThe last page reports 20 kWh. {citation}\n\n"
                        f"See [[{destination}|Heat pumps]].",
                        "tags": ["field-report"],
                        "related": ["heat-pumps"],
                    },
                    {
                        "path": concept,
                        "summary": "Measurements and limitations from the supplied reports.",
                        "body": "# Heat pumps"
                        + f"\n\nReport {source['name']} measures 20 kWh on page 10. {citation}\n\n"
                        f"Read the [source summary](../{source['summary_path']}).",
                        "tags": ["measurements"],
                    },
                ]
            }
        )


def test_ten_pages_incremental_merge_export_and_unchanged_rerun(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "a" / "report.pdf")
    llm = ScriptedLLM()
    build(inputs, output, llm=llm)
    assert len(llm.requests) == 2
    assert "--- PDF page 10 ---" in llm.requests[0]["source_text"]
    assert "Measured output on page 10: 20 kWh" in llm.requests[1]["source_text"]

    # A second, same-named file in a different folder contributes to the same concept.
    make_pdf(inputs / "b" / "report.pdf", subject="Heat pump follow-up")
    build(inputs, output, llm=llm)
    assert len(llm.requests) == 5
    assert "existing_page" in llm.requests[4]  # Separate merge call.
    assert "existing_pages" not in llm.requests[3]  # Generation writes a contribution.
    pages = load_pages(output / "wiki")
    assert len(pages) == 3
    concept = pages["concepts/heat-pumps.md"]
    assert len(concept.metadata["sources"]) == 2
    assert "a/report.pdf" in concept.body and "b/report.pdf" in concept.body
    assert list((output / ".state" / "backups").rglob("heat-pumps.md"))

    before = (output / "wiki" / "concepts" / "heat-pumps.md").read_bytes()
    build(inputs, output, llm=llm)
    assert len(llm.requests) == 5  # No model calls for unchanged inputs.
    assert before == (output / "wiki" / "concepts" / "heat-pumps.md").read_bytes()
    html = (output / "site" / "index.html").read_text(encoding="utf-8")
    assert "Linked from" in html
    assert ".pdf#page=10" in html
    assert '.md"' not in html
    with zipfile.ZipFile(output / "site.zip") as archive:
        assert "index.html" in archive.namelist()
        assert "style.css" not in archive.namelist()
        assert [p for p in archive.namelist() if p.endswith(".html")] == ["index.html"]
        assert all("/" not in p for p in archive.namelist())
        assert len([p for p in archive.namelist() if p.endswith(".pdf")]) == 2
        assert not any(".state" in p for p in archive.namelist())


def test_invalid_generation_does_not_overwrite_pages_and_can_retry(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "first.pdf")
    with pytest.raises(ValueError, match="Broken link"):
        build(inputs, output, llm=ScriptedLLM(bad_link=True))
    assert not load_pages(output / "wiki")
    build(inputs, output, llm=ScriptedLLM())
    old_page = (output / "wiki" / "concepts" / "heat-pumps.md").read_bytes()
    make_pdf(inputs / "second.pdf")
    with pytest.raises(ValueError, match="Broken link"):
        build(inputs, output, llm=ScriptedLLM(bad_link=True))
    assert (output / "wiki" / "concepts" / "heat-pumps.md").read_bytes() == old_page
    manifest = json.loads((output / ".state" / "manifest.json").read_text())
    assert len(manifest["sources"]) == 1


@pytest.mark.parametrize("change", ["edit", "remove"])
def test_changed_or_removed_pdfs_stop_before_model_calls(tmp_path, change):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "a.pdf")
    build(inputs, output, llm=ScriptedLLM())
    make_pdf(inputs / "b.pdf")
    if change == "edit":
        make_pdf(inputs / "a.pdf", subject="Changed")
    else:
        (inputs / "a.pdf").unlink()
    llm = ScriptedLLM()
    with pytest.raises(ValueError, match="new --output"):
        build(inputs, output, llm=llm)
    assert llm.requests == []


def test_empty_pdf_page_is_not_silently_discarded(tmp_path):
    path = tmp_path / "scan.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.showPage()
    pdf.save()
    with pytest.raises(ValueError, match="OCR"):
        read_pdf(path, "scan.pdf")


def test_export_escapes_html_and_keeps_code_links_literal(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "report.pdf")
    build(inputs, output, llm=ScriptedLLM())
    path = output / "wiki" / "concepts" / "heat-pumps.md"
    with path.open("a", encoding="utf-8") as file:
        file.write('\n\n<script>alert("x")</script>\n\n`[literal](missing.md)`\n')
    export_html(output)
    html = (output / "site" / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<code>[literal](missing.md)</code>" in html
