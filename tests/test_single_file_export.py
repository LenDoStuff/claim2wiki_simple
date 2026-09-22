"""The shared folder contains one standalone HTML file and its PDF references."""

import json
import zipfile
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from test_pipeline import ScriptedLLM, make_pdf

from doc2wiki.pipeline import build
from doc2wiki.wiki.html import export_html


class LinksAndAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.hrefs = []
        self.external_assets = []
        self.has_inline_style = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "href" in attrs:
            self.hrefs.append(attrs["href"])
        if tag in {"script", "link", "iframe", "img"}:
            self.external_assets.append(tag)
        if tag == "style":
            self.has_inline_style = True


def test_one_html_file_with_working_anchors_and_flat_pdf_references(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "a/report.pdf")
    make_pdf(inputs / "b/report.pdf")
    build(inputs, output, llm=ScriptedLLM())
    site = output / "site"
    manifest = json.loads((output / ".state/manifest.json").read_text())
    expected = {"index.html"} | {f"{source_id}.pdf" for source_id in manifest["sources"]}
    assert {p.name for p in site.iterdir()} == expected
    assert all(p.is_file() for p in site.iterdir())
    parser = LinksAndAssets()
    parser.feed((site / "index.html").read_text(encoding="utf-8"))
    assert parser.has_inline_style and not parser.external_assets
    assert len(parser.ids) == len(set(parser.ids))
    assert "page-concepts/heat-pumps" in parser.ids
    assert {"page-index", "page-overview", "page-reviews", "page-log"} <= set(parser.ids)
    cited_pdfs = set()
    for href in parser.hrefs:
        link = urlsplit(href)
        assert not link.scheme and not link.netloc and not link.query
        if not link.path:
            assert link.fragment in parser.ids
        else:
            assert link.path in expected and link.path.endswith(".pdf")
            assert link.fragment == "page=10"
            assert (site / link.path).read_bytes() == (
                output / "wiki/pdfs" / link.path
            ).read_bytes()
            cited_pdfs.add(link.path)
    assert len(cited_pdfs) == 2  # Identical original filenames do not overwrite one another.
    with zipfile.ZipFile(output / "site.zip") as archive:
        assert set(archive.namelist()) == expected
        assert archive.read("index.html") == (site / "index.html").read_bytes()


def test_reexport_replaces_legacy_assets_and_preserves_markdown(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "report.pdf")
    build(inputs, output, llm=ScriptedLLM())
    before = {p: p.read_bytes() for p in (output / "wiki").rglob("*") if p.is_file()}
    site = output / "site"
    (site / "concepts").mkdir()
    (site / "concepts/old-page.html").write_text("Old multi-file export")
    (site / "pdfs").mkdir()
    (site / "pdfs/stale.pdf").write_bytes(b"old generated PDF")
    (site / "style.css").write_text("old external stylesheet")
    assert export_html(output) == site / "index.html"
    assert all(p.is_file() for p in site.iterdir())
    assert len(list(site.glob("*.html"))) == 1
    assert len(list(site.glob("*.pdf"))) == 1
    assert not (site / "style.css").exists()
    assert before == {p: p.read_bytes() for p in (output / "wiki").rglob("*") if p.is_file()}
    with zipfile.ZipFile(output / "site.zip") as archive:
        assert all("/" not in name for name in archive.namelist())


def test_invalid_markdown_does_not_destroy_previous_export(tmp_path):
    inputs, output = tmp_path / "input", tmp_path / "output"
    make_pdf(inputs / "report.pdf")
    build(inputs, output, llm=ScriptedLLM())
    before = {p: p.read_bytes() for p in (output / "site").iterdir()}
    archive = (output / "site.zip").read_bytes()
    with (output / "wiki/concepts/heat-pumps.md").open("a", encoding="utf-8") as page:
        page.write("\n\n[Missing page](../concepts/missing.md)\n")
    with pytest.raises(ValueError, match="Broken link"):
        export_html(output)
    assert before == {p: p.read_bytes() for p in (output / "site").iterdir()}
    assert (output / "site.zip").read_bytes() == archive
