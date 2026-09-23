import json

from src.report import (
    GATE2_MARGIN,
    forced_table,
    gate2,
    load_forced,
    markdown_table,
    per_episode_agreement,
)


def _step(kind="tool_call", match=True):
    return {
        "reference_kind": kind,
        "predicted_kind": kind,
        "parse_error": None,
        "kind_match": True,
        "name_match": match,
        "normalized_match": match,
        "strict_match": match,
        "hallucinated_tool": False,
        "reference_tools": ("t",),
        "predicted_tools": ("t",),
    }


def _write(path, fingerprint, label, catalog, episodes, *, smoke=False):
    with path.open("a") as fh:
        for i, steps in enumerate(episodes):
            fh.write(
                json.dumps(
                    {
                        "config_fingerprint": fingerprint,
                        "config_name": f"forced:{label}:c{catalog}",
                        "smoke": smoke,
                        "task_id": str(i),
                        "seed": 0,
                        "steps": steps,
                    }
                )
                + "\n"
            )


def test_last_identity_wins_even_when_it_has_fewer_records(tmp_path):
    """A re-score that excludes episodes is newer, not smaller-and-therefore-worse."""
    path = tmp_path / "forced-teacher-c16.jsonl"
    _write(path, "old", "teacher", 16, [[_step()], [_step()], [_step(match=False)]])
    _write(path, "new", "teacher", 16, [[_step()], [_step()]])

    loaded = load_forced(tmp_path, 16)
    assert set(loaded) == {"teacher"}
    assert [r["config_fingerprint"] for r in loaded["teacher"]] == ["new", "new"]


def test_smoke_records_are_kept_apart(tmp_path):
    _write(tmp_path / "forced-a-c16.jsonl", "f", "a", 16, [[_step()]])
    _write(tmp_path / "forced-b-c16.smoke.jsonl", "g", "b", 16, [[_step()]], smoke=True)

    assert set(load_forced(tmp_path, 16)) == {"a"}
    assert set(load_forced(tmp_path, 16, smoke=True)) == {"b"}


def test_per_episode_agreement_ignores_reply_only_episodes():
    records = [
        {"steps": [_step(), _step(match=False)]},  # 0.5
        {"steps": [_step(kind="reply")]},  # no tool calls: contributes nothing
        {"steps": [_step()]},  # 1.0
    ]
    assert per_episode_agreement(records) == [0.5, 1.0]


def test_forced_table_reports_episode_level_ci(tmp_path):
    path = tmp_path / "forced-qwen15b-sft-c16.jsonl"
    _write(path, "f", "qwen15b-sft", 16, [[_step()], [_step(match=False)], [_step()]])

    table = forced_table(tmp_path, 16)
    row = table["labels"]["qwen15b-sft"]
    assert row["n_episodes"] == 3
    assert row["step_agreement"] == round(2 / 3, 4)
    assert row["step_agreement_by_episode"]["n"] == 3
    assert "qwen15b-sft" in markdown_table(table)


def test_gate2_needs_both_arms(tmp_path):
    path = tmp_path / "forced-qwen15b-sft-c16.jsonl"
    _write(path, "f", "qwen15b-sft", 16, [[_step()]])

    verdict = gate2(forced_table(tmp_path, 16), "qwen15b")
    assert verdict["verdict"] == "incomplete"
    assert verdict["missing"] == ["qwen15b-zeroshot"]


def test_gate2_passes_only_on_the_margin(tmp_path):
    zero = tmp_path / "forced-qwen15b-zeroshot-c16.jsonl"
    sft = tmp_path / "forced-qwen15b-sft-c16.jsonl"
    # 2/10 zero-shot against 8/10 fine-tuned: +60 points, comfortably over.
    _write(zero, "a", "qwen15b-zeroshot", 16, [[_step(match=i < 2) for i in range(10)]])
    _write(sft, "b", "qwen15b-sft", 16, [[_step(match=i < 8) for i in range(10)]])

    verdict = gate2(forced_table(tmp_path, 16), "qwen15b")
    assert verdict["verdict"] == "pass"
    assert verdict["delta"] == 0.6

    # Same arms, but a 5-point gap fails.
    for p in (zero, sft):
        p.unlink()
    _write(zero, "a", "qwen15b-zeroshot", 16, [[_step(match=i < 5) for i in range(10)]])
    _write(sft, "b", "qwen15b-sft", 16, [[_step(match=i < 6) for i in range(10)]])

    verdict = gate2(forced_table(tmp_path, 16), "qwen15b")
    assert verdict["verdict"] == "fail"
    assert verdict["delta"] < GATE2_MARGIN
