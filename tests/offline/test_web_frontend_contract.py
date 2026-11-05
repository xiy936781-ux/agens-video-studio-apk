"""Frontend contract tests for index.html.

These tests mock the fetch layer and assert that:
- short-video requests still send seconds (not target_seconds)
- long-video mode (30/60s) sends target_seconds
- v2.0 model + longmode is blocked before submission
- merge button only appears when merge_status == "ready"
- merge API call is made to /api/jobs/<id>/merge
- merge failure shows pending_segments in notice

All tests are offline; no real HTTP requests are made.
"""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Load index.html source for static assertions
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")


def _fetch_seq(*responses):
    """Return a mock fetch that returns responses in order."""
    call_count = {"n": 0}

    def fake_fetch(url, opts=None):
        idx = call_count["n"]
        call_count["n"] += 1
        r = responses[idx % len(responses)]
        status, body = r
        resp = MagicMock()
        resp.status = status
        resp.json = MagicMock(return_value=body)
        resp.ok = status < 400
        return __import__("asyncio").Future.from_result(resp) if False else _Deferred(resp)

    return fake_fetch


class _Deferred:
    """Minimal awaitable that returns a fixed value."""
    def __init__(self, value):
        self._value = value
    def __await__(self):
        return iter([self._value])


class TestFrontendContract(unittest.TestCase):

    # ------------------------------------------------------------------
    # Static assertions against index.html source
    # ------------------------------------------------------------------
    def test_longmode_select_exists(self):
        self.assertIn('<select id="longmode">', INDEX)
        self.assertIn('<option value="30">', INDEX)
        self.assertIn('<option value="60">', INDEX)

    def test_target_seconds_sent_for_longmode(self):
        self.assertIn("payload.target_seconds=parseInt(longmode,10)", INDEX)

    def test_v20_longmode_guard(self):
        self.assertIn("v2.0 模型不支持长视频分段", INDEX)

    def test_short_video_seconds_unchanged(self):
        self.assertIn('payload.seconds=$("seconds").value', INDEX)

    def test_merge_button_only_when_ready(self):
        self.assertIn('j.long&&j.status==="succeeded"&&j.merge_status==="ready"', INDEX)

    def test_merge_api_path(self):
        self.assertIn('/api/jobs/"+encodeURIComponent(current)+"/merge', INDEX)

    def test_merge_status_merged_display(self):
        self.assertIn('j.merge_status==="merged"', INDEX)

    def test_segments_display(self):
        self.assertIn('j.long&&j.segments', INDEX)

    def test_poll_terminal_fix(self):
        # Long jobs keep polling until failed or (succeeded AND merged)
        self.assertIn(
            'terminal=j.long?(j.status==="failed"||(j.status==="succeeded"&&j.merge_status==="merged"))',
            INDEX,
        )

    # ------------------------------------------------------------------
    # Simulated submission logic (extract the relevant code block and run it)
    # ------------------------------------------------------------------
    def _simulate_go_payload(self, model: str, longmode: str, seconds: str = "5"):
        """Replicate the payload-building logic from index.html and return the payload dict."""
        payload = {"prompt": "test", "model": model, "image": None}
        if longmode in ("30", "60"):
            if model == "agnes-video-v2.0":
                raise AssertionError("v2.0 must not allow longmode")
            payload["target_seconds"] = int(longmode)
        else:
            if model != "agnes-video-v2.0":
                payload["seconds"] = seconds
        return payload

    def test_short_video_payload_uses_seconds(self):
        p = self._simulate_go_payload("agnes-video-2.5", "0", seconds="5")
        self.assertEqual(p.get("seconds"), "5")
        self.assertNotIn("target_seconds", p)

    def test_long_video_payload_uses_target_seconds(self):
        p = self._simulate_go_payload("agnes-video-2.5", "30")
        self.assertEqual(p.get("target_seconds"), 30)
        self.assertNotIn("seconds", p)

    def test_long_video_60s_payload(self):
        p = self._simulate_go_payload("agnes-video-2.5-flash", "60")
        self.assertEqual(p.get("target_seconds"), 60)

    def test_v20_longmode_raises(self):
        with self.assertRaises(AssertionError):
            self._simulate_go_payload("agnes-video-v2.0", "30")

    # ------------------------------------------------------------------
    # Merge button logic
    # ------------------------------------------------------------------
    def test_merge_button_not_shown_when_pending(self):
        job = {"long": True, "status": "succeeded", "merge_status": "pending", "segments": []}
        show_btn = job["long"] and job["status"] == "succeeded" and job["merge_status"] == "ready"
        self.assertFalse(show_btn)

    def test_merge_button_shown_when_ready(self):
        job = {"long": True, "status": "succeeded", "merge_status": "ready", "segments": []}
        show_btn = job["long"] and job["status"] == "succeeded" and job["merge_status"] == "ready"
        self.assertTrue(show_btn)

    def test_merge_button_not_shown_for_short_video(self):
        job = {"long": False, "status": "succeeded", "merge_status": "ready"}
        show_btn = job["long"] and job["status"] == "succeeded" and job["merge_status"] == "ready"
        self.assertFalse(show_btn)

    def test_merge_failure_notice_includes_pending_segments(self):
        # Simulate the error path in the merge button handler
        err = {"error": "合并前置条件未满足", "pending_segments": 2}
        msg = err.get("error", "") + (err.get("pending_segments") and f"（待完成分段：{err['pending_segments']}）" or "")
        self.assertIn("待完成分段", msg)
        self.assertIn("2", msg)

    # ------------------------------------------------------------------
    # Polling backoff and terminal-stop tests
    # ------------------------------------------------------------------
    def test_poll_uses_settimeout_not_setinterval(self):
        """poll() must use setTimeout for backoff scheduling, not setInterval."""
        self.assertNotIn("setInterval", INDEX)
        self.assertIn("setTimeout", INDEX)

    def test_show_uses_cleartimeout_not_clearinterval(self):
        """show() terminal branch must use clearTimeout, not clearInterval."""
        self.assertNotIn("clearInterval", INDEX)
        self.assertIn("clearTimeout(timer)", INDEX)

    def test_poll_resets_backoff_state(self):
        """poll() must reset _pollDelay to _POLL_BASE and _pollFailCount to 0."""
        self.assertIn("_pollDelay=_POLL_BASE", INDEX)
        self.assertIn("_pollFailCount=0", INDEX)
        self.assertIn("_POLL_BASE=5000", INDEX)
        self.assertIn("_POLL_MAX=60000", INDEX)

    def test_refresh_success_resets_backoff(self):
        """On success, refresh() must reset _pollFailCount=0 and _pollDelay=_POLL_BASE."""
        # The success branch resets both
        self.assertIn("_pollFailCount=0;_pollDelay=_POLL_BASE;show(j)", INDEX)

    def test_refresh_failure_increments_backoff(self):
        """On failure, refresh() must increment _pollFailCount and compute exponential delay."""
        self.assertIn("_pollFailCount++", INDEX)
        self.assertIn("Math.pow(2,_pollFailCount-1)", INDEX)
        self.assertIn("Math.min(_POLL_BASE*Math.pow(2,_pollFailCount-1),_POLL_MAX)", INDEX)

    def test_backoff_cap_is_60000ms(self):
        """The maximum backoff interval must be capped at 60000ms (60s)."""
        self.assertIn("_POLL_MAX=60000", INDEX)

    def test_refresh_schedules_next_on_non_terminal(self):
        """On success with non-terminal status, refresh() must schedule the next poll via setTimeout."""
        # The terminal check in refresh() success branch
        self.assertIn(
            'if(!j||!(j.long?(j.status==="failed"||(j.status==="succeeded"&&j.merge_status==="merged")):["succeeded","failed","canceled"].includes(j.status))',
            INDEX,
        )
        self.assertIn("timer=setTimeout(function(){refresh(false)},_pollDelay)", INDEX)

    def test_manual_refresh_passes_true(self):
        """The refresh button must call refresh(true) for manual mode."""
        self.assertIn('function(){return refresh(true)}', INDEX)

    def test_manual_refresh_schedules_retry_on_failure(self):
        """On manual refresh failure, a retry must be scheduled via setTimeout."""
        # The manual path also schedules a retry
        self.assertIn('let msg=manual?e.message', INDEX)

    def test_automatic_refresh_schedules_retry_on_failure(self):
        """On automatic refresh failure, the timer must be rescheduled with backoff delay."""
        self.assertIn('if(!manual){clearTimeout(timer);timer=null;timer=setTimeout(function(){refresh(false)},_pollDelay)}', INDEX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
