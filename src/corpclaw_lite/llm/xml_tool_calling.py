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
_TOOL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)

# Qwen3-style format used in reasoning_content:
# <tool_call><function=name><parameter=key>value</parameter>...</function></tool_call>
_QWEN3_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=(?P<name>[^>]+)>\s*(?P<params>(?:<parameter=[^>]+>.*?</parameter>\s*)*)\s*</function>\s*</tool_call>",
    re.DOTALL,
)
_QWEN3_TOOL_BODY_RE = re.compile(
    r"^\s*<function=(?P<name>[^>]+)>\s*(?P<params>.*?)\s*</function>\s*$",
    re.DOTALL,
)
_QWEN3_PARAM_RE = re.compile(
    r"<parameter=(?P<key>[^>]+)>\s*(?P<value>.*?)\s*</parameter>",
    re.DOTALL,
)
_STANDARD_TOOL_BODY_RE = re.compile(
    r"^\s*<name>(?P<name>[^<]+)</name>\s*<arguments>(?P<arguments>.*?)</arguments>\s*$",
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
    tool_calls: tuple[ToolCall, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    @property
    def tool_call(self) -> ToolCall | None:
        """Return the single parsed call for backward-compatible callers."""
        return self.tool_calls[0] if len(self.tool_calls) == 1 else None


def contains_xml_tool_call_markers(content: str) -> bool:
    """Return True when text contains XML tool-call markers."""
    return bool(_XML_TOOL_HINT_RE.search(content))


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
        tool_calls=(ToolCall(id="xml-tool-call", name=name, arguments=arguments),),
    )


def _invalid_result(error_code: str, error_message: str) -> XMLToolCallParseResult:
    return XMLToolCallParseResult(
        status="invalid_arguments",
        error_code=error_code,
        error_message=error_message,
    )


def _parse_qwen3_body(
    body: str,
    index: int,
    *,
    allowed_tool_names: set[str] | None,
) -> XMLToolCallParseResult:
    match = _QWEN3_TOOL_BODY_RE.match(body)
    if not match:
        return XMLToolCallParseResult(
            status="malformed_xml",
            error_code="malformed_xml",
            error_message="No valid Qwen3 <function=...> block found.",
        )
    name = match.group("name").strip()
    if allowed_tool_names is not None and name not in allowed_tool_names:
        return XMLToolCallParseResult(
            status="invalid_tool_name",
            error_code="invalid_tool_name",
            error_message=f"Tool {name!r} is not in the allowed tool set.",
        )

    arguments: dict[str, Any] = {}
    for pm in _QWEN3_PARAM_RE.finditer(match.group("params")):
        key = pm.group("key").strip()
        value = pm.group("value").strip()
        try:
            arguments[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            arguments[key] = value

    return XMLToolCallParseResult(
        status="valid",
        tool_calls=(ToolCall(id=f"xml-tool-call-{index}", name=name, arguments=arguments),),
    )


def _parse_standard_body(
    body: str,
    index: int,
    *,
    allowed_tool_names: set[str] | None,
) -> XMLToolCallParseResult:
    match = _STANDARD_TOOL_BODY_RE.match(body)
    if not match:
        return XMLToolCallParseResult(
            status="malformed_xml",
            error_code="malformed_xml",
            error_message="No valid <name>/<arguments> XML tool-call block found.",
        )

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
        return _invalid_result("invalid_json", "Tool arguments must be valid JSON.")
    if not isinstance(raw_parsed, dict):
        return _invalid_result("expected_object", "Tool arguments must decode to a JSON object.")

    parsed = cast(dict[str, Any], raw_parsed)
    return XMLToolCallParseResult(
        status="valid",
        tool_calls=(ToolCall(id=f"xml-tool-call-{index}", name=name, arguments=parsed),),
    )


def parse_xml_tool_calls(
    content: str,
    *,
    allowed_tool_names: set[str] | None = None,
) -> XMLToolCallParseResult:
    """Parse one or more XML tool-call envelopes into internal ToolCalls.

    Validation is all-or-nothing: a malformed or disallowed call invalidates the
    whole batch so callers never execute a partial XML action set.
    """
    matches = list(_TOOL_BLOCK_RE.finditer(content))
    if not matches:
        if not contains_xml_tool_call_markers(content):
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

    tool_calls: list[ToolCall] = []
    for index, match in enumerate(matches, start=1):
        body = match.group("body")
        if "<function=" in body:
            result = _parse_qwen3_body(body, index, allowed_tool_names=allowed_tool_names)
        else:
            result = _parse_standard_body(body, index, allowed_tool_names=allowed_tool_names)
        if result.status != "valid":
            return result
        tool_calls.extend(result.tool_calls)

    return XMLToolCallParseResult(status="valid", tool_calls=tuple(tool_calls))


def parse_xml_tool_call(
    content: str,
    *,
    allowed_tool_names: set[str] | None = None,
) -> XMLToolCallParseResult:
    """Parse a single XML tool-call envelope into an internal ToolCall."""
    # Keep the old single-call API behavior while the provider uses the new
    # multi-call parser.
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
        )
    if not isinstance(raw_parsed, dict):
        return XMLToolCallParseResult(
            status="invalid_arguments",
            error_code="expected_object",
            error_message="Tool arguments must decode to a JSON object.",
        )
    parsed = cast(dict[str, Any], raw_parsed)
    return XMLToolCallParseResult(
        status="valid",
        tool_calls=(ToolCall(id="xml-tool-call", name=name, arguments=parsed),),
    )


def build_xml_fallback_system(tool_names: list[str]) -> str:
    """Build a compact system instruction block for XML fallback mode.

    Part of the public API (exported via __all__) — used by callers that need
    to inject XML tool-calling instructions into the system prompt when the
    model does not natively support function calling.
    """
    available = ", ".join(name for name in tool_names if name) or "none"
    return (
        "If you need a tool, respond with one or more XML blocks:\n"
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
        "Return valid tool calls through the configured tool-calling format, or answer normally "
        "if no tool is needed. Do not expose raw <tool_call> XML to the user."
    )


__all__ = [
    "XMLToolCallParseResult",
    "build_xml_fallback_system",
    "build_xml_repair_prompt",
    "contains_xml_tool_call_markers",
    "parse_xml_tool_call",
    "parse_xml_tool_calls",
]
