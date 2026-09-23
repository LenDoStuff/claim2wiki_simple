import hashlib
import io
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AccessToken
from pypdf import PdfReader, PdfWriter
from test_pipeline import make_pdf

from doc2wiki.pipeline import discover_pdfs
from doc2wiki.preprocessing.ocr import resource_endpoint, searchable_pdf
from doc2wiki.preprocessing.split import excerpt, parse_decisions, read_categories, split_pdf


@pytest.fixture
def azure_read(monkeypatch, tmp_path):
    """Use the real Azure SDK, replacing only Azure CLI auth and HTTP transport."""
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/wiki"
    )
    monkeypatch.setenv("FOUNDRY_DOCUMENT_INTELLIGENCE_CONNECTION", "read-ocr")
    pdf = tmp_path / "azure-output.pdf"
    make_pdf(pdf, pages=3)
    data = {"pdf": pdf.read_bytes(), "pages": 3, "requests": [], "scopes": [], "closed": False}
    operation = "https://read-service.cognitiveservices.azure.com/documentintelligence/documentModels/prebuilt-read/analyzeResults/test-operation"

    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            data["closed"] = True

        def get_token(self, *scopes, **kwargs):
            data["scopes"].extend(scopes)
            return AccessToken("test-token", int(time.time()) + 3_600)

    def send(session, request, **kwargs):
        data["requests"].append(request)
        response = requests.Response()
        response.request, response.url = request, request.url
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        if urlsplit(request.url).path.endswith("/connections/read-ocr"):
            body = json.dumps(
                {
                    "name": "read-ocr",
                    "id": "read-ocr",
                    "type": "ApiKey",
                    "target": "https://read-service.cognitiveservices.azure.com/",
                    "isDefault": False,
                    "metadata": {},
                }
            ).encode()
        elif request.method == "POST":
            response.status_code = 202
            response.headers["Operation-Location"] = operation + "?api-version=2024-11-30"
            response.headers["Retry-After"] = "0"
            body = b"{}"
        elif urlsplit(request.url).path.endswith("/pdf"):
            response.headers["Content-Type"] = "application/pdf"
            body = data["pdf"]
        else:
            body = json.dumps(
                {
                    "status": "succeeded",
                    "analyzeResult": {
                        "apiVersion": "2024-11-30",
                        "modelId": "prebuilt-read",
                        "content": "Synthetic text",
                        "pages": [{"pageNumber": i} for i in range(1, data["pages"] + 1)],
                    },
                }
            ).encode()
        response._content = body
        response.raw = io.BytesIO(body)
        return response

    monkeypatch.setattr("doc2wiki.preprocessing.ocr.AzureCliCredential", Credential)
    monkeypatch.setattr(
        "doc2wiki.preprocessing.ocr.DocumentIntelligenceClient",
        lambda **kwargs: DocumentIntelligenceClient(**kwargs, polling_interval=0),
    )
    monkeypatch.setattr(requests.Session, "send", send)
    return data


def test_read_downloads_searchable_pdf_and_skips_completed_work(azure_read, tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "prepared/result.pdf"
    make_pdf(source, pages=3)
    original = source.read_bytes()
    searchable_pdf(source, output)
    assert output.read_bytes() == azure_read["pdf"]
    assert source.read_bytes() == original
    requests_sent = azure_read["requests"]
    assert [request.method for request in requests_sent] == ["GET", "POST", "GET", "GET"]
    assert urlsplit(requests_sent[0].url).path == "/api/projects/wiki/connections/read-ocr"
    analyze = requests_sent[1]
    assert urlsplit(analyze.url).hostname == "read-service.cognitiveservices.azure.com"
    assert (
        urlsplit(analyze.url).path == "/documentintelligence/documentModels/prebuilt-read:analyze"
    )
    assert parse_qs(urlsplit(analyze.url).query)["output"] == ["pdf"]
    assert parse_qs(urlsplit(analyze.url).query)["api-version"] == ["2024-11-30"]
    assert analyze.headers["Content-Type"] == "application/octet-stream"
    assert analyze.headers["Authorization"] == "Bearer test-token"
    assert "https://cognitiveservices.azure.com/.default" in azure_read["scopes"]
    assert "https://ai.azure.com/.default" in azure_read["scopes"]
    assert azure_read["closed"]
    state = json.loads(output.with_suffix(".ocr.json").read_text())
    assert state["page_count"] == 3
    assert state["operation_id"] == "test-operation"
    assert not state["empty_text_pages"]
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT")
    searchable_pdf(source, output)
    assert len(requests_sent) == 4
    output.write_bytes(b"modified")
    with pytest.raises(ValueError, match="missing or changed"):
        searchable_pdf(source, output)
    assert len(requests_sent) == 4


@pytest.mark.parametrize("failure", ["free-tier", "download"])
def test_ocr_rejects_missing_pages(azure_read, tmp_path, failure):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source, pages=4 if failure == "download" else 3)
    azure_read["pages"] = 4 if failure == "download" else 2
    with pytest.raises(ValueError, match="page"):
        searchable_pdf(source, output)
    assert not output.exists()
    assert not output.with_suffix(".ocr.json").exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://example/api/projects/wiki",
        "https://example",
        "https://example/api/projects/wiki?key=x",
    ],
)
def test_ocr_requires_a_project_endpoint(endpoint):
    with pytest.raises(ValueError, match="FOUNDRY_PROJECT_ENDPOINT"):
        resource_endpoint(endpoint, None)


