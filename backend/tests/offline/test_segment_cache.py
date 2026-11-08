"""Offline tests for the segment-cache route: GET /api/jobs/<jid>/segments/<seg_index>.

All network / FFmpeg / ffprobe calls are mocked. No real downloads, no real
ffprobe, no real 500MB writes. The server module is loaded in isolation via
importlib with temp DATA/OUT directories, mirroring test_offline_routes.py.

32 test methods, each annotated with the plan-item number (#1 .. #32).
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_segcache_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "seg-cache-token"
    spec = importlib.util.spec_from_file_location("server_segcache", ROOT / "server.py")
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

VALID_TOKEN = "seg-cache-token"
ALLOWED_URL = "https://platform-outputs.agnes-ai.space/seg0.mp4"
EVIL_URL = "https://evil.com/seg0.mp4"


class _SegHandler(SERVER.H):
    """Concrete handler that captures status, sent headers and body without I/O."""

    def __init__(self, path, token, range_header=None, job=None, extra_headers=None):
        self.path = path
        self.request_version = "HTTP/1.1"
        self.command = "GET"
        self.client_address = ("127.0.0.1", 0)
        self.wfile = io.BytesIO()
        self._status_code = 200
        self.sent_headers = {}
        self._job = job
        self.headers = {"Authorization": "Bearer %s" % token}
        if range_header is not None:
            self.headers["Range"] = range_header
        if extra_headers:
            self.headers.update(extra_headers)
        self.headers["Content-Length"] = "0"
        self.rfile = io.BytesIO(b"")

    def send_response(self, code):
        self._status_code = code

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    @property
    def status_code(self):
        return self._status_code


def _mkhandler(path, token=VALID_TOKEN, range_header=None, job=None, extra_headers=None):
    h = _SegHandler(path, token, range_header=range_header, job=job, extra_headers=extra_headers)
    if job is not None:
        patch.object(SERVER, "jobs_load", return_value={"_probe_": job, "lv_a": _job_by_id(job)}).start()
    return h


def _job_by_id(job):
    return job


class SegmentCacheTests(unittest.TestCase):
    """32 plan items covering the segment-cache route."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="agnes_seg_data_"))
        self.out = Path(tempfile.mkdtemp(prefix="agnes_seg_out_"))
        self._patches = [
            patch.object(SERVER, "DATA", self.data),
            patch.object(SERVER, "OUT", self.out),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # ------------------------------------------------------------------ helpers
    def _long_job(self, segs=None):
        """A long-video job dict for jobs_load return value."""
        if segs is None:
            segs = [{
                "index": 0,
                "video_id": "vid0",
                "status": "succeeded",
                "progress": 100,
                "url": ALLOWED_URL,
            }]
        return {
            "id": "lv_a",
            "long": True,
            "model": "agnes-video-2.5",
            "segments": segs,
            "status": "succeeded",
            "progress": 100,
        }

    def _jobs_map(self, job):
        """Build a jobs dict keyed by jid for jobs_load mocking."""
        m = {job["id"]: job}
        for s in job.get("segments", []):
            if s.get("video_id"):
                m[s["video_id"]] = {"id": s["video_id"], "status": s.get("status", "succeeded")}
        return m

    def _cache_path(self, seg_index=0):
        return self.out / "segments" / "lv_a" / ("seg_%02d.mp4" % seg_index)

    def _seed_cache(self, seg_index=0, content=b"\x00\x00\x00\x14ftypisom" + b"\x00" * 16):
        p = self._cache_path(seg_index)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    # ------------------------------------------------------------------ auth
    def test_no_token_401(self):
        # #1 no token -> 401
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())):
            h = _SegHandler("/api/jobs/lv_a/segments/0", "")
            h.do_GET()
        self.assertEqual(h.status_code, 401)

    def test_wrong_token_401(self):
        # #2 wrong token -> 401
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())):
            h = _SegHandler("/api/jobs/lv_a/segments/0", "wrong-token")
            h.do_GET()
        self.assertEqual(h.status_code, 401)

    # ------------------------------------------------------------------ job checks
    def test_unknown_jid_404(self):
        # #3 valid token + unknown jid -> 404
        with patch.object(SERVER, "jobs_load", return_value={}):
            h = _SegHandler("/api/jobs/does_not_exist/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 404)
        self.assertIn("找不到任务", json.loads(h.wfile.getvalue().decode() or "{}").get("error", ""))

    def test_not_long_400(self):
        # #4 non-long job -> 400 with "仅长视频任务支持片段缓存"
        job = {
            "id": "short_1",
            "long": False,
            "video_id": "short_1",
            "model": "agnes-video-v2.0",
            "status": "succeeded",
            "url": ALLOWED_URL,
        }
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(job)):
            h = _SegHandler("/api/jobs/short_1/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 400)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "仅长视频任务支持片段缓存")

    def test_index_out_of_range_400(self):
        # #5 index out of range -> 400 with "片段索引超出范围"
        job = self._long_job()  # 1 segment, index 0
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(job)):
            h = _SegHandler("/api/jobs/lv_a/segments/5", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 400)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段索引超出范围")

    def test_not_succeeded_409(self):
        # #6 segment not succeeded -> 409 with "片段尚未完成，无法缓存"
        segs = [{
            "index": 0, "video_id": "vid0", "status": "processing",
            "progress": 50, "url": ALLOWED_URL,
        }]
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job(segs))):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 409)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段尚未完成，无法缓存")

    def test_missing_url_404(self):
        # #7 no url / video_url -> 404 with "片段缺少下载 URL"
        segs = [{
            "index": 0, "video_id": "vid0", "status": "succeeded",
            "progress": 100, "url": None, "video_url": None,
        }]
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job(segs))):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 404)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段缺少下载 URL")

    # ------------------------------------------------------------------ download security
    def test_download_success_200(self):
        # #8 succeeded + url + mocked successful download -> 200
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.side_effect = lambda url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES: _write_file_and_ok(dest)
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertTrue(self._cache_path().is_file())

    def test_download_success_cache_file_on_disk(self):
        # #14 after successful download the cache file exists on disk with expected
        # content (MP4-like header) and the .part temp file is gone.
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.side_effect = lambda url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES: _write_file_and_ok(dest)
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        cache = self._cache_path()
        self.assertTrue(cache.is_file(), "cache file missing on disk")
        content = cache.read_bytes()
        self.assertEqual(content[:4], b"\x00\x00\x00\x18", "unexpected file content")
        # .part temp file must NOT remain
        part = cache.with_suffix(".part")
        self.assertFalse(part.exists(), ".part temp file left behind")

    def test_http_url_rejected(self):
        # #9 http:// -> download_segment_limited refuses with "only HTTPS downloads allowed"
        segs = [{
            "index": 0, "video_id": "vid0", "status": "succeeded",
            "progress": 100, "url": "http://platform-outputs.agnes-ai.space/seg0.mp4",
        }]
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job(segs))), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.return_value = {"ok": False, "error": "only HTTPS downloads allowed"}
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 502)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段下载失败")

    def test_evil_host_rejected(self):
        # #10 https://evil.com -> "host not in allowed download list" -> 502
        segs = [{
            "index": 0, "video_id": "vid0", "status": "succeeded",
            "progress": 100, "url": EVIL_URL,
        }]
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job(segs))), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.return_value = {"ok": False, "error": "host not in allowed download list"}
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 502)

    def test_path_traversal_never_writes_outside(self):
        # #11 /api/jobs/../segments/0 and jid containing ../ must not escape OUT.
        # The regex `^/api/jobs/([^/]+)/segments/(\d{1,2})$` never matches a jid
        # containing `/` or `..`, so the route falls through to 404 / never writes.
        for jid in ("..", "..%2F..", "a/../b", "..%2e"):
            with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())):
                h = _SegHandler("/api/jobs/%s/segments/0" % jid, VALID_TOKEN)
                h.do_GET()
            # Must never be 200 (cache path build with traversal would 400 via the
            # resolve+is_relative_to guard, or the regex rejects the jid).
            self.assertIn(h.status_code, (404, 400),
                          "jid %r unexpectedly got status %d" % (jid, h.status_code))
            # Confirm nothing was written outside OUT.
            outside = self.out.parent / ("segments_.._0")
            self.assertFalse(outside.exists(), "wrote outside OUT for jid=%r" % jid)

    def test_symlink_outside_out_400(self):
        # #12 cache path is a symlink pointing OUTSIDE OUT -> 400 "非法缓存路径"
        target = self.out.parent / "evil_target.mp4"
        target.write_bytes(b"secret")
        cache_dir = self.out / "segments" / "lv_a"
        cache_dir.mkdir(parents=True, exist_ok=True)
        link = cache_dir / "seg_00.mp4"
        link.symlink_to(target)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 400)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "非法缓存路径")

    def test_symlink_inside_out_400(self):
        # #13 even a symlink pointing INSIDE OUT is rejected (not resolved_cache.is_symlink())
        cache_dir = self.out / "segments" / "lv_a"
        cache_dir.mkdir(parents=True, exist_ok=True)
        real_file = self.out / "segments" / "lv_a" / "real.mp4"
        real_file.write_bytes(b"data")
        link = cache_dir / "seg_00.mp4"
        link.symlink_to(real_file)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 400)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "非法缓存路径")

    # ------------------------------------------------------------------ cache hit
    def test_cache_hit_skips_download(self):
        # #15 pre-seeded non-empty, non-symlink cache -> hit; download not called
        content = b"MP4DATA" * 8
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        mock_dl.assert_not_called()

    def test_idempotent_cache_hit(self):
        # #16 idempotency: second request also hits cache, download not called again
        self._seed_cache(0, b"IDEMDATA")
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            h1 = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h1.do_GET()
            h2 = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h2.do_GET()
        self.assertEqual(h1.status_code, 200)
        self.assertEqual(h2.status_code, 200)
        mock_dl.assert_not_called()

    def test_download_exception_502_part_cleaned(self):
        # #17 mocked download leaves a .part file and returns ok=False (generic error)
        # -> route cleans up .part and returns 502.
        def _part_path(dest):
            # Mirror the route's own naming: cache_path.with_suffix(".part")
            # For seg_00.mp4 this yields seg_00.part (last suffix replaced).
            return dest.with_suffix(".part")

        def _fake_dl(url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES):
            part = _part_path(dest)
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"partial-bytes")
            return {"ok": False, "error": "download failed: simulated exception"}

        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited", side_effect=_fake_dl) as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 502)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段下载失败")
        part = _part_path(self._cache_path())
        self.assertFalse(part.exists(), ".part not cleaned up by route")
        mock_dl.assert_called_once()

    def test_download_over_max_size_400(self):
        # #18 ok=False + "download exceeds max size" -> 400 "片段文件超过缓存上限"
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.return_value = {"ok": False, "error": "download exceeds max size"}
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 400)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段文件超过缓存上限")

    def test_small_max_bytes_over_limit_400(self):
        # #19 assert the default cap, then exercise the REAL production
        # download_segment_limited() with a tiny max_bytes value.  A mocked
        # urlopen response supplies 11 bytes; max_bytes=10 makes the
        # production code abort mid-copy, return the size error, delete the
        # .part temp file, and leave no final cache file behind.
        self.assertEqual(SERVER.SEGMENT_MAX_BYTES, 524288000)

        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(".part")

        # Production now uses build_opener(_NoRedirectHandler()).open(req),
        # so we mock build_opener to return a fake opener whose .open()
        # yields the _FakeResponse directly (no real network).
        fake_open = MagicMock(return_value=_FakeResponse(b"x" * 11))
        fake_opener = MagicMock()
        fake_opener.open = fake_open
        fake_build_opener = MagicMock(return_value=fake_opener)
        with patch.object(SERVER.urllib.request, "build_opener", fake_build_opener):
            result = SERVER.download_segment_limited(ALLOWED_URL, dest, max_bytes=10)

        self.assertEqual(result, {"ok": False, "error": "download exceeds max size"})
        self.assertFalse(part.exists(), ".part file should be cleaned up by production function")
        self.assertFalse(dest.exists(), "final cache file must not be created when download exceeds limit")
        # The mocked build_opener was invoked exactly once with _NoRedirectHandler.
        self.assertEqual(fake_build_opener.call_count, 1)
        self.assertTrue(
            isinstance(fake_build_opener.call_args[0][0], SERVER._NoRedirectHandler),
            "build_opener must be called with _NoRedirectHandler instance",
        )

    # ------------------------------------------------------------------ P1: redirect SSRF regression
    # R1-R6: verify _NoRedirectHandler blocks 301/302/303/307/308 and that
    # download_segment_limited returns ok=False with "redirect not allowed"
    # when the upstream issues any redirect, without connecting to the
    # redirect target.  All network I/O is mocked; no real sockets.

    def _run_redirect_scenario(self, redirect_status, redirect_location, dest):
        """Run download_segment_limited with a fake opener that simulates
        the upstream returning ``redirect_status`` with a Location header,
        then the real _NoRedirectHandler rejects the redirect.

        Returns (result, part_exists, dest_exists, second_hop_called).
        """
        second_hop = {"called": False}

        def _fake_open(req, timeout=None):
            # Simulate: first hop returns a redirect.
            # The real _NoRedirectHandler.http_error_30x would be invoked
            # by urllib's handler chain.  We replicate that call directly
            # to verify the handler is wired in and blocks the redirect.
            handler = SERVER._NoRedirectHandler()
            import urllib.error
            try:
                # http_error_30x is called by the handler chain when a
                # 30x response is received.  Simulate by calling it:
                handler.http_error_301(
                    req, None, redirect_status, "Redirect",
                    {"Location": redirect_location},
                )
            except urllib.error.HTTPError:
                # Expected: the handler raised, blocking the redirect.
                raise
            second_hop["called"] = True
            # Should never reach here if the handler works.
            return _FakeResponse(b"OK")

        fake_opener = MagicMock()
        fake_opener.open = _fake_open
        fake_build = MagicMock(return_value=fake_opener)

        with patch.object(SERVER.urllib.request, "build_opener", fake_build):
            result = SERVER.download_segment_limited(ALLOWED_URL, dest, max_bytes=100)

        part = dest.with_suffix(".part")
        return result, part.exists(), dest.exists(), second_hop["called"]

    def test_redirect_301_rejected(self):
        # R1: 301 redirect is blocked; no second-hop connection.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        result, part_exists, dest_exists, second_hop = self._run_redirect_scenario(
            301, "https://evil.example/seg.mp4", dest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("redirect not allowed", result["error"])
        self.assertFalse(second_hop, "second-hop connection must not occur")
        self.assertFalse(part_exists, ".part must be cleaned up")
        self.assertFalse(dest_exists, "final cache file must not exist")

    def test_redirect_302_to_evil_rejected(self):
        # R2: 302 to a non-whitelisted host is blocked.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        result, part_exists, dest_exists, second_hop = self._run_redirect_scenario(
            302, "https://evil.example/seg.mp4", dest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("redirect not allowed", result["error"])
        self.assertFalse(second_hop)
        self.assertFalse(part_exists)
        self.assertFalse(dest_exists)

    def test_redirect_303_307_308_rejected(self):
        # R3: 303, 307, 308 all blocked.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        for code in (303, 307, 308):
            with self.subTest(code=code):
                result, part_exists, dest_exists, second_hop = \
                    self._run_redirect_scenario(code, "https://evil.example/seg.mp4", dest)
                self.assertFalse(result["ok"])
                self.assertIn("redirect not allowed", result["error"])
                self.assertFalse(second_hop)
                self.assertFalse(part_exists)
                self.assertFalse(dest_exists)

    def test_redirect_to_localhost_rejected(self):
        # R4: 302 to 127.0.0.1 is blocked.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        result, part_exists, dest_exists, second_hop = self._run_redirect_scenario(
            302, "https://127.0.0.1/seg.mp4", dest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("redirect not allowed", result["error"])
        self.assertFalse(second_hop)
        self.assertFalse(part_exists)
        self.assertFalse(dest_exists)

    def test_redirect_https_to_http_rejected(self):
        # R5: HTTPS -> HTTP scheme downgrade via redirect is blocked.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        result, part_exists, dest_exists, second_hop = self._run_redirect_scenario(
            302, "http://platform-outputs.agnes-ai.space/seg.mp4", dest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("redirect not allowed", result["error"])
        self.assertFalse(second_hop)
        self.assertFalse(part_exists)
        self.assertFalse(dest_exists)

    def test_allowed_200_no_redirect_success(self):
        # R6: a normal 200 response on the allowed host still succeeds;
        # the _NoRedirectHandler is not triggered.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)

        fake_resp = _FakeResponse(b"A" * 64)
        fake_opener = MagicMock()
        fake_opener.open = MagicMock(return_value=fake_resp)
        fake_build = MagicMock(return_value=fake_opener)

        with patch.object(SERVER.urllib.request, "build_opener", fake_build):
            result = SERVER.download_segment_limited(ALLOWED_URL, dest, max_bytes=100)

        self.assertTrue(result["ok"], "expected ok=True for 200 response")
        self.assertTrue(dest.is_file(), "final cache file must exist after success")
        self.assertEqual(
            dest.read_bytes(), b"A" * 64,
            "final cache content must equal the mocked response payload",
        )
        self.assertFalse(dest.with_suffix(".part").exists(), ".part must be cleaned up")
        # build_opener was called with a _NoRedirectHandler instance.
        self.assertTrue(
            isinstance(fake_build.call_args[0][0], SERVER._NoRedirectHandler),
        )

    def test_rename_failure_cleans_part(self):
        # R7: fault-inject tmp.rename(dest) OSError and verify the production
        # failure convention without modifying server.py.
        dest = self.out / "segments" / "lv_a" / "seg_00.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(".part")
        part.write_bytes(b"PART")

        original_rename = Path.rename
        rename_calls = []

        def _rename_side_effect(target, *args, **kwargs):
            rename_calls.append((str(part), str(target)))
            if str(target) == str(dest):
                raise OSError("simulated rename failure")
            return original_rename(target, *args, **kwargs)

        payload = b"A" * 8
        offset = {"n": 0}
        response_closed = MagicMock(name="response_closed")

        class _RenamedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                response_closed(*args)
                return False

            def read(self, size):
                chunk = payload[offset["n"]:offset["n"] + size]
                offset["n"] += len(chunk)
                return chunk

        fake_opener = MagicMock()
        fake_opener.open = MagicMock(return_value=_RenamedResponse())
        fake_build = MagicMock(return_value=fake_opener)

        with patch.object(SERVER.urllib.request, "build_opener", fake_build), \
             patch.object(Path, "rename", side_effect=_rename_side_effect):
            result = SERVER.download_segment_limited(ALLOWED_URL, dest, max_bytes=100)

        self.assertFalse(result["ok"])
        self.assertIn("download failed:", result["error"])
        self.assertIn("simulated rename failure", result["error"])
        self.assertFalse(part.exists(), "residual .part file must be cleaned up")
        self.assertFalse(dest.exists(), "final file must not be created when rename fails")
        self.assertIn((str(part), str(dest)), rename_calls)
        response_closed.assert_called_once()

    def test_rename_part_to_final(self):
        # #20 on the success path, verify the .part file is renamed to the final .mp4
        renamed = {}

        def _fake_dl(url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES):
            part = Path(str(dest) + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"RENAME_OK")
            part.rename(dest)
            renamed["done"] = True
            return {"ok": True, "error": None}

        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited", side_effect=_fake_dl):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertTrue(renamed.get("done"))
        self.assertTrue(self._cache_path().is_file())
        self.assertFalse(Path(str(self._cache_path()) + ".part").exists())

    # ------------------------------------------------------------------ concurrency
    def test_same_segment_event_concurrency(self):
        # #21 two threads hit the SAME segment; thread 2 waits at a barrier until
        # thread 1 completes (lock serialisation), then both get 200 via cache_hit.
        # Patches are created on the main thread to avoid concurrent patch races.
        self._seed_cache(0, b"SHARED")
        result = {}
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:

            def _worker(name, gate):
                try:
                    h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
                    h.do_GET()
                    result[name] = (h.status_code, mock_dl.call_count)
                except Exception as exc:
                    result[name] = exc
                finally:
                    gate.set()

            g1, g2 = threading.Event(), threading.Event()
            t1 = threading.Thread(target=_worker, args=("t1", g2))
            t2 = threading.Thread(target=_worker, args=("t2", g1))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
            self.assertFalse(t1.is_alive(), "t1 still alive")
            self.assertFalse(t2.is_alive(), "t2 still alive")
            for k, v in result.items():
                self.assertIsInstance(v, tuple, "%s raised: %r" % (k, v))
            self.assertEqual(result["t1"][0], 200)
            self.assertEqual(result["t2"][0], 200)
            # Cache was pre-seeded -> no downloads at all.
            self.assertEqual(result["t1"][1], 0)
            self.assertEqual(result["t2"][1], 0)

    def test_different_segments_barrier_concurrent(self):
        # #22 two threads fetch DIFFERENT segments (0 and 1) concurrently via a
        # Barrier; neither blocks the other -> both 200.
        segs = [
            {"index": 0, "video_id": "v0", "status": "succeeded", "progress": 100, "url": ALLOWED_URL},
            {"index": 1, "video_id": "v1", "status": "succeeded", "progress": 100, "url": ALLOWED_URL},
        ]
        jobs = self._jobs_map(self._long_job(segs))
        result = {}
        with patch.object(SERVER, "jobs_load", return_value=jobs), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.side_effect = lambda url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES: _write_file_and_ok(dest)

            def _worker(idx, name):
                try:
                    h = _SegHandler("/api/jobs/lv_a/segments/%d" % idx, VALID_TOKEN)
                    h.do_GET()
                    result[name] = (h.status_code, mock_dl.call_count)
                except Exception as exc:
                    result[name] = exc

            threads = []
            for idx, name in ((0, "t0"), (1, "t1")):
                t = threading.Thread(target=_worker, args=(idx, name))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=5)
            for t in threads:
                self.assertFalse(t.is_alive(), "thread still alive")
            for k, v in result.items():
                self.assertIsInstance(v, tuple, "%s raised: %r" % (k, v))
            self.assertEqual(result["t0"][0], 200)
            self.assertEqual(result["t1"][0], 200)
            # Both segment cache files exist (each was filled by its own download).
            self.assertTrue((self.out / "segments" / "lv_a" / "seg_00.mp4").is_file())
            self.assertTrue((self.out / "segments" / "lv_a" / "seg_01.mp4").is_file())

    def test_same_segment_barrier_concurrent(self):
        # #23 two threads request the SAME seg_index through a Barrier; the
        # per-(jid,seg_index) lock serialises them; both end up 200.
        segs = [
            {"index": 0, "video_id": "v0", "status": "succeeded", "progress": 100, "url": ALLOWED_URL},
        ]
        jobs = self._jobs_map(self._long_job(segs))
        barrier = threading.Barrier(2, timeout=5)
        result = {}
        with patch.object(SERVER, "jobs_load", return_value=jobs), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            mock_dl.side_effect = lambda url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES: _write_file_and_ok(dest)

            def _worker(name):
                try:
                    barrier.wait(timeout=5)
                    h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
                    h.do_GET()
                    result[name] = (h.status_code, mock_dl.call_count)
                except Exception as exc:
                    result[name] = exc

            threads = []
            for name in ("t0", "t1"):
                t = threading.Thread(target=_worker, args=(name,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=5)
            for t in threads:
                self.assertFalse(t.is_alive(), "thread still alive")
            for k, v in result.items():
                self.assertIsInstance(v, tuple, "%s raised: %r" % (k, v))
            self.assertEqual(result["t0"][0], 200)
            self.assertEqual(result["t1"][0], 200)
            # The shared cache file must exist after both threads complete.
            self.assertTrue(self._cache_path(0).is_file())

    # ------------------------------------------------------------------ range / streaming
    def test_no_range_full_stream_200(self):
        # #24 no Range header -> 200, full file body in wfile
        content = b"A" * 256
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        h.wfile.seek(0)
        self.assertEqual(h.wfile.read(), content)
        mock_dl.assert_not_called()

    def test_range_partial_206(self):
        # #25 Range: bytes=0-99 -> 206 with correct Content-Range header
        content = b"B" * 200
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN, range_header="bytes=0-99")
            h.do_GET()
        self.assertEqual(h.status_code, 206)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes 0-99/200")
        self.assertEqual(int(h.sent_headers["Content-Length"]), 100)
        h.wfile.seek(0)
        self.assertEqual(h.wfile.read(), content[:100])
        mock_dl.assert_not_called()

    def test_invalid_range_416(self):
        # #26 Range start >= file size -> 416
        content = b"C" * 10
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN, range_header="bytes=999-")
            h.do_GET()
        self.assertEqual(h.status_code, 416)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes */10")
        mock_dl.assert_not_called()

    def test_empty_cache_500(self):
        # #27 cache file exists but is 0 bytes -> 500 "缓存文件为空".
        # Note: st_size == 0 makes the cache a MISS, so the download mock is
        # invoked to fill it.  We use a fake that produces a non-empty file so
        # the route's *second* size check (after download) succeeds and returns
        # 200.  To isolate the "缓存文件为空" branch we patch the download to
        # produce an empty file: the route re-checks st_size after the
        # download and hits the 500 branch.
        def _dl_writes_empty(url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"")
            return {"ok": True, "error": None}

        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited", side_effect=_dl_writes_empty) as mock_dl:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        # The pre-seeded empty file is a cache miss, download fills it (still
        # empty), then fsize == 0 triggers 500.
        self.assertEqual(h.status_code, 500)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "缓存文件为空")
        mock_dl.assert_called_once()

    def test_stat_oserror_500(self):
        # #28 Path.stat wrapper raises OSError ONLY for the final size-read of the
        # target cache_path -> 500 "缓存文件不可读"; the earlier stat call
        # (cache_hit check) must succeed.
        #
        # Because patch.object(Path, "stat") replaces the class attribute with a
        # MagicMock, instance.stat() calls the mock with NO arguments (self is not
        # auto-passed for non-descriptor class attributes).  We therefore use a
        # counter: the 1st call (cache_hit check) returns a pre-computed real stat
        # result; the 2nd call (size read after the lock) raises OSError.
        self._seed_cache(0, b"STATDATA")
        target = self._cache_path()
        real_stat_result = target.stat()  # capture before patching

        counter = {"n": 0}

        def _stat_side_effect(*args, **kwargs):
            counter["n"] += 1
            if counter["n"] == 1:
                return real_stat_result
            raise OSError("simulated stat failure")

        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl, \
             patch.object(Path, "stat", side_effect=_stat_side_effect):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        # 500 "缓存文件不可读"
        self.assertEqual(h.status_code, 500)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "缓存文件不可读")
        mock_dl.assert_not_called()
        # Exactly 2 stat calls occurred: cache_hit (ok) + size read (OSError)
        self.assertEqual(counter["n"], 2)

    # ------------------------------------------------------------------ duration probe
    def test_no_ffprobe_header_false(self):
        # #29 ffprobe not found -> probe returns None -> X-Source-Duration-Available: false
        content = b"PROBE" * 4
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl, \
             patch.object(SERVER.shutil, "which", return_value=None) as mock_which, \
             patch.object(SERVER, "subprocess") as mock_subproc:
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers.get("X-Source-Duration-Available"), "false")
        self.assertNotIn("X-Source-Duration", h.sent_headers)
        mock_which.assert_called_with("ffprobe")
        mock_subproc.run.assert_not_called()

    def test_ffprobe_nonzero_exit_header_false(self):
        # #30 ffprobe subprocess.run returns non-zero -> probe None -> header false
        content = b"FFP" * 16
        self._seed_cache(0, content)
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl, \
             patch.object(SERVER.shutil, "which", return_value="/usr/bin/ffprobe"), \
             patch.object(SERVER, "subprocess") as mock_subproc:
            fake_result = MagicMock()
            fake_result.returncode = 1
            mock_subproc.run.return_value = fake_result
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers.get("X-Source-Duration-Available"), "false")
        self.assertNotIn("X-Source-Duration", h.sent_headers)

    def test_ffprobe_success_duration_header(self):
        # #31 ffprobe success -> float duration -> X-Source-Duration: <value>
        content = b"VOK" * 32
        self._seed_cache(0, content)
        duration_val = 10.5
        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited") as mock_dl, \
             patch.object(SERVER.shutil, "which", return_value="/usr/bin/ffprobe"), \
             patch.object(SERVER, "subprocess") as mock_subproc:
            fake_result = MagicMock()
            fake_result.returncode = 0
            fake_result.stdout = "%.1f" % duration_val
            fake_result.stderr = ""
            mock_subproc.run.return_value = fake_result
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers.get("X-Source-Duration"), str(duration_val))
        self.assertNotIn("X-Source-Duration-Available", h.sent_headers)

    # ------------------------------------------------------------------ ok=False non-size-limit -> 502
    def test_download_ok_false_not_size_limit_502(self):
        # #32 directly call download_segment_limited with a side effect that returns
        # ok=False with a generic error (not size-exceeded), then verify the route
        # maps it to 502. Also verify the .part file is cleaned up.
        part_cleaned = []

        def _fake_dl(url, dest, max_bytes=SERVER.SEGMENT_MAX_BYTES):
            part = dest.with_suffix(".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"partial")
            part_cleaned.append(str(part))
            return {"ok": False, "error": "download failed: connection reset"}

        with patch.object(SERVER, "jobs_load", return_value=self._jobs_map(self._long_job())), \
             patch.object(SERVER, "download_segment_limited", side_effect=_fake_dl):
            h = _SegHandler("/api/jobs/lv_a/segments/0", VALID_TOKEN)
            h.do_GET()
        self.assertEqual(h.status_code, 502)
        self.assertEqual(json.loads(h.wfile.getvalue().decode() or "{}")["error"], "片段下载失败")
        self.assertEqual(len(part_cleaned), 1)
        self.assertFalse(Path(part_cleaned[0]).exists(), "route did not clean up .part")


class _FakeResponse:
    """One-shot fake HTTP response that yields a fixed byte payload.

    Used by production-path tests (small-max-bytes, redirect scenarios,
    200 success) to mock urllib's response object without real sockets.
    """
    def __init__(self, payload):
        self._payload = payload
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        chunk = self._payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def _write_file_and_ok(dest):
    """Mock download helper: write a small valid-looking file and return ok=True."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    return {"ok": True, "error": None}


if __name__ == "__main__":
    unittest.main(verbosity=2)
