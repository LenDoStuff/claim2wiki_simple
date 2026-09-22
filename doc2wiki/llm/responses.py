"""Parse llm_wiki-style Markdown, FILE blocks, and REVIEW blocks."""

import re
from typing import TypeVar, cast

import yaml
from pydantic import BaseModel

from ..wiki.markdown import PAGE_PATH
from .models import (
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

T = TypeVar("T", bound=BaseModel)


def outside_code(text: str):
    """Yield line positions outside fenced examples, so literal markers stay literal."""
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", content)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                fence = None
        elif fence is None:
            yield offset, content
        offset += len(line)


def read_blocks(text: str, *, leading_markdown: bool = False, partial: bool = False):
    """Keep complete blocks. Only explicit output repair may discard an unfinished tail."""
    blocks, current, cursor, leading = [], None, 0, ""
    for offset, line in outside_code(text):
        opening = re.fullmatch(r"---(FILE|REVIEW): (.+)---", line)
        closing = re.fullmatch(r"---END (FILE|REVIEW)---", line)
        if opening:
            if current:
                raise ValueError("A new block started before the preceding block ended.")
            gap = text[cursor:offset]
            if gap.strip():
                if leading_markdown and not blocks:
                    leading = gap
                else:
                    raise ValueError(
                        "Expected FILE/REVIEW blocks without surrounding prose or JSON."
                    )
            current = (*opening.groups(), offset + len(line) + 1)
        elif closing:
            if current is None or current[0] != closing[1]:
                raise ValueError(f"Unexpected block ending: {line}")
            kind, label, start = current
            blocks.append((kind, label.strip(), text[start:offset].strip()))
            cursor, current = offset + len(line) + 1, None
    if current:
        if not partial:
            raise IncompleteResponse(f"Unfinished {current[0]} block: {current[1]}", text)
    elif text[cursor:].strip():
        if leading_markdown and not blocks:
            leading = text
        elif not partial:
            raise ValueError("Expected FILE/REVIEW blocks without surrounding prose or JSON.")
    return blocks, leading.strip()


def page_path(path: str) -> str:
    path = path.removeprefix("wiki/")
    if not PAGE_PATH.fullmatch(path):
        raise ValueError(f"Invalid Markdown page path: {path}")
    return path


def markdown_page(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Generated Markdown page is missing YAML frontmatter.")
    front, body = text[4:].split("\n---\n", 1)
    try:
        metadata = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise ValueError("Generated page has invalid YAML frontmatter.") from exc
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(field), str) or not metadata[field].strip()
        for field in ("title", "type")
    ):
        raise ValueError("Generated frontmatter must include a title and type.")
    for field in ("sources", "tags", "related"):
        values = metadata.get(field, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f"Frontmatter {field} must be a list of strings.")
        metadata[field] = values
    for field in ("created", "updated"):
        if field in metadata:
            metadata[field] = str(metadata[field])  # YAML may decode ISO dates as date objects.
    if "summary" not in metadata:
        # Upstream pages do not require summary. Derive a short catalog label locally.
        prose = next(
            (
                line.strip()
                for _, line in outside_code(body)
                if line.strip() and not line.lstrip().startswith(("#", "|"))
            ),
            metadata["title"],
        )
        metadata["summary"] = " ".join(prose.split())[:240]
    if not isinstance(metadata["summary"], str) or not metadata["summary"].strip():
        raise ValueError("Frontmatter summary must be nonempty text when supplied.")
    if not body.strip().startswith("# "):
        raise ValueError("Generated Markdown page is missing its title heading.")
    return metadata, body.strip()


