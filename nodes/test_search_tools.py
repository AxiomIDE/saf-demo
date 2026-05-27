"""Tests for SearchTools — stub-file replay + degraded mode + URL-build paths."""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))


@pytest.fixture(autouse=True)
def _fake_gen(monkeypatch):
    """Same fake-`gen` machinery as test_llm_reasoner — see that file for context."""

    gen = types.ModuleType("gen")
    messages = types.ModuleType("gen.axiom_official_saf_demo_messages_pb2")
    axiom_context = types.ModuleType("gen.axiom_context")

    class _Repeated(list):
        def extend(self, it):
            list.extend(self, it)

        def append(self, v):
            list.append(self, v)

    class MutationRecord:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Candidate:
        def __init__(self, package_name="", package_version="", description="",
                     input_message_name="", output_message_name="", score=0.0):
            self.package_name = package_name
            self.package_version = package_version
            self.description = description
            self.input_message_name = input_message_name
            self.output_message_name = output_message_name
            self.score = score

    class ToolSpec:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0
            self.need = ""
            self.preferred_package = ""
            self.preferred_version = ""
            self.note = ""

    class ToolCandidates:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0
            self.note = ""
            self.need = ""
            self.candidates = _Repeated()

    messages.MutationRecord = MutationRecord
    messages.Candidate = Candidate
    messages.ToolSpec = ToolSpec
    messages.ToolCandidates = ToolCandidates

    class AxiomContext: pass
    axiom_context.AxiomContext = AxiomContext

    monkeypatch.setitem(sys.modules, "gen", gen)
    monkeypatch.setitem(sys.modules, "gen.axiom_official_saf_demo_messages_pb2", messages)
    monkeypatch.setitem(sys.modules, "gen.axiom_context", axiom_context)
    yield


class _FakeLog:
    def __init__(self): self.records = []
    def _log(self, lvl, msg, **a): self.records.append((lvl, msg, a))
    def debug(self, msg, **a): self._log("debug", msg, **a)
    def info(self, msg, **a): self._log("info", msg, **a)
    def warn(self, msg, **a): self._log("warn", msg, **a)
    def error(self, msg, **a): self._log("error", msg, **a)


class _FakeCtx:
    def __init__(self):
        self.log = _FakeLog()


def test_stub_mode_returns_candidates_for_iteration(monkeypatch, tmp_path):
    trace = {
        "by_iteration": {
            "1": [
                {"package_name": "axiom-official/http-fetch", "version": "0.1.0",
                 "description": "GET a URL", "input_message_name": "HTTPReq",
                 "output_message_name": "HTTPResp", "score": 0.92},
                {"package_name": "axiom-official/curl", "version": "0.2.0", "score": 0.50},
            ],
            "2": [],
        }
    }
    p = tmp_path / "search-trace.json"
    p.write_text(json.dumps(trace))
    monkeypatch.setenv("AXIOM_SEARCH_STUB_PATH", str(p))
    monkeypatch.delenv("REGISTRY_URL", raising=False)

    from nodes import search_tools
    from gen.axiom_official_saf_demo_messages_pb2 import ToolSpec

    spec = ToolSpec()
    spec.goal = "g"
    spec.iteration = 1
    spec.need = "fetch a URL"
    out = search_tools.search_tools(_FakeCtx(), spec)

    assert len(out.candidates) == 2
    assert out.candidates[0].package_name == "axiom-official/http-fetch"
    assert out.candidates[0].score == pytest.approx(0.92)


def test_stub_mode_missing_iteration_returns_empty(monkeypatch, tmp_path):
    p = tmp_path / "search-trace.json"
    p.write_text(json.dumps({"by_iteration": {"1": [{"package_name": "x"}]}}))
    monkeypatch.setenv("AXIOM_SEARCH_STUB_PATH", str(p))
    monkeypatch.delenv("REGISTRY_URL", raising=False)

    from nodes import search_tools
    from gen.axiom_official_saf_demo_messages_pb2 import ToolSpec

    spec = ToolSpec()
    spec.iteration = 7
    spec.need = "x"
    out = search_tools.search_tools(_FakeCtx(), spec)
    assert list(out.candidates) == []


def test_degraded_mode_no_env_returns_empty(monkeypatch):
    monkeypatch.delenv("AXIOM_SEARCH_STUB_PATH", raising=False)
    monkeypatch.delenv("REGISTRY_URL", raising=False)

    from nodes import search_tools
    from gen.axiom_official_saf_demo_messages_pb2 import ToolSpec

    spec = ToolSpec()
    spec.iteration = 1
    spec.need = "y"
    ctx = _FakeCtx()
    out = search_tools.search_tools(ctx, spec)

    assert list(out.candidates) == []
    # Warn log was emitted explaining the degraded path.
    assert any(rec[0] == "warn" for rec in ctx.log.records)


def test_registry_call_failure_returns_empty(monkeypatch):
    """If urlopen raises, we collapse to an empty candidate list."""
    monkeypatch.delenv("AXIOM_SEARCH_STUB_PATH", raising=False)
    monkeypatch.setenv("REGISTRY_URL", "http://registry.invalid")

    from nodes import search_tools
    import urllib.request

    def _boom(*a, **kw):
        raise ConnectionError("nope")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    from gen.axiom_official_saf_demo_messages_pb2 import ToolSpec
    spec = ToolSpec()
    spec.iteration = 1
    spec.need = "z"
    ctx = _FakeCtx()
    out = search_tools.search_tools(ctx, spec)
    assert list(out.candidates) == []
    assert any(rec[0] == "error" for rec in ctx.log.records)


def test_registry_call_happy_path(monkeypatch):
    """urlopen returns valid JSON => we materialize the top N candidates."""
    monkeypatch.delenv("AXIOM_SEARCH_STUB_PATH", raising=False)
    monkeypatch.setenv("REGISTRY_URL", "http://registry.local:8082")

    from nodes import search_tools
    import urllib.request

    body = json.dumps({
        "packages": [
            {
                "name": "axiom-official/http-fetch",
                "version": "0.1.0",
                "description": "Fetches URLs",
                "score": 0.9,
                "nodes": [{
                    "name": "Fetch",
                    "input_message_name": "FetchIn",
                    "output_message_name": "FetchOut",
                }],
            },
            {
                "name": "axiom-official/curl",
                "version": "0.2.0",
                "score": 0.5,
                "nodes": [{
                    "name": "Curl",
                    "input_message_name": "CurlIn",
                    "output_message_name": "CurlOut",
                }],
            },
        ],
    }).encode("utf-8")

    class _Resp:
        def __init__(self, body): self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self._b

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Resp(body))

    from gen.axiom_official_saf_demo_messages_pb2 import ToolSpec
    spec = ToolSpec()
    spec.iteration = 1
    spec.need = "fetch"
    out = search_tools.search_tools(_FakeCtx(), spec)
    assert len(out.candidates) == 2
    assert out.candidates[0].package_name == "axiom-official/http-fetch"
    assert out.candidates[1].score == pytest.approx(0.5)
