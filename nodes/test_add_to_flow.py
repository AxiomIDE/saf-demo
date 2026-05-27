"""Tests for AddToFlow — mutation emission, top-candidate selection,
and corrective-terminate paths."""
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
    messages = types.ModuleType("gen.axiom_official_saf_demo_messages_pb2")
    axiom_context = types.ModuleType("gen.axiom_context")

    class _Repeated(list):
        def extend(self, it): list.extend(self, it)
        def append(self, v): list.append(self, v)

    class MutationRecord:
        def __init__(self, iteration=0, package_name="", package_version="", note=""):
            self.iteration = iteration
            self.package_name = package_name
            self.package_version = package_version
            self.note = note

    class Candidate:
        def __init__(self, package_name="", package_version="", description="",
                     input_message_name="", output_message_name="", score=0.0):
            self.package_name = package_name
            self.package_version = package_version
            self.description = description
            self.input_message_name = input_message_name
            self.output_message_name = output_message_name
            self.score = score

    class ToolCandidates:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 1
            self.need = ""
            self.note = ""
            self.candidates = _Repeated()

    class MutationAck:
        def __init__(self):
            self.goal = ""
            self.history = _Repeated()
            self.iteration = 0
            self.ok = False
            self.note = ""

    messages.MutationRecord = MutationRecord
    messages.Candidate = Candidate
    messages.ToolCandidates = ToolCandidates
    messages.MutationAck = MutationAck

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


class _FakeMutationFlow:
    def __init__(self, parent_count=5):
        self.added_nodes = []
        self.added_edges = []
        self._parent_count = parent_count

    def add_node(self, package, version, canvas_position=None):
        iid = self._parent_count + len(self.added_nodes)
        self.added_nodes.append((package, version))
        return iid

    def add_edge(self, src_instance, dst_instance):
        self.added_edges.append((src_instance, dst_instance))


class _FakeReflection:
    def __init__(self, current_instance=3, loop_dst=1):
        self.flow = self
        class _Pos: pass
        self.position = _Pos()
        self.position.current_instance = current_instance
        class _LoopEdge:
            def __init__(self, src, dst):
                self.src_instance = src
                self.dst_instance = dst
        self.loop_edges = [_LoopEdge(current_instance, loop_dst)]


class _FakeMutation:
    def __init__(self):
        self.flow = _FakeMutationFlow()


class _FakeCtx:
    def __init__(self, *, loop_dst=1, current_instance=3):
        self.log = _FakeLog()
        self.reflection = _FakeReflection(current_instance=current_instance,
                                           loop_dst=loop_dst)
        self.mutation = _FakeMutation()


def _candidate(messages_mod, name, version, score):
    c = messages_mod.Candidate()
    c.package_name = name
    c.package_version = version
    c.score = score
    return c


def test_happy_path_emits_mutation(monkeypatch):
    from nodes import add_to_flow
    from gen.axiom_official_saf_demo_messages_pb2 import (
        Candidate, ToolCandidates,
    )

    inp = ToolCandidates()
    inp.goal = "g"
    inp.iteration = 2
    inp.need = "fetch a URL"
    inp.note = "need http"
    inp.candidates.extend([
        _candidate(sys.modules["gen.axiom_official_saf_demo_messages_pb2"],
                   "axiom-official/curl", "0.2.0", 0.4),
        _candidate(sys.modules["gen.axiom_official_saf_demo_messages_pb2"],
                   "axiom-official/http-fetch", "0.1.0", 0.91),
    ])

    ctx = _FakeCtx(current_instance=3, loop_dst=1)
    ack = add_to_flow.add_to_flow(ctx, inp)

    assert ack.ok is True
    assert ack.iteration == 3  # bumped for next loop
    # Top candidate by score wins.
    assert ctx.mutation.flow.added_nodes == [("axiom-official/http-fetch", "0.1.0")]
    # Edges: current → new_node, new_node → loop target.
    assert ctx.mutation.flow.added_edges[0] == (3, 5)  # parent_count=5 → new iid 5
    assert ctx.mutation.flow.added_edges[1] == (5, 1)  # → loop target
    # History grew by one.
    assert len(list(ack.history)) == 1
    assert list(ack.history)[0].package_name == "axiom-official/http-fetch"


def test_zero_candidates_terminates_without_mutation():
    from nodes import add_to_flow
    from gen.axiom_official_saf_demo_messages_pb2 import ToolCandidates

    inp = ToolCandidates()
    inp.iteration = 1
    inp.need = "magical-tool-that-doesnt-exist"
    ctx = _FakeCtx()
    ack = add_to_flow.add_to_flow(ctx, inp)

    assert ack.ok is False
    assert "no candidates" in ack.note.lower()
    assert ctx.mutation.flow.added_nodes == []
    assert ctx.mutation.flow.added_edges == []


def test_missing_loop_target_terminates():
    from nodes import add_to_flow
    from gen.axiom_official_saf_demo_messages_pb2 import (
        Candidate, ToolCandidates,
    )

    inp = ToolCandidates()
    inp.iteration = 1
    inp.need = "x"
    c = inp.candidates
    one = sys.modules["gen.axiom_official_saf_demo_messages_pb2"].Candidate()
    one.package_name = "axiom-official/x"
    one.package_version = "0.1.0"
    one.score = 0.5
    c.append(one)

    ctx = _FakeCtx()
    ctx.reflection.loop_edges = []  # no loop in reflection
    ack = add_to_flow.add_to_flow(ctx, inp)
    assert ack.ok is False
    assert "loop" in ack.note.lower()


def test_history_threads_through_iterations():
    """The reasoner's MutationRecord list passes through unmodified
    except for the new entry appended on each successful AddToFlow."""
    from nodes import add_to_flow
    from gen.axiom_official_saf_demo_messages_pb2 import (
        Candidate, MutationRecord, ToolCandidates,
    )

    inp = ToolCandidates()
    inp.iteration = 3
    inp.need = "step 3"
    inp.note = "step-3-rationale"
    inp.history.append(MutationRecord(iteration=1, package_name="a/b", package_version="0.1.0"))
    inp.history.append(MutationRecord(iteration=2, package_name="c/d", package_version="0.2.0"))
    c = sys.modules["gen.axiom_official_saf_demo_messages_pb2"].Candidate()
    c.package_name = "e/f"
    c.package_version = "0.3.0"
    c.score = 0.7
    inp.candidates.append(c)

    ctx = _FakeCtx()
    ack = add_to_flow.add_to_flow(ctx, inp)

    history = list(ack.history)
    assert len(history) == 3
    assert [h.package_name for h in history] == ["a/b", "c/d", "e/f"]
    assert history[-1].note == "step-3-rationale"
