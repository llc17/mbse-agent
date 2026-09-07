"""Shared fail-closed authorization checks for any paid online execution."""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.json"


def assert_online_allowed(*, allow_online: bool, run_kind: str) -> dict:
    """Return the valid protocol or refuse before any provider is constructed."""

    if not allow_online:
        raise PermissionError(
            "pipeline execution may call a paid API; explicit online approval is required"
        )
    if run_kind not in {"smoke", "pilot", "formal"}:
        raise ValueError(f"unsupported run_kind: {run_kind}")
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PermissionError(f"cannot verify online execution protocol: {exc}") from exc
    if not isinstance(protocol, dict):
        raise PermissionError("online execution protocol root is not an object")

    from week9.experiments.freeze_protocol import validate_protocol

    protocol_errors = validate_protocol(protocol)
    if protocol_errors:
        raise PermissionError(
            "the experiment protocol is not valid/frozen: "
            + "; ".join(protocol_errors)
        )
    policy = protocol.get("online_execution_policy", {})
    if not policy.get("current_milestone_allows_online_calls"):
        raise PermissionError("the frozen protocol disables online calls for this milestone")
    if not policy.get("user_confirmation_id"):
        raise PermissionError("the frozen protocol has no user confirmation record")
    if not policy.get("process_tree_cleanup_ready"):
        raise PermissionError("process-tree cleanup is not certified for online execution")
    if run_kind == "formal" and not protocol.get("formal_online_runs_allowed"):
        raise PermissionError("formal online runs are not enabled in the frozen protocol")
    return protocol


__all__ = ["PROTOCOL_PATH", "assert_online_allowed"]
