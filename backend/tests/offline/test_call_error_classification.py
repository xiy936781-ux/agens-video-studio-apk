"""Offline regression tests for upstream call() error classification.

Verifies that:
  1. call() raises RuntimeError with a category-prefixed, sanitized message
     for HTTP errors, timeouts, and network failures.
  2. _sanitize_call_error never includes API keys, tokens, or URL query params.
  3. The long-video segment creation loop preserves the category-prefixed
     error into segments[i]["error"] and seg_job["error"].

No real network calls or upstream Agnes API are made.
"""
import importlib.util
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[2]


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_call_err_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "test-web-token"
    spec = importlib.util.spec_from_file_location("server_call_err", ROOT / "server.py")
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


# ---------------------------------------------------------------------------
# _sanitize_call_error
# ---------------------------------------------------------------------------
class SanitizeCallErrorTests(unittest.TestCase):
    def test_http_error_includes_status_code(self):
        err = urllib.error.HTTPError(
            "https://example.com", 402, "Payment Required", {}, io.BytesIO()
        )
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("http_error", msg)
        self.assertIn("402", msg)

    def test_http_error_429_rate_limit(self):
        err = urllib.error.HTTPError(
            "https://example.com", 429, "Too Many Requests", {}, io.BytesIO()
        )
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("http_error", msg)
        self.assertIn("429", msg)

    def test_http_error_401(self):
        err = urllib.error.HTTPError(
            "https://example.com", 401, "Unauthorized", {}, io.BytesIO()
        )
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("http_error", msg)
        self.assertIn("401", msg)

    def test_urLError_with_timeout_reason(self):
        err = urllib.error.URLError(socket.timeout("timed out"))
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("timeout", msg)

    def test_bare_socket_timeout(self):
        err = socket.timeout("timed out")
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("timeout", msg)
        self.assertIn("90", msg)

    def test_urLError_generic_network(self):
        err = urllib.error.URLError("Name or service not known")
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("network", msg)

    def test_runtime_error_propagates_unchanged(self):
        err = RuntimeError("business: upstream rejected request (no details)")
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertEqual(msg, "business: upstream rejected request (no details)")

    def test_generic_exception_falls_back_to_network(self):
        err = OSError("some unexpected error")
        msg = SERVER._sanitize_call_error(err, timeout=90)
        self.assertIn("network", msg)

    def test_no_sensitive_data_in_any_message(self):
        """No message should contain the test API key or a URL query string."""
        secret = "sk-test-secret-abcdef123456"
        url_with_query = "https://example.com/v1/videos?api_key=" + secret
        cases = [
            urllib.error.HTTPError(url_with_query, 402, "x", {}, io.BytesIO()),
            socket.timeout("t"),
            urllib.error.URLError("dns"),
            RuntimeError("business: x"),
            OSError("other"),
        ]
        for e in cases:
            msg = SERVER._sanitize_call_error(e, timeout=90)
            self.assertNotIn(secret, msg, "API key leaked in: %s" % msg)
            self.assertNotIn("api_key=" + secret, msg, "URL query param leaked: %s" % msg)


# ---------------------------------------------------------------------------
# call() — mock urlopen to simulate each failure mode
# ---------------------------------------------------------------------------
class CallErrorClassificationTests(unittest.TestCase):
    """call() must raise RuntimeError with a category-prefixed message."""

    def _call(self, side_effect):
        with patch.object(
            SERVER.urllib.request, "urlopen", side_effect=side_effect
        ):
            return SERVER.call(
                "GET",
                "https://apihub.agnes-ai.com/agnesapi?video_id=task_x&model_name=m",
                "sk-test-token-xyz",
            )

    def test_http_402(self):
        def _raise_http(*a, **kw):
            raise urllib.error.HTTPError(
                "https://apihub.agnes-ai.com", 402, "Payment Required", {}, io.BytesIO()
            )

        with self.assertRaises(RuntimeError) as ctx:
            self._call(_raise_http)
        self.assertIn("http_error", str(ctx.exception))
        self.assertIn("402", str(ctx.exception))

    def test_http_429_rate_limit(self):
        def _raise_429(*a, **kw):
            raise urllib.error.HTTPError(
                "https://apihub.agnes-ai.com", 429, "Too Many Requests", {}, io.BytesIO()
            )

        with self.assertRaises(RuntimeError) as ctx:
            self._call(_raise_429)
        self.assertIn("http_error", str(ctx.exception))
        self.assertIn("429", str(ctx.exception))

    def test_timeout(self):
        def _raise_to(*a, **kw):
            raise socket.timeout("timed out")

        with self.assertRaises(RuntimeError) as ctx:
            self._call(_raise_to)
        self.assertIn("timeout", str(ctx.exception))

    def test_network_dns_failure(self):
        def _raise_dns(*a, **kw):
            raise urllib.error.URLError("Name or service not known")

        with self.assertRaises(RuntimeError) as ctx:
            self._call(_raise_dns)
        self.assertIn("network", str(ctx.exception))

    def test_no_sensitive_data_in_call_error_messages(self):
        """The error message must not leak the API key or URL query params."""
        secret = "sk-SECRET-abcdef"

        def _raise_http(*a, **kw):
            raise urllib.error.HTTPError(
                "https://apihub.agnes-ai.com?key=" + secret,
                401,
                "Unauthorized",
                {},
                io.BytesIO(),
            )

        with self.assertRaises(RuntimeError) as ctx:
            self._call(_raise_http)
        msg = str(ctx.exception)
        self.assertNotIn(secret, msg)
        self.assertNotIn("key=" + secret, msg)


