"""Tests for the Terminate node — passthrough that surfaces ReasonerOut
fields into TerminalResult for the SPA's ResultPanel."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))


@pytest.fixture(autouse=True)
def _fake_gen(monkeypatch):
    gen = types.ModuleType("gen")
    messages = types.ModuleType("gen.messages_pb2")
    axiom_context = types.ModuleType("gen.axiom_context")

    class ReasonerOut:
        def __init__(self):
            self.goal = ""
            self.iteration = 0
            self.action_type = ""
            self.terminal_answer = ""

    class TerminalResult:
        def __init__(self):
            self.goal = ""
            self.answer = ""
            self.iteration = 0

    messages.ReasonerOut = ReasonerOut
    messages.TerminalResult = TerminalResult

    class AxiomContext: pass
    axiom_context.AxiomContext = AxiomContext

    monkeypatch.setitem(sys.modules, "gen", gen)
    monkeypatch.setitem(sys.modules, "gen.messages_pb2", messages)
    monkeypatch.setitem(sys.modules, "gen.axiom_context", axiom_context)
    yield


class _FakeLog:
    def __init__(self): self.records = []
    def _log(self, lvl, msg, **a): self.records.append((lvl, msg, a))
    def debug(self, m, **a): self._log("debug", m, **a)
    def info(self, m, **a): self._log("info", m, **a)
    def warn(self, m, **a): self._log("warn", m, **a)
    def error(self, m, **a): self._log("error", m, **a)


class _FakeCtx:
    def __init__(self):
        self.log = _FakeLog()


def test_terminate_passes_through_answer():
    from nodes import terminate
    from gen.messages_pb2 import ReasonerOut

    inp = ReasonerOut()
    inp.goal = "summarize a URL"
    inp.iteration = 4
    inp.action_type = "terminate"
    inp.terminal_answer = "Flow now does fetch → extract → summarize."

    out = terminate.terminate(_FakeCtx(), inp)
    assert out.goal == "summarize a URL"
    assert out.answer == "Flow now does fetch → extract → summarize."
    assert out.iteration == 4


def test_terminate_blank_answer_substitutes_placeholder():
    """A reasoner that emits an empty terminal_answer should surface as
    a quiet "(no answer)" rather than an empty string — the SPA's
    ResultPanel shouldn't render a blank that looks like the node misfired."""
    from nodes import terminate
    from gen.messages_pb2 import ReasonerOut

    inp = ReasonerOut()
    inp.iteration = 3
    inp.terminal_answer = "   "  # whitespace only
    out = terminate.terminate(_FakeCtx(), inp)
    assert out.answer == "(no answer)"
