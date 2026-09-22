"""Internal records parsed from Markdown responses; never sent as JSON schemas."""

from typing import Literal

from pydantic import BaseModel, Field


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
    metadata: dict = Field(default_factory=dict)


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
    metadata: dict = Field(default_factory=dict)
    reviews: list[Review] = Field(default_factory=list)


class ReviewSuggestions(BaseModel):
    reviews: list[Review]


class ChunkAnalysis(BaseModel):
    analysis: str
    digest: str


class IncompleteResponse(ValueError):
    """Generation may recover complete FILE blocks and retry unfinished pages."""

    def __init__(self, message: str, text: str = "") -> None:
        super().__init__(message)
        self.text = text
