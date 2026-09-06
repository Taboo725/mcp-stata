"""Strip pydantic's auto-generated ``title`` keys from advertised tool schemas.

Pydantic titles every model and every field, so ``model_json_schema()`` emits
``"title": "As Json"`` beside ``"as_json"`` and ``"title": "stata_get_helpArguments"``
on each argument model. The titles restate the key they sit next to, and hosts ship
the whole schema to the model on every request: across mcp-stata's tools they cost
about 4,400 characters (~1,100 tokens) of pure duplication per request.

Removing them changes nothing functional. Argument validation runs against
``FuncMetadata.arg_model`` and structured results against ``output_model``; the
schema dicts patched here are only what the server advertises.

Set ``MCP_STATA_SLIM_TOOL_SCHEMAS=0`` to keep the titles.
"""

from __future__ import annotations

import os
from typing import Any

_PATCH_ATTR = "_mcp_stata_slim_schema_patch"

SLIM_SCHEMAS_ENV = "MCP_STATA_SLIM_TOOL_SCHEMAS"

# Keys whose values are themselves schemas, keyed by how they nest.
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_VALUES = ("items", "additionalProperties", "not", "contains")


def _slim_enabled() -> bool:
    return os.environ.get(SLIM_SCHEMAS_ENV, "1").strip().lower() not in {"0", "false", "no"}


def strip_schema_titles(node: Any) -> Any:
    """Recursively drop ``title`` from every schema object in *node*, in place.

    Only schema objects lose their title. A property that happens to be *named*
    ``title`` is reached through ``properties``, so it keeps its own entry and
    merely loses the redundant title inside it.
    """
    if isinstance(node, list):
        for item in node:
            strip_schema_titles(item)
        return node
    if not isinstance(node, dict):
        return node

    node.pop("title", None)

    for key in _SCHEMA_MAPS:
        sub = node.get(key)
        if isinstance(sub, dict):
            for value in sub.values():
                strip_schema_titles(value)
    for key in _SCHEMA_LISTS:
        sub = node.get(key)
        if isinstance(sub, list):
            strip_schema_titles(sub)
    for key in _SCHEMA_VALUES:
        sub = node.get(key)
        if isinstance(sub, dict):
            strip_schema_titles(sub)

    return node


def install_slim_tool_schemas() -> None:
    """Make :meth:`Tool.from_function` advertise title-free schemas.

    Must run before the ``@mcp.tool`` decorators, which build their schemas at
    import time.
    """
    from mcp.server.fastmcp.tools.base import Tool

    if getattr(Tool, _PATCH_ATTR, False):
        return

    _original_from_function = Tool.from_function.__func__  # type: ignore[attr-defined]

    def from_function(cls: type[Tool], *args: Any, **kwargs: Any) -> Tool:
        tool = _original_from_function(cls, *args, **kwargs)
        if _slim_enabled():
            strip_schema_titles(tool.parameters)
            if tool.fn_metadata.output_schema is not None:
                strip_schema_titles(tool.fn_metadata.output_schema)
        return tool

    Tool.from_function = classmethod(from_function)  # type: ignore[method-assign]
    setattr(Tool, _PATCH_ATTR, True)
