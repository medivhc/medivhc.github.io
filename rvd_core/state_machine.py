"""Global task state machine and requirement statuses.

Only `Controller` calls `transition`. No worker report, test exit code or hook
event can set SATISFIED directly: SATISFIED is only reachable through
`decide_global_state`, which requires every mandatory requirement VERIFIED with
fresh, valid evidence.
"""
from __future__ import annotations

from enum import Enum


class GState(str, Enum):
    NEW = "NEW"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"
    PAUSED_INCOMPLETE = "PAUSED_INCOMPLETE"
    SATISFIED = "SATISFIED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class RStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


G = GState
ALLOWED: dict[GState, set[GState]] = {
    G.NEW: {G.READY, G.CANCELLED, G.ERROR},
    G.READY: {G.RUNNING, G.VERIFYING, G.RECOVERING, G.BLOCKED, G.PAUSED_INCOMPLETE, G.CANCELLED, G.ERROR},
    G.RUNNING: {G.VERIFYING, G.RECOVERING, G.PAUSED_INCOMPLETE, G.BLOCKED, G.CANCELLED, G.ERROR},
    G.VERIFYING: {G.SATISFIED, G.RECOVERING, G.BLOCKED, G.PAUSED_INCOMPLETE, G.CANCELLED, G.ERROR},
    G.RECOVERING: {G.RUNNING, G.VERIFYING, G.BLOCKED, G.PAUSED_INCOMPLETE, G.READY, G.CANCELLED, G.ERROR},
    G.BLOCKED: {G.READY, G.VERIFYING, G.RECOVERING, G.PAUSED_INCOMPLETE, G.CANCELLED, G.ERROR},
    G.PAUSED_INCOMPLETE: {G.READY, G.VERIFYING, G.RECOVERING, G.BLOCKED, G.CANCELLED, G.ERROR},
    # SATISFIED can be re-opened when evidence goes stale or the contract is amended.
    G.SATISFIED: {G.VERIFYING, G.RECOVERING, G.READY, G.CANCELLED},
    # CANCELLED is terminal for automation; only an explicit user `rvd resume` re-opens it.
    G.CANCELLED: {G.READY},
    G.ERROR: {G.READY, G.VERIFYING, G.RECOVERING, G.BLOCKED, G.PAUSED_INCOMPLETE, G.CANCELLED},
}

# Sources allowed to perform transitions that automation must never perform.
USER_ONLY = {(G.CANCELLED, G.READY)}


class TransitionError(RuntimeError):
    pass


def check_transition(src: GState, dst: GState, source: str) -> None:
    if src == dst:
        return
    if dst not in ALLOWED[src]:
        raise TransitionError(f"illegal transition {src.value} -> {dst.value}")
    if (src, dst) in USER_ONLY and source != "user":
        raise TransitionError(f"{src.value} -> {dst.value} requires an explicit user action")
    if dst == G.SATISFIED and source != "controller.decide":
        raise TransitionError("SATISFIED can only be set by the controller's evidence decision")


def decide_global_state(req_status: dict[str, RStatus], mandatory: set[str], *,
                        open_actionable: int, open_blocked: int, exhausted: bool) -> tuple[GState, str]:
    """Deterministic decision from requirement statuses + issue queue."""
    unmet = sorted(r for r in mandatory if req_status.get(r) != RStatus.VERIFIED)
    if not unmet:
        return G.SATISFIED, "all mandatory requirements verified with fresh evidence"
    if open_actionable > 0 and not exhausted:
        return G.RECOVERING, f"{len(unmet)} mandatory requirement(s) unmet; recovery actions available"
    if exhausted:
        return G.PAUSED_INCOMPLETE, f"{len(unmet)} unmet; budget or viable paths exhausted"
    if open_blocked > 0:
        return G.BLOCKED, f"{len(unmet)} unmet; remaining gaps need external resources or authorization"
    return G.RECOVERING, f"{len(unmet)} mandatory requirement(s) unmet"
