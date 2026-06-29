# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import openai

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    TokenUsage,
    ToolCall,
    get_backend_request_options,
    get_request_options,
)
from corpclaw_lite.llm.presets import ModelPreset, ModelProfile, SamplingProfile
from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_calls

__all__ = [
    "OpenAIProvider",
]

logger = logging.getLogger(__name__)


def _text_delta(value: Any) -> str:
    """Return provider delta text only when it is actually a string."""
    return value if isinstance(value, str) else ""


def _raw_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _raw_int(value: Any, key: str, default: int = 0) -> int:
    raw = _raw_get(value, key, default)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int | float | str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def _raw_float(value: Any, key: str, default: float = 0.0) -> float:
    raw = _raw_get(value, key, default)
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, int | float | str):
        try:
            return float(raw)
        except ValueError:
            return default
    return default


def _usage_from_raw(raw_usage: Any) -> TokenUsage:
    if not raw_usage:
        return TokenUsage()
    details = _raw_get(raw_usage, "prompt_tokens_details", None) or {}
    cached_tokens = _raw_int(details, "cached_tokens")
    input_tokens = _raw_int(raw_usage, "prompt_tokens")
    output_tokens = _raw_int(raw_usage, "completion_tokens")
    total_tokens = _raw_int(raw_usage, "total_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens,
        cached_input_tokens=cached_tokens,
    )


def _apply_timings(usage: TokenUsage, raw_timings: Any) -> TokenUsage:
    if not raw_timings:
        return usage
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        prompt_processing_tokens=_raw_int(raw_timings, "prompt_n"),
        prompt_processing_ms=_raw_float(raw_timings, "prompt_ms"),
        predicted_tokens=_raw_int(raw_timings, "predicted_n"),
        predicted_ms=_raw_float(raw_timings, "predicted_ms"),
    )


def _enable_stream_usage(kwargs: dict[str, Any]) -> None:
    stream_options: dict[str, Any] = dict(kwargs.get("stream_options") or {})
    stream_options["include_usage"] = True
    kwargs["stream_options"] = stream_options


