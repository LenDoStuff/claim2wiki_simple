import json
import time

import httpx2
import pytest
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials import AccessToken

from doc2wiki.llm import LLM, check_budget
from doc2wiki.models import Analysis, Generation, IncompleteResponse, ReviewSuggestions


@pytest.fixture
def foundry(monkeypatch):
    """Run the real Agent, Foundry and project SDKs; replace only auth and HTTP."""
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test-resource.services.ai.azure.com/api/projects/wiki",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "my-luna-deployment")
    requests, credentials, transports = [], [], []
    reply = {
        "status": "completed",
        "text": "## Findings\nSome evidence\n\n## Page Plan\n"
        "### wiki/concepts/topic.md | Topic\nExplain the evidence.",
    }

    class Credential:
        def __init__(self):
            self.closed = False
            credentials.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def get_token(self, *scopes, **kwargs):
            return AccessToken("test-token", int(time.time()) + 3600)

    def respond(request):
        requests.append(request)
        content = [{"type": "output_text", "text": reply["text"], "annotations": []}]
        if reply.get("refusal"):
            content = [{"type": "refusal", "refusal": "Cannot process this."}]
        return httpx2.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": reply["status"],
                "model": "gpt-5.6-luna",
                "incomplete_details": {"reason": "max_output_tokens"}
                if reply["status"] == "incomplete"
                else None,
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": reply["status"],
                        "content": content,
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    original_get_client = AIProjectClient.get_openai_client

    def get_client(project, **kwargs):
        transport = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        transports.append(transport)
        return original_get_client(project, http_client=transport, **kwargs)

    monkeypatch.setattr("doc2wiki.llm.AzureCliCredential", Credential)
    monkeypatch.setattr(AIProjectClient, "get_openai_client", get_client)
    return requests, reply, credentials, transports


def test_real_foundry_project_agent_and_plain_markdown_response(foundry, tmp_path):
    requests, reply, credentials, transports = foundry
    llm = LLM()
    llm.response_dir = tmp_path
    result = llm.ask("Analyze", "First source", Analysis, 8_192)
    assert result.findings == reply["text"]
    assert result.pages[0].path == "concepts/topic.md"
    # Two synchronous asks must own separate event-loop-bound clients and no shared history.
    llm.ask("Analyze", "Second source", Analysis, 8_192)
    assert len(requests) == 2
    for request in requests:
        assert request.url.host == "test-resource.services.ai.azure.com"
        assert request.url.path == "/api/projects/wiki/openai/v1/responses"
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        assert body["model"] == "my-luna-deployment"
        assert body["reasoning"] == {"effort": "low"}
        assert body["truncation"] == "disabled"
        assert body["store"] is False
        assert body["max_output_tokens"] == 8_192
        assert body.get("text", {}).get("format", {}).get("type") in (None, "text")
        assert "response_format" not in body
        assert "previous_response_id" not in body
        assert "conversation" not in body
    assert "First source" not in requests[1].content.decode()
    assert len(llm.usage) == 2
    assert llm.usage[0]["deployment"] == "my-luna-deployment"
    assert all(c.closed for c in credentials)
    assert all(t.is_closed for t in transports)
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert all(p.read_text(encoding="utf-8") == reply["text"] for p in tmp_path.glob("*.md"))


def test_budget_includes_output_allowance_and_reserve(monkeypatch):
    monkeypatch.setattr("doc2wiki.llm.estimate_tokens", lambda text: 950_000)
    with pytest.raises(ValueError, match="Nothing was truncated"):
        check_budget("system", "source", 32_768)


@pytest.mark.parametrize("status, refusal", [("incomplete", False), ("completed", True)])
def test_incomplete_and_refused_responses_fail(foundry, status, refusal):
    _, reply, credentials, transports = foundry
    reply.update(status=status, refusal=refusal)
    with pytest.raises(ValueError):
        LLM().ask("Analyze", "A source", Analysis, 8_192)
    assert all(c.closed for c in credentials)
    assert all(t.is_closed for t in transports)


def test_truncated_markdown_is_available_for_targeted_repair(foundry):
    _, reply, _, _ = foundry
    reply.update(status="incomplete", text="---FILE: wiki/concepts/long.md---\n---\ntitle: Long")
    with pytest.raises(IncompleteResponse) as error:
        LLM().ask("Generate", "A source", Generation, 32_768)
    assert error.value.text == reply["text"]


def test_empty_review_response_is_valid_but_refusal_is_not(foundry):
    _, reply, _, _ = foundry
    reply["text"] = ""
    assert LLM().ask("Review", "A source", ReviewSuggestions, 8_192).reviews == []
    reply["refusal"] = True
    with pytest.raises(ValueError):
        LLM().ask("Review", "A source", ReviewSuggestions, 8_192)


def test_foundry_returns_original_file_blocks_with_unescaped_markdown(foundry):
    requests, reply, _, _ = foundry
    reply["text"] = (
        "---FILE: wiki/concepts/readings.md---\n---\ntype: concept\ntitle: Readings\n"
        "tags: [measurements]\n---\n\n# Readings\n\nEvidence from the source.\n\n"
        "```sql\nCREATE TABLE readings (\n    id INT PRIMARY KEY\n);\n```\n---END FILE---"
    )
    result = LLM().ask("Generate Markdown FILE blocks.", "A source", Generation, 32_768)
    assert result.pages[0].path == "concepts/readings.md"
    assert "\n    id INT PRIMARY KEY\n" in result.pages[0].body
    assert result.pages[0].tags == ["measurements"]
    assert "json_schema" not in requests[0].content.decode()


@pytest.mark.parametrize("missing", ["FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL"])
def test_missing_foundry_configuration_is_actionable(monkeypatch, missing):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://test.invalid/api/projects/wiki")
    monkeypatch.setenv("FOUNDRY_MODEL", "luna-deployment")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match="Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL in .env"):
        LLM()
