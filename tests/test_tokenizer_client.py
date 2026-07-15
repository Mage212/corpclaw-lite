"""Unit tests for B-092 TokenizerClient (offline / mocked HTTP)."""

from __future__ import annotations

import httpx
import pytest

from corpclaw_lite.llm.tokenizer_client import (
    TokenEstimate,
    TokenizerClient,
    estimate_tokens_heuristic,
    normalize_native_base_url,
)


def test_estimate_tokens_heuristic_ascii() -> None:
    text = "abcd" * 25  # 100 chars
    assert estimate_tokens_heuristic(text) == 25  # 100 / 4


def test_estimate_tokens_heuristic_cyrillic() -> None:
    text = "привет" * 10  # non-ASCII heavy
    # utf-8 is 2 bytes per cyrillic char → divisor 2
    encoded_len = len(text.encode("utf-8"))
    assert encoded_len > len(text) * 1.3
    assert estimate_tokens_heuristic(text) == encoded_len // 2


def test_estimate_tokens_heuristic_empty() -> None:
    assert estimate_tokens_heuristic("") == 0


def test_normalize_native_base_url_strips_v1() -> None:
    assert normalize_native_base_url("http://host:8080/v1") == "http://host:8080"
    assert normalize_native_base_url("http://host:8080/v1/") == "http://host:8080"
    assert normalize_native_base_url("http://host:8080/") == "http://host:8080"


@pytest.mark.asyncio
async def test_mode_heuristic_never_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    client = TokenizerClient(
        base_url="http://host:8080/v1",
        mode="heuristic",
        transport=httpx.MockTransport(handler),
    )
    result = await client.estimate("hello world")
    assert result.approximate is True
    assert result.source == "heuristic"
    assert result.n_tokens == estimate_tokens_heuristic("hello world")
    assert calls == 0


@pytest.mark.asyncio
async def test_auto_without_base_url_is_heuristic() -> None:
    client = TokenizerClient(base_url=None, mode="auto")
    result = await client.estimate("abc")
    assert result == TokenEstimate(
        n_tokens=estimate_tokens_heuristic("abc"),
        approximate=True,
        source="heuristic",
    )


@pytest.mark.asyncio
async def test_tokenize_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tokenize"
        assert request.method == "POST"
        return httpx.Response(200, json={"tokens": list(range(42))})

    client = TokenizerClient(
        base_url="http://example.local:8080/v1",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    result = await client.estimate("payload")
    assert result.n_tokens == 42
    assert result.approximate is False
    assert result.source == "tokenize"


@pytest.mark.asyncio
async def test_tokenize_strips_v1_from_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"n_tokens": 7})

    client = TokenizerClient(
        base_url="http://llama.local:8080/v1",
        mode="auto",
        transport=httpx.MockTransport(handler),
    )
    result = await client.estimate("x")
    assert result.n_tokens == 7
    assert result.source == "tokenize"
    assert seen == ["http://llama.local:8080/tokenize"]


@pytest.mark.asyncio
async def test_cache_hit_skips_second_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4, 5]})

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    first = await client.estimate("same-content")
    second = await client.estimate("same-content")
    assert first.source == "tokenize"
    assert first.n_tokens == 5
    assert second.source == "cache"
    assert second.n_tokens == 5
    assert second.approximate is False
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_miss_different_content() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tokens": list(range(calls))})

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    a = await client.estimate("a")
    b = await client.estimate("b")
    assert a.n_tokens == 1
    assert b.n_tokens == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_http_error_falls_back_to_heuristic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    content = "fallback-me"
    result = await client.estimate(content)
    assert result.approximate is True
    assert result.source == "heuristic"
    assert result.n_tokens == estimate_tokens_heuristic(content)


@pytest.mark.asyncio
async def test_bad_json_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    result = await client.estimate("x")
    assert result.source == "heuristic"
    assert result.approximate is True


@pytest.mark.asyncio
async def test_connect_error_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="auto",
        transport=httpx.MockTransport(handler),
    )
    result = await client.estimate("offline")
    assert result.source == "heuristic"
    assert result.approximate is True


@pytest.mark.asyncio
async def test_cache_preserves_heuristic_approximate_flag() -> None:
    client = TokenizerClient(base_url=None, mode="auto")
    first = await client.estimate("cached-heuristic")
    second = await client.estimate("cached-heuristic")
    assert first.source == "heuristic"
    assert first.approximate is True
    assert second.source == "cache"
    assert second.approximate is True
    assert second.n_tokens == first.n_tokens


@pytest.mark.asyncio
async def test_large_content_heuristic_ok() -> None:
    content = "word " * 50_000
    client = TokenizerClient(mode="heuristic")
    result = await client.estimate(content)
    assert result.n_tokens > 0
    assert result.approximate is True


@pytest.mark.asyncio
async def test_cache_max_entries_evicts_oldest() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tokens": [1]})

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        cache_max_entries=2,
        transport=httpx.MockTransport(handler),
    )
    await client.estimate("one")
    await client.estimate("two")
    await client.estimate("three")  # evicts "one"
    await client.estimate("one")  # miss → HTTP again
    assert calls == 4  # one, two, three, one again


def test_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="unsupported tokenizer mode"):
        TokenizerClient(mode="bogus")  # type: ignore[arg-type]
