"""Offline tests for the playlink feature (signed short-lived playback URLs).

Covers the playlink draft (~/playlink_patch_draft.md) plus the
inline/attachment disposition split:
  - _issue_play_sig / _verify_play_signature round-trip and failure reasons
    (secret_unset, missing, malformed, expired, invalid)
  - disposition bound into the signature (whitelist inline/attachment);
    non-whitelisted values rejected up front (malformed)
  - legacy 4-field signatures (pre-disposition) still verify as inline
  - GET /api/download/<basename> Bearer-or-signed auth split
    (Bearer still works, always inline; signed URL works; no creds -> 403;
     expired -> 410; attachment URL -> Content-Disposition: attachment)
  - GET /api/jobs/<jid>/playlink Bearer-only issuance, job-condition guards,
    ?disposition=attachment issuance, and path-traversal still blocked

No real network, video API, FFmpeg, or service startup is used.  The
module is loaded in isolation with temp DATA/OUT and fixed test-only
secrets; nothing is read from the real data/ directory.
"""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

PLAY_SECRET = "pl-test-secret-0123456789abcdef"
WEB_TOKEN = "pl-web-token"


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_playlink_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    old_play = os.environ.pop("AGNES_PLAY_SECRET", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = WEB_TOKEN
    os.environ["AGNES_PLAY_SECRET"] = PLAY_SECRET
    spec = importlib.util.spec_from_file_location("server_playlink", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if old_data is not None:
        os.environ["AGNES_DATA_DIR"] = old_data
    if old_out is not None:
        os.environ["AGNES_OUTPUT_DIR"] = old_out
    if old_token is not None:
        os.environ["AGNES_API_TOKEN"] = old_token
    if old_play is not None:
        os.environ["AGNES_PLAY_SECRET"] = old_play
    else:
        os.environ.pop("AGNES_PLAY_SECRET", None)
    return module


SERVER = _load_server_module()
assert SERVER.PLAY_SECRET == PLAY_SECRET, "test env must load the fixed play secret"


def _issue(basename, exp, purpose="play", method="GET", disposition="inline"):
    return SERVER._issue_play_sig(basename, exp, purpose=purpose, method=method,
                                  disposition=disposition)


def _issue_legacy(basename, exp, purpose="play", method="GET"):
    """Produce a pre-disposition (4-field) signature, to exercise legacy compat."""
    key = SERVER.PLAY_SECRET.encode("utf-8")
    payload = ("%s|%s|%s|%d" % (purpose, basename, method, exp)).encode("utf-8")
    sig = hmac.new(key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _signed_path(basename, exp, sig, purpose="play", method="GET",
                 disposition=None):
    base = "/api/download/%s?purpose=%s&method=%s&exp=%d&sig=%s" % (
        basename, purpose, method, exp, sig)
    if disposition is not None:
        base += "&disposition=%s" % disposition
    return base


class _PlayHandler(SERVER.H):
    def __init__(self, path, bearer=WEB_TOKEN, range_header=None):
        self.path = path
        self.request_version = "HTTP/1.1"
        self.command = "GET"
        self.client_address = ("127.0.0.1", 0)
        self.wfile = io.BytesIO()
        self._status_code = 200
        self.sent_headers = {}
        headers = {"Content-Length": "0"}
        if bearer is not None:
            headers["Authorization"] = "Bearer %s" % bearer
        if range_header is not None:
            headers["Range"] = range_header
        self.headers = headers
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

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode() or "{}")


class _PlayLinkBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agnes_playlink_out_"))
        self.out = self.root / "outputs"
        self.out.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self._p_out = patch.object(SERVER, "OUT", self.out)
        self._p_data = patch.object(SERVER, "DATA", self.data)
        self._p_out.start()
        self._p_data.start()

    def tearDown(self):
        self._p_out.stop()
        self._p_data.stop()

    def _seed_job(self, jid, long_=True, status="succeeded",
                  merge_status="merged", merged_file=None):
        job = {
            "id": jid,
            "long": long_,
            "status": status,
            "merge_status": merge_status,
            "model": "agnes-video-2.5",
            "prompt": "p",
        }
        if merged_file:
            job["merged_file"] = str(self.out / merged_file)
        (self.data / "jobs.json").write_text(json.dumps({jid: job}))
        return job

    def _write_merged(self, name="merged_lvx.mp4", payload=b"0123456789abcdef"):
        p = self.out / name
        p.write_bytes(payload)
        return p

    def _signed_get(self, basename, exp=None, bearer=None, range_header=None,
                    purpose="play", method="GET", sig_override=None,
                    disposition=None):
        if exp is None:
            exp = int(time.time()) + 3600
        sig = sig_override if sig_override is not None else _issue(basename, exp)
        path = _signed_path(basename, exp, sig, purpose=purpose, method=method,
                            disposition=disposition)
        h = _PlayHandler(path, bearer=bearer, range_header=range_header)
        h.do_GET()
        return h


# ---------------------------------------------------------------------------
# _verify_play_signature unit behavior
# ---------------------------------------------------------------------------
class PlaySignatureUnitTests(_PlayLinkBase):
    def test_round_trip_valid_inline(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp)
        ok, reason, disp = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp), sig)
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(disp, "inline")

    def test_round_trip_valid_attachment(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp, disposition="attachment")
        ok, reason, disp = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp),
                                                         sig, "attachment")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(disp, "attachment")

    def test_expired(self):
        exp = int(time.time()) - 10
        sig = _issue("a.mp4", exp - 10)  # sign for an already-past expiry
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_malformed_exp(self):
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", "not-a-number", "sig")
        self.assertFalse(ok)
        self.assertEqual(reason, "malformed")

    def test_missing_params(self):
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "", "GET", "123", "sig")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing")
        ok, reason, _ = SERVER._verify_play_signature("", "play", "GET", "123", "sig")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing")

    def test_invalid_hmac(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp)
        tampered = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp), tampered)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_tampered_basename(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp)
        ok, reason, _ = SERVER._verify_play_signature("b.mp4", "play", "GET", str(exp), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_tampered_purpose(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp, purpose="play")
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "download", "GET", str(exp), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_tampered_method(self):
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp, method="GET")
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "POST", str(exp), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_expiry_boundary_now_and_now_minus_1(self):
        """Pins the strict < comparison: exp == now is valid (only strictly
        past expires), exp == now-1 is expired. The expiry check runs before
        HMAC, so each case signs with the SAME exp that is presented."""
        now = int(time.time())
        # Signed exactly at the current second: not yet expired.
        sig = _issue("a.mp4", now)
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                      str(now), sig)
        self.assertTrue(ok, "exp == now must not be treated as expired: %s" % reason)
        # Signed one second in the past: expired.
        sig_past = _issue("a.mp4", now - 1)
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                      str(now - 1), sig_past)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_tampered_exp_field_rejected(self):
        """Changing the signed exp value (replaying/lengthening the window)
        must fail HMAC even if the new exp is still in the future."""
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp)
        # Present a different (larger) exp than the one that was signed.
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                      str(exp + 600), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_legacy_sig_with_explicit_inline_param_verifies(self):
        """A pre-disposition (4-field) signature presented with an explicit
        disposition=inline param must still verify (legacy fallback binds
        inline), so in-flight player links that carry ?disposition=inline
        keep working."""
        exp = int(time.time()) + 3600
        sig = _issue_legacy("a.mp4", exp)
        ok, reason, disp = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                         str(exp), sig, "inline")
        self.assertTrue(ok, reason)
        self.assertEqual(disp, "inline")

    def test_legacy_sig_cannot_escalate_to_attachment_unit(self):
        """Unit-level twin of the route-level escalation check: a legacy
        signature presented with disposition=attachment must be rejected,
        not upgraded."""
        exp = int(time.time()) + 3600
        sig = _issue_legacy("a.mp4", exp)
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                      str(exp), sig, "attachment")
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_tampered_disposition(self):
        """An attachment sig must not verify when the URL says inline."""
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp, disposition="attachment")
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp), sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_non_whitelisted_disposition_malformed(self):
        """A disposition outside the whitelist is rejected before HMAC."""
        exp = int(time.time()) + 3600
        sig = _issue("a.mp4", exp)
        ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp),
                                                       sig, "download")
        self.assertFalse(ok)
        self.assertEqual(reason, "malformed")

    def test_secret_unset_fail_closed(self):
        with patch.object(SERVER, "PLAY_SECRET", ""):
            ok, reason, _ = SERVER._verify_play_signature("a.mp4", "play", "GET", "123", "sig")
        self.assertFalse(ok)
        self.assertEqual(reason, "secret_unset")

    def test_issue_requires_secret(self):
        with patch.object(SERVER, "PLAY_SECRET", ""):
            with self.assertRaises(RuntimeError):
                _issue("a.mp4", int(time.time()) + 60)

    def test_issue_rejects_non_whitelisted_disposition(self):
        with self.assertRaises(ValueError):
            _issue("a.mp4", int(time.time()) + 60, disposition="download")

    def test_legacy_4_field_sig_still_verifies_as_inline(self):
        """Signatures issued before the disposition field existed must keep
        working (as inline), so in-flight player links are not invalidated."""
        exp = int(time.time()) + 3600
        sig = _issue_legacy("a.mp4", exp)
        ok, reason, disp = SERVER._verify_play_signature("a.mp4", "play", "GET", str(exp), sig)
        self.assertTrue(ok, reason)
        self.assertEqual(disp, "inline")

    def test_legacy_sig_with_disposition_param_still_inline(self):
        exp = int(time.time()) + 3600
        sig = _issue_legacy("a.mp4", exp)
        # URL carries disposition=attachment but the legacy signature was
        # computed without it -> 5-field check fails, legacy fallback binds
        # inline semantics, but the requested disposition mismatches: the
        # legacy sig verifies only with the empty disposition path.
        ok, reason, disp = SERVER._verify_play_signature("a.mp4", "play", "GET",
                                                         str(exp), sig, "attachment")
        # 5-field payload (inline default replaced by attachment) != legacy
        # 4-field; legacy fallback requires disp param empty/inline, so this
        # must fail.
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")


