# ADR-051 (2026-05-26) + ADR-052 (2026-05-26): the reasoner is the
# brain of the self-assembling agent demo. It is intentionally NOT
# mutation_capable — it only proposes; the AddToFlow node down the
# loop is what emits MutationBatch. Keeping the LLM call out of the
# mutation path means a stubbed/recorded trace can drive the demo end
# to end without touching the real Anthropic API.
"""LLMReasoner — decides the next mutation or terminates the agent.

Two execution modes:

  1. **Live mode** (default). Calls Claude via the anthropic SDK using
     the tenant-secret ``ANTHROPIC_API_KEY``. Cost/latency budget is
     documented in ../README.md.

  2. **Stub mode** (CI / e2e). The reasoner replays a canned transcript
     when either of these is set, in priority order:

       - tenant secret ``AXIOM_LLM_STUB_JSON`` — the *content* of the
         transcript, registered via ``POST /v1/secrets``. Preferred by
         the e2e-gate suite because it requires no container-side
         state.
       - env var ``AXIOM_LLM_STUB_PATH`` — a *file path* the node
         container can read. Convenient for ``axiom dev`` work where
         the file is mounted into the workspace.

     Transcript shape: see ``testdata/golden-trace.json``.

Failure policy when the LLM proposes a tool that SearchTools cannot
find (resolved one iteration later) is implemented downstream by
``AddToFlow``: it terminates the agent with a corrective note. The
reasoner here only enforces an iteration cap.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from gen.messages_pb2 import (
    MutationRecord,
    ReasonerIn,
    ReasonerOut,
    ToolSpec,
)
from gen.axiom_context import AxiomContext


# Hard cap on reasoner iterations. The seed flow's loop edge has its
# own max_iterations (set on publish), but we double-gate here so the
# demo can't accidentally bill an unbounded number of LLM calls.
MAX_ITERATIONS = 8


# Model + budget. Documented in the README; bump cautiously — the demo
# is meant to be cheap enough that a contributor can run it against
# their personal Anthropic key during a screencast.
MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 512


SYSTEM_PROMPT = """You are the reasoning loop of a self-assembling Axiom flow.

You will see:
  - GOAL: what the user wants the flow to accomplish.
  - FLOW: the current graph topology (nodes + edges that already exist).
  - HISTORY: every tool you've already added on previous iterations.
  - ITERATION: how many times you've been called in this run.

Your job is to decide the SINGLE next action:

  - "add_tool": propose one more tool to splice into the flow.
    Return a JSON object with shape:
      {
        "action": "add_tool",
        "need": "<short natural-language description of the capability needed>",
        "preferred_package": "<optional package name like axiom-official/http-fetch>",
        "preferred_version": "<optional version like 0.1.0>",
        "note": "<one-sentence rationale you will see again next iteration>"
      }

  - "terminate": you believe the assembled flow now suffices to meet GOAL.
    Return:
      {
        "action": "terminate",
        "terminal_answer": "<one-paragraph explanation of what the flow now does>"
      }

Rules:
  - Output ONLY a single JSON object. No prose, no markdown fences.
  - Add tools one at a time — there's a downstream search step that
    resolves your "need" into a concrete package on the marketplace.
  - Don't propose a tool you've already added (check HISTORY).
