# ADR-051 (2026-05-26): the sole mutation_capable node in the
# self-assembling demo. Picks the top candidate from SearchTools and
# emits a MutationBatch via ax.mutation.flow.* that the platform forks
# into a child execution. Failure policy is corrective-terminate: if
# SearchTools returned zero candidates, the node returns a terminating
# MutationAck WITHOUT emitting a batch — the seed flow branches on
# .ok and routes to the terminal node instead of looping.
"""AddToFlow — splices the chosen tool into the running flow.

Behavior:

  1. If ``input.candidates`` is empty, return an ``ok=false`` MutationAck
     with a corrective note appended to history. The seed flow's
     conditional edge sees ``ok=false`` and routes to the terminal node,
     ending the agent cleanly.

  2. Otherwise, pick the top candidate (highest .score, breaking ties
     by list order) and call ``ax.mutation.flow.add_node`` /
     ``add_edge`` to graft it between the current node and the loop-
     back edge target (LLMReasoner).

  3. The returned MutationAck propagates the updated MutationRecord
     history so the next reasoner iteration can see what's been added.

The actual fork (child execution, new graph artifact, FORKED parent)
is performed by the worker after it reads NodeResponse.mutation_batch.
This node knows nothing about it.
"""
from __future__ import annotations

from typing import List, Optional

from gen.axiom_official_saf_demo_messages_pb2 import (
    Candidate,
    MutationAck,
    MutationRecord,
    ToolCandidates,
)
from gen.axiom_context import AxiomContext


def add_to_flow(ax: AxiomContext, input: ToolCandidates) -> MutationAck:
    history: List[MutationRecord] = list(input.history)

    if not input.candidates:
        ax.log.warn(
            "AddToFlow received zero candidates — terminating",
            iteration=input.iteration,
            need=input.need[:80],
        )
        return _terminate_no_candidates(input, history)

    chosen = _pick_top(list(input.candidates))
    ax.log.info(
        "AddToFlow chose candidate",
        package=chosen.package_name,
        version=chosen.package_version,
        score=float(chosen.score),
        iteration=input.iteration,
    )

    # Find the loop-back edge target so we can wire (current → new tool
    # → loop_target). The reasoner that started this iteration sits at
    # position.current_instance; the AddToFlow node sits one or two
    # hops downstream depending on the seed-flow shape. We use the
    # loop_edges surface to find the destination instance that closes
    # the loop back to the reasoner.
    pos = ax.reflection.flow.position
    loop_target = _find_loop_target(ax)
    if loop_target is None:
        ax.log.warn(
            "AddToFlow could not resolve loop target — terminating",
            current_instance=pos.current_instance,
        )
        return _terminate_no_loop(input, history)

    try:
        new_iid = ax.mutation.flow.add_node(
            package=chosen.package_name,
            version=chosen.package_version,
        )
        # Wire the new tool: current AddToFlow → new tool → loop target.
        ax.mutation.flow.add_edge(
            src_instance=pos.current_instance,
            dst_instance=new_iid,
        )
        ax.mutation.flow.add_edge(
            src_instance=new_iid,
            dst_instance=loop_target,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a terminating ack
        ax.log.error("mutation buffer failed", error=str(exc))
        return _terminate_failure(input, history, str(exc))

    history.append(
        MutationRecord(
            iteration=input.iteration,
            package_name=chosen.package_name,
            package_version=chosen.package_version,
            note=input.note,
        )
    )

    ack = MutationAck()
    ack.goal = input.goal
    ack.history.extend(history)
    ack.iteration = input.iteration + 1
    ack.ok = True
    ack.note = f"added {chosen.package_name}@{chosen.package_version}"
    return ack


def _pick_top(cands: List[Candidate]) -> Candidate:
    return max(cands, key=lambda c: float(c.score or 0.0))


def _find_loop_target(ax: AxiomContext) -> Optional[int]:
    """Return the dst_instance of the loop edge whose source is on the
    current node's path back to the reasoner.

    The seed flow has exactly one loop edge — AddToFlow → LLMReasoner.
    We return that edge's dst_instance. If reflection is unavailable
    (older worker) or there is no loop edge, returns None and the
    caller terminates.
    """
    try:
        loop_edges = list(ax.reflection.flow.loop_edges or [])
    except Exception:  # noqa: BLE001
        return None

    if not loop_edges:
        return None

    # Prefer the loop edge that originates from our own current instance.
    pos = ax.reflection.flow.position
    for e in loop_edges:
        if int(e.src_instance) == int(pos.current_instance):
            return int(e.dst_instance)
    # Otherwise just use the first loop edge — single-loop demo invariant.
    return int(loop_edges[0].dst_instance)


def _terminate_no_candidates(
    input: ToolCandidates, history: List[MutationRecord]
) -> MutationAck:
    ack = MutationAck()
    ack.goal = input.goal
    ack.history.extend(history)
    ack.iteration = input.iteration + 1
    ack.ok = False
    ack.note = f"no candidates for need={input.need!r}; terminating"
    return ack


def _terminate_no_loop(
    input: ToolCandidates, history: List[MutationRecord]
) -> MutationAck:
    ack = MutationAck()
    ack.goal = input.goal
    ack.history.extend(history)
    ack.iteration = input.iteration + 1
    ack.ok = False
    ack.note = "no loop edge in reflection — flow misconfigured"
    return ack


def _terminate_failure(
    input: ToolCandidates, history: List[MutationRecord], reason: str
) -> MutationAck:
    ack = MutationAck()
    ack.goal = input.goal
    ack.history.extend(history)
    ack.iteration = input.iteration + 1
    ack.ok = False
    ack.note = f"mutation failed: {reason}"
    return ack


__all__ = ["add_to_flow"]
