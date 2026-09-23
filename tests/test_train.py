"""train.py on CPU: a tiny random Qwen2 and the real Qwen tokenizer.

Skipped where torch, transformers, peft or the cached tokenizer are missing
(CI installs none of them). Locally they prove the two things that fail
silently on a GPU: the loss mask covers exactly the teacher's decisions, and
the tokens a decision is trained after are the tokens eval will send.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
pytest.importorskip("peft")

from src.config import load_config  # noqa: E402
from src.prompts import _GOLDEN, serialize_episode, serialize_state  # noqa: E402
from src.train import (  # noqa: E402
    IGNORE,
    TokenBoundaryError,
    check_alignment,
    encode_all,
    encode_episode,
    masked_lm_loss,
    train,
    wrap_lora,
)
from src.trajectories import decision_steps  # noqa: E402


@pytest.fixture(scope="module")
def tokenizer():
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "Qwen/Qwen2.5-0.5B-Instruct", local_files_only=True
        )
    except Exception:
        pytest.skip("Qwen tokenizer not in the local HF cache")


def _record():
    segs = serialize_episode(**_GOLDEN)
    return {"task_id": "7", "rep": 0, "segments": [[s.text, s.train] for s in segs]}


def test_labels_cover_exactly_the_teacher_decisions(tokenizer):
    rec = _record()
    enc = encode_episode(tokenizer, rec)
    labelled = tokenizer.decode(
        [t for t, y in zip(enc.input_ids, enc.labels, strict=True) if y != IGNORE]
    )
    targets = "".join(text for text, train in rec["segments"] if train)
    assert labelled == targets
    assert "How can I help" not in labelled  # the hardcoded greeting is context


def test_eval_prompt_tokens_equal_the_training_prefix(tokenizer):
    """Review item 5, on the golden episode: the invariant check passes, and an
    independent re-derivation from serialize_state agrees with it."""
    rec = _record()
    enc = encode_episode(tokenizer, rec)
    check_alignment(tokenizer, rec, enc)
    for step in decision_steps(_GOLDEN["messages"]):
        prompt = serialize_state(
            system=_GOLDEN["system"], tools=_GOLDEN["tools"], messages=step.context
        )
        ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        assert enc.input_ids[: len(ids)] == ids
        assert enc.labels[len(ids)] != IGNORE  # the decision starts right there


def test_a_span_edge_inside_a_token_is_caught(tokenizer):
    rec = _record()
    text, _ = rec["segments"][0]
    rec["segments"][0] = [text[:-3], False]  # move a span edge off a token boundary
    rec["segments"].insert(1, [text[-3:], True])
    with pytest.raises(TokenBoundaryError):
        encode_all(tokenizer, [rec], 10_000)


def test_nothing_is_truncated(tokenizer):
    from src.invariants import InvariantViolation

    with pytest.raises(InvariantViolation, match=r"exceed train\.max_seq_len"):
        encode_all(tokenizer, [_record()], 50)


def _tiny_model(tokenizer):
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
    return transformers.Qwen2ForCausalLM(cfg)


def test_masked_loss_matches_the_full_logits_loss(tokenizer):
    """Gathering hidden states before the head must not change the loss."""
    cfg = load_config("configs/qwen05b.yaml", smoke=True)
    model = wrap_lora(cfg, _tiny_model(tokenizer))
    enc = encode_episode(tokenizer, _record())
    ids, labels = torch.tensor([enc.input_ids]), torch.tensor([enc.labels])
    loss_sum, n = masked_lm_loss(model, ids, labels)
    full = model(input_ids=ids).logits[:, :-1].float()
    ref = torch.nn.functional.cross_entropy(
        full.reshape(-1, full.shape[-1]),
        labels[:, 1:].reshape(-1),
        ignore_index=IGNORE,
        reduction="sum",
    )
    assert n == enc.n_target - (1 if enc.labels[0] != IGNORE else 0)
    assert torch.allclose(loss_sum, ref, rtol=1e-4)


def test_one_step_trains_saves_and_resumes(tokenizer, tmp_path):
    cfg = load_config("configs/qwen05b.yaml", smoke=True)  # 1 epoch, max_steps 1, batch 1
    examples = encode_all(tokenizer, [_record()], cfg.train.max_seq_len)
    model = wrap_lora(cfg, _tiny_model(tokenizer))
    before = {n: p.detach().clone() for n, p in model.named_parameters() if p.requires_grad}
    logs = []
    result = train(cfg, model, examples, tmp_path, log=lambda m, **_: logs.append(m))
    assert result["global_step"] == 1 and result["final_loss"] == result["final_loss"]  # not NaN
    changed = any(
        not torch.equal(before[n], p) for n, p in model.named_parameters() if p.requires_grad
    )
    assert changed, "LoRA weights did not move"
    assert (tmp_path / "checkpoint-epoch-0" / "adapter_model.safetensors").exists()
    assert (tmp_path / "final" / "adapter_config.json").exists()

    # a restart in the same directory resumes past the finished epoch: no new steps
    again = wrap_lora(cfg, _tiny_model(tokenizer))
    resumed = []
    result2 = train(cfg, again, examples, tmp_path, log=lambda m, **_: resumed.append(m))
    assert any("resumed from checkpoint-epoch-0" in m for m in resumed)
    assert result2["global_step"] == 1
    assert not any("step 2" in m for m in resumed)


def test_smoke_accepts_a_manifest_stamped_with_the_real_harvest_fingerprint(tmp_path):
    """SMOKE_OVERRIDES move teacher_fingerprint(), but build_sft.py stamps every
    manifest with the NON-smoke identity. Comparing the smoke config's own
    fingerprint rejected every manifest that exists; --smoke was unusable."""
    import hashlib
    import json

    from src.config import load_config
    from src.train import load_sft

    real = load_config("configs/qwen05b.yaml")
    smoke = load_config("configs/qwen05b.yaml", smoke=True)
    assert real.teacher_fingerprint() != smoke.teacher_fingerprint(), "premise of this test"

    data = tmp_path / "train.smoke.jsonl"
    data.write_text('{"task_id": "0", "rep": 0, "segments": []}\n')
    (tmp_path / "manifest.smoke.json").write_text(
        json.dumps(
            {
                "file": data.name,
                "file_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                "teacher_fingerprint": real.teacher_fingerprint(),
                "prompt_template_hash": smoke.prompt_template_hash,
                "system_prompt_hash": smoke.system_prompt_hash,
                "catalog_hash": smoke.eval.catalog_hashes[16],
            }
        )
    )

    records, _ = load_sft(tmp_path, smoke, harvest_fingerprint=real.teacher_fingerprint())
    assert len(records) == 1
