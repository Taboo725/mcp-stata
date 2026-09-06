"""_truncate_text keeps both ends of long Stata output."""
import pytest

from mcp_stata.server import _truncate_text


def test_short_text_is_untouched():
    assert _truncate_text("hello") == "hello"
    assert _truncate_text(None) is None
    assert _truncate_text("") == ""
    exactly = "x" * 5000
    assert _truncate_text(exactly) == exactly


def test_long_text_keeps_head_and_tail():
    text = "HEAD" + "x" * 20000 + "TAIL"
    out = _truncate_text(text)

    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "truncated: 20008 total characters" in out
    # The marker adds a line of its own; the retained payload stays at the cap.
    assert len(out) < 5000 + 200


def test_limit_is_respected():
    out = _truncate_text("A" + "y" * 600 + "Z", limit=300)
    assert out.startswith("A")
    assert out.endswith("Z")
    assert "showing the first 100 and the last 200" in out