"""


def llm_reasoner(ax: AxiomContext, input: ReasonerIn) -> ReasonerOut:
    iteration = max(int(input.iteration), 1)

    # Hard iteration cap. The downstream AddToFlow node also enforces
    # a cap via MutationLineageCap (ADR-051); this is the SDK-side
    # guard that keeps the LLM bill bounded.
    if iteration > MAX_ITERATIONS:
        ax.log.warn(
            "iteration cap reached — terminating",
            iteration=iteration,
            cap=MAX_ITERATIONS,
        )
        return _terminate(
            input,
            iteration,
            f"Reached the {MAX_ITERATIONS}-iteration cap before settling on a terminal action.",
        )

    decision = _decide(ax, input, iteration)

    action = decision.get("action", "")
    if action == "terminate":
        answer = str(decision.get("terminal_answer", "")).strip() or "(no answer)"
        ax.log.info("reasoner terminating", iteration=iteration, answer=answer[:80])
        return _terminate(input, iteration, answer)

    # Default + explicit "add_tool" branch. Anything else (malformed
    # output, etc.) falls through here and is corrected by SearchTools
    # returning zero candidates on a bogus need.
    need = str(decision.get("need", "")).strip()
    if not need:
        ax.log.warn("reasoner produced no need — terminating", iteration=iteration)
        return _terminate(input, iteration, "Reasoner produced no actionable need.")

    note = str(decision.get("note", "")).strip()
    preferred_pkg = str(decision.get("preferred_package", "")).strip()
    preferred_ver = str(decision.get("preferred_version", "")).strip()

    ax.log.info(
        "reasoner proposing tool",
        iteration=iteration,
        need=need[:80],
        preferred_package=preferred_pkg,
    )

    out = ReasonerOut()
    out.goal = input.goal
    out.history.extend(input.history)
    out.iteration = iteration
    out.action_type = "add_tool"

    spec = ToolSpec()
    spec.goal = input.goal
    spec.history.extend(input.history)
    spec.iteration = iteration
    spec.need = need
    spec.preferred_package = preferred_pkg
    spec.preferred_version = preferred_ver
    spec.note = note
    out.tool_spec.CopyFrom(spec)
    return out


def _terminate(input: ReasonerIn, iteration: int, answer: str) -> ReasonerOut:
    out = ReasonerOut()
    out.goal = input.goal
    out.history.extend(input.history)
    out.iteration = iteration
    out.action_type = "terminate"
    out.terminal_answer = answer
    return out


def _decide(ax: AxiomContext, input: ReasonerIn, iteration: int) -> Dict[str, Any]:
    """Return the parsed JSON decision from either a stub or the live LLM.

    Stub-source priority: ``AXIOM_LLM_STUB_JSON`` (tenant secret carrying
    the trace contents) wins; ``AXIOM_LLM_STUB_PATH`` (env var pointing
    at a file) is the fallback. Live LLM call only if neither is set.
    """
    stub_json, ok = _safe_secret(ax, "AXIOM_LLM_STUB_JSON")
    if ok and stub_json.strip():
        return _decide_from_stub_text(ax, stub_json, iteration, source="secret")

    stub_path = os.environ.get("AXIOM_LLM_STUB_PATH", "").strip()
    if stub_path:
        try:
            with open(stub_path, "r") as f:
                text = f.read()
        except OSError as exc:
            ax.log.error("failed to load LLM stub file", path=stub_path, error=str(exc))
            return {
                "action": "terminate",
                "terminal_answer": f"stub load failed: {exc}",
            }
        return _decide_from_stub_text(ax, text, iteration, source=f"file:{stub_path}")

    return _decide_from_anthropic(ax, input, iteration)


def _safe_secret(ax: AxiomContext, name: str) -> tuple:
    """Wrap ax.secrets.get so a missing-secret path can't raise. Returns
    (value, ok) — same contract as the SDK protocol."""
    try:
        v = ax.secrets.get(name)
    except Exception:  # noqa: BLE001
        return ("", False)
    if isinstance(v, tuple) and len(v) == 2:
        return v
    # Defensive: if a future SDK shape changes the return type, treat
    # any truthy non-empty value as success.
    if v:
        return (str(v), True)
    return ("", False)


def _decide_from_stub_text(
    ax: AxiomContext, text: str, iteration: int, source: str
) -> Dict[str, Any]:
    """Replay a recorded transcript supplied as raw JSON text. The text
    is a JSON object of the form::

        {
          "goal": "<reference goal string>",
          "responses": [
            {"action": "add_tool", "need": "...", "note": "..."},
            {"action": "add_tool", "need": "...", "note": "..."},
            ...
            {"action": "terminate", "terminal_answer": "..."}
          ]
        }

    Iteration N (1-based) takes responses[N-1]. If the transcript ends
    before the iteration arrives, the reasoner terminates with a
    corrective note — this is the same shape as the live-mode failure
    path.
    """
    try:
        trace = json.loads(text)
    except json.JSONDecodeError as exc:
        ax.log.error("LLM stub text not valid JSON", source=source, error=str(exc))
        return {"action": "terminate", "terminal_answer": f"stub parse failed: {exc}"}

    responses: List[Dict[str, Any]] = list(trace.get("responses") or [])
    idx = iteration - 1
    if idx < 0 or idx >= len(responses):
        ax.log.warn(
            "stub transcript exhausted",
            iteration=iteration,
            available=len(responses),
            source=source,
        )
        return {
            "action": "terminate",
            "terminal_answer": f"Stub transcript has no response for iteration {iteration}.",
        }

    ax.log.info("reasoner using stub response", iteration=iteration, source=source)
    return dict(responses[idx])


def _decide_from_anthropic(
    ax: AxiomContext, input: ReasonerIn, iteration: int
) -> Dict[str, Any]:
    """Call Claude. Quota/transport errors degrade to a terminate decision
    with a corrective note so the flow exits cleanly instead of leaking
    a Python exception through the trust boundary."""
    api_key, ok = ax.secrets.get("ANTHROPIC_API_KEY")
    if not ok or not api_key:
        ax.log.error("ANTHROPIC_API_KEY not configured")
        return {
            "action": "terminate",
            "terminal_answer": "ANTHROPIC_API_KEY not configured on the tenant.",
        }

    # Imported lazily so unit tests + stub mode don't require the
    # anthropic package to be installed.
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        ax.log.error("anthropic SDK not available", error=str(exc))
        return {
            "action": "terminate",
            "terminal_answer": "anthropic SDK not installed.",
        }

    prompt = _build_user_prompt(ax, input, iteration)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — collapse provider errors to a terminate
        ax.log.error("Anthropic call failed", error=str(exc), iteration=iteration)
        return {
            "action": "terminate",
            "terminal_answer": f"LLM call failed: {exc}",
        }

    text = _extract_text(msg)
    try:
        return _parse_json_block(text)
    except ValueError as exc:
        ax.log.warn("LLM produced unparseable JSON — terminating", error=str(exc))
        return {
            "action": "terminate",
            "terminal_answer": f"LLM output was not parseable JSON: {exc}",
        }


def _extract_text(msg: Any) -> str:
    """Extract the first text block from an anthropic Message. Tolerates
    SDK shape changes by falling back to repr() if structure is unexpected."""
    try:
        blocks = list(getattr(msg, "content", []) or [])
        for blk in blocks:
            text = getattr(blk, "text", None)
            if isinstance(text, str) and text.strip():
                return text
    except Exception:  # noqa: BLE001
        pass
    return str(msg)


def _parse_json_block(text: str) -> Dict[str, Any]:
    """Find and parse the first JSON object in ``text``. Tolerates the
    common case where the model wraps its answer in ```json fences."""
    text = text.strip()
    if "```" in text:
        # Strip markdown fences regardless of whether they are ``` or ```json.
        start = text.find("```")
        # Skip the opening fence + optional language tag.
        nl = text.find("\n", start)
        if nl != -1:
            inner_start = nl + 1
            end = text.find("```", inner_start)
            if end != -1:
                text = text[inner_start:end].strip()

    if not text:
        raise ValueError("empty text after fence stripping")

    # Best-effort: if there's extra prose, slice from first { to matching }.
    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last < first:
            raise ValueError("no JSON object found in text")
        text = text[first : last + 1]

    decoded = json.loads(text)
    if not isinstance(decoded, dict):
        raise ValueError("JSON root is not an object")
    return decoded


