import hashlib
import re
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from chatbot.ingestion.loaders import LoadedDoc

HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str | None
    page: int | None
    content: str
    # What gets embedded: the chunk plus its title and heading path, so a chunk like
    # "Refunds take 5 days" still matches a query about the EU refund policy.
    embed_text: str
    content_hash: str


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def chunk_document(doc: LoadedDoc, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    header_splitter = MarkdownHeaderTextSplitter(HEADERS, strip_headers=True)
    text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="o200k_base", chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )

    chunks: list[Chunk] = []
    for page in doc.pages:
        for section in header_splitter.split_text(page.text):
            path = [section.metadata[key] for _, key in HEADERS if key in section.metadata]
            # The top heading is usually the document title, which is stored separately.
            if path and path[0] == doc.title:
                path = path[1:]
            heading = " > ".join(path) or None
            for piece in text_splitter.split_text(section.page_content):
                if not piece.strip():
                    continue
                embed_text = "\n".join(filter(None, [doc.title, heading, "", piece]))
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        heading=heading,
                        page=page.number,
                        content=piece,
                        embed_text=embed_text,
                        content_hash=hashlib.sha256(_normalize(embed_text).encode()).hexdigest(),
                    )
                )
    return chunks
