"""Prepare PDFs explicitly, ingest them, or re-export edited Markdown."""

import argparse
from pathlib import Path

from agent_framework.exceptions import AgentFrameworkException
from azure.core.exceptions import AzureError
from dotenv import load_dotenv
from pypdf.errors import PdfReadError

from .config import init_config
from .ingestion.pdf import read_pdf
from .llm.client import estimate_tokens
from .pipeline import build, discover_pdfs
from .wiki.html import export_html


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Turn PDFs into a Markdown and HTML wiki.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Create editable purpose, schema, and category files without overwriting."
    )
    init.add_argument("directory", type=Path, nargs="?", default=Path("."))
    ocr = commands.add_parser("ocr", help="One-time Azure Read OCR to a PDF with embedded text.")
    ocr.add_argument("source", type=Path, help="Original PDF file.")
    ocr.add_argument("--output", type=Path, required=True, help="Separate searchable PDF file.")
    split = commands.add_parser("split", help="One-time logical PDF splitting and categorization.")
    split.add_argument("source", type=Path, help="Searchable PDF file, optionally from OCR.")
    split.add_argument("--output", type=Path, required=True, help="Dedicated folder for this PDF.")
    split.add_argument(
        "--categories-file", type=Path, help="Defaults to ./categories.yaml or template."
    )
    split.add_argument("--batch-pages", type=int, default=30, help="Pages per model call (1-50).")
    split.add_argument(
        "--max-pages", type=int, help="Optionally cap each part of a logical document."
    )
    ingest = commands.add_parser("build", help="Add new PDFs to a wiki, then export HTML.")
    ingest.add_argument("input", type=Path, help="PDF directory (searched recursively).")
    ingest.add_argument("--output", type=Path, default=Path("output"))
    ingest.add_argument("--title", default="Document Wiki")
    ingest.add_argument("--language", default="English")
    purpose = ingest.add_mutually_exclusive_group()
    purpose.add_argument("--purpose", help="Override purpose.md with this text.")
    purpose.add_argument(
        "--purpose-file", type=Path, help="Defaults to ./purpose.md, or bundled template."
    )
    ingest.add_argument(
        "--schema-file", type=Path, help="Defaults to ./schema.md, or bundled template."
    )
    ingest.add_argument(
        "--dry-run", action="store_true", help="Check PDF text; no API calls or writes."
    )
    export = commands.add_parser(
        "export", help="Rebuild HTML from existing Markdown; no API calls."
    )
    export.add_argument("output", type=Path, nargs="?", default=Path("output"))
    args = parser.parse_args()
    try:
        if args.command == "init":
            init_config(args.directory)
        elif args.command == "ocr":
            from .preprocessing.ocr import searchable_pdf

            searchable_pdf(args.source, args.output)
        elif args.command == "split":
            from .preprocessing.split import split_pdf

            split_pdf(
                args.source,
                args.output,
                categories_file=args.categories_file,
                batch_pages=args.batch_pages,
                max_pages=args.max_pages,
            )
        elif args.command == "export":
            export_html(args.output.resolve())
        elif args.dry_run:
            root, output = args.input.resolve(), args.output.resolve()
            for path in discover_pdfs(root, output):
                source = read_pdf(path, path.relative_to(root).as_posix())
                print(
                    f"{source.name}: {len(source.pages)} pages, "
                    f"~{estimate_tokens(source.text):,} text tokens"
                )
        else:
            build(
                args.input,
                args.output,
                title=args.title,
                language=args.language,
                purpose=args.purpose,
                purpose_file=args.purpose_file,
                schema_file=args.schema_file,
            )
    except (ValueError, OSError, AgentFrameworkException, AzureError, PdfReadError) as exc:
        parser.exit(1, f"Error: {exc}\n")
