# Air-Gapped Deployment Guide

This guide explains how to transfer the entire onprem-llm-stack — container
images, model weights, configuration, and validation tooling — from an
internet-connected staging machine to a production environment with **no
outbound network access**.

After following this guide the target machine will:
- Run the full dev-profile stack (LiteLLM gateway + Open WebUI)
- Pass the smoke-eval acceptance test with a local pass/fail verdict
- Never make an outbound network call at runtime

---

## Prerequisites

| Requirement | Staging machine (online) | Target machine (air-gapped) |
|-------------|-------------------------|-----------------------------|
| OS          | Linux (Ubuntu 22.04+)   | Linux (Ubuntu 22.04+)       |
| Docker      | 24+, Compose v2+        | 24+, Compose v2+            |
| GPU         | Not required            | NVIDIA GPU + drivers        |
| Disk        | ~20 GB free             | ~60 GB free                 |
| Python      | 3.10+                   | 3.10+                       |

---

## Step 1 — Pull and save container images (staging machine)

All image tags are pinned in `docker-compose.yml`. Pull them explicitly before
saving so you have the exact versions:

```bash
# dev-profile images (always needed)
docker pull ghcr.io/berriai/litellm:v1.88.1
docker pull ghcr.io/open-webui/open-webui:v0.9.6

# prod-profile image (only if you will run vLLM in production)
# docker pull vllm/vllm-openai:v0.22.1
```

Save to a compressed archive:

```bash
docker save \
  ghcr.io/berriai/litellm:v1.88.1 \
  ghcr.io/open-webui/open-webui:v0.9.6 \
  | gzip > onprem-llm-stack-images.tar.gz

# Add vLLM if needed:
# docker save \
#   ghcr.io/berriai/litellm:v1.88.1 \
#   ghcr.io/open-webui/open-webui:v0.9.6 \
#   vllm/vllm-openai:v0.22.1 \
#   | gzip > onprem-llm-stack-images.tar.gz
```

Verify the archive:

```bash
gunzip -c onprem-llm-stack-images.tar.gz | docker image ls --format json | \
  python3 -c "import sys,json; [print(json.loads(l).get('Repository')+':'+json.loads(l).get('Tag','')) for l in sys.stdin]"
# Should print the two (or three) image names above.
```

---

## Step 2 — Copy the repository (staging machine)

Package the repository, excluding git history, generated data, and secrets:

```bash
# From the repository root
git archive --format=tar.gz --prefix=onprem-llm-stack/ HEAD \
  > onprem-llm-stack-repo.tar.gz
```

`git archive` exports only tracked files — the `.env`, `data/audit.db`, and
`configs/virtual_keys.yaml` (which are git-ignored) are not included.
You will configure them fresh on the target machine.

---

## Step 3 — Save Ollama model weights (staging machine)

Model weights are large binary files stored in Ollama's model cache, **not**
in the Docker image. They must be transferred separately.

```bash
# Find the current model cache location
echo $OLLAMA_MODELS          # if set
ls ~/.ollama/models/          # default if OLLAMA_MODELS is unset

# The full cache (both models) is typically 35–45 GB.
# Use rsync for reliable partial-transfer resume:
rsync -av --progress \
  /mnt/data/models/ollama/ \
  /media/transfer/ollama-models/

# Verify the models are present
du -sh /media/transfer/ollama-models/blobs/
```

> **Tip:** If you manage models with a custom `OLLAMA_MODELS` path (as in this
> stack), set the same path on the target machine so no reconfiguration is
> needed after transfer.

---

## Step 4 — Transfer to the target machine

Copy the three artefacts to the air-gapped machine via USB drive, internal
network file share, or secure copy (before the network is severed):

```
onprem-llm-stack-images.tar.gz   (~3 GB)
onprem-llm-stack-repo.tar.gz     (~50 KB)
ollama-models/                   (~35–45 GB)
```

---

## Step 5 — Load images (target machine)

```bash
gunzip -c onprem-llm-stack-images.tar.gz | docker load
# Loaded image: ghcr.io/berriai/litellm:v1.88.1
# Loaded image: ghcr.io/open-webui/open-webui:v0.9.6

docker images | grep -E 'litellm|open-webui'
```

