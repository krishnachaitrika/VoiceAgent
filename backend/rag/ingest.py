import asyncio
import logging
import os
import re
from typing import List, Dict
from database.base import AsyncSessionLocal
from database.crud import store_documents_bulk
from rag.embedder import embed_batch
import config

logger = logging.getLogger(__name__)

CHUNK_SIZE = config.RAG_CHUNK_SIZE
CHUNK_OVERLAP = config.RAG_CHUNK_OVERLAP
# OpenAI's embeddings endpoint accepts a list of inputs in one request, but
# very large documents still need to be split into a few requests to stay
# well under its per-request token/array-size limits.
EMBED_BATCH_SIZE = 100
DOCS_PATH = os.path.join(os.path.dirname(__file__), "../documents/technozis_enquiry.md")

# Matches a markdown heading line (#, ##, ### ...) so headings always start
# a fresh chunk rather than getting buried mid-chunk with unrelated content
# above them.
_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
# Matches sentence-ending punctuation followed by whitespace, used as a
# fallback split point so a chunk is never cut off mid-sentence even when
# a single paragraph is longer than chunk_size on its own.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _split_into_paragraphs(text: str) -> List[str]:
    """
    Split on blank lines AND markdown headings, so a heading like
    '### 2. Salesforce CRM Implementation' always starts a new paragraph
    instead of getting glued onto the end of the previous section.
    """
    # Insert a paragraph break immediately before every heading line, then
    # split on blank lines as normal.
    text_with_breaks = _HEADING_RE.sub(lambda m: "\n\n" + m.group(0), text)
    raw_paragraphs = re.split(r"\n\s*\n", text_with_breaks)
    return [p.strip() for p in raw_paragraphs if p.strip()]


def _split_long_paragraph(paragraph: str, chunk_size: int) -> List[str]:
    """
    A paragraph longer than chunk_size (e.g. a dense bullet list) still
    needs splitting, but at sentence boundaries — never mid-sentence, and
    never mid-bullet-line. Falls back to the paragraph itself if it has no
    clean sentence breaks (e.g. a single very long line) rather than
    slicing it blindly.
    """
    if len(paragraph) <= chunk_size:
        return [paragraph]

    # Prefer splitting on bullet-list line breaks first — keeps each
    # "- Sales Cloud — pipeline management..." style line intact.
    lines = paragraph.split("\n")
    if len(lines) > 1:
        pieces, current = [], ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > chunk_size and current:
                pieces.append(current)
                current = line
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces

    # Single long line/paragraph with no line breaks — split on sentences.
    sentences = _SENTENCE_END_RE.split(paragraph)
    pieces, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > chunk_size and current:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [paragraph]


def _clean_overlap_tail(text: str, overlap: int) -> str:
    """
    Take the last `overlap` characters of `text`, but snap forward to the
    next word boundary rather than slicing blindly — a raw character slice
    can land mid-word (e.g. "cost-effective" → "-effective"), which is the
    same mid-sentence-cut problem this whole rewrite exists to fix, just
    hiding in the overlap logic instead of the main chunk boundary.
    """
    if overlap <= 0 or len(text) <= overlap:
        return text
    tail = text[-overlap:]
    space_idx = tail.find(" ")
    if space_idx == -1:
        return tail  # no space found — single very long word, use as-is
    return tail[space_idx + 1:]


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[Dict]:
    """
    Structure-aware chunking, replacing the previous blind character-count
    slice (which cut mid-sentence and mid-bullet-list, splitting facts
    across chunk boundaries — the likely cause of "missing details" and
    partial-fact hallucination in RAG answers).

    Strategy:
      1. Split on paragraph/heading boundaries first — a heading always
         starts a fresh chunk, so a section's content never gets glued to
         the unrelated section before it.
      2. Greedily pack whole paragraphs together up to chunk_size, so
         short related paragraphs (e.g. a heading + its first sentence)
         stay in the same chunk instead of being needlessly split.
      3. Only a paragraph that's ALREADY longer than chunk_size on its own
         gets split further — and even then, at bullet-line or sentence
         boundaries, never mid-sentence.
      4. Adjacent chunks share `overlap` characters of trailing context
         from the previous chunk, so a fact sitting near a boundary still
         appears complete in at least one chunk.
    """
    paragraphs = _split_into_paragraphs(text)

    # Expand any paragraph that's too long on its own into smaller pieces
    # at clean boundaries, before packing.
    pieces: List[str] = []
    for para in paragraphs:
        pieces.extend(_split_long_paragraph(para, chunk_size))

    # Greedily pack pieces into chunks up to chunk_size, carrying forward
    # `overlap` characters of the previous chunk's tail as a prefix so
    # context isn't lost at the seam.
    chunks: List[Dict] = []
    current = ""
    char_pos = 0
    chunk_start_pos = 0

    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) > chunk_size and current:
            chunks.append({
                "content": current.strip(),
                "start_char": chunk_start_pos,
                "end_char": char_pos,
            })
            # Carry the tail of the current chunk forward as overlap context,
            # snapped to a word boundary so it never starts mid-word.
            tail = _clean_overlap_tail(current, overlap)
            current = f"{tail}\n\n{piece}".strip() if tail else piece
            chunk_start_pos = max(char_pos - len(tail), 0)
        else:
            current = candidate
        char_pos += len(piece) + 2  # account for the "\n\n" join

    if current.strip():
        chunks.append({
            "content": current.strip(),
            "start_char": chunk_start_pos,
            "end_char": char_pos,
        })

    return [c for c in chunks if c["content"]]


