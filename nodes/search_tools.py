# ADR-051 (2026-05-26): SearchTools sits between the reasoner and the
# mutation emitter. The architectural rule "nodes communicate only
# through the sidecar" applies to platform-internal capabilities;
# outbound HTTP to the marketplace registry is treated the same as
# outbound HTTP to any external API (the same way intent_router calls
# Anthropic). For deterministic CI we also accept a transcript file —
# same mechanism the reasoner uses.
"""SearchTools — resolves a ToolSpec into a ranked list of marketplace candidates.

Modes (priority order):

  - **Stub mode (secret)**: tenant secret ``AXIOM_SEARCH_STUB_JSON``
    carries the transcript content. Preferred by the e2e-gate suite —
    no container-side state required.
  - **Stub mode (file)**: env var ``AXIOM_SEARCH_STUB_PATH`` points at
    a JSON file the node container can read. Convenient for
    ``axiom dev`` workflows.
  - **Live mode**: env var ``REGISTRY_URL`` set => the node queries
    ``GET /packages/search?q=...`` and materializes top N matches.
  - **Degraded mode**: nothing configured => empty list. Downstream
    AddToFlow detects zero candidates and terminates the agent with a
    corrective note instead of emitting a mutation.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from gen.messages_pb2 import (
    Candidate,
    ToolCandidates,
    ToolSpec,
)
from gen.axiom_context import AxiomContext


TOP_N = 3
HTTP_TIMEOUT_SECONDS = 5.0


def search_tools(ax: AxiomContext, input: ToolSpec) -> ToolCandidates:
    out = ToolCandidates()
    out.goal = input.goal
    out.history.extend(input.history)
    out.iteration = input.iteration
    out.need = input.need
    out.note = input.note

    stub_secret, ok = _safe_secret(ax, "AXIOM_SEARCH_STUB_JSON")
    stub_path = os.environ.get("AXIOM_SEARCH_STUB_PATH", "").strip()
    registry_url = os.environ.get("REGISTRY_URL", "").strip()

    if ok and stub_secret.strip():
        candidates = _candidates_from_stub_text(ax, stub_secret, input, source="secret")
    elif stub_path:
        candidates = _candidates_from_stub_file(ax, stub_path, input)
    elif registry_url:
        candidates = _candidates_from_registry(ax, input)
    else:
        ax.log.warn(
            "SearchTools degraded: no REGISTRY_URL, AXIOM_SEARCH_STUB_JSON, "
            "or AXIOM_SEARCH_STUB_PATH configured"
        )
        candidates = []

    for c in candidates:
        out.candidates.append(c)

    ax.log.info(
        "search candidates resolved",
        need=input.need[:80],
        n=len(candidates),
        iteration=input.iteration,
    )
    return out


def _safe_secret(ax: AxiomContext, name: str) -> tuple:
    """Wrap ax.secrets.get so a missing-secret path can't raise."""
    try:
        v = ax.secrets.get(name)
    except Exception:  # noqa: BLE001
        return ("", False)
    if isinstance(v, tuple) and len(v) == 2:
        return v
    if v:
        return (str(v), True)
    return ("", False)


def _candidates_from_stub_text(
    ax: AxiomContext, text: str, input: ToolSpec, source: str
) -> List[Candidate]:
    """Parse stub text directly and return candidates for the current iteration."""
    try:
        trace = json.loads(text)
    except json.JSONDecodeError as exc:
        ax.log.error("search stub text not valid JSON", source=source, error=str(exc))
        return []
    return _materialize_candidates(ax, trace, input, source=source)


def _candidates_from_stub_file(
    ax: AxiomContext, path: str, input: ToolSpec
) -> List[Candidate]:
    """Replay candidates from a JSON file. Same iteration key as the LLM stub."""
    try:
        with open(path, "r") as f:
            trace = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        ax.log.error("failed to load search stub", path=path, error=str(exc))
        return []
    return _materialize_candidates(ax, trace, input, source=f"file:{path}")


def _materialize_candidates(
    ax: AxiomContext, trace: Dict[str, Any], input: ToolSpec, source: str
) -> List[Candidate]:
    by_iter: Dict[str, Any] = dict(trace.get("by_iteration") or {})
    raw = by_iter.get(str(input.iteration), [])
    if not isinstance(raw, list):
        ax.log.warn(
            "search stub entry not a list",
            iteration=input.iteration,
            kind=type(raw).__name__,
            source=source,
        )
        return []

    out: List[Candidate] = []
    for entry in raw[:TOP_N]:
        if not isinstance(entry, dict):
            continue
        out.append(_candidate_from_dict(entry))
    return out


def _candidates_from_registry(ax: AxiomContext, input: ToolSpec) -> List[Candidate]:
    """Call the registry's /packages/search endpoint and return the top matches.

    Errors collapse to an empty list — the reasoner can adjust on the
    next iteration. We do NOT raise: leaking registry transport errors
    into node failure would push the demo through the retry path rather
    than the agentic-decision path.
    """
    base = os.environ["REGISTRY_URL"].rstrip("/")
    q = input.preferred_package or input.need
    url = f"{base}/packages/search?" + urllib.parse.urlencode({"q": q, "limit": TOP_N})

    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except Exception as exc:  # noqa: BLE001
        ax.log.error("registry search failed", url=url, error=str(exc))
        return []

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        ax.log.error("registry search returned non-JSON", error=str(exc))
        return []

    # Registry response shape: {"packages": [{"name": ..., "version": ...,
    # "nodes": [{"name": ..., "input_message_name": ..., ...}]}]}.
    # We materialize the first published node of each matching package.
    out: List[Candidate] = []
    for pkg in (payload.get("packages") or [])[:TOP_N]:
        nodes = pkg.get("nodes") or []
        if not nodes:
            continue
        n = nodes[0]
        out.append(
            Candidate(
                package_name=str(pkg.get("name", "")),
                package_version=str(pkg.get("version", "")),
                description=str(pkg.get("description", "")),
                input_message_name=str(n.get("input_message_name", "")),
                output_message_name=str(n.get("output_message_name", "")),
                score=float(pkg.get("score", 0.0) or 0.0),
            )
        )
    return out


def _candidate_from_dict(d: Dict[str, Any]) -> Candidate:
    c = Candidate()
    c.package_name = str(d.get("package_name") or d.get("package") or "")
    c.package_version = str(d.get("package_version") or d.get("version") or "")
    c.description = str(d.get("description") or "")
    c.input_message_name = str(d.get("input_message_name") or "")
    c.output_message_name = str(d.get("output_message_name") or "")
    try:
        c.score = float(d.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        c.score = 0.0
    return c


__all__ = ["search_tools", "TOP_N"]
