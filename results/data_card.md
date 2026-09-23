# Data card — teacher harvest

- **Harvest log:** results/harvest-harvest.jsonl
- **Teacher:** nvidia/nemotron-3-ultra-550b-a55b:free
- **Teacher fingerprint:** 4138d3faa22a
- **Generated:** 2026-09-22 03:08 UTC
- **Token counts:** Qwen/Qwen2.5-0.5B-Instruct tokenizer

## Episodes

- Logged cells: 162; successful (reward 1.0): **108**
- Train tasks with at least one success: **46 / 54**
- Tasks with no successful episode (absent from training): 20, 32, 38, 59, 99, 104, 108, 112
- Successful episodes per task: 1 → 6 tasks, 2 → 18 tasks, 3 → 22 tasks
- By sampling temperature: T=0.0: 39, T=0.7: 35, T=1.0: 34

## Decisions (greeting excluded)

- **1099** teacher decisions: **703** tool calls, **396** replies to the user
- Parallel tool calls: 0
- Per episode: mean 10.2, median 10, max 14

## Tool frequency (teacher tool calls)

| Tool | Calls | Share |
|---|---:|---:|
| `get_order_details` | 258 | 36.7% |
| `get_user_details` | 100 | 14.2% |
| `find_user_id_by_name_zip` | 83 | 11.8% |
| `get_product_details` | 77 | 11.0% |
| `modify_pending_order_items` | 35 | 5.0% |
| `return_delivered_order_items` | 31 | 4.4% |
| `find_user_id_by_email` | 30 | 4.3% |
| `exchange_delivered_order_items` | 24 | 3.4% |
| `cancel_pending_order` | 18 | 2.6% |
| `modify_pending_order_address` | 16 | 2.3% |
| `list_all_product_types` | 10 | 1.4% |
| `modify_user_address` | 10 | 1.4% |
| `transfer_to_human_agents` | 5 | 0.7% |
| `modify_pending_order_payment` | 3 | 0.4% |
| `get_item_details` | 2 | 0.3% |
| `calculate` | 1 | 0.1% |

- Never called: none
- Called but not in the native catalog: none

## Tool results

- 703 results, 8 errors; characters: mean 998.9, p95 2164, max 3370

## Prompt lengths (Qwen/Qwen2.5-0.5B-Instruct tokenizer)

- System prompt (tau2 instructions + retail policy): 1487 tokens
- Native tools, compact rendering: 2000 tokens (~125 per tool)
- `decision_*`: the prompt at one teacher decision (what eval sends).
- `episode_*`: one whole training sequence (what train.py sees).

| Variant | p50 | p95 | max | > 2048 | > 4096 | > 8192 | > 12288 | > 16384 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| decision_catalog16 | 5197 | 8969 | 11898 | 100% | 68% | 9% | 0% | 0% |
| decision_catalog16_no_policy | 3710 | 7482 | 10411 | 100% | 43% | 2% | 0% | 0% |
| episode_catalog16 | 7911 | 10890 | 12176 | 100% | 100% | 45% | 0% | 0% |
| decision_catalog40_extrapolated | 8197 | 11969 | 14898 | 100% | 100% | 50% | 3% | 0% |
| decision_catalog80_extrapolated | 13197 | 16969 | 19898 | 100% | 100% | 100% | 66% | 8% |
