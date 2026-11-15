"""Literal braces that are not SMCL directives must render as text.

``{}`` (a Mata loop body echoed back, a ``display "{}"``, a regex) used to
raise ``IndexError: list index out of range`` inside the renderer, which failed
the whole command and replaced its output with that message.
"""

import pytest

from mcp_stata.smcl.smcl2html import smcl_to_markdown, strip_smcl


@pytest.mark.parametrize(
    "smcl, expected",
    [
        ("{com}. while (x) {}", ". while (x) {}"),
        ("{txt}a { } b", "a { } b"),
        ('{com}. display "{}"', '. display "{}"'),
        ("{:x} y", "{:x} y"),
        ("{res}ok{txt}", "ok"),
    ],
)
def test_strip_smcl_keeps_non_directive_braces(smcl, expected):
    assert strip_smcl(smcl) == expected


@pytest.mark.parametrize("smcl", ["{}", "{ }", "{:x}", "{col 5}{}", "{bf:{}}", "{c |}{}"])
def test_renderers_never_raise_on_brace_text(smcl):
    strip_smcl(smcl)
    smcl_to_markdown(smcl)
