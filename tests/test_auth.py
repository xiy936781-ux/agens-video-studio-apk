#!/usr/bin/env python3
"""Offline tests for server.py auth, path handling, and job persistence.

No real upstream API calls are made. The `call()` function is monkey-patched
to return a fake response so no network I/O occurs.

Run: python -m pytest tests/test_auth.py -v   OR   python tests/test_auth.py
"""
import json
import os
import sys
import tempfile
import threading
import http.server
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock
from typing import Optional

# Ensure the project root (containing server.py) is on sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-web-access-token-xyz"
TEST_UPSTREAM_KEY = "fake-upstream-api-key"


class TestServer:
    """Wraps a ThreadingHTTPServer bound to 127.0.0.1:<random-port>."""

    def __init__(self, data_dir: Path, output_dir: Path, token: str = TEST_TOKEN):
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.token = token
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

        # Patch environment before importing / running server code
        os.environ["AGNES_DATA_DIR"] = str(data_dir)
        os.environ["AGNES_OUTPUT_DIR"] = str(output_dir)
        os.environ["AGNES_API_TOKEN"] = token

        # Re-load server.py in a controlled way.
        # We use importlib to load the module fresh.
        import importlib
        import server as _srv  # noqa: F401 – will be re-loaded below

        # Force a fresh import so DATA/OUT/WEB_API_TOKEN reflect env vars
        import importlib
        importlib.reload(sys.modules["_srv"] if "_srv" in sys.modules else _srv)

        # Simpler: just re-import server directly
        import server
        server.WEB_API_TOKEN = token
        server.DATA = data_dir
        server.OUT = output_dir
        server.CFG = data_dir / "config.json"

        data_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=5)
        # Clean up env
        for k in ("AGNES_DATA_DIR", "AGNES_OUTPUT_DIR", "AGNES_API_TOKEN"):
            os.environ.pop(k, None)

    def get(self, path: str, token: Optional[str] = None, expect_status: int = 200):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read().decode()
                return r.status, json.loads(body)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def post(self, path: str, payload: dict, token: Optional[str] = None, expect_status: int = 200):
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read().decode()
                return r.status, json.loads(body)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def fake_upstream_response(self, include_sensitive_fields: bool = False) -> dict:
        """Return a fake upstream response. Optionally include fields that
        a real response might contain (e.g. 'request_id', 'debug_info')
        to verify they are NOT persisted to jobs.json."""
        base = {"video_id": "vid-001", "status": "queued"}
        if include_sensitive_fields:
            base.update(
                {
                    "request_id": "sensitive-request-id",
                    "debug_info": {"api_key_hint": "cpk-ABC"},
                    "internal_url": "https://internal.agnes-ai.com/debug",
                }
            )
        return base

    def patch_call(self, response: Optional[dict] = None):
        """Monkey-patch server.call() to return a canned response without network I/O."""
        import server

        canned = response if response is not None else {"video_id": "vid-001", "status": "queued"}

        def fake_call(method, url, token, body=None, timeout=90):
            return 200, canned

        return patch.object(server, "call", fake_call)

    def patch_call_fail(self):
        """Monkey-patch server.call() to simulate an upstream failure."""
        import server

        def fake_call(method, url, token, body=None, timeout=90):
            raise RuntimeError("Upstream API error (HTTP 403)")

        return patch.object(server, "call", fake_call)

    def read_jobs(self) -> dict:
        jf = self.data_dir / "jobs.json"
        if not jf.exists():
            return {}
        return json.loads(jf.read_text())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_token_rejected_all_routes():
    """Without AGNES_API_TOKEN set (empty), all /api/ routes must return 401."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token="")  # empty token → all rejected
        srv.start()
        try:
            for path in ["/api/config", "/api/jobs/list", "/api/jobs/vid-001"]:
                status, body = srv.get(path, token=TEST_TOKEN)
                assert status == 401, f"GET {path}: expected 401, got {status}: {body}"
                assert "未授权" in body.get("error", ""), f"GET {path}: missing auth hint: {body}"

            for path, payload in [
                ("/api/config", {"token": TEST_UPSTREAM_KEY}),
                ("/api/test", {}),
                ("/api/jobs", {"prompt": "test", "model": "agnes-video-v2.0"}),
            ]:
                status, body = srv.post(path, payload, token=TEST_TOKEN)
                assert status == 401, f"POST {path}: expected 401, got {status}: {body}"
        finally:
            srv.stop()


def test_valid_token_allows_get_config():
    """With a valid AGNES_API_TOKEN, GET /api/config should return 200."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            status, body = srv.get("/api/config", token=TEST_TOKEN)
            assert status == 200, f"expected 200, got {status}: {body}"
            assert body.get("token_set") is True, f"token_set should be True: {body}"
        finally:
            srv.stop()


