"""The student behind a vLLM OpenAI-compatible server: launch command, request
bodies, response parsing and an HTTP client.

Shared by eval_free.py (Stage A/D, which starts the server itself) and the
router service (router_service.py, where docker compose or a notebook starts
it). Requests go through urllib, NOT litellm: the throttle counts every litellm
call against the daily OpenRouter quota, and the student is local.

Pure Python at import time (tests/test_core_is_light.py).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.logprobs import StepConfidence, score_completion

# Candidates kept at the first position, where p(<tool_call>) is read
# (logprobs.py, choice 5); generate.py --logprobs records the same number.
FIRST_TOP_K = 20

SERVER_PORT = 8765
SERVED_NAME = "student"  # the name the adapter (or the bare model) is served under


def server_command(
    cfg,
    adapter_path: str | None,
    max_model_len: int,
    port: int = SERVER_PORT,
    logprobs: bool = False,
) -> list[str]:
    """`vllm serve` with VLLMPolicy's engine settings, so the two differ only in
    how requests arrive: one at a time in-process, or several at once over HTTP.
    With `logprobs`, the engine reports raw log-probs, as generate.py --logprobs
    does in-process (the model's own, before temperature or penalties)."""
    cmd = [
        sys.executable, "-m", "vllm.entrypoints.openai.api_server",
        "--model", cfg.model.base_model,
        "--dtype", "half",
        "--max-model-len", str(max_model_len),
        "--gpu-memory-utilization", "0.9",
        "--seed", str(cfg.seed),
        "--port", str(port),
    ]  # fmt: skip
    if logprobs:
        cmd += ["--logprobs-mode", "raw_logprobs"]
    if adapter_path is None:
        cmd += ["--served-model-name", SERVED_NAME]
    else:
        cmd += [
            "--enable-lora",
            "--max-lora-rank", str(cfg.train.lora_r),
            "--lora-modules", f"{SERVED_NAME}={adapter_path}",
        ]  # fmt: skip
    return cmd


def completion_request(cfg, prompt: str, logprobs: bool = False) -> dict[str, Any]:
    """The /v1/completions body: VLLMPolicy's SamplingParams, field for field.
    With `logprobs`, also generate.py's FIRST_TOP_K alternatives per position,
    keyed by token id (vLLM's `return_tokens_as_token_ids`) so nothing has to
    re-tokenize a string."""
    from src.prompts import IM_END

    body: dict[str, Any] = {
        "model": SERVED_NAME,
        "prompt": prompt,
        "temperature": 0.0,
        "max_tokens": cfg.teacher.max_tokens,
        "stop": [IM_END],
        "seed": cfg.seed,
    }
    if logprobs:
        body |= {"logprobs": FIRST_TOP_K, "return_tokens_as_token_ids": True}
    return body


def completion_text(body: Mapping[str, Any]) -> str:
    return str(_only_choice(body).get("text") or "")


def _only_choice(body: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = body.get("choices") or []
    if len(choices) != 1:
        raise ValueError(f"expected one completion, got {len(choices)}: {body}")
    return choices[0]


def _token_id(key: str) -> int:
    """vLLM renders a token as "token_id:<n>" under return_tokens_as_token_ids."""
    prefix, _, n = key.partition(":")
    if prefix != "token_id" or not n.lstrip("-").isdigit():
        raise ValueError(
            f"token {key!r} is not 'token_id:<n>'; was return_tokens_as_token_ids set?"
        )
    return int(n)


@dataclass(frozen=True)
class ScoredCompletion:
    """One completion with what src/logprobs.score_completion needs."""

    text: str
    token_ids: list[int]
    token_logprobs: list[float]
    first_alternatives: dict[int, float]  # token id -> log-prob, top-k at position 0


def scored_completion(body: Mapping[str, Any]) -> ScoredCompletion:
    """A /v1/completions response requested with completion_request(logprobs=True)."""
    choice = _only_choice(body)
    lp = choice.get("logprobs") or {}
    tokens, values = lp.get("tokens") or [], lp.get("token_logprobs") or []
    if len(tokens) != len(values):
        raise ValueError(f"{len(tokens)} tokens but {len(values)} log-probs")
    tops = lp.get("top_logprobs") or []
    first = {_token_id(k): float(v) for k, v in (tops[0] or {}).items()} if tops else {}
    return ScoredCompletion(
        text=str(choice.get("text") or ""),
        token_ids=[_token_id(t) for t in tokens],
        token_logprobs=[float(v) for v in values],
        first_alternatives=first,
    )


class StudentClient:
    """HTTP client for a running student server. `decode` and `call_token_id`
    come from the base model's tokenizer (logprobs.hf_decoder), and `count`
    tokenizes a prompt for the context check; both are passed in so this module
    stays free of transformers."""

    def __init__(
        self,
        cfg,
        url: str,
        *,
        decode: Callable[[Sequence[int]], str],
        call_token_id: int,
        count: Callable[[str], int],
        max_model_len: int,
        timeout_s: float = 600.0,
    ):
        self.cfg = cfg
        self.url = url.rstrip("/")
        self.decode = decode
        self.call_token_id = call_token_id
        self.count = count
        self.limit = max_model_len - cfg.teacher.max_tokens
        self.timeout_s = timeout_s

    @classmethod
    def for_model(cls, cfg, url: str, max_model_len: int, **kwargs) -> StudentClient:
        """With the base model's tokenizer (needs transformers)."""
        from transformers import AutoTokenizer

        from src.logprobs import TOOL_CALL_TOKEN, hf_decoder

        tok = AutoTokenizer.from_pretrained(cfg.model.base_model)
        return cls(
            cfg,
            url,
            decode=hf_decoder(tok),
            call_token_id=tok.convert_tokens_to_ids(TOOL_CALL_TOKEN),
            count=lambda p: len(tok(p, add_special_tokens=False)["input_ids"]),
            max_model_len=max_model_len,
            **kwargs,
        )

    def fits(self, prompt: str) -> bool:
        return self.count(prompt) <= self.limit

    def post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v1/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read())

    def complete(self, prompt: str) -> str:
        return completion_text(self.post(completion_request(self.cfg, prompt)))

    def score(self, prompt: str) -> tuple[str, StepConfidence]:
        """The completion and its step confidence (src/logprobs.py)."""
        out = scored_completion(self.post(completion_request(self.cfg, prompt, logprobs=True)))
        conf = score_completion(
            out.token_ids,
            out.token_logprobs,
            self.decode,
            first_alternatives=out.first_alternatives or None,
            call_token_id=self.call_token_id,
        )
        return out.text, conf


def wait_ready(
    url: str, timeout_s: float = 900.0, alive: Callable[[], bool] = lambda: True
) -> None:
    """Block until the server's /health answers 200. `alive` lets a caller that
    owns the process fail fast when it exits."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not alive():
            raise RuntimeError(f"the server behind {url} exited before it was ready")
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(5)
    raise TimeoutError(f"{url} not ready after {timeout_s:.0f}s")
