# doc2wiki — Python POC

Build a connected wiki from PDFs: Markdown, offline HTML, and a shareable ZIP.
Uses an **Azure AI Foundry project**, **Microsoft Agent Framework**, and a
**GPT-5.6 Luna deployment**. No app, chat, query engine, embeddings, or database.

The ingestion follows [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki):
purpose/schema → analysis → page generation → separate existing-page merges →
review items → Markdown/index/log. The implementation favors readable functions
and explicit stages. Start with [`doc2wiki/pipeline.py`](doc2wiki/pipeline.py).

## Setup and run

Python 3.11+, Azure CLI, and a Foundry project with your Luna deployment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
# Fill in the two Foundry settings in .env.
az login

# The repository already includes these files. This command creates missing
# copies when starting another project and never overwrites existing ones.
.\.venv\Scripts\python.exe -m doc2wiki init
# Edit purpose.md and schema.md before importing documents.

New-Item -ItemType Directory -Force input
# Put PDFs in input/, optionally in subfolders.
.\.venv\Scripts\python.exe -m doc2wiki build input --dry-run
.\.venv\Scripts\python.exe -m doc2wiki build input
```

On macOS/Linux use `.venv/bin/python`. Environment variables also work:

```dotenv
FOUNDRY_PROJECT_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com/api/projects/YOUR-PROJECT
FOUNDRY_MODEL=YOUR-LUNA-DEPLOYMENT-NAME
```

Use the **project endpoint**, including `/api/projects/...`, and the **deployment
name**, which can differ from the model ID. Your Azure CLI identity needs access
to that deployment. The pipeline creates an `AIProjectClient`, passes it into
`FoundryChatClient`, then calls `Agent.run()` with a Pydantic response schema.
Calls have separate sessions and close their network clients. No persistent
agent resource or OpenAI API key is required. See Microsoft's
[Foundry provider](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
and [structured output](https://learn.microsoft.com/en-us/agent-framework/agents/structured-outputs)
documentation.

The dry run extracts all PDF pages and reports approximate token counts. It does
not call the model or write wiki files. The first tokenizer use may download its
vocabulary. It is not a cost estimate for all pipeline stages.

## Purpose and schema

**[`purpose.md`](purpose.md)** contains the goal, key questions, scope, and thesis.
This is a real prompt input: the entire file is inserted into the **system
instructions for analysis, generation, merging, chunk analysis, and reviews**.
Replace the generic defaults with the questions you want this wiki to answer.

**[`schema.md`](schema.md)** controls page types, folder routing, and writing
conventions. Defaults include source, entity, concept, comparison, and synthesis.
For example, add this row to introduce a methodology type:

```markdown
| methodology | wiki/methodologies/ | Experimental methods, assumptions, and limitations. |
```

Types and folders must be unique lowercase kebab-case names. Folders are one
level below `wiki/`; keep the `source | wiki/sources/` row. Routing is validated
in Python, while the remaining prose is passed to the model. Custom types also
appear in Markdown navigation and HTML. There is no 12-page limit.

```powershell
.\.venv\Scripts\python.exe -m doc2wiki build input `
  --output output/energy --title "Energy Research" --language German `
  --purpose-file research-purpose.md --schema-file research-schema.md