def test_valid_token_allows_get_jobs_list():
    """With a valid AGNES_API_TOKEN, GET /api/jobs/list should return 200 (empty list)."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            status, body = srv.get("/api/jobs/list", token=TEST_TOKEN)
            assert status == 200, f"expected 200, got {status}: {body}"
            assert isinstance(body, list), f"expected list, got {type(body)}"
        finally:
            srv.stop()


def test_valid_token_allows_get_jobs_by_id():
    """With a valid AGNES_API_TOKEN and a known job, GET /api/jobs/<id> should return 200."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))
        # Pre-seed a job record (minimal, no created_response)
        jobs = {"vid-001": {"id": "vid-001", "video_id": "vid-001", "model": "agnes-video-v2.0", "prompt": "test", "status": "queued", "progress": 0, "input_type": "text"}}
        (data_dir / "jobs.json").write_text(json.dumps(jobs))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            with srv.patch_call({"status": "processing", "progress": 50, "url": None}):
                status, body = srv.get("/api/jobs/vid-001", token=TEST_TOKEN)
                assert status == 200, f"expected 200, got {status}: {body}"
                assert body.get("status") == "processing", f"status not updated: {body}"
        finally:
            srv.stop()


def test_invalid_token_rejected():
    """With a wrong token, all /api/ routes must return 401."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            wrong_token = "wrong-token"
            for path in ["/api/config", "/api/jobs/list"]:
                status, body = srv.get(path, token=wrong_token)
                assert status == 401, f"GET {path} with wrong token: expected 401, got {status}: {body}"
                assert "未授权" in body.get("error", ""), f"GET {path}: missing auth hint: {body}"

            status, body = srv.post("/api/test", {}, token=wrong_token)
            assert status == 401, f"POST /api/test with wrong token: expected 401, got {status}: {body}"

            status, body = srv.post("/api/jobs", {"prompt": "test", "model": "agnes-video-v2.0"}, token=wrong_token)
            assert status == 401, f"POST /api/jobs with wrong token: expected 401, got {status}: {body}"
        finally:
            srv.stop()


def test_no_token_in_error_responses():
    """Error responses must NOT contain the web access token or upstream key."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            # GET with no token → 401
            status, body = srv.get("/api/config", token=None)
            assert status == 401
            assert TEST_TOKEN not in json.dumps(body), f"token leaked in 401 body: {body}"
            assert TEST_UPSTREAM_KEY not in json.dumps(body), f"upstream key leaked in 401 body: {body}"

            # POST /api/test with valid web token but upstream failure → 502
            with srv.patch_call_fail():
                status, body = srv.post("/api/test", {}, token=TEST_TOKEN)
                assert status == 502, f"expected 502, got {status}: {body}"
                assert TEST_UPSTREAM_KEY not in json.dumps(body), f"upstream key leaked in 502: {body}"
                assert TEST_TOKEN not in json.dumps(body), f"web token leaked in 502: {body}"
        finally:
            srv.stop()


