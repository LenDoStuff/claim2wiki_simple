"""Microsoft Agent Framework with an Azure AI Foundry project and Markdown output."""

import asyncio
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import tiktoken
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import AzureCliCredential
from pydantic import BaseModel

from .models import IncompleteResponse, ReviewSuggestions
from .responses import parse_response

# These limits assume the configured Foundry deployment serves GPT-5.6 Luna.
CONTEXT_WINDOW = 1_050_000
MAX_OUTPUT_TOKENS = 32_768
SAFETY_MARGIN = 16_000
T = TypeVar("T", bound=BaseModel)


def estimate_tokens(text: str) -> int:
    # Explicit approximation: the Luna-specific tokenizer is not assumed to exist.
    encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text, disallowed_special=()))


def check_budget(system: str, user: str, output_tokens: int) -> int:
    estimate = math.ceil(estimate_tokens(system + user) * 1.10) + 1_024
    if estimate + output_tokens + SAFETY_MARGIN > CONTEXT_WINDOW:
        raise ValueError(
            f"Request exceeds the {CONTEXT_WINDOW:,}-token context budget "
            f"(~{estimate:,} input + {output_tokens:,} output + {SAFETY_MARGIN:,} reserve). "
            "Split the PDF or use a smaller wiki. Nothing was truncated."
        )
    return estimate


class LLM:
    def __init__(self) -> None:
        self.project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        self.deployment = os.getenv("FOUNDRY_MODEL", "").strip()
        if not self.project_endpoint or not self.deployment:
            raise ValueError(
                "Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL in .env, then run az login."
            )
        self.usage: list[dict] = []
        self.response_dir: Path | None = None

    def ask(self, system: str, user: str, result_type: type[T], output_tokens: int) -> T:
        text = self.ask_text(
            system,
            user,
            stage=result_type.__name__,
            output_tokens=output_tokens,
            allow_empty=result_type is ReviewSuggestions,
        )
        return parse_response(text, result_type)

    def ask_text(
        self,
        system: str,
        user: str,
        *,
        stage: str,
        output_tokens: int = 8_192,
        allow_empty: bool = False,
    ) -> str:
        """A plain Markdown response, also used by the standalone PDF splitter."""
        estimate = check_budget(system, user, output_tokens)
        print(f"  {stage}: ~{estimate:,} input tokens", flush=True)
        return asyncio.run(self._ask_text(system, user, stage, output_tokens, allow_empty))

    async def _ask_text(
        self, system: str, user: str, stage: str, output_tokens: int, allow_empty: bool
    ) -> str:
        async with (
            AzureCliCredential() as credential,
            AIProjectClient(endpoint=self.project_endpoint, credential=credential) as project,
        ):
            client = FoundryChatClient(project_client=project, model=self.deployment)
            client.client.max_retries = 0
            client.client.timeout = 600
            async with client.client:
                agent = Agent(
                    client=client,
                    name=f"doc2wiki-{stage.lower()}",
                    instructions=system,
                )
                response = await agent.run(
                    user,
                    options={
                        "max_tokens": output_tokens,
                        "reasoning": {"effort": "low"},
                        "truncation": "disabled",
                        "store": False,
                    },
                )
        text = response.text or ""
        if self.response_dir:
            self.response_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            (self.response_dir / f"{stamp}-{stage}.md").write_text(text, encoding="utf-8")
        if response.usage_details:
            self.usage.append(
                {
                    "stage": stage,
                    "deployment": self.deployment,
                    **response.usage_details,
                }
            )
        if response.finish_reason == "length":
            raise IncompleteResponse(f"{stage} reached its output limit.", text)
        if response.finish_reason != "stop" or (not text.strip() and not allow_empty):
            raise ValueError(
                f"{stage} returned no complete result "
                f"(finish_reason={response.finish_reason}). The source has not been committed."
            )
        return text
