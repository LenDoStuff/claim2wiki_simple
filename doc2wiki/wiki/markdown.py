"""Markdown storage and the small amount of validation needed before writing."""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from markdown_it import MarkdownIt

PAGE_PATH = re.compile(r"[a-z][a-z0-9-]*/[a-z0-9]+(?:-[a-z0-9]+)*\.md")
MD = MarkdownIt("commonmark", {"html": False}).enable("table")
AUXILIARY = {"index.md", "overview.md", "reviews.md", "log.md"}


def wikilink_rule(state, silent: bool) -> bool:
    # An inline parser rule leaves fenced code, inline code, and escaped text alone.
    if state.linkLevel or not state.src.startswith("[[", state.pos):
        return False
    end = state.src.find("]]", state.pos + 2)
    if end < 0:
        return False
    content = state.src[state.pos + 2 : end]
    if not content.strip() or "\n" in content or "[" in content:
        return False
    if not silent:
        token = state.push("wikilink", "", 0)
        target, _, label = content.partition("|")
        token.meta = {"target": target.strip(), "label": label.strip() or target.strip()}
    state.pos = end + 2
    return True


MD.inline.ruler.before("link", "wikilink", wikilink_rule)


@dataclass
class Page:
    path: str
    metadata: dict
    body: str

    def markdown(self) -> str:
        metadata = yaml.safe_dump(self.metadata, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{metadata}\n---\n\n{self.body.strip()}\n"


def load_pages(wiki: Path) -> dict[str, Page]:
    pages = {}
    for folder in sorted(wiki.glob("*/")):
        if folder.name in {"pdfs", "media"}:
            continue
        for path in sorted(folder.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---\n") or "\n---\n" not in text[4:]:
                raise ValueError(f"{path}: missing YAML frontmatter.")
            front, body = text[4:].split("\n---\n", 1)
            metadata = yaml.safe_load(front)
            if not isinstance(metadata, dict) or not all(
                field in metadata for field in ("title", "summary", "sources", "created", "type")
            ):
                raise ValueError(f"{path}: incomplete page metadata.")
            relative = path.relative_to(wiki).as_posix()
            if not PAGE_PATH.fullmatch(relative):
                raise ValueError(f"Invalid wiki page path: {relative}")
            pages[relative] = Page(relative, metadata, body.strip())
    return pages


def links(body: str):
    for token in MD.parse(body):
        for child in token.children or []:
            if child.type == "image":
                raise ValueError("Images are not supported in this text-only POC.")
            if child.type == "link_open":
                yield child.attrGet("href")
            if child.type == "wikilink":
                yield f"[[{child.meta['target']}]]"


def resolve_wikilink(target: str, page_paths: set[str]) -> str:
    target = target.removeprefix("wiki/").removesuffix(".md")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?", target):
        raise ValueError(f"Invalid wikilink: [[{target}]]")
    matches = [
        path
        for path in page_paths
        if path.removesuffix(".md") == target
        or ("/" not in target and path.rsplit("/", 1)[-1].removesuffix(".md") == target)
    ]
    if len(matches) != 1:
        raise ValueError(f"Broken link or ambiguous wikilink: [[{target}]]")
    return matches[0]


def local_target(page_path: str, href: str, page_paths: set[str] | None = None) -> tuple[str, str]:
    if href.startswith("[[") and href.endswith("]]"):
        return resolve_wikilink(href[2:-2], page_paths or set()), ""
    url = urlsplit(href)
    if url.scheme or url.netloc or url.query or not url.path or "\\" in url.path:
        raise ValueError(f"Unsupported link in {page_path}: {href}")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(page_path), unquote(url.path)))
    return target, url.fragment


def validate_links(page: Page, page_paths: set[str], sources: dict) -> set[str]:
    cited = set()
    for href in links(page.body):
        target, fragment = local_target(page.path, href, page_paths)
        if target.endswith(".md") and target in page_paths and not fragment:
            continue
        if target.startswith("pdfs/") and target.endswith(".pdf"):
            source_id = target[5:-4]
            match = re.fullmatch(r"page=([1-9][0-9]*)", fragment)
            if source_id in sources and match and int(match[1]) <= sources[source_id]["page_count"]:
                cited.add(source_id)
                continue
        raise ValueError(f"Broken link or invalid PDF citation in {page.path}: {href}")
    return cited


