# CLAUDE.md — Working Principles for onprem-llm-stack

## Default assumptions (enterprise reality, not ideal scenario)

- The environment is, or will become, **air-gapped** before production.
  Every component must work without outbound internet access at runtime.
- Data traversing this stack may contain **PII** until proven otherwise.
  Do not add logging, callbacks, or telemetry that exfiltrates request/response content.
- The host may run **multiple concurrent users** with different trust levels.
  LiteLLM master key is the only auth layer in Phase A; treat it as a shared secret.
- Hardware is finite: **RTX 5090, 32 GB VRAM**. VRAM is the bottleneck.
  Any model or batch-size change must be evaluated against this constraint.

## Hard rules

1. **No runtime internet dependencies.** All container images, Python packages, and
   config fetches must be resolvable from a local registry or from what is already
   baked into the pinned image. A `pip install` inside an entrypoint, a `git clone`
   inside a healthcheck, or a public CDN asset are all violations.

2. **Image tags are always pinned to a specific digest-stable version.**
   `latest`, `main`, branch tags, and other mutable refs are forbidden.
   Rationale: air-gapped mirrors must be able to replicate an exact, reproducible set.
   See ADR-0003.

3. **Secrets flow through environment variables only.**
   Never hardcode a key, password, or token in any tracked file.
   Never log a secret (LiteLLM can log prompts — confirm `no_redact` is off in prod).
   `.env` is in `.gitignore` and must never be committed.

4. **Before adding a new component, answer: "how does this work without internet?"**
   If the answer is "it doesn't", the component is not suitable for this stack without
   a mitigation plan (air-gapped mirror, bundled assets, etc.).

## Profiles

| Profile | Inference backend | When to use |
|---------|------------------|-------------|
| `dev`   | Ollama on host (`:11434`) | Local development; avoids double-loading models into VRAM |
| `prod`  | vLLM container | Staging/production; higher throughput, OpenAI-compatible |

Run with `docker compose --profile dev up -d` or `--profile prod up -d`.
Never run both profiles simultaneously on the same host (VRAM contention).

## Adding new services (checklist)

- [ ] Image tag pinned to a specific version
- [ ] Docker healthcheck defined (`test`, `interval`, `timeout`, `retries`, `start_period`)
- [ ] Service added to `scripts/healthcheck.sh`
- [ ] Any new env vars documented with comments in `.env.example`
- [ ] Operates correctly with no outbound network at runtime
- [ ] Does not write request/response content to uncontrolled sinks

## Out of scope for Phase A (do not add)

- Virtual keys / per-user budget enforcement (Phase B)
- Audit callback / usage logging to external sinks (Phase B)
- PII guardrails / prompt injection detection (Phase C)
- SSO / LDAP integration (Phase C)
- Observability stack (Prometheus, Grafana, Jaeger) (Phase D)
- Kubernetes manifests (Phase D)
- Multi-GPU sharding (Phase D)
- Any CI pipeline or automated test suite (Phase E)

## Commit discipline

Split commits by concern: skeleton / compose / config / scripts / docs.
Message format: `<area>: <what> — <why in one clause>`.
Example: `compose: pin image versions — air-gapped reproducibility requires exact tags`.
