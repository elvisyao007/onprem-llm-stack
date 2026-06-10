#!/usr/bin/env python3
"""
smoke_eval.py — acceptance test for onprem-llm-stack.

Runs the built-in golden set through a generator model, scores every answer
with an independent judge model, and prints a pass/fail verdict table.

Design principles (from eval-driven-llm methodology):
  • Pinned judge:  the judge model is fixed and distinct from the generator.
  • Independence:  generator and judge must be different model families so the
                   generator cannot trivially self-pass its own outputs.
  • Determinism:   judge runs at temperature=0; same golden set → same verdict
                   on the same deployment.
  • Air-gapped:    zero internet calls; all traffic goes to the local LiteLLM
                   gateway, which routes to on-host Ollama (or vLLM in prod).

Usage:
    python3 scripts/smoke_eval.py [OPTIONS]

Options:
    --base-url URL       LiteLLM API base URL  (default: $EVAL_BASE_URL or http://localhost:4000/v1)
    --key KEY            API key               (default: $EVAL_API_KEY or $LITELLM_MASTER_KEY)
    --generator MODEL    Generator model       (default: $EVAL_GENERATOR or qwen3-32b)
    --judge MODEL        Judge model           (default: $EVAL_JUDGE or gemma4-31b)
    --threshold FLOAT    Pass-rate threshold   (default: $EVAL_PASS_THRESHOLD or 0.7)
    --golden-set PATH    Golden set JSON       (default: eval/golden_set.json)
    --timeout INT        Seconds per API call  (default: $EVAL_TIMEOUT or 120)

Exit codes:
    0 — verdict PASS
    1 — verdict FAIL (pass rate below threshold, or errors)
    2 — configuration / connectivity error
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).resolve().parent.parent
GOLDEN_SET  = REPO_ROOT / "eval" / "golden_set.json"
REPORTS_DIR = REPO_ROOT / "eval" / "reports"

# ── Defaults ───────────────────────────────────────────────────────────────
DEFAULT_BASE_URL   = "http://localhost:4000/v1"
DEFAULT_GENERATOR  = "qwen3-32b"
DEFAULT_JUDGE      = "gemma4-31b"
DEFAULT_THRESHOLD  = 0.70
DEFAULT_TIMEOUT    = 120

GEN_MAX_TOKENS   = 1024   # enough for Qwen3 thinking phase + substantive answer
JUDGE_MAX_TOKENS = 300    # judge outputs only a short JSON object

# ── API helper ─────────────────────────────────────────────────────────────

def _chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _content(resp: dict) -> str:
    try:
        return resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        return ""


# ── Judge helpers ──────────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are a strict, objective evaluator for an LLM acceptance test. "
    "Output ONLY a single JSON object — no markdown fences, no preamble, no extra text."
)


def _judge_prompt(question: str, expected_points: list, answer: str) -> str:
    points_block = "\n".join(f"  - {p}" for p in expected_points)
    return (
        f"QUESTION:\n{question}\n\n"
        f"EXPECTED KEY POINTS (the answer must substantially address all of these):\n"
        f"{points_block}\n\n"
        f"ANSWER TO EVALUATE:\n{answer or '(empty — no answer was generated)'}\n\n"
        'Output ONLY this JSON (replace SCORE with 0 or 1, no other text):\n'
        '{"score": SCORE, "reason": "one sentence in English"}\n\n'
        "score=1  →  answer substantially covers the expected key points\n"
        "score=0  →  answer clearly misses one or more key points"
    )


def _parse_judge(text: str) -> tuple:
    """Return (score: int, reason: str). Robust against markdown fences."""
    # Strip ```json ... ``` wrapping if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()

    try:
        obj = json.loads(text)
        return int(obj.get("score", 0) == 1), str(obj.get("reason", ""))
    except (json.JSONDecodeError, ValueError):
        pass

    # Extract first {...} block containing "score"
    m = re.search(r'\{[^{}]*"score"\s*:\s*([01])[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return int(obj.get("score", 0) == 1), str(obj.get("reason", "extracted"))
        except (json.JSONDecodeError, ValueError):
            pass

    # Final fallback
    if re.search(r'"score"\s*:\s*1', text):
        return 1, "score=1 extracted from malformed response"
    return 0, f"judge parse failed — raw: {text[:100]}"


# ── Core eval loop ─────────────────────────────────────────────────────────

def run_eval(
    base_url: str,
    api_key: str,
    generator: str,
    judge: str,
    golden_set_path: Path,
    threshold: float,
    timeout: int,
) -> dict:
    with open(golden_set_path, encoding="utf-8") as fh:
        golden = json.load(fh)
    questions = golden["questions"]

    W = 72
    print()
    print("=" * W)
    print(f"  onprem-llm-stack — Smoke Eval")
    print(f"  Generator : {generator}")
    print(f"  Judge     : {judge}   <- judge != generator, non-self-evaluation")
    print(f"  Questions : {len(questions)}    Threshold : {threshold * 100:.0f}% pass rate required")
    print("=" * W)
    print()
    hdr = f"  {'ID':<4}  {'Lang':<4}  {'Category':<15}  {'Result':<6}  {'Gen ms':>7}  Reason"
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)

    results = []

    for q in questions:
        qid      = q["id"]
        lang     = q.get("lang", "?")
        cat      = q.get("category", "?")
        question = q["question"]
        expected = q["expected_points"]

        # ── Generate ────────────────────────────────────────
        t0 = time.monotonic()
        gen_answer = ""
        gen_error  = None
        try:
            gen_resp   = _chat(base_url, api_key, generator,
                               [{"role": "user", "content": question}],
                               temperature=0.1, max_tokens=GEN_MAX_TOKENS,
                               timeout=timeout)
            gen_answer = _content(gen_resp)
        except Exception as exc:
            gen_error = str(exc)
        gen_ms = int((time.monotonic() - t0) * 1000)

        if gen_error and not gen_answer:
            row = {"id": qid, "lang": lang, "category": cat,
                   "question": question, "expected_points": expected,
                   "generator_answer": "", "gen_latency_ms": gen_ms,
                   "judge_score": 0, "judge_reason": f"generation failed: {gen_error}",
                   "judge_latency_ms": 0, "status": "error"}
            results.append(row)
            print(f"  {qid:<4}  {lang:<4}  {cat:<15}  {'ERROR':<6}  {gen_ms:>7}  generation error")
            continue

        # ── Judge ────────────────────────────────────────────
        t1 = time.monotonic()
        judge_score  = 0
        judge_reason = "judge call failed"
        try:
            judge_resp   = _chat(base_url, api_key, judge,
                                 [{"role": "system", "content": _JUDGE_SYSTEM},
                                  {"role": "user",   "content": _judge_prompt(question, expected, gen_answer)}],
                                 temperature=0.0, max_tokens=JUDGE_MAX_TOKENS,
                                 timeout=timeout)
            judge_raw    = _content(judge_resp)
            judge_score, judge_reason = _parse_judge(judge_raw)
        except Exception as exc:
            judge_reason = f"judge error: {exc}"
        judge_ms = int((time.monotonic() - t1) * 1000)

        status = "pass" if judge_score == 1 else "fail"
        label  = "PASS" if judge_score == 1 else "FAIL"
        short  = (judge_reason[:52] + "…") if len(judge_reason) > 53 else judge_reason
        print(f"  {qid:<4}  {lang:<4}  {cat:<15}  {label:<6}  {gen_ms:>7}  {short}")

        results.append({
            "id": qid, "lang": lang, "category": cat,
            "question": question, "expected_points": expected,
            "generator_answer": gen_answer, "gen_latency_ms": gen_ms,
            "judge_score": judge_score, "judge_reason": judge_reason,
            "judge_latency_ms": judge_ms, "status": status,
        })

    # ── Summary ─────────────────────────────────────────────
    n_total  = len(results)
    n_pass   = sum(1 for r in results if r["status"] == "pass")
    n_err    = sum(1 for r in results if r["status"] == "error")
    rate     = n_pass / n_total if n_total else 0.0
    verdict  = "PASS" if rate >= threshold else "FAIL"
    avg_gen  = (
        sum(r["gen_latency_ms"] for r in results if r["status"] != "error")
        / max(1, n_total - n_err)
    )

    ts_utc  = datetime.now(timezone.utc)
    ts_str  = ts_utc.strftime("%Y%m%dT%H%M%S")
    rep_dir = REPORTS_DIR / ts_str
    rep_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "timestamp":           ts_utc.isoformat(),
        "generator_model":     generator,
        "judge_model":         judge,
        "judge_independent":   generator != judge,
        "base_url":            base_url,
        "golden_set":          str(golden_set_path),
        "total_questions":     n_total,
        "passed":              n_pass,
        "failed":              n_total - n_pass - n_err,
        "errors":              n_err,
        "pass_rate":           round(rate, 4),
        "pass_threshold":      threshold,
        "verdict":             verdict,
        "avg_gen_latency_ms":  round(avg_gen, 1),
        "report_dir":          str(rep_dir),
    }
    (rep_dir / "results.json").write_text(
        json.dumps(results,  ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rep_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(sep)
    print(f"  Passed    : {n_pass} / {n_total}  ({rate * 100:.1f}%)")
    if n_err:
        print(f"  Errors    : {n_err}  (counted as failures)")
    v_sym = "PASS" if verdict == "PASS" else "FAIL"
    print(f"  Verdict   : {v_sym}  (threshold >= {threshold * 100:.0f}%)")
    print(f"  Avg gen   : {avg_gen:.0f} ms/question")
    print(f"  Judge     : {judge}  (independent, non-self-evaluation)")
    print(f"  Report    : {rep_dir}/")
    print("=" * W)
    print()

    return summary


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-eval acceptance test for onprem-llm-stack")
    ap.add_argument("--base-url",   default=os.environ.get("EVAL_BASE_URL",        DEFAULT_BASE_URL))
    ap.add_argument("--key",        default=os.environ.get("EVAL_API_KEY")
                                         or os.environ.get("LITELLM_MASTER_KEY"))
    ap.add_argument("--generator",  default=os.environ.get("EVAL_GENERATOR",       DEFAULT_GENERATOR))
    ap.add_argument("--judge",      default=os.environ.get("EVAL_JUDGE",           DEFAULT_JUDGE))
    ap.add_argument("--threshold",  type=float,
                                    default=float(os.environ.get("EVAL_PASS_THRESHOLD", DEFAULT_THRESHOLD)))
    ap.add_argument("--golden-set", default=str(GOLDEN_SET))
    ap.add_argument("--timeout",    type=int,
                                    default=int(os.environ.get("EVAL_TIMEOUT",     DEFAULT_TIMEOUT)))
    args = ap.parse_args()

    if not args.key:
        print("ERROR: no API key. Set EVAL_API_KEY (or LITELLM_MASTER_KEY) in .env, or pass --key.",
              file=sys.stderr)
        return 2

    if args.generator == args.judge:
        print(f"WARNING: --generator and --judge are the same ({args.generator}).",
              file=sys.stderr)
        print("         Non-self-evaluation principle is violated.", file=sys.stderr)

    try:
        summary = run_eval(
            base_url=args.base_url,
            api_key=args.key,
            generator=args.generator,
            judge=args.judge,
            golden_set_path=Path(args.golden_set),
            threshold=args.threshold,
            timeout=args.timeout,
        )
        return 0 if summary["verdict"] == "PASS" else 1

    except urllib.error.URLError as exc:
        print(f"\nERROR: cannot reach {args.base_url}", file=sys.stderr)
        print(f"  Is the stack running?  docker compose --profile dev up -d", file=sys.stderr)
        print(f"  Details: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
