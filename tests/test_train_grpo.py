"""train_grpo.py: the pure parts everywhere; sampling and the loop on CPU with a tiny Qwen2."""

from __future__ import annotations

import json
import math

import pytest

from src.config import load_config
from src.invariants import InvariantViolation
from src.make_pairs import State, state_record
from src.train_grpo import batch_for, group_advantages, to_state
from src.trajectories import Action, ToolCall


def test_equal_rewards_have_no_advantage():
    assert group_advantages([0.4, 0.4, 0.4]) is None
    adv = group_advantages([1.0, 0.0, 0.0, 1.0])
    assert adv is not None and sum(adv) == pytest.approx(0.0)
    assert adv[0] == pytest.approx(1.0, abs=1e-3) and adv[1] == pytest.approx(-1.0, abs=1e-3)


def test_batches_are_seeded_and_cover_every_state_each_pass():
    first_pass = [i for step in range(5) for i in batch_for(step, 10, 2, seed=3)]
    assert sorted(first_pass) == list(range(10))
    assert batch_for(7, 10, 2, seed=3) == batch_for(7, 10, 2, seed=3)
    second_pass = [i for step in range(5, 10) for i in batch_for(step, 10, 2, seed=3)]
    assert sorted(second_pass) == list(range(10)) and second_pass != first_pass  # reshuffled


def test_an_exported_state_round_trips_with_its_replay_calls():
    ctx = (
        {"role": "assistant", "content": "Hi! How can I help you today?"},
        {"role": "user", "content": "cancel W1"},
        {
            "role": "assistant",
            "tool_calls": [{"name": "get_order", "arguments": {"order_id": "W1"}}],
        },
        {"role": "tool", "content": "W1:pending"},
    )
    ref = Action(
        kind="tool_call", calls=(ToolCall(name="cancel_order", arguments={"order_id": "W1"}),)
    )
    g = to_state(json.loads(json.dumps(state_record(State("5:0:4", "5", 0, 4, "P", ctx, ref)))))
    assert g.reference == ref and g.prompt == "P"
    from src.make_pairs import prior_calls

    assert prior_calls(g.context) == prior_calls(ctx)


# --- the loop, on CPU ----------------------------------------------------------

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
pytest.importorskip("peft")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]


def fake_run(prefix, calls):
    return [f"{c.name}:{c.arguments}" for c in calls], "db"


@pytest.fixture(scope="module")
def tokenizer():
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "Qwen/Qwen2.5-0.5B-Instruct", local_files_only=True
        )
    except Exception:
        pytest.skip("Qwen tokenizer not in the local HF cache")


def _tiny_policy(tokenizer):
    from src.train import wrap_lora

    cfg = transformers.Qwen2Config(
        vocab_size=len(tokenizer),
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=4096,
        tie_word_embeddings=True,
    )
    torch.manual_seed(0)
    model = wrap_lora(
        load_config("configs/qwen05b.yaml", smoke=True), transformers.Qwen2ForCausalLM(cfg)
    )
    for m in model.modules():
        if isinstance(m, torch.nn.Dropout):
            m.p = 0.0
    with torch.no_grad():
        for n, p in model.named_parameters():
            if "lora_B" in n:
                p.normal_(0, 0.02)
    return model


PROMPT = "<|im_start|>system\nYou are a retail agent.<|im_end|>\n<|im_start|>user\ncancel W1<|im_end|>\n<|im_start|>assistant\n"
CANCEL = Action(
    kind="tool_call", calls=(ToolCall(name="cancel_order", arguments={"order_id": "W1"}),)
)
GOOD = (
    '<tool_call>\n{"name": "cancel_order", "arguments": {"order_id": "W1"}}\n</tool_call><|im_end|>'
)
BAD = "Sure, anything else?<|im_end|>"


def _sampler(texts):
    def sample(model, tokenizer, state, cfg, device, seed):
        p = tokenizer(state.prompt, add_special_tokens=False).input_ids
        return [(p, tokenizer(t, add_special_tokens=False).input_ids, t) for t in texts]

    return sample


def _state():
    from src.train_grpo import GState

    return GState("1:0:2", "1", PROMPT, CANCEL, ())


def test_the_kl_term_is_zero_at_the_reference_and_its_gradient_is_the_policy_gradient(tokenizer):
    from src.train import IGNORE, Encoded
    from src.train_grpo import completion_loss, token_logps

    model = _tiny_policy(tokenizer)
    p = tokenizer(PROMPT, add_special_tokens=False).input_ids
    gen = tokenizer(GOOD, add_special_tokens=False).input_ids
    enc = Encoded("k", p + gen, [IGNORE] * len(p) + gen)
    dev = torch.device("cpu")
    params = [q for q in model.parameters() if q.requires_grad]
    with torch.no_grad():
        ref = token_logps(model, enc, dev)
    pol = token_logps(model, enc, dev)
    assert len(pol) == len(gen)
    loss, kl = completion_loss(pol, ref, 0.7, 0.04)
    assert float(kl) == pytest.approx(0.0, abs=1e-7)
    got = torch.autograd.grad(loss, params)
    want = torch.autograd.grad(-0.7 * token_logps(model, enc, dev).mean(), params)
    for g, w in zip(got, want, strict=True):
        assert torch.allclose(g, w, atol=1e-6, rtol=1e-4)


