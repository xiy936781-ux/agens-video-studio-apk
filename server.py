#!/usr/bin/env python3
import http.server, json, os, hmac, hashlib, socket, urllib.request, urllib.error, threading, re, shutil, subprocess, tempfile, time, uuid, base64
from pathlib import Path
import timeline_model
from urllib.parse import urlparse, quote, parse_qs

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("AGNES_DATA_DIR", str(ROOT / "data")))
OUT = Path(os.environ.get("AGNES_OUTPUT_DIR", str(ROOT / "outputs")))
DATA.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
CFG = DATA / "config.json"

# Website access token (separate from upstream Agnes API key).
# Read once at startup; change requires restart.
WEB_API_TOKEN = os.environ.get("AGNES_API_TOKEN", "")

# Independent short-lived playback signing secret.
# Must NOT be the same value as AGNES_API_TOKEN.  When unset, the
# signed-playback path fails closed (403) while Bearer downloads keep working.
PLAY_SECRET = os.environ.get("AGNES_PLAY_SECRET", "")

# ---- Long-video configuration ----
LONG_VIDEO_SECONDS = {30, 60}
SEGMENT_SECONDS = 10  # must be within 4–12 for agnes-video-2.5/2.5-flash
ALLOWED_DOWNLOAD_HOSTS = {"platform-outputs.agnes-ai.space"}

# ---- Segment cache limits ----
SEGMENT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB hard cap per cached file
PROXY_CACHE_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB hard cap per proxied short-video cache

# Per-(jid, seg_index) download lock registry: prevents concurrent duplicate
# downloads of the same segment while allowing parallel work on other jid/idx.
_seg_cache_locks = {}
_seg_cache_locks_guard = threading.Lock()


def _seg_cache_lock(jid: str, seg_index: int) -> threading.Lock:
    key = (jid, seg_index)
    with _seg_cache_locks_guard:
        lock = _seg_cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _seg_cache_locks[key] = lock
        return lock

# Lock for thread-safe jobs.json read/write
_jobs_lock = threading.Lock()

# Task-level merge mutex registry: guards the lock registry with a short-held
# global lock; each jid gets its own threading.Lock() so concurrent requests
# for the SAME task are serialized, while FFmpeg (up to 300s) is never held
# under _jobs_lock.
_merge_task_locks = {}
_merge_task_locks_guard = threading.Lock()


def _merge_task_lock(jid: str) -> threading.Lock:
    """Return a stable per-jid lock object.

    Locks are intentionally cached for the process lifetime. Removing the
    registry entry after one request would create a race where a waiting
    thread holds the old lock while a new request creates a second lock for
    the same jid.
    """
    with _merge_task_locks_guard:
        lock = _merge_task_locks.get(jid)
        if lock is None:
            lock = threading.Lock()
            _merge_task_locks[jid] = lock
        return lock


def _merge_release_task_lock(jid: str) -> None:
    """Compatibility hook.

    Task locks are kept in the registry for the process lifetime, so there is
    nothing to delete here.  Keeping this function makes call sites obvious
    without reintroducing a lock-registry race.
    """
    return


def _fresh_merge_output_path(jid: str) -> Path:
    """Return a UUID-based output path so distinct requests never collide on
    the same timestamped file and never overwrite each other's output."""
    return OUT / ("lv_%s_merged_%s.mp4" % (jid, uuid.uuid4().hex))


def _is_valid_merged_file(value) -> bool:
    """True only when value resolves to a non-empty regular file inside OUT.
    Rejects relative paths that escape OUT, absolute paths outside OUT,
    non-files, symlinks that resolve outside OUT, and zero-byte files."""
    if not isinstance(value, str) or not value:
        return False
    p = Path(value)
    if not p.is_absolute():
        p = OUT / p
    try:
        resolved = p.resolve()
        resolved.relative_to(OUT.resolve())
    except (ValueError, OSError):
        return False
    try:
        return resolved.is_file() and resolved.stat().st_size > 0
    except OSError:
        return False


def _is_visual_setting_sentence(sentence: str) -> bool:
    """Conservative check: a sentence is a visual-setting fragment only when it
    contains visual/setting keywords AND does not contain action/plot markers.

    This prevents sentences like '她在雨夜城市街头追逐凶手' from being
    misclassified as shared visual context.
    """
    visual_markers = (
        "场景", "风格", "服装", "光线", "色调",
        "电影感", "浅景深", "视觉风格",
    )
    action_markers = (
        "追逐", "跑", "走", "跳", "打", "杀", "逃", "攻击", "移动",
        "出现", "消失", "离开", "进入", "走出", "停下", "转头",
        "抬头", "低头", "伸手", "拿起", "放下", "推开", "拉开",
        "打开", "关闭", "喊", "叫", "哭", "笑", "说话", "喊叫",
    )
    has_visual = any(m in sentence for m in visual_markers)
    has_action = any(m in sentence for m in action_markers)
    return has_visual and not has_action


def _split_storyboard_text(full_prompt: str):
    """Split a storyboard prompt into a stable visual context and story beats.

    This is intentionally deterministic and lightweight. It is not a general
    narrative parser and does not claim to understand arbitrary stories.

    Conservative rule: a sentence is treated as a shared-visual fragment only
    when it contains visual/setting keywords AND no obvious action/plot
    markers. Sentences containing action verbs are always kept as plot beats
    even if they also mention scene words such as 雨夜/城市/镜头.
    """
    normalized = full_prompt.replace("\r", "").strip()
    sentence_beats = [
        re.sub(r"^第\d+/\d+镜[：:]?\s*", "", part).strip()
        for part in re.split(r"[。；;！!？?\n]+", normalized)
        if part.strip()
    ]

    shared_fragments = []
    plot_beats = []
    for beat in sentence_beats:
        if _is_visual_setting_sentence(beat):
            shared_fragments.append(beat)
        else:
            plot_beats.append(beat)

    if shared_fragments:
        shared_context = " ".join(shared_fragments)[:160].strip()
    else:
        # No pure visual-setting fragment was found.  Only fall back to a
        # short leading slice when it does NOT contain an action marker,
        # otherwise leave the shared prefix empty to avoid duplicating plot
        # text into the context line.
        action_markers = (
            "追逐", "跑", "走", "跳", "打", "杀", "逃", "攻击", "移动",
            "出现", "消失", "离开", "进入", "走出", "停下", "转头",
            "抬头", "低头", "伸手", "拿起", "放下", "推开", "拉开",
            "打开", "关闭", "喊", "叫", "哭", "笑", "说话", "喊叫",
        )
        candidate = normalized[:60].strip()
        shared_context = candidate if not any(m in candidate for m in action_markers) else ""

    # If all sentences were classified as plot beats (no shared fragment),
    # the shared context is the first clause; ensure it is not identical to
    # the first plot beat to avoid double-printing the same content.
    if not plot_beats:
        plot_beats = [b for b in sentence_beats if b != shared_context]

    return shared_context, plot_beats


