"""Dogfooding script: audit invoice-agent Phase A traces with eval-sanity v0.3.

Usage (from payloads/invoice-agent/ directory):
    python eval_trajectories.py

Loads all traces from ./traces/, evaluates each against the correct spec
(success for INV-001/002/003, flagged for INV-004/005), and prints a full
trajectory audit report. Also compares the two INV-001 runs with
detect_trajectory_regression to demonstrate baseline-vs-candidate comparison.

No models, no network. eval-sanity is zero-dependency; stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from the invoice-agent directory without installing
# eval-sanity if the eval-sanity repo is a sibling on disk.
_here = Path(__file__).parent
_eval_sanity_root = _here.parent.parent.parent / "eval-sanity"
if _eval_sanity_root.exists() and str(_eval_sanity_root) not in sys.path:
    sys.path.insert(0, str(_eval_sanity_root))

from eval_sanity import (  # noqa: E402
    OrderConstraint,
    TrajectorySpec,
    detect_trajectory_regression,
    trajectory_report,
)
from eval_sanity.trajectory import Trajectory  # noqa: E402

# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

SUCCESS_SPEC = TrajectorySpec(
    expected_tools=["extract_fields", "validate", "write_back"],
    forbidden_tools=[],
    order_constraints=[
        OrderConstraint(
            tool="write_back",
            required_predecessor="validate",
            require_pass_field="passed",
        )
    ],
    max_steps=6,
    expected_final_status="success",
)

FLAGGED_SPEC = TrajectorySpec(
    expected_tools=["extract_fields", "validate"],
    forbidden_tools=["write_back"],
    order_constraints=[],
    max_steps=5,
    expected_final_status="flagged",
)


def _spec_for_status(status: str) -> TrajectorySpec:
    return SUCCESS_SPEC if status == "success" else FLAGGED_SPEC


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------


def main() -> None:
    traces_dir = _here / "traces"
    if not traces_dir.exists():
        print(f"traces/ directory not found at {traces_dir}")
        sys.exit(1)

    trace_files = sorted(traces_dir.glob("*.json"))
    if not trace_files:
        print("No trace JSON files found.")
        sys.exit(0)

    print("=" * 70)
    print("eval-sanity v0.3 — invoice-agent trajectory audit")
    print(f"Traces directory: {traces_dir}")
    print(f"Trace files found: {len(trace_files)}")
    print("=" * 70)

    reports: dict[str, list] = {}   # invoice_id → list[TrajectoryReport]
    all_reports = []

    for tf in trace_files:
        traj = Trajectory.from_json(tf)
        status = traj.final_result.get("status", "unknown")
        spec = _spec_for_status(status)
        rep = trajectory_report(traj, spec)
        all_reports.append(rep)
        reports.setdefault(traj.task_id, []).append(rep)

        print()
        print(rep.format())

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n_pass = sum(1 for r in all_reports if r.passed)
    print(f"{'Trace file':<48}  {'Result':<6}  {'Steps':>5}  {'Viol':>4}  {'Redun':>5}")
    print("-" * 70)
    for tf, rep in zip(trace_files, all_reports):
        verdict = "PASS" if rep.passed else "FAIL"
        print(
            f"{tf.name:<48}  {verdict:<6}  "
            f"{rep.step_efficiency.total_steps:>5}  "
            f"{len(rep.order_violations):>4}  "
            f"{rep.step_efficiency.redundant_count:>5}"
        )
    print("-" * 70)
    print(f"  {n_pass}/{len(all_reports)} traces passed their spec.\n")

    # ------------------------------------------------------------------
    # Regression comparison: two INV-001 runs (baseline vs candidate)
    # ------------------------------------------------------------------
    inv001_reports = reports.get("INV-2024-001", [])
    if len(inv001_reports) >= 2:
        print("=" * 70)
        print("REGRESSION CHECK: INV-2024-001 run 1 (baseline) vs run 2 (candidate)")
        print("=" * 70)
        reg = detect_trajectory_regression(
            [inv001_reports[0]],
            [inv001_reports[1]],
        )
        print(reg.format())
        print()
        if reg.alarm:
            print("  ALARM: regression detected between the two INV-001 runs.")
        else:
            print("  No regression between the two INV-001 runs (expected: same task, same model).")

    # ------------------------------------------------------------------
    # Cross-category regression: success group vs flagged group
    # (demonstrates that flagged traces with forbidden write_back would alarm)
    # ------------------------------------------------------------------
    success_reports = [r for r in all_reports
                       if r.task_completion and r.task_completion.expected_status == "success"]
    flagged_reports = [r for r in all_reports
                       if r.task_completion and r.task_completion.expected_status == "flagged"]

    if success_reports and flagged_reports:
        print("=" * 70)
        print("INFO: success traces vs flagged traces (different specs — illustrative only)")
        print("=" * 70)
        print(f"  success-spec reports : {len(success_reports)} trace(s)")
        print(f"  flagged-spec reports : {len(flagged_reports)} trace(s)")
        print(
            f"  success completion rate: "
            f"{sum(1 for r in success_reports if r.task_completion.passed) / len(success_reports):.1%}"
        )
        print(
            f"  flagged completion rate: "
            f"{sum(1 for r in flagged_reports if r.task_completion.passed) / len(flagged_reports):.1%}"
        )

    print()
    print("eval-sanity v0.3 dogfooding complete. Zero LLM calls.")


if __name__ == "__main__":
    main()
