"""Offline tests for GET /api/jobs/<jid>/proxy (R2 short-video local proxy)
and the NaN-safe progress normalization.

No real network, video API, FFmpeg, or service startup is used.
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys_path_needed = str(ROOT)


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_proxy_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "proxy-token"
    spec = importlib.util.spec_from_file_location("server_proxy", ROOT / "server.py")
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
VALID_TOKEN = "proxy-token"
WHITELIST_URL = "https://platform-outputs.agnes-ai.space/video/x.mp4"


class _ProxyHandler(SERVER.H):
    def __init__(self, path, token=VALID_TOKEN, range_header=None, jobs=None):
        self.path = path
        self.request_version = "HTTP/1.1"
        self.command = "GET"
        self.client_address = ("127.0.0.1", 0)
        self.wfile = io.BytesIO()
        self._status_code = 200
        self.sent_headers = {}
        self.headers = {"Authorization": "Bearer %s" % token, "Content-Length": "0"}
        if range_header is not None:
            self.headers["Range"] = range_header
        self.rfile = io.BytesIO(b"")
        self._jobs_override = jobs

    def send_response(self, code):
        self._status_code = code

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    @property
    def status_code(self):
        return self._status_code

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode() or "{}")


def _seed_job(out_root, jid, long_=False, video_url=None):
    """Write a jobs.json containing a single job and point SERVER.DATA there."""
    job = {
        "id": jid,
        "long": long_,
        "status": "succeeded" if video_url else "processing",
        "progress": 100 if video_url else 0,
        "video_url": video_url,
        "model": "m",
        "prompt": "p",
    }
    data_dir = out_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "jobs.json").write_text(json.dumps({jid: job}))
    return job


class ProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agnes_proxy_out_"))
        self.out = self.root / "outputs"
        self.out.mkdir(parents=True, exist_ok=True)
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self._p_out = patch.object(SERVER, "OUT", self.out)
        self._p_data = patch.object(SERVER, "DATA", self.data)
        self._p_out.start()
        self._p_data.start()

    def tearDown(self):
        self._p_out.stop()
        self._p_data.stop()

    def _prewrite_proxy_cache(self, jid, payload=b"0123456789abcdef"):
        p = self.out / "proxy" / ("%s.mp4" % jid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return p

    def test_200_full_with_inline_and_accept_ranges(self):
        _seed_job(self.root, "jobA", video_url=WHITELIST_URL)
        self._prewrite_proxy_cache("jobA")
        h = _ProxyHandler("/api/jobs/jobA/proxy")
        h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Type"], "video/mp4")
        self.assertEqual(h.sent_headers["Accept-Ranges"], "bytes")
        self.assertEqual(h.sent_headers["Content-Length"], "16")
        self.assertIn("inline", h.sent_headers["Content-Disposition"])

    def test_206_range(self):
        _seed_job(self.root, "jobA", video_url=WHITELIST_URL)
        self._prewrite_proxy_cache("jobA")
        h = _ProxyHandler("/api/jobs/jobA/proxy", range_header="bytes=0-7")
        h.do_GET()
        self.assertEqual(h.status_code, 206)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes 0-7/16")
        self.assertEqual(h.sent_headers["Content-Length"], "8")
        self.assertEqual(h.wfile.getvalue(), b"01234567")

    def test_416_unsatisfiable(self):
        _seed_job(self.root, "jobA", video_url=WHITELIST_URL)
        self._prewrite_proxy_cache("jobA")
        h = _ProxyHandler("/api/jobs/jobA/proxy", range_header="bytes=99-100")
        h.do_GET()
        self.assertEqual(h.status_code, 416)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes */16")
        self.assertEqual(h.sent_headers["Content-Length"], "0")

    def test_404_unknown_job(self):
        _seed_job(self.root, "jobA", video_url=WHITELIST_URL)
        h = _ProxyHandler("/api/jobs/unknown/proxy")
        h.do_GET()
        self.assertEqual(h.status_code, 404)
        self.assertEqual(h.body_json()["error"], "找不到任务")

    def test_404_missing_url(self):
        _seed_job(self.root, "jobB", video_url=None)
        h = _ProxyHandler("/api/jobs/jobB/proxy")
        h.do_GET()
        self.assertEqual(h.status_code, 404)

    def test_400_long_video_rejected(self):
        _seed_job(self.root, "jobC", long_=True, video_url=WHITELIST_URL)
        self._prewrite_proxy_cache("jobC")
        h = _ProxyHandler("/api/jobs/jobC/proxy")
        h.do_GET()
        self.assertEqual(h.status_code, 400)

    def test_400_non_whitelist_host(self):
        _seed_job(self.root, "jobD", video_url="https://evil.com/x.mp4")
        self._prewrite_proxy_cache("jobD")
        h = _ProxyHandler("/api/jobs/jobD/proxy")
        h.do_GET()
        self.assertEqual(h.status_code, 400)

    def test_502_download_failure(self):
        _seed_job(self.root, "jobE", video_url=WHITELIST_URL)
        with patch.object(SERVER, "download_segment_limited",
                          return_value={"ok": False, "error": "download failed: timeout"}):
            h = _ProxyHandler("/api/jobs/jobE/proxy")
            h.do_GET()
        self.assertEqual(h.status_code, 502)

    def test_proxy_cache_written_then_served(self):
        _seed_job(self.root, "jobF", video_url=WHITELIST_URL)
        with patch.object(SERVER, "download_segment_limited") as m_dl:
            def fake_dl(url, dest, max_bytes=None):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"0123456789abcdef")
                return {"ok": True, "error": None}
            m_dl.side_effect = fake_dl
            h = _ProxyHandler("/api/jobs/jobF/proxy")
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.wfile.getvalue(), b"0123456789abcdef")

    def test_proxy_not_shadowed_by_generic_jobs_route(self):
        """The proxy route must be matched before the generic /api/jobs/<jid>
        block, otherwise jid would be mis-parsed as 'proxy'."""
        _seed_job(self.root, "jobA", video_url=WHITELIST_URL)
        self._prewrite_proxy_cache("jobA")
        h = _ProxyHandler("/api/jobs/jobA/proxy")
        h.do_GET()
        # If the generic block shadowed it, jid="proxy" -> 404 找不到任务.
        self.assertEqual(h.status_code, 200, "proxy route was shadowed by generic /api/jobs/ block")


# ---------------------------------------------------------------------------
# NaN-safe progress normalization
# ---------------------------------------------------------------------------
class NormalizeProgressTests(unittest.TestCase):
    def test_float_nan_clamped_to_zero(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": float("nan")})
        self.assertEqual(r["progress"], 0)
        self.assertIsInstance(r["progress"], int)

    def test_float_inf_clamped_to_hundred(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": float("inf")})
        self.assertEqual(r["progress"], 100)

    def test_negative_inf_clamped_to_zero(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": float("-inf")})
        self.assertEqual(r["progress"], 0)

    def test_numeric_string_clamped(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": "50"})
        self.assertEqual(r["progress"], 50)

    def test_non_numeric_string_preserved(self):
        # Existing contract: non-numeric strings pass through unchanged; the
        # frontend display separately guards against rendering them as NaN.
        r = SERVER.normalize({}, {"status": "processing", "progress": "unknown"})
        self.assertEqual(r["progress"], "unknown")

    def test_bool_progress(self):
        r = SERVER.normalize({}, {"status": "succeeded", "progress": True})
        self.assertEqual(r["progress"], 100)
        r2 = SERVER.normalize({}, {"status": "queued", "progress": False})
        self.assertEqual(r2["progress"], 0)

    def test_none_progress_defaults_zero(self):
        r = SERVER.normalize({}, {"status": "queued"})
        self.assertEqual(r["progress"], 0)

    def test_out_of_range_clamped(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": 105})
        self.assertEqual(r["progress"], 100)
        r2 = SERVER.normalize({}, {"status": "processing", "progress": -5})
        self.assertEqual(r2["progress"], 0)

    def test_large_positive_int_clamped_to_hundred(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": 10 ** 1000})
        self.assertEqual(r["progress"], 100)

    def test_large_negative_int_clamped_to_zero(self):
        r = SERVER.normalize({}, {"status": "processing", "progress": -(10 ** 1000)})
        self.assertEqual(r["progress"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
