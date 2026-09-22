"""Microsoft Agent Framework with an Azure AI Foundry project and typed output."""

import asyncio
import json
import math
import os
from typing import Literal, TypeVar

import tiktoken
from agent_framework import Agent
from agent_framework.exceptions import ChatClientException
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import AzureCliCredential
from pydantic import BaseModel, Field, ValidationError

# These limits assume the configured Foundry deployment serves GPT-5.6 Luna.
CONTEXT_WINDOW = 1_050_000
MAX_OUTPUT_TOKENS = 32_768
SAFETY_MARGIN = 16_000
T = TypeVar("T", bound=BaseModel)


class PagePlan(BaseModel):
    path: str
    title: str
    instructions: str


class Analysis(BaseModel):
    findings: str
    pages: list[PagePlan]


class PageDraft(BaseModel):
    path: str
    summary: str
    body: str
    tags: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)


class Review(BaseModel):
    kind: Literal["contradiction", "duplicate", "missing-page", "suggestion"]
    title: str
    description: str
    pages: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class Generation(BaseModel):
    pages: list[PageDraft]
    reviews: list[Review] = Field(default_factory=list)


class PageMerge(BaseModel):
    summary: str
    body: str
    reviews: list[Review] = Field(default_factory=list)


class ReviewSuggestions(BaseModel):
    reviews: list[Review]


class ChunkAnalysis(BaseModel):
    analysis: str
    digest: str


class IncompleteResponse(ValueError):
    """The output allowance was exhausted; generation can retry smaller units."""


def estimate_tokens(text: str) -> int:
    # Explicit approximation: the Luna-specific tokenizer is not assumed to exist.
    encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text, disallowed_special=()))


def check_budget(system: str, user: str, schema: type[BaseModel], output_tokens: int) -> int:
    payload = system + user + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    estimate = math.ceil(estimate_tokens(payload) * 1.10) + 1_024
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

    def ask(self, system: str, user: str, schema: type[T], output_tokens: int) -> T:
        estimate = check_budget(system, user, schema, output_tokens)
        print(f"  {schema.__name__}: ~{estimate:,} input tokens", flush=True)
        # Keep the file pipeline synchronous; each call owns and closes its async clients.
        return asyncio.run(self._ask(system, user, schema, output_tokens))

    async def _ask(self, system: str, user: str, schema: type[T], output_tokens: int) -> T:
        async with (
            AzureCliCredential() as credential,
            AIProjectClient(endpoint=self.project_endpoint, credential=credential) as project,
        ):
            client = FoundryChatClient(project_client=project, model=self.deployment)
            # Foundry obtains the underlying Responses transport from the project.
            client.client.max_retries = 0
            client.client.timeout = 600
            async with client.client:
                agent = Agent(
                    client=client,
                    name=f"doc2wiki-{schema.__name__.lower()}",
                    instructions=system,
                )
                try:
                    response = await agent.run(
                        user,
                        options={
                            "response_format": schema,
                            "max_tokens": output_tokens,
                            "reasoning": {"effort": "low"},
                            "truncation": "disabled",
                            "store": False,
                        },
                    )
                except ChatClientException as exc:
                    # The SDK can parse partial JSON before exposing finish_reason.
                    # Classify only EOF errors as incomplete; auth/refusals still fail.
                    cause = exc.__cause__
                    if isinstance(cause, ValidationError) and any(
                        error["type"] == "json_invalid"
                        and "EOF" in error.get("ctx", {}).get("error", "")
                        for error in cause.errors()
                    ):
                        raise IncompleteResponse(
                            f"{schema.__name__} returned truncated JSON."
                        ) from exc
                    raise
        if response.usage_details:
            self.usage.append(
                {"stage": schema.__name__, "deployment": self.deployment, **response.usage_details}
            )
        if response.finish_reason != "stop" or response.value is None:
            error = IncompleteResponse if response.finish_reason == "length" else ValueError
            raise error(
                f"{schema.__name__} returned no complete result "
                f"(finish_reason={response.finish_reason}). "
                "The source has not been committed."
            )
        return schema.model_validate(response.value)
