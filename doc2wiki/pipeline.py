"""Extract -> analyze -> generate -> merge existing pages -> review -> save/export."""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from agent_framework.exceptions import AgentFrameworkException
from azure.core.exceptions import AzureError

from .config import read_config, schema_folders
from .llm import (
    LLM,
    MAX_OUTPUT_TOKENS,
    Analysis,
    Generation,
    IncompleteResponse,
    PageDraft,
    PagePlan,
    Review,
    ReviewSuggestions,
)
from .long_source import source_context
from .merge import merge_pages
from .pdf import Source, read_pdf
from .prompts import ANALYZE, GENERATE, REVIEW, instructions
from .storage import write_json
from .wiki import (
    PAGE_PATH,
    Page,
    catalog,
    load_pages,
    resolve_wikilink,
    validate_links,
    write_navigation,
    write_reports,
)


def discover_pdfs(input_dir: Path, output: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise ValueError(f"PDF input must be a directory: {input_dir}")
    if input_dir == output or output in input_dir.parents:
        raise ValueError("The output directory cannot contain the input directory.")
    paths = sorted(
        p
        for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".pdf" and output not in p.parents
    )
    if not paths:
        raise ValueError(f"No PDF files found in {input_dir}.")
    return paths


def validate_plan(
    analysis: Analysis,
    source: Source,
    pages: dict[str, Page],
    folders: dict[str, str] | None = None,
) -> None:
    folders = folders or schema_folders(read_config(None, "schema.md"))
    paths = [page.path for page in analysis.pages]
    if len(paths) != len(set(paths)):
        raise ValueError("Analysis must plan unique pages.")
    if source.summary_path not in paths:
        analysis.pages.insert(
            0,
            PagePlan(
                path=source.summary_path,
                title=Path(source.name).stem,
                instructions="Summarize the entire PDF and preserve exact structured data.",
            ),
        )
    for page in analysis.pages:
        if (
            not PAGE_PATH.fullmatch(page.path)
            or page.path.split("/")[0] not in folders
            or not page.title.strip()
        ):
            raise ValueError(f"Invalid planned page: {page.path}")
        if page.path.startswith("sources/") and page.path != source.summary_path:
            raise ValueError("A PDF may only write its own source summary.")
        if page.path in pages:
            page.title = pages[page.path].metadata["title"]


def generate_pages(context: dict, purpose: str, schema: str, llm) -> Generation:
    """One normal call; only missing/truncated output gets a per-page repair pass."""
    plan = context["analysis"]["pages"]
    system = instructions(GENERATE, purpose, schema)
    try:
        result = llm.ask(
            system,
            json.dumps({**context, "requested_pages": plan}, ensure_ascii=False),
            Generation,
            MAX_OUTPUT_TOKENS,
        )
    except IncompleteResponse:
        if len(plan) == 1:
            raise
        print("  Output limit reached; generating each planned page separately.", flush=True)
        result = Generation(pages=[])
    expected = {p["path"] for p in plan}
    returned = [p.path for p in result.pages]
    if len(returned) != len(set(returned)) or not set(returned).issubset(expected):
        raise ValueError("Generation returned duplicate or unplanned paths.")
    for planned in plan:
        if planned["path"] in returned:
            continue
        print(f"  Repair missing page: {planned['path']}", flush=True)
        repaired = llm.ask(
            system,
            json.dumps({**context, "requested_pages": [planned]}, ensure_ascii=False),
            Generation,
            MAX_OUTPUT_TOKENS,
        )
        if [p.path for p in repaired.pages] != [planned["path"]]:
            if not repaired.pages and planned["path"] == context["source"]["summary_path"]:
                # Upstream also guarantees a source page when the generator omits it.
                source = context["source"]
                repaired.pages = [
                    PageDraft(
                        path=planned["path"],
                        summary="Analysis-based source summary; review required.",
                        body=f"# {planned['title']}\n\n> Generation omitted this source summary. "
                        "This fallback contains the source analysis and needs review.\n\n"
                        + context["analysis"]["findings"]
                        + f"\n\n[Original PDF, p. 1](../pdfs/{source['id']}.pdf#page=1)",
                    )
                ]
                repaired.reviews.append(
                    Review(
                        kind="suggestion",
                        title="Review fallback source summary",
                        description=f"Generation omitted {planned['path']} twice. Check the analysis-based summary against the PDF.",
                        pages=[planned["path"]],
                    )
                )
            else:
                raise ValueError(f"Repair did not return the requested page: {planned['path']}")
        result.pages.extend(repaired.pages)
        result.reviews.extend(repaired.reviews)
    return result


def prepare_pages(
    generation: Generation,
    analysis: Analysis,
    source: Source,
    pages: dict[str, Page],
    sources: dict,
    folders: dict[str, str] | None = None,
) -> dict[str, Page]:
    folders = folders or schema_folders(read_config(None, "schema.md"))
    plan = {p.path: p for p in analysis.pages}
    paths = [p.path for p in generation.pages]
    if set(paths) != set(plan) or len(paths) != len(set(paths)):
        raise ValueError("Generation did not return exactly the planned pages.")
    result = {}
    today = datetime.now(UTC).date().isoformat()
    known_paths = set(pages) | set(plan)
    for draft in generation.pages:
        old = pages.get(draft.path)
        if not draft.body.startswith("# ") or not draft.summary.strip():
            raise ValueError(f"{draft.path}: missing heading or catalog summary.")
        metadata = {
            "title": plan[draft.path].title,
            "type": folders[draft.path.split("/")[0]],
            "summary": " ".join(draft.summary.split()),
            "created": today,
            "updated": today,
            "sources": [source.id],
            "tags": draft.tags,
            "related": draft.related,
        }
        if old:
            for field in ("title", "type", "created"):
                metadata[field] = old.metadata[field]
            for field in ("sources", "tags", "related"):
                metadata[field] = sorted(set(old.metadata.get(field, [])) | set(metadata[field]))
        for target in metadata["related"]:
            resolve_wikilink(target, known_paths)
        page = Page(draft.path, metadata, draft.body)
        cited = validate_links(page, known_paths, sources)
        if source.id not in cited:
            raise ValueError(f"{draft.path}: missing citation to the current PDF.")
        page.metadata["sources"] = sorted(set(page.metadata["sources"]) | cited)
        result[page.path] = page
    return result


def collect_reviews(
    generation: Generation, updates: dict[str, Page], context: dict, purpose: str, schema: str, llm
) -> list[Review]:
    reviews = list(generation.reviews)
    # Same trigger as upstream: substantial output, four pages, or an emitted review.
    if len(updates) >= 4 or sum(len(p.body) for p in updates.values()) >= 10_000 or reviews:
        try:
            result = llm.ask(
                instructions(REVIEW, purpose, schema),
                json.dumps(
                    {
                        **context,
                        "generated_pages": {path: p.markdown() for path, p in updates.items()},
                        "existing_reviews": [r.model_dump() for r in reviews],
                    },
                    ensure_ascii=False,
                ),
                ReviewSuggestions,
                8_192,
            )
            reviews.extend(result.reviews)
        except (ValueError, AgentFrameworkException, AzureError) as exc:
            # Optional follow-up discovery must not discard successfully generated pages.
            print(f"  Review suggestions failed: {exc}", flush=True)
            reviews.append(
                Review(
                    kind="suggestion",
                    title="Review pass incomplete",
                    description="The optional review call failed. Check this source for unresolved gaps and contradictions.",
                    pages=[context["source"]["summary_path"]],
                )
            )
    unique = {}
    for review in reviews:
        key = (review.kind, review.title.casefold().strip(), tuple(sorted(review.pages)))
        unique.setdefault(key, review)
    return list(unique.values())


def build(
    input_dir: Path,
    output: Path,
    *,
    title: str = "Document Wiki",
    language: str = "English",
    purpose: str | None = None,
    purpose_file: Path | None = None,
    schema_file: Path | None = None,
    llm: LLM | None = None,
) -> None:
    input_dir, output = input_dir.resolve(), output.resolve()
    paths = discover_pdfs(input_dir, output)
    purpose = purpose if purpose is not None else read_config(purpose_file, "purpose.md")
    if not purpose.strip():
        raise ValueError("Purpose must not be empty.")
    schema = read_config(schema_file, "schema.md")
    folders = schema_folders(schema)
    state, wiki = output / ".state", output / "wiki"
    manifest_path = state / "manifest.json"
    settings = {"title": title, "language": language, "purpose": purpose, "schema": schema}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(manifest["settings"][key] != settings[key] for key in ("title", "language")):
            raise ValueError(
                "Wiki title/language changed. Use the original settings or a new --output."
            )
    else:
        if output.exists() and any(output.iterdir()):
            raise ValueError("Output is not empty and has no manifest. Choose a new --output.")
        manifest = {"settings": settings, "sources": {}}

    known = {entry["name"]: entry for entry in manifest["sources"].values()}
    missing = set(known) - {p.relative_to(input_dir).as_posix() for p in paths}
    if missing:
        raise ValueError(
            f"Previously imported PDFs were removed: {', '.join(sorted(missing))}. "
            "Rebuild into a new --output directory."
        )
    pending = []
    for path in paths:
        name = path.relative_to(input_dir).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if name in known:
            if digest != known[name]["sha256"]:
                raise ValueError(f"{name} changed. Rebuild into a new --output directory.")
            print(f"Skip unchanged: {name}", flush=True)
        else:
            pending.append(read_pdf(path, name))

    pages = load_pages(wiki)
    if pending and llm is None:
        llm = LLM()
    manifest["settings"] = settings
    write_json(manifest_path, manifest)
    for source in pending:
        print(f"Ingest: {source.name} ({len(source.pages)} pages)", flush=True)
        available_sources = {
            **manifest["sources"],
            source.id: {
                "name": source.name,
                "sha256": source.sha256,
                "page_count": len(source.pages),
            },
        }
        context = {
            "settings": {"title": title, "language": language},
            "source": {"id": source.id, "name": source.name, "summary_path": source.summary_path},
            "catalog": catalog(pages),
            "available_sources": available_sources,
            "overview": (wiki / "overview.md").read_text(encoding="utf-8")
            if (wiki / "overview.md").exists()
            else "",
        }
        write_json(state / "config" / f"{source.id}.json", settings)
        context["source_text"] = source_context(source, context, purpose, schema, llm, state)
        analysis = llm.ask(
            instructions(ANALYZE, purpose, schema),
            json.dumps(context, ensure_ascii=False),
            Analysis,
            8_192,
        )
        validate_plan(analysis, source, pages, folders)
        write_json(state / "analysis" / f"{source.id}.json", analysis.model_dump())
        context["analysis"] = analysis.model_dump()
        generation = generate_pages(context, purpose, schema, llm)
        write_json(state / "generation" / f"{source.id}.json", generation.model_dump())
        updates = prepare_pages(generation, analysis, source, pages, available_sources, folders)
        generation.reviews.extend(merge_pages(updates, pages, context, purpose, schema, llm, state))
        reviews = collect_reviews(generation, updates, context, purpose, schema, llm)
        write_json(
            state / "reviews" / f"{source.id}.json", {"reviews": [r.model_dump() for r in reviews]}
        )

        # All required model output and links are checked before replacing any page.
        for path, page in updates.items():
            destination = wiki / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                backup = state / "backups" / source.id / path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            destination.write_text(page.markdown(), encoding="utf-8")
        (wiki / "pdfs").mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.path, wiki / "pdfs" / f"{source.id}.pdf")
        (state / "extracted").mkdir(parents=True, exist_ok=True)
        (state / "extracted" / f"{source.id}.txt").write_text(source.text, encoding="utf-8")
        manifest.setdefault("reviews", []).extend(
            {**r.model_dump(), "source_name": source.name, "source_id": source.id} for r in reviews
        )
        manifest.setdefault("log", []).append(
            {
                "date": datetime.now(UTC).isoformat(),
                "source_name": source.name,
                "created": len(set(updates) - set(pages)),
                "updated": len(set(updates) & set(pages)),
                "reviews": len(reviews),
                "pages": list(updates),
            }
        )
        pages.update(updates)
        manifest["sources"] = available_sources
        write_navigation(wiki, pages, title)
        write_reports(wiki, pages, manifest)
        write_json(manifest_path, manifest)
        write_json(
            state / "usage.json",
            {"provider": "azure-ai-foundry", "model": "gpt-5.6-luna", "calls_this_run": llm.usage},
        )
        print(f"  Saved {len(updates)} pages; {len(reviews)} review items", flush=True)

    # Also supports exporting an older POC output before any new PDFs are added.
    if pages and not (wiki / "overview.md").exists():
        write_reports(wiki, pages, manifest)
    from .render import export_html

    export_html(output)
