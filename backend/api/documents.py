"""
api/documents.py — v2 "Document Upload UI" from the architecture diagram.

Lets someone upload a .md, .txt, or .pdf file straight from the dashboard
(Settings → Knowledge Base), instead of only being able to run
scripts/ingest_documents.py by hand. Each upload is chunked (same
500-token / 50-overlap chunker as the CLI script), embedded with
text-embedding-3-small, and stored in the same pgvector `documents` table
— so search_kb picks it up on the very next call, no redeploy needed.
"""
import asyncio
import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database.base import get_db
from cache.kb_version import invalidate_kb_version
from database import crud
from rag.ingest import ingest_text
from auth import require_dashboard_auth, require_dashboard_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", dependencies=[Depends(require_dashboard_auth)])

ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB


def _extract_text(filename: str, raw: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Allowed: .md, .txt, .pdf")

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise HTTPException(status_code=500, detail="pypdf is not installed on the server")
        reader = PdfReader(BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")


@router.post("/upload", dependencies=[Depends(require_dashboard_admin)])
async def upload_document(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Upload a knowledge-base document. Chunks + embeds + stores immediately."""
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")

    # PDF extraction (pypdf's PdfReader().extract_text()) is a blocking,
    # CPU/IO-bound call — run it in a worker thread so it doesn't stall the
    # shared event loop (the same loop handles live Twilio calls) while
    # parsing a large upload (up to MAX_UPLOAD_BYTES).
    text = await asyncio.to_thread(_extract_text, file.filename, raw)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No extractable text found in file")

    try:
        chunk_count = await ingest_text(file.filename, text)
        # Same reasoning as the delete path: make the new document answerable
        # on the very next turn instead of after KB_VERSION_TTL_SECONDS, and
        # make any previously cached answer to the same question unreachable.
        # VA-T-003 was exactly this — a corrected fact re-uploaded, while every
        # already-cached answer went on serving the old one.
        invalidate_kb_version()
    except Exception as e:
        logger.error(f"Document ingestion failed for {file.filename}: {e}")
        raise HTTPException(status_code=500, detail="Failed to ingest document")

    logger.info(f"Ingested '{file.filename}' — {chunk_count} chunks")
    return {"filename": file.filename, "chunks_stored": chunk_count}


@router.get("")
async def list_documents(db: AsyncSession = Depends(get_db)):
    """List all uploaded knowledge-base sources with their chunk counts."""
    sources = await crud.get_document_sources(db)
    return {"documents": sources}


@router.delete("/{source}", dependencies=[Depends(require_dashboard_admin)])
async def delete_document(
    source: str,
    confirm: bool = Query(False, description="Must be true — deletion is immediate and permanent"),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove every chunk belonging to one uploaded source file (VA-B4 fix:
    requires an explicit confirm=true, since this permanently deletes
    every chunk under `source` in one call with no undo).
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to permanently delete this source")

    existing = await crud.get_document_sources(db)
    if not any(doc["source"] == source for doc in existing):
        raise HTTPException(status_code=404, detail="Source not found")

    deleted = await crud.delete_document_source(db, source)
    # VA-T-016: without this the knowledge-base version only refreshes after
    # KB_VERSION_TTL_SECONDS, so for that window the RAG cache would still be
    # keyed on the OLD version and callers would keep hearing the deleted
    # document. Deletion is precisely the case where "eventually" is not good
    # enough — the content is usually being removed because it must stop being
    # said.
    invalidate_kb_version()
    logger.info(
        f"Deleted source {source!r} ({deleted} chunks) and invalidated the "
        f"knowledge-base version — cached answers and retrievals for the "
        f"previous version are now unreachable."
    )
    return {"message": f"Deleted {deleted} chunks", "source": source}