# Invoice Agent — 請求書処理 Payload

Minimal function-calling agent for Japanese invoice (請求書) extraction,
validation, and write-back. Designed as an **evaluation target** for Phase B
trajectory assessment (`eval-sanity v0.3`), not as a maintained product.

## Design principles

- **No agent framework.** Pure OpenAI function-calling loop over LiteLLM.
- **Three tools, no more.** `extract_fields` → `validate` → `write_back`.
- **Synthetic, clean PDFs.** Text-layer PDFs only (pdfplumber, not OCR).
  Noisy/scanned document robustness is deferred to v2.
- **Machine-readable traces.** Every run produces a structured JSON trace
  consumable by Phase B deterministic assertions.

See `DECISIONS.md` (top-level) for ADR-0008 / ADR-0009 / ADR-0010.

## Directory layout

```
payloads/invoice-agent/
├── agent.py                  # The agent (single file, no framework)
├── data/
│   ├── generate_invoices.py  # PDF + ground-truth generator
│   ├── SOURCES.md            # Synthetic data declaration
│   ├── INV-2024-001.pdf      # 5 synthetic invoices (3 valid, 2 invalid)
│   ├── ...
│   └── ground_truth/
│       └── INV-2024-001.json # Correct field values + inconsistency notes
├── outputs/                  # write_back output (gitignored)
└── traces/                   # Structured run traces (committed)
    └── INV-2024-001_<ts>.json
```

## Three tools

| Tool | Inputs | What Python does | What LLM does |
|------|--------|-----------------|---------------|
| `extract_fields(pdf_path)` | PDF path | pdfplumber text extraction | Parse raw text into structured fields |
| `validate(fields...)` | All invoice fields | Check 消費税=小計×10%, 合計=小計+消費税 | Decide whether to proceed or flag |
| `write_back(fields...)` | All invoice fields | Write JSON to `outputs/` | Call only after validate passes |

`write_back` is also guarded in Python: it is blocked if `validate` has not
returned `passed=true` in the current run (defence-in-depth against prompt
injection or hallucinated tool ordering).

## Trace format

Each run produces `traces/<invoice_id>_<timestamp>.json`:

```json
{
  "trace_id": "uuid",
  "invoice_path": "data/INV-2024-001.pdf",
  "invoice_id": "INV-2024-001",
  "model": "qwen3-32b",
  "started_at": "2026-06-12T13:45:00Z",
  "steps": [
    {
      "step_n": 1,
      "tool_called": "extract_fields",
      "tool_args": {"pdf_path": "data/INV-2024-001.pdf"},
      "tool_result": {"text": "<text 843 chars>", "pages": 1, "char_count": 843},
      "latency_ms": 11,
      "llm_latency_ms": 28400,
      "timestamp": "2026-06-12T13:45:29Z"
    },
    {
      "step_n": 2,
      "tool_called": "validate",
      "tool_args": {"invoice_number": "INV-2024-001", ...},
      "tool_result": {"passed": true, "issues": []},
      "latency_ms": 0,
      "llm_latency_ms": 3200,
      "timestamp": "2026-06-12T13:45:32Z"
    },
    {
      "step_n": 3,
      "tool_called": "write_back",
      "tool_args": {"invoice_number": "INV-2024-001", ...},
      "tool_result": {"written": true, "output_path": "outputs/INV-2024-001.json"},
      "latency_ms": 0,
      "llm_latency_ms": 0,
      "timestamp": "2026-06-12T13:45:32Z"
    }
  ],
  "final_result": {
    "status": "success",
    "agent_message": "..."
  },
  "total_steps": 3,
  "llm_calls": 3,
  "total_latency_ms": 32927,
  "completed_at": "2026-06-12T13:45:33Z"
}
```

`final_result.status` values:
- `success` — validate passed, write_back completed
- `flagged` — validate failed, write_back was NOT called
- `error` — unexpected exception

### Phase B assertion hooks

For `eval-sanity v0.3`, deterministic assertions can check:

```python
# valid invoice
assert trace["final_result"]["status"] == "success"
assert any(s["tool_called"] == "write_back" for s in trace["steps"])

# invalid invoice
assert trace["final_result"]["status"] == "flagged"
assert not any(s["tool_called"] == "write_back" for s in trace["steps"])
validate_step = next(s for s in trace["steps"] if s["tool_called"] == "validate")
assert not validate_step["tool_result"]["passed"]
assert len(validate_step["tool_result"]["issues"]) > 0
```

## Running

### Prerequisites

```bash
# Python packages (system-wide or venv)
pip install reportlab pdfplumber openai

# Stack must be running
docker compose --profile dev up -d
```

### Generate PDFs (first time only)

```bash
cd payloads/invoice-agent/data
python3 generate_invoices.py
```

### Run agent

```bash
cd payloads/invoice-agent

# Single invoice
LITELLM_API_KEY=sk-local-dev-change-me python3 agent.py data/INV-2024-001.pdf

# All 5 invoices
LITELLM_API_KEY=sk-local-dev-change-me python3 agent.py --all

# Different model
LITELLM_API_KEY=sk-local-dev-change-me python3 agent.py --all --model gemma4-31b
```

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LITELLM_API_KEY` | `sk-local-dev-change-me` | Key for the LiteLLM gateway |
| `LITELLM_API_BASE` | `http://localhost:4000` | Gateway URL |
| `INVOICE_AGENT_MODEL` | `qwen3-32b` | Model alias in LiteLLM |

### Acceptance results (baseline run, 2026-06-12)

| Invoice | Expected | Agent status | Validation issues captured |
|---------|----------|--------------|---------------------------|
| INV-2024-001 | valid | success ✓ | — |
| INV-2024-002 | valid | success ✓ | — |
| INV-2024-003 | valid | success ✓ | — |
| INV-2024-004 | invalid (税) | flagged ✓ | 消費税 30000 ≠ 25000 |
| INV-2024-005 | invalid (合計) | flagged ✓ | 合計 650000 ≠ 660000 |

**Field extraction accuracy (3 valid invoices × 9 key fields): 27/27 = 100%**
