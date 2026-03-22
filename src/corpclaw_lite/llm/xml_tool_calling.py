"""XML fallback parser for tool calling."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from corpclaw_lite.llm.base import ToolCall

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<name>(?P<name>[^<]+)</name>\s*<arguments>(?P<arguments>.*?)</arguments>\s*</tool_call>",
    re.DOTALL,
)
_XML_TOOL_HINT_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|name|arguments)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class XMLToolCallParseResult:
    """Structured parse result for XML fallback parsing."""

    status: str
    tool_call: ToolCall | None = None
    error_code: str | None = None
    error_message: str | None = None


def parse_xml_tool_call(
    content: str,
    *,
    allowed_tool_names: set[str] | None = None,
) -> XMLToolCallParseResult:
    """Parse a single XML tool-call envelope into an internal ToolCall."""
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
        parsed = json.loads(raw_arguments) if raw_arguments else {}
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
    if not isinstance(parsed, dict):
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
    return XMLToolCallParseResult(
        status="valid",
        tool_call=ToolCall(id="xml-tool-call", name=name, arguments=parsed),
    )


def build_xml_fallback_system(tool_names: list[str]) -> str:
    """Build a compact system instruction block for XML fallback mode."""
    available = ", ".join(name for name in tool_names if name) or "none"
    return (
        "If you need a tool, respond with exactly one XML block:\n"
        '<tool_call><name>TOOL</name><arguments>{"key":"value"}</arguments></tool_call>\n'
        f"Available tools: {available}\n"
        "If no tool is needed, answer normally."
    )


def build_xml_repair_prompt(error_message: str) -> str:
    """Build a retry instruction asking the model to repair malformed XML."""
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