# ---------------------------------------------------------------------------
# /api/download auth split: Bearer OR signed
# ---------------------------------------------------------------------------
class DownloadAuthSplitTests(_PlayLinkBase):
    def test_bearer_download_still_works_200(self):
        self._write_merged()
        h = _PlayHandler("/api/download/merged_lvx.mp4")
        h.do_GET()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Disposition"], 'inline; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')
        self.assertEqual(h.wfile.getvalue(), b"0123456789abcdef")

    def test_bearer_download_range_206(self):
        self._write_merged()
        h = _PlayHandler("/api/download/merged_lvx.mp4", range_header="bytes=0-0")
        h.do_GET()
        self.assertEqual(h.status_code, 206)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes 0-0/16")
        self.assertEqual(h.wfile.getvalue(), b"0")

    def test_no_auth_no_sig_returns_403(self):
        self._write_merged()
        h = _PlayHandler("/api/download/merged_lvx.mp4", bearer=None)
        h.do_GET()
        self.assertEqual(h.status_code, 403)
        self.assertIn("播放签名无效", h.body_json()["error"])

    def test_invalid_bearer_still_falls_back_to_sig(self):
        self._write_merged()
        h = _PlayHandler("/api/download/merged_lvx.mp4?purpose=x", bearer="wrong-token")
        h.do_GET()
        self.assertEqual(h.status_code, 403)

    def test_valid_sig_get_200_inline(self):
        self._write_merged()
        h = self._signed_get("merged_lvx.mp4", bearer=None)
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Type"], "video/mp4")
        self.assertEqual(h.sent_headers["Content-Disposition"], 'inline; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')
        self.assertEqual(h.wfile.getvalue(), b"0123456789abcdef")

    def test_valid_sig_range_206(self):
        self._write_merged()
        h = self._signed_get("merged_lvx.mp4", bearer=None, range_header="bytes=4-7")
        self.assertEqual(h.status_code, 206)
        self.assertEqual(h.sent_headers["Content-Range"], "bytes 4-7/16")
        self.assertEqual(h.wfile.getvalue(), b"4567")

    def test_attachment_sig_get_200_attachment(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp, disposition="attachment")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None,
                             sig_override=sig, disposition="attachment")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Disposition"],
                         'attachment; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')
        self.assertEqual(h.wfile.getvalue(), b"0123456789abcdef")

    def test_attachment_sig_range_206_attachment(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp, disposition="attachment")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, range_header="bytes=4-7",
                             sig_override=sig, disposition="attachment")
        self.assertEqual(h.status_code, 206)
        self.assertEqual(h.sent_headers["Content-Disposition"],
                         'attachment; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')
        self.assertEqual(h.wfile.getvalue(), b"4567")

    def test_inline_sig_with_attachment_param_rejected(self):
        """A sig bound to inline must not be accepted when the URL asks
        for attachment (header override must be impossible)."""
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp, disposition="inline")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None,
                             sig_override=sig, disposition="attachment")
        self.assertEqual(h.status_code, 403)

    def test_non_whitelisted_disposition_param_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp)
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None,
                             sig_override=sig, disposition="download")
        self.assertEqual(h.status_code, 403)

    def test_sig_expired_410(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp)
        h = self._signed_get("merged_lvx.mp4", exp=exp - 7200, bearer=None, sig_override=sig)
        # requested exp (now-3600) < now -> 410 Gone
        self.assertEqual(h.status_code, 410)

    def test_sig_tampered_basename_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("other.mp4", exp)
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, sig_override=sig)
        self.assertEqual(h.status_code, 403)

    def test_sig_tampered_purpose_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp, purpose="play")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, purpose="download", sig_override=sig)
        self.assertEqual(h.status_code, 403)

    def test_sig_tampered_method_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp, method="GET")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, method="POST", sig_override=sig)
        self.assertEqual(h.status_code, 403)

    def test_sig_invalid_hmac_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp)
        tampered = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, sig_override=tampered)
        self.assertEqual(h.status_code, 403)

    def test_legacy_sig_served_inline(self):
        """Pre-disposition links keep working, always inline."""
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue_legacy("merged_lvx.mp4", exp)
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, sig_override=sig)
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Disposition"],
                         'inline; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')

    def test_legacy_sig_cannot_escalate_to_attachment_at_route(self):
        """A legacy 4-field signature must not upgrade to attachment just
        because the URL asks for it; the disposition param is not part of the
        legacy signature, so it must be rejected (403), never served
        attachment. This is the auth-boundary twin of the unit test."""
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue_legacy("merged_lvx.mp4", exp)
        h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None,
                             sig_override=sig, disposition="attachment")
        self.assertEqual(h.status_code, 403)

    def test_secret_unset_fail_closed_403(self):
        self._write_merged()
        exp = int(time.time()) + 3600
        sig = _issue("merged_lvx.mp4", exp)
        with patch.object(SERVER, "PLAY_SECRET", ""):
            h = self._signed_get("merged_lvx.mp4", exp=exp, bearer=None, sig_override=sig)
        self.assertEqual(h.status_code, 403)

    def test_secret_unset_bearer_still_works(self):
        self._write_merged()
        with patch.object(SERVER, "PLAY_SECRET", ""):
            h = _PlayHandler("/api/download/merged_lvx.mp4")
            h.do_GET()
        self.assertEqual(h.status_code, 200)

    def test_download_route_referrer_policy_header(self):
        self._write_merged()
        h = _PlayHandler("/api/download/merged_lvx.mp4")
        h.do_GET()
        self.assertEqual(h.sent_headers.get("Referrer-Policy"), "no-referrer")

    def test_path_traversal_still_blocked(self):
        for path in ("/api/download/../etc/passwd", "/api/download/sub/file.mp4"):
            with self.subTest(path=path):
                h = _PlayHandler(path, bearer=WEB_TOKEN)
                h.do_GET()
                self.assertEqual(h.status_code, 400, path)
                self.assertEqual(h.body_json()["error"], "非法文件名", path)

    def test_missing_file_404(self):
        h = _PlayHandler("/api/download/ghost.mp4")
        h.do_GET()
        self.assertEqual(h.status_code, 404)


