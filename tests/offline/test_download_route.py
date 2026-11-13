"""Offline tests for GET /api/download/<basename>.mp4 range and inline behavior.

These tests load server.py in isolation with temp DATA/OUT directories and
mock the concrete handler I/O. No real network, video API, FFmpeg, or
service startup is used.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_server_module():
    tmp = Path(tempfile.mkdtemp(prefix="agnes_download_"))
    old_data = os.environ.pop("AGNES_DATA_DIR", None)
    old_out = os.environ.pop("AGNES_OUTPUT_DIR", None)
    old_token = os.environ.pop("AGNES_API_TOKEN", None)
    old_play = os.environ.pop("AGNES_PLAY_SECRET", None)
    os.environ["AGNES_DATA_DIR"] = str(tmp / "data")
    os.environ["AGNES_OUTPUT_DIR"] = str(tmp / "outputs")
    os.environ["AGNES_API_TOKEN"] = "download-token"
    os.environ["AGNES_PLAY_SECRET"] = "download-play-secret-0123456789abcdef"
    spec = importlib.util.spec_from_file_location("server_download", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in (("AGNES_DATA_DIR", old_data), ("AGNES_OUTPUT_DIR", old_out),
                       ("AGNES_API_TOKEN", old_token), ("AGNES_PLAY_SECRET", old_play)):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return module


SERVER = _load_server_module()
VALID_TOKEN = "download-token"


class _DownloadHandler(SERVER.H):
    def __init__(self, path, token=VALID_TOKEN, range_header=None):
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


def _write_mp4(out, name="test_local.mp4", payload=None):
    path = out / name
    if payload is None:
        payload = b"0123456789abcdef"
    path.write_bytes(payload)
    return path


class _HeadHandler(SERVER.H):
    """HEAD-capable handler mirroring _DownloadHandler."""
    def __init__(self, path, token=None, range_header=None):
        self.path = path
        self.request_version = "HTTP/1.1"
        self.command = "HEAD"
        self.client_address = ("127.0.0.1", 0)
        self.wfile = io.BytesIO()
        self._status_code = 200
        self.sent_headers = {}
        self.headers = {"Content-Length": "0"}
        if token is not None:
            self.headers["Authorization"] = "Bearer %s" % token
        if range_header is not None:
            self.headers["Range"] = range_header
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


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="agnes_download_out_"))
        self._patches = [patch.object(SERVER, "OUT", self.out)]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _get(self, path, range_header=None, write_file=True):
        if write_file:
            _write_mp4(self.out)
        handler = _DownloadHandler(path, range_header=range_header)
        handler.do_GET()
        return handler

    def _head(self, path, bearer=VALID_TOKEN, range_header=None):
        _write_mp4(self.out)
        handler = _HeadHandler(path, token=bearer, range_header=range_header)
        handler.do_HEAD()
        return handler

    def _signed_head_path(self, disposition="attachment"):
        _write_mp4(self.out)
        exp = int(time.time()) + 3600
        sig = SERVER._issue_play_sig("test_local.mp4", exp,
                                     disposition=disposition)
        return ("/api/download/test_local.mp4?purpose=play&method=GET&exp=%d"
                "&disposition=%s&sig=%s" % (exp, disposition, sig))

    def test_full_get_200_inline_with_accept_ranges(self):
        handler = self._get("/api/download/test_local.mp4")
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.sent_headers["Content-Type"], "video/mp4")
        self.assertEqual(handler.sent_headers["Accept-Ranges"], "bytes")
        self.assertEqual(handler.sent_headers["Content-Length"], "16")
        self.assertEqual(
            handler.sent_headers["Content-Disposition"],
            'inline; filename="test_local.mp4"; filename*=UTF-8\'\'test_local.mp4',
        )
        self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef")

    def test_closed_range_206_sends_only_requested_bytes(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=0-15")
        self.assertEqual(handler.status_code, 206)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes 0-15/16")
        self.assertEqual(handler.sent_headers["Content-Length"], "16")
        self.assertEqual(handler.sent_headers["Accept-Ranges"], "bytes")
        self.assertEqual(
            handler.sent_headers["Content-Disposition"],
            'inline; filename="test_local.mp4"; filename*=UTF-8\'\'test_local.mp4',
        )
        self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef")

    def test_open_ended_range_206_sends_until_eof(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=10-")
        self.assertEqual(handler.status_code, 206)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes 10-15/16")
        self.assertEqual(handler.sent_headers["Content-Length"], "6")
        self.assertEqual(handler.wfile.getvalue(), b"abcdef")

    def test_suffix_range_206_sends_last_bytes(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=-16")
        self.assertEqual(handler.status_code, 206)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes 0-15/16")
        self.assertEqual(handler.sent_headers["Content-Length"], "16")
        self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef")

    def test_closed_range_end_beyond_size_clamps_to_last_byte(self):
        # end (100) > total_size (16): RFC 7233 clamps to the last byte, so
        # the request is still satisfiable -> 206 with end = total_size - 1.
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=0-100")
        self.assertEqual(handler.status_code, 206)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes 0-15/16")
        self.assertEqual(handler.sent_headers["Content-Length"], "16")
        self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef")

    def test_head_download_now_implemented(self):
        # do_HEAD is now implemented; replace the old "not implemented" pin.
        self.assertTrue(hasattr(SERVER.H, "do_HEAD"),
                        "do_HEAD was removed; update this test")

    def test_head_bearer_200_no_body_full_headers(self):
        h = self._head("/api/download/test_local.mp4")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.wfile.getvalue(), b"", "HEAD must send no body")
        self.assertEqual(h.sent_headers["Content-Type"], "video/mp4")
        self.assertEqual(h.sent_headers["Accept-Ranges"], "bytes")
        self.assertEqual(h.sent_headers["Content-Length"], "16")
        self.assertEqual(h.sent_headers["Content-Disposition"],
                         'inline; filename="test_local.mp4"; filename*=UTF-8\'\'test_local.mp4')

    def test_head_signed_attachment_200_no_body(self):
        path = self._signed_head_path(disposition="attachment")
        h = _HeadHandler(path)  # no Bearer: authed purely by the signed URL
        h.do_HEAD()
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.wfile.getvalue(), b"")
        self.assertEqual(h.sent_headers["Content-Length"], "16")
        self.assertEqual(h.sent_headers["Content-Disposition"],
                         'attachment; filename="test_local.mp4"; filename*=UTF-8\'\'test_local.mp4')

    def test_head_ignores_range_and_reports_full_size(self):
        # A Range header on HEAD is ignored: Content-Length is the full size,
        # not the ranged length.
        h = self._head("/api/download/test_local.mp4", range_header="bytes=4-7")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.sent_headers["Content-Length"], "16",
                         "HEAD must ignore Range and report the full size")
        self.assertEqual(h.wfile.getvalue(), b"")

    def test_head_missing_file_404_no_body(self):
        h = self._head("/api/download/ghost.mp4")
        self.assertEqual(h.status_code, 404)
        self.assertEqual(h.wfile.getvalue(), b"", "error responses send no video body")

    def test_head_unauthorized_403_no_body(self):
        # No Bearer and no valid signature -> fail closed, no body.
        h = self._head("/api/download/test_local.mp4", bearer=None)
        self.assertEqual(h.status_code, 403)
        self.assertEqual(h.wfile.getvalue(), b"")

    def test_head_expired_signature_410_no_body(self):
        # An expired signed URL still yields the aligned 410, with no body.
        exp = int(time.time()) - 7200  # already in the past
        sig = SERVER._issue_play_sig("test_local.mp4", exp, purpose="play",
                                     method="GET")
        url = ("/api/download/test_local.mp4?purpose=play&method=GET&exp=%d"
               "&disposition=inline&sig=%s" % (exp, sig))
        h = _HeadHandler(url)
        h.do_HEAD()
        self.assertEqual(h.status_code, 410, "expired signature -> 410")
        self.assertEqual(h.wfile.getvalue(), b"", "HEAD 410 must send no body")

    def test_head_illegal_basename_400_no_body(self):
        # An illegal basename is rejected with the aligned 400, no body.
        h = self._head("/api/download/../etc/passwd")
        self.assertEqual(h.status_code, 400)
        self.assertEqual(h.wfile.getvalue(), b"", "HEAD 400 must send no body")

    def test_head_non_download_path_501(self):
        # HEAD on a non-download route keeps the 501 contract.
        h = _HeadHandler("/api/config", token=VALID_TOKEN)
        h.do_HEAD()
        self.assertEqual(h.status_code, 501)

    def test_head_get_200_header_parity(self):
        """A no-Range GET and the matching HEAD set the same five resource
        headers; the only difference is that GET sends the body and HEAD
        does not."""
        _write_mp4(self.out)
        g = self._get("/api/download/test_local.mp4", write_file=False)  # no Range
        h = self._head("/api/download/test_local.mp4")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(h.status_code, 200)
        for key in ("Content-Type", "Accept-Ranges", "Content-Length",
                    "Content-Disposition", "Referrer-Policy"):
            self.assertEqual(h.sent_headers.get(key), g.sent_headers.get(key),
                             "header %s must match between GET and HEAD" % key)
        self.assertEqual(g.wfile.getvalue(), b"0123456789abcdef")  # GET body
        self.assertEqual(h.wfile.getvalue(), b"")                  # HEAD no body

    def test_head_get_404_header_parity(self):
        """On a missing file, GET and HEAD both return 404 with the same
        JSON error headers (Content-Type / Content-Length / Cache-Control);
        only the body differs (GET sends the JSON, HEAD does not)."""
        _write_mp4(self.out)
        g = _DownloadHandler("/api/download/ghost.mp4")
        g.do_GET()
        h = _HeadHandler("/api/download/ghost.mp4", token=VALID_TOKEN)
        h.do_HEAD()
        self.assertEqual(g.status_code, 404)
        self.assertEqual(h.status_code, 404)
        for key in ("Content-Type", "Content-Length", "Cache-Control"):
            self.assertEqual(h.sent_headers.get(key), g.sent_headers.get(key),
                             "header %s must match between GET and HEAD 404" % key)
        self.assertEqual(g.wfile.getvalue(), b'{"error": "\xe6\x96\x87\xe4\xbb\xb6\xe4\xb8\x8d\xe5\xad\x98\xe5\x9c\xa8"}')
        self.assertEqual(h.wfile.getvalue(), b"", "HEAD 404 must send no body")

    def _signed_path_for_method(self, method, disposition="inline"):
        """Build a signed /api/download URL whose signature is bound to
        `method` — mirroring the production issuance format (which always
        signs method=GET)."""
        _write_mp4(self.out)
        exp = int(time.time()) + 3600
        sig = SERVER._issue_play_sig("test_local.mp4", exp,
                                     purpose="play", method=method,
                                     disposition=disposition)
        return ("/api/download/test_local.mp4?purpose=play&method=%s&exp=%d"
                "&disposition=%s&sig=%s" % (method, exp, disposition, sig))

    def test_get_signed_url_authors_head_probe(self):
        """Direction 1: the production GET-signed playlink (method=GET) is
        valid for a HEAD metadata probe — 200 with headers and no body — and
        still for a GET download (regression guard that HEAD support did not
        break the GET path)."""
        url = self._signed_path_for_method("GET")
        # HEAD: reuse the GET signature for probing (no body).
        h = _HeadHandler(url)
        h.do_HEAD()
        self.assertEqual(h.status_code, 200, "GET-signed URL must authorize HEAD")
        self.assertEqual(h.wfile.getvalue(), b"")
        # GET: the same URL still downloads (unchanged production behavior).
        g = _DownloadHandler(url, token=None)
        g.do_GET()
        self.assertEqual(g.status_code, 200, "GET-signed URL must authorize GET")
        self.assertEqual(g.wfile.getvalue(), b"0123456789abcdef")

    def test_head_signed_url_cannot_author_get_or_head(self):
        """Direction 2: a signature bound to method=HEAD (which the
        production issuer never produces) must be rejected by BOTH a GET
        download and a HEAD request, so a HEAD-only signature can never
        execute a GET download."""
        url = self._signed_path_for_method("HEAD")
        # GET with a HEAD-bound signature -> rejected, no video body.
        g = _DownloadHandler(url, token=None)
        g.do_GET()
        self.assertEqual(g.status_code, 403, "HEAD signature must not authorize GET")
        self.assertNotEqual(g.wfile.getvalue(), b"0123456789abcdef", "no video bytes on rejection")
        # HEAD with a HEAD-bound signature -> also rejected (only method=GET
        # is the issuable value, so a HEAD-bound sig verifies for nothing).
        h = _HeadHandler(url)
        h.do_HEAD()
        self.assertEqual(h.status_code, 403, "HEAD signature must not authorize HEAD")
        self.assertEqual(h.wfile.getvalue(), b"")

    def test_unsatisfiable_start_beyond_size_returns_416(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=16-17")
        self.assertEqual(handler.status_code, 416)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes */16")
        self.assertEqual(handler.sent_headers["Content-Length"], "0")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_unsatisfiable_start_greater_than_end_returns_416(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=10-5")
        self.assertEqual(handler.status_code, 416)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes */16")
        self.assertEqual(handler.sent_headers["Content-Length"], "0")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_zero_suffix_range_returns_416(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=-0")
        self.assertEqual(handler.status_code, 416)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes */16")
        self.assertEqual(handler.sent_headers["Content-Length"], "0")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_malformed_range_is_ignored_and_returns_200(self):
        for range_header in ("bytes=abc", "bytes=-abc", "bytes=10-ab", "bytes=", "bytes", "items=1-2"):
            with self.subTest(range_header=range_header):
                handler = self._get("/api/download/test_local.mp4", range_header=range_header)
                self.assertEqual(handler.status_code, 200, range_header)
                self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef", range_header)
                self.assertEqual(handler.sent_headers["Content-Length"], "16", range_header)

    def test_multi_range_is_ignored_and_returns_200(self):
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=0-5,10-15")
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.wfile.getvalue(), b"0123456789abcdef")

    def test_empty_file_full_get_200_with_zero_length(self):
        _write_mp4(self.out, payload=b"")
        handler = self._get("/api/download/test_local.mp4", write_file=False)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.sent_headers["Content-Length"], "0")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_empty_file_unsatisfiable_range_returns_416(self):
        _write_mp4(self.out, payload=b"")
        handler = self._get("/api/download/test_local.mp4", range_header="bytes=0-1", write_file=False)
        self.assertEqual(handler.status_code, 416)
        self.assertEqual(handler.sent_headers["Content-Range"], "bytes */0")
        self.assertEqual(handler.sent_headers["Content-Length"], "0")

    def test_missing_file_returns_404(self):
        handler = _DownloadHandler("/api/download/missing.mp4")
        handler.do_GET()
        self.assertEqual(handler.status_code, 404)
        self.assertEqual(handler.body_json()["error"], "文件不存在")

    def test_illegal_filename_returns_400(self):
        for path in ("/api/download/", "/api/download/file.txt", "/api/download/sub/file.mp4", "/api/download/../server.py"):
            with self.subTest(path=path):
                handler = _DownloadHandler(path)
                handler.do_GET()
                self.assertEqual(handler.status_code, 400, path)
                self.assertEqual(handler.body_json()["error"], "非法文件名", path)

    def test_directory_instead_of_file_returns_404(self):
        self.out.mkdir(exist_ok=True)
        (self.out / "dir.mp4").mkdir()
        handler = _DownloadHandler("/api/download/dir.mp4")
        handler.do_GET()
        self.assertEqual(handler.status_code, 404)

    def test_symlink_outside_out_returns_404(self):
        outside = Path(tempfile.mkdtemp(prefix="agnes_download_outside_"))
        target = _write_mp4(outside, name="outside.mp4")
        link = self.out / "link.mp4"
        link.symlink_to(target)
        handler = _DownloadHandler("/api/download/link.mp4")
        handler.do_GET()
        self.assertEqual(handler.status_code, 404)


class SafeContentDispositionTests(unittest.TestCase):
    """Unit tests for the shared _safe_content_disposition generator.

    Verifies CRLF/NUL header-injection safety, the RFC 5987 filename*
    encoding (UTF-8, not quote-plus), the ASCII fallback, and that the same
    generator is used for both inline and attachment modes.
    """

    def _hdr(self, filename, mode):
        return SERVER._safe_content_disposition(filename, mode)

    def test_ascii_filename(self):
        hdr = self._hdr("video.mp4", "attachment")
        self.assertEqual(
            hdr,
            "attachment; filename=\"video.mp4\"; "
            "filename*=UTF-8''video.mp4",
        )

    def test_cjk_filename_utf8_percent_encoding(self):
        hdr = self._hdr("我的视频 01.mp4", "attachment")
        self.assertTrue(hdr.startswith("attachment; filename="))
        star = hdr.split("filename*=UTF-8''", 1)[1]
        # Decoding filename* yields the exact sanitized (safe) name.
        self.assertEqual(unquote(star), "我的视频 01.mp4")
        # Spaces must be %20, never quote-plus "+".
        self.assertIn("%20", star.upper())
        self.assertNotIn("+", star)

    def test_space_quote_semicolon(self):
        hdr = self._hdr("a b;c'.mp4", "inline")
        self.assertTrue(hdr.startswith("inline;"))
        star = hdr.split("filename*=UTF-8''", 1)[1]
        self.assertEqual(unquote(star), "a b;c'.mp4")
        self.assertNotIn("+", star)

    def test_crlf_control_chars_stripped(self):
        # CRLF / NUL must not survive into the header value.
        for bad in ("evil\r\nx: y.mp4", "evil\x00f.mp4", "evil\x1bf.mp4"):
            with self.subTest(input=repr(bad)):
                hdr = self._hdr(bad, "attachment")
                # The fallback filename (ASCII, inside quotes) is clean.
                fallback = hdr.split('filename="', 1)[1].split('"', 1)[0]
                for ch in fallback:
                    self.assertNotIn(ch, "\r\n\x00\x1b")
                expected = {"evil\r\nx: y.mp4": "evilx: y.mp4",
                            "evil\x00f.mp4": "evilf.mp4",
                            "evil\x1bf.mp4": "evilf.mp4"}[bad]
                self.assertEqual(unquote(hdr.split("filename*=UTF-8''", 1)[1]),
                                 expected)

    def test_path_separators_stripped(self):
        # Only the final path component is ever embedded in the header.
        hdr = self._hdr("../../etc/passwd.mp4", "attachment")
        self.assertEqual(unquote(hdr.split("filename*=UTF-8''", 1)[1]), "passwd.mp4")
        self.assertNotIn("/", hdr.split('filename="', 1)[1].split('"', 1)[0])

    def test_windows_backslash_path_keeps_only_last_segment(self):
        # A Windows-style directory prefix must never reach filename/filename*.
        for mode in ("inline", "attachment"):
            with self.subTest(mode=mode):
                hdr = self._hdr("C:\\fake\\folder\\视频 01.mp4", mode)
                self.assertEqual(unquote(hdr.split("filename*=UTF-8''", 1)[1]),
                                 "视频 01.mp4")
                # No drive-letter prefix or backslash survives.
                self.assertNotIn("C:", hdr)
                self.assertNotIn("\\", hdr.split('filename="', 1)[1].split('"', 1)[0])

    def test_mixed_separators_keep_only_last_segment(self):
        # Mixed POSIX + Windows separators: last segment wins, no sep survives.
        for mode in ("inline", "attachment"):
            with self.subTest(mode=mode):
                hdr = self._hdr("a/b\\c/d 视频.mp4", mode)
                self.assertEqual(unquote(hdr.split("filename*=UTF-8''", 1)[1]),
                                 "d 视频.mp4")
                self.assertNotIn("/", hdr.split('filename="', 1)[1].split('"', 1)[0])
                self.assertNotIn("\\", hdr.split('filename="', 1)[1].split('"', 1)[0])

    def test_c1_control_chars_stripped(self):
        # C1 controls (U+0080–U+009F) must not survive into the header, while
        # ordinary CJK is preserved.
        for ch in ("\u008d", "\u009b", "\u009c", "\u0080"):
            with self.subTest(ctrl=repr(ch)):
                raw = "evil%sfile 视频.mp4" % ch
                hdr = self._hdr(raw, "attachment")
                fallback = hdr.split('filename="', 1)[1].split('"', 1)[0]
                self.assertNotIn(ch, fallback)
                self.assertEqual(unquote(hdr.split("filename*=UTF-8''", 1)[1]),
                                 "evilfile 视频.mp4")

    def test_empty_or_control_only_falls_back_to_default(self):
        for name in ("", "\r\n\x00", "."):
            with self.subTest(input=repr(name)):
                hdr = self._hdr(name, "inline")
                self.assertIn('filename="video.mp4"', hdr)

    def test_inline_and_attachment_share_generator(self):
        # The only difference between the two modes is the leading keyword.
        a = self._hdr("clip.mp4", "attachment")
        i = self._hdr("clip.mp4", "inline")
        self.assertEqual(a.split("; filename=", 1)[1], i.split("; filename=", 1)[1])
        self.assertTrue(a.startswith("attachment;"))
        self.assertTrue(i.startswith("inline;"))


class DownloadPathTraversalTests(unittest.TestCase):
    """Offline regression cases for the /api/download basename boundary.

    The route does NOT percent-decode self.path before basename extraction,
    so every encoded traversal vector below reaches the validator as literal
    characters and must be rejected (400/404), never served from outside OUT.
    These cases pin that property so a future "normalize the URL" change is a
    deliberate, re-tested edit rather than a silent bypass.
    """

    def _out(self):
        out = Path(tempfile.mkdtemp(prefix="agnes_download_out_"))
        patcher = patch.object(SERVER, "OUT", out)
        patcher.start()
        self.addCleanup(patcher.stop)
        return out

    def _status(self, path, out):
        _write_mp4(out, "inside.mp4")
        h = _DownloadHandler(path)
        h.do_GET()
        return h

    def test_encoded_parent_dir_is_rejected(self):
        out = self._out()
        # %2e%2e%2f == "..": not decoded, so it stays a literal, non-existent
        # in-OUT name -> 404, never a real traversal.
        h = self._status("/api/download/%2e%2e%2finside.mp4", out)
        self.assertEqual(h.status_code, 404)

    def test_mixed_literal_dotdot_with_encoded_slash_is_rejected(self):
        out = self._out()
        h = self._status("/api/download/..%2finside.mp4", out)
        self.assertIn(h.status_code, (400, 404))

    def test_encoded_backslash_is_rejected(self):
        out = self._out()
        # %5c == backslash; not a POSIX separator, resolves to a literal name.
        h = self._status("/api/download/%5cetc%5cinside.mp4", out)
        self.assertEqual(h.status_code, 404)

    def test_encoded_nul_is_rejected(self):
        out = self._out()
        h = self._status("/api/download/a%00b.mp4", out)
        self.assertEqual(h.status_code, 404)

    def test_double_encoded_dotdot_is_rejected(self):
        out = self._out()
        h = self._status("/api/download/%252e%252e%2finside.mp4", out)
        self.assertEqual(h.status_code, 404)

    def test_non_mp4_suffix_rejected(self):
        out = self._out()
        h = self._status("/api/download/inside.txt", out)
        self.assertEqual(h.status_code, 400)
        self.assertEqual(h.body_json()["error"], "非法文件名")

    def test_literal_slash_rejected(self):
        out = self._out()
        h = self._status("/api/download/sub/inside.mp4", out)
        self.assertEqual(h.status_code, 400)

    def test_symlink_escaping_out_is_404(self):
        out = self._out()
        outside = Path(tempfile.mkdtemp(prefix="agnes_download_outside_"))
        target = _write_mp4(outside, "secret.mp4")
        link = out / "link.mp4"
        link.symlink_to(target)
        h = self._status("/api/download/link.mp4", out)
        # resolve() + is_relative_to(OUT) blocks escape even for a valid file.
        self.assertEqual(h.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