def _allocate_storyboard_beats(plot_beats, num_segments):
    """Distribute story beats across segments deterministically.

    Beats are assigned in original order to consecutive slots. When there are
    more beats than segments, beats are grouped by equal-size chunks so the
    global order across segments is preserved. When there are fewer beats
    than segments, the remaining segments get conservative continuation
    placeholders without inventing new plot events.
    """
    if not plot_beats:
        return [
            "信息不足：保持首镜动作与视觉基调，不引入新情节"
            if i == 0
            else "信息不足：延续上一镜头，不引入新情节"
            for i in range(num_segments)
        ]

    grouped = [""] * num_segments
    chunk_size = max(1, len(plot_beats) // num_segments)
    for i in range(num_segments):
        start = i * chunk_size
        if i == num_segments - 1:
            end = len(plot_beats)
        else:
            end = start + chunk_size
        chunk = plot_beats[start:end]
        grouped[i] = "；".join(chunk)

    if len(plot_beats) < num_segments:
        for i in range(len(plot_beats), num_segments):
            if i == num_segments - 1:
                grouped[i] = "信息不足：收束画面，保持已有视觉基调，不引入新情节"
            else:
                grouped[i] = "信息不足：延续上一镜头，不引入新情节"

    return [g.strip() for g in grouped]


def build_segment_prompts(full_prompt: str, num_segments: int) -> list:
    """Build ordered per-segment storyboard prompts for long-video creation.

    This is a deliberately conservative, rule-based helper. It keeps a shared
    appearance/scene/style prefix consistent, distributes plot beats across
    the requested number of segments, and adds deterministic continuation or
    closing beats when the source text is too short to fill every segment.

    It does not call an LLM and does not claim to understand arbitrary
    narrative structure.
    """
    if not isinstance(full_prompt, str):
        full_prompt = str(full_prompt or "")
    full_prompt = full_prompt.strip()
    if not full_prompt:
        raise ValueError("full_prompt must be a non-empty string")
    if num_segments not in (3, 6):
        raise ValueError("num_segments must be 3 or 6")

    shared_context, plot_beats = _split_storyboard_text(full_prompt)
    beats = _allocate_storyboard_beats(plot_beats, num_segments)

    prompts = []
    for i in range(num_segments):
        label = "第%d/%d镜" % (i + 1, num_segments)
        prompts.append("%s\n%s：%s" % (shared_context, label, beats[i]))

    return prompts


# ---- FFmpeg helpers (independent, testable) ----

def ffmpeg_available() -> bool:
    """Return True if ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def validate_segment_files(files: list) -> dict:
    """Validate that all downloaded segments look like real video files.
    Returns {"ok": bool, "errors": [...]}
    """
    errors = []
    for i, f in enumerate(files):
        p = Path(f)
        if not p.exists() or p.stat().st_size == 0:
            errors.append("segment %d: file missing or empty" % i)
            continue
        # Simple header check: MP4 files start with 'ftyp' at offset 4
        try:
            with open(p, "rb") as fh:
                head = fh.read(12)
            if len(head) < 12 or head[4:8] != b"ftyp":
                errors.append("segment %d: not a valid MP4 file" % i)
        except Exception:
            errors.append("segment %d: unreadable file" % i)
    return {"ok": len(errors) == 0, "errors": errors}


def merge_segments_ffmpeg(segment_files: list, output_path: Path) -> dict:
    """Concatenate video segments using FFmpeg.
    Returns {"ok": bool, "error": str|None, "output": str|None}
    """
    if not ffmpeg_available():
        return {"ok": False, "error": "FFmpeg not installed on this host", "output": None}
    # Build concat list file
    list_fd, list_path = tempfile.mkstemp(suffix=".txt", dir=str(OUT))
    try:
        os.close(list_fd)
        lines = ["file '%s'" % f.replace("'", "'\\''") for f in segment_files]
        Path(list_path).write_text("\n".join(lines) + "\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            stderr_tail = (r.stderr or b"").decode(errors="replace")[-500:]
            return {"ok": False, "error": "ffmpeg exited %d: %s" % (r.returncode, stderr_tail), "output": None}
        if not output_path.exists() or output_path.stat().st_size == 0:
            return {"ok": False, "error": "merge output file missing or empty", "output": None}
        return {"ok": True, "error": None, "output": str(output_path)}
    except Exception as e:
        return {"ok": False, "error": "merge exception: %s" % e, "output": None}
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def download_segment(url: str, dest: Path) -> dict:
    """SSRF-safe download of a video segment from an allowed host.
    Returns {"ok": bool, "error": str|None}
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {"ok": False, "error": "only HTTPS downloads allowed"}
    if parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        return {"ok": False, "error": "host not in allowed download list"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgnesVideo/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, length=1024 * 1024)
            tmp.rename(dest)
        return {"ok": True, "error": None}
    except Exception as e:
        return {"ok": False, "error": "download failed: %s" % e}


# ---- Segment cache helpers (new for timeline editor Phase 1) ----

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject all automatic redirects (301/302/303/307/308) to prevent
    SSRF bypass via redirect to disallowed hosts.  Segment cache URLs are
    expected to be stable CDN/object-storage URLs with no redirect dependency.
    """
    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "redirect not allowed", headers, fp)
    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def download_segment_limited(url: str, dest: Path,
                            max_bytes: int = SEGMENT_MAX_BYTES) -> dict:
    """Download a segment with a hard byte cap enforced *during* the copy.

    If the stream exceeds ``max_bytes`` the download is aborted mid-copy and
    the .part file is cleaned up.  Returns {"ok": bool, "error": str|None}.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {"ok": False, "error": "only HTTPS downloads allowed"}
    if parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        return {"ok": False, "error": "host not in allowed download list"}
    tmp = dest.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgnesVideo/1.0"})
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(req, timeout=120) as resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            try:
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            tmp.unlink(missing_ok=True)
                            return {"ok": False, "error": "download exceeds max size"}
                        out.write(chunk)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        tmp.rename(dest)
        return {"ok": True, "error": None}
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return {"ok": False, "error": "download failed: %s" % e}


def probe_video_duration(path: Path):
    """Return actual video duration in seconds via local ffprobe, or None.

    Uses ``ffprobe -v quiet -show_entries format=duration -of default=noprint_wrappers=1``.
    No network calls.  Returns None when ffprobe is missing or probe fails.
    """
    if not path.is_file() or path.is_symlink() or shutil.which("ffprobe") is None:
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        value = float(r.stdout.strip())
        return value if value >= 0 else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def parse_range_header(value: str, total_size: int):
    """Parse a single-range HTTP Range header.

    Returns:
        ("full", None, None) if no Range header was supplied.
        ("valid", start, end) inclusive 0-based byte offsets for legal Range.
        ("invalid", None, None) for malformed or unsatisfiable Range.
    """
    if not value:
        return ("full", None, None)
    if not value.startswith("bytes="):
        return ("invalid", None, None)
    spec = value[6:]
    if "," in spec:
        return ("invalid", None, None)
    if "-" not in spec:
        return ("invalid", None, None)
    left, right = spec.split("-", 1)
    try:
        if left == "" and right == "":
            return ("invalid", None, None)
        if left == "":
            suffix = int(right)
            if suffix <= 0:
                return ("invalid", None, None)
            start = max(0, total_size - suffix)
            return ("valid", start, total_size - 1)
        start = int(left)
        if start >= total_size:
            return ("invalid", None, None)
        if right == "":
            return ("valid", start, total_size - 1)
        end = int(right)
    except ValueError:
        return ("invalid", None, None)
    if end >= total_size:
        end = total_size - 1
    if start > end:
        return ("invalid", None, None)
    return ("valid", start, end)


# ---- End FFmpeg helpers ----


def _required_token():
    """Return the configured web access token or raise if unset."""
    return WEB_API_TOKEN


def verify_web_token(provided: str) -> bool:
    """Constant-time compare the provided Bearer token against AGNES_API_TOKEN.

    If AGNES_API_TOKEN is empty, all API requests are rejected.
    """
    expected = _required_token()
    if not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def get_bearer_token(headers) -> str:
    """Extract the Bearer token from HTTP headers."""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def cfg():
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {"token": ""}


def savecfg(c):
    CFG.write_text(json.dumps(c, ensure_ascii=False, indent=2))
    os.chmod(CFG, 0o600)


# Exception categories raised/caught for upstream API calls.
# Each carries a sanitized, human-readable message safe for frontend display.
UPSTREAM_ERROR_CATEGORIES = (
    "http_error",     # HTTP non-2xx; message includes the status code only
    "timeout",        # request exceeded the deadline
    "network",        # connection/DNS-level failure
    "business",       # upstream returned 2xx but payload rejected our request
)


def _sanitize_call_error(e: Exception, timeout: int) -> str:
    """Return a short, sanitized description of an upstream call failure.

    Never includes the API key, bearer token, Authorization header value, or
    URL query parameters.  Only stable, low-sensitivity identifiers such as
    the HTTP status code are surfaced.
    """
    if isinstance(e, urllib.error.HTTPError):
        return "http_error: HTTP %s" % e.code
    if isinstance(e, urllib.error.URLError) and isinstance(getattr(e, "reason", None), socket.timeout):
        return "timeout: request exceeded %ss deadline" % timeout
    if isinstance(e, socket.timeout):
        return "timeout: request exceeded %ss deadline" % timeout
    if isinstance(e, urllib.error.URLError):
        return "network: upstream connection failed"
    if isinstance(e, RuntimeError):
        # Propagate our own structured RuntimeError messages unchanged.
        return str(e)
    # Last-resort fallback: do not echo the raw exception (could embed URLs
    # with query strings); use a generic network label.
    return "network: upstream request failed"


def call(method, url, token, body=None, timeout=90):
    """Call the upstream Agnes API. Raises RuntimeError on any failure.

    The RuntimeError message is a sanitized, category-prefixed string chosen
    from UPSTREAM_ERROR_CATEGORIES so callers (and the frontend) can distinguish
    HTTP errors, timeouts, and network failures without leaking credentials or
    URL query parameters.
    """
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            data = json.loads(b.decode()) if b else {}
            # 2xx but the upstream business payload signals a rejection
            # (e.g. 402/quota surfaced inside a 200, or a body-level error
            # flag).  We only treat an explicit, non-empty "error" field as a
            # business failure; never echo its text into the exception.
            if isinstance(data, dict) and data.get("error") and not data.get("video_id"):
                raise RuntimeError("business: upstream rejected request (no details)")
            return r.status, data
    except urllib.error.HTTPError as e:
        raise RuntimeError(_sanitize_call_error(e, timeout))
    except RuntimeError:
        # Our own business failure above: re-raise as-is.
        raise
    except socket.timeout:
        raise RuntimeError("timeout: request exceeded %ss deadline" % timeout)
    except Exception as e:
        raise RuntimeError(_sanitize_call_error(e, timeout))


def _mp4_range_plan(range_header, total_size):
    """Shared single-Range plan for local MP4 serving.

    Returns ("full"|"partial"|"unsatisfiable", start, end). Malformed or
    unsupported Range syntax is ignored as a full 200; syntactically valid
    but unsatisfiable ranges return 416. This is local to /api/download and
    /api/jobs/<jid>/proxy, and does not alter parse_range_header.
    """
    if not range_header:
        return ("full", None, None)
    parts = range_header.split(",", 1)
    if len(parts) != 1 or not parts[0].startswith("bytes="):
        return ("full", None, None)
    spec = parts[0][6:]
    if "-" not in spec or spec.count("-") != 1:
        return ("full", None, None)
    left, right = spec.split("-", 1)
    if left == "" and right == "":
        return ("full", None, None)
    if left == "":
        if not right.isdigit():
            return ("full", None, None)
        suffix = int(right)
        if suffix == 0:
            return ("unsatisfiable", None, None)
        start = max(0, total_size - suffix)
        return ("partial", start, total_size - 1)
    if not left.isdigit():
        return ("full", None, None)
    start = int(left)
    if start >= total_size:
        return ("unsatisfiable", None, None)
    if right == "":
        return ("partial", start, total_size - 1)
    if not right.isdigit():
        return ("full", None, None)
    end = int(right)
    if end >= total_size:
        end = total_size - 1
    if start > end:
        return ("unsatisfiable", None, None)
    return ("partial", start, end)


# ---------------------------------------------------------------------------
# Short-lived signed playback URL support (GET /api/download/ only)
# ---------------------------------------------------------------------------

def _play_secret() -> str:
    """Return the independent playback signing secret if configured.

    Intentionally separate from AGNES_API_TOKEN so a long-lived web token
    can never be reused as a signed-URL credential.
    """
    return PLAY_SECRET


# Whitelist of Content-Disposition modes bindable into a signed URL.
# The client cannot override response headers: the disposition value is
# part of the HMAC payload, and only these two strings are ever accepted.
PLAY_DISPOSITIONS = ("inline", "attachment")


def _issue_play_sig(basename: str, exp: int, purpose: str = "play", method: str = "GET",
                    disposition: str = "inline") -> str:
    """HMAC-SHA256 signed playback capability (base64url, no padding).

    Payload (strict, single-line, canonical, 5 fields):
        purpose|basename|method|exp|disposition

    Binds basename, purpose, method, disposition and a short expiry; no
    long-lived Bearer token is ever embedded in the URL.
    """
    secret = _play_secret()
    if not secret:
        raise RuntimeError("AGNES_PLAY_SECRET is not configured")
    if disposition not in PLAY_DISPOSITIONS:
        raise ValueError("disposition must be one of %s" % (PLAY_DISPOSITIONS,))
    payload = "|".join([purpose, basename, method, str(exp), disposition]).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _verify_play_signature(basename: str, purpose: str, method: str, exp_str: str,
                           sig: str, disposition: str = "") -> tuple:
    """Validate a short-lived playback signature.

    Returns (True, None, disposition_used) on success, or
    (False, reason, "inline") on failure where reason is one of:
      "secret_unset" - AGNES_PLAY_SECRET not configured (fail closed)
      "missing"      - any required parameter absent
      "malformed"    - exp is not an integer, or disposition is not
                       whitelisted (inline/attachment)
      "expired"      - exp < now
      "invalid"      - HMAC mismatch (covers all tampered fields)

    Backwards compatibility: pre-disposition ("legacy") 4-field signatures
    (purpose|basename|method|exp) still verify and are treated as
    disposition="inline", so in-flight player links keep working.
    """
    secret = _play_secret()
    if not secret:
        return (False, "secret_unset", "inline")
    disp = disposition if disposition else "inline"
    if disp not in PLAY_DISPOSITIONS:
        # Unknown disposition is rejected up front; the issuer never signs
        # non-whitelisted values, so this also blocks header override.
        return (False, "malformed", "inline")
    if not basename or not purpose or not method or not exp_str or not sig:
        return (False, "missing", "inline")
    try:
        exp_int = int(exp_str)
    except (TypeError, ValueError):
        return (False, "malformed", "inline")
    if exp_int < int(time.time()):
        return (False, "expired", "inline")
    key = secret.encode("utf-8")
    payload5 = ("%s|%s|%s|%d|%s" % (purpose, basename, method, exp_int, disp)).encode("utf-8")
    expected5 = base64.urlsafe_b64encode(
        hmac.new(key, payload5, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    if hmac.compare_digest(expected5, sig):
        return (True, None, disp)
    # Legacy 4-field signature (issued before disposition was added):
    # verifies only with the inline semantics it was originally bound to.
    payload4 = ("%s|%s|%s|%d" % (purpose, basename, method, exp_int)).encode("utf-8")
    expected4 = base64.urlsafe_b64encode(
        hmac.new(key, payload4, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    # Legacy signatures are valid only for inline requests. Reject an
    # explicit attachment request: that disposition was never signed.
    if disp == "inline" and hmac.compare_digest(expected4, sig):
        return (True, None, "inline")
    return (False, "invalid", "inline")


# Characters that must never reach a response header value.  This is the
# full Unicode "Cc" control block used in HTTP header hardening:
#   U+0000–U+001F  (C0, incl. \r \n \0  → CRLF/NUL response-splitting)
#   U+007F–U+009F  (DEL + C1, incl. the 8-bit control/escape range)
# Ordinary printable Unicode (incl. CJK and legal punctuation) is preserved.
_HEADER_FORBIDDEN = set(
    chr(c) for c in list(range(0x00, 0x20)) + list(range(0x7f, 0xa0))
)

# Path separators on both POSIX and Windows.  A backslash must be treated
# exactly like "/" so a directory prefix (C:\fake\folder\...) can never leak
# into the header.
_PATH_SEPARATORS = ("/", "\\")


def _sanitize_filename_component(name: str) -> str:
    """Reduce *name* to a single safe path component for a header value.

    - Split on every path separator ("/" and "\\") and keep only the final
      segment, so no directory prefix (POSIX, Windows, or mixed) survives.
    - Remove any remaining path-separator characters (defence-in-depth).
    - Drop a leading "." so relative path tricks (., .., .hidden) are removed.
    - Remove C0/C1 control chars (U+0000–U+001F, U+007F–U+009F), the CRLF/NUL
      header-injection vectors.
    - Collapse runs of whitespace to a single space and trim.
    - Fall back to a fixed safe default when nothing usable remains.
    """
    # Last segment across every separator; backslashes can't appear in a
    # POSIX basename, but we normalize them so Windows-style names behave.
    for sep in _PATH_SEPARATORS:
        name = name.replace(sep, "/")
    base = name.rsplit("/", 1)[-1]
    base = base.lstrip(".")
    cleaned = "".join(ch for ch in base if ch not in _HEADER_FORBIDDEN)
    # Belt-and-braces: ensure no separator char survived into the segment.
    cleaned = cleaned.replace("/", "").replace("\\", "")
    # Collapse internal whitespace runs to a single space and trim.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "video.mp4"
    return cleaned


def _safe_content_disposition(filename: str, mode: str) -> str:
    """Build a Content-Disposition header value safe for header injection.

    Produces the RFC 6266 two-part form::

        <mode>; filename="<ascii-fallback>"; filename*=UTF-8''<pct-encoded>

    where:
      * <ascii-fallback> is the sanitized name with any non-ASCII byte
        dropped, giving a usable fallback for clients that ignore
        filename* (and guaranteed control-char free);
      * <pct-encoded> is the percent-encoding of the sanitized name's UTF-8
        bytes (RFC 5987), NOT quote-plus (spaces stay %20, never +).

    The same generator serves both "inline" and "attachment" so the two
    cannot diverge.  Only the sanitized basename is ever embedded; the raw
    user-supplied filename never reaches the header unprocessed.
    """
    if mode not in PLAY_DISPOSITIONS:
        mode = "inline"
    safe = _sanitize_filename_component(filename)

    # ASCII fallback: keep only safe printable ASCII (letters/digits and a
    # small punctuation set), dropping non-ASCII (incl. CJK) plus the
    # double-quote and backslash so the fallback is always header-safe.
    ascii_fallback = "".join(
        ch for ch in safe
        if 0x20 < ord(ch) < 0x7f and ch not in '"\\'
    ).strip()
    if not ascii_fallback:
        ascii_fallback = "video.mp4"

    # RFC 5987 / 3986 percent-encoding of UTF-8 bytes; unreserved chars kept
    # as-is, everything else upper-hex percent-encoded (space -> %20).
    unreserved = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
        "-._~"
    )
    enc_parts = []
    for byte in safe.encode("utf-8"):
        ch = chr(byte)
        if ch in unreserved:
            enc_parts.append(ch)
        else:
            enc_parts.append("%%%02X" % byte)
    pct = "".join(enc_parts)

    return '%s; filename="%s"; filename*=UTF-8\'\'%s' % (mode, ascii_fallback, pct)


def jobs_load():
    try:
        return json.loads((DATA / "jobs.json").read_text())
    except Exception:
        return {}


def jobs_save(j):
    """Atomically persist jobs dict. Uses tmp+rename to avoid corruption."""
    tmp = DATA / "jobs.json.tmp"
    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.rename(DATA / "jobs.json")


def jobs_save_locked(j):
    """Thread-safe atomic jobs write."""
    with _jobs_lock:
        jobs_save(j)


def _sanitize_progress_int(value, default=0):
    """Return a safe progress value for persistence and frontend display.

    - ``None`` (progress key absent) -> ``default`` (0).
    - ``bool`` -> 0 or 100.
    - finite numeric (int/float) -> clamped to 0..100.
    - ``float('nan')`` / string ``"NaN"`` -> 0; ``float('inf')`` / ``"inf"``
      -> 100; ``-inf`` -> 0.
    - numeric strings (e.g. ``"50"``) -> 50 (clamped).
    - any other value that cannot be interpreted as a number (e.g. the
      literal string ``"unknown"``) -> returned unchanged, preserving the
      existing ``test_progress_non_numeric_ignored`` contract.  The frontend
      display separately guards against non-numeric progress so it never
      renders as ``NaN``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return 100 if value else 0
    if isinstance(value, int):
        # Arbitrary-precision int: clamp directly without float conversion.
        return max(0, min(100, value))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number != number:  # NaN
        number = 0.0
    elif number == float("inf"):
        number = 100.0
    elif number == float("-inf"):
        number = 0.0
    return max(0, min(100, int(number)))


def _proxy_cache_path(jid: str) -> Path:
    """Return a stable per-job proxy cache path under OUT/proxy."""
    safe_jid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jid))
    return OUT / "proxy" / ("%s.mp4" % safe_jid)


def normalize(j, live):
    merged = {**j, **live}
    s = str(merged.get("status", "unknown")).lower()
    mapping = {
        "queued": "queued",
        "pending": "queued",
        "starting": "queued",
        "in_progress": "processing",
        "processing": "processing",
        "running": "processing",
        "completed": "succeeded",
        "succeeded": "succeeded",
        "success": "succeeded",
        "failed": "failed",
        "error": "failed",
        "cancelled": "canceled",
        "canceled": "canceled",
    }
    merged["status"] = mapping.get(s, s)
    merged["progress"] = _sanitize_progress_int(merged.get("progress"), 0)
    return merged


def _job_record(jid, model, prompt, image, video_id, upstream_resp, model_params=None):
    """Build a minimal job record to persist. Do NOT store the full upstream response."""
    rec = {
        "id": jid,
        "video_id": video_id,
        "model": model,
        "prompt": prompt,
        "status": "queued",
        "progress": 0,
        "input_type": "image+text" if image else "text",
        "created_at": int(time.time()),
    }
    if model_params:
        for k, v in model_params.items():
            if v is not None:
                rec[k] = v
    return rec


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def sendj(self, o, status=200):
        b = json.dumps(o, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # HEAD responses must not carry a body (RFC 7231): keep the status
        # and headers identical to GET, but suppress the JSON payload.
        if self.command != "HEAD":
            self.wfile.write(b)

    def _auth_ok(self) -> bool:
        """Check the web access token. Return True if valid."""
        provided = get_bearer_token(self.headers)
        return verify_web_token(provided)

    def _require_auth(self) -> bool:
        """Reject with 401 if the web token is missing or invalid. Return True to proceed."""
        if not self._auth_ok():
            self.sendj({"error": "未授权或令牌无效。请重新输入网站访问令牌。"}, 401)
            return False
        return True

    def _require_download_auth(self) -> bool:
        """Allow Bearer OR short-lived signed URL for /api/download only.

        Preferred path: the existing Bearer token (unchanged behavior,
        always served inline).  Fallback: a short-lived signed playback
        URL, whose disposition (inline/attachment) is bound into the
        signature and cannot be overridden by the client.  Fails closed
        (403) when AGNES_PLAY_SECRET is not configured and no valid
        Bearer is present, so an unconfigured environment never opens
        signed downloads.  All other /api/* routes remain Bearer-only.
        """
        self._verified_disposition = "inline"
        if self._auth_ok():
            return True

        q = parse_qs(urlparse(self.path).query)
        purpose = (q.get("purpose") or [""])[0]
        method = (q.get("method") or [""])[0]
        exp_str = (q.get("exp") or [""])[0]
        sig = (q.get("sig") or [""])[0]
        disposition = (q.get("disposition") or [""])[0]
        basename = self.path[len("/api/download/"):].split("?", 1)[0]

        # Method binding (production contract): the issuer
        # (_handle_playlink) only signs method="GET".  The verifier must
        # enforce that the signed `method` param is exactly "GET":
        #   - a GET request consumes the GET-signed URL;
        #   - a HEAD request may reuse the GET-signed URL for metadata
        #     probing (headers only, no body);
        #   - no other method value is accepted, so a hypothetical
        #     HEAD-only signature can never authorize a GET download.
        if method != "GET":
            self.sendj({"error": "播放签名无效（method）"}, 403)
            return False

        ok, reason, disp_used = _verify_play_signature(
            basename, purpose, method, exp_str, sig, disposition)
        if ok:
            self._verified_disposition = disp_used
            return True

        status_map = {
            "secret_unset": 403,  # fail closed when secret not configured
            "missing": 403,
            "malformed": 403,
            "expired": 410,
            "invalid": 403,
        }
        code = status_map.get(reason, 403)
        self.sendj({"error": "播放签名无效（%s）" % reason}, code)
        return False

    def _handle_playlink(self, jid: str, disposition: str = "inline") -> None:
        """GET /api/jobs/<jid>/playlink[?disposition=attachment] — Bearer only.

        Issues a short-lived signed URL for the merged long-video MP4 so a
        native <video> element can play without carrying the Authorization
        header, and so the download anchor can fetch a reliable attachment
        download.  The disposition is bound into the signature (whitelist:
        inline/attachment); the client cannot override response headers.
        Only available when AGNES_PLAY_SECRET is configured (fail closed
        -> 503 otherwise).
        """
        if disposition not in PLAY_DISPOSITIONS:
            return self.sendj({"error": "非法 disposition 值"}, 400)
        if not _play_secret():
            return self.sendj({"error": "播放签名未配置，无法签发"}, 503)

        jobs = jobs_load()
        j = jobs.get(jid)
        if not j:
            return self.sendj({"error": "找不到任务"}, 404)
        if not j.get("long"):
            return self.sendj({"error": "该任务不是长视频任务"}, 403)
        if j.get("status") != "succeeded":
            return self.sendj({"error": "任务尚未完成"}, 403)
        if j.get("merge_status") != "merged":
            return self.sendj({"error": "长视频尚未合并完成"}, 403)

        merged_file = j.get("merged_file")
        if not merged_file:
            return self.sendj({"error": "缺少合并文件"}, 403)

        basename = str(merged_file).split("/")[-1]
        if not basename or not basename.endswith(".mp4") or "/" in basename or ".." in basename:
            return self.sendj({"error": "非法文件名"}, 400)

        # Re-run the same path safety checks as /api/download.
        safe_path = OUT / basename
        try:
            resolved = safe_path.resolve()
            out_resolved = OUT.resolve()
            if hasattr(resolved, "is_relative_to"):
                inside = resolved.is_relative_to(out_resolved)
            else:
                inside = str(resolved).startswith(str(out_resolved))
            if not inside or not resolved.is_file():
                return self.sendj({"error": "文件不存在"}, 404)
        except Exception:
            return self.sendj({"error": "文件不存在"}, 404)

        exp = int(time.time()) + 600  # 600s TTL
        sig = _issue_play_sig(basename, exp, purpose="play", method="GET",
                              disposition=disposition)
        url = ("/api/download/%s?purpose=play&method=GET&exp=%d&disposition=%s&sig=%s"
               % (quote(basename), exp, disposition, sig))
        return self.sendj({"url": url, "exp": exp, "expires_in": 600,
                           "disposition": disposition})

    def body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > 7 * 1024 * 1024:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            b = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        # /api/download supports Bearer OR short-lived signed URL;
        # all other /api/* routes remain Bearer-only.
        if p.startswith("/api/download/"):
            if not self._require_download_auth():
                return
        else:
            if p.startswith("/api/") and not self._require_auth():
                return
        if p == "/api/config":
            c = cfg()
            return self.sendj({"token_set": bool(c.get("token"))})
        if p == "/api/jobs/list":
            return self.sendj(list(jobs_load().values())[::-1])
        # ---- Segment cache: GET /api/jobs/<jid>/segments/<seg_index> ----
        seg_match = re.match(r"^/api/jobs/([^/]+)/segments/(\d{1,2})$", p)
        if seg_match:
            seg_jid = seg_match.group(1)
            seg_index = int(seg_match.group(2))

            jobs = jobs_load()
            j = jobs.get(seg_jid)
            if not j:
                return self.sendj({"error": "找不到任务"}, 404)
            if not j.get("long"):
                return self.sendj({"error": "仅长视频任务支持片段缓存"}, 400)

            segs = j.get("segments", [])
            if seg_index >= len(segs):
                return self.sendj({"error": "片段索引超出范围"}, 400)
            seg = segs[seg_index]
            if seg.get("status") != "succeeded":
                return self.sendj({"error": "片段尚未完成，无法缓存"}, 409)

            seg_url = seg.get("url") or seg.get("video_url")
            if not seg_url:
                return self.sendj({"error": "片段缺少下载 URL"}, 404)

            # Build safe cache path: OUT/segments/<jid>/seg_<idx:02d>.mp4
            cache_dir = OUT / "segments" / seg_jid
            cache_path = cache_dir / ("seg_%02d.mp4" % seg_index)
            # Security: resolve and verify inside OUT/segments/<jid>.
            try:
                resolved_cache = cache_path.resolve()
                out_resolved = OUT.resolve()
                cache_dir_resolved = (OUT / "segments").resolve()
                cache_ok = (
                    resolved_cache.is_relative_to(cache_dir_resolved)
                    and resolved_cache.name == cache_path.name
                    and not resolved_cache.is_symlink()
                )
                if not cache_ok:
                    return self.sendj({"error": "非法缓存路径"}, 400)
            except Exception:
                return self.sendj({"error": "非法缓存路径"}, 400)

            # Acquire per-(jid, seg_index) lock to prevent concurrent duplicate downloads
            lock = _seg_cache_lock(seg_jid, seg_index)
            with lock:
                # Double-check: cache may have been filled by a concurrent request
                cache_hit = (
                    cache_path.is_file()
                    and not cache_path.is_symlink()
                    and cache_path.stat().st_size > 0
                )
                if not cache_hit:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    dl_result = download_segment_limited(str(seg_url), cache_path)
                    if not dl_result["ok"]:
                        part = cache_path.with_suffix(".part")
                        if part.exists():
                            part.unlink(missing_ok=True)
                        if dl_result.get("error") == "download exceeds max size":
                            return self.sendj(
                                {"error": "片段文件超过缓存上限"},
                                400,
                            )
                        return self.sendj(
                            {"error": "片段下载失败"},
                            502,
                        )

            # Read file for streaming
            try:
                if not cache_path.is_file() or cache_path.is_symlink():
                    return self.sendj({"error": "非法缓存路径"}, 400)
                fsize = cache_path.stat().st_size
            except OSError:
                return self.sendj({"error": "缓存文件不可读"}, 500)
            if fsize == 0:
                return self.sendj({"error": "缓存文件为空"}, 500)

            # Probe actual video duration (local ffprobe only; no network)
            actual_duration = probe_video_duration(cache_path)

            # Parse Range header for partial content support
            range_header = self.headers.get("Range", "")
            range_kind, start, end = parse_range_header(range_header, fsize)

            if range_kind == "invalid":
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % fsize)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return

            if range_kind == "valid":
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
                self.send_header("Content-Length", str(end - start + 1))
                if actual_duration is not None:
                    self.send_header("X-Source-Duration", str(actual_duration))
                else:
                    self.send_header("X-Source-Duration-Available", "false")
                self.end_headers()
                try:
                    with open(cache_path, "rb") as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except OSError:
                    pass
            else:
                # Full file: 200
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(fsize))
                if actual_duration is not None:
                    self.send_header("X-Source-Duration", str(actual_duration))
                else:
                    self.send_header("X-Source-Duration-Available", "false")
                self.end_headers()
                try:
                    with open(cache_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except OSError:
                    pass
            return

        # ---- Short-video proxy: GET /api/jobs/<jid>/proxy ----
        # Must be matched BEFORE the generic /api/jobs/<jid> block below,
        # otherwise jid would be mis-parsed as the literal string "proxy".
        # Serves the short-video's upstream CDN file via a local, whitelisted,
        # byte-capped download cache (OUT/proxy/<jid>.mp4) so mobile browsers
        # can play/seek inline without reaching the external CDN directly.
        proxy_match = re.match(r"^/api/jobs/([^/]+)/proxy$", p)
        if proxy_match:
            proxy_jid = proxy_match.group(1)
            jobs = jobs_load()
            j = jobs.get(proxy_jid)
            if not j:
                return self.sendj({"error": "找不到任务"}, 404)
            src_url = j.get("video_url") or j.get("url") or j.get("output_url")
            if not src_url:
                return self.sendj({"error": "任务尚无可播放视频 URL"}, 404)
            if j.get("long"):
                return self.sendj({"error": "长视频请使用合并输出或片段缓存"}, 400)
            parsed = urlparse(src_url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                return self.sendj({"error": "视频源不在白名单内"}, 400)
            proxy_cache = _proxy_cache_path(proxy_jid)
            lock = _seg_cache_lock(proxy_jid, -1)
            with lock:
                cache_hit = (
                    proxy_cache.is_file()
                    and not proxy_cache.is_symlink()
                    and proxy_cache.stat().st_size > 0
                )
                if not cache_hit:
                    proxy_cache.parent.mkdir(parents=True, exist_ok=True)
                    dl = download_segment_limited(str(src_url), proxy_cache, max_bytes=PROXY_CACHE_MAX_BYTES)
                    if not dl["ok"]:
                        part = proxy_cache.with_suffix(".part")
                        if part.exists():
                            part.unlink(missing_ok=True)
                        if dl.get("error") == "download exceeds max size":
                            return self.sendj({"error": "视频文件超过缓存上限"}, 400)
                        return self.sendj({"error": "视频下载失败，请稍后重试"}, 502)
            try:
                resolved = proxy_cache.resolve()
                out_resolved = OUT.resolve()
                if not resolved.is_relative_to(out_resolved) or not resolved.is_file():
                    return self.sendj({"error": "缓存文件不存在"}, 404)
                fsize = resolved.stat().st_size
                action, start, end = _mp4_range_plan(self.headers.get("Range", ""), fsize)
                if action == "unsatisfiable":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % fsize)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                disposition = 'inline; filename="%s.mp4"' % re.sub(r"[^A-Za-z0-9_.-]", "_", proxy_jid)
                if action == "partial":
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Disposition", disposition)
                    self.end_headers()
                    with open(resolved, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(fsize))
                    self.send_header("Content-Disposition", disposition)
                    self.end_headers()
                    with open(resolved, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
            except Exception:
                return self.sendj({"error": "文件读取失败"}, 500)
            return

        if p.startswith("/api/jobs/"):
            jid = p.rsplit("/", 1)[-1]
            # Signed short-lived playback URL (Bearer only, merged long videos).
            if p.endswith("/playlink"):
                jid = p.rsplit("/playlink", 1)[0].rsplit("/", 1)[-1]
                pl_q = parse_qs(urlparse(self.path).query)
                pl_disp = (pl_q.get("disposition") or ["inline"])[0]
                return self._handle_playlink(jid, disposition=pl_disp)
            jobs = jobs_load()
            j = jobs.get(jid)
            if not j:
                return self.sendj({"error": "找不到任务"}, 404)

            # ---- Long-video parent: poll all segments ----
            if j.get("long"):
                token = cfg().get("token", "")
                if not token:
                    return self.sendj({"error": "请先在设置页保存 Agnes 上游 API Key"}, 400)
                model_name = j.get("model") or "agnes-video-v2.0"
                all_done = True
                any_failed = False
                total_progress = 0
                segs = j.get("segments", [])
                for i, seg in enumerate(segs):
                    vid = seg.get("video_id")
                    if not vid:
                        if seg.get("status") == "failed":
                            any_failed = True
                        else:
                            seg["status"] = "failed"
                            seg["error"] = "分段未创建"
                            any_failed = True
                        continue
                    try:
                        _, live = call("GET", "https://apihub.agnes-ai.com/agnesapi?video_id=" + quote(vid) + "&model_name=" + quote(model_name), token)
                        safe_live = {k: v for k, v in live.items() if k in ("status", "progress", "url", "message", "error_code")}
                        updated = normalize(seg, safe_live)
                        # Write back to j["segments"][i] so changes persist
                        for k, v in updated.items():
                            seg[k] = v
                        if seg.get("url"):
                            seg["video_url"] = seg.get("url")
                        seg_progress = seg.get("progress")
                        if isinstance(seg_progress, (int, float)):
                            total_progress += int(seg_progress)
                        if seg.get("status") == "failed":
                            any_failed = True
                            all_done = False
                        elif seg.get("status") in ("queued", "processing"):
                            all_done = False
                    except Exception:
                        # Keep the current persisted segment state and let it
                        # drive the aggregate decision on the next pass.
                        pass
                # Update parent status
                num = len(segs)
                if not any_failed:
                    all_done = all(s.get("status") == "succeeded" for s in segs)
                    any_failed = any(s.get("status") == "failed" for s in segs)
                if any_failed:
                    j["status"] = "failed"
                    if num:
                        j["progress"] = int(total_progress / num)
                elif all_done:
                    j["status"] = "succeeded"
                    j["progress"] = 100
                    if j.get("merge_status") != "merged":
                        j["merge_status"] = "ready"
                else:
                    j["status"] = "processing"
                    if num:
                        j["progress"] = int(total_progress / num)
                with _jobs_lock:
                    jobs = jobs_load()
                    jobs[jid] = j
                    # Also update individual segment entries
                    for seg in j.get("segments", []):
                        sv = seg.get("video_id")
                        if sv and sv in jobs:
                            jobs[sv] = seg
                    jobs_save(jobs)
                return self.sendj(j)

            # ---- Short-video / single segment: existing behavior ----
            token = cfg().get("token", "")
            if not token:
                return self.sendj({"error": "请先在设置页保存 Agnes 上游 API Key"}, 400)
            vid = j.get("video_id") or jid
            model_name = j.get("model") or "agnes-video-v2.0"
            try:
                _, live = call("GET", "https://apihub.agnes-ai.com/agnesapi?video_id=" + quote(vid) + "&model_name=" + quote(model_name), token)
                j = normalize(j, live)
                # Strip any unexpected keys from the live response that might be sensitive
                safe_live = {k: v for k, v in live.items() if k in ("status", "progress", "url", "message", "error_code")}
                j = normalize({k: v for k, v in j.items() if k != "created_response"}, safe_live)
                if j.get("url"):
                    j["video_url"] = j.get("url")
                if j.get("status") == "succeeded" and j.get("video_url"):
                    j["output_url"] = j["video_url"]
                with _jobs_lock:
                    jobs = jobs_load()
                    jobs[jid] = j
                    jobs_save(jobs)
                return self.sendj(j)
            except Exception:
                return self.sendj({"error": "状态查询失败，请稍后重试"}, 502)

        # ---- Secure MP4 download: GET /api/download/<filename> ----
        if p.startswith("/api/download/"):
            filename = p[len("/api/download/"):].split("?", 1)[0]
            if not filename or not filename.endswith(".mp4") or "/" in filename or ".." in filename:
                return self.sendj({"error": "非法文件名"}, 400)
            safe_path = OUT / filename
            try:
                resolved = safe_path.resolve()
                out_resolved = OUT.resolve()
                if hasattr(resolved, "is_relative_to"):
                    inside = resolved.is_relative_to(out_resolved)
                else:
                    inside = str(resolved).startswith(str(out_resolved))
                if not inside or not resolved.is_file():
                    return self.sendj({"error": "文件不存在"}, 404)
            except Exception:
                return self.sendj({"error": "文件不存在"}, 404)

            try:
                fsize = resolved.stat().st_size
                action, start, end = _mp4_range_plan(self.headers.get("Range", ""), fsize)
                if action == "unsatisfiable":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % fsize)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                # disposition is fixed at auth time: Bearer -> inline (unchanged);
                # signed URL -> the value bound into its signature (whitelisted).
                disp_mode = getattr(self, "_verified_disposition", "inline")
                if disp_mode not in PLAY_DISPOSITIONS:
                    disp_mode = "inline"
                # Build the header via the shared safe generator so the
                # sanitized basename + RFC 5987 encoding is used and no raw
                # user value (incl. CRLF/NUL) reaches the header.
                disposition = _safe_content_disposition(filename, disp_mode)
                if action == "partial":
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Disposition", disposition)
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    with open(resolved, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(fsize))
                    self.send_header("Content-Disposition", disposition)
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    with open(resolved, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
            except Exception:
                return self.sendj({"error": "文件读取失败"}, 500)
            return

        return self.sendj({"error": "not found"}, 404)

    def do_HEAD(self):
        """HEAD /api/download/<basename>: same auth/validation/headers as the
        GET 200 path, but no body and Range is ignored.  Mirrors GET so a
        client can probe availability/size/content-disposition without
        downloading bytes.  Non-download paths fall through to the default
        501 (unchanged contract for those routes)."""
        p = urlparse(self.path).path
        if not p.startswith("/api/download/"):
            # Other routes keep their existing (501) HEAD behavior.
            self.send_response(501)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._require_download_auth():
            return

        filename = p[len("/api/download/"):].split("?", 1)[0]
        if not filename or not filename.endswith(".mp4") or "/" in filename or ".." in filename:
            return self.sendj({"error": "非法文件名"}, 400)
        safe_path = OUT / filename
        try:
            resolved = safe_path.resolve()
            out_resolved = OUT.resolve()
            if hasattr(resolved, "is_relative_to"):
                inside = resolved.is_relative_to(out_resolved)
            else:
                inside = str(resolved).startswith(str(out_resolved))
            if not inside or not resolved.is_file():
                return self.sendj({"error": "文件不存在"}, 404)
        except Exception:
            return self.sendj({"error": "文件不存在"}, 404)

        try:
            fsize = resolved.stat().st_size
        except OSError:
            return self.sendj({"error": "文件读取失败"}, 500)
        # disposition is fixed at auth time exactly as in the GET path.
        disp_mode = getattr(self, "_verified_disposition", "inline")
        if disp_mode not in PLAY_DISPOSITIONS:
            disp_mode = "inline"
        disposition = _safe_content_disposition(filename, disp_mode)

        # HEAD: full-GET 200 resource headers, but never send a body and
        # ignore any Range header (the resource is always reported whole).
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fsize))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        # No body written on purpose.

    def do_POST(self):
        p = urlparse(self.path).path
        # All /api/ POST routes require authentication.
        if p.startswith("/api/"):
            if not self._require_auth():
                return
        try:
            d = self.body()
        except Exception:
            return self.sendj({"error": "请求格式无效"}, 400)

        if p == "/api/config":
            c = cfg()
            if d.get("token"):
                c["token"] = str(d["token"]).strip()
            savecfg(c)
            return self.sendj({"ok": True, "token_set": bool(c.get("token"))})

        if p == "/api/test":
            token = cfg().get("token", "")
            if not token:
                return self.sendj({"error": "尚未保存 Agnes 上游 API Key"}, 400)
            try:
                status, _ = call("GET", "https://apihub.agnes-ai.com/v1/models", token, timeout=30)
                return self.sendj({"ok": True, "http_status": status, "message": "API 认证请求成功"})
            except Exception:
                return self.sendj({"error": "API 认证请求失败，请检查上游 API Key"}, 502)

        if p.startswith("/api/jobs/") and p.endswith("/merge"):
            merge_jid = p[len("/api/jobs/"):].rstrip("/merge").rstrip("/")
            # Initial job check (no lock needed, just to fast-reject unknown jid)
            _jobs = jobs_load()
            _j = _jobs.get(merge_jid)
            if not _j:
                return self.sendj({"error": "找不到任务"}, 404)
            if not _j.get("long"):
                return self.sendj({"error": "仅长视频任务支持合并"}, 400)

            # Acquire task-level mutex: concurrent requests for the same jid
            # are serialized; FFmpeg (up to 300s) is NOT held under _jobs_lock.
            _task_lock = _merge_task_lock(merge_jid)
            _task_lock.acquire()
            resp_payload = {"ok": False}
            resp_status = 500
            seg_urls = []
            tmp_dir = None
            try:
                # Re-load latest job state under the global lock
                with _jobs_lock:
                    latest_jobs = jobs_load()
                    j = latest_jobs.get(merge_jid)
                if not j:
                    resp_payload = {"error": "找不到任务"}
                    resp_status = 404

                if resp_status == 404:
                    pass
                else:
                    # Idempotency: reuse a valid, non-empty merged file inside OUT
                    all_succeeded = all(
                        s.get("status") == "succeeded" for s in j.get("segments", [])
                    )
                    if all_succeeded and _is_valid_merged_file(j.get("merged_file")):
                        resp_payload = {
                            "ok": True,
                            "reused": True,
                            "output_file": j["merged_file"],
                        }
                        resp_status = 200

                if resp_status == 200 and resp_payload.get("reused"):
                    pass
                else:
                    # Precondition: all segments must be succeeded
                    all_succeeded = all(
                        s.get("status") == "succeeded" for s in j.get("segments", [])
                    )
                    if not all_succeeded:
                        failed = [
                            s for s in j.get("segments", [])
                            if s.get("status") != "succeeded"
                        ]
                        resp_payload = {
                            "error": "合并前置条件未满足",
                            "pending_segments": len(failed),
                        }
                        resp_status = 400

                if resp_status == 400:
                    pass
                else:
                    # Download all segments
                    seg_urls = [
                        s.get("url") or s.get("video_url")
                        for s in j.get("segments", [])
                    ]
                    if any(u is None for u in seg_urls):
                        resp_payload = {"error": "部分分段缺少 URL，无法合并"}
                        resp_status = 400

                if resp_status == 400:
                    pass
                else:
                    for u in seg_urls:
                        if u:
                            pu = urlparse(u)
                            if pu.scheme != "https" or pu.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                                resp_payload = {"error": "视频 URL 不在允许的下载域名列表中"}
                                resp_status = 400
                                break

                if resp_status == 400:
                    pass
                else:
                    tmp_dir = OUT / ("merge_%s" % merge_jid)
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    files = []
                    for i, url in enumerate(seg_urls):
                        dest = tmp_dir / ("seg_%02d.mp4" % i)
                        r = download_segment(url, dest)
                        if not r["ok"]:
                            resp_payload = {
                                "error": "分段下载失败",
                                "segment": i,
                                "detail": r["error"],
                            }
                            resp_status = 502
                            break
                        files.append(str(dest))

                    if resp_status == 502:
                        pass
                    else:
                        v = validate_segment_files(files)
                        if not v["ok"]:
                            resp_payload = {
                                "error": "视频文件验证失败",
                                "details": v["errors"],
                            }
                            resp_status = 502

                    if resp_status == 502:
                        pass
                    else:
                        if not ffmpeg_available():
                            resp_payload = {
                                "error": "FFmpeg 未安装，无法合并",
                                "feature_unavailable": True,
                            }
                            resp_status = 503

                    if resp_status == 503:
                        pass
                    else:
                        # UUID-based output path: never collides with another request
                        out_path = _fresh_merge_output_path(merge_jid)
                        mr = merge_segments_ffmpeg(files, out_path)
                        if not mr["ok"]:
                            resp_payload = {"error": "合并失败", "detail": mr["error"]}
                            resp_status = 502

                    if resp_status in (502, 503):
                        pass
                    else:
                        # Persist: reload latest jobs and update ONLY merge fields
                        persisted = True
                        with _jobs_lock:
                            fresh_jobs = jobs_load()
                            j2 = fresh_jobs.get(merge_jid)
                            if not j2:
                                j2 = dict(j)
                                fresh_jobs[merge_jid] = j2
                            j2["merge_status"] = "merged"
                            j2["merged_file"] = str(out_path)
                            j2["merged_size"] = out_path.stat().st_size
                            try:
                                jobs_save(fresh_jobs)
                            except Exception:
                                persisted = False

                        if not persisted:
                            resp_payload = {
                                "ok": True,
                                "output_file": str(out_path),
                                "warning": "文件已生成但任务状态未持久化，请重试",
                                "size_bytes": out_path.stat().st_size,
                            }
                        else:
                            resp_payload = {
                                "ok": True,
                                "output_file": str(out_path),
                                "size_bytes": out_path.stat().st_size,
                            }
                        resp_status = 200
            finally:
                if tmp_dir is not None:
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                _task_lock.release()
                _merge_release_task_lock(merge_jid)

            return self.sendj(resp_payload, resp_status)

        if p == "/api/jobs":
            token = cfg().get("token", "")
            if not token:
                return self.sendj({"error": "请先在设置页保存 Agnes 上游 API Key"}, 400)
            prompt = str(d.get("prompt") or "").strip()
            if not prompt:
                return self.sendj({"error": "请输入视频描述"}, 400)
            model = d.get("model") or "agnes-video-v2.0"
            if model not in ("agnes-video-v2.0", "agnes-video-2.5-flash", "agnes-video-2.5"):
                return self.sendj({"error": "模型名不在当前界面允许列表中"}, 400)

            # ---- Long-video branch (30s or 60s) ----
            target_seconds = d.get("target_seconds")
            if target_seconds is not None:
                try:
                    ts = int(target_seconds)
                except Exception:
                    return self.sendj({"error": "target_seconds 必须是 30 或 60"}, 400)
                if ts not in LONG_VIDEO_SECONDS:
                    return self.sendj({"error": "target_seconds 仅支持 30 或 60"}, 400)
                if model == "agnes-video-v2.0":
                    return self.sendj({"error": "v2.0 模型不支持长视频分段"}, 400)
                # 10s segments, each within 4–12 range for 2.5/2.5-flash
                num_segments = ts // SEGMENT_SECONDS
                parent_id = "lv_%d" % int(time.time() * 1000)
                segment_prompts = build_segment_prompts(prompt, num_segments)
                segments = []
                segment_jobs = []
                for idx in range(num_segments):
                    seg_prompt = segment_prompts[idx]
                    seg_body = {
                        "model": model,
                        "prompt": seg_prompt,
                        "seconds": str(SEGMENT_SECONDS),
                        "size": "720P" if model == "agnes-video-2.5-flash" else "1080P",
                        "mode": "text",
                    }
                    seg_job = _job_record("pending", model, prompt, None, None, None,
                                          {"target_seconds": ts, "segment_index": idx,
                                           "segment_seconds": SEGMENT_SECONDS, "parent_id": parent_id})
                    segment_jobs.append((seg_body, seg_job))
                    segments.append({"index": idx, "video_id": None, "status": "pending_creation",
                                     "progress": 0, "url": None, "error": None})
                # Create all segments via upstream API
                created_any = False
                for seg_body, seg_job in segment_jobs:
                    try:
                        _, r = call("POST", "https://apihub.agnes-ai.com/v1/videos", token, seg_body)
                        vid = r.get("video_id")
                        if not vid:
                            raise RuntimeError("business: upstream did not return a video_id")
                        seg_job["id"] = vid
                        seg_job["video_id"] = vid
                        segments[seg_job["segment_index"]]["video_id"] = vid
                        segments[seg_job["segment_index"]]["status"] = "queued"
                        created_any = True
                    except Exception as e:
                        # Preserve the sanitized category-prefixed error so the
                        # user can tell HTTP error / timeout / network / business
                        # failure apart.  call() already sanitizes; str(e) is safe.
                        seg_err = str(e)
                        idx = seg_job["segment_index"]
                        segments[idx]["status"] = "failed"
                        segments[idx]["error"] = "创建失败: %s" % seg_err
                        seg_job["status"] = "failed"
                        seg_job["error"] = "创建失败: %s" % seg_err
                if not created_any:
                    return self.sendj({"error": "所有分段创建均失败，请重试"}, 502)
                # Build parent job record
                parent_rec = {
                    "id": parent_id,
                    "long": True,
                    "model": model,
                    "prompt": prompt,
                    "total_seconds": ts,
                    "segment_seconds": SEGMENT_SECONDS,
                    "num_segments": num_segments,
                    "segments": segments,
                    "status": "queued",
                    "progress": 0,
                    "input_type": "text",
                    "merge_status": "pending",
                    "merged_url": None,
                    "created_at": int(time.time()),
                }
                jobs = jobs_load()
                # Save each segment as its own entry (for individual polling)
                for seg_body, seg_job in segment_jobs:
                    if seg_job.get("video_id"):
                        jobs[seg_job["video_id"]] = seg_job
                    elif seg_job.get("status") == "failed":
                        pass
                # Save parent record
                jobs[parent_id] = parent_rec
                jobs_save_locked(jobs)
                return self.sendj(parent_rec, 201)

            # ---- Short-video branch (existing behavior) ----
            model_params = {}
            if model == "agnes-video-v2.0":
                body = {
                    "model": model,
                    "prompt": prompt,
                    "width": int(d.get("width", 1152)),
                    "height": int(d.get("height", 768)),
                    "num_frames": int(d.get("num_frames", 81)),
                    "frame_rate": int(d.get("frame_rate", 24)),
                }
                model_params = {"width": body["width"], "height": body["height"], "num_frames": body["num_frames"], "frame_rate": body["frame_rate"]}
            else:
                seconds = str(d.get("seconds") or "5")
                try:
                    sec = float(seconds)
                except Exception:
                    return self.sendj({"error": "seconds 必须是 4 到 12 的数字字符串"}, 400)
                if not 4 <= sec <= 12:
                    return self.sendj({"error": "2.5/2.5-flash 的 seconds 必须限制在 4 到 12 秒"}, 400)
                body = {
                    "model": model,
                    "prompt": prompt,
                    "seconds": seconds,
                    "size": "720P" if model == "agnes-video-2.5-flash" else "1080P",
                    "mode": "text",
                }
                model_params = {"seconds": seconds, "size": body["size"]}

            image = d.get("image")
            if image:
                if not (str(image).startswith("https://") or str(image).startswith("data:image/")):
                    return self.sendj({"error": "图片必须是 HTTPS 链接或图片 Data URI"}, 400)
                if len(str(image)) > 5_500_000:
                    return self.sendj({"error": "图片数据过大，请压缩后再试"}, 400)
                if model == "agnes-video-v2.0":
                    body["image"] = image
                else:
                    body["mode"] = "keyframe"
                    body["first_frame"] = image
                    body["aspect_ratio"] = "16:9"

            try:
                _, r = call("POST", "https://apihub.agnes-ai.com/v1/videos", token, body)
                vid = r.get("video_id")
                if not vid:
                    return self.sendj({"error": "创建接口未返回 video_id，未能确认任务已创建"}, 502)
                jid = vid
                j = _job_record(jid, model, prompt, image, vid, None, model_params)
                with _jobs_lock:
                    jobs = jobs_load()
                    jobs[jid] = j
                    jobs_save(jobs)
                return self.sendj(j, 201)
            except Exception:
                return self.sendj({"error": "创建任务失败，请稍后重试"}, 502)

        return self.sendj({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    print("Termux AI Video Studio (Agens): http://127.0.0.1:%d" % port)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
