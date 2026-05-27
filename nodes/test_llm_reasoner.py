"""Tests for the LLMReasoner node.

These tests stub the Anthropic call entirely — they exercise the stub-
file replay path plus the JSON-parsing / error-degradation paths. The
live-LLM path is tested manually via the screencast workflow.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# The Python node tooling normally injects the generated proto bindings
# at `gen/` next to the package. For unit testing we synthesize a tiny
# duck-typed `gen` module so we don't need to run `axiom build` before
# `pytest`.
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))


@pytest.fixture(autouse=True)
def _fake_gen(monkeypatch, tmp_path):
    """Install a fake `gen` package that mirrors the generated message
    shapes just well enough for the reasoner to construct and inspect
    them. Real generated bindings use protobuf classes; the duck types
    below match what the reasoner code reads/writes."""
    import types

    gen = types.ModuleType("gen")
    messages = types.ModuleType("gen.axiom_official_saf_demo_messages_pb2")
    axiom_context = types.ModuleType("gen.axiom_context")

    class _Repeated(list):
        def extend(self, it):
            list.extend(self, it)

        def append(self, v):
            list.append(self, v)

    class MutationRecord:
        def __init__(self, iteration=0, package_name="", package_version="", note=""):
            self.iteration = iteration
            self.package_name = package_name
            self.package_version = package_version
            self.note = note

    class ToolSpec:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0
            self.need = ""
            self.preferred_package = ""
            self.preferred_version = ""
            self.note = ""

    class ReasonerOut:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0
            self.action_type = ""
            self.tool_spec = ToolSpec()
            self.terminal_answer = ""

        def CopyFrom(self, src):
            for k, v in vars(src).items():
                setattr(self, k, v)

    # tool_spec.CopyFrom needs to take a ToolSpec; emulate it.
    def _ts_copyfrom(self, src):
        for k, v in vars(src).items():
            setattr(self, k, v)
    ToolSpec.CopyFrom = _ts_copyfrom

    class ReasonerIn:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0

    messages.MutationRecord = MutationRecord
    messages.ToolSpec = ToolSpec
    messages.ReasonerOut = ReasonerOut
    messages.ReasonerIn = ReasonerIn

    class AxiomContext:  # marker only, runtime is duck-typed
        pass

    axiom_context.AxiomContext = AxiomContext

    monkeypatch.setitem(sys.modules, "gen", gen)
    monkeypatch.setitem(sys.modules, "gen.axiom_official_saf_demo_messages_pb2", messages)
    monkeypatch.setitem(sys.modules, "gen.axiom_context", axiom_context)
    yield


class _FakeLog:
    def __init__(self):
        self.records = []

    def _log(self, level, msg, **attrs):
        self.records.append((level, msg, attrs))

    def debug(self, msg, **a): self._log("debug", msg, **a)
    def info(self, msg, **a): self._log("info", msg, **a)
    def warn(self, msg, **a): self._log("warn", msg, **a)
    def error(self, msg, **a): self._log("error", msg, **a)


class _FakeSecrets:
    def __init__(self, mapping=None):
        self._m = mapping or {}

    def get(self, name):
        v = self._m.get(name)
        if v is None:
            return ("", False)
        return (v, True)


class _FakeCtx:
    def __init__(self, secrets=None):
        self.log = _FakeLog()
        self.secrets = secrets or _FakeSecrets()


def _make_input(messages_mod, goal="goal-x", iteration=1, history=()):
    inp = messages_mod.ReasonerIn()
    inp.goal = goal
    inp.iteration = iteration
    for h in history:
        inp.history.append(h)
    return inp


def test_stub_mode_add_tool(monkeypatch, tmp_path):
    """Stub mode returns the add_tool decision for iteration 1."""
    transcript = {
        "goal": "summarize a URL",
        "responses": [
            {
                "action": "add_tool",
                "need": "fetch URL contents",
                "preferred_package": "axiom-official/http-fetch",
                "preferred_version": "0.1.0",
                "note": "I need raw HTML before I can summarize.",
            }
        ],
    }
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(transcript))
    monkeypatch.setenv("AXIOM_LLM_STUB_PATH", str(p))

    from nodes import llm_reasoner
    from gen.axiom_official_saf_demo_messages_pb2 import ReasonerIn

    ctx = _FakeCtx()
    inp = ReasonerIn()
    inp.goal = "summarize a URL"
    inp.iteration = 1
    out = llm_reasoner.llm_reasoner(ctx, inp)

    assert out.action_type == "add_tool"
    assert out.tool_spec.need == "fetch URL contents"
    assert out.tool_spec.preferred_package == "axiom-official/http-fetch"
    assert out.tool_spec.note.startswith("I need raw HTML")


def test_stub_mode_terminate(monkeypatch, tmp_path):
    """Stub mode returns the terminate decision when the transcript says so."""
    transcript = {
        "goal": "summarize",
        "responses": [
            {"action": "terminate", "terminal_answer": "done summarizing"}
        ],
    }
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(transcript))
    monkeypatch.setenv("AXIOM_LLM_STUB_PATH", str(p))

    from nodes import llm_reasoner
    from gen.axiom_official_saf_demo_messages_pb2 import ReasonerIn

    inp = ReasonerIn()
    inp.goal = "summarize"
    inp.iteration = 1
    out = llm_reasoner.llm_reasoner(_FakeCtx(), inp)

    assert out.action_type == "terminate"
    assert out.terminal_answer == "done summarizing"


def test_stub_mode_exhausted_terminates(monkeypatch, tmp_path):
    """When the stub has no entry for the iteration the reasoner terminates."""
    transcript = {"responses": [{"action": "add_tool", "need": "x"}]}
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(transcript))
    monkeypatch.setenv("AXIOM_LLM_STUB_PATH", str(p))

    from nodes import llm_reasoner
    from gen.axiom_official_saf_demo_messages_pb2 import ReasonerIn

    inp = ReasonerIn()
    inp.goal = "x"
    inp.iteration = 5  # past the end
    out = llm_reasoner.llm_reasoner(_FakeCtx(), inp)
    assert out.action_type == "terminate"


def test_iteration_cap_overrides_stub(monkeypatch, tmp_path):
    """Even with a viable stub, hitting MAX_ITERATIONS forces terminate."""
    transcript = {
        "responses": [{"action": "add_tool", "need": "y"}] * 20,
    }
    p = tmp_path / "trace.json"
    p.write_text(json.dumps(transcript))
    monkeypatch.setenv("AXIOM_LLM_STUB_PATH", str(p))

    from nodes import llm_reasoner
    from gen.axiom_official_saf_demo_messages_pb2 import ReasonerIn

    inp = ReasonerIn()
    inp.goal = "y"
    inp.iteration = llm_reasoner.MAX_ITERATIONS + 1
    out = llm_reasoner.llm_reasoner(_FakeCtx(), inp)
    assert out.action_type == "terminate"
    assert "cap" in out.terminal_answer.lower()


def test_no_api_key_terminates_in_live_mode(monkeypatch):
    """Live mode with no ANTHROPIC_API_KEY in secrets => terminate cleanly."""
    monkeypatch.delenv("AXIOM_LLM_STUB_PATH", raising=False)

    from nodes import llm_reasoner
    from gen.axiom_official_saf_demo_messages_pb2 import ReasonerIn

    inp = ReasonerIn()
    inp.goal = "anything"
    inp.iteration = 1
    out = llm_reasoner.llm_reasoner(_FakeCtx(), inp)
    assert out.action_type == "terminate"
    assert "ANTHROPIC_API_KEY" in out.terminal_answer


def test_parse_json_block_strips_fences():
    """The fence-stripping helper handles ```json wrappers."""
    from nodes.llm_reasoner import _parse_json_block

    obj = _parse_json_block('```json\n{"action": "terminate", "terminal_answer": "ok"}\n```')
    assert obj["action"] == "terminate"


def test_parse_json_block_handles_extra_prose():
    """Models sometimes add a sentence before/after the JSON; we still parse."""
    from nodes.llm_reasoner import _parse_json_block

    obj = _parse_json_block("Here you go:\n{\"action\": \"add_tool\", \"need\": \"foo\"}\nLet me know.")
    assert obj["need"] == "foo"


def test_parse_json_block_raises_on_garbage():
    from nodes.llm_reasoner import _parse_json_block

    with pytest.raises(ValueError):
        _parse_json_block("not json at all")
