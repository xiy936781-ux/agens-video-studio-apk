"""Targeted offline tests for the merge_segments_ffmpeg fd/with fix.

Covers:
- normal helper flow (subprocess mocked, real file writes for concat list)
- invalid segment file (missing / empty / non-MP4)
- FFmpeg missing
- FFmpeg non-zero exit
- concat temp file cleanup
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Isolated env BEFORE import
_TMP = Path(tempfile.mkdtemp(prefix="agnes_merge_fix_test_"))
os.environ["AGNES_DATA_DIR"] = str(_TMP / "data")
os.environ["AGNES_OUTPUT_DIR"] = str(_TMP / "outputs")
os.environ.pop("AGNES_API_TOKEN", None)

import server  # noqa: E402
from server import (  # noqa: E402
    merge_segments_ffmpeg,
    ffmpeg_available,
    validate_segment_files,
)


def _mk_mp4(path: Path, valid: bool = True) -> Path:
    if valid:
        path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    else:
        path.write_bytes(b"not_an_mp4_file" + b"\x00" * 20)
    return path


class TestMergeFix(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agnes_merge_case_"))
        self._orig_out = server.OUT
        server.OUT = self.tmp

    def tearDown(self):
        server.OUT = self._orig_out

    # ------------------------------------------------------------------
    # 1) Normal helper flow: subprocess returns 0, output file created
    # ------------------------------------------------------------------
    def test_normal_flow_ok(self):
        seg1 = _mk_mp4(self.tmp / "seg1.mp4")
        seg2 = _mk_mp4(self.tmp / "seg2.mp4")
        out = self.tmp / "merged.mp4"
        # Pre-create the output file (simulates FFmpeg writing it)
        out.write_bytes(b"\x00" * 64)

        mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch.object(server, "ffmpeg_available", return_value=True), \
             patch.object(server.subprocess, "run", return_value=mock_result) as mock_run:
            r = merge_segments_ffmpeg([str(seg1), str(seg2)], out)
            self.assertTrue(r["ok"], r)
            self.assertIsNone(r["error"])
            self.assertEqual(r["output"], str(out))
            # Assert subprocess was called with the correct ffmpeg command
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertIn("ffmpeg", args[0])
            self.assertIn("-f", args)
            self.assertIn("concat", args)
            self.assertIn("-c", args)
            self.assertIn("copy", args)

    # ------------------------------------------------------------------
    # 2) Invalid segment: missing file
    # ------------------------------------------------------------------
    def test_invalid_segment_missing(self):
        r = validate_segment_files([str(self.tmp / "nonexistent.mp4")])
        self.assertFalse(r["ok"])
        self.assertIn("missing or empty", r["errors"][0])

    # 3) Invalid segment: empty file
    def test_invalid_segment_empty(self):
        f = self.tmp / "empty.mp4"
        f.write_bytes(b"")
        r = validate_segment_files([str(f)])
        self.assertFalse(r["ok"])
        self.assertIn("missing or empty", r["errors"][0])

    # 4) Invalid segment: non-MP4 bytes
    def test_invalid_segment_non_mp4(self):
        f = _mk_mp4(self.tmp / "bad.mp4", valid=False)
        r = validate_segment_files([str(f)])
        self.assertFalse(r["ok"])
        self.assertIn("not a valid MP4", r["errors"][0])

    # ------------------------------------------------------------------
    # 5) FFmpeg missing
    # ------------------------------------------------------------------
    def test_ffmpeg_missing(self):
        seg = _mk_mp4(self.tmp / "seg.mp4")
        out = self.tmp / "out.mp4"
        with patch.object(server, "ffmpeg_available", return_value=False):
            r = merge_segments_ffmpeg([str(seg)], out)
            self.assertFalse(r["ok"])
            self.assertIn("FFmpeg not installed", r["error"])
            self.assertIsNone(r["output"])

    # ------------------------------------------------------------------
    # 6) FFmpeg non-zero exit
    # ------------------------------------------------------------------
    def test_ffmpeg_nonzero_exit(self):
        seg = _mk_mp4(self.tmp / "seg.mp4")
        out = self.tmp / "out.mp4"
        mock_result = MagicMock(returncode=1, stdout=b"", stderr=b"Error: invalid input")
        with patch.object(server, "ffmpeg_available", return_value=True), \
             patch.object(server.subprocess, "run", return_value=mock_result):
            r = merge_segments_ffmpeg([str(seg)], out)
            self.assertFalse(r["ok"])
            self.assertIn("ffmpeg exited 1", r["error"])
            self.assertIsNone(r["output"])

    # ------------------------------------------------------------------
    # 7) Concat temp file cleanup
    # ------------------------------------------------------------------
    def test_concat_temp_cleaned_up(self):
        seg = _mk_mp4(self.tmp / "seg.mp4")
        out = self.tmp / "out.mp4"
        out.write_bytes(b"\x00" * 32)
        mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch.object(server, "ffmpeg_available", return_value=True), \
             patch.object(server.subprocess, "run", return_value=mock_result):
            r = merge_segments_ffmpeg([str(seg)], out)
            self.assertTrue(r["ok"])
        # After the call, no .txt temp file should remain in self.tmp (OUT dir)
        leftover = list(self.tmp.glob("*.txt"))
        self.assertEqual(leftover, [], f"leftover concat list files: {leftover}")

    # ------------------------------------------------------------------
    # 8) No fd/context-manager error: the old bug is fixed
    # ------------------------------------------------------------------
    def test_no_context_manager_error(self):
        """Regression: the old code raised TypeError for `with os.write(...)`.
        The fixed code must NOT raise that error."""
        seg = _mk_mp4(self.tmp / "seg.mp4")
        out = self.tmp / "out.mp4"
        out.write_bytes(b"\x00" * 32)
        mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch.object(server, "ffmpeg_available", return_value=True), \
             patch.object(server.subprocess, "run", return_value=mock_result):
            r = merge_segments_ffmpeg([str(seg)], out)
            # If the old bug were present, r["error"] would contain "context manager"
            err = r.get("error") or ""
            self.assertNotIn("context manager", err,
                             f"old bug still present: {r}")
            self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