def pdf_citations(page: Page) -> set[tuple[str, str]]:
    return {
        local_target(page.path, href)
        for href in links(page.body)
        if not href.startswith("[[") and ".pdf#" in href
    }


def catalog(pages: dict[str, Page]) -> list[dict]:
    return [
        {
            "path": page.path,
            "title": page.metadata["title"],
            "type": page.metadata["type"],
            "summary": page.metadata["summary"],
            "tags": page.metadata.get("tags", []),
        }
        for page in pages.values()
    ]


def escape_markdown(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>!#])", r"\\\1", " ".join(text.split()))


def write_navigation(wiki: Path, pages: dict[str, Page], title: str) -> None:
    # Navigation is deterministic: it never needs a model call or drops old entries.
    lines = [
        f"# {escape_markdown(title)}",
        "",
        "A connected reference built from PDF sources.",
        "",
        "[Overview](overview.md) · [Review items](reviews.md) · [Ingestion log](log.md)",
        "",
    ]
    for folder in sorted({p.path.split("/")[0] for p in pages.values()}):
        heading = folder.replace("-", " ").title()
        group = sorted(
            (p for p in pages.values() if p.path.startswith(folder + "/")),
            key=lambda p: p.metadata["title"].casefold(),
        )
        if group:
            lines.extend([f"## {heading}", ""])
            for page in group:
                label = escape_markdown(page.metadata["title"])
                summary = escape_markdown(page.metadata["summary"])
                lines.append(f"- [{label}]({page.path}) - {summary}")
            lines.append("")
    (wiki / "index.md").write_text("\n".join(lines), encoding="utf-8")


def write_reports(wiki: Path, pages: dict[str, Page], manifest: dict) -> None:
    """Application-owned overview, review queue and log, all readable offline."""
    wiki.mkdir(parents=True, exist_ok=True)
    reviews = manifest.get("reviews", [])
    lines = [
        "# Review items",
        "",
        "Follow-up items for human judgment; no research is run automatically.",
        "",
    ]
    if not reviews:
        lines.extend(["No outstanding review items were identified.", ""])
    for number, review in enumerate(reviews, 1):
        lines.extend(
            [
                f"## {number}. {escape_markdown(review['title'])}",
                "",
                f"**{review['kind']}** · Source: {escape_markdown(review['source_name'])}",
                "",
                escape_markdown(review["description"]),
                "",
            ]
        )
        for path in review.get("pages", []):
            if path in pages:
                lines.append(f"- [{escape_markdown(pages[path].metadata['title'])}]({path})")
            else:
                lines.append(f"- Suggested page: {escape_markdown(path)}")
        for query in review.get("search_queries", []):
            lines.append(f"- Suggested search: {escape_markdown(query)}")
        lines.extend(
            [
                "",
                "Decision: _pending_ (record your decision in this page after the final build).",
                "",
            ]
        )
    (wiki / "reviews.md").write_text("\n".join(lines), encoding="utf-8")
    lines = ["# Ingestion log", ""]
    for entry in manifest.get("log", []):
        lines.extend(
            [
                f"## {entry['date']} — {escape_markdown(entry['source_name'])}",
                "",
                (
                    f"Created {entry['created']} pages; updated {entry['updated']} pages; "
                    f"added {entry['reviews']} review items."
                ),
                "",
            ]
        )
        lines.extend(f"- [{escape_markdown(path)}]({path})" for path in entry["pages"])
        lines.append("")
    (wiki / "log.md").write_text("\n".join(lines), encoding="utf-8")
    lines = [
        f"# {escape_markdown(manifest['settings']['title'])} — Overview",
        "",
        (
            f"{len(pages)} knowledge pages from {len(manifest['sources'])} PDFs. "
            f"{len(reviews)} review items."
        ),
        "",
        "[All pages](index.md) · [Review items](reviews.md) · [Ingestion log](log.md)",
        "",
        "## Source coverage",
        "",
    ]
    for page in pages.values():
        if page.metadata["type"] == "source":
            lines.append(
                f"- [{escape_markdown(page.metadata['title'])}]({page.path}) — "
                f"{escape_markdown(page.metadata['summary'])}"
            )
    (wiki / "overview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