```

By default, the current directory's `purpose.md` and `schema.md` are used, or the
bundled templates if absent. An explicitly supplied missing file is an error.
`--purpose "..."` is a short-text override, mutually exclusive with `--purpose-file`.
Purpose/schema edits affect **new ingestions**; unchanged PDFs are still skipped.
Rebuild into a new output directory to apply new guidance to the entire corpus.
Each ingestion saves a config snapshot under `.state/config/` for provenance.

## Ingestion flow

1. **Extract:** Read all text with `pypdf` layout extraction, retaining physical
   PDF page numbers. Source IDs include a filename and relative-path hash.
2. **Analyze:** Identify central entities/concepts, arguments, supporting evidence,
   evidence strength, scope boundaries, connections, contradictions, and open
   questions. Plan source summaries and meaningful shared pages under the schema.
   Analysis sees the existing page catalog and current overview.
3. **Generate:** Produce the planned contributions, with tags, related pages,
   `[[wikilinks]]`, and page-level PDF citations. Prompts require exact structured
   data, including tables, SQL DDL, schemas, API signatures, configuration, values,
   types, constraints, keys, and indexes. Generation uses the source and analysis.
4. **Repair incomplete output:** Retry only omitted pages once. If the combined
   response hits its output allowance, generate each planned page individually.
   A single page that still exhausts the allowance stops the run. If the source
   summary is omitted twice, preserve an explicitly labeled analysis-based summary
   and a review item, matching the reference's guaranteed-source-page behavior.
5. **Merge:** For each existing page with a different incoming body, make a
   **separate model call** with both complete versions. Integrate facts, preserve
   caveats, and attribute disagreements. Python unions `sources`, `tags`, and
   `related`, locks `type`, `title`, and `created`, and updates `updated`.
   Identical bodies skip the merge call. Reject a merge shorter than 70% of the
   longer input or missing any distinct PDF page citation from either input.
6. **Review:** Collect contradiction, duplicate, missing-page, and suggestion
   items from generation/merging. Like upstream, run an additional review pass
   when output reaches 10,000 characters, produces at least four pages, or already
   contains reviews. Suggested searches are text only; nothing searches the web.
   A failed optional review pass leaves a visible follow-up item.
7. **Save and export:** Validate paths, planned-page coverage, links, citations,
   and metadata before overwriting pages. Back up replaced Markdown. Update the
   index, overview, log, review report, manifest, and offline HTML/ZIP.

Add PDFs to the same input folder and rerun. SHA-256 hashes skip unchanged files.
Changing/removing an imported PDF, or changing the title/language, requires a new
output directory. No source deletion or replacement workflow is included.

## Long PDFs and Luna's context

The [Luna model specification](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
lists **1,050,000 context tokens** and **128,000 maximum output tokens** (checked
September 21, 2026). Normal ten-page PDFs are processed whole. Page count alone
does not determine size; density and the size of existing wiki pages also matter.

Following the reference, sources exceeding **300,000 characters** use semantic
chunks targeting **60,000 characters**, preferring paragraph/line/sentence
boundaries. Every chunk retains its original PDF page markers. Chunk analysis
receives limited overlap plus a rolling global digest and writes detailed notes
with exact evidence. Checkpoints resume completed chunks after interruption and
are invalidated by changed source, guidance, catalog, or deployment.

All chunk notes and the final digest feed a consolidation analysis/page plan,
then generation. Detailed notes are never silently trimmed. This adds an explicit
consolidation call to the reference's digest-and-notes approach. Like upstream,
long-source output depends on the chunk analyzer preserving the evidence.

Every request estimates the entire system/user/schema payload with `o200k_base`,
adds 10% plus 1,024 tokens, reserves 16,000 tokens of headroom, and reserves the
requested output: **8,192** for analysis/chunks/reviews or **32,768** for generation
and merging. These allowances include reasoning; effort is `low`. This matches
upstream's 32,768 generation allowance for large contexts while giving analysis
more room. The tokenizer is an approximation. API truncation is disabled.

An oversized catalog, merged page, or accumulated chunk analysis still fails
explicitly at the context guard. This is for modest POC collections, not unbounded
corpora. Azure rate limits can be lower than model capacity. The deployment must
actually serve Luna; the POC does not discover its model or limits automatically.

## Read and share

Open **`output/site/index.html`** directly. Share **`output/site.zip`**; recipients
unzip it and open `index.html`. No server, JavaScript, CDN, or credentials needed.
Original PDFs are included so citations work offline. `#page=N` support depends
on the recipient's PDF viewer.

Use **`output/wiki/`** as the Markdown vault. Wikilinks support `[[slug]]`,
`[[slug|label]]`, and `[[folder/slug|label]]` for ambiguous slugs. HTML resolves
these links and creates backlinks. Fenced and inline code remain literal.
Ordinary relative Markdown page links are also accepted. Cross-page heading
fragments and external links are not supported in this POC.

`reviews.md` and its HTML page expose the review items. After the final build you
can record decisions in the Markdown, edit knowledge pages, and export again:

```powershell
.\.venv\Scripts\python.exe -m doc2wiki export output
```

Export makes no model calls and preserves edited review/log/overview content.
A new ingestion regenerates those reports from the manifest; it is not an
interactive review-management app. Keep metadata and link conventions intact.