def _build_user_prompt(ax: AxiomContext, input: ReasonerIn, iteration: int) -> str:
    """Render reflection + ReasonerIn into a human-readable prompt for Claude.

    Flow topology is read live from ``ax.reflection.flow.*`` (ADR-050)
    rather than from the input message — by the time the reasoner runs
    on iteration N, the worker has already forked the execution onto a
    child graph N-1 times, so reflection reflects the post-mutation
    shape automatically."""
    lines = [
        f"GOAL: {input.goal}",
        f"ITERATION: {iteration}",
        "",
        "FLOW (current topology):",
    ]
    try:
        nodes = list(ax.reflection.flow.nodes)
        edges = list(ax.reflection.flow.edges)
    except Exception:  # noqa: BLE001 — degrade rather than fail
        nodes, edges = [], []
    if not nodes:
        lines.append("  (no nodes reported by reflection)")
    else:
        for n in nodes:
            lines.append(
                f"  - instance {n.instance_id}: {n.name} "
                f"({n.package_name}@{n.package_version}) "
                f"{n.input_message_name} → {n.output_message_name}"
            )
    if edges:
        lines.append("  edges: " + ", ".join(
            f"{e.src_instance}→{e.dst_instance}" for e in edges
        ))

    lines.append("")
    lines.append("HISTORY (tools already added this run):")
    if not input.history:
        lines.append("  (none — first iteration)")
    else:
        for rec in input.history:
            note = f" — {rec.note}" if rec.note else ""
            lines.append(
                f"  iter {rec.iteration}: added {rec.package_name}@{rec.package_version}{note}"
            )

    lines.append("")
    lines.append("Return a SINGLE JSON object per the system instructions.")
    return "\n".join(lines)


__all__ = ["llm_reasoner", "MAX_ITERATIONS", "MODEL"]
