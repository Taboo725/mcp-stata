"""The mcp SDK 2.0 removed ``mcp.server.fastmcp``, which the server imports.

Without an upper bound a fresh install resolves mcp 2.x and the server dies at
start-up (upstream issue #64). Guard the pin so it is not dropped by accident.
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def test_mcp_dependency_excludes_sdk_2():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    (mcp,) = [Requirement(d) for d in deps if Requirement(d).name == "mcp"]
    assert Version("1.24.0") in mcp.specifier
    assert Version("2.0.0") not in mcp.specifier


def test_fastmcp_module_still_importable():
    import mcp.server.fastmcp  # noqa: F401
