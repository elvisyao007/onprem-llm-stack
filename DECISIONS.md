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