# ---------------------------------------------------------------------------
# GET /api/jobs/<jid>/playlink
# ---------------------------------------------------------------------------
class PlaylinkRouteTests(_PlayLinkBase):
    def _run_playlink(self, path, bearer=WEB_TOKEN):
        h = _PlayHandler(path, bearer=bearer)
        h.do_GET()
        return h

    def test_bearer_only_no_auth_401(self):
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink", bearer=None)
        self.assertEqual(h.status_code, 401)

    def test_unknown_job_404(self):
        h = self._run_playlink("/api/jobs/ghost/playlink")
        self.assertEqual(h.status_code, 404)
        self.assertEqual(h.body_json()["error"], "找不到任务")

    def test_short_video_rejected_403(self):
        self._seed_job("sv1", long_=False)
        h = self._run_playlink("/api/jobs/sv1/playlink")
        self.assertEqual(h.status_code, 403)

    def test_not_succeeded_rejected_403(self):
        self._seed_job("lvx", status="processing", merge_status=None, merged_file=None)
        h = self._run_playlink("/api/jobs/lvx/playlink")
        self.assertEqual(h.status_code, 403)

    def test_not_merged_rejected_403(self):
        self._seed_job("lvx", merge_status="ready", merged_file=None)
        h = self._run_playlink("/api/jobs/lvx/playlink")
        self.assertEqual(h.status_code, 403)

    def test_merged_file_missing_on_disk_404(self):
        self._seed_job("lvx", merged_file="never_written.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink")
        self.assertEqual(h.status_code, 404)

    def test_bad_disposition_400(self):
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink?disposition=download")
        self.assertEqual(h.status_code, 400)
        self.assertEqual(h.body_json()["error"], "非法 disposition 值")

    def test_success_returns_url_exp_expires_in_disposition(self):
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink")
        h.do_GET()
        self.assertEqual(h.status_code, 200)
        body = h.body_json()
        self.assertIn("url", body)
        self.assertIn("exp", body)
        self.assertEqual(body["expires_in"], 600)
        self.assertEqual(body["disposition"], "inline")
        self.assertTrue(body["url"].startswith("/api/download/merged_lvx.mp4?"))
        self.assertIn("&disposition=inline&", body["url"])

        # Round-trip: the issued signature must be accepted by the download
        # auth (inline mode).
        exp = body["exp"]
        self.assertGreater(exp, int(time.time()))
        sig = body["url"].split("sig=")[-1]
        ok, reason, disp = SERVER._verify_play_signature("merged_lvx.mp4", "play", "GET",
                                                        str(exp), sig)
        self.assertTrue(ok, "issued signature rejected: %s" % reason)
        self.assertEqual(disp, "inline")

        # And a full request through the download route with that exact URL
        # works and is served inline.
        h2 = _PlayHandler(body["url"], bearer=None)
        h2.do_GET()
        self.assertEqual(h2.status_code, 200)
        self.assertEqual(h2.sent_headers["Content-Disposition"],
                         'inline; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')
        self.assertEqual(h2.wfile.getvalue(), b"0123456789abcdef")

    def test_attachment_disposition_issuance(self):
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink?disposition=attachment")
        h.do_GET()
        self.assertEqual(h.status_code, 200)
        body = h.body_json()
        self.assertEqual(body["disposition"], "attachment")
        self.assertIn("&disposition=attachment&", body["url"])
        sig = body["url"].split("sig=")[-1]
        ok, reason, disp = SERVER._verify_play_signature("merged_lvx.mp4", "play", "GET",
                                                         str(body["exp"]), sig, "attachment")
        self.assertTrue(ok, reason)
        self.assertEqual(disp, "attachment")

        # Served with attachment disposition via the download route:
        h2 = _PlayHandler(body["url"], bearer=None)
        h2.do_GET()
        self.assertEqual(h2.status_code, 200)
        self.assertEqual(h2.sent_headers["Content-Disposition"],
                         'attachment; filename="merged_lvx.mp4"; filename*=UTF-8\'\'merged_lvx.mp4')

    def test_secret_unset_503(self):
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        with patch.object(SERVER, "PLAY_SECRET", ""):
            h = self._run_playlink("/api/jobs/lvx/playlink")
            h.do_GET()
        self.assertEqual(h.status_code, 503)

    def test_playlink_not_shadowed_by_generic_jobs_route(self):
        """The playlink path must be handled before the generic /api/jobs/<jid>
        lookup, otherwise jid would be parsed as 'playlink' -> 404."""
        self._write_merged()
        self._seed_job("lvx", merged_file="merged_lvx.mp4")
        h = self._run_playlink("/api/jobs/lvx/playlink")
        h.do_GET()
        self.assertEqual(h.status_code, 200, "playlink route was shadowed by generic /api/jobs/ block")


if __name__ == "__main__":
    unittest.main(verbosity=2)