class OpenAIProvider(Provider):
    """LLM Provider passing through to OpenAI-compatible models."""

    def __init__(
        self,
        settings: ProviderSettings,
        preset: ModelPreset | None = None,
        *,
        model_profile: ModelProfile | None = None,
        sampling: SamplingProfile | None = None,
    ):
        self._model = settings.model
        # Always store the split profiles as the canonical internal shape (D-056).
        # Legacy ``preset=`` is bridged to a (ModelProfile, SamplingProfile) pair
        # so every code path below deals with one representation. If both are
        # supplied, the explicit profiles win.
        if model_profile is None and sampling is None and preset is not None:
            from corpclaw_lite.llm.presets import profile_from_legacy_preset

            model_profile, sampling = profile_from_legacy_preset(preset)
        self._preset = preset  # kept for back-compat introspection (deprecated)
        self._model_profile = model_profile
        self._sampling = sampling
        api_key = settings.api_key or "dummy"  # local models may not need a real key
        if settings.base_url:
            self._client = openai.AsyncOpenAI(api_key=api_key, base_url=settings.base_url)
        else:
            self._client = openai.AsyncOpenAI(api_key=api_key)

    # OpenAI SDK accepts these top-level params in chat.completions.create().
    # Everything else (top_k, min_p, repeat_penalty, etc.) must go into
    # extra_body — the SDK merges it into the JSON body, so LM Studio / vLLM
    # receive them without the SDK rejecting them client-side.
    _OPENAI_STANDARD_PARAMS = frozenset(
        {
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "stop",
            "n",
            "seed",
            "stream",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "user",
            "response_format",
            "tool_choice",
            "parallel_tool_calls",
        }
    )

    def _thinking_disabled(self) -> bool:
        """Return True if thinking is turned off by sampling or per-call override.

        Some models (e.g. gemma4) enable thinking via a ``system_prompt_prefix``
        token (``<|think|>``) rather than a ``chat_template_kwargs`` flag. For
        those, "thinking off" must suppress the prefix — setting
        ``enable_thinking=False`` alone has no effect. This helper checks both
        the SamplingProfile and the per-call RequestOptions so the prefix is
        suppressed whenever thinking is off by any layer.
        """
        if self._sampling is not None and self._sampling.thinking_mode == "off":
            return True
        opts = get_request_options()
        return opts is not None and opts.thinking is not None and opts.thinking.mode == "off"

    def _apply_model_profile(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """Apply the ModelProfile: default inference params + system_prompt_prefix.

        Lowest inference priority — SamplingProfile and RequestOptions override
        these. ``setdefault`` is used so higher layers always win. Non-standard
        params (top_k, min_p, repeat_penalty, ...) are routed to ``extra_body``
        so the OpenAI SDK doesn't reject them.

        The ``system_prompt_prefix`` (e.g. gemma4 ``<|think|>``) is suppressed
        when thinking is disabled (SamplingProfile or RequestOptions) — for
        prefix-based models the prefix IS the thinking switch.
        """
        if not self._model_profile:
            return system

        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})
        for k, v in self._model_profile.default_inference.items():
            if k in self._OPENAI_STANDARD_PARAMS:
                kwargs.setdefault(k, v)
            else:
                extra_body.setdefault(k, v)
        if extra_body:
            kwargs["extra_body"] = extra_body

        # Suppress the thinking prefix when thinking is off. For gemma4-style
        # models the prefix (<|think|>) is what enables reasoning; without this
        # check, thinking_mode=off would set enable_thinking=False (a Qwen
        # mechanism) but leave the prefix active, so gemma4 keeps reasoning.
        if self._model_profile.system_prompt_prefix and not self._thinking_disabled():
            prefix = self._model_profile.system_prompt_prefix
            return f"{prefix}\n{system}" if system else prefix
        return system

    def _apply_sampling(self, kwargs: dict[str, Any]) -> None:
        """Apply the SamplingProfile: thinking mode/budget + inference overrides.

        Middle inference priority — overrides ModelProfile defaults, is itself
        overridden by RequestOptions (per-call). Thinking mode maps to backend
        controls:
          - ``off``    → ``extra_body.chat_template_kwargs.enable_thinking=false``
            (Qwen-style disable) AND suppresses the ModelProfile's
            ``system_prompt_prefix`` (gemma4 ``<|think|>``) — see
            ``_apply_model_profile`` / ``_thinking_disabled``. Both mechanisms
            are applied so thinking-off works across model families.
          - ``budget`` → cap ``max_tokens`` to ``budget + 1024`` (soft cap on
            reasoning output, model-dependent effectiveness).
          - ``default``→ the model's natural thinking.
        """
        if not self._sampling:
            return

        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})

        # Inference overrides — middle priority, wins over ModelProfile defaults.
        # Direct assignment (not setdefault) so the task-level layer overrides
        # the model-level defaults; per-call RequestOptions override these in turn.
        for k, v in self._sampling.inference_overrides.items():
            if k in self._OPENAI_STANDARD_PARAMS:
                kwargs[k] = v
            else:
                extra_body[k] = v

        # Thinking mode.
        if self._sampling.thinking_mode == "off":
            ctk = dict(extra_body.get("chat_template_kwargs") or {})
            ctk.setdefault("enable_thinking", False)
            extra_body["chat_template_kwargs"] = ctk
        elif self._sampling.thinking_mode == "budget" and self._sampling.thinking_budget:
            kwargs.setdefault("max_tokens", self._sampling.thinking_budget + 1024)

        if extra_body:
            kwargs["extra_body"] = extra_body

    def _apply_request_options(self, kwargs: dict[str, Any]) -> None:
        """Apply per-call RequestOptions (inference + thinking overrides).

        Highest inference priority — overrides ModelProfile and SamplingProfile.
        Reads the per-call contextvar set by PhasePolicy (or callers). The
        transport-level ``extra_body`` from ``BackendRequestOptions`` is merged
        separately by ``_apply_backend_options``.
        """
        options = get_request_options()
        if options is None:
            return

        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})

        if options.inference:
            for k, v in options.inference.items():
                if k in self._OPENAI_STANDARD_PARAMS:
                    # Per-call override wins over profile/sampling → set directly.
                    kwargs[k] = v
                else:
                    extra_body[k] = v

        if options.thinking:
            mode = options.thinking.mode
            if mode == "off":
                ctk = dict(extra_body.get("chat_template_kwargs") or {})
                ctk["enable_thinking"] = False  # per-call override wins
                extra_body["chat_template_kwargs"] = ctk
            elif mode == "budget" and options.thinking.budget is not None:
                kwargs["max_tokens"] = options.thinking.budget + 1024
            elif mode == "default":
                # Force the model's natural thinking ON — cancel any
                # thinking_mode=off set by the sampling profile (e.g. aggregation
                # phase must reason to synthesise, even on an off-configured run).
                ctk = dict(extra_body.get("chat_template_kwargs") or {})
                ctk["enable_thinking"] = True
                extra_body["chat_template_kwargs"] = ctk

        if extra_body:
            kwargs["extra_body"] = extra_body

    def _apply_preset(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """DEPRECATED: combined preset application (back-compat).

        New code should call ``_apply_model_profile`` + ``_apply_sampling``
        + ``_apply_request_options``. Kept so any external caller of the old
        private API keeps working.
        """
        system = self._apply_model_profile(system, kwargs)
        self._apply_sampling(kwargs)
        return system

    # ── Raw request/response capture (D-056 post-0.2.0) ──────────────────────

    def _capture_llm_io(
        self,
        phase: str,
        kwargs: dict[str, Any],
        raw_response: Any | None = None,
        *,
        finish_reason: str | None = None,
        error: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        """Capture the raw request + response to logs/llm_payloads.jsonl.

        No-op when payload capture is disabled (the common case). Extracts the
        request/response summaries and hands them to the PayloadCaptureLogger
        singleton, which applies the field allowlist + credential scrubbing.
        """
        from corpclaw_lite.logging.payload import get_payload_logger

        pl = get_payload_logger()
        if pl is None or not pl.enabled:
            return

        from corpclaw_lite.llm.base import (
            get_capture_session_id,
            get_capture_user_id,
            get_run_id,
        )

        response_summary = (
            self._response_summary(raw_response, finish_reason) if raw_response else None
        )
        pl.capture(
            run_id=get_run_id(),
            user_id=get_capture_user_id(),
            session_id=get_capture_session_id(),
            phase=phase,
            request=self._request_summary(kwargs),
            response=response_summary,
            finish_reason=finish_reason,
            error=error,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _request_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build a request summary dict from the provider kwargs.

        Keys match the ``request.*`` allowlist paths: ``model``, ``messages``,
        ``tools``, ``params`` (standard OpenAI params), ``extra_body``.
        """
        # Standard OpenAI params that are NOT messages/tools/extra_body/model.
        standard_param_keys = {
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "stop",
            "seed",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "user",
            "n",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "stream",
        }
        params = {k: v for k, v in kwargs.items() if k in standard_param_keys}
        return {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "tools": kwargs.get("tools"),
            "params": params or None,
            "extra_body": kwargs.get("extra_body"),
        }

    @staticmethod
    def _response_summary(raw_response: Any, finish_reason: str | None) -> dict[str, Any]:
        """Build a response summary dict from the raw SDK/streamed response.

        Keys match the ``response.*`` allowlist paths: ``content``,
        ``reasoning``, ``tool_calls``, ``usage``, ``finish_reason``. Handles
        both SDK ChatCompletion objects (have ``.model_dump()``) and the
        streamed SimpleNamespace assembled in ``chat_streamed``.
        """
        # SDK objects expose model_dump(); SimpleNamespace does not.
        if hasattr(raw_response, "model_dump"):
            try:
                data = raw_response.model_dump()
            except Exception:
                data = {}
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            msg = choice.get("message") or {}
            return {
                "content": msg.get("content") or "",
                "reasoning": msg.get("reasoning_content") or "",
                "tool_calls": msg.get("tool_calls") or [],
                "usage": data.get("usage"),
                "finish_reason": choice.get("finish_reason") or finish_reason,
            }
        # Fallback: SimpleNamespace from chat_streamed — has choices/message-like
        # attributes. Try attribute access.
        try:
            choices = getattr(raw_response, "choices", None) or []
            choice = choices[0] if choices else None
            msg = getattr(choice, "message", None) if choice else None
            if msg is not None:
                return {
                    "content": getattr(msg, "content", "") or "",
                    "reasoning": getattr(msg, "reasoning_content", "") or "",
                    "tool_calls": getattr(msg, "tool_calls", []) or [],
                    "usage": getattr(raw_response, "usage", None),
                    "finish_reason": getattr(choice, "finish_reason", None) or finish_reason,
                }
        except Exception:
            pass
        return {
            "content": None,
            "reasoning": None,
            "tool_calls": None,
            "usage": None,
            "finish_reason": finish_reason,
        }

    def _parse_reasoning(self, message: Any) -> tuple[str, str]:
        """Extract (reasoning, content) based on the model's thinking parser.

        Reads ``ModelProfile.thinking_parser`` (new API) with a back-compat
        fallback to the legacy ``ModelPreset.thinking``. Returns
        (reasoning_text, clean_content) — reasoning is empty string if no
        thinking config is set or no reasoning was found.
        """
        cfg = None
        if self._model_profile is not None:
            cfg = self._model_profile.thinking_parser
        elif self._preset is not None:
            cfg = self._preset.thinking

        if cfg is None:
            # No thinking config — return content as-is
            return "", message.content or ""

        if cfg.source == "native":
            # Qwen3-style: API returns reasoning in a dedicated field
            reasoning = getattr(message, "reasoning_content", None) or ""
            return reasoning, message.content or ""

        # source == "content" — parse tags from content (Gemma4-style)
        raw = message.content or ""
        if cfg.open_tag in raw and cfg.close_tag in raw:
            s = raw.index(cfg.open_tag) + len(cfg.open_tag)
            e = raw.index(cfg.close_tag, s)
            reasoning = raw[s:e].strip()
            content = raw[e + len(cfg.close_tag) :].strip()
            return reasoning, content

        return "", raw

    # Minimum reasoning length for the reasoning→content fallback (sub-case b).
    # Qwen3's "everything in reasoning_content" edge case yields real answers
    # (hundreds+ of chars). A short reasoning fragment (e.g. gemma4's 12-char
    # degenerate output at thinking-OFF + low temp) is NOT a real answer —
    # copying it to content would create a truncated XML marker and trigger a
    # false "malformed_xml_tool_call" crash. Below this threshold we leave
    # content empty so the agent retries instead of crashing on garbage.
    _REASONING_FALLBACK_MIN_CHARS = 100

    def _resolve_reasoning_fallback(
        self,
        content: str,
        finish_reason: str | None,
        raw_message: Any,
        tools: list[dict[str, Any]] | None,
        tool_calls: list[ToolCall],
    ) -> tuple[str, list[ToolCall]]:
        """Handle Qwen3/LM Studio edge case: response stuck in reasoning_content.

        LM Studio + Qwen3: the model sometimes puts everything into
        reasoning_content and leaves content + tool_calls empty.
        Two sub-cases:
          a) reasoning_content has XML tool calls → parse them (NOT a final answer)
          b) reasoning_content is plain text → use as the final answer

        Sub-case (b) only fires for substantial reasoning (≥
        ``_REASONING_FALLBACK_MIN_CHARS``): a short reasoning fragment (e.g.
        gemma4's degenerate 12-char output) is not a real answer and copying it
        to content would cause a false malformed-XML crash. Below the threshold,
        content stays empty so the agent can retry instead of crashing.

        Returns updated (content, tool_calls). Unchanged if no fallback needed.
        """
        # Only trigger when content is empty, no native tool calls, and stop
        if content or getattr(raw_message, "tool_calls", None) or finish_reason != "stop":
            return content, tool_calls

        reasoning_text: str = getattr(raw_message, "reasoning_content", None) or ""
        if not reasoning_text:
            return content, tool_calls

        # Sub-case (a): reasoning contains XML tool-call markers — parse it.
        # This can be a real tool-call embedded in reasoning, so it is checked
        # regardless of length.
        if tools:
            _MARKERS = ("<tool_call>", "<function=")
            if any(m in reasoning_text for m in _MARKERS):
                allowed_names = {t["function"]["name"] for t in tools if "function" in t}
                parse_result = parse_xml_tool_calls(
                    reasoning_text, allowed_tool_names=allowed_names
                )
                if parse_result.tool_calls:
                    logger.info(
                        "XML fallback (reasoning_content): parsed %d tool_call(s)",
                        len(parse_result.tool_calls),
                    )
                    return "", [*tool_calls, *parse_result.tool_calls]
                logger.debug("XML fallback (reasoning_content): no tool_call parsed from reasoning")
                # Markers present but unparseable — do NOT copy a short fragment
                # to content (truncated marker → false malformed_xml crash).
                if len(reasoning_text.strip()) < self._REASONING_FALLBACK_MIN_CHARS:
                    return content, tool_calls
                return reasoning_text.strip(), tool_calls

        # Sub-case (b): plain-text reasoning as the answer. Only for substantial
        # reasoning — a short fragment is degenerate output, not an answer.
        if len(reasoning_text.strip()) < self._REASONING_FALLBACK_MIN_CHARS:
            return content, tool_calls
        return reasoning_text.strip(), tool_calls

    # ── Main chat ─────────────────────────────────────────────────────────────

    def _build_chat_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Build OpenAI-compatible chat kwargs shared by full and streamed calls.

        Merge priority (each layer wins over the previous via setdefault /
        direct assignment for per-call overrides):
            ModelProfile.default_inference  (lowest)
              < SamplingProfile.inference_overrides + thinking_mode
                < RequestOptions.inference / RequestOptions.thinking (per-call)
                  < BackendRequestOptions.extra_body (transport, lowest of its
                    own layer — merged separately by ``_apply_backend_options``)
        """
        final_messages: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"model": self._model}

        # 1. Model profile (default inference + system_prompt_prefix).
        system = self._apply_model_profile(system, kwargs)
        # 2. Sampling profile (thinking mode + inference overrides).
        self._apply_sampling(kwargs)
        # 3. Per-call request options (PhasePolicy / caller overrides).
        self._apply_request_options(kwargs)
        # 4. Transport-level backend options (queue/cache extra_body).
        self._apply_backend_options(kwargs)

        if system:
            final_messages.append({"role": "system", "content": system})
        final_messages.extend(messages)

        # Defensive: ensure no None content in any message (breaks Jinja templates)
        for msg in final_messages:
            if msg.get("content") is None:
                msg["content"] = ""

        kwargs["messages"] = final_messages

        if tools:
            # OpenAI format usually accepts tools directly
            kwargs["tools"] = tools

        return kwargs, final_messages

    def _apply_backend_options(self, kwargs: dict[str, Any]) -> None:
        """Merge request-local backend-specific options into OpenAI kwargs."""
        options = get_backend_request_options()
        if options is None or not options.extra_body:
            return
        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})
        extra_body.update(options.extra_body)
        kwargs["extra_body"] = extra_body

    def _tool_calls_from_native(self, native_tool_calls: Any) -> list[ToolCall]:
        """Normalize OpenAI SDK tool calls into our ToolCall model."""
        tool_calls: list[ToolCall] = []
        if not native_tool_calls:
            return tool_calls
        for tc in native_tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {"__raw__": tc.function.arguments}

            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )
        return tool_calls

    def _finalize_response(
        self,
        *,
        content: str,
        reasoning: str,
        raw_message: Any,
        finish_reason: str | None,
        tools: list[dict[str, Any]] | None,
        usage: TokenUsage,
    ) -> LLMResponse:
        """Apply the same post-processing to full and streamed responses."""
        tool_calls = self._tool_calls_from_native(getattr(raw_message, "tool_calls", None))

        content, tool_calls = self._resolve_reasoning_fallback(
            content, finish_reason, raw_message, tools, tool_calls
        )

        if not tool_calls and tools and content:
            allowed_names = {t["function"]["name"] for t in tools if "function" in t}
            parse_result = parse_xml_tool_calls(content, allowed_tool_names=allowed_names)
            if parse_result.tool_calls:
                logger.info(
                    "XML fallback (content): parsed %d tool_call(s)",
                    len(parse_result.tool_calls),
                )
                tool_calls.extend(parse_result.tool_calls)
                content = ""
            elif parse_result.error_code:
                # XML markers present but parsing failed — capture the raw
                # unparsed content for diagnosis (the "could not safely parse"
                # path). Always captured regardless of allowlist so the model's
                # raw output is visible when tool-call parsing breaks.
                from corpclaw_lite.llm.base import (
                    get_capture_session_id,
                    get_capture_user_id,
                    get_run_id,
                )
                from corpclaw_lite.logging.payload import get_payload_logger

                pl = get_payload_logger()
                if pl is not None and pl.enabled:
                    pl.capture(
                        run_id=get_run_id(),
                        user_id=get_capture_user_id(),
                        session_id=get_capture_session_id(),
                        phase="xml_parse_failure",
                        request=None,
                        response=None,
                        finish_reason=finish_reason,
                        error=parse_result.error_code,
                        diagnostic={
                            "raw_unparsed_content": content,
                            "raw_reasoning": reasoning,
                            "parse_error_message": parse_result.error_message,
                        },
                    )

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=usage,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Execute a full chat message and return response with potential tool calls."""
        kwargs, final_messages = self._build_chat_kwargs(messages, tools, system)

        logger.debug(
            "[%s] Sending %d messages, roles=%s, tools=%d",
            self._model,
            len(final_messages),
            [m["role"] for m in final_messages],
            len(tools) if tools else 0,
        )

        response = await self._client.chat.completions.create(**kwargs)
        if not response.choices:
            self._capture_llm_io("chat", kwargs, response, finish_reason=None)
            return LLMResponse(content="", tool_calls=[], reasoning="")
        choice = response.choices[0]

        # ── Extract reasoning + content ───────────────────────────────────────
        reasoning, content = self._parse_reasoning(choice.message)

        usage = _usage_from_raw(response.usage)
        usage = _apply_timings(usage, getattr(response, "timings", None))

        self._capture_llm_io("chat", kwargs, response, finish_reason=choice.finish_reason)

        return self._finalize_response(
            content=content,
            reasoning=reasoning,
            raw_message=choice.message,
            finish_reason=choice.finish_reason,
            tools=tools,
            usage=usage,
        )

    async def chat_streamed(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_event: Callable[[LLMStreamEvent], None] | None = None,
    ) -> LLMResponse:
        """Stream for backend telemetry, then return a complete LLMResponse."""
        kwargs, final_messages = self._build_chat_kwargs(messages, tools, system)
        kwargs["stream"] = True
        _enable_stream_usage(kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str | None]] = {}
        finish_reason: str | None = None
        usage = TokenUsage()

        def emit(event: LLMStreamEvent) -> None:
            if on_event is not None:
                on_event(event)

        emit(LLMStreamEvent(stage="started"))

        logger.debug(
            "[%s] Streaming %d messages, roles=%s, tools=%d",
            self._model,
            len(final_messages),
            [m["role"] for m in final_messages],
            len(tools) if tools else 0,
        )

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:  # type: ignore
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = _usage_from_raw(chunk_usage)
            chunk_timings = getattr(chunk, "timings", None)
            if chunk_timings:
                usage = _apply_timings(usage, chunk_timings)
            if not getattr(chunk, "choices", None):
                continue

            choice = chunk.choices[0]
            choice_finish_reason = _text_delta(getattr(choice, "finish_reason", None))
            finish_reason = choice_finish_reason or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            reasoning_delta = _text_delta(getattr(delta, "reasoning_content", None))
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                emit(
                    LLMStreamEvent(
                        stage="reasoning",
                        reasoning_delta=reasoning_delta,
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

            content_delta = _text_delta(getattr(delta, "content", None))
            if content_delta:
                content_parts.append(content_delta)
                emit(
                    LLMStreamEvent(
                        stage="answer",
                        content_delta=content_delta,
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

            for tc in getattr(delta, "tool_calls", None) or []:
                idx = int(getattr(tc, "index", 0) or 0)
                part = tool_parts.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if getattr(tc, "id", None):
                    part["id"] = tc.id
                fn = getattr(tc, "function", None)
                name_delta = _text_delta(getattr(fn, "name", None)) if fn is not None else ""
                args_delta = _text_delta(getattr(fn, "arguments", None)) if fn is not None else ""
                if name_delta:
                    part["name"] = name_delta
                if args_delta:
                    part["arguments"] = f"{part['arguments'] or ''}{args_delta}"
                emit(
                    LLMStreamEvent(
                        stage="tool_call",
                        tool_call_id=part["id"],
                        tool_call_name=part["name"],
                        tool_call_arguments_delta=args_delta or "",
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

        native_tool_calls: list[Any] = []
        for idx in sorted(tool_parts):
            part = tool_parts[idx]
            name = part["name"]
            if not name:
                continue
            native_tool_calls.append(
                SimpleNamespace(
                    id=part["id"] or f"call_{idx}",
                    function=SimpleNamespace(
                        name=name,
                        arguments=part["arguments"] or "{}",
                    ),
                )
            )

        content = "".join(content_parts)
        raw_message = SimpleNamespace(
            content=content,
            reasoning_content="".join(reasoning_parts),
            tool_calls=native_tool_calls,
        )
        reasoning, content = self._parse_reasoning(raw_message)
        if not reasoning and raw_message.reasoning_content:
            reasoning = raw_message.reasoning_content
        response = self._finalize_response(
            content=content,
            reasoning=reasoning,
            raw_message=raw_message,
            finish_reason=finish_reason,
            tools=tools,
            usage=usage,
        )
        # Capture raw I/O for streamed calls. raw_message is a SimpleNamespace
        # (content/reasoning_content/tool_calls); usage + finish_reason are locals.
        self._capture_llm_io("chat_streamed", kwargs, raw_message, finish_reason=finish_reason)
        emit(
            LLMStreamEvent(
                stage="finished",
                finish_reason=finish_reason,
                reasoning_chars=len(response.reasoning or ""),
                content_chars=len(response.content or ""),
                tool_call_count=len(response.tool_calls or []),
            )
        )
        return response

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a chat request with an inline base64 image (OpenAI format)."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_media_type};base64,{image_data}",
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        kwargs: dict[str, Any] = {"model": self._model}

        # Apply the same profile/sampling/per-call layers as chat(), but NOT
        # the ModelProfile's system_prompt_prefix — vision prompts must not
        # receive the thinking prefix (e.g. gemma4 <|think|>). We reuse the
        # split apply methods and inline only the model-profile inference
        # defaults, skipping the prefix.
        if self._model_profile:
            extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})
            for k, v in self._model_profile.default_inference.items():
                if k in self._OPENAI_STANDARD_PARAMS:
                    kwargs.setdefault(k, v)
                else:
                    extra_body.setdefault(k, v)
            if extra_body:
                kwargs["extra_body"] = extra_body
        # Sampling profile (thinking mode + inference overrides) — same as chat.
        self._apply_sampling(kwargs)
        # Per-call request options (PhasePolicy / caller overrides) — same as chat.
        self._apply_request_options(kwargs)
        # Transport-level backend options (queue/cache extra_body).
        self._apply_backend_options(kwargs)

        if system:
            messages.insert(0, {"role": "system", "content": system})

        kwargs["messages"] = messages

        response = await self._client.chat.completions.create(**kwargs)
        if not response.choices:
            self._capture_llm_io("chat_with_image", kwargs, response, finish_reason=None)
            return LLMResponse(content="")
        choice = response.choices[0]
        content = choice.message.content or ""

        usage = _usage_from_raw(response.usage)
        usage = _apply_timings(usage, getattr(response, "timings", None))
        self._capture_llm_io(
            "chat_with_image", kwargs, response, finish_reason=choice.finish_reason
        )
        return LLMResponse(content=content, usage=usage)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat response without parsing tool calls.

        This is a text-streaming convenience method. Agent orchestration should
        use ``chat_streamed()`` so tool calls and reasoning can be reconstructed.
        """
        kwargs, _final_messages = self._build_chat_kwargs(messages, tools, system)
        kwargs["stream"] = True
        _enable_stream_usage(kwargs)

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:  # type: ignore
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            yield StreamChunk(
                content=_text_delta(getattr(delta, "content", None)),
                reasoning=_text_delta(getattr(delta, "reasoning_content", None)),
                finish_reason=_text_delta(getattr(choice, "finish_reason", None)) or None,
            )
