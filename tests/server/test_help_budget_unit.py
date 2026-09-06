"""stata_get_help returns a section, not a whole 40k-character page."""

import pytest

from mcp_stata.server import (
    HELP_BUDGET_ENV,
    _extract_help_format,
    _help_budget,
    _split_help_sections,
)

# Shaped like a real rendered help page: preamble, then "## " sections.
HELP = "\n".join(
    [
        "# Help for regress",
        "",
        "## Syntax",
        "",
        "`regress depvar indepvars [if] [in] [weight] [, options]`",
        "",
        "### Model",
        "  noconstant    suppress constant term",
        "",
        "## Description",
        "",
        "`regress` performs ordinary least-squares linear regression.",
        "",
        "## Options",
        "",
        "  vce(vcetype)  robust, cluster, bootstrap",
        "",
        "## Examples",
        "",
        "  . regress price mpg",
        "",
        "## Stored results",
        "",
        "  e(N)  number of observations",
    ]
)


def test_split_keeps_preamble_separate_from_sections():
    sections = _split_help_sections(HELP.splitlines())
    assert sections[0][0] == ""
    assert [h for h, _ in sections[1:]] == [
        "Syntax",
        "Description",
        "Options",
        "Examples",
        "Stored results",
    ]


def test_brief_is_the_default_and_keeps_syntax_plus_description():
    out = _extract_help_format(HELP, "brief")

    assert out.startswith("# Help for regress")
    assert "ordinary least-squares" in out
    assert "noconstant" in out
    # The expensive sections are named, not included.
    assert "e(N)  number of observations" not in out
    assert "vce(vcetype)" not in out
    assert "Omitted sections: Options, Examples, Stored results" in out


def test_named_sections_are_returned_whole():
    options = _extract_help_format(HELP, "options")
    assert "vce(vcetype)" in options
    # The syntax line also contains the word "options"; section parsing must not
    # anchor there, which is what the old substring scan did.
    assert "depvar indepvars" not in options

    examples = _extract_help_format(HELP, "examples")
    assert ". regress price mpg" in examples
    assert "vce(vcetype)" not in examples

    syntax = _extract_help_format(HELP, "syntax")
    assert "depvar indepvars" in syntax
    assert "ordinary least-squares" not in syntax


def test_full_is_still_available_but_capped(monkeypatch):
    monkeypatch.setenv(HELP_BUDGET_ENV, "4000")
    assert _extract_help_format(HELP, "full") == HELP  # short page passes through

    huge = "# Help for xtreg\n\n## Syntax\n\n" + ("x" * 50000) + "\nTAIL"
    out = _extract_help_format(huge, "full")
    assert len(out) < 4000 + 200
    assert out.startswith("# Help for xtreg")
    assert out.endswith("TAIL")
    assert "Re-request with a narrower format=" in out


def test_plain_text_help_has_no_headings_and_falls_back_to_head():
    plain = "\n".join(f"line {i}" for i in range(200))
    out = _extract_help_format(plain, "brief")
    assert out.startswith("line 0")
    assert out.endswith("line 39")
    assert "Omitted sections" not in out


@pytest.mark.parametrize(
    "value,expected",
    [("", 4000), ("1500", 1500), ("0", 0), ("not-a-number", 4000), ("-5", 0)],
)
def test_budget_env_override(monkeypatch, value, expected):
    monkeypatch.setenv(HELP_BUDGET_ENV, value)
    assert _help_budget() == expected


def test_budget_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv(HELP_BUDGET_ENV, "0")
    huge = "# Help for xtreg\n\n## Syntax\n\n" + ("x" * 50000)
    assert _extract_help_format(huge, "full") == huge
