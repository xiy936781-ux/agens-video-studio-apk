"""Offline route-level security tests.

These tests exercise the H handler's public routes without real HTTP sockets,
upstream Agnes API calls, FFmpeg subprocesses, or production config writes.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_offline_routes_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "test-web-token"
    spec = importlib.util.spec_from_file_location("server_under_test", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATA = module.DATA if old_data is None else module.DATA
    module.OUT = module.OUT if old_out is None else module.OUT
    if old_data is not None:
        os.environ["AGNES_DATA_DIR"] = old_data
    if old_out is not None:
        os.environ["AGNES_OUTPUT_DIR"] = old_out
    if old_token is not None:
        os.environ["AGNES_API_TOKEN"] = old_token
    return module


SERVER = _load_server_module()


class _Handler(SERVER.H):
    """Concrete handler subclass that captures response status without I/O."""

    def __init__(self, path, method, token, body=None):
        self.path = path
        self.request_version = "HTTP/1.1"
        self.command = method
        self.client_address = ("127.0.0.1", 0)
        self.wfile = io.BytesIO()
        self._status_code = 200
        self.headers = {
            "Authorization": f"Bearer {token}"
        }
        payload = b"" if method == "GET" else json.dumps(body or {}).encode()
        self.headers["Content-Length"] = str(len(payload))
        self.rfile = io.BytesIO(payload)

    def send_response(self, code):
        self._status_code = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    @property
    def status_code(self):
        return self._status_code


def call_route(path: str, method: str, token: str = "test-web-token", body: dict | None = None):
    h = _Handler(path, method, token, body)
    if method == "GET":
        h.do_GET()
    else:
        h.do_POST()
    h.wfile.seek(0)
    payload = h.wfile.read()
    return h.status_code, json.loads(payload.decode() or "{}")


class FixedNestedLockRouteTests(unittest.TestCase):
    """Regression tests for the 4 deadlock fixes.

    Each test mocks all upstream/network/FFmpeg/download calls, runs the route
    in a background thread with a join timeout, and asserts that jobs_save
    was called and the thread completed in time (no deadlock).
    """

    TIMEOUT = 5.0

    def _run_in_thread(self, path, method, body=None):
        """Run a route in a thread with a timeout guard. Returns (completed, status)."""
        result = {}

        def _worker():
            try:
                status, _ = call_route(path, method, body=body)
                result["status"] = status
                result["completed"] = True
            except Exception as exc:
                result["error"] = exc
                result["completed"] = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(self.TIMEOUT)
        if t.is_alive():
            self.fail(f"DEADLOCK: route {method} {path} did not finish within {self.TIMEOUT}s")
        return result

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="agnes_fixed_data_"))
        self.out = Path(tempfile.mkdtemp(prefix="agnes_fixed_out_"))
        self._started = []
        for name, value in (("DATA", self.data), ("OUT", self.out)):
            p = patch.object(SERVER, name, value)
            p.start()
            self._started.append(p)

    def tearDown(self):
        for p in reversed(self._started):
            p.stop()

    # --- GET /api/jobs/<long-video-id> poll ---
    def test_poll_long_video_job_no_deadlock(self):
        job = {
            "id": "lv_test_001",
            "long": True,
            "model": "agnes-video-2.5",
            "segments": [
                {"index": 0, "video_id": "vid_a", "status": "succeeded", "progress": 100},
                {"index": 1, "video_id": "vid_b", "status": "succeeded", "progress": 100},
            ],
            "status": "processing",
            "progress": 0,
        }
        with patch.object(SERVER, "jobs_load", return_value={"lv_test_001": job, "vid_a": {}, "vid_b": {}}), \
             patch.object(SERVER, "cfg", return_value={"token": "fake-token"}), \
             patch.object(SERVER, "call", return_value=(200, {"status": "succeeded", "progress": 100,
                                                              "url": "https://platform-outputs.agnes-ai.space/f.mp4"})), \
             patch.object(SERVER, "jobs_save") as mock_save:
            res = self._run_in_thread("/api/jobs/lv_test_001", "GET")
            self.assertTrue(res.get("completed"), f"thread error: {res.get('error')}")
            self.assertEqual(res["status"], 200)
            mock_save.assert_called_once()
            saved = mock_save.call_args[0][0]
            self.assertIn("lv_test_001", saved)

    # --- GET /api/jobs/<short-video-id> poll ---
    def test_poll_short_video_job_no_deadlock(self):
        job = {
            "id": "vid_short_001",
            "video_id": "vid_short_001",
            "model": "agnes-video-v2.0",
            "status": "processing",
            "progress": 50,
        }
        with patch.object(SERVER, "jobs_load", return_value={"vid_short_001": job}), \
             patch.object(SERVER, "cfg", return_value={"token": "fake-token"}), \
             patch.object(SERVER, "call", return_value=(200, {"status": "succeeded", "progress": 100,
                                                              "url": "https://platform-outputs.agnes-ai.space/f.mp4"})), \
             patch.object(SERVER, "jobs_save") as mock_save:
            res = self._run_in_thread("/api/jobs/vid_short_001", "GET")
            self.assertTrue(res.get("completed"), f"thread error: {res.get('error')}")
            self.assertEqual(res["status"], 200)
            mock_save.assert_called_once()

    # --- POST /api/jobs/<id>/merge ---
    def test_merge_job_no_deadlock(self):
        job = {
            "id": "lv_merge_001",
            "long": True,
            "segments": [
                {"index": 0, "video_id": "vid_m1", "status": "succeeded", "progress": 100,
                 "url": "https://platform-outputs.agnes-ai.space/a.mp4"},
                {"index": 1, "video_id": "vid_m2", "status": "succeeded", "progress": 100,
                 "url": "https://platform-outputs.agnes-ai.space/b.mp4"},
            ],
            "status": "succeeded",
        }
        def _fake_merge(files, out_path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
            return {"ok": True, "error": None, "output": str(out_path)}

        with patch.object(SERVER, "jobs_load", return_value={"lv_merge_001": job}), \
             patch.object(SERVER, "download_segment", return_value={"ok": True, "error": None}), \
             patch.object(SERVER, "validate_segment_files", return_value={"ok": True, "errors": []}), \
             patch.object(SERVER, "ffmpeg_available", return_value=True), \
             patch.object(SERVER, "merge_segments_ffmpeg", side_effect=_fake_merge), \
             patch.object(SERVER, "jobs_save") as mock_save:
            res = self._run_in_thread("/api/jobs/lv_merge_001/merge", "POST")
            self.assertTrue(res.get("completed"), f"thread error: {res.get('error')}")
            self.assertEqual(res["status"], 200)
            mock_save.assert_called_once()

    # --- POST /api/jobs (short-video creation) ---
    def test_create_short_video_no_deadlock(self):
        with patch.object(SERVER, "cfg", return_value={"token": "fake-token"}), \
             patch.object(SERVER, "call", return_value=(200, {"video_id": "vid_new_001"})), \
             patch.object(SERVER, "jobs_load", return_value={}), \
             patch.object(SERVER, "jobs_save") as mock_save:
            res = self._run_in_thread(
                "/api/jobs", "POST",
                body={"prompt": "test", "model": "agnes-video-v2.0"},
            )
            self.assertTrue(res.get("completed"), f"thread error: {res.get('error')}")
            self.assertEqual(res["status"], 201)
            mock_save.assert_called_once()


class RouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="agnes_routes_data_"))
        self.out = Path(tempfile.mkdtemp(prefix="agnes_routes_out_"))
        self._patches = []
        for name, value in (("DATA", self.data), ("OUT", self.out)):
            setattr(SERVER, name, value)
            self._patches.append(patch.object(SERVER, name, value))
        for p in self._patches:
            p.start()
        self._jobs_patch = patch.object(SERVER, "jobs_load", return_value={})
        self._jobs_patch.start()

    def tearDown(self):
        self._jobs_patch.stop()
        for p in reversed(self._patches):
            p.stop()

    def test_unknown_api_get_returns_404(self):
        with patch.object(SERVER, "verify_web_token", return_value=True):
            status, body = call_route("/api/does-not-exist", "GET")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_unknown_api_post_returns_404(self):
        with patch.object(SERVER, "verify_web_token", return_value=True):
            status, body = call_route("/api/does-not-exist", "POST", body={})
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_post_requires_auth_on_api_routes(self):
        h = _Handler("/api/config", "POST", token="", body={})
        with patch.object(SERVER, "verify_web_token", return_value=False):
            h.do_POST()
        self.assertEqual(h.status_code, 401)

    def test_get_requires_auth_on_api_routes(self):
        h = _Handler("/api/config", "GET", token="")
        with patch.object(SERVER, "verify_web_token", return_value=False):
            h.do_GET()
        self.assertEqual(h.status_code, 401)

    def test_root_get_serves_index_html(self):
        idx = Path(tempfile.mkdtemp(prefix="agnes_routes_index_")) / "index.html"
        idx.write_text("<html></html>", encoding="utf-8")
        fake_root = idx.parent
        with patch.object(SERVER, "ROOT", fake_root):
            h = _Handler("/", "GET", token="")
            h.do_GET()
            self.assertEqual(h.status_code, 200)
            h.wfile.seek(0)
            payload = h.wfile.read()
            self.assertEqual(payload, b"<html></html>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
