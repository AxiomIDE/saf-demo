# EPIC-SAF-003 follow-up: explicit terminal node for the seed flow.
#
# The reasoner's terminate branch needs *something* to forward to,
# otherwise no edge condition matches when ReasonerOut.action_type ==
# "terminate" and the dispatch loop falls through to its "graph
# produced no terminal result" error path — surfacing the leaf as
# FAILED. With this node + a conditional edge from the reasoner gated
# on action_type == "terminate", the leaf execution completes cleanly
# (FLOW_COMPLETED) and TerminalResult.answer reaches the ResultPanel.
"""Terminate — extract the reasoner's final answer + state into TerminalResult."""
from __future__ import annotations

from gen.messages_pb2 import ReasonerOut, TerminalResult
from gen.axiom_context import AxiomContext


def terminate(ax: AxiomContext, input: ReasonerOut) -> TerminalResult:
    answer = input.terminal_answer.strip()
    if not answer:
        # Defensive: a reasoner that produced no terminal_answer is
        # better surfaced as a quiet "(no answer)" than a blank string
        # that looks like the node misfired.
        answer = "(no answer)"
    ax.log.info(
        "terminate node reached",
        iteration=input.iteration,
        answer=answer[:80],
    )

    out = TerminalResult()
    out.goal = input.goal
    out.answer = answer
    out.iteration = input.iteration
    return out


__all__ = ["terminate"]
