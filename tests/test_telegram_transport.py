"""Tests for Telegram fallback transport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from corpclaw_lite.channels.telegram.transport import (
    TelegramFallbackTransport,
    _is_retryable_connect_error,
    _normalize_fallback_ips,
    _rewrite_request_for_ip,
    discover_fallback_ips,
    parse_fallback_ip_env,
)


class TestNormalizeFallbackIps:
    def test_valid_public_ipv4(self) -> None:
        result = _normalize_fallback_ips(["149.154.167.220", "1.1.1.1"])
        assert result == ["149.154.167.220", "1.1.1.1"]

    def test_empty_input(self) -> None:
        assert _normalize_fallback_ips([]) == []

    def test_strips_whitespace(self) -> None:
        result = _normalize_fallback_ips(["  149.154.167.220  "])
        assert result == ["149.154.167.220"]

    def test_ignores_empty_strings(self) -> None:
        result = _normalize_fallback_ips(["", "  ", "149.154.167.220"])
        assert result == ["149.154.167.220"]

    def test_ignores_invalid_ip(self) -> None:
        result = _normalize_fallback_ips(["not-an-ip", "999.999.999.999"])
        assert result == []

    def test_ignores_ipv6(self) -> None:
        result = _normalize_fallback_ips(["::1", "2001:db8::1"])
        assert result == []

    def test_ignores_private_ips(self) -> None:
        result = _normalize_fallback_ips(["192.168.1.1", "10.0.0.1", "172.16.0.1"])
        assert result == []

    def test_ignores_loopback(self) -> None:
        result = _normalize_fallback_ips(["127.0.0.1"])
        assert result == []

    def test_ignores_link_local(self) -> None:
        result = _normalize_fallback_ips(["169.254.1.1"])
        assert result == []

    def test_deduplicates(self) -> None:
        result = _normalize_fallback_ips(["1.1.1.1", "1.1.1.1", "8.8.8.8"])
        assert result == ["1.1.1.1", "1.1.1.1", "8.8.8.8"]
        # Deduplication happens in TelegramFallbackTransport.__init__, not in normalize


class TestParseFallbackIpEnv:
    def test_none_returns_empty(self) -> None:
        assert parse_fallback_ip_env(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert parse_fallback_ip_env("") == []

    def test_comma_separated(self) -> None:
        result = parse_fallback_ip_env("149.154.167.220, 1.1.1.1")
        assert result == ["149.154.167.220", "1.1.1.1"]

    def test_filters_invalid(self) -> None:
        result = parse_fallback_ip_env("149.154.167.220, invalid, 192.168.1.1")
        assert result == ["149.154.167.220"]


class TestIsRetryableConnectError:
    def test_connect_timeout(self) -> None:
        assert _is_retryable_connect_error(httpx.ConnectTimeout("timeout")) is True

    def test_connect_error(self) -> None:
        assert _is_retryable_connect_error(httpx.ConnectError("refused")) is True

    def test_other_error(self) -> None:
        assert _is_retryable_connect_error(ValueError("bad")) is False

    def test_read_timeout(self) -> None:
        assert _is_retryable_connect_error(httpx.ReadTimeout("read timeout")) is False


class TestRewriteRequestForIp:
    def test_preserves_host_and_sni(self) -> None:
        original = httpx.Request(
            method="GET",
            url="https://api.telegram.org/bot123/getMe",
        )
        rewritten = _rewrite_request_for_ip(original, "149.154.167.220")

        assert rewritten.url.host == "149.154.167.220"
        assert rewritten.headers["host"] == "api.telegram.org"
        assert rewritten.extensions.get("sni_hostname") == "api.telegram.org"
        assert rewritten.method == "GET"

    def test_preserves_path(self) -> None:
        original = httpx.Request(
            method="POST",
            url="https://api.telegram.org/bot123/sendMessage",
        )
        rewritten = _rewrite_request_for_ip(original, "1.1.1.1")
        assert "/bot123/sendMessage" in str(rewritten.url)


class TestTelegramFallbackTransport:
    @pytest.mark.asyncio
    async def test_non_telegram_host_goes_to_primary(self) -> None:
        transport = TelegramFallbackTransport(["1.1.1.1"])
        request = httpx.Request("GET", "https://example.com/api")

        mock_response = httpx.Response(200)
        with patch.object(
            transport._primary, "handle_async_request", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await transport.handle_async_request(request)
            assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_no_fallback_ips_goes_to_primary(self) -> None:
        transport = TelegramFallbackTransport([])
        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")

        mock_response = httpx.Response(200)
        with patch.object(
            transport._primary, "handle_async_request", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await transport.handle_async_request(request)
            assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_primary_success_no_fallback(self) -> None:
        transport = TelegramFallbackTransport(["149.154.167.220"])
        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")

        mock_response = httpx.Response(200)
        with patch.object(
            transport._primary, "handle_async_request", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await transport.handle_async_request(request)
            assert result.status_code == 200
            assert transport._sticky_ip is None

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_succeeds(self) -> None:
        transport = TelegramFallbackTransport(["149.154.167.220"])
        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")

        mock_response = httpx.Response(200)

        with (
            patch.object(
                transport._primary,
                "handle_async_request",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("refused"),
            ),
            patch.object(
                transport._fallbacks["149.154.167.220"],
                "handle_async_request",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
        ):
            result = await transport.handle_async_request(request)
            assert result.status_code == 200
            assert transport._sticky_ip == "149.154.167.220"

    @pytest.mark.asyncio
    async def test_all_exhausted_raises_last_error(self) -> None:
        transport = TelegramFallbackTransport(["149.154.167.220"])
        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")

        with (
            patch.object(
                transport._primary,
                "handle_async_request",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("primary refused"),
            ),
            patch.object(
                transport._fallbacks["149.154.167.220"],
                "handle_async_request",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectTimeout("fallback timeout"),
            ),
        ):
            with pytest.raises(httpx.ConnectTimeout, match="fallback timeout"):
                await transport.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_non_retryable_error_reraises(self) -> None:
        transport = TelegramFallbackTransport(["149.154.167.220"])
        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")

        with patch.object(
            transport._primary,
            "handle_async_request",
            new_callable=AsyncMock,
            side_effect=ValueError("permanent error"),
        ):
            with pytest.raises(ValueError, match="permanent error"):
                await transport.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_sticky_ip_used_first(self) -> None:
        transport = TelegramFallbackTransport(["1.1.1.1", "149.154.167.220"])
        transport._sticky_ip = "149.154.167.220"

        request = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")
        mock_response = httpx.Response(200)

        with patch.object(
            transport._fallbacks["149.154.167.220"],
            "handle_async_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as fallback_mock:
            result = await transport.handle_async_request(request)
            assert result.status_code == 200
            fallback_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aclose(self) -> None:
        transport = TelegramFallbackTransport(["1.1.1.1"])

        with (
            patch.object(transport._primary, "aclose", new_callable=AsyncMock),
            patch.object(transport._fallbacks["1.1.1.1"], "aclose", new_callable=AsyncMock),
        ):
            await transport.aclose()


class TestDiscoverFallbackIps:
    @pytest.mark.asyncio
    async def test_returns_seed_when_doh_fails(self) -> None:
        with patch(
            "corpclaw_lite.channels.telegram.transport._resolve_system_dns",
            return_value={"1.2.3.4"},
        ):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=Exception("network down"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await discover_fallback_ips()
                assert result == ["149.154.167.220"]

    @pytest.mark.asyncio
    async def test_excludes_system_dns_ips(self) -> None:
        with patch(
            "corpclaw_lite.channels.telegram.transport._resolve_system_dns",
            return_value={"149.154.167.220"},
        ):
            with patch(
                "corpclaw_lite.channels.telegram.transport._query_doh_provider",
                side_effect=[
                    ["149.154.167.220", "1.1.1.1"],
                    ["8.8.8.8"],
                ],
            ):
                with patch(
                    "corpclaw_lite.channels.telegram.transport.httpx.AsyncClient"
                ) as mock_client_cls:
                    mock_client = AsyncMock()
                    mock_client_cls.return_value = mock_client
                    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client.__aexit__ = AsyncMock(return_value=False)

                    result = await discover_fallback_ips()
                    assert "149.154.167.220" not in result
                    assert "1.1.1.1" in result
