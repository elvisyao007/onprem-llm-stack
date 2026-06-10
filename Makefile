.PHONY: smoke-eval

## Acceptance test: run the golden set through generator + judge models.
## The stack must be running (docker compose --profile dev up -d).
## EVAL_API_KEY (or LITELLM_MASTER_KEY) must be set in .env.
smoke-eval:
	@bash -c 'set -a; [ -f .env ] && . ./.env; set +a; python3 scripts/smoke_eval.py'
