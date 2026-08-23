.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := uv run

.PHONY: help
help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ setup
.PHONY: setup
setup: ## install python + node deps and pre-commit hooks
	uv sync --all-extras
	cd console && npm install
	$(PY) pre-commit install || true

# ------------------------------------------------------------------ quality
.PHONY: lint
lint: ## ruff
	$(PY) ruff check .
	$(PY) ruff format --check .

.PHONY: fmt
fmt: ## autoformat
	$(PY) ruff format .
	$(PY) ruff check --fix .

.PHONY: types
types: ## mypy strict
	$(PY) mypy paynaka merchant haat chaos

.PHONY: test
test: ## full test suite
	$(PY) pytest

.PHONY: test-fwd
test-fwd: ## forward tests only
	$(PY) pytest -m "not adversarial"

.PHONY: test-adv
test-adv: ## adversarial tests only
	$(PY) pytest -m adversarial

.PHONY: cov
cov: ## tests with coverage report
	$(PY) pytest --cov --cov-report=term-missing --cov-report=html

.PHONY: secrets
secrets: ## scan working tree and full history for leaked credentials
	@command -v gitleaks >/dev/null 2>&1 \
	  && gitleaks detect --no-banner --redact -v \
	  || echo "gitleaks not installed - see docs/SECURITY.md"

.PHONY: check
check: lint types test secrets ## everything CI runs

# ------------------------------------------------------------------ run
.PHONY: dev
dev: console-data ## merchant :8001 + paynaka :8002 + console :5173
	@echo "starting merchant :8001, paynaka :8002, console :5173"
	@$(PY) uvicorn merchant.app:app --port 8001 --reload & \
	 $(PY) uvicorn paynaka.app:app  --port 8002 --reload & \
	 cd console && npm run dev

.PHONY: merchant
merchant: ## merchant service only
	$(PY) uvicorn merchant.app:app --port 8001 --reload

.PHONY: naka
naka: ## paynaka service only
	$(PY) uvicorn paynaka.app:app --port 8002 --reload

# ------------------------------------------------------------------ demos
.PHONY: demo-happy
demo-happy: ## clean purchase, gate on
	$(PY) python -m buyer.cli --scenario happy

.PHONY: demo-attack
demo-attack: ## the headline: poisoned catalog, gate off then on
	$(PY) python -m buyer.cli --scenario attack --compare

# ------------------------------------------------------------------ benchmark
.PHONY: preflight
preflight: ## a few cents: is the sweep worth running at all?
	$(PY) python -m scripts.preflight

.PHONY: estimate
estimate: ## measure what a full sweep would cost, no API calls
	$(PY) python -m scripts.estimate_cost

.PHONY: bench
bench: ## visible corpus, four defences -> RESULTS.md
	$(PY) python -m haat.runner --corpus visible --defences all

.PHONY: bench-sealed
bench-sealed: ## held-out families. refuses to run before the freeze tag.
	@git rev-parse v1.0-freeze >/dev/null 2>&1 \
	  || { echo "REFUSED: tag v1.0-freeze does not exist. Sealed corpus stays sealed."; exit 1; }
	$(PY) python -m haat.runner --corpus sealed --defences all

.PHONY: chaos
chaos: ## duplicate, reordered and lost webhooks. no model, no keys, no network.
	$(PY) python -m chaos.runner

.PHONY: chaos-verbose
chaos-verbose: ## the same, showing every refund delivery and how it was resolved
	$(PY) python -m chaos.runner --verbose

.PHONY: console-data
console-data: ## write what the console displays into console/public
	$(PY) python -m scripts.console_data

.PHONY: sentinel
sentinel: ## layer-two detector: recall and false positives, side by side
	$(PY) python -m haat.sentinel_eval --per-rule

.PHONY: sentinel-sealed
sentinel-sealed: ## score the detector on held-out families. refuses before the freeze.
	$(PY) python -m haat.sentinel_eval --include-sealed --per-rule

.PHONY: audit-verify
audit-verify: ## recompute the hash chain, print first break
	$(PY) python -m paynaka.audit --verify

.PHONY: clean
clean: ## remove generated artifacts (never touches source)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage haat/out var
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