def test_ocr_refuses_to_overwrite_source_or_untracked_output(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    with pytest.raises(ValueError, match="separate"):
        searchable_pdf(source, source)
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"user file")
    with pytest.raises(ValueError, match="already exists"):
        searchable_pdf(source, output)
    assert output.read_bytes() == b"user file"


def test_ocr_requires_a_named_connection(monkeypatch):
    monkeypatch.delenv("FOUNDRY_DOCUMENT_INTELLIGENCE_CONNECTION", raising=False)
    with pytest.raises(ValueError, match="CONNECTION"):
        resource_endpoint("https://example.services.ai.azure.com/api/projects/wiki", None)


def test_ocr_rejects_a_download_without_embedded_text(azure_read, tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source, pages=3)
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    azure_read["pdf"] = buffer.getvalue()
    with pytest.raises(ValueError, match="no extractable text"):
        searchable_pdf(source, output)
    assert not output.exists()


def table(rows):
    return (
        "| Page | Start | Category | Title | Confidence | Reason |\n"
        + "| --- | --- | --- | --- | --- | --- |\n"
        + "\n".join("| " + " | ".join(map(str, row)) + " |" for row in rows)
    )


class BoundaryModel:
    deployment = "test-model"

    def __init__(self, starts=None, fail_page=None):
        self.starts = starts or {
            1: ("expert_report", "Survey report"),
            4: ("invoice", "Invoice A"),
            6: ("invoice", "Invoice B"),
        }
        self.requests = []
        self.fail_page = fail_page

    def ask_text(self, system, user, *, stage, **kwargs):
        context = json.loads(user)
        self.requests.append((stage, context))
        if self.fail_page == context["pages"][0]["page"]:
            raise ValueError("Simulated model interruption")
        rows = []
        for page in context["pages"]:
            number = page["page"]
            start = max(first for first in self.starts if first <= number)
            category, title = self.starts[start]
            rows.append(
                (
                    number,
                    "yes" if number in self.starts else "no",
                    category,
                    title,
                    0.6 if number == 3 else 0.95,
                    "Visible document identity",
                )
            )
        return table(rows)


def test_split_preserves_pages_reviews_and_batch_context(tmp_path):
    source, output = tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(source, pages=6)
    original = source.read_bytes()
    model = BoundaryModel()
    manifest_path = split_pdf(source, output, batch_pages=2, llm=model)
    manifest = json.loads(manifest_path.read_text())
    assert [(d["start_page"], d["end_page"], d["category"]) for d in manifest["documents"]] == [
        (1, 3, "expert_report"),
        (4, 5, "invoice"),
        (6, 6, "invoice"),
    ]
    assert source.read_bytes() == original
    originals = PdfReader(source).pages
    for document in manifest["documents"]:
        part = PdfReader(output / document["path"])
        assert part.metadata["/SourcePageStart"] == str(document["start_page"])
        assert part.metadata["/SourceFile"] == source.name
        assert [p.extract_text() for p in part.pages] == [
            p.extract_text() for p in originals[document["start_page"] - 1 : document["end_page"]]
        ]
    assert [stage for stage, _ in model.requests] == ["Split", "Split", "SplitReview", "Split"]
    next_batch = model.requests[1][1]
    assert next_batch["previous_page"]["page"] == 2
    assert next_batch["current_document"]["page"] == 1
    assert model.requests[-1][1]["current_document"]["page"] == 4
    assert any("Page 3" in item for item in manifest["review_items"])
    assert "confidence 0.60" in (output / "review.md").read_text()
    assert discover_pdfs(output, tmp_path / "wiki-output") == [
        (output / item["path"]).resolve() for item in manifest["documents"]
    ]
    split_pdf(source, output, batch_pages=2, llm=model)
    assert len(model.requests) == 4


