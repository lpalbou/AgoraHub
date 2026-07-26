"""In-process fake OpenAI-compatible embeddings endpoint (test fixture,
specified by the ops cycle-3 report): stdlib only, deterministic
seeded-hash vectors (seed swap = imposter model behind the same name),
knobs for latency / fail-next / hang. Runs on a random localhost port."""
from __future__ import annotations

import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def seeded_vector(text: str, seed: str, dim: int = 32) -> list[float]:
    """Deterministic pseudo-embedding: same (text, seed) = same vector;
    different seed = a different geometry (the imposter-model drill)."""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{seed}|{text}|{counter}".encode()).digest()
        for i in range(0, len(h) - 3, 4):
            (u,) = struct.unpack("<I", h[i:i + 4])
            out.append((u / 2**32) * 2.0 - 1.0)
            if len(out) == dim:
                break
        counter += 1
    return out


class FakeEmbedServer:
    def __init__(self, *, seed: str = "model-a", dim: int = 32) -> None:
        self.seed = seed
        self.dim = dim
        self.latency = 0.0          # seconds added to every response
        self.fail_next = 0          # next N requests return 500
        self.hang_next = 0          # next N requests never respond
        self.requests: list[dict] = []   # request log (batch/instruction asserts)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib API)
                import time as _t
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fixture.requests.append(body)
                if fixture.hang_next > 0:
                    fixture.hang_next -= 1
                    # Longer than any client budget, SHORT enough that the
                    # daemon handler thread drains at teardown (a 3600s
                    # sleep in a single-threaded server wedged the suite —
                    # the fixture reproduced the bug class it exists for).
                    _t.sleep(8)
                if fixture.latency:
                    _t.sleep(fixture.latency)
                if fixture.fail_next > 0:
                    fixture.fail_next -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                inputs = body.get("input", [])
                if isinstance(inputs, str):
                    inputs = [inputs]
                data = [{"index": i,
                         "embedding": seeded_vector(t, fixture.seed, fixture.dim)}
                        for i, t in enumerate(inputs)]
                payload = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a) -> None:   # quiet
                pass

        # Threading server: a hung/slow handler must never block the next
        # request or the shutdown (daemon threads die with the process).
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> "FakeEmbedServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