def review_block(label: str, content: str) -> Review:
    kind, separator, title = label.partition("|")
    if not separator or not title.strip():
        raise ValueError("REVIEW header must contain a type and title separated by '|'.")
    fields, removals = {}, []
    for offset, line in outside_code(content):
        match = re.fullmatch(r"(OPTIONS|PAGES|SEARCH):\s*(.*)", line)
        if match:
            key, value = match.groups()
            if key in fields:
                raise ValueError(f"Duplicate REVIEW field: {key}")
            fields[key] = value
            removals.append((offset, offset + len(line)))
    for start, end in reversed(removals):
        content = content[:start] + content[end:]
    if not content.strip():
        raise ValueError("REVIEW block is missing its description.")
    if "OPTIONS" in fields and [p.strip() for p in fields["OPTIONS"].split("|")] != [
        "Create Page",
        "Skip",
    ]:
        raise ValueError("REVIEW options must be Create Page | Skip.")
    return Review(
        kind=kind.strip(),
        title=title.strip(),
        description=content.strip(),
        pages=[
            p.strip().removeprefix("wiki/") for p in fields.get("PAGES", "").split(",") if p.strip()
        ],
        search_queries=[q.strip() for q in fields.get("SEARCH", "").split("|") if q.strip()],
    )


def parse_generation(text: str, *, partial: bool = False) -> Generation:
    blocks, _ = read_blocks(text.replace("\r\n", "\n").strip(), partial=partial)
    result = Generation(pages=[])
    for kind, label, content in blocks:
        if kind == "REVIEW":
            result.reviews.append(review_block(label, content))
        else:
            metadata, body = markdown_page(content)
            result.pages.append(
                PageDraft(
                    path=page_path(label),
                    summary=metadata["summary"],
                    body=body,
                    tags=metadata["tags"],
                    related=metadata["related"],
                    metadata=metadata,
                )
            )
    return result


def parse_response(text: str, result_type: type[T]) -> T:
    text = text.replace("\r\n", "\n").strip()
    if result_type is Generation:
        result = parse_generation(text)
    elif result_type is Analysis:
        lines = list(outside_code(text))
        section = next((offset for offset, line in lines if line == "## Page Plan"), None)
        if section is None:
            raise ValueError("Analysis must end with a Markdown '## Page Plan' section.")
        headings = [
            (offset, re.fullmatch(r"### (\S+\.md) \| (.+)", line), len(line))
            for offset, line in lines
            if offset > section and line.startswith("### ")
        ]
        plans = []
        for i, (offset, match, length) in enumerate(headings):
            if match is None:
                raise ValueError("Page plan heading must be '### wiki/folder/slug.md | Title'.")
            end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
            plans.append(
                PagePlan(
                    path=page_path(match[1]),
                    title=match[2].strip(),
                    instructions=text[offset + length : end].strip(),
                )
            )
        if not plans:
            raise ValueError("Analysis page plan contains no pages.")
        result = Analysis(findings=text, pages=plans)
    elif result_type is ChunkAnalysis:
        sections = {
            line: offset + len(line)
            for offset, line in outside_code(text)
            if line in {"## Chunk Analysis", "## Updated Global Digest"}
        }
        start, end = sections.get("## Chunk Analysis"), sections.get("## Updated Global Digest")
        if start is None or end is None or start >= end:
            raise ValueError(
                "Chunk response needs Chunk Analysis and Updated Global Digest headings."
            )
        result = ChunkAnalysis(
            analysis=text[start : end - len("## Updated Global Digest")].strip(),
            digest=text[end:].strip(),
        )
    elif result_type in (PageMerge, ReviewSuggestions):
        blocks, page = read_blocks(text, leading_markdown=result_type is PageMerge)
        if any(kind != "REVIEW" for kind, _, _ in blocks):
            raise ValueError("This stage does not accept FILE blocks.")
        reviews = [review_block(label, body) for _, label, body in blocks]
        if result_type is PageMerge:
            metadata, body = markdown_page(page)
            result = PageMerge(
                summary=metadata["summary"], body=body, metadata=metadata, reviews=reviews
            )
        else:
            result = ReviewSuggestions(reviews=reviews)
    else:
        raise ValueError(f"No Markdown parser for {result_type.__name__}.")
    return cast(T, result)
