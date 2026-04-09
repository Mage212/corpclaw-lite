"""XML fallback parser for tool calling."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast

from corpclaw_lite.llm.base import ToolCall

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<name>(?P<name>[^<]+)</name>\s*<arguments>(?P<arguments>.*?)</arguments>\s*</tool_call>",
    re.DOTALL,
)

# Qwen3-style format used in reasoning_content:
# <tool_call><function=name><parameter=key>value</parameter>...</function></tool_call>
_QWEN3_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=(?P<name>[^>]+)>\s*(?P<params>(?:<parameter=[^>]+>.*?</parameter>\s*)*)\s*</function>\s*</tool_call>",
    re.DOTALL,
)
_QWEN3_PARAM_RE = re.compile(
    r"<parameter=(?P<key>[^>]+)>\s*(?P<value>.*?)\s*</parameter>",
    re.DOTALL,
)

_XML_TOOL_HINT_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|name|arguments|function=)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class XMLToolCallParseResult:
    """Structured parse result for XML fallback parsing."""

    status: str
    tool_call: ToolCall | None = None
    error_code: str | None = None
    error_message: str | None = None


def _parse_qwen3_tool_call(
    content: str,
    *,
    allowed_tool_names: set[str] | None = None,
) -> XMLToolCallParseResult | None:
    """Try to parse Qwen3-style <function=...><parameter=...> XML.

    Returns None if no match found (caller should try other patterns).
    """
    matches = list(_QWEN3_TOOL_CALL_RE.finditer(content))
    if not matches:
        return None
    if len(matches) > 1:
        return XMLToolCallParseResult(
            status="multiple_tool_calls",
            error_code="multiple_tool_calls",
            error_message="Only one XML tool call is allowed per response.",
        )
    match = matches[0]
    name = match.group("name").strip()
    params_block = match.group("params")

    if allowed_tool_names is not None and name not in allowed_tool_names:
        return XMLToolCallParseResult(
            status="invalid_tool_name",
            error_code="invalid_tool_name",
            error_message=f"Tool {name!r} is not in the allowed tool set.",
        )

    # Parse <parameter=key>value</parameter> pairs into a dict
    arguments: dict[str, Any] = {}
    for pm in _QWEN3_PARAM_RE.finditer(params_block):
        key = pm.group("key").strip()
        value = pm.group("value").strip()
        # Try to parse as JSON value (numbers, booleans, etc.)
        try:
            arguments[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            arguments[key] = value

    return XMLToolCallParseResult(
        status="valid",
        tool_call=ToolCall(id="xml-tool-call", name=name, arguments=arguments),
    )


def parse_xml_tool_call(
    content: str,
    *,
    allowed_tool_names: set[str] | None = None,
) -> XMLToolCallParseResult:
    """Parse a single XML tool-call envelope into an internal ToolCall."""
    # Try Qwen3-style format first (most specific)
    qwen3_result = _parse_qwen3_tool_call(content, allowed_tool_names=allowed_tool_names)
    if qwen3_result is not None:
        return qwen3_result

    # Standard format: <tool_call><name>X</name><arguments>JSON</arguments></tool_call>
    matches = list(_TOOL_CALL_RE.finditer(content))
    if not matches:
        if not _XML_TOOL_HINT_RE.search(content):
            return XMLToolCallParseResult(
                status="no_tool_call",
                error_code="no_tool_call",
                error_message="No XML tool-call markers detected.",
            )
        return XMLToolCallParseResult(
            status="malformed_xml",
            error_code="malformed_xml",
            error_message="No valid <tool_call> block found.",
        )
    if len(matches) > 1:
        return XMLToolCallParseResult(
            status="multiple_tool_calls",
            error_code="multiple_tool_calls",
            error_message="Only one XML tool call is allowed per response.",
        )

    match = matches[0]
    name = match.group("name").strip()
    raw_arguments = match.group("arguments").strip()
    if allowed_tool_names is not None and name not in allowed_tool_names:
        return XMLToolCallParseResult(
            status="invalid_tool_name",
            error_code="invalid_tool_name",
            error_message=f"Tool {name!r} is not in the allowed tool set.",
        )
    try:
        raw_parsed: Any = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return XMLToolCallParseResult(
            status="invalid_arguments",
            error_code="invalid_json",
            error_message="Tool arguments must be valid JSON.",
            tool_call=ToolCall(
                id="xml-tool-call",
                name=name,
                arguments={
                    "__tool_argument_error__": "invalid_json",
                    "__raw_arguments__": raw_arguments[:1000],
                },
            ),
        )
    if not isinstance(raw_parsed, dict):
        return XMLToolCallParseResult(
            status="invalid_arguments",
            error_code="expected_object",
            error_message="Tool arguments must decode to a JSON object.",
            tool_call=ToolCall(
                id="xml-tool-call",
                name=name,
                arguments={
                    "__tool_argument_error__": "expected_object",
                    "__raw_arguments__": raw_arguments[:1000],
                },
            ),
        )
    parsed = cast(dict[str, Any], raw_parsed)
    return XMLToolCallParseResult(
        status="valid",
        tool_call=ToolCall(id="xml-tool-call", name=name, arguments=parsed),
    )


def build_xml_fallback_system(tool_names: list[str]) -> str:
    """Build a compact system instruction block for XML fallback mode.

    Part of the public API (exported via __all__) — used by callers that need
    to inject XML tool-calling instructions into the system prompt when the
    model does not natively support function calling.
    """
    available = ", ".join(name for name in tool_names if name) or "none"
    return (
        "If you need a tool, respond with exactly one XML block:\n"
        '<tool_call><name>TOOL</name><arguments>{"key":"value"}</arguments></tool_call>\n'
        f"Available tools: {available}\n"
        "If no tool is needed, answer normally."
    )


def build_xml_repair_prompt(error_message: str) -> str:
    """Build a retry instruction asking the model to repair malformed XML.

    Part of the public API (exported via __all__) — used alongside
    build_xml_fallback_system() for retry flows.
    """
    return (
        "Your previous tool-call output was invalid. "
        f"Error: {error_message} "
        "Return exactly one valid <tool_call> XML block or answer normally if no tool is needed."
    )


__all__ = [
    "XMLToolCallParseResult",
    "build_xml_fallback_system",
    "build_xml_repair_prompt",
    "parse_xml_tool_call",
]
