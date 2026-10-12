"""Turn files into markdown-ish text, one entry per page (page is None for non-paged formats)."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import docx
import pymupdf
from markdownify import markdownify


@dataclass(frozen=True)
class Page:
    number: int | None
    text: str


@dataclass(frozen=True)
class LoadedDoc:
    path: Path
    title: str
    pages: list[Page]


def _first_heading(text: str) -> str | None:
    match = re.search(r"^#{1,2}\s+(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _load_text(path: Path) -> list[Page]:
    return [Page(None, path.read_text(encoding="utf-8"))]


def _load_html(path: Path) -> list[Page]:
    html = path.read_text(encoding="utf-8")
    # Drop <head> so <title>/<style>/<script> text doesn't end up in the body.
    html = re.sub(r"<head.*?</head>", "", html, flags=re.DOTALL | re.IGNORECASE)
    md = markdownify(html, heading_style="ATX", strip=["script", "style"])
    return [Page(None, re.sub(r"\n{3,}", "\n\n", md).strip())]


def _load_docx(path: Path) -> list[Page]:
    lines = []
    for para in docx.Document(str(path)).paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name if para.style is not None else ""
        if style.startswith("Heading ") and style[8:].isdigit():
            lines.append(f"{'#' * int(style[8:])} {text}")
        else:
            lines.append(text)
    return [Page(None, "\n\n".join(lines))]


def _load_pdf(path: Path) -> list[Page]:
    with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
        return [
            Page(i + 1, text) for i, page in enumerate(pdf) if (text := page.get_text().strip())
        ]


LOADERS: dict[str, Callable[[Path], list[Page]]] = {
    ".md": _load_text,
    ".markdown": _load_text,
    ".txt": _load_text,
    ".html": _load_html,
    ".htm": _load_html,
    ".docx": _load_docx,
    ".pdf": _load_pdf,
}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in LOADERS


def load(path: Path) -> LoadedDoc:
    pages = LOADERS[path.suffix.lower()](path)
    title = next((h for p in pages if (h := _first_heading(p.text))), None)
    return LoadedDoc(path=path, title=title or path.stem.replace("-", " ").title(), pages=pages)
