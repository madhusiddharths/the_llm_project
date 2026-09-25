# FUTURE

Where scope creep goes to be quiet.

Plan §2: "If a new idea arrives mid-build, write it in `FUTURE.md` and keep
moving." Nothing in this file gets built before Oct 11.

---

## Ruled out for this build (plan §2, "do not add these, at any point")

- Corrective/self-RAG, grader nodes, query rewriters
- Multi-LoRA adapter swapping
- Any vector database
- A frontend, demo video, or chat UI
- The airline domain — retail only, unless week 3 runs early
- The AI-Counselling load-test add-on — post-ship, separate afternoon

These are listed so that re-proposing one is visibly a decision to reverse,
rather than a new idea.

**Reversed, 2026-09-22:** multi-LoRA serving returns in **v2 only**, as Stage F2 of the extension plan (Part II of the plan, X6). It is a serving measurement: throughput and latency with mixed adapters on one vLLM instance. It is not an adapter-swapping router. It stays out of v1.

---

## Parked ideas

<!-- Append here, dated. One line each. Do not expand into a design. -->

- 2026-09-09 — NVIDIA build.nvidia.com holds ~1,000 unspent trial credits (5,000
  lifetime max, non-renewable). Tool calling verified there. Best use is not bulk
  work but a drift check: run ~100 episodes against NVIDIA's first-party
  Nemotron and compare step agreement with the OpenRouter-served copy. That
  directly measures the quantisation-drift risk the teacher config flags, and
  turns a stated limitation into a measured one. Costs ~100 of the 1,000.
- 2026-09-09 — Request NVIDIA's additional 4,000 credits with an .edu address.
  Free, minutes of work, and worth doing purely as reserve capacity.
- 2026-09-25 — Distractor-augmented SFT (V1-07 option C, v2 ablation). Retrain on
  the same 1,099 teacher decisions with random BFCL decoys in the tool list,
  drawn only from tools outside catalog-40/80. Answers stay the teacher's.
  Question: does decoy practice flatten the c16→c80 cliff on unseen decoys?
  Compare against the v1 adapters. Never use the eval catalogs (plan §5).
- 2026-09-25 — Public function-calling mix (V1-07 option B, the plan's original
  8–15k BFCL/Glaive/ToolACE augmentation), with every eval-catalog tool name
  filtered out. Question: does general tool-calling practice transfer to retail?
  Watch the ~10:1 dilution of teacher data. Cost ~5–10 GPU h at 1.5B (estimate).
