"""
Minimal function-calling agent for Japanese invoice (請求書) processing.

Three tools (no agent framework):
  extract_fields(pdf_path)  → pdfplumber text extraction; LLM parses fields
  validate(fields...)       → arithmetic consistency check (税/合計)
  write_back(fields...)     → persist structured JSON to outputs/

Trace format (machine-readable for Phase B eval):
  traces/<invoice_id>_<timestamp>.json

Usage:
  python3 agent.py data/INV-2024-001.pdf
  python3 agent.py --all          # run all 5 invoices
  python3 agent.py --all --model gemma4-31b
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
from openai import OpenAI

# ── Configuration ─────────────────────────────────────────────────────────────
HERE = Path(__file__).parent
OUTPUTS_DIR = HERE / "outputs"
TRACES_DIR = HERE / "traces"
DATA_DIR = HERE / "data"

LITELLM_BASE_URL = os.environ.get("LITELLM_API_BASE", "http://localhost:4000")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-local-dev-change-me")
DEFAULT_MODEL = os.environ.get("INVOICE_AGENT_MODEL", "qwen3-32b")

OUTPUTS_DIR.mkdir(exist_ok=True)
TRACES_DIR.mkdir(exist_ok=True)

# ── Tool definitions ──────────────────────────────────────────────────────────
_LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "quantity": {"type": "integer"},
        "unit_price": {"type": "integer"},
        "amount": {"type": "integer"},
    },
    "required": ["description", "quantity", "unit_price", "amount"],
}

_FIELDS_PROPERTIES = {
    "invoice_number": {"type": "string"},
    "issue_date": {"type": "string", "description": "発行日 YYYY-MM-DD"},
    "due_date": {"type": "string", "description": "支払期限 YYYY-MM-DD"},
    "issuer_name": {"type": "string", "description": "請求元会社名"},
    "issuer_address": {"type": "string"},
    "issuer_registration_number": {"type": "string", "description": "適格請求書発行事業者登録番号 (T始まり)"},
    "recipient_name": {"type": "string", "description": "請求先会社名"},
    "recipient_address": {"type": "string"},
    "line_items": {
        "type": "array",
        "description": "明細行",
        "items": _LINE_ITEM_SCHEMA,
    },
    "subtotal": {"type": "integer", "description": "小計 (円)"},
    "consumption_tax": {"type": "integer", "description": "消費税額 (円)"},
    "total": {"type": "integer", "description": "合計金額 (円)"},
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "extract_fields",
            "description": (
                "PDFファイルから全テキストを抽出します。"
                "返されたテキストを解析して請求書フィールドを特定し、"
                "次に validate を呼び出してください。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pdf_path": {"type": "string", "description": "PDFファイルのパス"},
                },
                "required": ["pdf_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate",
            "description": (
                "請求書フィールドの数値整合性を検証します。"
                "消費税 = 小計 × 10%、合計 = 小計 + 消費税 を確認します。"
                "検証に合格した場合のみ write_back を呼び出してください。"
                "失敗した場合は write_back を呼ばずに問題を報告してください。"
            ),
            "parameters": {
                "type": "object",
                "properties": _FIELDS_PROPERTIES,
                "required": [
                    "invoice_number", "subtotal", "consumption_tax", "total", "line_items"
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_back",
            "description": (
                "検証合格済みの請求書フィールドを outputs/ にJSONで保存します。"
                "必ず validate の合格後にのみ呼び出してください。"
            ),
            "parameters": {
                "type": "object",
                "properties": _FIELDS_PROPERTIES,
                "required": [
                    "invoice_number", "subtotal", "consumption_tax", "total"
                ],
            },
        },
    },
]

SYSTEM_PROMPT = """\
あなたは日本語の請求書（請求書）を処理するAIアシスタントです。
次の手順で処理してください：