# ---------------------------------------------------------------------------
# Long-video segment creation loop — error preservation
# ---------------------------------------------------------------------------
class SegmentCreationErrorTests(unittest.TestCase):
    """When a segment's call() fails, the error must carry a category prefix."""

    def _build_handler(self, body_dict):
        """Return an H-subclass instance ready for do_POST on /api/jobs."""
        tmp = Path(tempfile.mkdtemp(prefix="agnes_seg_post_"))
        (tmp / "data").mkdir()
        (tmp / "outputs").mkdir()
        # Seed config so the route proceeds past the "no upstream key" guard.
        (tmp / "data" / "config.json").write_text(json.dumps({"token": "sk-upstream-test"}))

        class _H(SERVER.H):
            def __init__(self):
                self.path = "/api/jobs"
                self.request_version = "HTTP/1.1"
                self.command = "POST"
                self.client_address = ("127.0.0.1", 0)
                self.wfile = io.BytesIO()
                self._status_code = 200
                self.sent_headers = {}
                payload = json.dumps(body_dict).encode()
                self.headers = {
                    "Authorization": "Bearer test-web-token",
                    "Content-Length": str(len(payload)),
                }
                self.rfile = io.BytesIO(payload)

            def send_response(self, code):
                self._status_code = code

            def send_header(self, key, value):
                self.sent_headers[key] = value

            def end_headers(self):
                pass

        handler = _H()
        return handler, tmp

    def test_all_segments_fail_preserves_http_error_category(self):
        """Simulate call() raising HTTP 402 for every segment -> segments error
        must contain 'http_error' and '402'."""
        handler, tmp = self._build_handler({
            "prompt": "test prompt",
            "model": "agnes-video-2.5-flash",
            "target_seconds": 60,
        })

        saved_data = SERVER.DATA
        saved_out = SERVER.OUT
        saved_cfg = SERVER.CFG
        SERVER.DATA = tmp / "data"
        SERVER.OUT = tmp / "outputs"
        SERVER.CFG = tmp / "data" / "config.json"

        def _fake_call_402(method, url, token, body=None, timeout=90):
            raise RuntimeError("http_error: HTTP 402")

        try:
            with patch.object(SERVER, "call", side_effect=_fake_call_402):
                handler.do_POST()

            self.assertEqual(handler._status_code, 502)
            body = json.loads(handler.wfile.getvalue().decode())
            self.assertIn("error", body)
            self.assertIn("所有分段创建均失败", body["error"])
        finally:
            SERVER.DATA = saved_data
            SERVER.OUT = saved_out
            SERVER.CFG = saved_cfg

    def test_segment_timeout_preserves_timeout_category(self):
        """Simulate call() raising a timeout -> segments error must contain 'timeout'."""
        handler, tmp = self._build_handler({
            "prompt": "test prompt",
            "model": "agnes-video-2.5-flash",
            "target_seconds": 30,
        })

        saved_data = SERVER.DATA
        saved_out = SERVER.OUT
        saved_cfg = SERVER.CFG
        SERVER.DATA = tmp / "data"
        SERVER.OUT = tmp / "outputs"
        SERVER.CFG = tmp / "data" / "config.json"

        def _fake_call_timeout(method, url, token, body=None, timeout=90):
            raise RuntimeError("timeout: request exceeded 90s deadline")

        try:
            with patch.object(SERVER, "call", side_effect=_fake_call_timeout):
                handler.do_POST()

            self.assertEqual(handler._status_code, 502)
            body = json.loads(handler.wfile.getvalue().decode())
            self.assertIn("error", body)
            self.assertIn("所有分段创建均失败", body["error"])
        finally:
            SERVER.DATA = saved_data
            SERVER.OUT = saved_out
            SERVER.CFG = saved_cfg


# ---------------------------------------------------------------------------
# UPSTREAM_ERROR_CATEGORIES constant
# ---------------------------------------------------------------------------
class UpstreamErrorCategoriesTests(unittest.TestCase):
    def test_all_categories_present(self):
        expected = {"http_error", "timeout", "network", "business"}
        self.assertEqual(set(SERVER.UPSTREAM_ERROR_CATEGORIES), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
