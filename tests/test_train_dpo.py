"""train_dpo.py: the loss algebra everywhere; the loop on CPU with a tiny Qwen2.

The factorised backward (one sequence in memory at a time) must give exactly
the gradient of the joint DPO loss; that is the claim the T4 memory budget rests on.
"""

from __future__ import annotations

import json
import math

import pytest

from src.config import load_config
from src.invariants import InvariantViolation
from src.train_dpo import dpo_terms, load_pairs


def test_dpo_terms_match_the_definition_and_its_derivative():
    beta, rc, rr = 0.1, -5.0, -7.0
    loss, coef_c, coef_r, z = dpo_terms(beta, -4.0, -9.0, rc, rr)
    assert z == pytest.approx(beta * ((-4.0 - rc) - (-9.0 - rr)))
    assert loss == pytest.approx(-math.log(1 / (1 + math.exp(-z))))
    eps = 1e-6
    numeric = (dpo_terms(beta, -4.0 + eps, -9.0, rc, rr)[0] - loss) / eps
    assert coef_c == pytest.approx(numeric, rel=1e-4)
    assert coef_r == pytest.approx(-coef_c)
    assert dpo_terms(beta, 0, 0, 0, 0)[0] == pytest.approx(math.log(2))


def test_dpo_terms_never_overflow():
    for pc in (-1e6, 1e6):
        loss, coef_c, _, _ = dpo_terms(1.0, pc, 0.0, 0.0, 0.0)
        assert math.isfinite(loss) and math.isfinite(coef_c)


def _pairs_dir(tmp_path, **manifest_overrides):
    from src.hashing import hash_file

    cfg = load_config("configs/v2/qwen15b-dpo.yaml")
    (tmp_path / "pairs.jsonl").write_text(json.dumps({"pair_id": "1:0:2:0", "task_id": "1"}) + "\n")
    manifest = {
        "file": "pairs.jsonl",
        "file_sha256": hash_file(tmp_path / "pairs.jsonl"),
        "prompt_template_hash": cfg.prompt_template_hash,
        "system_prompt_hash": cfg.system_prompt_hash,
        "catalog_hash": cfg.eval.catalog_hashes[16],
        "teacher_fingerprint": cfg.teacher_fingerprint(),
        "executed": True,
        **manifest_overrides,
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return cfg


def test_load_pairs_accepts_a_matching_manifest(tmp_path):
    cfg = _pairs_dir(tmp_path)
    pairs, _ = load_pairs(tmp_path, cfg, harvest_fingerprint=cfg.teacher_fingerprint())
    assert len(pairs) == 1


def test_load_pairs_refuses_pairs_built_without_the_equivalence_check(tmp_path):
    cfg = _pairs_dir(tmp_path, executed=False)
    with pytest.raises(InvariantViolation, match="no-execute"):
        load_pairs(tmp_path, cfg, harvest_fingerprint=cfg.teacher_fingerprint())


def test_load_pairs_refuses_a_different_serializer(tmp_path):
    cfg = _pairs_dir(tmp_path, prompt_template_hash="0" * 64)
    with pytest.raises(InvariantViolation, match="prompt_template_hash"):
        load_pairs(tmp_path, cfg, harvest_fingerprint=cfg.teacher_fingerprint())


# --- the loop, on CPU ----------------------------------------------------------

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")
pytest.importorskip("peft")


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
    # LoRA B starts at zero, which would make every gradient below trivially agree.
    with torch.no_grad():
        for n, p in model.named_parameters():
            if "lora_B" in n:
                p.normal_(0, 0.02)
    return model


def _pair(pid="1:0:2:0"):
    prompt = "<|im_start|>system\nYou are a retail agent.<|im_end|>\n<|im_start|>user\ncancel W1<|im_end|>\n<|im_start|>assistant\n"
    return {
        "pair_id": pid,
        "task_id": "1",
        "prompt": prompt,
        "chosen": '<tool_call>\n{"name": "cancel_order", "arguments": {"order_id": "W1"}}\n</tool_call><|im_end|>',
        "rejected": "Sure, anything else?<|im_end|>",
    }


def test_the_factorised_backward_equals_the_joint_loss_gradient(tokenizer):
    from src.train_dpo import encode_pair, sequence_logp

    model = _tiny_policy(tokenizer)
    p = encode_pair(tokenizer, _pair())
    dev = torch.device("cpu")
    beta, rc, rr = 0.5, -3.0, -4.0
    params = [q for q in model.parameters() if q.requires_grad]

    pc, pr = sequence_logp(model, p.chosen, dev), sequence_logp(model, p.rejected, dev)
    joint = -torch.nn.functional.logsigmoid(beta * ((pc - rc) - (pr - rr)))
    want = torch.autograd.grad(joint, params)

    model.zero_grad()
    _, coef_c, coef_r, _ = dpo_terms(beta, float(pc), float(pr), rc, rr)
    (sequence_logp(model, p.chosen, dev) * coef_c).backward()
    (sequence_logp(model, p.rejected, dev) * coef_r).backward()
    for w, q in zip(want, params, strict=True):
        assert torch.allclose(w, q.grad, atol=1e-6, rtol=1e-4)


def test_one_dpo_step_moves_the_adapter_saves_and_resumes(tokenizer, tmp_path):
    from src.train_dpo import encode_pairs, reference_logps, train

    cfg = load_config("configs/v2/qwen05b-dpo.yaml", smoke=True)  # 1 epoch, 1 step, 1 pair
    model = _tiny_policy(tokenizer)
    pairs, skipped = encode_pairs(tokenizer, [_pair()], cfg.train.max_seq_len)
    assert len(pairs) == 1 and skipped == {"token_boundary": 0, "too_long": 0}
    ref = reference_logps(
        model, pairs, torch.device("cpu"), tmp_path / "ref.json", lambda *a, **k: None
    )
    before = {n: q.detach().clone() for n, q in model.named_parameters() if q.requires_grad}
    logs = []
    result = train(cfg, model, pairs, ref, tmp_path, log=lambda m, **_: logs.append(m))
    assert result["global_step"] == 1
    assert result["final"]["loss"] == pytest.approx(
        math.log(2), abs=1e-4
    )  # policy == reference at step 0
    assert any(
        not torch.equal(before[n], q) for n, q in model.named_parameters() if q.requires_grad
    )
    assert (tmp_path / "final" / "adapter_config.json").exists()

    again = _tiny_policy(tokenizer)
    resumed = []
    assert (
        train(cfg, again, pairs, ref, tmp_path, log=lambda m, **_: resumed.append(m))["global_step"]
        == 1
    )
    assert any("resumed from checkpoint-epoch-0" in m for m in resumed)