def test_sampling_returns_a_group_of_stopped_or_capped_completions(tokenizer):
    from src.train_grpo import sample_group

    cfg = load_config("configs/v2/qwen05b-grpo.yaml", smoke=True)  # 2 samples, 32 tokens
    model = _tiny_policy(tokenizer)
    out = sample_group(model, tokenizer, _state(), cfg, torch.device("cpu"), seed=1)
    assert len(out) == 2 and all(len(gen) <= 32 for _, gen, _ in out)
    again = sample_group(model, tokenizer, _state(), cfg, torch.device("cpu"), seed=1)
    assert [g for _, g, _ in out] == [g for _, g, _ in again]  # seeded (choice 6)
    assert model.training


def test_one_step_moves_the_adapter_toward_the_better_sample_and_resumes(tokenizer, tmp_path):
    from src.train_grpo import train

    cfg = load_config("configs/v2/qwen05b-grpo.yaml", smoke=True)
    model = _tiny_policy(tokenizer)
    before = {n: q.detach().clone() for n, q in model.named_parameters() if q.requires_grad}
    logs = []
    result = train(
        cfg,
        model,
        tokenizer,
        [_state()],
        TOOLS,
        fake_run,
        tmp_path,
        log=lambda m, **_: logs.append(m),
        sampler=_sampler([GOOD, BAD]),
    )
    assert result["global_step"] == 1 and result["final"]["reward"] == pytest.approx(0.5)
    assert result["final"]["zero_std_groups"] == 0.0
    assert any(
        not torch.equal(before[n], q) for n, q in model.named_parameters() if q.requires_grad
    )
    assert (tmp_path / "checkpoint-step-1" / "trainer_state.pt").exists()

    resumed = []
    again = train(
        cfg,
        _tiny_policy(tokenizer),
        tokenizer,
        [_state()],
        TOOLS,
        fake_run,
        tmp_path,
        log=lambda m, **_: resumed.append(m),
        sampler=_sampler([GOOD, BAD]),
    )
    assert again["global_step"] == 1 and any("resumed from checkpoint-step-1" in m for m in resumed)

    other = load_config("configs/v2/qwen15b-grpo.yaml", smoke=True)
    with pytest.raises(InvariantViolation, match="another config"):
        train(
            other,
            _tiny_policy(tokenizer),
            tokenizer,
            [_state()],
            TOOLS,
            fake_run,
            tmp_path,
            log=lambda *a, **k: None,
            sampler=_sampler([GOOD, BAD]),
        )


def test_a_group_with_equal_rewards_does_not_move_the_adapter(tokenizer, tmp_path):
    from src.train_grpo import train

    cfg = load_config("configs/v2/qwen05b-grpo.yaml", smoke=True)
    model = _tiny_policy(tokenizer)
    before = {n: q.detach().clone() for n, q in model.named_parameters() if q.requires_grad}
    result = train(
        cfg,
        model,
        tokenizer,
        [_state()],
        TOOLS,
        fake_run,
        tmp_path,
        log=lambda *a, **k: None,
        sampler=_sampler([GOOD, GOOD]),
    )
    assert result["final"]["zero_std_groups"] == 1.0 and math.isclose(result["final"]["kl"], 0.0)
    assert result["final"]["backward_seqs"] == 0
    assert all(torch.equal(before[n], q) for n, q in model.named_parameters() if q.requires_grad)


def test_the_smoke_runs_a_backward_on_a_tied_group_without_moving_the_adapter(tokenizer, tmp_path):
    # The smoke is a memory check on the longest prompt; a tie must not skip it.
    from src.train_grpo import train

    cfg = load_config("configs/v2/qwen05b-grpo.yaml", smoke=True)
    model = _tiny_policy(tokenizer)
    before = {n: q.detach().clone() for n, q in model.named_parameters() if q.requires_grad}
    result = train(
        cfg,
        model,
        tokenizer,
        [_state()],
        TOOLS,
        fake_run,
        tmp_path,
        log=lambda *a, **k: None,
        sampler=_sampler([GOOD, GOOD]),
        backward_on_ties=True,
    )
    assert result["final"]["zero_std_groups"] == 1.0
    assert result["final"]["backward_seqs"] == 2
    assert all(torch.equal(before[n], q) for n, q in model.named_parameters() if q.requires_grad)
