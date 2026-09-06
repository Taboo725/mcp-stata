"""Package detection resolves commands instead of grepping `ado dir`."""

import pytest

from mcp_stata.server import (
    EXTRA_PACKAGES_ENV,
    _DEFAULT_PACKAGE_TARGETS,
    _package_probe_code,
    _package_summary,
    _package_targets,
)


@pytest.fixture(autouse=True)
def _no_extra_packages(monkeypatch):
    monkeypatch.delenv(EXTRA_PACKAGES_ENV, raising=False)


def test_probe_output_maps_to_availability():
    probe = "mcp_pkg:reghdfe=1\nmcp_pkg:estout=1\nmcp_pkg:gtools=0\n"
    summary = _package_summary(probe)

    assert summary["reghdfe"] is True
    assert summary["estout"] is True
    assert summary["gtools"] is False
    # Targets the probe never reported stay absent rather than missing entirely.
    assert summary["ppmlhdfe"] is False
    assert set(summary) == set(_package_targets())


def test_an_ado_dir_listing_no_longer_decides_anything():
    """The bug: reghdfe and estout are installed under PLUS but hold no
    stata.trk entry, so `ado dir` never names them and both read as missing."""
    ado_dir_listing = (
        "[1] package csdid from http://fmwww.bc.edu/repec/bocode/c\n"
        "      'CSDID': module for Difference-in-Difference with multiple periods\n"
        "[2] package boottest from http://fmwww.bc.edu/repec/bocode/b\n"
        "      'BOOTTEST': module to provide fast execution of the wild bootstrap\n"
    )
    summary = _package_summary(ado_dir_listing)

    # No probe markers, so nothing is claimed as present — including the two
    # packages the listing does name, which it cannot vouch for.
    assert not any(summary.values())


def test_a_mention_in_prose_is_not_an_installation():
    """The old scan matched the bare word anywhere, so a package *description*
    naming a dependency reported that dependency as installed."""
    probe = "mcp_pkg:ivreghdfe=1\nmcp_pkg:reghdfe=0\n"
    prose = "'IVREGHDFE': module for estimation, requires reghdfe and ftools"
    summary = _package_summary(probe + prose)

    assert summary["ivreghdfe"] is True
    assert summary["reghdfe"] is False
    assert summary["ftools"] is False


def test_probe_code_covers_every_target():
    targets = _package_targets()
    code = _package_probe_code(targets)

    for name in targets:
        assert name in code
    assert "capture which `__mcp_pkg'" in code
    assert 'display "mcp_pkg:`__mcp_pkg\'="' in code
    # _rc is latched before display, which would otherwise clobber it.
    assert code.index("local __mcp_rc = _rc") < code.index("display")


def test_extra_packages_env_extends_the_list(monkeypatch):
    monkeypatch.setenv(EXTRA_PACKAGES_ENV, "ivreg2, xtabond2 winsor2")
    targets = _package_targets()

    assert targets[: len(_DEFAULT_PACKAGE_TARGETS)] == list(_DEFAULT_PACKAGE_TARGETS)
    assert targets[len(_DEFAULT_PACKAGE_TARGETS) :] == ["ivreg2", "xtabond2", "winsor2"]
    assert _package_summary("mcp_pkg:ivreg2=1")["ivreg2"] is True


def test_extra_packages_env_rejects_non_identifiers(monkeypatch):
    """Names are interpolated into Stata code, so anything but an identifier
    is dropped rather than passed through."""
    monkeypatch.setenv(EXTRA_PACKAGES_ENV, "ok_name, `bad', drop-me, 9lives, reghdfe")
    targets = _package_targets()

    assert "ok_name" in targets
    assert "`bad'" not in targets
    assert "drop-me" not in targets
    assert "9lives" not in targets
    # Already present: listed once, not twice.
    assert targets.count("reghdfe") == 1


def test_missing_or_empty_probe_output_is_not_a_crash():
    assert not any(_package_summary(None).values())
    assert not any(_package_summary("").values())
