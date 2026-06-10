# Architecture Decision Records

This file tracks significant architectural decisions for `onprem-llm-stack`.
Each record explains *why* a decision was made, not just *what* was decided,
so future contributors can judge whether the rationale still holds.

---

## ADR-0001: dev/prod dual-profile split (Ollama vs vLLM)

**Status:** Accepted  
**Date:** 2026-06-10  
**Deciders:** Elvis Yao

### Context

The stack must serve two distinct operating contexts:

1. **Local development** — a single engineer iterating on prompts or integrations
   on the same machine that hosts the model weights. The machine already runs Ollama
   with `qwen3:32b` (20 GB) and `gemma4:31b` (19 GB) loaded. Spinning up a second
   inference container would double the VRAM footprint (up to 78 GB), exceeding the
   RTX 5090's 32 GB.

2. **Production / staging** — a dedicated server where throughput matters. Ollama's
   single-stream serving model is a bottleneck under concurrent load; vLLM's
   continuous-batching engine is 3-5× more efficient at multi-user workloads.

The question was: should we choose one backend for both contexts, or support both?

### Decision

Implement two Docker Compose profiles:

- **`dev`**: LiteLLM proxies to Ollama running directly on the host
  (`host.docker.internal:11434`). No inference container is started.
- **`prod`**: A vLLM container is added; LiteLLM routes to `vllm:8000` internally.

Both profiles share the same LiteLLM and Open WebUI services — only the inference
backend changes. The same `litellm_config.yaml` carries both route definitions
(prod routes are commented out by default).

### Rationale

- **Choosing only Ollama** would require spinning it up as a container in prod,
  losing continuous batching and accepting lower throughput — an unacceptable
  trade-off for a multi-user enterprise deployment.
- **Choosing only vLLM** would require every developer to download and manage model
  weights separately from their host Ollama installation, doubling storage and VRAM
  use during development.
- **The dual-profile approach** lets each context use the right tool. The operational
  cost (one extra `--profile` flag) is negligible compared to the VRAM and
  developer-experience savings.

### Consequences

**Positive:**
- Zero additional VRAM consumed during dev; models already loaded by host Ollama.
- Production uses continuous batching for high throughput.
- The gateway (LiteLLM) and UI (Open WebUI) are identical in both profiles —
  no environment-specific surprises.

**Negative / trade-offs:**
- Two backends to keep up-to-date and understand.
- Dev and prod model versions can drift if not synchronised (mitigated by always
  registering both sets of model aliases in `litellm_config.yaml`).
- `host.docker.internal:host-gateway` is a Linux-only workaround; macOS/Windows
  users would not need it (host.docker.internal is resolved automatically), but the
  extra_hosts entry is harmless on those platforms.

---

## ADR-0002: LiteLLM as the API gateway layer

**Status:** Accepted  
**Date:** 2026-06-10  
**Deciders:** Elvis Yao

### Context

The stack needs an intermediary between clients (Open WebUI, curl, SDK callers)
and the inference backends. The gateway must handle:

- **Authentication** — a single entry point that enforces a master key, with a
  path to per-user virtual keys (Phase B).
- **Model routing** — mapping logical names (e.g. `qwen3-32b`) to backend-specific
  identifiers (`ollama/qwen3:32b` or `openai/model-id` for vLLM).
- **Provider abstraction** — the inference backend (Ollama, vLLM, or any future
  OpenAI-compatible service) should be swappable without changing clients.
- **Future budget enforcement** — per-user token budgets and audit callbacks are
  in scope for Phase B.

Alternatives considered: direct Ollama exposure (no gateway), Nginx with simple
auth, a custom FastAPI proxy.

### Decision

Use **LiteLLM Proxy** (`ghcr.io/berriai/litellm`) as the gateway.

### Rationale

- **Auth + budget in one place:** LiteLLM provides master key auth, virtual key
  management, and per-key spend limits out of the box — no custom code required
  for Phase B.
- **Multi-backend routing via config:** Switching from Ollama to vLLM (or adding
  a remote API for fallback) is a one-line config change, not a code change.
- **OpenAI-compatible API surface:** Clients already written against OpenAI's API
  require zero changes — they point `OPENAI_BASE_URL` at LiteLLM and continue
  using the same SDK calls.
