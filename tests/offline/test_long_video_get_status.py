"""Offline regression tests for long-video GET status aggregation.

These tests exercise the H handler's long-video GET route without real HTTP
sockets, upstream Agnes API calls, or production data writes.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_long_video_status_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "test-web-token"
    spec = importlib.util.spec_from_file_location("server_under_test", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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


def call_get(path: str, token: str = "test-web-token"):
    h = _Handler(path, "GET", token)
    h.do_GET()
    h.wfile.seek(0)
    payload = h.wfile.read()
    return h.status_code, json.loads(payload.decode() or "{}")


class LongVideoGetStatusTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="agnes_long_status_data_"))
        self.out = Path(tempfile.mkdtemp(prefix="agnes_long_status_out_"))
        self._patches = []
        for name, value in (("DATA", self.data), ("OUT", self.out)):
            p = patch.object(SERVER, name, value)
            p.start()
            self._patches.append(p)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _run_long_get(self, job, call_return, call_side_effect=None):
        jobs = {"lv_x": job}
        for seg in job.get("segments", []):
            vid = seg.get("video_id")
            if vid:
                jobs[vid] = dict(seg)
        ctx = [
            patch.object(SERVER, "jobs_load", return_value=jobs),
            patch.object(SERVER, "cfg", return_value={"token": "fake-token"}),
            patch.object(SERVER, "jobs_save"),
        ]
        if call_side_effect is not None:
            ctx.append(patch.object(SERVER, "call", side_effect=call_side_effect))
        else:
            ctx.append(patch.object(SERVER, "call", return_value=call_return))

        with ctx[0], ctx[1], ctx[2] as mock_save, ctx[3]:
            status, body = call_get("/api/jobs/lv_x")
            return status, body, mock_save, ctx[3]

    def test_progress_none_does_not_break_aggregation(self):
        job = {
            "id": "lv_x",
            "long": True,
            "model": "agnes-video-2.5-flash",
            "status": "processing",
            "progress": 0,
            "merge_status": "ready",
            "segments": [
                {"index": 0, "video_id": "vid_a", "status": "succeeded", "progress": 100},
                {"index": 1, "video_id": "vid_b", "status": "succeeded", "progress": 100},
                {"index": 2, "video_id": "vid_c", "status": "succeeded", "progress": 100},
            ],
        }
        call_return = (200, {"status": "succeeded", "progress": None,
                             "url": "https://platform-outputs.agnes-ai.space/f.mp4"})
        status, body, mock_save, mock_call = self._run_long_get(job, call_return)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["progress"], 100)
        self.assertEqual(body["merge_status"], "ready")
        mock_save.assert_called_once()

    def test_poll_exception_keeps_persisted_succeeded_state(self):
        job = {
            "id": "lv_x",
            "long": True,
            "model": "agnes-video-2.5-flash",
            "status": "processing",
            "progress": 33,
            "merge_status": "ready",
            "segments": [
                {"index": 0, "video_id": "vid_a", "status": "succeeded", "progress": 100},
                {"index": 1, "video_id": "vid_b", "status": "succeeded", "progress": 100},
                {"index": 2, "video_id": "vid_c", "status": "succeeded", "progress": 100},
            ],
        }

        def _raise(_method, _url, _token, _body=None, _timeout=90):
            raise RuntimeError("network timeout")

        status, body, mock_save, _ = self._run_long_get(job, None, _raise)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["merge_status"], "ready")
        self.assertEqual(body["progress"], 100)
        mock_save.assert_called_once()

    def test_merged_status_is_not_reset_by_plain_get(self):
        job = {
            "id": "lv_x",
            "long": True,
            "model": "agnes-video-2.5-flash",
            "status": "succeeded",
            "progress": 100,
            "merge_status": "merged",
            "merged_file": "/var/lib/agnes-video/outputs/lv_x_merged.mp4",
            "segments": [
                {"index": 0, "video_id": "vid_a", "status": "succeeded", "progress": 100},
            ],
        }
        call_return = (200, {"status": "succeeded", "progress": 100,
                             "url": "https://platform-outputs.agnes-ai.space/f.mp4"})
        status, body, mock_save, _ = self._run_long_get(job, call_return)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["merge_status"], "merged")
        self.assertEqual(body["merged_file"], "/var/lib/agnes-video/outputs/lv_x_merged.mp4")
        mock_save.assert_called_once()

    def test_failed_segment_still_marks_parent_failed(self):
        job = {
            "id": "lv_x",
            "long": True,
            "model": "agnes-video-2.5-flash",
            "status": "processing",
            "progress": 50,
            "merge_status": "ready",
            "segments": [
                {"index": 0, "video_id": "vid_a", "status": "succeeded", "progress": 100},
                {"index": 1, "video_id": "vid_b", "status": "failed", "progress": 0},
            ],
        }
        call_return = (200, {"status": "failed", "progress": 0, "error_code": 500})
        status, body, _, _ = self._run_long_get(job, call_return)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["merge_status"], "ready")


if __name__ == "__main__":
    unittest.main()
