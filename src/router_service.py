"""The router as an HTTP service (V1-18): `docker compose up` runs this next to a
vLLM server holding the student (plan §6 week 4).

    POST /route   {"messages": [...]}  or  {"prompt": "<rendered>"}
                  -> {"action", "escalated", "answered_by", "route", "confidence",
                      "student_ms", "teacher_ms", "total_ms", ...}
    GET  /health  -> 200 once the student server answers

`messages` is the serializer's dict shape (src/prompts.py): role, content and,
for assistant turns, tool_calls [{"name", "arguments"}]. The routing is
router_node.build_router; this file is only transport.

Standard-library HTTP (ThreadingHTTPServer): one thread per request, and the
student server batches whatever arrives at once, which is what the load test
measures. No web framework to install in the image.

    python src/router_service.py --config configs/qwen15b.yaml --catalog 80 \\
        --student-url http://127.0.0.1:8765 --threshold 0.7
    # the load test: escalations are reported, never sent (no API quota)
    python src/router_service.py --config configs/qwen15b.yaml --catalog 80 \\
        --student-url http://127.0.0.1:8765 --threshold 0.7 --no-teacher
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROUTER_PORT = 8080
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def make_handler(
    route_fn: Callable[[Mapping[str, Any]], dict[str, Any]], healthy: Callable[[], bool]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: Mapping[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._send(404, {"error": "not found"})
            elif healthy():
                self._send(200, {"status": "ok"})
            else:
                self._send(503, {"status": "student server not ready"})

        def do_POST(self) -> None:
            if self.path != "/route":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(request, dict):
                    raise ValueError("the body must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                self._send(200, route_fn(request))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # the student or teacher failed; say so, stay up
                self._send(502, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *args: Any) -> None:  # one line per request is noise
            pass

    return Handler


def serve(
    route_fn: Callable[[Mapping[str, Any]], dict[str, Any]],
    healthy: Callable[[], bool],
    host: str = "0.0.0.0",
    port: int = ROUTER_PORT,
) -> ThreadingHTTPServer:
    """A started-but-not-running server; call serve_forever() (or run it in a thread)."""
    server = ThreadingHTTPServer((host, port), make_handler(route_fn, healthy))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    from src.catalogs import load_catalog, load_system_prompt
    from src.cli import build_parser, resolve
    from src.router_node import build_router, openai_teacher, route
    from src.serving import StudentClient

    parser = build_parser("Router service: student with log-prob escalation")
    parser.add_argument("--catalog", type=int, default=80)
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument(
        "--student-url", default=os.environ.get("STUDENT_URL", "http://127.0.0.1:8765")
    )
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=ROUTER_PORT)
    parser.add_argument(
        "--no-teacher",
        action="store_true",
        default=os.environ.get("NO_TEACHER", "") not in ("", "0"),
        help="report escalations without calling the teacher (load test; no API quota; env NO_TEACHER=1)",
    )
    args = parser.parse_args(argv)
    cfg = resolve(args)

    system, catalog = load_system_prompt(cfg), load_catalog(cfg, args.catalog)
    student = StudentClient.for_model(cfg, args.student_url, args.max_model_len)
    teacher = None
    if not args.no_teacher:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            parser.error("OPENROUTER_API_KEY is not set (or pass --no-teacher)")
        teacher = openai_teacher(
            model=cfg.teacher.model,
            base_url=OPENROUTER_URL,
            api_key=key,
            system=system,
            catalog=catalog,
            max_tokens=cfg.teacher.max_tokens,
        )
    app = build_router(student, teacher, threshold=args.threshold, system=system, catalog=catalog)

    def healthy() -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{student.url}/health", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    server = serve(lambda req: route(app, req), healthy, args.host, args.port)
    teacher_note = "off (escalations reported only)" if teacher is None else cfg.teacher.model
    print(
        f"[router_service] catalog {args.catalog}, threshold {args.threshold}, "
        f"student {args.student_url}, teacher {teacher_note}; listening on :{args.port}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