- **Audit callbacks:** LiteLLM supports pluggable callbacks (Langfuse, S3, custom
  webhooks) for usage logging — the hook point is already in place for Phase B.
- **Mature, actively maintained:** LiteLLM is the de-facto standard for this
  category. The alternative (a custom FastAPI proxy) would require building and
  maintaining the same feature set ourselves.

### Consequences

**Positive:**
- Single point for auth, routing, rate-limiting, and future spend control.
- Inference engine is fully decoupled from clients.
- Phase B virtual keys require only a `virtual_keys` config addition — no
  architectural change.

**Negative / trade-offs:**
- LiteLLM is a non-trivial dependency with its own release cadence; image tags
  must be updated deliberately.
- Without a database (Phase B), virtual key state is lost on container restart —
  acceptable for Phase A (master key only).
- LiteLLM can log prompts/completions; `redact_user_api_key_info: true` and
  explicit no-callback config mitigate this in Phase A.

---

## ADR-0003: All image tags pinned to exact versions

**Status:** Accepted  
**Date:** 2026-06-10  
**Deciders:** Elvis Yao

### Context

Enterprise on-prem deployments frequently operate in **air-gapped environments**:
the production server has no outbound internet access after initial provisioning.
An air-gapped mirror (private registry, offline tarball) must contain a precise,
enumerable set of images. Mutable tags (`latest`, `main`, branch names) break this:

- A `docker pull latest` today and tomorrow may fetch different layers.
- A mirror built against `latest` on Monday will diverge from a fresh pull on
  Friday — reproducibility is lost.
- In an incident, you cannot know which exact code is running without checking
  the image digest, which defeats the purpose of the tag.

### Decision

All images in `docker-compose.yml` are pinned to an explicit, immutable version tag:

```yaml
litellm:   ghcr.io/berriai/litellm:v1.88.1
open-webui: ghcr.io/open-webui/open-webui:v0.9.6
vllm:       vllm/vllm-openai:v0.22.1
```

Updating a dependency requires a deliberate `docker-compose.yml` edit and a
corresponding commit — not an accidental `docker pull`.

### Rationale

- **Air-gapped reproducibility is a hard requirement, not an optimisation.**
  If we cannot enumerate exactly which images to mirror, we cannot deploy offline.
- **Pinned tags are a forcing function for deliberate upgrades.** Security patches
  and breaking changes are reviewed before they enter the stack, not absorbed silently.
- **Incident response is faster.** `git log docker-compose.yml` shows exactly when
  each component version changed and why.

### Consequences

**Positive:**
- Mirror set is enumerable; air-gapped provisioning is deterministic.
- Any machine that ran the stack 6 months ago can reproduce the exact environment today.
- Diff between prod and staging is always visible in git.

**Negative / trade-offs:**
- Manual version-bump PRs are required to pick up security patches — they do not
  arrive automatically.
- Version discovery (finding new pinned tags) requires checking upstream release
  pages; there is no automated alert (Phase E could add Renovate/Dependabot).
- `latest` is faster to prototype with; that friction is intentional.

---

## ADR-0004: File-based virtual keys + SQLite audit log (no external database)

**Status:** Accepted  
**Date:** 2026-06-11  
**Deciders:** Elvis Yao

### Context

Phase B requires per-user API keys with model access control and a local audit trail.
LiteLLM's built-in key management (`/key/generate`, per-key budgets) uses Prisma ORM
backed exclusively by PostgreSQL (`datasource.provider = "postgresql"` is hardcoded in
`schema.prisma`). We verified this empirically: calling `/key/generate` without
`DATABASE_URL` returns `{"error": "No connected db."}`.

Adding a PostgreSQL container would:
1. Increase the mirror image set by ~400 MB for an air-gapped deployment.
2. Add an always-running database process, a new failure domain, and backup concerns.
3. Violate the Phase A principle: "no external database dependency in Phase A/B."

### Decision

**Virtual key management:** Implement LiteLLM's `custom_auth` hook with a YAML file
store (`configs/virtual_keys.yaml`). The auth function (`callbacks/key_auth.py`):
- Validates bearer tokens against the YAML file (30-second TTL cache, mtime-aware)
- Returns `UserAPIKeyAuth(user_id=..., models=[...])` so LiteLLM enforces model access natively
- Checks per-key budget ceiling via a `key_budgets` table in SQLite

