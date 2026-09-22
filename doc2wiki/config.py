"""User-editable project purpose and schema, with a small Markdown table parser."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).with_name("templates")


def read_config(path: Path | None, name: str) -> str:
    if path is None:
        path = Path(name) if Path(name).is_file() else TEMPLATES / name
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path} must not be empty.")
    return text


def schema_folders(schema: str) -> dict[str, str]:
    """Read | Type | Folder | Description | rows. Folders stay one level deep."""
    folders = {}
    types = set()
    for line in schema.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[1].startswith("wiki/"):
            continue
        page_type, folder = cells[0], cells[1].removeprefix("wiki/").rstrip("/")
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]*", page_type)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", folder)
            or folder in {"pdfs", "media"}
        ):
            raise ValueError(f"Invalid schema type/folder: {line}")
        if folder in folders or page_type in types:
            raise ValueError(f"Duplicate schema type/folder: {line}")
        folders[folder] = page_type
        types.add(page_type)
    if folders.get("sources") != "source":
        raise ValueError("schema.md must include a '| source | wiki/sources/ | ... |' row.")
    return folders


def init_config(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("purpose.md", "schema.md"):
        destination = directory / name
        if destination.exists():
            print(f"Keep existing: {destination}")
        else:
            destination.write_text((TEMPLATES / name).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Created: {destination}")
