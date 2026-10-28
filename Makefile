# Week 0 scaffolding. `make check` is the acceptance test for this milestone.
#
# Nothing here runs longer than two minutes. Training, full eval sweeps, and the
# teacher harvest are yours to launch (division-of-labor.md).

PY := .venv/bin/python
# Offline, <2 min, no GPU and no API quota. The other smoke paths need hardware
# or quota and run where the real job runs: train/generate/eval_free --smoke on
# Kaggle (notebooks/kaggle_*.ipynb), harvest --smoke against the live API.
SCRIPTS := eval_forced router_node taxonomy serve_bench
CONFIG ?= configs/qwen05b.yaml

.PHONY: help setup check lint typecheck test smoke preflight prompt-hash phoenix tracking harvest harvest-plan baseline pull pull-free eval mlx-calibrate free-mlx stage-d-mlx pairs-export pairs-generate pairs forced-mlx score-forced confidence-mlx train-dpo train-grpo serve-bench eval-headline clean

help:
	@echo "setup        create .venv and install local deps"
	@echo "check        lint + tests (the Week 0 acceptance test)"
	@echo "smoke        run the offline --smoke paths (<2 min; GPU/API smokes run on Kaggle)"
	@echo "preflight    pre-launch checklist; run before any job over an hour"
	@echo "phoenix      start the local Phoenix collector on :6006 (blocks)"
	@echo "tracking     verify W&B and Phoenix are both live"
	@echo "harvest-plan show what the harvest would do; makes no API calls"
	@echo "harvest      run the teacher harvest until the daily quota is spent"
	@echo "baseline     teacher baseline (quota): SIM=gemma CATALOG=80; resumes, stops at the day's budget"
	@echo "pull         fetch Kaggle completions from the Hub into results/"
	@echo "pull-free    fetch the Stage A/D episode logs from the Hub into results/"
	@echo "eval         the headline tables from results/: teacher, Stage A/D, forced eval"
	@echo "mlx-calibrate  1.5B SFT forced eval c16 on the Mac vs vLLM (~1 h, no quota)"
	@echo "free-mlx     free-running on the Mac: CFG=qwen3b CATALOGS=\"80 16 40\" [SIM=gemma] (quota)"
	@echo "stage-d-mlx  Stage D on the Mac: MODEL=qwen15b THRESHOLDS=\"0.7 0.95 0.5 0.85\" (quota)"
	@echo "pairs-export E2: train-split states -> data/pairs/prompts-train-c16.jsonl (seconds)"
	@echo "pairs-generate E2: MODEL=qwen15b greedy completions over the train states (Mac MLX, ~1 h)"
	@echo "pairs        E2: student completions -> DPO pairs: MODEL=qwen15b (needs the completions file)"
	@echo "forced-mlx   forced eval of a Hub adapter on the Mac: MODEL=qwen15b ADAPTER=qwen15b-sft-f25"
	@echo "score-forced score pulled vLLM completions: MODEL=qwen15b ADAPTER=qwen15b-sft-f25 [CATALOGS=\"16 40 80\"]"
	@echo "confidence-mlx router confidence check (log-probs + escalation sweep): MODEL=qwen3b CATALOG=80"
	@echo "prompt-hash  print the live serializer hash for configs/base.yaml"
	@echo "SIM=gemma    run any quota target under simulator epoch 2 (configs/sim/gemma.yaml)"

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

# Simulator epoch: empty = base.yaml's (v1); SIM=gemma -> configs/gemma/<model>.yaml.
SIM ?=
CATALOG ?= 16
cfgpath = configs/$(if $(SIM),$(SIM)/,)$(1).yaml

# The teacher baseline for one catalog. Any model config works (the harvest is
# keyed to the teacher, not the student); qwen3b is used for the trajectory dir.
baseline:
	@set -a; [ -f .env ] && . ./.env; set +a; caffeinate -is $(PY) src/harvest.py \
		--config $(call cfgpath,qwen3b) --mode baseline --catalog $(CATALOG)

pull:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/pull_results.py $(ARGS)

pull-free:
	@set -a; [ -f .env ] && . ./.env; set +a; $(PY) src/pull_results.py --free

# The README's numbers, recomputed from the logs in results/ (no GPU, no API).
# Re-running the episodes themselves is the Kaggle notebooks' job.
eval: pull-free
	$(PY) src/stats.py baseline --config $(call cfgpath,qwen05b) || true
	$(PY) src/stats.py baseline --config $(call cfgpath,qwen05b) --catalog 80 || true
	$(PY) src/stats.py baseline --config $(call cfgpath,qwen05b) --catalog 40 || echo "(teacher c40 not run under this simulator yet)"
	$(PY) src/stats.py free --config $(call cfgpath,qwen05b) --markdown
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
		caffeinate -is $(PY) src/eval_free.py --config $(call cfgpath,$(CFG)) --catalog $$c \
			--adapter $(HF_USER)/tool-router-$(CFG)-sft --hub-sync $(HUB_SYNC) \
			--backend mlx --concurrency $(MLX_CONCURRENCY) || exit 1; \
	done

