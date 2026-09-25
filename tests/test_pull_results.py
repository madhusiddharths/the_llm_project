from src.pull_results import EXPECTED_LINES, check


def test_check_flags_short_completions_and_ignores_meta(tmp_path):
    whole = tmp_path / "completions-a-c40.jsonl"
    whole.write_text('{"step_id": "x"}\n' * EXPECTED_LINES)
    short = tmp_path / "completions-b-c40.jsonl"
    short.write_text('{"step_id": "x"}\n' * (EXPECTED_LINES - 1))
    meta = tmp_path / "completions-a-c40.meta.json"
    meta.write_text("{}")

    assert check([whole, meta]) == 0
    assert check([whole, short, meta]) == 1


def test_check_ignores_blank_lines(tmp_path):
    p = tmp_path / "completions-c-c80.jsonl"
    p.write_text('{"step_id": "x"}\n' * EXPECTED_LINES + "\n\n")
    assert check([p]) == 0
