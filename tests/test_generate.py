import pytest


@pytest.mark.parametrize(
    "extra",
    [
        ["--backend", "mlx", "--model", "m"],
        ["--backend", "hf", "--guided-catalog", "16"],
        ["--model", "m", "--adapter", "a"],
    ],
)
def test_generate_refuses_stage_f_flags_off_vllm_or_with_an_adapter(tmp_path, extra):
    from src.generate import main as generate_main

    with pytest.raises(SystemExit):
        generate_main(["--prompts", "p.jsonl", "--out", str(tmp_path / "o.jsonl"), *extra])
