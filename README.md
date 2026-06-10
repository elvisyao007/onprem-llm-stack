# onprem-llm-stack

**One command to run a private LLM stack. One command to prove it works.**

---

## 30-second overview

**Problem:** Enterprise teams need capable open-weight LLMs (Qwen, Gemma, …)
running on their own hardware — without sending prompts to cloud APIs, without
rewriting every client, and without spending a week on infrastructure glue.
The harder problem is proving the deployment is production-ready once it's up.

**Solution:** A single `docker compose` command starts an OpenAI-compatible
inference gateway, a multi-user chat UI, per-user API keys with audit logging,
and a built-in acceptance test that gives a pass/fail verdict — all air-gapped,
no cloud calls at runtime or at validation time.

**Key differentiators:**

1. **Data never leaves the box — including the audit logs.**
   LiteLLM gateway, Open WebUI, and a local SQLite audit database all run on
   your hardware. No Kafka, no cloud logging, no external database. Every
   prompt, every token count, every access denial stays on-prem.

2. **Multi-user auth with attributable access-control audit.**
   Each user gets a scoped API key (YAML-managed, zero database dependency).
   Model-access violations are logged with the *violating user's* identity —
   not just "unknown request denied" — because attributability of
   unauthorized attempts is the core security-audit value.

3. **Built-in acceptance test: a score, not just "it runs".**
   `make smoke-eval` runs a 15-question golden set through your chosen
   generator model, scores every answer with an independent judge model
   (different model family — non-self-evaluation), and prints a `PASS/FAIL`
   verdict. The judge runs locally; the test makes zero internet calls.
   This is the line between demo-grade and production-grade deployment.

---

## Architecture

```
Browser / API client
    │  :3000 (UI)           │  :4000 (API)
    ▼                        ▼
┌──────────────┐    ┌──────────────────────────────────┐
│  Open WebUI  │───►│  LiteLLM Gateway :4000           │
│  (chat UI)   │    │  • virtual key auth (YAML)        │
└──────────────┘    │  • model-access enforcement       │
                    │  • SQLite audit log (data/audit.db)│
                    └────────────────┬─────────────────┘
                                     │
                  dev profile        │       prod profile
                       │             │             │
                       ▼             │             ▼
               Ollama :11434         │      vLLM :8000
              (host process)         │    (Docker container)
              qwen3:32b              │    configurable model
              gemma4:31b             │
```

Full diagram and component table: [`docs/architecture.md`](docs/architecture.md)

---

## Quick start (dev profile)

**Prerequisites:** Docker + Compose v2, Ollama on the host with `qwen3:32b`
and `gemma4:31b` already pulled.

### 1 — One-time host setup (Linux only)

Ollama must listen on `0.0.0.0` so the LiteLLM container can reach it:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

### 2 — Configure and start

```bash
cp .env.example .env
$EDITOR .env                             # set LITELLM_MASTER_KEY at minimum

cp configs/virtual_keys.yaml.example configs/virtual_keys.yaml

docker compose --profile dev up -d
bash scripts/healthcheck.sh              # wait until all services healthy
```

Services:
- **Web UI:** http://localhost:3000
- **API endpoint:** http://localhost:4000/v1  (Bearer: `LITELLM_MASTER_KEY`)

### 3 — Create per-user keys

```bash
# Alice: access to qwen3-32b, $10 budget
bash scripts/create_key.sh --user alice --models qwen3-32b --budget 10 --rpm 60

# Bob: access to gemma4-31b, $5 budget
bash scripts/create_key.sh --user bob --models gemma4-31b --budget 5 --rpm 30
```

### 4 — Make an API call

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <alice-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"Hello"}],"max_tokens":512}'
```

Alice calling a model she has no access to returns HTTP 403:

```json
{"error": {"code": "model_access_denied", "message": "Model 'gemma4-31b' not allowed..."}}
```

### 5 — Review the audit log

```bash
python3 scripts/audit_report.py          # per-user table: requests, fail rate, tokens, latency
python3 scripts/audit_report.py --since 2026-06-01
```

### 6 — Run the acceptance test

```bash
# Create a key with access to both generator and judge models
bash scripts/create_key.sh \
  --user smoke-eval --models qwen3-32b,gemma4-31b --budget 5 --rpm 120

