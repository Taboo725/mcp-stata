"""Break must reach Stata even where ``sfi.breakIn`` does not exist.

StataNow 19.5's ``sfi`` has no ``breakIn``; the old code silently did nothing,
so ``stata_control(break)`` and task cancellation never interrupted Stata and
the next command queued behind the "cancelled" one. The fallback is the call
pystata itself makes on Ctrl+C, ``pystata.config.stlib.StataSO_SetBreak()``.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from mcp_stata.stata_client import StataClient


def _install(monkeypatch, *, break_in=None, set_break=None):
    sfi = types.ModuleType("sfi")
    if break_in is not None:
        sfi.breakIn = break_in
    monkeypatch.setitem(sys.modules, "sfi", sfi)

    pystata = types.ModuleType("pystata")
    config = types.ModuleType("pystata.config")
    config.stlib = None if set_break is None else types.SimpleNamespace(StataSO_SetBreak=set_break)
    pystata.config = config
    monkeypatch.setitem(sys.modules, "pystata", pystata)
    monkeypatch.setitem(sys.modules, "pystata.config", config)


def _client() -> StataClient:
    client = StataClient.__new__(StataClient)
    client._break_requested = False
    return client


def test_falls_back_to_native_set_break(monkeypatch):
    set_break = MagicMock()
    _install(monkeypatch, set_break=set_break)
    fn, name = StataClient._break_hook()
    assert name == "StataSO_SetBreak"

    client = _client()
    client._request_break_in()
    assert set_break.call_count == 3
    assert client._break_requested is True


def test_fast_path_uses_native_set_break(monkeypatch):
    set_break = MagicMock()
    _install(monkeypatch, set_break=set_break)
    client = _client()
    client._request_break_in_fast()
    set_break.assert_called_once()
    assert client._break_requested is True


def test_prefers_sfi_break_in_when_present(monkeypatch):
    break_in, set_break = MagicMock(), MagicMock()
    _install(monkeypatch, break_in=break_in, set_break=set_break)
    _, name = StataClient._break_hook()
    assert name == "sfi.breakIn"
    client = _client()
    client._poll_break_ack = MagicMock()
    client._request_break_in()
    assert break_in.call_count == 3
    set_break.assert_not_called()


def test_no_hook_is_harmless(monkeypatch):
    _install(monkeypatch)
    assert StataClient._break_hook() == (None, "")
    client = _client()
    client._request_break_in()
    client._request_break_in_fast()
    assert client._break_requested is False


def test_raising_hook_is_swallowed(monkeypatch):
    _install(monkeypatch, set_break=MagicMock(side_effect=OSError("boom")))
    client = _client()
    client._request_break_in()
    client._request_break_in_fast()
