import json
import time

import pytest

from src.config import load_config
from src.serve_bench import (
    _parse_adapters,
    aggregate,
    block_sizes,
    deal,
    fake_send,
    main,
    read_stream,
    request_body,
    sweep,
)
from src.serving import server_command

CFG = load_config("configs/qwen05b.yaml")


def test_stage_f_options_set_model_prefix_flag_and_served_name():
    cmd = server_command(CFG, None, 4096, model="/q", prefix_caching=False)
    assert cmd[cmd.index("--model") + 1] == "/q"
    assert "--no-enable-prefix-caching" in cmd and "--enable-lora" not in cmd
    assert cmd[-2:] == ["--served-model-name", "student"]
    assert "--enable-prefix-caching" in server_command(CFG, None, 4096, prefix_caching=True)


def test_stage_f_loads_every_adapter_in_one_batch():
    cmd = server_command(CFG, None, 4096, adapters={"sft": "/a", "grpo": "/b"})
    assert cmd[cmd.index("--max-loras") + 1] == "2"
    assert cmd[cmd.index("--lora-modules") + 1 :] == ["sft=/a", "grpo=/b"]
    one = server_command(CFG, None, 4096, adapters={"sft": "/a"})
    assert "--max-loras" not in one and one[-2:] == ["--lora-modules", "sft=/a"]


def test_unset_stage_f_options_add_no_flags():
    for adapter in (None, "/a"):
        assert server_command(CFG, adapter, 4096) == server_command(
            CFG, adapter, 4096, model=None, adapters=None, prefix_caching=None
        )
    assert not any("prefix" in a or "max-loras" in a for a in server_command(CFG, "/a", 4096))


def test_request_body_streams_greedy_and_adds_guided_regex():
    body = request_body(CFG, "p", "grpo", "x|y")
    assert body["model"] == "grpo" and body["stream"] is True and body["temperature"] == 0.0
    assert body["stream_options"] == {"include_usage": True}
    assert body["structured_outputs"] == {"regex": "x|y"}
    assert "structured_outputs" not in request_body(CFG, "p", "student", None)


def _sse(*chunks):
    return [f"data: {json.dumps(c)}".encode() for c in chunks] + [b"", b"data: [DONE]"]


def test_read_stream_times_first_text_and_reads_usage():
    lines = _sse(
        {"choices": [{"text": ""}]},
        {"choices": [{"text": "Hi"}]},
        {"choices": [], "usage": {"completion_tokens": 5}},
    )
    out = read_stream(iter(lines), time.perf_counter())
    assert out["completion_tokens"] == 5 and out["ttft_ms"] is not None
    assert out["latency_ms"] >= out["ttft_ms"]


def test_read_stream_needs_usage():
    with pytest.raises(ValueError, match="usage"):
        read_stream(iter(_sse({"choices": [{"text": "a"}]})), time.perf_counter())


def test_deal_gives_disjoint_blocks_and_refuses_too_few():
    blocks = deal([str(i) for i in range(10)], [2, 3, 4], seed=1)
    flat = [p for b in blocks for p in b]
    assert [len(b) for b in blocks] == [2, 3, 4] and len(set(flat)) == 9
    assert blocks == deal([str(i) for i in range(10)], [2, 3, 4], seed=1)
    with pytest.raises(ValueError, match="distinct prompts"):
        deal(["a"], [1, 1], seed=1)


def test_sweep_never_repeats_a_prompt():
    seen = []

    def send(i, prompt):
        seen.append(prompt)
        return fake_send(i, prompt)

    blocks = deal([str(i) for i in range(40)], block_sizes([1, 4], 3, 1, 2), seed=0)
    levels = sweep(send, iter(blocks), [1, 4], 2)
    assert [lv["concurrency"] for lv in levels] == [1, 4]
    assert len(seen) == 2 * 2 * (3 + 1) == len(set(seen))
    assert all(len(lv["repeats"]) == 2 for lv in levels)


def test_aggregate_mean_sd_and_none():
    out = aggregate([{"a": 1.0, "b": None}, {"a": 3.0, "b": None}])
    assert out == {"a": {"mean": 2.0, "sd": 1.414}, "b": None}


def test_parse_adapters():
    assert _parse_adapters(["sft=org/x"]) == {"sft": "org/x"}
    with pytest.raises(ValueError):
        _parse_adapters(["org/x"])


def test_main_fake_backend_writes_report(tmp_path):
    prompts = tmp_path / "prompts-c16.jsonl"
    prompts.write_text(
        "".join(json.dumps({"step_id": str(i), "prompt": f"p{i}"}) + "\n" for i in range(20))
    )
    out = tmp_path / "bench.json"
    argv = ["--backend", "fake", "--catalog", "16", "--prompts", str(prompts), "--label", "t",
            "--levels", "1", "2", "--per-level", "3", "--warmup", "1", "--repeats", "2",
            "--out", str(out)]  # fmt: skip
    assert main(argv) == 0
    report = json.loads(out.read_text())
    assert report["settings"]["prefix_caching"] is True and len(report["levels"]) == 2
    with pytest.raises(SystemExit):  # 2 x 2 x 4 = 16 needed, then 3 x 2 x 4 = 24 > 20
        main([*argv[:-1], str(out), "--repeats", "3"])
