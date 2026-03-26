def test_xml_fallback_parsing() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    content = """
Here is my answer.
<tool_call>
<name>read_file</name>
<arguments>{"path": "/tmp/test"}</arguments>
</tool_call>
"""
    result = parse_xml_tool_call(content)
    assert result.status == "valid"
    assert result.tool_call is not None
    assert result.tool_call.name == "read_file"
    assert result.tool_call.arguments["path"] == "/tmp/test"
