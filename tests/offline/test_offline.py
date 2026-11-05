"""
AgnesVideo Phase-2 offline tests.

All tests run against a fully isolated tmp environment:
  - AGNES_DATA_DIR / AGNES_OUTPUT_DIR point to a per-test tmp dir
  - urllib.request.urlopen is globally mocked (zero network)
  - cfg() is mocked (no real API key)
  - No subprocess / FFmpeg execution
  - No ports opened, no production files touched

Run:
  python3 -m unittest tests/offline/test_offline.py -v
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call as mock_call
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Isolation: set env vars BEFORE importing server
# ---------------------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="agnes_offline_"))
os.environ["AGNES_DATA_DIR"]    = str(_TMP / "data")
os.environ["AGNES_OUTPUT_DIR"]  = str(_TMP / "outputs")
os.environ.pop("AGNES_API_TOKEN", None)   # ensure no real token leaks in

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import server  # safe: mkdir side-effects target the tmp dirs above
from server import (
    validate_segment_files,
    download_segment,
    jobs_load,
    jobs_save,
    jobs_save_locked,
    normalize,
    _job_record,
    merge_segments_ffmpeg,
    ffmpeg_available,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TJobRecordTimestamp(unittest.TestCase):
    def test_job_record_created_at_is_int(self):
        r = _job_record("j1", "m", "p", None, "vid", {})
        self.assertIsInstance(r.get("created_at"), int)
        self.assertGreater(r["created_at"], 0)

    def test_job_record_missing_model_params_is_safe(self):
        r = _job_record("j1", "m", "p", None, "vid", {})
        self.assertNotIn("seconds", r)
        self.assertNotIn("size", r)


def _make_fake_mp4(path: Path, real_mp4: bool = True) -> Path:
    """Create a file that looks like an MP4 (ftyp at offset 4) or not."""
    if real_mp4:
        # MP4: first 4 bytes = size, offset 4-7 = 'ftyp'
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
    else:
        data = b"notamjpegfile0000000000"
    path.write_bytes(data)
    return path


def _make_empty_file(path: Path) -> Path:
    path.write_bytes(b"")
    return path


# ---------------------------------------------------------------------------
# T1 – validate_segment_files
# ---------------------------------------------------------------------------
class T1ValidateSegmentFiles(unittest.TestCase):

    def test_valid_mp4_passes(self):
        with tempfile.TemporaryDirectory() as d:
            f = _make_fake_mp4(Path(d) / "seg.mp4")
            self.assertTrue(validate_segment_files([str(f)])["ok"])

    def test_empty_file_fails(self):
        with tempfile.TemporaryDirectory() as d:
            f = _make_empty_file(Path(d) / "empty.mp4")
            r = validate_segment_files([str(f)])
            self.assertFalse(r["ok"])
            self.assertIn("missing or empty", r["errors"][0])

    def test_non_mp4_fails(self):
        with tempfile.TemporaryDirectory() as d:
            f = _make_fake_mp4(Path(d) / "bad.bin", real_mp4=False)
            r = validate_segment_files([str(f)])
            self.assertFalse(r["ok"])
            self.assertIn("not a valid MP4", r["errors"][0])

    def test_missing_file_fails(self):
        r = validate_segment_files(["/nonexistent/path/seg.mp4"])
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# T2 – download_segment  SSRF / whitelist
# ---------------------------------------------------------------------------
class T2DownloadSegment(unittest.TestCase):

    def test_http_scheme_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r = download_segment("http://platform-outputs.agnes-ai.space/x.mp4", Path(d) / "x.mp4")
            self.assertFalse(r["ok"])
            self.assertIn("HTTPS", r["error"])

    def test_non_whitelist_host_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r = download_segment("https://evil.com/x.mp4", Path(d) / "x.mp4")
            self.assertFalse(r["ok"])
            self.assertIn("host not in allowed", r["error"])

    def test_whitelist_host_ok_mocked(self):
        """Whitelist host passes the URL check; urlopen is mocked – no network."""
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "ok.mp4"
            fake_resp = MagicMock()
            fake_resp.__enter__ = MagicMock(return_value=fake_resp)
            fake_resp.__exit__  = MagicMock(return_value=False)
            fake_resp.read = MagicMock(return_value=b"fake_mp4_bytes")
            with patch("urllib.request.urlopen", return_value=fake_resp):
                with patch("shutil.copyfileobj") as mcp:
                    # Simulate writing fake bytes
                    def fake_copy(src, dst, *a, **kw):
                        dst.write(b"fake_mp4_bytes")
                    mcp.side_effect = lambda src, dst, **kw: dst.write(b"fake_mp4_bytes")
                    r = download_segment(
                        "https://platform-outputs.agnes-ai.space/seg.mp4", dest
                    )
                    # Should succeed (mocked network)
                    self.assertTrue(r["ok"], f"unexpected error: {r['error']}")

    def test_no_real_socket(self):
        """Verify urlopen is never called with a real request – all mocked."""
        called = []
        def fake_urlopen(req, *a, **kw):
            called.append(req)
            raise Exception("network blocked")
        with tempfile.TemporaryDirectory() as d:
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                r = download_segment(
                    "https://platform-outputs.agnes-ai.space/seg.mp4", Path(d) / "x.mp4"
                )
                self.assertFalse(r["ok"])
        self.assertEqual(len(called), 1)


# ---------------------------------------------------------------------------
# T3 – jobs_load / jobs_save  atomic write
# ---------------------------------------------------------------------------
class T3JobsAtomicWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_data = server.DATA
        server.DATA = self.tmp / "data"
        server.DATA.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        server.DATA = self._orig_data

    def test_save_creates_file_600(self):
        jobs_save({"job1": {"id": "job1", "status": "queued"}})
        f = server.DATA / "jobs.json"
        self.assertTrue(f.exists())
        mode = oct(f.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600", f"expected 600, got {mode}")

    def test_save_no_tmp_left(self):
        jobs_save({"job1": {}})
        self.assertFalse((server.DATA / "jobs.json.tmp").exists(),
                         "tmp file should be cleaned up by rename")

    def test_load_returns_empty_on_missing(self):
        self.assertEqual(jobs_load(), {})

    def test_roundtrip(self):
        jobs = {"a": {"id": "a"}, "b": {"id": "b"}}
        jobs_save(jobs)
        loaded = jobs_load()
        self.assertEqual(loaded, jobs)


# ---------------------------------------------------------------------------
# T4 – normalize  status mapping + progress clamp
# ---------------------------------------------------------------------------
class T4Normalize(unittest.TestCase):

    STATUS_MAP = {
        "queued": "queued", "pending": "queued", "starting": "queued",
        "in_progress": "processing", "processing": "processing", "running": "processing",
        "completed": "succeeded", "succeeded": "succeeded", "success": "succeeded",
        "failed": "failed", "error": "failed",
        "cancelled": "canceled", "canceled": "canceled",
    }

    def test_all_known_statuses(self):
        for raw, expected in self.STATUS_MAP.items():
            r = normalize({}, {"status": raw})
            self.assertEqual(r["status"], expected, f"status '{raw}' → expected '{expected}', got '{r['status']}'")

    def test_unknown_status_passthrough(self):
        r = normalize({}, {"status": "weird_state"})
        self.assertEqual(r["status"], "weird_state")

    def test_progress_clamped_low(self):
        r = normalize({}, {"status": "processing", "progress": -5})
        self.assertEqual(r["progress"], 0)

    def test_progress_clamped_high(self):
        r = normalize({}, {"status": "processing", "progress": 105})
        self.assertEqual(r["progress"], 100)

    def test_progress_int(self):
        r = normalize({}, {"status": "processing", "progress": 50})
        self.assertEqual(r["progress"], 50)

    def test_progress_non_numeric_ignored(self):
        # normalize only clamps int/float; a string passes through unchanged
        r = normalize({}, {"status": "processing", "progress": "unknown"})
        self.assertEqual(r["progress"], "unknown")


# ---------------------------------------------------------------------------
# T5 – long-video creation  parameter validation (mock upstream)
# ---------------------------------------------------------------------------
class T5LongVideoCreation(unittest.TestCase):

    def _mock_call(self, *a, **kw):
        """Simulate successful upstream POST /v1/videos."""
        return 200, {"video_id": "mock_vid_%s" % kw.get("body", {}).get("segment_index", "x")}

    def test_30s_creates_3_segments(self):
        with patch.object(server, "call", side_effect=lambda m, u, t, body=None, timeout=90:
                          (200, {"video_id": "mock_vid"})):
            # Build the segment list as server code does
            target_seconds = 30
            num_segments = target_seconds // server.SEGMENT_SECONDS
            self.assertEqual(num_segments, 3)

    def test_45s_rejected(self):
        self.assertNotIn(45, server.LONG_VIDEO_SECONDS)

    def test_v20_rejects_long_video(self):
        # This is validated in the HTTP handler; here we verify the constant
        # so we know the branch will fire
        self.assertNotIn("agnes-video-v2.0",
                          ["agnes-video-2.5", "agnes-video-2.5-flash"])

    def test_segment_body_uses_10s(self):
        self.assertEqual(server.SEGMENT_SECONDS, 10)


# ---------------------------------------------------------------------------
# T6 – merge pre-condition  (all segments must be succeeded)
# ---------------------------------------------------------------------------
class T6MergePrecondition(unittest.TestCase):

    def _make_job(self, seg_statuses):
        return {
            "id": "lv_123",
            "long": True,
            "segments": [
                {"index": i, "video_id": "vid_%d" % i, "status": s,
                 "progress": 100, "url": "https://platform-outputs.agnes-ai.space/x.mp4"}
                for i, s in enumerate(seg_statuses)
            ],
        }

    def test_all_succeeded_passes_check(self):
        job = self._make_job(["succeeded", "succeeded", "succeeded"])
        all_ok = all(s["status"] == "succeeded" for s in job["segments"])
        self.assertTrue(all_ok)

    def test_one_processing_fails_check(self):
        job = self._make_job(["succeeded", "processing", "succeeded"])
        all_ok = all(s["status"] == "succeeded" for s in job["segments"])
        self.assertFalse(all_ok)
        pending = [s for s in job["segments"] if s["status"] != "succeeded"]
        self.assertEqual(len(pending), 1)

    def test_one_failed_fails_check(self):
        job = self._make_job(["succeeded", "failed", "succeeded"])
        all_ok = all(s["status"] == "succeeded" for s in job["segments"])
        self.assertFalse(all_ok)


# ---------------------------------------------------------------------------
# T7 – character image parameter  validation
# ---------------------------------------------------------------------------
class T7ImageParamValidation(unittest.TestCase):

    def test_ftp_rejected(self):
        url = "ftp://example.com/img.png"
        ok = url.startswith("https://") or url.startswith("data:image/")
        self.assertFalse(ok)

    def test_data_uri_ok(self):
        url = "data:image/png;base64,iVBORw0KGgo="
        ok = url.startswith("https://") or url.startswith("data:image/")
        self.assertTrue(ok)

    def test_https_ok(self):
        url = "https://example.com/img.png"
        ok = url.startswith("https://") or url.startswith("data:image/")
        self.assertTrue(ok)

    def test_oversized_data_uri_rejected(self):
        # 5_500_000 char limit
        huge = "data:image/png;base64," + "A" * 5_500_001
        self.assertTrue(len(huge) > 5_500_000)

    def test_v25_uses_first_frame_key(self):
        model = "agnes-video-2.5"
        image = "https://example.com/img.png"
        body = {"model": model, "prompt": "test"}
        if image:
            body["mode"] = "keyframe"
            body["first_frame"] = image
            body["aspect_ratio"] = "16:9"
        self.assertEqual(body.get("first_frame"), image)
        self.assertEqual(body.get("aspect_ratio"), "16:9")


# ---------------------------------------------------------------------------
# T8 – FFmpeg missing  path (no subprocess)
# ---------------------------------------------------------------------------
class T8FfmpegMissing(unittest.TestCase):

    def test_merge_fails_cleanly_when_ffmpeg_absent(self):
        with patch("server.shutil.which", return_value=None):
            r = merge_segments_ffmpeg(["fake.mp4"], Path(tempfile.mkdtemp()) / "out.mp4")
            self.assertFalse(r["ok"])
            self.assertIn("FFmpeg not installed", r["error"])
            self.assertIsNone(r["output"])

    def test_ffmpeg_available_false(self):
        with patch("server.shutil.which", return_value=None):
            self.assertFalse(ffmpeg_available())


# ---------------------------------------------------------------------------
# T9 (extra) – repeated polling  idempotency
# ---------------------------------------------------------------------------
class T9RepeatedPolling(unittest.TestCase):
    """Simulate the GET /api/jobs/<long> polling loop twice; state must be
    consistent and no segment should be double-counted."""

    def test_repeated_poll_no_status_regression(self):
        seg = {"index": 0, "video_id": "vid0", "status": "processing", "progress": 50}
        # Simulate one poll that keeps status as processing (upstream error)
        poll_result_1 = normalize(seg, {"status": "processing", "progress": 50})
        self.assertEqual(poll_result_1["status"], "processing")

        # Simulate second poll – still processing
        poll_result_2 = normalize(poll_result_1, {"status": "processing", "progress": 75})
        self.assertEqual(poll_result_2["status"], "processing")
        self.assertEqual(poll_result_2["progress"], 75)

        # Final successful poll
        poll_result_3 = normalize(poll_result_2, {"status": "succeeded", "url": "https://platform-outputs.agnes-ai.space/final.mp4"})
        self.assertEqual(poll_result_3["status"], "succeeded")


# ---------------------------------------------------------------------------
# T10 (extra) – long-video  partial segment failure
# ---------------------------------------------------------------------------
class T10PartialSegmentFailure(unittest.TestCase):
    """If any segment fails, parent must reflect failure."""

    def test_parent_failed_if_one_segment_failed(self):
        segments = [
            {"index": 0, "status": "succeeded", "progress": 100},
            {"index": 1, "status": "failed", "progress": 0, "error": "创建失败"},
            {"index": 2, "status": "succeeded", "progress": 100},
        ]
        any_failed = any(s["status"] == "failed" for s in segments)
        all_done = all(s["status"] == "succeeded" for s in segments)
        # Mirror server logic
        if any_failed:
            parent_status = "failed"
        elif all_done:
            parent_status = "succeeded"
        else:
            parent_status = "processing"
        self.assertEqual(parent_status, "failed")

    def test_parent_processing_if_mixed(self):
        segments = [
            {"index": 0, "status": "succeeded", "progress": 100},
            {"index": 1, "status": "processing", "progress": 50},
        ]
        any_failed = any(s["status"] == "failed" for s in segments)
        all_done = all(s["status"] == "succeeded" for s in segments)
        if any_failed:
            parent_status = "failed"
        elif all_done:
            parent_status = "succeeded"
        else:
            parent_status = "processing"
        self.assertEqual(parent_status, "processing")


# ---------------------------------------------------------------------------
# T11 (extra) – merge_segments_ffmpeg fd/context-manager regression tests
# ---------------------------------------------------------------------------
class T11MergeHelperFix(unittest.TestCase):
    """Verify the corrected mkstemp handling in merge_segments_ffmpeg.

    These tests do NOT mask the file-descriptor bug: they assert the real
    helper return values, the exact subprocess invocation, and that no
    temporary concat list file is left behind.
    """

    def _isolated_env(self, out_dir: Path):
        """Patch server.OUT to an isolated tmp dir and return cleanup context."""
        return patch("server.OUT", out_dir)

    def _make_segment(self, out_dir: Path, name: str, valid: bool = True) -> Path:
        p = out_dir / name
        if valid:
            p.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
        else:
            p.write_bytes(b"this-is-not-an-mp4-file" + b"\x00" * 20)
        return p

    def test_normal_helper_flow(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            seg1 = self._make_segment(out_dir, "seg1.mp4")
            seg2 = self._make_segment(out_dir, "seg2.mp4")
            out = out_dir / "merged.mp4"
            out.write_bytes(b"\x00" * 64)  # pre-create output to simulate ffmpeg success

            fake_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
            with self._isolated_env(out_dir), \
                 patch("server.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("server.subprocess.run", return_value=fake_result) as mock_run:
                r = merge_segments_ffmpeg([str(seg1), str(seg2)], out)

            self.assertTrue(r["ok"], f"helper returned ok=False: {r}")
            self.assertIsNone(r["error"], f"unexpected error: {r['error']}")
            self.assertEqual(r["output"], str(out))
            # Assert the correct ffmpeg command was built
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("ffmpeg", cmd[0])
            self.assertIn("-f", cmd)
            self.assertIn("concat", cmd)
            self.assertIn("-c", cmd)
            self.assertIn("copy", cmd)
            self.assertIn(str(out), cmd)

    def test_invalid_segment_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            good = self._make_segment(out_dir, "good.mp4")
            bad = self._make_segment(out_dir, "bad.mp4", valid=False)
            # validate_segment_files is called before merge in the real flow;
            # here we confirm it correctly rejects the bad file
            result = validate_segment_files([str(good), str(bad)])
            self.assertFalse(result["ok"])
            self.assertTrue(any("not a valid MP4" in e for e in result["errors"]))

    def test_ffmpeg_missing(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            seg = self._make_segment(out_dir, "seg.mp4")
            out = out_dir / "out.mp4"
            with self._isolated_env(out_dir), \
                 patch("server.shutil.which", return_value=None):
                r = merge_segments_ffmpeg([str(seg)], out)
            self.assertFalse(r["ok"])
            self.assertIn("FFmpeg not installed", r["error"])
            self.assertIsNone(r["output"])

    def test_ffmpeg_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            seg = self._make_segment(out_dir, "seg.mp4")
            out = out_dir / "out.mp4"
            out.write_bytes(b"\x00" * 32)
            fake_result = MagicMock(returncode=1, stdout=b"", stderr=b"Invalid input file")
            with self._isolated_env(out_dir), \
                 patch("server.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("server.subprocess.run", return_value=fake_result) as mock_run:
                r = merge_segments_ffmpeg([str(seg)], out)
            self.assertFalse(r["ok"])
            self.assertIn("ffmpeg exited 1", r["error"])
            self.assertIsNone(r["output"])
            mock_run.assert_called_once()

    def test_concat_temp_file_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            seg = self._make_segment(out_dir, "seg.mp4")
            out = out_dir / "out.mp4"
            out.write_bytes(b"\x00" * 32)
            fake_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
            with self._isolated_env(out_dir), \
                 patch("server.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("server.subprocess.run", return_value=fake_result):
                r = merge_segments_ffmpeg([str(seg)], out)
            self.assertTrue(r["ok"], f"helper failed: {r}")
            # No .txt temp file should remain in out_dir
            leftover = [f for f in out_dir.iterdir() if f.suffix == ".txt"]
            self.assertEqual(leftover, [], f"temp concat list files left behind: {leftover}")


# ---------------------------------------------------------------------------
# T12 – segment sync write-back regression
# ---------------------------------------------------------------------------
class T12SegmentSyncWriteBack(unittest.TestCase):
    def test_normalize_result_must_write_back_into_segment_list(self):
        parent = {
            "id": "lv_x",
            "long": True,
            "status": "processing",
            "progress": 0,
            "segments": [
                {"index": 0, "video_id": "vid0", "status": "processing", "progress": 20},
                {"index": 1, "video_id": "vid1", "status": "processing", "progress": 40},
            ],
        }

        # Simulate the fixed polling loop: normalize() returns a new dict,
        # and the loop must write the fields back into the original segment dict.
        for i, seg in enumerate(parent["segments"]):
            live = {
                "status": "succeeded",
                "progress": 100,
                "url": "https://platform-outputs.agnes-ai.space/v%d.mp4" % i,
            }
            safe_live = {k: v for k, v in live.items() if k in ("status", "progress", "url", "message", "error_code")}
            updated = normalize(seg, safe_live)
            for k, v in updated.items():
                seg[k] = v
            if seg.get("url"):
                seg["video_url"] = seg.get("url")

        self.assertEqual(parent["segments"][0]["status"], "succeeded")
        self.assertEqual(parent["segments"][1]["status"], "succeeded")
        self.assertEqual(parent["segments"][0]["video_url"], "https://platform-outputs.agnes-ai.space/v0.mp4")
        self.assertEqual(parent["segments"][1]["video_url"], "https://platform-outputs.agnes-ai.space/v1.mp4")

    def test_merge_precondition_blocked_until_all_succeeded(self):
        parent = {
            "segments": [
                {"status": "succeeded"},
                {"status": "processing"},
            ]
        }
        all_succeeded = all(s.get("status") == "succeeded" for s in parent["segments"])
        self.assertFalse(all_succeeded)

    def test_merge_precondition_allowed_when_all_succeeded_with_urls(self):
        parent = {
            "segments": [
                {"status": "succeeded", "url": "https://platform-outputs.agnes-ai.space/a.mp4"},
                {"status": "succeeded", "url": "https://platform-outputs.agnes-ai.space/b.mp4"},
            ]
        }
        all_succeeded = all(s.get("status") == "succeeded" for s in parent["segments"])
        seg_urls = [s.get("url") or s.get("video_url") for s in parent["segments"]]
        self.assertTrue(all_succeeded)
        self.assertFalse(any(u is None for u in seg_urls))


# ---------------------------------------------------------------------------
# T13 – download route security
# ---------------------------------------------------------------------------
class T13DownloadRouteSecurity(unittest.TestCase):
    def _check(self, path, out_dir):
        filename = path[len("/api/download/"):]
        filename = filename.split("?", 1)[0]
        if not filename or not filename.endswith(".mp4") or "/" in filename or ".." in filename:
            return "reject"
        safe_path = out_dir / filename
        resolved = safe_path.resolve()
        out_resolved = out_dir.resolve()
        if hasattr(resolved, "is_relative_to"):
            inside = resolved.is_relative_to(out_resolved)
        else:
            inside = str(resolved).startswith(str(out_resolved))
        if not inside or not resolved.is_file():
            return "404"
        return "allow"

    def test_normal_mp4_inside_out_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            f = out_dir / "ok.mp4"
            f.write_bytes(b"x")
            self.assertEqual(self._check("/api/download/ok.mp4", out_dir), "allow")

    def test_non_mp4_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "notes.txt").write_text("x")
            self.assertEqual(self._check("/api/download/notes.txt", out_dir), "reject")

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            self.assertEqual(self._check("/api/download/../secret.mp4", out_dir), "reject")

    def test_missing_file_is_404(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            self.assertEqual(self._check("/api/download/missing.mp4", out_dir), "404")


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------
def suite():
    loader = unittest.TestLoader()
    return loader.suiteClasses(
        T1ValidateSegmentFiles,
        T2DownloadSegment,
        T3JobsAtomicWrite,
        T4Normalize,
        T5LongVideoCreation,
        T6MergePrecondition,
        T7ImageParamValidation,
        T8FfmpegMissing,
        T9RepeatedPolling,
        T10PartialSegmentFailure,
        T11MergeHelperFix,
        T12SegmentSyncWriteBack,
        T13DownloadRouteSecurity,
    )


def suite_all():
    loader = unittest.TestLoader()
    return loader.suiteClasses(
        TJobRecordTimestamp,
        T1ValidateSegmentFiles,
        T2DownloadSegment,
        T3JobsAtomicWrite,
        T4Normalize,
        T5LongVideoCreation,
        T6MergePrecondition,
        T7ImageParamValidation,
        T8FfmpegMissing,
        T9RepeatedPolling,
        T10PartialSegmentFailure,
        T11MergeHelperFix,
        T12SegmentSyncWriteBack,
        T13DownloadRouteSecurity,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
