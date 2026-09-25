"""
Tests for VA-A2: external clients must be lazily constructed, not built as
a side effect of importing the module.
"""
from services.providers import get_openai_client
from database.base import get_engine


def test_openai_client_not_constructed_until_first_call():
    get_openai_client.cache_clear()
    assert get_openai_client.cache_info().currsize == 0
    client = get_openai_client()
    assert client is not None
    assert get_openai_client.cache_info().currsize == 1


def test_openai_client_is_a_cached_singleton():
    get_openai_client.cache_clear()
    first = get_openai_client()
    second = get_openai_client()
    assert first is second


def test_db_engine_not_constructed_until_first_call():
    get_engine.cache_clear()
    assert get_engine.cache_info().currsize == 0
    engine = get_engine()
    assert engine is not None
    assert get_engine.cache_info().currsize == 1


def test_db_engine_is_a_cached_singleton():
    get_engine.cache_clear()
    first = get_engine()
    second = get_engine()
    assert first is second