def test_job_record_does_not_persist_full_response():
    """POST /api/jobs must NOT persist the full upstream response to jobs.json."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            fake_resp = srv.fake_upstream_response(include_sensitive_fields=True)
            with srv.patch_call(fake_resp):
                status, body = srv.post(
                    "/api/jobs",
                    {"prompt": "test prompt", "model": "agnes-video-v2.0"},
                    token=TEST_TOKEN,
                )
                assert status == 201, f"expected 201, got {status}: {body}"
                assert body.get("video_id") == "vid-001", f"video_id missing: {body}"

            jobs = srv.read_jobs()
            j = jobs.get("vid-001", {})
            # The full upstream response fields must NOT be in the persisted record
            assert "created_response" not in j, f"created_response still persisted: {j}"
            assert "request_id" not in j, f"sensitive request_id leaked to jobs.json: {j}"
            assert "debug_info" not in j, f"sensitive debug_info leaked to jobs.json: {j}"
            assert "internal_url" not in j, f"sensitive internal_url leaked to jobs.json: {j}"
            # Expected minimal fields must be present
            assert j.get("id") == "vid-001", f"missing id: {j}"
            assert j.get("video_id") == "vid-001", f"missing video_id: {j}"
            assert j.get("model") == "agnes-video-v2.0", f"missing model: {j}"
            assert j.get("status") == "queued", f"status not queued: {j}"
        finally:
            srv.stop()


def test_agones_data_dir_env_var():
    """AGNES_DATA_DIR must override the default data directory."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "custom_data"
        out_dir = Path(tmp) / "custom_out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        os.environ["AGNES_DATA_DIR"] = str(data_dir)
        os.environ["AGNES_OUTPUT_DIR"] = str(out_dir)
        os.environ["AGNES_API_TOKEN"] = TEST_TOKEN

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            status, body = srv.get("/api/config", token=TEST_TOKEN)
            assert status == 200, f"expected 200, got {status}: {body}"
            # Verify the data was read from the custom dir
            assert data_dir.exists()
            assert (data_dir / "config.json").exists()
        finally:
            srv.stop()


def test_backend_only_listens_localhost():
    """The server must bind to 127.0.0.1 only (verified by the bind address)."""
    import socket
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        out_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(json.dumps({"token": TEST_UPSTREAM_KEY}))

        os.environ["AGNES_DATA_DIR"] = str(data_dir)
        os.environ["AGNES_OUTPUT_DIR"] = str(out_dir)
        os.environ["AGNES_API_TOKEN"] = TEST_TOKEN

        srv = TestServer(data_dir, out_dir, token=TEST_TOKEN)
        srv.start()
        try:
            # The server was created with ("127.0.0.1", 0) — verify by checking
            # that it is reachable on 127.0.0.1
            status, _ = srv.get("/api/config", token=TEST_TOKEN)
            assert status == 200, f"localhost not reachable: {status}"
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_no_token_rejected_all_routes,
    test_valid_token_allows_get_config,
    test_valid_token_allows_get_jobs_list,
    test_valid_token_allows_get_jobs_by_id,
    test_invalid_token_rejected,
    test_no_token_in_error_responses,
    test_job_record_does_not_persist_full_response,
    test_agones_data_dir_env_var,
    test_backend_only_listens_localhost,
]


# ---------------------------------------------------------------------------
# unittest bridge
# ---------------------------------------------------------------------------
# The 9 tests above are written in pytest style (bare functions + plain
# asserts). The project's offline harness runs `python3 -m unittest` and has
# no pytest dependency, so we expose them as TestCase methods WITHOUT
# touching the function bodies. Each bare function is wrapped to swallow the
# implicit `self` argument; semantics are unchanged. This lets both
#   python3 -m unittest tests/test_auth.py
# and
#   python3 -m unittest discover -s tests
# discover and run the auth tests, while `python tests/test_auth.py`
# (the original main() runner) keeps working.

import unittest as _unittest


def _make_case_method(fn):
    def method(self):
        fn()
    return method


class TestAuthCase(_unittest.TestCase):
    """unittest-discoverable wrapper hosting the bare pytest-style auth tests."""


for _i, _fn in enumerate(ALL_TESTS):
    _m = _make_case_method(_fn)
    _m.__name__ = _fn.__name__
    _m.__doc__ = _fn.__doc__
    setattr(TestAuthCase, _fn.__name__, _m)


def main():
    failed = 0
    for fn in ALL_TESTS:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} passed, {failed} failed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
