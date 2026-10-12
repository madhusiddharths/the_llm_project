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

.PHONY: help setup check lint typecheck test smoke preflight prompt-hash phoenix tracking harvest harvest-plan pull pull-free eval mlx-calibrate free-mlx stage-d-mlx clean

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
	@echo "pull-free    fetch the Stage A/D episode logs from the Hub into results/"
	@echo "eval         the headline tables from results/: teacher, Stage A/D, forced eval"
	@echo "mlx-calibrate  1.5B SFT forced eval c16 on the Mac vs vLLM (~1 h, no quota)"
	@echo "free-mlx     free-running on the Mac: CFG=qwen3b CATALOGS=\"80 16 40\" (quota)"
	@echo "stage-d-mlx  Stage D on the Mac: MODEL=qwen15b THRESHOLDS=\"0.7 0.95 0.5 0.85\" (quota)"
	@echo "prompt-hash  print the live serializer hash for configs/base.yaml"

setup:
	uv venv .venv --python 3.12
	uv pip install -r requirements.txt

check: lint typecheck test

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

# Basic-mode pyright over the pure-Python core. pyrightconfig.json excludes the
# modules that are mostly calls into tau2, vLLM, torch and wandb, whose type
# information is incomplete enough to bury real findings in false ones.
typecheck:
	$(PY) -m pyright

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

pull-free:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/pull_results.py --free

# The README's numbers, recomputed from the logs in results/ (no GPU, no API).
# Re-running the episodes themselves is the Kaggle notebooks' job.
eval: pull-free
	$(PY) src/stats.py baseline
	$(PY) src/stats.py baseline --catalog 80
	@[ -f results/harvest-baseline-c40.jsonl ] && $(PY) src/stats.py baseline --catalog 40 || echo "(teacher c40 not run or not complete yet)"
	$(PY) src/stats.py free --markdown
	@for c in 16 40 80; do echo "\n--- forced eval, catalog $$c ---"; $(PY) src/report.py --catalog $$c --markdown || exit 1; done

# --- lane 2: the student on the Mac (src/mlx_engine.py; requirements-mac.txt) ---
# caffeinate keeps the Mac awake for the length of the run; a sleeping Mac kills it.
HUB_SYNC ?= madhusiddharths1/tool-router-results
HF_USER ?= madhusiddharths1
MLX_CONCURRENCY ?= 3
CFG ?= qwen3b
CATALOGS ?= 80 16 40
MODEL ?= qwen15b
THRESHOLDS ?= 0.7 0.95 0.5 0.85

mlx-calibrate:
	@set -a; [ -f .env ] && . ./.env; set +a; caffeinate -is $(PY) src/generate.py \
		--config configs/qwen15b.yaml --adapter $(HF_USER)/tool-router-qwen15b-sft \
		--prompts data/forced/prompts-c16.jsonl --out results/completions-qwen15b-sft-mlx-c16.jsonl --backend mlx
	$(PY) src/engine_check.py results/completions-qwen15b-sft-c16.jsonl results/completions-qwen15b-sft-mlx-c16.jsonl || true
	$(PY) src/eval_forced.py --config configs/qwen15b.yaml --catalog 16 --backend file \
		--completions results/completions-qwen15b-sft-mlx-c16.jsonl --label qwen15b-sft-mlx
	$(PY) src/report.py --catalog 16 --markdown

# One quota day of free-running: each catalog finishes or stops at the day's budget.
free-mlx:
	@set -a; [ -f .env ] && . ./.env; set +a; for c in $(CATALOGS); do \
		echo "===== $(CFG) catalog $$c (mlx) ====="; \
		caffeinate -is $(PY) src/eval_free.py --config configs/$(CFG).yaml --catalog $$c \
			--adapter $(HF_USER)/tool-router-$(CFG)-sft --hub-sync $(HUB_SYNC) \
			--backend mlx --concurrency $(MLX_CONCURRENCY) || exit 1; \
	done

stage-d-mlx:
	@set -a; [ -f .env ] && . ./.env; set +a; for t in $(THRESHOLDS); do \
		echo "===== $(MODEL) catalog 80 threshold $$t (mlx) ====="; \
		caffeinate -is $(PY) src/eval_free.py --config configs/$(MODEL).yaml --catalog 80 \
			--adapter $(HF_USER)/tool-router-$(MODEL)-sft --hub-sync $(HUB_SYNC) \
			--backend mlx --concurrency $(MLX_CONCURRENCY) --escalate $$t || exit 1; \
	done

prompt-hash:
	@$(PY) -c "from src.prompts import template_hash; print(template_hash())"

clean:
	rm -rf .pytest_cache .ruff_cache results/*.smoke.jsonl
	find src tests -name __pycache__ -type d -exec rm -rf {} +
