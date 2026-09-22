# Data card — teacher harvest

- **Harvest log:** results/harvest-harvest.jsonl
- **Teacher:** nvidia/nemotron-3-ultra-550b-a55b:free
- **Teacher fingerprint:** 4138d3faa22a
- **Generated:** 2026-09-22 01:21 UTC
- **Token counts:** estimated at 3.5 chars/token (tokenizer not cached)

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

## Prompt lengths (estimated at 3.5 chars/token (tokenizer not cached))

- System prompt (tau2 instructions + retail policy): 2016 tokens
- Native tools, compact rendering: 2218 tokens (~139 per tool)
- `decision_*`: the prompt at one teacher decision (what eval sends).
- `episode_*`: one whole training sequence (what train.py sees).

| Variant | p50 | p95 | max | > 2048 | > 4096 | > 8192 | > 12288 | > 16384 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| decision_catalog16 | 5689 | 8567 | 11335 | 100% | 100% | 8% | 0% | 0% |
| decision_catalog16_no_policy | 3673 | 6551 | 9319 | 100% | 40% | 0% | 0% | 0% |
| episode_catalog16 | 7922 | 10229 | 11643 | 100% | 100% | 41% | 0% | 0% |
| decision_catalog40_extrapolated | 9016 | 11894 | 14662 | 100% | 100% | 69% | 3% | 0% |
| decision_catalog80_extrapolated | 14561 | 17439 | 20207 | 100% | 100% | 100% | 100% | 16% |
