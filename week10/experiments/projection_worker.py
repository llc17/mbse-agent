"""Isolated worker for one already authorized V9 diagnostic row.

This is not an independent online entry point. The worker derives every
control path from the frozen project layout and a batch id, revalidates the
authorization and durable ledger, then atomically consumes one row slot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK10_ROOT = PROJECT_ROOT / "week10"
RESULTS_ROOT = WEEK10_ROOT / "results"
PROTOCOL_PATH = WEEK10_ROOT / "configs" / "projection_diagnostic_protocol_v1.json"
SUITE_PATH = WEEK10_ROOT / "configs" / "development_diagnostic_suite.json"
RECEIPT_ROOT = WEEK10_ROOT / "configs" / "authorization_receipts"
CONSUMED_ROOT = RESULTS_ROOT / ".v9_projection_receipts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week10.experiments.report_projection import _validate_ledger  # noqa: E402
from week10.experiments.run_projection_diagnostic import (  # noqa: E402
    _atomic_write,
    _known_totals,
    _load_json,
    _reservation,
    _stop_for_result,
    validate_online_context,
)
from week10.src.online_generation import DeepSeekNaturalLanguageProvider  # noqa: E402
from week10.src.runner import run_case  # noqa: E402


_BATCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _claim(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())


def execute_authorized_attempt(
    *, ledger_path: Path, row_index: int, batch_id: str
) -> dict[str, Any]:
    """Run one pre-reserved row after rechecking every frozen boundary."""

    if not _BATCH.fullmatch(batch_id):
        raise ValueError("WORKER_BATCH_INVALID")
    ledger_path = ledger_path.resolve()
    if (
        ledger_path.name != "ledger.json"
        or ledger_path.parent != (RESULTS_ROOT / batch_id).resolve()
    ):
        raise ValueError("WORKER_LEDGER_PATH_INVALID")
    receipt_path = RECEIPT_ROOT / f"{batch_id}.json"
    context = validate_online_context(
        protocol_path=PROTOCOL_PATH,
        suite_path=SUITE_PATH,
        protocol_sidecar_path=PROTOCOL_PATH.with_name(PROTOCOL_PATH.name + ".sha256"),
        offline_acceptance_path=PROJECT_ROOT
        / "week10/configs/offline_acceptance_v1.json",
        authorization_receipt_path=receipt_path,
    )
    if context["receipt"]["batch_id"] != batch_id:
        raise ValueError("WORKER_BATCH_INVALID")
    receipt_sha256 = context["receipt_sha256"]
    consumed_marker_path = CONSUMED_ROOT / f"{receipt_sha256}.consumed.json"
    marker = _load_json(consumed_marker_path, require_canonical=True)
    if marker != {
        "schema_version": "v9-projection-authorization-consumed-1",
        "receipt_sha256": receipt_sha256,
        "protocol_sha256": context["protocol_sha256"],
        "batch_id": batch_id,
    }:
        raise ValueError("WORKER_CONSUMED_MARKER_INVALID")
    ledger = _load_json(ledger_path, require_canonical=True)
    _validate_ledger(ledger, context["suite"], context["protocol"])
    rows = ledger["rows"]
    if type(row_index) is not int or row_index < 0 or row_index >= len(rows):
        raise ValueError("WORKER_ROW_INVALID")
    row = rows[row_index]
    if (
        row["attempted"] is not True
        or row["status"] != "INTERRUPTED"
        or row["stop_reason"] != "INTERRUPTED"
        or row["result"] is not None
    ):
        raise ValueError("WORKER_ROW_NOT_RESERVED")
    if (
        ledger.get("mode") != "online"
        or ledger.get("offline_acceptance_verified") is not True
        or ledger.get("batch_status") != "PLANNED"
        or ledger.get("budget", {}).get("budget_status") != "OK"
    ):
        raise ValueError("WORKER_LEDGER_STATE_INVALID")
    if any(
        prior["status"] != "COMPLETED"
        or prior["attempted"] is not True
        or _stop_for_result(prior) is not None
        for prior in rows[:row_index]
    ):
        raise ValueError("WORKER_PRIOR_ROW_INVALID")
    if any(
        later["attempted"]
        or later["status"] != "PLANNED"
        or later["result"] is not None
        or later["stop_reason"] != "NOT_STARTED"
        for later in rows[row_index + 1 :]
    ):
        raise ValueError("WORKER_LATER_ROW_INVALID")
    reserve_tokens, reserve_cost = _reservation(context["protocol"])
    if (
        ledger["budget"]["reserved_tokens"] < reserve_tokens
        or ledger["budget"]["reserved_cost_usd"] + 1e-12 < reserve_cost
    ):
        raise ValueError("WORKER_BUDGET_NOT_RESERVED")
    calls, known_tokens = _known_totals(ledger)
    if (
        calls > context["protocol"]["max_calls"]
        or known_tokens + ledger["budget"]["reserved_tokens"]
        > context["protocol"]["max_total_tokens"]
        or ledger["budget"]["known_cost_usd"]
        + ledger["budget"]["reserved_cost_usd"]
        > context["protocol"]["max_cost_usd"]
    ):
        raise ValueError("WORKER_BUDGET_INVALID")
    expected_schedule = context["schedule"]
    if any(
        row[name] != expected_schedule[row_index][name]
        for name in (
            "run_id",
            "case_id",
            "arm",
            "positive",
            "repetition",
            "requirement_sha256",
            "prompt_sha256",
        )
    ):
        raise ValueError("WORKER_ROW_DRIFT")
    dispatch_marker = (
        CONSUMED_ROOT / f"{receipt_sha256}.{row['run_id']}.dispatch.json"
    )
    _claim(
        dispatch_marker,
        {
            "schema_version": "v9-projection-dispatch-marker-1",
            "batch_id": batch_id,
            "run_id": row["run_id"],
            "protocol_sha256": context["protocol_sha256"],
        },
    )
    case = next(
        item
        for item in context["suite"]["cases"]
        if item["case_id"] == row["case_id"]
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    provider = DeepSeekNaturalLanguageProvider(
        api_key=api_key,
        api_base=context["protocol"]["api_base"],
        model=context["protocol"]["model"],
        temperature=context["protocol"]["temperature"],
        max_tokens=context["protocol"]["max_tokens"],
        timeout_s=context["protocol"]["http_timeout_s"],
        prompt_arm=row["arm"],
    )
    result = run_case(
        row["case_id"],
        case["requirement_text"],
        workspace_root=ledger_path.parent / f"{row['run_id']}.work",
        generation_provider=provider,
        run_id=row["run_id"],
    ).public_dict()
    result_path = ledger_path.parent / f".{row['run_id']}.result.json"
    _atomic_write(result_path, result, create=True)
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--row-index", required=True, type=int)
    parser.add_argument("--batch-id", required=True)
    args = parser.parse_args(argv)
    try:
        execute_authorized_attempt(
            ledger_path=Path(args.ledger),
            row_index=args.row_index,
            batch_id=args.batch_id,
        )
    except BaseException:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["execute_authorized_attempt"]