1. extract_fields を呼び出してPDFからテキストを抽出する
2. 抽出テキストから全フィールドを解析し、validate を呼び出す
3. validate が passed=true を返した場合のみ write_back を呼び出す
4. validate が passed=false を返した場合は write_back を呼ばず、問題を報告する

数値は全て整数（円単位）で扱ってください。¥や,は取り除いて解析してください。
"""


# ── Tool implementations ──────────────────────────────────────────────────────
def _tool_extract_fields(pdf_path: str) -> dict:
    path = Path(pdf_path)
    if not path.exists():
        # try relative to DATA_DIR
        path = DATA_DIR / pdf_path
    if not path.exists():
        return {"error": f"File not found: {pdf_path}"}
    with pdfplumber.open(path) as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]
    full_text = "\n\n".join(pages_text)
    return {
        "text": full_text,
        "pages": len(pages_text),
        "char_count": len(full_text),
    }


def _tool_validate(fields: dict) -> dict:
    issues = []
    subtotal = fields.get("subtotal", 0)
    consumption_tax = fields.get("consumption_tax", 0)
    total = fields.get("total", 0)

    expected_tax = round(subtotal * 0.1)
    expected_total = subtotal + consumption_tax

    if consumption_tax != expected_tax:
        issues.append(
            f"消費税の金額が不正: 記載値={consumption_tax}円、"
            f"正しい値={expected_tax}円 (小計{subtotal}円の10%)"
        )
    if total != expected_total:
        issues.append(
            f"合計金額が不正: 記載値={total}円、"
            f"正しい値={expected_total}円 (小計{subtotal}円 + 消費税{consumption_tax}円)"
        )

    # line-item sanity: each amount should equal qty × unit_price
    for li in fields.get("line_items", []):
        expected_amount = li.get("quantity", 0) * li.get("unit_price", 0)
        if li.get("amount") != expected_amount:
            issues.append(
                f"明細金額不整合: '{li.get('description')}' "
                f"記載={li.get('amount')}円 ≠ {li['quantity']}×{li['unit_price']}={expected_amount}円"
            )

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "details": {
            "subtotal": subtotal,
            "consumption_tax_stated": consumption_tax,
            "consumption_tax_expected": expected_tax,
            "total_stated": total,
            "total_expected": expected_total,
        },
    }


def _tool_write_back(fields: dict) -> dict:
    invoice_number = fields.get("invoice_number", "UNKNOWN")
    output_path = OUTPUTS_DIR / f"{invoice_number}.json"
    payload = {
        **fields,
        "_written_at": datetime.now(timezone.utc).isoformat(),
        "_agent_version": "invoice-agent/v1",
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"written": True, "output_path": str(output_path)}


TOOL_DISPATCH = {
    "extract_fields": lambda args: _tool_extract_fields(**args),
    "validate": lambda args: _tool_validate(args),
    "write_back": lambda args: _tool_write_back(args),
}


# ── Agent loop ────────────────────────────────────────────────────────────────
def run_agent(pdf_path: str, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)
    invoice_id = Path(pdf_path).stem

    trace = {
        "trace_id": str(uuid.uuid4()),
        "invoice_path": str(pdf_path),
        "invoice_id": invoice_id,
        "model": model,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
        "final_result": None,
        "total_steps": 0,
        "llm_calls": 0,
        "total_latency_ms": 0,
        "completed_at": None,
    }

    run_start = time.monotonic()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"次の請求書PDFを処理してください: {pdf_path}"},
    ]

    # Guard: track whether validate passed so write_back can be blocked if needed
    validate_passed = None

    try:
        while True:
            llm_start = time.monotonic()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                extra_body={"think": False},
                temperature=0,
            )
            trace["llm_calls"] += 1
            llm_latency_ms = int((time.monotonic() - llm_start) * 1000)

            choice = response.choices[0]
            msg = choice.message

            # Append assistant message (convert to dict for json serialisation)
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (msg.tool_calls or [])
                ] or None,
            })

            if choice.finish_reason == "stop" or not msg.tool_calls:
                trace["final_result"] = {
                    "status": (
                        "success" if any(
                            s["tool_called"] == "write_back" and s["tool_result"].get("written")
                            for s in trace["steps"]
                        )
                        else "flagged" if validate_passed is False
                        else "completed_no_write"
                    ),
                    "agent_message": msg.content or "",
                }
                break

            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    args = {}
                    print(f"  [WARN] Failed to parse tool args: {e}", file=sys.stderr)

                # Defense: block write_back if validate has not passed
                if name == "write_back" and validate_passed is not True:
                    result = {
                        "written": False,
                        "error": (
                            "write_back blocked: validate must return passed=true first. "
                            f"validate_passed={validate_passed}"
                        ),
                    }
                else:
                    tool_start = time.monotonic()
                    dispatch = TOOL_DISPATCH.get(name)
                    if dispatch is None:
                        result = {"error": f"Unknown tool: {name}"}
                    else:
                        result = dispatch(args)
                    tool_latency_ms = int((time.monotonic() - tool_start) * 1000)

                    if name == "validate":
                        validate_passed = result.get("passed", False)

                step = {
                    "step_n": len(trace["steps"]) + 1,
                    "tool_called": name,
                    "tool_args": _redact_text(args),
                    "tool_result": result,
                    "latency_ms": tool_latency_ms if name != "write_back" or validate_passed is True else 0,
                    "llm_latency_ms": llm_latency_ms,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                trace["steps"].append(step)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

                llm_latency_ms = 0  # reset; only charge to first call in this round

    except Exception as e:
        trace["final_result"] = {"status": "error", "error": str(e)}
        raise
    finally:
        elapsed = time.monotonic() - run_start
        trace["total_latency_ms"] = int(elapsed * 1000)
        trace["total_steps"] = len(trace["steps"])
        trace["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_trace(trace)

    return trace


def _redact_text(args: dict) -> dict:
    """Replace large text blobs in trace with a length summary to keep traces readable."""
    result = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 300:
            result[k] = f"<text {len(v)} chars>"
        else:
            result[k] = v
    return result


def _save_trace(trace: dict) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    fname = f"{trace['invoice_id']}_{ts}.json"
    path = TRACES_DIR / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Invoice agent — extract / validate / write-back")
    parser.add_argument("pdf", nargs="?", help="Path to a single invoice PDF")
    parser.add_argument("--all", action="store_true", help="Process all 5 PDFs in data/")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if args.all:
        pdfs = sorted(DATA_DIR.glob("INV-*.pdf"))
        if not pdfs:
            print(f"No INV-*.pdf found in {DATA_DIR}", file=sys.stderr)
            sys.exit(1)
    elif args.pdf:
        pdfs = [Path(args.pdf)]
    else:
        parser.print_help()
        sys.exit(1)

    print(f"Model: {args.model}  |  Gateway: {LITELLM_BASE_URL}\n")

    results = []
    for pdf in pdfs:
        print(f"Processing: {pdf.name}")
        trace = run_agent(str(pdf), model=args.model)
        status = trace["final_result"]["status"]
        steps = trace["total_steps"]
        ms = trace["total_latency_ms"]
        print(f"  → status={status}  steps={steps}  latency={ms}ms")
        if status == "flagged":
            for s in trace["steps"]:
                if s["tool_called"] == "validate" and not s["tool_result"].get("passed"):
                    for issue in s["tool_result"].get("issues", []):
                        print(f"     ⚠  {issue}")
        results.append({"invoice": pdf.name, "status": status, "steps": steps, "latency_ms": ms})
        print()

    print("=" * 60)
    print(f"{'Invoice':<20} {'Status':<20} {'Steps':>5} {'Latency(ms)':>12}")
    print("-" * 60)
    for r in results:
        print(f"{r['invoice']:<20} {r['status']:<20} {r['steps']:>5} {r['latency_ms']:>12}")


if __name__ == "__main__":
    main()
