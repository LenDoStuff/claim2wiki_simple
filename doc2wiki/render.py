"""Export plain Markdown as a self-contained, offline HTML folder and ZIP."""

import json
import posixpath
import shutil
from html import escape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from markdown_it.token import Token

from .wiki import (
    AUXILIARY,
    MD,
    Page,
    links,
    load_pages,
    local_target,
    resolve_wikilink,
    validate_links,
    write_navigation,
    write_reports,
)


def render_markdown(
    body: str, current: str = "index.md", page_paths: set[str] | None = None
) -> str:
    tokens = MD.parse(body)
    for token in tokens:
        if token.children:
            children = []
            for child in token.children:
                if child.type == "wikilink":
                    target = resolve_wikilink(child.meta["target"], page_paths or set())
                    opening = Token("link_open", "a", 1)
                    opening.attrSet("href", relative_html(target, current))
                    label = Token("text", "", 0)
                    label.content = child.meta["label"]
                    children.extend([opening, label, Token("link_close", "a", -1)])
                else:
                    children.append(child)
            token.children = children
        for child in token.children or []:
            if child.type == "link_open":
                url = urlsplit(child.attrGet("href"))
                if not url.scheme and url.path.endswith(".md"):
                    child.attrSet("href", urlunsplit(url._replace(path=url.path[:-3] + ".html")))
    return MD.renderer.render(tokens, MD.options, {})


def relative_html(target: str, current: str) -> str:
    return posixpath.relpath(target[:-3] + ".html", posixpath.dirname(current) or ".")


def export_html(output: Path) -> Path:
    wiki, site = output / "wiki", output / "site"
    manifest = json.loads((output / ".state" / "manifest.json").read_text(encoding="utf-8"))
    title = manifest["settings"]["title"]
    language = {"english": "en", "german": "de"}.get(manifest["settings"]["language"].lower(), "")
    pages = load_pages(wiki)
    if not pages:
        raise ValueError("No wiki pages to export.")
    for page in pages.values():
        validate_links(page, set(pages) | AUXILIARY, manifest["sources"])
    for source_id in manifest["sources"]:
        if not (wiki / "pdfs" / f"{source_id}.pdf").is_file():
            raise ValueError(f"Missing original PDF: {source_id}")
    write_navigation(wiki, pages, title)
    if not (wiki / "overview.md").exists():
        write_reports(wiki, pages, manifest)
    home = Page(
        "index.md",
        {"title": title, "type": "overview"},
        (wiki / "index.md").read_text(encoding="utf-8"),
    )
    all_pages = {"index.md": home, **pages}
    for name, label in (
        ("overview", "Overview"),
        ("reviews", "Review items"),
        ("log", "Ingestion log"),
    ):
        all_pages[f"{name}.md"] = Page(
            f"{name}.md",
            {"title": label, "type": name},
            (wiki / f"{name}.md").read_text(encoding="utf-8"),
        )
    backlinks = {path: set() for path in all_pages}
    for page in all_pages.values():
        validate_links(page, set(all_pages), manifest["sources"])
        for href in links(page.body):
            target, _ = local_target(page.path, href, set(all_pages))
            if target in backlinks and target != page.path:
                backlinks[target].add(page.path)

    site.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).with_name("style.css"), site / "style.css")
    shutil.copytree(wiki / "pdfs", site / "pdfs", dirs_exist_ok=True)
    for path, page in all_pages.items():
        prefix = "../" if "/" in path else ""
        navigation = ["<h2>Project</h2><ul>"]
        for name in ("overview.md", "reviews.md", "log.md"):
            navigation.append(
                f'<li><a href="{relative_html(name, path)}">'
                f"{escape(all_pages[name].metadata['title'])}</a></li>"
            )
        navigation.append("</ul>")
        for folder in sorted({p.path.split("/")[0] for p in pages.values()}):
            label = escape(folder.replace("-", " ").title())
            group = sorted(
                (p for p in pages.values() if p.path.startswith(folder + "/")),
                key=lambda p: p.metadata["title"].casefold(),
            )
            if not group:
                continue
            navigation.append(f"<h2>{label} <span>{len(group)}</span></h2><ul>")
            for item in group:
                active = ' aria-current="page"' if item.path == path else ""
                navigation.append(
                    f'<li><a href="{relative_html(item.path, path)}"{active}>'
                    f"{escape(item.metadata['title'])}</a></li>"
                )
            navigation.append("</ul>")
        references = ""
        if backlinks[path]:
            items = "".join(
                f'<li><a href="{relative_html(p, path)}">{escape(all_pages[p].metadata["title"])}</a></li>'
                for p in sorted(backlinks[path])
            )
            references = (
                f'<section class="backlinks"><h2>Linked from</h2><ul>{items}</ul></section>'
            )
        source_count = len(manifest["sources"])
        html = f'''<!doctype html>
<html lang="{language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(page.metadata["title"])} | {escape(title)}</title>
  <link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<aside>
  <a class="brand" href="{prefix}index.html"><span class="mark">D</span>{escape(title)}</a>
  <p class="library-count">{len(pages)} pages &middot; {source_count} PDFs</p>
  <nav aria-label="Wiki navigation">{"".join(navigation)}</nav>
  <p class="offline">PDF library &middot; available offline</p>
</aside>
<main id="content">
  <header><a href="{prefix}index.html">Library</a><span>/</span>{escape(page.metadata["type"].title())}</header>
  <article>{render_markdown(page.body, path, set(all_pages))}</article>
  {references}
  <footer>Built from source documents. Follow citations to inspect the original PDF.</footer>
</main>
</body>
</html>
'''
        destination = site / Path(path).with_suffix(".html")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(html, encoding="utf-8")
    shutil.make_archive(str(output / "site"), "zip", site)
    print(f"HTML: {site / 'index.html'}\nShare: {output / 'site.zip'}", flush=True)
    return site / "index.html"
