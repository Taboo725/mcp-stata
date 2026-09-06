"""Advertised tool schemas carry no redundant pydantic titles."""

import json

from mcp.server.fastmcp.tools.base import Tool

import mcp_stata.server  # noqa: F401 - side effect: installs the schema-slimming patch
from mcp_stata.fastmcp_schema_slim import strip_schema_titles
from mcp_stata.models import ToolEnvelope


async def _fixture_tool(topic: str, as_json: bool = False) -> ToolEnvelope:
    """Minimal structured tool used to exercise schema generation."""

    raise RuntimeError("not invoked in these tests")


def _titled_nodes(node, path="$"):
    """Yield paths of schema objects that still carry a ``title`` key.

    ``ArtifactRef`` declares a property *named* ``title``; that key lives in a
    ``properties`` map and is not a title of its own, so it must not be reported.
    """
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from _titled_nodes(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    if "title" in node:
        yield path
    for key in ("properties", "$defs", "definitions", "patternProperties"):
        for name, value in (node.get(key) or {}).items():
            yield from _titled_nodes(value, f"{path}.{key}.{name}")
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        yield from _titled_nodes(node.get(key) or [], f"{path}.{key}")
    for key in ("items", "additionalProperties", "not", "contains"):
        sub = node.get(key)
        if isinstance(sub, dict):
            yield from _titled_nodes(sub, f"{path}.{key}")


def test_registered_tools_advertise_no_titles():
    offenders = []
    for tool in mcp_stata.server.mcp._tool_manager.list_tools():
        offenders += [f"{tool.name} in:{p}" for p in _titled_nodes(tool.parameters)]
        offenders += [
            f"{tool.name} out:{p}" for p in _titled_nodes(tool.fn_metadata.output_schema)
        ]
    assert offenders == []


def test_a_property_named_title_is_still_advertised():
    schemas = json.dumps(
        [t.fn_metadata.output_schema for t in mcp_stata.server.mcp._tool_manager.list_tools()]
    )
    assert '"title"' in schemas  # ArtifactRef.title


def test_arguments_survive_the_strip():
    tool = Tool.from_function(_fixture_tool, structured_output=True)

    props = tool.parameters["properties"]
    assert set(props) == {"topic", "as_json"}
    assert props["topic"]["type"] == "string"
    assert props["as_json"]["default"] is False
    assert tool.parameters["required"] == ["topic"]
    assert "title" not in tool.parameters
    assert "title" not in props["topic"]


def test_strip_is_recursive_but_keeps_a_property_named_title():
    schema = {
        "title": "Args",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "nested": {
                "title": "Nested",
                "anyOf": [
                    {"title": "A", "type": "string"},
                    {"title": "B", "items": {"title": "C", "type": "integer"}},
                ],
            },
        },
        "$defs": {"Ref": {"title": "Ref", "type": "object"}},
    }
    strip_schema_titles(schema)

    # The property keyed "title" is still declared; only its own title is gone.
    assert set(schema["properties"]) == {"title", "nested"}
    assert schema["properties"]["title"] == {"type": "string"}
    assert '"title"' not in json.dumps(
        {k: v for k, v in schema.items() if k != "properties"}
    )
    assert schema["properties"]["nested"]["anyOf"][1]["items"] == {"type": "integer"}
    assert schema["$defs"]["Ref"] == {"type": "object"}


def test_strip_tolerates_non_schema_input():
    assert strip_schema_titles(None) is None
    assert strip_schema_titles("text") == "text"
    assert strip_schema_titles([]) == []