```text
output/
  wiki/
    index.md, overview.md, log.md, reviews.md
    sources/, entities/, concepts/, comparisons/, syntheses/, ...
    pdfs/                       # Original documents
  site/                         # Same structure, HTML plus CSS/PDFs
  site.zip                      # Share this; excludes state and credentials
  .state/
    manifest.json               # Imported hashes, settings, reviews, log
    config/                     # Per-source purpose/schema snapshots
    analysis/, generation/      # Plans and generated contributions
    merges/, reviews/           # Separate stage results
    chunks/                     # Resumable long-source analysis
    extracted/                  # Complete text with page markers
    backups/                    # Previous Markdown versions
    usage.json                  # Available API usage for this process
```

## Fidelity and remaining limits

The analysis/generation instructions, purpose/schema roles, long-source digest
flow, review triggers/types, separate merges, metadata unions/locks, 70% merge
check, and source-summary fallback follow the reference implementation at
[`e808211`](https://github.com/nashsu/llm_wiki/tree/e8082119649e6a8e1cf85eaf289adcabfdf39d4e).
Primary reference files are
[`ingest.ts`](https://github.com/nashsu/llm_wiki/blob/e8082119649e6a8e1cf85eaf289adcabfdf39d4e/src/lib/ingest.ts),
[`page-merge.ts`](https://github.com/nashsu/llm_wiki/blob/e8082119649e6a8e1cf85eaf289adcabfdf39d4e/src/lib/page-merge.ts),
and [`templates.ts`](https://github.com/nashsu/llm_wiki/blob/e8082119649e6a8e1cf85eaf289adcabfdf39d4e/src/lib/templates.ts).

Intentional differences and limits:

- **Typed JSON replaces FILE/REVIEW block parsing.** Python writes frontmatter
  and deterministic navigation, a coverage overview, ingestion log, and reports.
  No LLM-written global overview essay or query/research interface is included.
- **Failed merges stop before overwriting pages.** Upstream can fall back to
  the incoming body; this POC preserves the existing wiki instead. It also checks
  individual PDF page citations, not just source identities. These checks cannot
  prove that every fact survived or that a cited page supports the claim.
- **Selectable-text PDFs only.** No OCR, visual chart interpretation, or image
  extraction. Empty text pages fail with an actionable message. Layout extraction
  can still misread complex tables/formulas. This differs from upstream's richer
  document-conversion integrations and limits quality on visually complex PDFs.
- ASCII slugs and one-level type folders keep routing readable and portable.
  Titles/prose can use any language. HTML navigation labels are English.
- No concurrency, disk-write transaction recovery, automatic source replacement,
  search, math/diagram renderer, or review action executor.
- Calls send extracted content to your configured Azure project with `store=False`.
  SDK parse failures can occur before token usage is exposed, so `usage.json` is
  diagnostic rather than a complete billing record. No fallback model is used.

**Equivalent output quality has not been established.** The restored workflow
reduces architectural differences, but quality needs a side-by-side run on the
same PDFs, purpose/schema, and live Azure deployment. All automated tests use
simulated model responses; no live Luna quality result is claimed.

## Code and validation

| File | Responsibility |
| --- | --- |
| `cli.py`, `config.py` | Commands and editable purpose/schema inputs |
| `pdf.py`, `long_source.py` | Extraction, semantic chunks, digest checkpoints |
| `prompts.py` | Full, readable stage instructions |
| `llm.py` | Typed results, Luna budget, Foundry/Agent Framework calls |
| `pipeline.py`, `merge.py` | Ingestion stages, output repair, separate merges |
| `wiki.py`, `storage.py` | Metadata, links, reports, and JSON storage |
| `render.py`, `style.css` | Offline HTML and ZIP |

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check doc2wiki tests
```

Tests use real ten-page PDF fixtures and simulated LLM results. They cover
incremental ingestion, separate merges, locked/union metadata, page-citation
retention, custom schema routing, purpose injection, reviews, wikilink rendering,
output repair, long-source checkpoint recovery, and HTML/ZIP export. Integration
tests exercise the real Microsoft Agent Framework, Foundry client, and project
SDK against a mock HTTP transport, including truncated JSON and client cleanup.

See `NOTICE` and `LICENSE` for upstream attribution and GPLv3 licensing.