**Audit logging:** LiteLLM's `CustomLogger` protocol with SQLite backend (`data/audit.db`).
Records: timestamp, user_id, key_label (truncated, non-reversible), model, tokens in/out,
latency, status. Database is mounted as a host-path volume (survives container restarts).

**Why not log prompt/response content by default:** Privacy-minimization principle.
Audit metadata (who, when, what model, how many tokens) satisfies compliance requirements
for access control and usage reporting without retaining conversation content.
Content logging is available as an opt-in (`AUDIT_LOG_CONTENT=true`) with 500-character
truncation to prevent unbounded storage growth.

### Rationale

- **Data sovereignty is the primary selling point of on-prem LLM.** Audit data that
  leaves the box to a cloud database defeats the purpose. SQLite + local volume keeps
  all data on-box.
- **PostgreSQL is not required for Phase B access control.** LiteLLM's `custom_auth`
  hook is designed precisely for scenarios where the default database-backed auth
  is unsuitable. Our YAML store fulfills the same contract.
- **Air-gapped deployability:** SQLite requires zero additional images to mirror. The
  only new runtime dependencies are Python stdlib (`sqlite3`, `threading`) and
  PyYAML (already bundled in the LiteLLM image).
- **Operator simplicity:** Adding a user requires editing a YAML file and waiting
  ≤30 seconds for the cache to refresh — no database migrations, no admin UI, no
  connection pooling to configure.

### Consequences

**Positive:**
- Zero new container images in the mirror set.
- Audit data is a local SQLite file — trivially backed up, inspectable with any
  SQLite client, and exportable to CSV with two lines of Python.
- Key changes take effect within 30 seconds without a container restart.
- `scripts/audit_report.py` provides an immediate one-page view with no dependencies.

**Negative / trade-offs:**
- Key state is not replicated: running multiple LiteLLM replicas requires shared
  access to the same YAML file and SQLite database (e.g. via NFS or a shared bind
  mount). Horizontal scaling defers to Phase D.
- Budget resets (monthly reset, partial refunds) require manual YAML edits or
  a script; LiteLLM's database-backed budget_duration automation is not available.
- RPM enforcement is stored in the YAML but not yet enforced at runtime (Phase C).
  The field is preserved in the schema so Phase C can add enforcement without a
  schema migration.

---

## ADR-0005: Default audit log excludes prompt and response content

**Status:** Accepted  
**Date:** 2026-06-11  
**Deciders:** Elvis Yao

### Context

The audit callback has access to the full prompt (messages array) and response text.
A naive logging implementation would record everything.

### Decision

`AUDIT_LOG_CONTENT` defaults to `false`. When false, the `content_prompt` and
`content_response` columns in `requests` are always `NULL`. When `true`, the columns
are populated with at most 500 characters each (truncated, no ellipsis marker — the
truncation is consistent and machine-detectable).

### Rationale

**Privacy minimization as a default.** Enterprise LLM prompts frequently contain:
- Customer PII (names, addresses, emails, IDs)
- Confidential business logic or trade secrets embedded in system prompts
- Draft communications, legal analysis, code with proprietary algorithms

Storing this content in an audit log changes the sensitivity classification of the log
file: it becomes a PII-bearing artifact requiring GDPR/CCPA treatment, encryption at
rest, restricted access, and defined retention schedules.

Audit *metadata* (user_id, model, token counts, latency, timestamp) satisfies the
compliance questions "who accessed what capability, when, and how much did they use?"
without touching content.

**The opt-in path** (`AUDIT_LOG_CONTENT=true`) is available for teams that have
explicitly decided to accept the content-storage compliance burden — for example, to
support incident investigation or abuse detection. The 500-character truncation
prevents the audit log from becoming a shadow copy of the full conversation history.

### Consequences

**Positive:**
- Audit database has a well-defined, low sensitivity classification by default.
- No accidental PII in backup files, log rotation, or CLI output.
- Easier to share audit reports with stakeholders who should not see content.

**Negative / trade-offs:**
- Cannot reconstruct the exact prompt that caused a failure from the audit log alone
  (must correlate with application-layer logs if needed).
- Abuse detection based on content patterns requires enabling `AUDIT_LOG_CONTENT=true`
  and a separate analysis step.
