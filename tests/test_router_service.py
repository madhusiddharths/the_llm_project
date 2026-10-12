"""The router over HTTP, and the load test against it, with a fake student."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from src.load_test import percentile, run_level, summarize


@pytest.fixture
def service():
    """A real ThreadingHTTPServer on a free port, routing with a fake student."""
    pytest.importorskip("langgraph")
    from src.logprobs import StepConfidence
    from src.router_node import build_router, route
    from src.router_service import serve

    class Student:
        def fits(self, prompt):
            return True

        def score(self, prompt):
            s = 0.9 if "sure" in prompt else 0.2
            return "Hi.", StepConfidence("reply", (), None, None, 0, 1 - s, False, s, s)

    app = build_router(Student(), None, threshold=0.7, system="S", catalog=[])
    server = serve(lambda req: route(app, req), lambda: True, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url, body):
    req = urllib.request.Request(
        f"{url}/route", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def test_a_route_request_round_trips(service):
    status, body = _post(service, {"prompt": "I am sure"})
    assert status == 200 and body["answered_by"] == "student" and body["action"]["content"] == "Hi."
    status, body = _post(service, {"prompt": "hmm"})
    assert body["escalated"] and body["answered_by"] == "nobody"
    with urllib.request.urlopen(f"{service}/health", timeout=5) as r:
        assert r.status == 200


def test_a_bad_request_is_a_400_and_the_service_stays_up(service):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(service, {"text": "no messages"})
    assert err.value.code == 400
    assert _post(service, {"prompt": "I am sure"})[0] == 200


def test_the_load_test_measures_every_request_at_each_level(service):
    from src.load_test import post_route

    prompts = ["I am sure", "hmm"] * 8
    row = run_level(lambda p: post_route(service, p), prompts, concurrency=4)
    assert row["concurrency"] == 4 and row["requests"] == 16 and row["errors"] == 0
    assert row["escalation_rate"] == 0.5 and row["p99_ms"] >= row["p50_ms"] > 0


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 50) == 50 and percentile(values, 99) == 99
    assert percentile(values, 100) == 100 and percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


def test_failed_requests_are_counted_not_timed():
    s = summarize([10.0, 20.0], wall_s=1.0, errors=3)
    assert s["requests"] == 5 and s["errors"] == 3 and s["throughput_rps"] == 2.0


def test_compose_starts_the_student_exactly_as_server_command_does():
    """compose.yaml spells the vLLM flags out; they must not drift from serving.py."""
    import re
    from pathlib import Path

    import yaml

    from src.config import load_config
    from src.serving import server_command

    text = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", Path("compose.yaml").read_text())
    compose = yaml.safe_load(text)
    args = compose["services"]["student"]["command"]
    adapter = "madhusiddharths1/tool-router-qwen15b-sft"
    want = server_command(load_config("configs/qwen15b.yaml"), adapter, 32768, 8765, logprobs=True)
    assert args == want[3:]  # past `python -m vllm.entrypoints.openai.api_server`
    router = compose["services"]["router"]["command"]
    assert router[router.index("--student-url") + 1] == "http://student:8765"