---

## Step 6 — Install Ollama and restore model weights (target machine)

Ollama must be installed **without internet access**. Download the installer on
the staging machine and transfer it:

```bash
# On staging machine — download the Ollama installer
curl -L https://ollama.ai/install.sh -o ollama-install.sh
# Transfer ollama-install.sh to target machine alongside the other artefacts
```

On the target machine:

```bash
# Install Ollama (the script does not require internet if the binary is bundled)
bash ollama-install.sh

# Point Ollama at the transferred model weights
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_MODELS=/mnt/data/models/ollama"\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl enable --now ollama

# Copy the transferred weights to the model cache
rsync -av /media/transfer/ollama-models/ /mnt/data/models/ollama/

# Verify models are visible
ollama list
# NAME                ID            SIZE    MODIFIED
# qwen3:32b           ...           20 GB   ...
# gemma4:31b          ...           19 GB   ...
```

---

## Step 7 — Configure and start the stack (target machine)

```bash
# Unpack the repository
mkdir -p ~/onprem-llm-stack
tar -xzf onprem-llm-stack-repo.tar.gz -C ~/
cd ~/onprem-llm-stack

# Create your .env from the template
cp .env.example .env
# Edit at minimum: LITELLM_MASTER_KEY, WEBUI_SECRET_KEY
$EDITOR .env

# Create virtual key definitions
cp configs/virtual_keys.yaml.example configs/virtual_keys.yaml
# Edit to add your user keys
$EDITOR configs/virtual_keys.yaml

# Start the dev-profile stack (no internet required — images already loaded)
docker compose --profile dev up -d

# Verify services are healthy
bash scripts/healthcheck.sh
```

---

## Step 8 — Run smoke-eval to validate the deployment (target machine)

The smoke-eval acceptance test makes **zero internet calls**. All traffic goes
to the local LiteLLM gateway → host Ollama. The judge is the local
`gemma4-31b` model.

```bash
# Create a key with access to both generator and judge models
bash scripts/create_key.sh \
  --user smoke-eval \
  --models qwen3-32b,gemma4-31b \
  --budget 5 \
  --rpm 120

# Add the printed key to .env
echo 'EVAL_API_KEY=sk-user-smoke-eval-<printed-key>' >> .env

# Run the acceptance test (sources .env automatically)
make smoke-eval
```

Expected output:

```
========================================================================
  onprem-llm-stack — Smoke Eval
  Generator : qwen3-32b
  Judge     : gemma4-31b   <- judge != generator, non-self-evaluation
  Questions : 15    Threshold : 70% pass rate required
========================================================================

  ID    Lang  Category         Result  Gen ms   Reason
  ...

========================================================================
  Passed    : 13 / 15  (86.7%)
  Verdict   : PASS  (threshold >= 70%)
  ...
========================================================================
```

A `PASS` verdict confirms the stack is production-ready on this hardware with
these model weights. Reports are written to `eval/reports/<timestamp>/`.

---

## Offline operation checklist

Before cutting the network on the target machine, verify:

- [ ] `docker images` shows `litellm:v1.88.1` and `open-webui:v0.9.6`
- [ ] `ollama list` shows `qwen3:32b` and `gemma4:31b`
- [ ] `bash scripts/healthcheck.sh` exits 0
- [ ] `make smoke-eval` completes with `PASS`
- [ ] `.env` contains real values for `LITELLM_MASTER_KEY` and `WEBUI_SECRET_KEY`
- [ ] `configs/virtual_keys.yaml` has the required user keys

Once all boxes are checked, the stack will operate indefinitely without
outbound internet access.

---

## Updating in an air-gapped environment

When a new version is available:

1. On the staging machine: pull the new image, save it, export the updated
   `docker-compose.yml` (with the new pinned tag).
2. Transfer the new `.tar.gz` and the updated compose file.
3. On the target machine: `docker load`, then `docker compose --profile dev up -d`.
4. Run `make smoke-eval` again to confirm the new version passes.

Never use `docker pull` or `git pull` directly on the air-gapped machine.
