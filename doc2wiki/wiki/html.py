"""Export one offline HTML file with its referenced PDFs in the same folder."""

import json
import re
import shutil
from html import escape
from pathlib import Path, PurePosixPath

from markdown_it.token import Token

from .markdown import (
    AUXILIARY,
    MD,
    Page,
    links,
    load_pages,
    local_target,
    validate_links,
    write_navigation,
    write_reports,
)


def page_id(path: str) -> str:
    # Keep the folder in the anchor so equal slugs in different folders stay distinct.
    return "page-" + path.removesuffix(".md")


def render_markdown(
    body: str, current: str = "index.md", page_paths: set[str] | None = None
) -> str:
    page_paths = page_paths or set()
    tokens = MD.parse(body)
    for token in tokens:
        if token.children:
            children = []
            for child in token.children:
                if child.type == "wikilink":
                    opening = Token("link_open", "a", 1)
                    opening.attrSet("href", f"[[{child.meta['target']}]]")
                    label = Token("text", "", 0)
                    label.content = child.meta["label"]
                    children.extend([opening, label, Token("link_close", "a", -1)])
                else:
                    children.append(child)
            token.children = children
        for child in token.children or []:
            if child.type != "link_open":
                continue
            target, fragment = local_target(current, child.attrGet("href"), page_paths)
            if target in page_paths and not fragment:
                child.attrSet("href", "#" + page_id(target))
            elif target.startswith("pdfs/") and target.endswith(".pdf"):
                child.attrSet("href", PurePosixPath(target).name + "#" + fragment)
            else:
                raise ValueError(f"Cannot export link in {current}: {child.attrGet('href')}")
    return MD.renderer.render(tokens, MD.options, {})


def export_html(output: Path) -> Path:
    output = output.resolve()
    wiki, site = output / "wiki", output / "site"
    # site/ is a disposable generated folder. Never follow a junction or symlink
    # when rebuilding it, and verify its resolved absolute location before deletion.
    if site.is_symlink() or site.resolve() != site or (site.exists() and not site.is_dir()):
        raise ValueError(f"HTML output must be a regular directory directly under {output}: {site}")
    manifest = json.loads((output / ".state" / "manifest.json").read_text(encoding="utf-8"))
    title = manifest["settings"]["title"]
    language = {"english": "en", "german": "de"}.get(manifest["settings"]["language"].lower(), "")
    pages = load_pages(wiki)
    if not pages:
        raise ValueError("No wiki pages to export.")
    for page in pages.values():
        validate_links(page, set(pages) | AUXILIARY, manifest["sources"])
    for source_id in manifest["sources"]:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id):
            raise ValueError(f"Invalid source ID: {source_id}")
        if not (wiki / "pdfs" / f"{source_id}.pdf").is_file():
            raise ValueError(f"Missing original PDF: {source_id}")
    write_navigation(wiki, pages, title)
    if not (wiki / "overview.md").exists():
        write_reports(wiki, pages, manifest)
    all_pages = {}
    for name, label in (
        ("index", title),
        ("overview", "Overview"),
        ("reviews", "Review items"),
        ("log", "Ingestion log"),
    ):
        all_pages[f"{name}.md"] = Page(
            f"{name}.md",
            {"title": label, "type": "overview" if name == "index" else name},
            (wiki / f"{name}.md").read_text(encoding="utf-8"),
        )
    all_pages.update(pages)
    page_paths = set(all_pages)
    backlinks = {path: set() for path in all_pages}
    for page in all_pages.values():
        validate_links(page, page_paths, manifest["sources"])
        for href in links(page.body):
            target, _ = local_target(page.path, href, page_paths)
            if target in backlinks and target != page.path:
                backlinks[target].add(page.path)

    navigation = ["<h2>Project</h2><ul>"]
    for name in ("index.md", "overview.md", "reviews.md", "log.md"):
        label = "All pages" if name == "index.md" else all_pages[name].metadata["title"]
        navigation.append(f'<li><a href="#{page_id(name)}">{escape(label)}</a></li>')
    navigation.append("</ul>")
    for folder in sorted({p.path.split("/")[0] for p in pages.values()}):
        group = sorted(
            (p for p in pages.values() if p.path.startswith(folder + "/")),
            key=lambda p: p.metadata["title"].casefold(),
        )
        label = escape(folder.replace("-", " ").title())
        navigation.append(f"<h2>{label} <span>{len(group)}</span></h2><ul>")
        for page in group:
            navigation.append(
                f'<li><a href="#{page_id(page.path)}">{escape(page.metadata["title"])}</a></li>'
            )
        navigation.append("</ul>")

    sections = []
    for path, page in all_pages.items():
        references = ""
        if backlinks[path]:
            items = "".join(
                f'<li><a href="#{page_id(p)}">{escape(all_pages[p].metadata["title"])}</a></li>'
                for p in sorted(backlinks[path])
            )
            references = (
                f'<section class="backlinks"><h2>Linked from</h2><ul>{items}</ul></section>'
            )
        sections.append(f'''<section class="wiki-page" id="{page_id(path)}" tabindex="-1" aria-label="{escape(page.metadata["title"])}">
  <header><a href="#page-index">All pages</a><span>/</span>{escape(page.metadata["type"].title())}</header>
  <article>{render_markdown(page.body, path, page_paths)}</article>
  {references}
</section>''')

    style = Path(__file__).with_name("style.css").read_text(encoding="utf-8")
    html = f'''<!doctype html>
<html lang="{language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{style}</style>
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<aside>
  <a class="brand" href="#page-index"><span class="mark">D</span>{escape(title)}</a>
  <p class="library-count">{len(pages)} pages &middot; {len(manifest["sources"])} PDFs</p>
  <nav aria-label="Wiki navigation">{"".join(navigation)}</nav>
  <p class="offline">One HTML file &middot; PDFs alongside &middot; available offline</p>
</aside>
<main id="content">
  {"".join(sections)}
  <footer>Built from source documents. Follow citations to inspect the original PDF.</footer>
</main>
</body>
</html>
'''
    # Render and validate everything before replacing the generated export.
    # Rebuilding also removes old multi-file HTML exports and their assets.
    if site.exists():
        shutil.rmtree(site)
    site.mkdir()
    for source_id in manifest["sources"]:
        shutil.copy2(wiki / "pdfs" / f"{source_id}.pdf", site / f"{source_id}.pdf")
    destination = site / "index.html"
    destination.write_text(html, encoding="utf-8")
    shutil.make_archive(str(output / "site"), "zip", site)
    print(f"HTML: {destination}\nShare: {output / 'site.zip'}", flush=True)
    return destination