# Add the printed key to .env: EVAL_API_KEY=sk-user-smoke-eval-<key>
$EDITOR .env

make smoke-eval
```

Output:

```
========================================================================
  onprem-llm-stack — Smoke Eval
  Generator : qwen3-32b
  Judge     : gemma4-31b   <- judge != generator, non-self-evaluation
  Questions : 15    Threshold : 70% pass rate required
========================================================================
  q01   en    llm_concepts   PASS     1183  Correctly defines context window...
  q02   en    retrieval      PASS      934  RAG retrieval + grounding covered
  ...
========================================================================
  Passed    : 13 / 15  (86.7%)
  Verdict   : PASS  (threshold >= 70%)
  Report    : eval/reports/20260611T120000/
========================================================================
```

### 7 — Stop

```bash
docker compose --profile dev down
```

---

## Air-gapped deployment

Full instructions for packaging images, model weights, and configuration for
a machine with no outbound internet access: [`docs/airgapped.md`](docs/airgapped.md)

The smoke-eval acceptance test runs entirely locally — the judge is the
on-prem `gemma4-31b` model. **Even validation doesn't call the cloud.**

---

## Relationship to eval-driven-llm and eval-sanity

`onprem-llm-stack` is the **deployment carrier**. It takes the evaluation
methodology from [eval-driven-llm](https://github.com/ElvisYaoOh/eval-driven-llm)
— pinned independent judge, deterministic golden set, pass/fail threshold —
and bakes it directly into the infrastructure layer as a one-command acceptance
test.

The full methodology (multi-model fleet evaluation, evaluation-driven
development workflow, benchmark harnesses) lives in `eval-driven-llm` and
`eval-sanity`. Those libraries are the reference implementation; this repo
applies their core principle — *"a score, not just a green CI light"* — to the
on-prem deployment problem.

---

## Per-user key management reference

| Operation | Command |
|-----------|---------|
| Create key | `bash scripts/create_key.sh --user NAME --models M1,M2 --budget USD --rpm N` |
| List users | `python3 scripts/audit_report.py` |
| View raw DB | `sqlite3 data/audit.db "SELECT * FROM requests ORDER BY ts DESC LIMIT 20"` |
| Check budget | `sqlite3 data/audit.db "SELECT * FROM key_budgets"` |

Keys are stored in `configs/virtual_keys.yaml` (git-ignored). Changes take
effect within 30 seconds (cache TTL) — no container restart needed.

---

## Roadmap

This is `v0.1` — the foundation layer. Planned additions:

| Feature | Phase | Notes |
|---------|-------|-------|
| PII detection / prompt-injection guardrails | C | Content-layer protection |
| RPM enforcement per key | C | Stored in YAML; enforcement not wired yet |
| SSO / LDAP integration | C | Enterprise identity provider |
| Langfuse / Prometheus observability | D | Opt-in; air-gapped compatible builds only |
| Kubernetes Helm chart | D | Horizontal scaling, rolling upgrades |
| Multi-GPU tensor-parallel sharding | D | For >32 GB VRAM requirements |
| Automated CI / Renovate dependency updates | E | — |

Phase A and B are complete and included in this release.

---

## Third-party model and data licenses

- **Qwen3-32B**: [Qwen License Agreement](https://huggingface.co/Qwen/Qwen3-32B/blob/main/LICENSE)
  (Apache-2.0 based with usage conditions for commercial use)
- **Gemma 4**: [Gemma Terms of Use](https://ai.google.dev/gemma/terms)
  (permitted for commercial use with attribution; review terms before deployment)
- **Eval golden set** (`eval/golden_set.json`): original content authored for
  this repo, Apache-2.0 licensed same as the rest of this project.

> These licenses govern the model weights only. This repo contains no model
> weights — it is infrastructure configuration and tooling.

---

## License

Apache-2.0 © 2026 Elvis Yao. See [LICENSE](LICENSE).
