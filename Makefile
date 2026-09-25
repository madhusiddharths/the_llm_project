# Week 0 scaffolding. `make check` is the acceptance test for this milestone.
#
# Nothing here runs longer than two minutes. Training, full eval sweeps, and the
# teacher harvest are yours to launch (division-of-labor.md).

PY := .venv/bin/python
# Offline, <2 min, no GPU and no API quota. The other smoke paths need hardware
# or quota and run where the real job runs: train/generate/eval_free --smoke on
# Kaggle (notebooks/kaggle_*.ipynb), harvest --smoke against the live API.
SCRIPTS := eval_forced router_node taxonomy
CONFIG ?= configs/qwen05b.yaml

.PHONY: help setup check lint test smoke preflight prompt-hash phoenix tracking harvest harvest-plan pull clean

help:
	@echo "setup        create .venv and install local deps"
	@echo "check        lint + tests (the Week 0 acceptance test)"
	@echo "smoke        run the offline --smoke paths (<2 min; GPU/API smokes run on Kaggle)"
	@echo "preflight    pre-launch checklist; run before any job over an hour"
	@echo "phoenix      start the local Phoenix collector on :6006 (blocks)"
	@echo "tracking     verify W&B and Phoenix are both live"
	@echo "harvest-plan show what the harvest would do; makes no API calls"
	@echo "harvest      run the teacher harvest until the daily quota is spent"
	@echo "pull         fetch Kaggle completions from the Hub into results/"
	@echo "prompt-hash  print the live serializer hash for configs/base.yaml"

setup:
	uv venv .venv --python 3.12
	uv pip install -r requirements.txt

check: lint test

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

test:
	$(PY) -m pytest

smoke:
	@for s in $(SCRIPTS); do \
		echo "--- $$s ---"; \
		$(PY) src/$$s.py --config $(CONFIG) --smoke || exit 1; \
	done
	@echo "\nAll smoke paths passed."

preflight:
	$(PY) -m src.preflight

phoenix:
	$(PY) -m phoenix.server.main serve

tracking:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) -m src.check_tracking

harvest-plan:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/harvest.py --config $(CONFIG) --dry-run

harvest:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/harvest.py --config $(CONFIG)

pull:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/pull_results.py $(ARGS)

prompt-hash:
	@$(PY) -c "from src.prompts import template_hash; print(template_hash())"

clean:
	rm -rf .pytest_cache .ruff_cache results/*.smoke.jsonl
	find src tests -name __pycache__ -type d -exec rm -rf {} +
