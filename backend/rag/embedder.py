import logging
import time
from typing import List
import config
from services.providers import get_openai_client

logger = logging.getLogger(__name__)


async def embed_text(text: str) -> List[float]:
    """Embed a single string using OpenAI text-embedding-3-small."""
    start = time.perf_counter()
    try:
        response = await get_openai_client().embeddings.create(
            model=config.EMBEDDING_MODEL,
            input=text,
        )
        elapsed = time.perf_counter() - start
        logger.info(f"Embedding generated in {elapsed:.3f}s")
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        raise


async def embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed a batch of strings in one API call."""
    start = time.perf_counter()
    try:
        response = await get_openai_client().embeddings.create(
            model=config.EMBEDDING_MODEL,
            input=texts,
        )
        elapsed = time.perf_counter() - start
        logger.info(f"Batch of {len(texts)} embeddings generated in {elapsed:.3f}s")
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error(f"Batch embedding failed: {e}")
        raise