def test_split_resumes_only_unfinished_batches(tmp_path):
    source, output = tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(source, pages=6)
    with pytest.raises(ValueError, match="interruption"):
        split_pdf(source, output, batch_pages=2, llm=BoundaryModel(fail_page=3))
    assert not list(output.rglob("*.pdf"))
    with pytest.raises(ValueError, match="splitting is incomplete"):
        discover_pdfs(output, tmp_path / "wiki-output")
    model = BoundaryModel()
    split_pdf(source, output, batch_pages=2, llm=model)
    assert model.requests[0][1]["pages"][0]["page"] == 3
    assert model.requests[0][1]["current_document"]["page"] == 1
    assert (output / "manifest.json").is_file()


def test_page_cap_keeps_logical_identity_and_rejects_changed_results(tmp_path):
    source, output = tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(source, pages=5)
    model = BoundaryModel(starts={1: ("expert_report", "One report")})
    manifest = json.loads(split_pdf(source, output, max_pages=2, llm=model).read_text())
    assert [(d["start_page"], d["end_page"]) for d in manifest["documents"]] == [
        (1, 2),
        (3, 4),
        (5, 5),
    ]
    assert {d["document_id"] for d in manifest["documents"]} == {"doc-0001"}
    assert [d["part"] for d in manifest["documents"]] == [1, 2, 3]
    with pytest.raises(ValueError, match="settings changed"):
        split_pdf(source, output, max_pages=3, llm=model)
    (output / manifest["documents"][0]["path"]).write_bytes(b"edited")
    with pytest.raises(ValueError, match="missing or changed"):
        split_pdf(source, output, max_pages=2, llm=model)


@pytest.mark.parametrize(
    "rows",
    [
        [(1, "no", "other", "Title", 0.9, "Reason")],
        [(2, "yes", "other", "Title", 0.9, "Reason")],
        [(1, "yes", "../outside", "Title", 0.9, "Reason")],
        [(1, "yes", "other", "Title", "nan", "Reason")],
        [(1, "yes", "other", "Title", 0.9, "Reason"), (1, "no", "other", "Title", 0.9, "Reason")],
    ],
)
def test_split_rejects_invalid_decisions(rows):
    with pytest.raises(ValueError):
        parse_decisions(table(rows), [1], {"other": "Unknown"})


def test_excerpt_only_limits_boundary_context():
    text = "A" * 5_000 + "B" * 5_000
    sample = excerpt(7, text)
    assert sample["page"] == 7
    assert sample["text"].startswith("A" * 4_000)
    assert sample["text"].endswith("B" * 2_000)
    assert "Middle omitted" in sample["text"]


def test_invalid_category_paths_are_rejected(tmp_path):
    config = tmp_path / "categories.yaml"
    config.write_text("categories:\n  ../outside: bad\n  other: unknown\n")
    with pytest.raises(ValueError, match="Invalid category"):
        read_categories(config)


def test_source_changes_are_not_silently_resplit(tmp_path):
    source, output = tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(source, pages=2)
    model = BoundaryModel()
    split_pdf(source, output, llm=model)
    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    make_pdf(source, pages=3)
    assert hashlib.sha256(source.read_bytes()).hexdigest() != old_hash
    with pytest.raises(ValueError, match="source or settings changed"):
        split_pdf(source, output, llm=model)
    assert len(model.requests) == 1


def test_split_manifest_order_overrides_category_order(tmp_path):
    source, output = tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(source, pages=3)
    model = BoundaryModel(starts={1: ("policy", "Policy"), 2: ("invoice", "Invoice")})
    split_pdf(source, output, llm=model)
    paths = discover_pdfs(output, tmp_path / "wiki")
    assert [path.parent.name for path in paths] == ["policy", "invoice"]
    make_pdf(output / "unexpected.pdf", pages=1)
    with pytest.raises(ValueError, match="manifest and PDF files differ"):
        discover_pdfs(output, tmp_path / "wiki")


def test_split_preserves_and_flags_blank_pages(tmp_path):
    original, source, output = tmp_path / "text.pdf", tmp_path / "bundle.pdf", tmp_path / "split"
    make_pdf(original, pages=2)
    reader, writer = PdfReader(original), PdfWriter()
    writer.add_page(reader.pages[0])
    writer.add_blank_page(width=595, height=842)
    writer.add_page(reader.pages[1])
    writer.write(source)
    model = BoundaryModel(starts={1: ("expert_report", "Report with blank page")})
    manifest = json.loads(split_pdf(source, output, llm=model).read_text())
    part = PdfReader(output / manifest["documents"][0]["path"])
    assert len(part.pages) == 3
    assert part.pages[1].extract_text() == ""
    assert any(
        "Page 2: no extracted text; page preserved" in item for item in manifest["review_items"]
    )
