"""
Run once to ingest the knowledge base into pgvector:
  cd backend
  python scripts/ingest_documents.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rag.ingest import ingest_documents

if __name__ == "__main__":
    asyncio.run(ingest_documents())