async def ingest_text(source_name: str, text: str) -> int:
    """
    Chunk arbitrary text, embed it, and store in pgvector tagged with
    `source_name`. Used by both the CLI script below (for the bundled
    technozis_enquiry.md) and the v2 document-upload API (api/documents.py)
    for anything a user uploads from the dashboard.

    Chunks are embedded in batches (one OpenAI API call per batch of up to
    EMBED_BATCH_SIZE chunks) instead of one call per chunk. Each batch is
    then stored with ONE bulk database commit instead of one commit per
    chunk — previously every single chunk was its own separate
    store_document() call with its own commit, meaning a 39-chunk document
    meant 39 sequential database round trips. On a real upload this alone
    took ~27 seconds just for the storage step (embeddings had already
    finished 27s earlier per the server logs), which was long enough for
    the dashboard's upload request to time out and show "Upload failed"
    even though the backend went on to finish successfully in the
    background. Bulk insert reduces that to one round trip per batch,
    which also means this scales the same way regardless of document size
    — no per-document tuning needed for bigger files.

    Returns the number of chunks stored.
    """
    chunks = chunk_text(text)
    total = len(chunks)
    stored = 0

    async with AsyncSessionLocal() as db:
        for batch_start in range(0, total, EMBED_BATCH_SIZE):
            batch = chunks[batch_start: batch_start + EMBED_BATCH_SIZE]
            try:
                embeddings = await embed_batch([c["content"] for c in batch])
            except Exception as e:
                logger.error(
                    f"Failed to embed batch {batch_start}-{batch_start + len(batch)} "
                    f"of {source_name}: {e}"
                )
                continue

            rows = []
            for offset, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                i = batch_start + offset + 1
                rows.append({
                    "content": chunk["content"],
                    "embedding": embedding,
                    "metadata": {
                        "source": source_name,
                        "chunk_index": i,
                        "start_char": chunk["start_char"],
                        "end_char": chunk["end_char"],
                    },
                })

            try:
                stored += await store_documents_bulk(db, rows)
            except Exception as e:
                logger.error(
                    f"Failed to bulk-store batch {batch_start}-{batch_start + len(batch)} "
                    f"of {source_name}: {e}"
                )
                continue

    return stored


async def ingest_documents() -> None:
    """Read the bundled knowledge base file, chunk it, embed, and store in pgvector."""
    if not os.path.exists(DOCS_PATH):
        logger.error(f"Knowledge base file not found at {DOCS_PATH}")
        return

    with open(DOCS_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    print("📄 Ingesting technozis_enquiry.md...")
    total = await ingest_text("technozis_enquiry.md", text)
    print(f"\n✅ Ingested {total} chunks into pgvector")


if __name__ == "__main__":
    asyncio.run(ingest_documents())