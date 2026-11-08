import json

import pytest

from src.compare import compare, grpo_pairs, markdown, pair_episodes, run_pair
from src.report import load_forced


def _step(i, kind="tool_call", *, match=True, predicted=None):
    predicted = predicted or (kind if match else ("reply" if kind == "tool_call" else "tool_call"))
    return {
        "step_id": f"s{i}",
        "reference_kind": kind,
        "predicted_kind": predicted,
        "parse_error": None,
        "kind_match": predicted == kind,
        "name_match": match and kind == "tool_call",
        "normalized_match": match and kind == "tool_call",
        "strict_match": match and kind == "tool_call",
        "hallucinated_tool": False,
        "reference_tools": ("t",) if kind == "tool_call" else (),
        "predicted_tools": ("t",) if predicted == "tool_call" else (),
    }


def _episodes(n, *, call_match, reply_match):
    return [
        {
            "task_id": str(i),
            "seed": 0,
            "steps": [_step(0, match=call_match), _step(1, "reply", match=reply_match)],
        }
        for i in range(n)
    ]


def test_identical_adapters_give_zero_delta_and_zero_width_ci():
    eps = _episodes(10, call_match=True, reply_match=False)
    r = compare(pair_episodes(eps, eps), resamples=200)
    for name in ("step_agreement", "reply_as_reply", "call_instead_of_reply"):
        assert r[name]["delta"] == 0 and r[name]["ci95_low"] == r[name]["ci95_high"] == 0


def test_rates_use_the_right_steps():
    base = _episodes(10, call_match=False, reply_match=False)
    new = _episodes(10, call_match=True, reply_match=True)
    r = compare(pair_episodes(base, new), resamples=200)
    assert r["step_agreement"] == {
        "n_steps": 10,
        "base": 0.0,
        "new": 1.0,
        "delta": 1.0,
        "ci95_low": 1.0,
        "ci95_high": 1.0,
    }
    # a call step answered with a reply is premature termination, over call steps only
    assert r["premature_termination"]["n_steps"] == 10
    assert r["premature_termination"]["delta"] == -1.0
    # a reply step answered with a call, over reply steps only
    assert r["call_instead_of_reply"]["n_steps"] == 10
    assert r["call_instead_of_reply"]["base"] == 1.0
    assert r["malformed_output"]["n_steps"] == 20


def test_ci_is_reproducible_and_covers_a_noisy_delta():
    base = _episodes(40, call_match=True, reply_match=False)
    new = [dict(e, steps=list(e["steps"])) for e in base]
    for e in new[:20]:  # half the episodes improve on the reply step
        e["steps"][1] = _step(1, "reply", match=True)
    pairs = pair_episodes(base, new)
    r1, r2 = compare(pairs), compare(pairs)
    assert r1 == r2
    rr = r1["reply_as_reply"]
    assert rr["delta"] == 0.5 and rr["ci95_low"] < 0.5 < rr["ci95_high"]


def test_pairing_refuses_different_episodes_or_steps():
    eps = _episodes(3, call_match=True, reply_match=True)
    with pytest.raises(ValueError, match="episodes differ"):
        pair_episodes(eps, eps[:2])
    other = [dict(e, steps=[dict(e["steps"][0], step_id="x"), *e["steps"][1:]]) for e in eps]
    with pytest.raises(ValueError, match="step ids differ"):
        pair_episodes(eps, other)


def _write(path, label, catalog, episodes):
    with path.open("w") as fh:
        for e in episodes:
            rec = {"config_fingerprint": "fp", "config_name": f"forced:{label}:c{catalog}"}
            fh.write(json.dumps({**rec, "smoke": False, **e}) + "\n")


def test_grpo_pairs_prefer_the_mlx_sft_base():
    by_catalog = {
        16: {"qwen15b-grpo-mlx": [], "qwen15b-sft-mlx": [], "qwen15b-sft": []},
        40: {"qwen15b-grpo-mlx": [], "qwen15b-sft": []},
        80: {"qwen15b-sft": []},
    }
    assert grpo_pairs(by_catalog) == [
        (16, "qwen15b-sft-mlx", "qwen15b-grpo-mlx"),
        (40, "qwen15b-sft", "qwen15b-grpo-mlx"),
    ]


def test_run_pair_reads_results_and_renders(tmp_path):
    _write(
        tmp_path / "forced-m-sft-c16.jsonl",
        "m-sft",
        16,
        _episodes(5, call_match=False, reply_match=True),
    )
    _write(
        tmp_path / "forced-m-grpo-mlx-c16.jsonl",
        "m-grpo-mlx",
        16,
        _episodes(5, call_match=True, reply_match=True),
    )
    result = run_pair(load_forced(tmp_path, 16), 16, "m-sft", "m-grpo-mlx")
    assert result["n_episodes"] == 5
    assert "| step_agreement | 5 | 0.0% | 100.0% | +100.0 * |" in markdown(result)
    with pytest.raises(FileNotFoundError):
        run_pair(load_forced(tmp_path, 40), 40, "m-sft", "m-grpo-mlx")