stage-d-mlx:
	@set -a; [ -f .env ] && . ./.env; set +a; for t in $(THRESHOLDS); do \
		echo "===== $(MODEL) catalog 80 threshold $$t (mlx) ====="; \
		caffeinate -is $(PY) src/eval_free.py --config $(call cfgpath,$(MODEL)) --catalog 80 \
			--adapter $(HF_USER)/tool-router-$(MODEL)-sft --hub-sync $(HUB_SYNC) \
			--backend mlx --concurrency $(MLX_CONCURRENCY) --escalate $$t || exit 1; \
	done

# --- v2 (plan X10). Each long run is yours to launch; these print or prepare. ---
PAIRS_DIR ?= data/pairs

pairs-export:
	$(PY) src/make_pairs.py export --config configs/qwen15b.yaml --out $(PAIRS_DIR)/prompts-train-c16.jsonl

# E2-2 on the Mac (lane 2, no quota): greedy completions over the train states.
pairs-generate:
	@set -a; [ -f .env ] && . ./.env; set +a; caffeinate -is $(PY) src/generate.py \
		--config configs/$(MODEL).yaml --adapter $(HF_USER)/tool-router-$(MODEL)-sft \
		--prompts $(PAIRS_DIR)/prompts-train-c16.jsonl \
		--out results/completions-$(MODEL)-sft-train-c16.jsonl --backend mlx

pairs:
	$(PY) src/make_pairs.py build --config configs/qwen15b.yaml \
		--completions results/completions-$(MODEL)-sft-train-c16.jsonl \
		--adapter $(HF_USER)/tool-router-$(MODEL)-sft --out $(PAIRS_DIR)/$(MODEL)

# E1-3 / E4-1 on the Mac (lane 2, no quota): forced eval of any Hub adapter.
#   make forced-mlx MODEL=qwen15b ADAPTER=qwen15b-sft-f25 CATALOGS="16 40 80"
ADAPTER ?= $(MODEL)-sft
forced-mlx:
	@set -a; [ -f .env ] && . ./.env; set +a; for c in $(CATALOGS); do \
		echo "===== $(ADAPTER) forced c$$c (mlx) ====="; \
		caffeinate -is $(PY) src/generate.py --config configs/$(MODEL).yaml \
			--adapter $(HF_USER)/tool-router-$(ADAPTER) --prompts data/forced/prompts-c$$c.jsonl \
			--out results/completions-$(ADAPTER)-mlx-c$$c.jsonl --backend mlx || exit 1; \
		$(PY) src/eval_forced.py --config configs/$(MODEL).yaml --catalog $$c --backend file \
			--completions results/completions-$(ADAPTER)-mlx-c$$c.jsonl --label $(ADAPTER)-mlx || exit 1; \
	done

# Score vLLM completions pulled from the Hub (`make pull`), e.g. the E1 adapters
# generated by notebooks/kaggle_e1_generate.ipynb. Seconds, Mac, no GPU.
#   make score-forced MODEL=qwen15b ADAPTER=qwen15b-sft-f25
score-forced:
	@for c in $(CATALOGS); do \
		$(PY) src/eval_forced.py --config configs/$(MODEL).yaml --catalog $$c --backend file \
			--completions results/completions-$(ADAPTER)-c$$c.jsonl --label $(ADAPTER) || exit 1; \
	done

# Router confidence check on the Mac (no quota): forced eval with log-probs,
# then the offline escalation sweep (AUROC: does low confidence find the wrong
# steps?). 2026-10-22: run for qwen3b before it becomes Stage D's model.
#   make confidence-mlx MODEL=qwen3b CATALOG=80
confidence-mlx:
	@set -a; [ -f .env ] && . ./.env; set +a; caffeinate -is $(PY) src/generate.py \
		--config configs/$(MODEL).yaml --adapter $(HF_USER)/tool-router-$(MODEL)-sft \
		--prompts data/forced/prompts-c$(CATALOG).jsonl --logprobs --backend mlx \
		--out results/completions-$(MODEL)-sft-mlx-c$(CATALOG)-logprobs.jsonl
	$(PY) src/eval_forced.py --config configs/$(MODEL).yaml --catalog $(CATALOG) --backend file \
		--completions results/completions-$(MODEL)-sft-mlx-c$(CATALOG)-logprobs.jsonl \
		--label $(MODEL)-sft-mlx-lp
	$(PY) src/escalation_sim.py --config configs/$(MODEL).yaml \
		--completions results/completions-$(MODEL)-sft-mlx-c$(CATALOG)-logprobs.jsonl \
		--results results/forced-$(MODEL)-sft-mlx-lp-c$(CATALOG).jsonl

train-dpo train-grpo serve-bench eval-headline:
	@echo "$@ runs on Kaggle; see notebooks/ (kaggle_dpo.ipynb, kaggle_grpo.ipynb)."

prompt-hash:
	@$(PY) -c "from src.prompts import template_hash; print(template_hash())"

clean:
	rm -rf .pytest_cache .ruff_cache results/*.smoke.jsonl
	find src tests -name __pycache__ -type d -exec rm -rf {} +
