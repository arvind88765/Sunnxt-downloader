#!/usr/bin/env python3
"""
SunnXT Downloader - Full GUI
Supports: Login (email/phone + password or OTP), Stream fetch, Quality selection, Download
Engines: N_m3u8DL-RE, yt-dlp, ffmpeg
"""

import os, sys, re, json, base64, time, threading, subprocess, shutil, queue, traceback
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS / CONFIG
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL   = "https://pwaapi.sunnxt.com"
BFF_URL    = "https://www.sunnxt.com"

# ISO 639-2/1 → display name for audio track labels and filenames
LANG_NAMES = {
    "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada",
    "hi": "Hindi",  "bn": "Bengali", "mr": "Marathi",  "en": "English",
    "pa": "Punjabi", "gu": "Gujarati", "or": "Odia",   "ur": "Urdu",
    "und": "Unknown",
}
UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
AES_KEY    = b"A3s68aORSgHs$71P"      # discovered from JS bundle
AES_IV     = b"\x00" * 16             # CryptoJS Hex.parse("0000000000000000")
CONTENT_LANG = "tamil,malayalam,kannada,telugu,bengali,marathi"  # exact browser order (HAR-confirmed)
PLAYBACK_FIELDS = "contents,user/currentdata,images,generalInfo,subtitles,relatedCast,globalServiceName,globalServiceId,relatedMedia,videos,thumbnailSeekPreview"

# ── Session persistence paths ────────────────────────────────────────────────
SESSION_FILE  = Path.home() / ".sunnxt_session.json"
COOKIE_FILE   = Path.home() / ".sunnxt_cookies.txt"
HISTORY_FILE  = Path.home() / ".sunnxt_history.json"
SESSION_VALIDITY_DAYS = 30   # estimated — SunnXT doesn't publish exact TTL

# ──────────────────────────────────────────────────────────────────────────────
# AES ENCRYPTION (CryptoJS-compatible)
# ──────────────────────────────────────────────────────────────────────────────

def _aes_encrypt(plaintext: str) -> str:
    """AES-CBC encrypt with key=AES_KEY, iv=AES_IV, returns base64 string."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise RuntimeError("pip install cryptography")

    data = plaintext.encode("utf-8")
    # PKCS7 pad to 16-byte boundary
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len] * pad_len)

    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV), backend=default_backend())
    enc = cipher.encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode("utf-8")


def _aes_decrypt(b64_ct: str) -> str:
    """AES-CBC decrypt, returns utf-8 string."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise RuntimeError("pip install cryptography")

    ct = base64.b64decode(b64_ct)
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV), backend=default_backend())
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    # Remove PKCS7 padding
    pad_len = pt[-1]
    return pt[:-pad_len].decode("utf-8")

# ──────────────────────────────────────────────────────────────────────────────
# HTTP SESSION (cookie-aware)
# ──────────────────────────────────────────────────────────────────────────────

_jar    = http.cookiejar.MozillaCookieJar(str(COOKIE_FILE))
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
# Cookie-less opener — HAR proves browser sends 0 cookies to BFF and gets 200
_nc_opener = urllib.request.build_opener()


def _api_headers(extra: dict = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "x-myplex-platform": "browser",
        "x-myplex-maturity-level": "",
        "X-ucv": "7",
        "contentlanguage": CONTENT_LANG,
        # Fetch metadata headers — browsers auto-add these; server checks for them
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=1, i",
        "Origin": "https://www.sunnxt.com",
        "Referer": "https://www.sunnxt.com/",
    }
    if extra:
        h.update(extra)
    return h


def _get(url: str, headers: dict = None, timeout: int = 15, opener=None) -> dict:
    hdrs = _api_headers(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    _op = opener if opener is not None else _opener
    try:
        with _op.open(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            return {"status": resp.status, "body": json.loads(text)}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return {"status": e.code, "body": json.loads(body), "error": str(e)}
        except Exception:
            return {"status": e.code, "body": body, "error": str(e)}
    except Exception as e:
        return {"status": -1, "body": None, "error": str(e)}


def _post(url: str, data: dict | str | bytes, headers: dict = None, form: bool = False) -> dict:
    hdrs = _api_headers(headers or {})
    if form:
        if isinstance(data, dict):
            encoded = urllib.parse.urlencode(data).encode("utf-8")
        else:
            encoded = data.encode("utf-8") if isinstance(data, str) else data
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=encoded, headers=hdrs, method="POST")
    else:
        if isinstance(data, dict):
            encoded = json.dumps(data).encode("utf-8")
        else:
            encoded = data.encode("utf-8") if isinstance(data, str) else data
        # Only set default Content-Type if caller didn't provide one
        if "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=encoded, headers=hdrs, method="POST")
    try:
        with _opener.open(req, timeout=20) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return {"status": resp.status, "body": json.loads(text), "raw": raw}
            except Exception:
                return {"status": resp.status, "body": text, "raw": raw}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return {"status": e.code, "body": json.loads(body), "error": str(e)}
        except Exception:
            return {"status": e.code, "body": body, "error": str(e)}
    except Exception as e:
        return {"status": -1, "body": None, "error": str(e)}

# ──────────────────────────────────────────────────────────────────────────────
# AUTH / LOGIN
# ──────────────────────────────────────────────────────────────────────────────

_session_state = {
    "logged_in": False,
    "user_id":   None,
    "username":  None,
    "login_time": None,   # unix timestamp when last logged in
}


def _decrypt_response(body) -> dict | None:
    """
    SunnXT login responses are AES-CBC encrypted.
    body may be dict like {"response": "<b64>", "version": 1}
    or a raw base64 string. Returns decrypted dict or None.
    """
    b64 = None
    if isinstance(body, dict):
        b64 = body.get("response") or body.get("payload") or body.get("data")
    elif isinstance(body, str):
        b64 = body
    if not b64:
        return None
    try:
        pt = _aes_decrypt(b64)
        return json.loads(pt)
    except Exception:
        return None


def api_login_email(email: str, password: str) -> tuple[bool, str]:
    """
    Login with email + password.
    Returns (success, message).

    SunNXT login flow (reverse-engineered from JS + HAR):
      POST /next/api/login  Content-Type: application/x-www-form-urlencoded
      Body: payload=<AES-CBC-base64-encrypted-JSON>&version=1
      Plaintext: {"userid": "<email_or_phone>", "password": "<password>"}

    The server responds with HTTP 200 and sets an HttpOnly session cookie.
    Chrome HAR exports strip HttpOnly cookies from both Set-Cookie (response)
    and Cookie (request) headers — so HAR appears to show "no cookies" but
    the browser IS sending them. Python's urllib CookieJar captures HttpOnly
    cookies correctly, so after this call _jar has the session credentials.

    The response body is an AES-CBC encrypted JSON (~118 bytes) or may be
    empty in some error cases. We accept HTTP 200 as auth success regardless
    of whether we can decrypt the body, because the real auth state is in the
    cookie, not the response body.
    """
    payload_json = json.dumps({"userid": email, "password": password})
    encrypted = _aes_encrypt(payload_json)
    body = urllib.parse.urlencode({"payload": encrypted, "version": "1"})
    url = f"{BFF_URL}/next/api/login"
    result = _post(url, body, form=True)
    if result["status"] == 200:
        bd = result["body"]
        # Try to decrypt encrypted response
        decrypted = _decrypt_response(bd)
        if decrypted:
            bd = decrypted

        # Check decrypted body for explicit failure codes
        if isinstance(bd, dict):
            code = bd.get("code") or bd.get("statusCode") or 0
            status = bd.get("status", "")
            # Explicit login failure in response body
            if isinstance(code, int) and code not in (0, 200) and status not in ("SUCCESS", ""):
                msg = bd.get("message") or bd.get("errorMessage") or str(bd)
                return False, f"Login failed: {msg}"

        # HTTP 200 = server accepted credentials and set HttpOnly session cookie
        # The cookie is now in _jar — no need to parse user_id from body (server
        # stores the session server-side, keyed by the HttpOnly cookie).
        _session_state["logged_in"] = True
        _session_state["username"] = email
        _session_state["login_time"] = time.time()
        # Try to extract user_id from body if available
        if isinstance(bd, dict):
            _session_state["user_id"] = (
                bd.get("userId") or bd.get("userid") or
                (bd.get("data", {}).get("userId") if isinstance(bd.get("data"), dict) else None) or ""
            )
            if bd.get("userName") or bd.get("username"):
                _session_state["username"] = bd.get("userName") or bd.get("username") or email
        # Count cookies set by this login
        _jar_count = sum(1 for _ in _jar)
        save_session()
        return True, f"Logged in as {_session_state['username']} ({_jar_count} session cookie(s) captured)"
    return False, f"HTTP {result['status']}: {result.get('error', result['body'])}"


def api_login_phone(phone: str, password: str) -> tuple[bool, str]:
    """Login with phone number + password."""
    return api_login_email(phone, password)


def api_send_otp(phone: str) -> tuple[bool, str]:
    """
    Request OTP for phone login.
    Returns (success, message).
    """
    # SunnXT mobile login endpoint
    url = f"{BFF_URL}/next/api/mobileSignIn"
    # Try sending mobile number
    body = urllib.parse.urlencode({"mobile": phone})
    result = _post(url, body, form=True)
    if result["status"] == 200:
        return True, "OTP sent"
    # fallback - try pwaapi
    url2 = f"{BASE_URL}/user/v2/otpLogin/"
    body2 = {"mobile": phone, "ucv": "7"}
    result2 = _post(url2, body2)
    if result2["status"] in (200, 201):
        return True, "OTP sent via pwaapi"
    return False, f"OTP send failed: {result.get('body', '')} | {result2.get('body', '')}"


def api_verify_otp(phone: str, otp: str) -> tuple[bool, str]:
    """
    Verify OTP and complete login.
    Returns (success, message).
    """
    url = f"{BASE_URL}/user/v2/otpVerify/"
    data = {"mobile": phone, "otp": otp, "ucv": "7"}
    result = _post(url, data)
    if result["status"] == 200:
        bd = result["body"]
        if isinstance(bd, dict) and bd.get("status") == "SUCCESS":
            _session_state["logged_in"] = True
            _session_state["user_id"] = bd.get("data", {}).get("userId", "")
            _session_state["username"] = phone
            _session_state["login_time"] = time.time()
            save_session()
            return True, f"OTP verified, logged in as {phone}"
    return False, f"OTP verify failed: {result.get('body', '')}"


def api_check_login() -> bool:
    """Verify session by calling userProfile."""
    url = f"{BFF_URL}/next/api/userProfile"
    result = _get(url, {"X-myplex-platform": "browser"})
    if result["status"] == 200 and isinstance(result["body"], dict):
        bd = result["body"]
        if bd.get("status") == "SUCCESS" or "userId" in str(bd):
            _session_state["logged_in"] = True
            if isinstance(bd.get("data"), dict):
                _session_state["user_id"] = bd["data"].get("userId", "")
                _session_state["username"] = bd["data"].get("userName", "")
            return True
    return False


def api_logout():
    """Logout by posting to logout endpoint and clearing cookies."""
    _post(f"{BFF_URL}/next/api/logOut", "", form=True)
    _jar.clear()
    _session_state.update({"logged_in": False, "user_id": None, "username": None, "login_time": None})
    # Delete saved session files
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        COOKIE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# SESSION PERSISTENCE
# ──────────────────────────────────────────────────────────────────────────────

def save_session():
    """Save cookies + session metadata to disk after successful login."""
    # Save cookies
    try:
        _jar.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    # Save metadata
    meta = {
        "logged_in":  _session_state.get("logged_in", False),
        "user_id":    _session_state.get("user_id", ""),
        "username":   _session_state.get("username", ""),
        "login_time": _session_state.get("login_time") or time.time(),
        "last_saved": time.time(),
    }
    try:
        SESSION_FILE.write_text(json.dumps(meta, indent=2))
    except Exception:
        pass


def load_session(cookie_path: str = None) -> dict | None:
    """
    Load saved cookies + metadata from disk.
    Returns session metadata dict or None if nothing found.
    Callers should then run api_check_login() to verify the session is still active.
    """
    meta = None
    # Load metadata
    try:
        if SESSION_FILE.exists():
            meta = json.loads(SESSION_FILE.read_text())
    except Exception:
        pass

    # Load cookies
    cfile = cookie_path or str(COOKIE_FILE)
    try:
        if Path(cfile).exists():
            tmp_jar = http.cookiejar.MozillaCookieJar(cfile)
            tmp_jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in tmp_jar:
                _jar.set_cookie(cookie)
    except Exception:
        pass

    if meta and meta.get("logged_in"):
        _session_state.update({
            "logged_in":  True,
            "user_id":    meta.get("user_id", ""),
            "username":   meta.get("username", ""),
            "login_time": meta.get("login_time"),
        })

    return meta


def get_session_age_str(login_time: float) -> str:
    """Return human-readable string of how long ago we logged in."""
    if not login_time:
        return "unknown"
    elapsed = time.time() - login_time
    days  = int(elapsed / 86400)
    hours = int((elapsed % 86400) / 3600)
    mins  = int((elapsed % 3600) / 60)
    if days > 0:
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {mins}m ago"
    return f"{mins}m ago"


def get_session_validity(login_time: float) -> tuple[str, str]:
    """
    Returns (status_text, color_key) where color_key is 'success'|'warn'|'error'.
    Assumes ~30-day session cookie lifetime (estimated from SunnXT's auth pattern).
    """
    if not login_time:
        return "No session data", "warn"
    elapsed_days = (time.time() - login_time) / 86400
    remaining    = SESSION_VALIDITY_DAYS - elapsed_days
    if remaining > 7:
        return f"~{int(remaining)} days remaining (est.)", "success"
    elif remaining > 1:
        return f"~{int(remaining)} days left — renew soon!", "warn"
    elif remaining > 0:
        return f"< 1 day left — re-login recommended!", "warn"
    else:
        return "Likely expired — please re-login", "error"

# ──────────────────────────────────────────────────────────────────────────────
# CONTENT ID EXTRACTION FROM URL
# ──────────────────────────────────────────────────────────────────────────────

def extract_content_id(url_or_id: str) -> str | None:
    """
    Extract numeric content ID from a SunnXT URL or return as-is if numeric.
    URL formats:
      https://www.sunnxt.com/tamil-movie-title/detail/151986
      https://www.sunnxt.com/en/live/sun-tv/channelid/12345
      151986
    """
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return url_or_id
    # Try /detail/{id}
    m = re.search(r"/detail/(\d+)", url_or_id)
    if m:
        return m.group(1)
    # Try /channelid/{id}
    m = re.search(r"/channelid/(\d+)", url_or_id)
    if m:
        return m.group(1)
    # Try trailing number
    m = re.search(r"/(\d{4,8})(?:[/?#]|$)", url_or_id)
    if m:
        return m.group(1)
    return None


def is_direct_stream_url(s: str) -> bool:
    """Return True if s looks like a direct MPD/M3U8 URL (not a SunnXT detail page)."""
    s = s.strip().lower()
    return (s.startswith("http") and
            (".mpd" in s or ".m3u8" in s or "manifest" in s) and
            "sunnxt.com/tamil-movie" not in s and
            "sunnxt.com/detail" not in s)


def detect_content_type(url: str) -> str:
    """Return 'live', 'movie', or 'episode' based on URL."""
    url_lower = url.lower()
    if "/live/" in url_lower or "live-tv" in url_lower or "channel" in url_lower:
        return "live"
    if "/episode/" in url_lower or "/show/" in url_lower or "/series/" in url_lower:
        return "episode"
    return "movie"

# ──────────────────────────────────────────────────────────────────────────────
# STREAM FETCH
# ──────────────────────────────────────────────────────────────────────────────

def _deep_find_urls(obj, found=None, depth=0):
    """Recursively walk any JSON structure and find stream-like URLs."""
    if found is None:
        found = []
    if depth > 15:
        return found
    if isinstance(obj, str):
        lo = obj.lower()
        # Explicit stream extensions — also accept pre-signed CloudFront URLs with query params
        if any(x in lo for x in (".mpd", ".m3u8")):
            found.append(obj)
        # Pre-signed CloudFront URLs (contain Policy= or Key-Pair-Id= — these are stream URLs)
        elif (obj.startswith("http") and "cloudfront.net" in lo and
              ("policy=" in lo or "key-pair-id=" in lo or "signature=" in lo)):
            found.append(obj)
        # CloudFront/Akamai CDN base URLs that are likely stream roots (no signature = probe needed)
        elif (("cloudfront.net" in lo or "akamaized.net" in lo or "akamaihd.net" in lo)
              and obj.startswith("http") and "images" not in lo
              and not any(ext in lo for ext in (".png", ".jpg", ".jpeg", ".gif",
                                                ".vtt", ".srt", ".css", ".js"))):
            found.append(obj)
        # Explicit stream keywords in URL path
        elif obj.startswith("http") and any(x in lo for x in (
            "/stream/", "/manifest", "/hls/", "/dash/", "playlist.m3u8",
            "index.m3u8", "manifest.mpd", "/video/", "aml"
        )):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _deep_find_urls(v, found, depth+1)
    elif isinstance(obj, list):
        for v in obj:
            _deep_find_urls(v, found, depth+1)
    return found


def _extract_from_results(data: dict, log_cb=None) -> dict:
    """
    Extract stream URL from the CORRECT SunnXT decrypted response structure.

    JS reverse-engineered structure (from _app-a8ffec4727b70cd9.js +
    6388-3af27093c16561d3.js + [index]-6879904931ed40af.js):

      {
        code: 200,
        status: "SUCCESS",
        results: [{
          videos: {
            status: "ALLOW",      // or ERR_GUEST_USER / ERR_USER_NOT_SUBSCRIBED etc.
            values: [{
              link: "https://xxx.cloudfront.net/...?Policy=...&Signature=...&Key-Pair-Id=...",
              format: "mpd",      // or "m3u8" / "hls"
              videoResolution: "1080",
              cdnType: "cloudfront",
              ...
            }]
          },
          generalInfo: { title: "...", ... },
          ...
        }]
      }

    Analytics JS reads: i.media.videos.values[0].link  (stream URL)
                        r.format                        (mpd/m3u8)
    """
    ERR_CODES = {
        "ERR_GUEST_USER":         "Not logged in — please login first",
        "ERR_NON_LOGGED_USER":    "Not logged in — please login first",
        "ERR_USER_NOT_SUBSCRIBED":"No active subscription on this account",
        "ERR_UPGRADE_PREMIUM":    "Premium subscription required",
        "ERR_GEO_RESTRICTED":     "Content not available in your region",
        "ERR_CONTENT_NOT_FOUND":  "Content not found",
        "ERR_NOT_FOUND":          "Content not found (ERR_NOT_FOUND) — this ID may not be streamable",
    }
    # Top-level API codes that indicate the content simply does not exist
    NOT_FOUND_STATUSES = {"ERR_NOT_FOUND", "ERR_CONTENT_NOT_FOUND"}
    out = {"mpd_url": None, "m3u8_url": None, "license_url": None,
           "title": None, "drm": False, "quality_list": [], "error": None}

    if not isinstance(data, dict):
        return out

    # Unwrap common envelope: {data: {response: ...}} or {data: {...}}
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    # Try decrypting if there's a "response" field that looks like base64 ciphertext
    if "response" in data and isinstance(data["response"], str):
        try:
            decrypted = _aes_decrypt(data["response"])
            data = json.loads(decrypted)
            if log_cb:
                log_cb("[decrypt] ✓ Decrypted BFF 'response' field")
        except Exception as ex:
            if log_cb:
                log_cb(f"[decrypt] Could not decrypt 'response' field: {ex}")

    def _extract_videos(videos_obj, source_label):
        """Pull MPD/M3U8 from a videos dict. Returns True if something was found.

        Priority logic:
          1. Akamai non-DRM 'dash' URL  (no Widevine needed — prefer these!)
          2. CloudFront 'dash-cenc' URL (DRM, needs key extraction)
          3. Any other MPD/M3U8 URL

        JWT/base64 tokens (link does not start with 'http') are SKIPPED — they
        are not directly usable stream URLs.
        """
        if not isinstance(videos_obj, dict):
            return False
        status = videos_obj.get("status", "")
        if log_cb:
            log_cb(f"[videos.status/{source_label}] {status!r}")
        if status in ERR_CODES:
            out["error"] = f"API error: {ERR_CODES[status]} (code={status})"
            if log_cb:
                log_cb(f"[videos] ✗ {out['error']}")
            return False
        values = videos_obj.get("values") or []
        if not isinstance(values, list):
            values = []

        # ── Two-pass extraction: prefer non-DRM Akamai dash over DRM dash-cenc ──
        # Pass 1: collect all candidate URLs with metadata
        candidates = []  # list of (priority, link, fmt, is_mpd, is_m3u8, lic, is_drm)
        for val in values:
            if not isinstance(val, dict):
                continue
            link = (val.get("link") or val.get("url") or
                    val.get("streamUrl") or val.get("streamLink") or "")
            fmt  = (val.get("format") or val.get("streamFormat") or "").lower()
            cdn  = (val.get("cdnType") or "").lower()
            lic  = (val.get("licenseUrl") or val.get("licenseServerUrl") or
                    val.get("licenseURL") or "")
            drm  = val.get("drmType") or val.get("isDRM") or val.get("drm") or False

            if not link:
                continue

            # Skip JWT tokens / base64 blobs — they are NOT usable stream URLs
            if not link.startswith("http"):
                if log_cb:
                    log_cb(f"[videos/{source_label}] skip JWT/token link "
                           f"(fmt={fmt} cdn={cdn}): {link[:60]}…")
                continue

            lo = link.lower()
            is_mpd  = ("mpd" in fmt or ".mpd" in lo or "dash" in fmt)
            is_m3u8 = ("m3u8" in fmt or "hls" in fmt or ".m3u8" in lo)
            # DRM formats: CENC (Widevine/PlayReady), Nagra, FairPlay (fp-aapl), WVM (Widevine classic)
            is_drm_fmt = ("cenc" in fmt or "widevine" in fmt or "nagra" in fmt
                          or "fp-aapl" in fmt or fmt == "wvm")

            # Resolution height for tiebreaking (higher = better, 0 = unknown)
            # Akamai CDN always provides _hd.mpd (e.g. 1280x720) AND _sd.mpd (320x240)
            # We want the HD stream, so parse "1280x720" → 720 and prefer larger heights.
            res_str = (val.get("resolution") or "").lower()
            try:
                res_h = int(res_str.split("x")[1]) if "x" in res_str else 0
            except (IndexError, ValueError):
                res_h = 0
            # EST (Electronic Sell-Through / offline) entries on -dd CDN are lower priority
            # than regular streaming entries, even when same cdnType. Detect by filename.
            is_est = "_est_" in lo

            # Priority: lower number = better
            # 0 = Akamai non-DRM dash HD (best — no decryption needed, e.g. movies2/episodes5)
            # 1 = Akamai non-DRM dash/hls (other Akamai non-DRM)
            # 2 = CloudFront non-cenc dash
            # 3 = CloudFront non-cenc m3u8
            # 4 = Any DRM MPD (needs Widevine/key extraction)
            # 5 = Any other MPD
            # 6 = Any M3U8
            # 9 = fallback / unknown
            if "akamai" in cdn and is_mpd and not is_drm_fmt and not is_est:
                priority = 0   # Akamai streaming MPD — best non-DRM choice
            elif "akamai" in cdn and is_mpd and not is_drm_fmt and is_est:
                priority = 1   # Akamai EST/offline MPD — still non-DRM but prefer streaming
            elif "akamai" in cdn and is_m3u8 and not is_drm_fmt:
                priority = 1
            elif ("cloudfront" in cdn or "sunnxt.com" in lo) and is_mpd and not is_drm_fmt:
                priority = 2
            elif ("cloudfront" in cdn or "sunnxt.com" in lo) and is_m3u8 and not is_drm_fmt:
                priority = 3
            elif is_mpd and is_drm_fmt:
                priority = 4
            elif is_mpd:
                priority = 5
            elif is_m3u8:
                priority = 6
            else:
                priority = 9

            candidates.append((priority, -res_h, link, fmt, is_mpd, is_m3u8, lic,
                                bool(drm and str(drm).lower() not in ("false","0","none",""))))

        # Sort by (priority, -resolution_height) — best CDN + highest resolution first
        candidates.sort(key=lambda x: (x[0], x[1]))

        # Pass 2: pick best MPD and best M3U8
        found = False
        for (prio, _neg_h, link, fmt, is_mpd, is_m3u8, lic, is_drm_val) in candidates:
            lo = link.lower()
            if is_mpd and not out["mpd_url"]:
                out["mpd_url"] = link
                tag = ("non-DRM Akamai" if prio <= 1 else ("DRM" if prio >= 4 else "CloudFront"))
                if log_cb:
                    log_cb(f"[videos/{source_label}] ✓ MPD [{tag} prio={prio}]: {link[:120]}…")
                found = True
            elif is_m3u8 and not out["m3u8_url"]:
                out["m3u8_url"] = link
                tag = "non-DRM Akamai" if prio <= 1 else "CloudFront"
                if log_cb:
                    log_cb(f"[videos/{source_label}] ✓ M3U8 [{tag} prio={prio}]: {link[:120]}…")
                found = True
            elif not out["mpd_url"] and not out["m3u8_url"]:
                if ".mpd" in lo or "policy=" in lo:
                    out["mpd_url"] = link
                    if log_cb: log_cb(f"[videos/{source_label}] ✓ inferred MPD: {link[:120]}…")
                    found = True
                elif ".m3u8" in lo:
                    out["m3u8_url"] = link
                    if log_cb: log_cb(f"[videos/{source_label}] ✓ inferred M3U8: {link[:120]}…")
                    found = True
            if lic and not out["license_url"]:
                out["license_url"] = lic
            if is_drm_val:
                out["drm"] = True
        return found

    # Navigate to results[0]
    results = data.get("results")
    if not isinstance(results, list) or not results:
        if log_cb:
            api_code = data.get("code", "?")
            api_msg  = data.get("message") or data.get("error") or data.get("status") or ""
            log_cb(f"[extract] No 'results' array — API code={api_code} msg={api_msg!r} "
                   f"top keys: {list(data.keys())[:12]}")
        # Map known top-level error statuses (e.g. ERR_NOT_FOUND from decrypted BFF 404)
        top_status = data.get("status", "")
        top_code   = int(data.get("code", 0) or 0)
        if top_status in ERR_CODES:
            out["error"] = f"API error: {ERR_CODES[top_status]} (code={top_status})"
            if log_cb:
                log_cb(f"[extract] ✗ top-level API error: {out['error']}")
        elif top_code == 404 and top_status not in ("SUCCESS",):
            out["error"] = (
                f"Content {top_status or 'ERR_NOT_FOUND'} — this content ID is not available "
                f"for streaming. Check on sunnxt.com if the content ID is correct."
            )
            if log_cb:
                log_cb(f"[extract] ✗ 404 not-found: {out['error']}")
        return out

    r0 = results[0]
    if not isinstance(r0, dict):
        return out

    # Title + API language (more reliable than MPD lang tags)
    gi = r0.get("generalInfo", {}) or {}
    out["title"] = (gi.get("title") or gi.get("displayTitle") or
                    data.get("title") or data.get("displayTitle"))
    # SunNXT often mis-tags MPD streams with wrong lang codes (e.g. Telugu
    # film tagged as 'ta'). Pull the actual language from generalInfo if present.
    _gi_lang = (gi.get("language") or gi.get("languages") or
                gi.get("originalLanguage") or gi.get("contentLanguage") or "")
    if _gi_lang:
        if isinstance(_gi_lang, list):
            _gi_lang = ",".join(_gi_lang)
        out["api_languages"] = _gi_lang  # e.g. "Telugu" or "Tamil,Telugu"
        if log_cb:
            log_cb(f"[extract] API language: {_gi_lang!r}")

    # ── Subtitle extraction ────────────────────────────────────────────────
    # SunNXT returns external VTT files at CloudFront CDN:
    #   subtitles.values[].{language, link_sub}
    # link_sub may or may not have .vtt extension — append it if missing.
    _subs_obj = r0.get("subtitles") or {}
    if isinstance(_subs_obj, dict):
        _sub_vals = _subs_obj.get("values") or []
    elif isinstance(_subs_obj, list):
        _sub_vals = _subs_obj
    else:
        _sub_vals = []
    _subtitle_tracks = []
    for _sv in _sub_vals:
        if not isinstance(_sv, dict):
            continue
        _sub_link = (_sv.get("link_sub") or _sv.get("link") or
                     _sv.get("url") or _sv.get("subtitleUrl") or "")
        _sub_lang = (_sv.get("language") or _sv.get("lang") or "English")
        if _sub_link and _sub_link.startswith("http"):
            if not _sub_link.lower().endswith(".vtt"):
                _sub_link = _sub_link + ".vtt"
            _subtitle_tracks.append({"language": _sub_lang, "url": _sub_link})
            if log_cb:
                log_cb(f"[subtitles] Found: {_sub_lang} → {_sub_link}")
    if _subtitle_tracks:
        out["subtitle_tracks"] = _subtitle_tracks

    if log_cb:
        log_cb(f"[extract] results[0] keys: {list(r0.keys())[:16]}")

    # ── DRM detection from content metadata (before extracting URLs) ───────
    # SunnXT puts the real DRM flag in results[0].content.drmEnabled, NOT
    # in the individual video values. Check this first so the badge is right.
    # Also check generalInfo.drm_partner (e.g. "Nagra") — non-empty means DRM.
    content_meta = r0.get("content", {}) or {}
    gi_drm       = gi.get("drm_partner", "") or ""
    if content_meta.get("drmEnabled") or content_meta.get("isMpegDash") or gi_drm:
        out["drm"] = True
        if log_cb:
            log_cb(f"[drm] Detected: content.drmEnabled={content_meta.get('drmEnabled')} "
                   f"isMpegDash={content_meta.get('isMpegDash')} "
                   f"drm_partner={gi_drm!r}")

    # ── Path A: results[0].videos (movies) ────────────────────────────────
    if _extract_videos(r0.get("videos"), "r0.videos"):
        out["quality_list"] = ["144p","240p","360p","480p","576p","720p","1080p","Best"]
        # NOTE: Do NOT clear the DRM flag here even when Akamai "dash" (non-CENC) URL
        # is selected. Some SunnXT content has format="dash" on Akamai but the MPD is
        # actually CENC-encrypted (e.g. content 245386 Devagudi shows *CENC on all
        # streams). Clearing drm=False would skip key extraction → encrypted download
        # → black screen / no audio.
        # For truly non-DRM Akamai streams: extract_pssh() returns None (no PSSH in
        # MPD) → "No PSSH or license URL" warning → drm_keys=[] → N_m3u8DL-RE runs
        # without --key flags → downloads fine. Safe to keep drm=True either way.
        return out
    if out.get("error"):
        return out

    # ── Path B: results[0].contents[*].videos (series episodes) ───────────
    contents = r0.get("contents") or []
    if isinstance(contents, dict):
        contents = contents.get("values") or contents.get("results") or []
    if isinstance(contents, list) and contents:
        if log_cb:
            log_cb(f"[extract] Trying contents[*].videos ({len(contents)} item(s))…")
        for i, item in enumerate(contents[:3]):   # check first 3
            if not isinstance(item, dict):
                continue
            gi2 = item.get("generalInfo", {}) or {}
            if not out["title"]:
                out["title"] = gi2.get("title") or gi2.get("displayTitle")
            if _extract_videos(item.get("videos"), f"contents[{i}].videos"):
                out["quality_list"] = ["144p","240p","360p","480p","576p","720p","1080p","Best"]
                return out
            if out.get("error"):
                return out

    # ── Path C: results[*] scan — maybe it's not in results[0] ────────────
    for i, r in enumerate(results[1:4], 1):
        if not isinstance(r, dict):
            continue
        if _extract_videos(r.get("videos"), f"results[{i}].videos"):
            out["quality_list"] = ["144p","240p","360p","480p","576p","720p","1080p","Best"]
            return out

    # ── Diagnostic: show what's actually in r0 ────────────────────────────
    if log_cb:
        log_cb(f"[extract] No videos found. r0 key dump:")
        for k, v in list(r0.items())[:8]:
            vtype = type(v).__name__
            vpreview = str(v)[:80] if not isinstance(v, (dict, list)) else f"{vtype}({len(v) if isinstance(v, (dict,list)) else '?'})"
            log_cb(f"  {k}: {vpreview}")

    if out["mpd_url"] or out["m3u8_url"]:
        out["quality_list"] = ["144p", "240p", "360p", "480p", "576p", "720p", "1080p", "Best"]

    return out


def _probe_stream_url(base_url: str, log_cb=None) -> str | None:
    """
    Given a CDN base URL without extension, probe common stream suffixes.
    SunnXT CloudFront streams: {base}/manifest.mpd or {base}/index.m3u8
    """
    suffixes = [
        "/manifest.mpd", ".mpd",
        "/index.m3u8", "/playlist.m3u8", ".m3u8",
        "/master.m3u8",
    ]
    for sfx in suffixes:
        candidate = base_url.rstrip("/") + sfx
        try:
            req = urllib.request.Request(candidate, method="HEAD")
            req.add_header("User-Agent", UA)
            req.add_header("Referer", "https://www.sunnxt.com/")
            with urllib.request.urlopen(req, timeout=8) as resp:
                ct = resp.headers.get("Content-Type", "")
                if resp.status == 200 and any(
                    x in ct for x in ("mpd", "m3u8", "dash", "octet", "video", "mpeg")
                ):
                    if log_cb:
                        log_cb(f"[probe] ✓ Found stream at: {candidate} ({ct})")
                    return candidate
        except Exception as e:
            if log_cb:
                log_cb(f"[probe] {sfx} → {type(e).__name__}")
    # Last resort: try the URL as-is (might serve manifest directly)
    try:
        req = urllib.request.Request(base_url, method="HEAD")
        req.add_header("User-Agent", UA)
        with urllib.request.urlopen(req, timeout=8) as resp:
            ct = resp.headers.get("Content-Type", "")
            if resp.status == 200 and any(x in ct for x in ("mpd", "dash", "m3u8", "video")):
                if log_cb:
                    log_cb(f"[probe] ✓ Base URL is stream: {base_url} ({ct})")
                return base_url
    except Exception:
        pass
    return None


def fetch_stream(content_id: str, content_type: str = "movie", debug_cb=None, log_cb=None) -> dict:
    """
    Fetch stream/playback URL for content_id.
    Returns dict with keys: mpd_url, m3u8_url, license_url, title, drm, quality_list
    debug_cb: optional callback(str) to receive raw API dumps
    """
    result = {
        "mpd_url": None, "m3u8_url": None, "license_url": None,
        "title": f"content_{content_id}", "drm": False, "quality_list": [],
        "error": None
    }

    def _dbg(label, data):
        """Full JSON dump — always to console, GUI only if Debug checkbox on."""
        try:
            txt = json.dumps(data, indent=2)[:4000] if data else "(empty)"
        except Exception:
            txt = str(data)[:4000]
        print(f"\n── {label} ──\n{txt}\n")
        if debug_cb:
            debug_cb(f"\n── {label} ──\n{txt}\n")

    hdrs = {
        "contentlanguage": CONTENT_LANG,
        "x-myplex-platform": "browser",
        # NOTE: x-myplex-maturity-level is already set to "" in _api_headers();
        # do NOT add it here again with a different case/value or both versions get sent.
        "x-ucv": "7",
        "Referer": "https://www.sunnxt.com/",
    }

    def _log(msg):
        """Step-level log — always in console + GUI (via log_cb or debug_cb)."""
        print(msg)
        cb = log_cb or debug_cb
        if cb:
            cb(msg)

    def _try_decrypt_body(body, label="") -> dict:
        """
        SunnXT BFF returns: {data: {response: "BASE64_AES_CBC"}}
        or sometimes the response key is at top level.
        Try to decrypt and return parsed dict.
        """
        if not isinstance(body, dict):
            return body
        # Try {data: {response: "..."}} envelope
        inner = body.get("data", body)
        if isinstance(inner, dict) and "response" in inner:
            try:
                pt = _aes_decrypt(inner["response"])
                decrypted = json.loads(pt)
                _log(f"[decrypt]{label} ✓ AES-CBC decrypted 'response' field")
                _dbg(f"DECRYPTED{label}", decrypted)
                return decrypted
            except Exception as ex:
                _log(f"[decrypt]{label} ✗ Failed: {ex}")
        # Try top-level "response" key
        if "response" in body and isinstance(body["response"], str):
            try:
                pt = _aes_decrypt(body["response"])
                decrypted = json.loads(pt)
                _log(f"[decrypt]{label} ✓ AES-CBC decrypted top-level 'response'")
                _dbg(f"DECRYPTED{label}", decrypted)
                return decrypted
            except Exception:
                pass
        return body

    def _apply_result(data, step_label) -> bool:
        """Run _extract_from_results then _parse_media_response, return True if URL found."""
        # Direct extraction via reverse-engineered path (highest priority)
        extracted = _extract_from_results(data, log_cb=_log)
        # Always merge metadata fields (title, api_languages, subtitle_tracks, drm)
        # even when no stream URL is found — so contentDetail gives us subtitles
        # even when Steps 1-3 fail for the stream URL.
        _META_KEYS = ("title", "api_languages", "subtitle_tracks", "drm")
        for _mk in _META_KEYS:
            if extracted.get(_mk) and not result.get(_mk):
                result[_mk] = extracted[_mk]
        if extracted.get("error"):
            err = extracted["error"]
            # Auth/subscription errors: hard stop — no point trying more steps
            hard_stop_keywords = ("Not logged in", "subscription", "Premium", "region")
            if any(k.lower() in err.lower() for k in hard_stop_keywords):
                result["error"] = err
                _log(f"[{step_label}] ✗ Auth/sub error (stopping): {err}")
                return False
            # Content-not-found: note it but keep trying other steps
            _log(f"[{step_label}] ✗ Content error (continuing): {err}")
            if not result.get("error"):
                result["_last_api_error"] = err  # tentative, overwritten if a step succeeds
            return False
        if extracted.get("mpd_url") or extracted.get("m3u8_url"):
            result.update({k: v for k, v in extracted.items() if v is not None and k != "error"})
            _log(f"[{step_label}] ✓ videos.values path: mpd={result['mpd_url']} m3u8={result['m3u8_url']}")
            return True

        # Fallback: full _parse_media_response walker
        parsed = _parse_media_response(data, content_id)
        if parsed.get("_api_error"):
            result["error"] = parsed["_api_error"]
            _log(f"[{step_label}] ✗ API error: {result['error']}")
            return False
        result.update({k: v for k, v in parsed.items()
                        if v is not None and k not in ("error", "_api_error")})
        _log(f"[{step_label}] parsed mpd={result['mpd_url']} m3u8={result['m3u8_url']}")
        if result["mpd_url"] or result["m3u8_url"]:
            return True

        # Last resort: deep scan for any stream-like URL
        urls = _deep_find_urls(data)
        _log(f"[{step_label}] deep scan: {len(urls)} url(s) → {[u[:80] for u in urls[:3]]}")
        for u in urls:
            lo = u.lower()
            if (".mpd" in lo or "policy=" in lo) and not result["mpd_url"]:
                result["mpd_url"] = u
                result.setdefault("quality_list", ["144p","240p","360p","480p","576p","720p","1080p","Best"])
            elif ".m3u8" in lo and not result["m3u8_url"]:
                result["m3u8_url"] = u
                result.setdefault("quality_list", ["144p","240p","360p","480p","576p","720p","1080p","Best"])
        return bool(result["mpd_url"] or result["m3u8_url"])

    r = r2 = r3 = r4 = r5 = {"status": -1, "body": None}

    # ── Step -1: Auto-login if session is missing ──────────────────────────────
    # The BFF (/next/api/media/{id}) requires a valid authenticated session for
    # premium/licensed content. Chrome HAR exports strip HttpOnly cookies, making
    # it appear the browser sends no cookies — it DOES send the HttpOnly session
    # cookie from /next/api/login. Python's CookieJar captures it correctly.
    # If the user hasn't logged in yet, try restoring from session file first.
    if not _session_state.get("logged_in"):
        _log("[Step -1] No active session — trying to restore from saved session file…")
        meta = load_session()
        if meta and meta.get("logged_in"):
            _log(f"[Step -1] Restored session for {meta.get('username','?')} "
                 f"(logged in {get_session_age_str(meta.get('login_time'))})")
            # Verify the restored session is still valid
            if api_check_login():
                _log("[Step -1] ✓ Session verified active")
            else:
                _log("[Step -1] ✗ Saved session expired — stream may return ERR_NOT_FOUND for auth-gated content")
        else:
            _log("[Step -1] No saved session found. Login via the Login tab before fetching premium content.")

    _log(f"[Session] logged_in={_session_state.get('logged_in')}  "
         f"cookies_in_jar={sum(1 for _ in _jar)}")

    # ── Step 0: Session warmup — visit content detail page before BFF call ────
    # Root cause of ERR_NOT_FOUND for some content IDs (e.g. 151986, 237539):
    # The Next.js BFF (/next/api/media/{id}) requires a valid server-side session
    # that is established when the browser visits the content's detail page.
    # Without visiting first, the BFF returns 404 even with a valid login session.
    # Strategy:
    #   A) Try /player/{id} — if it returns full SSR (>10KB with slug), warm done.
    #   B) If /player/{id} returns a stub (<10KB or slug=''), fetch seoUrl/slug from
    #      api.sunnxt.com contentDetail, then visit the real detail page URL which
    #      DOES return full SSR with the slug embedded in __NEXT_DATA__.
    import re as _re

    _build_id = ""
    _slug = ""
    _warm_referer = "https://www.sunnxt.com/home"

    def _fetch_page_and_extract(url, referer):
        """Fetch a page and extract buildId + slug from __NEXT_DATA__. Returns (html, buildId, slug)."""
        try:
            _req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Referer": referer,
                }
            )
            with _opener.open(_req, timeout=15) as _r:
                _h = _r.read().decode("utf-8", errors="replace")
                _log(f"[Step 0] Page: HTTP {_r.status} ({len(_h)} bytes) — {url}")
                _bid, _sl = "", ""
                _m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', _h, _re.DOTALL)
                if _m:
                    try:
                        _nd = json.loads(_m.group(1))
                        _bid = _nd.get("buildId", "")
                        _pp = _nd.get("props", {}).get("pageProps", {})
                        # Try all known slug field names
                        _cd = _pp.get("contentData") or _pp.get("content") or {}
                        if isinstance(_cd, dict):
                            _sl = (_cd.get("seoUrl") or _cd.get("slug") or
                                   _cd.get("contentSeoUrl") or "")
                        if not _sl:
                            _sl = (_pp.get("seoUrl") or _pp.get("slug") or
                                   _pp.get("contentSlug") or "")
                        _log(f"[Step 0] __NEXT_DATA__ buildId={_bid!r}  slug={_sl!r}")
                    except Exception as _ex:
                        _log(f"[Step 0] __NEXT_DATA__ parse error: {_ex}")
                return _h, _bid, _sl
        except Exception as _ex:
            _log(f"[Step 0] Page fetch error {url}: {_ex}")
            return "", "", ""

    # Step 0a: try /player/{id} first
    _player_url = f"https://www.sunnxt.com/player/{content_id}"
    _html0, _build_id, _slug = _fetch_page_and_extract(_player_url, "https://www.sunnxt.com/home")

    # Step 0b: if slug empty (stub page), get seoUrl + construct slug from api.sunnxt.com
    if not _slug:
        _log(f"[Step 0] slug empty from /player page — fetching metadata from api.sunnxt.com")
        try:
            # Include 'content' field to get language[] array needed for slug construction
            _cd_url = (f"https://api.sunnxt.com/content/v3/contentDetail/{content_id}/"
                       f"?level=devicemax&fields=generalInfo,content,tags")
            _cd_req = urllib.request.Request(
                _cd_url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Referer": "https://www.sunnxt.com/home",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                }
            )
            with _opener.open(_cd_req, timeout=10) as _cd_resp:
                _cd_body = json.loads(_cd_resp.read().decode("utf-8", errors="replace"))
                _log(f"[Step 0] contentDetail HTTP {_cd_resp.status} keys={list(_cd_body.keys())}")
                # results is a list; results[0] has generalInfo.seoUrl or .contentSeoUrl
                _results_list = _cd_body.get("results") or []
                if isinstance(_results_list, list) and _results_list:
                    _r0 = _results_list[0]
                    _gi = _r0.get("generalInfo") or {}
                    _cont = _r0.get("content") or {}
                    _slug = (_gi.get("seoUrl") or _gi.get("contentSeoUrl") or
                             _gi.get("slug") or _r0.get("seoUrl") or
                             _r0.get("slug") or "")
                    _log(f"[Step 0] seoUrl from contentDetail: {_slug!r}")
                    # Log key fields to diagnose content type / availability
                    _log(f"[Step 0] actionType={_gi.get('actionType')!r}  "
                         f"isSellable={_gi.get('isSellable')!r}  "
                         f"videoAvailable={_gi.get('videoAvailable')!r}  "
                         f"streamType={_gi.get('streamType')!r}  "
                         f"cdnType={_gi.get('cdnType')!r}")
                    _log(f"[Step 0] deepLink={str(_gi.get('deepLink',''))[:120]!r}")
                    _log(f"[Step 0] actionURL={str(_gi.get('actionURL',''))[:120]!r}")
                    _log(f"[Step 0] contentRights={_gi.get('contentRights')!r}  "
                         f"accessControl={_gi.get('accessControl')!r}")
                    _log(f"[Step 0] title={_gi.get('title')!r}  "
                         f"akamaiTag={_gi.get('akamaiTag')!r}")
                    # Construct slug if seoUrl is empty: {lang}-{type}-{title-slug}-{year}
                    if not _slug:
                        _title = _gi.get("title") or ""
                        _stype = (_gi.get("streamType") or _gi.get("type") or "movie").lower()
                        _rdate = (_cont.get("releaseDate") or _gi.get("releaseDate") or
                                  _r0.get("releaseDate") or "")
                        _year = _rdate[:4] if _rdate and len(_rdate) >= 4 else ""
                        # language: from content.language[] array (e.g. ["tamil"])
                        _lang_list = _cont.get("language") or _gi.get("language") or []
                        _lang = (_lang_list[0] if isinstance(_lang_list, list) and _lang_list
                                 else str(_lang_list)).lower() if _lang_list else ""
                        import re as _re2
                        _title_slug = _re2.sub(r"[^a-z0-9]+", "-",
                                                _title.lower()).strip("-") if _title else ""
                        if _title_slug and _lang and _year:
                            _slug = f"{_lang}-{_stype}-{_title_slug}-{_year}"
                            _log(f"[Step 0] slug CONSTRUCTED: {_slug!r} "
                                 f"(title={_title!r} lang={_lang!r} year={_year!r})")
                        else:
                            _log(f"[Step 0] slug construction failed: title={_title!r} "
                                 f"lang={_lang!r} year={_year!r}")
        except Exception as _ex:
            _log(f"[Step 0] contentDetail fetch error: {_ex}")

    # Step 0c: if we have a slug, visit the real detail page URL (no /en/ prefix in actual URL)
    if _slug:
        _detail_page_url = f"https://www.sunnxt.com/{_slug}/detail/{content_id}"
        _log(f"[Step 0] Visiting real detail page: {_detail_page_url}")
        _html1, _bid1, _sl1 = _fetch_page_and_extract(_detail_page_url, _player_url)
        if _bid1:
            _build_id = _bid1
        if _sl1:
            _slug = _sl1
        _warm_referer = _detail_page_url
    else:
        _warm_referer = _player_url

    # Step 0d: fetch _next/data — returns full stream data as plain JSON (no AES encryption)
    # This is the Next.js SSR endpoint; unlike BFF it doesn't geo-block on Sec-Fetch headers.
    if _build_id and _slug:
        try:
            _ndu = (f"https://www.sunnxt.com/_next/data/{_build_id}"
                    f"/en/{_slug}/detail/{content_id}.json"
                    f"?slug={_slug}&index={content_id}")
            _log(f"[Step 0d] _next/data: {_ndu}")
            _nd_req2 = urllib.request.Request(
                _ndu,
                headers={
                    "User-Agent": UA,
                    "Accept": "*/*",
                    "x-nextjs-data": "1",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                    "Referer": _warm_referer,
                }
            )
            with _nc_opener.open(_nd_req2, timeout=15) as _nd_resp2:
                _nd_status = _nd_resp2.status
                _nd_raw = _nd_resp2.read()
                _log(f"[Step 0d] _next/data: HTTP {_nd_status} ({len(_nd_raw)} bytes)")
                if _nd_status == 200:
                    try:
                        _nd_json = json.loads(_nd_raw.decode("utf-8", errors="replace"))
                        _nd_pp = _nd_json.get("pageProps") or {}
                        _nd_cd = _nd_pp.get("contentData") or {}
                        _dbg(f"_next/data pageProps.contentData", _nd_cd)
                        # contentData may be the full results payload — try to extract stream URLs
                        if _apply_result(_nd_cd, "Step0d/_next/data"):
                            _log("[Step 0d] ✓ _next/data returned stream URLs — using them")
                            return result
                        else:
                            _log("[Step 0d] _next/data: no stream URLs in contentData, continuing")
                    except Exception as _nd_ex:
                        _log(f"[Step 0d] _next/data parse error: {_nd_ex}")
        except Exception as _ex2:
            _log(f"[Step 0d] _next/data fetch error: {_ex2}")

    # Update Referer to the warmed page — matches what browser sends to BFF
    hdrs["Referer"] = _warm_referer

    # ── Step 1: BFF next/api/media/{id} — primary, encrypted response ─────
    media_url = (
        f"{BFF_URL}/next/api/media/{content_id}"
        f"?playbackCounter=1&fields={PLAYBACK_FIELDS}&licenseType=false"
    )
    _log(f"[Step 1] GET {media_url}")
    # Use cookie-jar opener — HAR showed 0 cookies but that's because Chrome strips
    # HttpOnly cookies from HAR exports. Browser actually sends the HttpOnly session
    # cookie set by /next/api/login. Python's CookieJar captures it; _opener sends it.
    _jar_size = sum(1 for _ in _jar)
    _log(f"[Step 1] Using session opener (jar has {_jar_size} cookie(s))")
    if _jar_size == 0 and not _session_state.get("logged_in"):
        _log("[Step 1] WARNING: No session cookies — content requiring login (e.g. premium/geo) will return ERR_NOT_FOUND. Login via the Login tab first.")
    r = _get(media_url, hdrs)
    _log(f"[Step 1] HTTP {r['status']} | error={r.get('error','none')}")
    _dbg(f"BFF /media/{content_id} raw [{r['status']}]", r.get("body"))
    if r["status"] == 200 and isinstance(r["body"], dict):
        data = _try_decrypt_body(r["body"], " [BFF]")
        if _apply_result(data, "Step1/BFF"):
            return result
        if result.get("error"):   # auth/sub error — no point continuing
            return result
    else:
        _log(f"[Step 1] FAILED: {str(r.get('body',''))[:300]}")

    # ── Step 2: pwaapi content/v3/media/{id} ──────────────────────────────
    pwa_url_v3 = (
        f"{BASE_URL}/content/v3/media/{content_id}"
        f"?playbackCounter=1&fields={PLAYBACK_FIELDS}&licenseType=false"
    )
    _log(f"[Step 2] GET {pwa_url_v3}")
    r2 = _get(pwa_url_v3, hdrs, timeout=10)
    _log(f"[Step 2] HTTP {r2['status']} | error={r2.get('error','none')}")
    _dbg(f"pwaapi v3 /media/{content_id} raw [{r2['status']}]", r2.get("body"))
    if r2["status"] == 200 and isinstance(r2["body"], dict):
        data2 = _try_decrypt_body(r2["body"], " [v3]")
        if _apply_result(data2, "Step2/v3"):
            return result
        if result.get("error"):
            return result
    else:
        _log(f"[Step 2] FAILED: {str(r2.get('body',''))[:300]}")

    # ── Step 3: pwaapi content/v8/media/{id} — discovered in JS ──────────
    pwa_url_v8 = (
        f"{BASE_URL}/content/v8/media/{content_id}"
        f"?playbackCounter=1&fields={PLAYBACK_FIELDS}&licenseType=false"
    )
    _log(f"[Step 3] GET {pwa_url_v8}")
    r3 = _get(pwa_url_v8, hdrs, timeout=10)
    _log(f"[Step 3] HTTP {r3['status']} | error={r3.get('error','none')}")
    _dbg(f"pwaapi v8 /media/{content_id} raw [{r3['status']}]", r3.get("body"))
    if r3["status"] == 200 and isinstance(r3["body"], dict):
        data3 = _try_decrypt_body(r3["body"], " [v8]")
        if _apply_result(data3, "Step3/v8"):
            return result
        if result.get("error"):
            return result
    else:
        _log(f"[Step 3] FAILED: {str(r3.get('body',''))[:300]}")

    # ── Step 4: Content detail via api.sunnxt.com (plain JSON, no encryption)
    # Browser uses api.sunnxt.com (NOT pwaapi.sunnxt.com) for contentDetail.
    # api.sunnxt.com returns plain JSON directly without AES-CBC wrapping.
    # NOTE: "user/currentdata" triggers server-side entitlement check — if the
    # session doesn't satisfy it, the server returns results:[] even for valid content.
    # Step 4a: try WITH user/currentdata (normal browser behaviour)
    detail_url = (
        f"https://api.sunnxt.com/content/v3/contentDetail/{content_id}/"
        f"?level=devicemax&fields=contents,user/currentdata,images,generalInfo,"
        f"subtitles,relatedCast,globalServiceName,globalServiceId,relatedMedia,"
        f"thumbnailSeekPreview,tags,publishingHouse"
    )
    _log(f"[Step 4] GET {detail_url}")
    r4 = _get(detail_url, hdrs, timeout=10)
    _log(f"[Step 4] HTTP {r4['status']} | error={r4.get('error','none')}")
    _dbg(f"api.sunnxt /contentDetail/{content_id} [{r4['status']}]", r4.get("body"))
    if r4["status"] == 200 and isinstance(r4["body"], dict):
        data4 = _try_decrypt_body(r4["body"], " [detail]")
        if _apply_result(data4, "Step4/detail"):
            return result
    else:
        _log(f"[Step 4] FAILED: {str(r4.get('body',''))[:300]}")

    # Step 4b: retry WITHOUT user/currentdata — bypasses entitlement filter,
    # may return video URLs for content whose entitlement check fails with our session.
    _r4b_body = r4.get("body") or {}
    _r4b_results = _r4b_body.get("results") if isinstance(_r4b_body, dict) else None
    if not _r4b_results:  # only retry if 4a gave empty results
        detail_url_nb = (
            f"https://api.sunnxt.com/content/v3/contentDetail/{content_id}/"
            f"?level=devicemax&fields=contents,images,generalInfo,subtitles,"
            f"relatedCast,globalServiceName,globalServiceId,relatedMedia,"
            f"thumbnailSeekPreview,tags,publishingHouse,videos"
        )
        _log(f"[Step 4b] GET {detail_url_nb}  (no user/currentdata)")
        r4b = _get(detail_url_nb, hdrs, timeout=10)
        _log(f"[Step 4b] HTTP {r4b['status']} | error={r4b.get('error','none')}")
        _dbg(f"api.sunnxt /contentDetail/{content_id} no-user [{r4b['status']}]", r4b.get("body"))
        if r4b["status"] == 200 and isinstance(r4b["body"], dict):
            data4b = _try_decrypt_body(r4b["body"], " [detail-nb]")
            if _apply_result(data4b, "Step4b/detail-nouser"):
                return result
        else:
            _log(f"[Step 4b] FAILED: {str(r4b.get('body',''))[:300]}")

    # ── Step 5: License info ───────────────────────────────────────────────
    if not result["license_url"]:
        lic = fetch_license_info(content_id)
        if lic:
            result["license_url"] = lic
            _log(f"[Step 5] License URL: {lic}")

    # ── Step 6: Probe signed CDN URLs (last resort — unsigned ones will 403) ─
    if not result["mpd_url"] and not result["m3u8_url"]:
        _log("[Step 6] No stream URL found yet — probing CDN base URLs…")
        cdn_candidates = []
        for resp_data in [r.get("body"), r2.get("body"), r3.get("body"), r4.get("body")]:
            if not isinstance(resp_data, dict):
                continue
            for u in _deep_find_urls(resp_data):
                if ("cloudfront.net" in u or "akamaized.net" in u) and u not in cdn_candidates:
                    cdn_candidates.append(u)
        _log(f"[Step 6] CDN candidates: {cdn_candidates}")
        for base in cdn_candidates:
            # Only probe unsigned base URLs — signed ones (with Policy=) should already be caught
            if "policy=" not in base.lower():
                found_url = _probe_stream_url(base, log_cb=_log)
                if found_url:
                    if ".m3u8" in found_url:
                        result["m3u8_url"] = found_url
                    else:
                        result["mpd_url"] = found_url
                    result.setdefault("quality_list", ["144p","240p","360p","480p","576p","720p","1080p","Best"])
                    _log(f"[Step 6] ✓ Probed stream: {found_url}")
                    break

    if not result["mpd_url"] and not result["m3u8_url"] and not result.get("error"):
        last_api_err = result.pop("_last_api_error", None)
        if last_api_err and ("not found" in last_api_err.lower() or "ERR_NOT_FOUND" in last_api_err):
            result["error"] = (
                f"Content ID {content_id} is not available for streaming (ERR_NOT_FOUND). "
                f"The content may have been removed, is not yet published, or is region-restricted. "
                f"Verify the correct content ID by opening the video on sunnxt.com."
            )
        else:
            result["error"] = (
                f"No stream URL found for {content_id}. "
                f"BFF={r['status']} v3={r2['status']} v8={r3['status']} detail={r4['status']}. "
                f"Enable Debug checkbox and check console for full JSON — "
                f"you may need to capture a fresh HAR while actually playing a video."
            )
        _log(f"[FAIL] {result['error']}")
    else:
        result.pop("_last_api_error", None)  # clean up if a step succeeded

    return result


def _parse_media_response(data: dict, content_id: str) -> dict:
    """
    Parse API response and extract stream URLs, title, DRM info.
    Handles both the legacy flat structure AND the new results[0].videos.values structure.
    """
    result = {
        "mpd_url": None, "m3u8_url": None, "license_url": None,
        "title": f"content_{content_id}", "drm": False, "quality_list": [],
    }
    if not data:
        return result

    # ── Path A: Try the new _extract_from_results path (highest priority) ──
    extracted = _extract_from_results(data)
    if extracted.get("mpd_url") or extracted.get("m3u8_url"):
        result.update({k: v for k, v in extracted.items() if v is not None})
        result["title"] = result["title"] or f"content_{content_id}"
        return result
    # Propagate error from auth check
    if extracted.get("error"):
        result["_api_error"] = extracted["error"]

    # ── Path B: Flatten {data: {...}} wrappers then walk flat structure ──────
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    # Try decrypting if wrapped response field exists
    if "response" in data and isinstance(data["response"], str):
        try:
            decrypted = _aes_decrypt(data["response"])
            data = json.loads(decrypted)
            # Re-try path A on newly decrypted data
            extracted2 = _extract_from_results(data)
            if extracted2.get("mpd_url") or extracted2.get("m3u8_url"):
                result.update({k: v for k, v in extracted2.items() if v is not None})
                result["title"] = result["title"] or f"content_{content_id}"
                return result
        except Exception:
            pass

    # Title extraction
    gi = data.get("generalInfo", {}) or {}
    result["title"] = (
        gi.get("title") or gi.get("displayTitle") or
        data.get("title") or data.get("displayTitle") or
        f"content_{content_id}"
    )

    def _parse_video_item(vid):
        url = (
            vid.get("link") or          # <- NEW: results[0].videos.values[0].link
            vid.get("amlUrl") or vid.get("url") or vid.get("streamUrl") or
            vid.get("videoUrl") or vid.get("manifestUrl") or
            vid.get("mediaUrl") or vid.get("contentUrl") or
            vid.get("dashUrl") or vid.get("hlsUrl") or
            vid.get("cdnUrl") or vid.get("playbackUrl") or ""
        )
        return url

    def _set_url(url, result):
        url_lo = url.lower()
        if ".mpd" in url_lo or "dash" in url_lo or "policy=" in url_lo:
            if not result["mpd_url"]:
                result["mpd_url"] = url
        elif ".m3u8" in url_lo or "hls" in url_lo:
            if not result["m3u8_url"]:
                result["m3u8_url"] = url
        elif (("cloudfront.net" in url_lo or "akamaized.net" in url_lo) and
              url.startswith("http") and "images" not in url_lo and
              not any(ext in url_lo for ext in (".png",".jpg",".vtt",".srt"))):
            # Bare CDN URL — could be MPD base, store as MPD tentatively
            if not result["mpd_url"]:
                result["mpd_url"] = url

    # videos field — can be dict with 'values' list, or plain list, or dict
    videos_field = data.get("videos", [])
    if isinstance(videos_field, dict):
        # New structure: { status: "...", values: [...] }
        vals = videos_field.get("values") or []
        for vid in vals:
            url = _parse_video_item(vid)
            if url:
                _set_url(url, result)
            lic = vid.get("licenseUrl") or vid.get("licenseServerUrl") or ""
            if lic and not result["license_url"]:
                result["license_url"] = lic
            drm = vid.get("drmType") or vid.get("isDRM") or False
            if drm and str(drm).lower() not in ("false","0","none",""):
                result["drm"] = True
        # Also try as a single video dict
        url = _parse_video_item(videos_field)
        if url:
            _set_url(url, result)
    elif isinstance(videos_field, list):
        for vid in videos_field:
            if not isinstance(vid, dict):
                continue
            url = _parse_video_item(vid)
            if url:
                _set_url(url, result)
            lic = vid.get("licenseUrl") or vid.get("licenseServerUrl") or vid.get("widevine_url") or ""
            if lic and not result["license_url"]:
                result["license_url"] = lic
            drm = vid.get("drmType") or vid.get("isDRM") or vid.get("encrypted") or False
            if drm and str(drm).lower() not in ("false", "0", "none",""):
                result["drm"] = True

    # Also check contents.videos
    contents = data.get("contents", {})
    if isinstance(contents, dict):
        for vid in contents.get("videos", []):
            url = (_parse_video_item(vid) if isinstance(vid, dict) else "")
            if not url:
                continue
            _set_url(url, result)

    # Build quality list
    if result["mpd_url"] or result["m3u8_url"]:
        result["quality_list"] = ["144p", "240p", "360p", "480p", "576p", "720p", "1080p", "Best"]

    return result


def fetch_license_info(content_id: str) -> str | None:
    """Fetch DRM license URL via modularLicense endpoint."""
    url = f"{BASE_URL}/licenseproxy/v3/modularLicense/?content_id={content_id}"
    r = _get(url)
    if r["status"] == 200 and isinstance(r["body"], dict):
        bd = r["body"]
        if "data" in bd and isinstance(bd["data"], dict):
            return (bd["data"].get("licenseUrl") or
                    bd["data"].get("widevine_url") or
                    bd["data"].get("dashLicenseUrl"))
    return None


def fetch_live_channels() -> list[dict]:
    """Fetch list of live TV channels."""
    url = (
        f"{BASE_URL}/content/v2/contentList"
        f"?type=live&fields=contents,images,generalInfo,publishingHouse,"
        f"relatedMedia,relatedCast,subtitles,relatedMedia,relatedMultimedia"
        f"&startIndex=1&count=100&orderBy=siblingOrder&orderMode=1"
        f"&language={CONTENT_LANG}"
    )
    r = _get(url)
    if r["status"] == 200 and isinstance(r["body"], dict):
        data = r["body"].get("data", {})
        items = data.get("items", []) or data.get("contents", []) or []
        channels = []
        for item in items:
            gi = item.get("generalInfo", {}) or {}
            channels.append({
                "id": item.get("globalServiceId") or item.get("id") or "",
                "title": gi.get("title") or gi.get("displayTitle") or "Unknown",
                "type": "live",
            })
        return channels
    return []

# ──────────────────────────────────────────────────────────────────────────────
# DRM KEY EXTRACTION (Widevine L3)
# ──────────────────────────────────────────────────────────────────────────────

def extract_pssh(mpd_text: str) -> str | None:
    """Extract Widevine PSSH from MPD XML."""
    # Look for Widevine system ID: edef8ba9-79d6-4ace-a3c8-27dcd51d21ed
    m = re.search(
        r'<cenc:pssh[^>]*>([A-Za-z0-9+/=]+)</cenc:pssh>',
        mpd_text, re.IGNORECASE
    )
    if m:
        return m.group(1)
    # Also try urn:uuid:edef8ba9
    m = re.search(
        r'<ContentProtection[^>]*edef8ba9[^>]*>.*?<cenc:pssh[^>]*>([A-Za-z0-9+/=]+)</cenc:pssh>',
        mpd_text, re.IGNORECASE | re.DOTALL
    )
    if m:
        return m.group(1)
    return None


def get_widevine_keys(
    pssh_b64: str,
    license_url: str,
    cdm_path: str,
    log_cb=None
) -> list[tuple[str, str]]:
    """
    Use pywidevine to extract Widevine keys.
    Returns list of (kid, key) tuples.
    """
    try:
        from pywidevine.cdm import Cdm
        from pywidevine.device import Device
        from pywidevine.pssh import PSSH
    except ImportError:
        if log_cb:
            log_cb("pywidevine not installed (pip install pywidevine)")
        return []

    try:
        device = Device.load(cdm_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()

        pssh = PSSH(pssh_b64)
        challenge = cdm.get_license_challenge(session_id, pssh)

        # SunnXT Nagra proxy — try request formats in order until one parses
        # Format 1: raw binary octet-stream (standard Widevine)
        # Format 2: base64 JSON body {"licenseRequest": "..."} (some proxies)
        challenge_b64 = base64.b64encode(challenge).decode()
        formats_to_try = [
            # (content-type, body, label)
            ("application/octet-stream", challenge, "raw binary"),
            ("application/json",
             json.dumps({"licenseRequest": challenge_b64}).encode(),
             "JSON licenseRequest"),
            ("application/json",
             json.dumps({"rawLicenseRequestBase64": challenge_b64}).encode(),
             "JSON rawLicenseRequestBase64"),
        ]

        lic_body = None
        last_status = None
        for ct, body_bytes, label in formats_to_try:
            if log_cb:
                log_cb(f"[DRM] trying license request format: {label}")
            hdrs_lic = {"Content-Type": ct}
            req_lic = urllib.request.Request(
                license_url, data=body_bytes,
                headers=_api_headers(hdrs_lic), method="POST"
            )
            try:
                with _opener.open(req_lic, timeout=20) as resp_lic:
                    last_status = resp_lic.status
                    raw_resp = resp_lic.read()
            except urllib.error.HTTPError as he:
                last_status = he.code
                raw_resp = he.read()
                if log_cb:
                    log_cb(f"[DRM] license HTTP {last_status} — {label}")
                continue

            if log_cb:
                # Show raw response snippet for debugging
                preview = raw_resp[:200]
                try:
                    preview_str = preview.decode("utf-8", errors="replace")
                except Exception:
                    preview_str = preview.hex()
                log_cb(f"[DRM] license response ({last_status}, {len(raw_resp)}B): {preview_str!r}")

            # Try to extract license bytes from response
            candidate = None
            try:
                resp_json = json.loads(raw_resp.decode("utf-8", errors="replace"))
                for field in ("license", "licenseData", "widevine_response",
                              "licenseMessage", "lic", "response", "data",
                              "rawLicense", "licenseBase64"):
                    val = resp_json.get(field)
                    if val and isinstance(val, str):
                        try:
                            candidate = base64.b64decode(val + "==")
                            if log_cb:
                                log_cb(f"[DRM] unwrapped license from JSON['{field}']")
                            break
                        except Exception:
                            pass
                if candidate is None and log_cb:
                    log_cb(f"[DRM] JSON keys: {list(resp_json.keys())}")
            except Exception:
                # Not JSON — use raw bytes directly
                candidate = raw_resp

            if candidate:
                try:
                    cdm.parse_license(session_id, candidate)
                    lic_body = candidate
                    if log_cb:
                        log_cb(f"[DRM] ✓ parse_license succeeded with format: {label}")
                    break
                except Exception as pe:
                    if log_cb:
                        log_cb(f"[DRM] parse_license failed ({label}): {pe}")

        if lic_body is None:
            # nagravisionDRMProxy failed for all formats.
            # Older content (e.g. Khaleja) uses Conax DRM (provider="conax.com")
            # which the Nagra proxy rejects.  The browser always succeeds via the
            # modularLicense endpoint — try that as an automatic fallback.
            m_cid = re.search(r'content_id=(\w+)', license_url)
            if m_cid:
                modular_url = (
                    "https://api.sunnxt.com/licenseproxy/v3/modularLicense/"
                    f"?content_id={m_cid.group(1)}"
                )
                if log_cb:
                    log_cb(f"[DRM] trying modularLicense fallback (Conax/older content): {modular_url}")
                req_mod = urllib.request.Request(
                    modular_url, data=challenge,
                    headers=_api_headers({"Content-Type": "application/octet-stream"}),
                    method="POST"
                )
                try:
                    with _opener.open(req_mod, timeout=20) as resp_mod:
                        raw_mod = resp_mod.read()
                        mod_status = resp_mod.status
                except urllib.error.HTTPError as he:
                    raw_mod = he.read()
                    mod_status = he.code
                except Exception as ex:
                    raw_mod = b""
                    mod_status = 0
                    if log_cb:
                        log_cb(f"[DRM] modularLicense connection error: {ex}")

                if raw_mod:
                    if log_cb:
                        preview = raw_mod[:200]
                        try:
                            preview_str = preview.decode("utf-8", errors="replace")
                        except Exception:
                            preview_str = preview.hex()
                        log_cb(f"[DRM] modularLicense response ({mod_status}, {len(raw_mod)}B): {preview_str!r}")
                    # Try raw bytes first, then unwrap JSON
                    for mod_candidate, mod_label in [
                        (raw_mod, "raw"),
                        *(
                            [(base64.b64decode(v + "=="), f"JSON['{k}']")
                             for k, v in (json.loads(raw_mod.decode("utf-8", errors="replace")).items()
                                          if not (lambda: False)() else {}.items())
                             if isinstance(v, str)]
                            if raw_mod[:1] == b"{"
                            else []
                        ),
                    ]:
                        try:
                            cdm.parse_license(session_id, mod_candidate)
                            lic_body = mod_candidate
                            if log_cb:
                                log_cb(f"[DRM] ✓ parse_license succeeded via modularLicense ({mod_label})")
                            break
                        except Exception as pe:
                            if log_cb:
                                log_cb(f"[DRM] modularLicense parse_license failed ({mod_label}): {pe}")

        if lic_body is None:
            if log_cb:
                log_cb(f"[DRM] all license formats failed (last HTTP {last_status})")
            return []

        keys = [(k.kid.hex, k.key.hex()) for k in cdm.get_keys(session_id)
                if k.type == "CONTENT"]
        cdm.close(session_id)
        return keys

    except Exception as e:
        if log_cb:
            log_cb(f"Widevine error: {e}")
        return []

# ──────────────────────────────────────────────────────────────────────────────
# DOWNLOAD ENGINE
# ──────────────────────────────────────────────────────────────────────────────

def run_download(
    stream_url: str,
    out_dir: str,
    out_name: str,
    quality: str,
    cfg: dict,
    progress_cb=None,
    log_cb=None,
    cancel_flag: list = None,     # [False] → set [True] to cancel
    drm_keys: list = None,        # [(kid, key), ...]
    speed_cb=None,                # (speed_str, eta_str) → update UI
    status_cb=None,               # (status_str) → update phase label
    audio_filter: str = "all",    # N_m3u8DL-RE --select-audio value
) -> tuple[bool, str]:
    """
    Download stream.
    Primary:  N_m3u8DL-RE (quality filter, multi-audio, subs, MKV mux)
    Fallback: yt-dlp → ffmpeg
    Returns (success, output_path).
    """
    if cancel_flag is None:
        cancel_flag = [False]

    safe_name = re.sub(r'[<>:"/\\|?*]', "_", out_name).strip()
    engine    = cfg.get("engine", "auto")
    is_drm    = bool(drm_keys)

    # Quality → N_m3u8DL-RE height filter  (height<=NNN picks best ≤ that height)
    # "Best" or unknown → None means no filter → N_m3u8DL-RE picks highest track
    quality_height = {
        "1080p": 1080, "720p": 720, "576p": 576, "480p": 480,
        "360p":  360,  "288p": 288, "240p": 240, "216p": 216,
        "180p":  180,  "144p": 144,
    }
    q_height = quality_height.get(quality, None)   # None = Best

    def _log(msg):
        if log_cb:
            log_cb(msg)

    def _cancelled():
        return cancel_flag and cancel_flag[0]

    # ── Tool discovery ──────────────────────────────────────────────────────────
    def _find_tool(cfg_key, *names):
        p = cfg.get(cfg_key, "").strip()
        if p:
            # Resolve relative paths (e.g. .\tool.exe) to absolute
            p_abs = os.path.abspath(p)
            if os.path.exists(p_abs):
                return p_abs
            if os.path.exists(p):
                return os.path.abspath(p)
        for n in names:
            found = shutil.which(n)
            if found:
                return os.path.abspath(found)
        for n in names:
            for base in [
                "/usr/local/bin", "/usr/bin",
                str(Path.home() / "bin"), str(Path.home() / ".local/bin"),
                str(Path.home() / "Downloads"),
                "C:\\tools", "C:\\Program Files", "C:\\Program Files (x86)",
                str(Path.home() / "AppData\\Local\\Microsoft\\WinGet\\Packages"),
            ]:
                candidate = os.path.join(base, n)
                if os.path.exists(candidate):
                    return os.path.abspath(candidate)
        return None

    nm3u8_path   = _find_tool("n_m3u8dl_path", "N_m3u8DL-RE", "N_m3u8DL-RE.exe")
    ytdlp_path   = _find_tool("ytdlp_path",    "yt-dlp",       "yt-dlp.exe")
    ffmpeg_path  = _find_tool("ffmpeg_path",   "ffmpeg",        "ffmpeg.exe")
    mkvmerge_path = _find_tool("mkvmerge_path", "mkvmerge",     "mkvmerge.exe")

    _log(f"[tools] N_m3u8DL-RE : {nm3u8_path  or '✗ not found'}")
    _log(f"[tools] yt-dlp       : {ytdlp_path  or '✗ not found'}")
    _log(f"[tools] ffmpeg       : {ffmpeg_path or '✗ not found'}")
    _log(f"[tools] mkvmerge     : {mkvmerge_path or '✗ not found (optional)'}")
    _log(f"[quality] Selected   : {quality} → CDN cap: maxres={q_height or 'max'} | N_m3u8DL-RE: best")

    if not (nm3u8_path or ytdlp_path or ffmpeg_path):
        _log("[ERROR] No download engine found!")
        _log("[HINT] Download N_m3u8DL-RE.exe from github.com/nilaoda/N_m3u8DL-RE")
        _log("[HINT] Set path in Settings tab → Save Settings")
        return False, ""

    # ── N_m3u8DL-RE ──────────────────────────────────────────────────────────
    if engine in ("auto", "N_m3u8DL-RE") and nm3u8_path:
        _log(f"[N_m3u8DL-RE] ▶ {safe_name} | quality={quality}")

        # Video quality filter: pick the best stream at or below requested height
        # ── CDN-level quality filter ────────────────────────────────────────────
        # SunnXT MPD URLs include aws.manifestfilter=video_height:MIN-MAX embedded
        # in the CloudFront signed URL.  Rewriting the MAX value means the CDN
        # only serves streams up to that height — far more reliable than relying
        # on N_m3u8DL-RE's internal filter which can be silently ignored.
        import re as _re
        if q_height and "aws.manifestfilter=video_height:" in stream_url:
            # Keep the original CDN lower-bound (usually 320) — setting it to 0
            # would mean no streams are served if user picks e.g. 144p.
            # We only cap the UPPER bound.  If q_height < cdn_min we still get
            # the lowest available quality (cdn_min-cdn_min).
            def _patch_cdn_filter(m):
                cdn_min = int(m.group(1))
                effective_max = max(q_height, cdn_min)   # never go below CDN floor
                return f'aws.manifestfilter=video_height:{cdn_min}-{effective_max}'
            stream_url = _re.sub(
                r'aws\.manifestfilter=video_height:(\d+)-\d+',
                _patch_cdn_filter,
                stream_url
            )
            _log(f"[quality] CloudFront CDN filter patched → height≤{q_height}")

        # Akamai CDN uses `maxres=NNN` query parameter for quality cap
        if q_height and "akamaized.net" in stream_url and "maxres=" in stream_url:
            stream_url = _re.sub(r'maxres=\d+', f'maxres={q_height}', stream_url)
            _log(f"[quality] Akamai maxres patched → maxres={q_height}")
        elif q_height and "akamaized.net" in stream_url and "maxres=" not in stream_url:
            # Append maxres if not present
            sep = "&" if "?" in stream_url else "?"
            stream_url = f"{stream_url}{sep}maxres={q_height}"
            _log(f"[quality] Akamai maxres appended → maxres={q_height}")

        # N_m3u8DL-RE --select-video filter:
        # Akamai's maxres= URL parameter is NOT always honored — some content (e.g. DC,
        # content_id=272457) serves all quality levels regardless of maxres= value.
        # CloudFront's aws.manifestfilter= is also unreliable for all content.
        #
        # RELIABLE approach: pre-fetch the MPD, parse all video Representation elements,
        # find the best stream with height ≤ q_height, extract its exact WxH resolution,
        # and pass `res=WxH` to --select-video.  N_m3u8DL-RE respects exact resolution.
        #
        # CONFIRMED BROKEN approaches (do NOT revert to these):
        #   Integer index ("4")       → silently ignored, falls back to 1080p
        #   `res=360p`                → no video selected → blank screen + audio-only MKV
        #   `for=best:height<=360`    → not supported → falls back to 1080p
        video_filter = "best"  # default: will be overridden if MPD parse succeeds

        if q_height and ".mpd" in stream_url.lower():
            try:
                import urllib.request as _ureq
                import xml.etree.ElementTree as _ET

                _log(f"[quality] Fetching MPD to determine stream index for {q_height}p cap…")
                _req_mpd = _ureq.Request(stream_url, headers={"User-Agent": "Mozilla/5.0"})
                with _ureq.urlopen(_req_mpd, timeout=15) as _r:
                    _mpd_xml = _r.read().decode("utf-8", errors="replace")

                # Parse video Representation elements — those have 'height' attribute
                _NS = "urn:mpeg:dash:schema:mpd:2011"
                _root = _ET.fromstring(_mpd_xml)
                _vid_reps = []
                for _rep in _root.iter(f"{{{_NS}}}Representation"):
                    _h = int(_rep.get("height", 0) or 0)
                    _w = int(_rep.get("width", 0) or 0)
                    _bw = int(_rep.get("bandwidth", 0) or 0)
                    if _h > 0:
                        _vid_reps.append((_bw, _h, _w))

                if _vid_reps:
                    # Sort by bandwidth descending — N_m3u8DL-RE's natural list order
                    _vid_reps.sort(key=lambda x: -x[0])
                    _log(f"[quality] MPD video streams: " +
                         ", ".join(f"{w}x{h}@{bw//1000}k" for bw, h, w in _vid_reps))

                    # Find first (best) stream with height ≤ requested quality
                    _chosen_bw = None
                    _chosen_h  = 0
                    _chosen_w  = 0
                    for _bw, _h, _w in _vid_reps:
                        if _h <= q_height:
                            _chosen_bw = _bw
                            _chosen_h  = _h
                            _chosen_w  = _w
                            break
                    else:
                        # All streams taller than cap — use lowest (last in list)
                        _chosen_bw, _chosen_h, _chosen_w = _vid_reps[-1]
                        _log(f"[quality] ⚠ {q_height}p not available in this MPD "
                             f"(lowest stream is {_chosen_h}p) — falling back to {_chosen_h}p")

                    if _chosen_bw is not None:
                        # Use exact WxH resolution selector — N_m3u8DL-RE respects this
                        # Integer index ("4") is silently ignored by N_m3u8DL-RE v0.6.0-beta
                        video_filter = f"res={_chosen_w}x{_chosen_h}"
                        _log(f"[quality] Selected stream: {_chosen_w}x{_chosen_h} "
                             f"({_chosen_h}p ≤{q_height}p cap) → "
                             f"--select-video \"{video_filter}\"")
                else:
                    _log("[quality] MPD parse: no video reps found — using best")
            except Exception as _e_mpd:
                _log(f"[quality] MPD parse error: {_e_mpd} — using best")

        # Mux format: prefer mkvmerge if available (better multi-audio support)
        if mkvmerge_path:
            mux_flag = f"format=mkv:muxer=mkvmerge"
            _log("[N_m3u8DL-RE] MKV muxer: mkvmerge ✓")
        else:
            mux_flag = "format=mkv"
            _log("[N_m3u8DL-RE] MKV muxer: ffmpeg (mkvmerge not found)")

        # Use out_dir as tmp-dir so ALL temp segment files stay on the same drive
        # as the output. Without this N_m3u8DL-RE defaults to %TEMP% (C:) which
        # can fill up the system drive on a large download.
        tmp_dir = out_dir

        cmd = [
            nm3u8_path, stream_url,
            "--save-dir",        out_dir,
            "--tmp-dir",         tmp_dir,        # keep temp files on same drive as output
            "--save-name",       safe_name,
            "--select-video",    video_filter,   # quality height filter
            "--select-audio",    audio_filter,   # audio track filter
            "--select-subtitle", "all",          # ALL subtitles
            "-M",                mux_flag,       # mux to MKV
            "--del-after-done",                  # clean up segments
            "--no-date-info",
            "-mt",                               # concurrent download (faster)
        ]
        if cfg.get("threads"):
            cmd += ["--thread-count", str(cfg["threads"])]
        if ffmpeg_path:
            cmd += ["--ffmpeg-binary-path", ffmpeg_path]
        # DRM keys
        if is_drm and drm_keys:
            for kid, key in drm_keys:
                cmd += ["--key", f"{kid}:{key}"]

        # N_m3u8DL-RE has no --mkvmerge-binary-path flag.
        # Inject mkvmerge dir into subprocess PATH so -M muxer=mkvmerge finds it.
        env = os.environ.copy()
        if mkvmerge_path:
            mkv_dir = os.path.dirname(os.path.abspath(mkvmerge_path))
            env["PATH"] = mkv_dir + os.pathsep + env.get("PATH", "")

        # Delete any existing output file so N_m3u8DL-RE's MP4DECRYPT step
        # doesn't crash with "Cannot create a file when that file already exists"
        for _ext in (".mkv", ".mp4", ".ts", ".m4v"):
            _existing = os.path.join(out_dir, safe_name + _ext)
            if os.path.exists(_existing):
                try:
                    os.remove(_existing)
                    _log(f"[N_m3u8DL-RE] Removed existing file: {os.path.basename(_existing)}")
                except Exception as _e_rm:
                    _log(f"[N_m3u8DL-RE] ⚠ Could not remove existing file: {_e_rm}")

        _log(f"[N_m3u8DL-RE] CMD: {' '.join(cmd[:6])} …")
        ok, path = _run_cmd(cmd, out_dir, safe_name, _log, progress_cb, cancel_flag, env=env, speed_cb=speed_cb, status_cb=status_cb)
        if ok:
            return True, path
        if _cancelled():
            return False, ""
        _log("[N_m3u8DL-RE] Failed → trying yt-dlp…")

    # ── yt-dlp fallback ────────────────────────────────────────────────────────
    if engine in ("auto", "yt-dlp") and ytdlp_path:
        _log(f"[yt-dlp] ▶ {safe_name} | quality={quality}")
        if q_height:
            fmt = f"bestvideo[height<={q_height}]+bestaudio/best[height<={q_height}]"
        else:
            fmt = "bestvideo+bestaudio/best"
        cmd = [
            ytdlp_path, stream_url,
            "-o",  os.path.join(out_dir, safe_name + ".%(ext)s"),
            "-f",  fmt,
            "--merge-output-format", "mkv",
            "--write-subs",
            "--all-subs",
            "--embed-subs",
            "--no-warnings",
        ]
        if cfg.get("threads"):
            cmd += ["--concurrent-fragments", str(cfg["threads"])]
        if is_drm and drm_keys:
            for kid, key in drm_keys:
                cmd += ["--key", f"{kid}:{key}"]

        ok, path = _run_cmd(cmd, out_dir, safe_name, _log, progress_cb, cancel_flag, speed_cb=speed_cb)
        if ok:
            return True, path
        if _cancelled():
            return False, ""
        _log("[yt-dlp] Failed → trying ffmpeg…")

    # ── ffmpeg last resort ─────────────────────────────────────────────────────
    if engine in ("auto", "ffmpeg") and ffmpeg_path:
        _log(f"[ffmpeg] ▶ {safe_name}.mkv (no quality filter in fallback mode)")
        out_file = os.path.join(out_dir, safe_name + ".mkv")
        cmd = [
            ffmpeg_path, "-y",
            "-i", stream_url,
            "-map", "0",        # include all streams (video + all audio + subs)
            "-c", "copy",
            out_file,
        ]
        ok, path = _run_cmd(cmd, out_dir, safe_name, _log, progress_cb, cancel_flag, speed_cb=speed_cb)
        if ok:
            return True, out_file

    _log("[ERROR] All download engines failed.")
    return False, ""


def _run_cmd(cmd, out_dir, safe_name, log_cb, progress_cb, cancel_flag, env=None, speed_cb=None, status_cb=None) -> tuple[bool, str]:
    """Run subprocess, stream output, check cancel."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            env=env,          # None = inherit parent env; set for PATH injection
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log_cb(line)
                # Parse progress — only from VIDEO stream lines to avoid audio 100%
                # stealing the bar while video is still downloading.
                # N_m3u8DL-RE lines: "Vid 640x360 | 1704 Kbps ---- 445/2450 18.16% ..."
                # Skip pure audio lines (start with "Aud ") for progress reporting.
                is_vid_line = line.lstrip().startswith("Vid ")
                is_aud_line = line.lstrip().startswith("Aud ")
                if progress_cb and not is_aud_line:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
                    if m:
                        progress_cb(float(m.group(1)))
                # Phase detection — update status label based on N_m3u8DL-RE output
                if status_cb:
                    if "Start downloading" in line:
                        status_cb("Downloading…")
                    elif "Binary merge" in line or "Binary merging" in line:
                        status_cb("Merging…")
                # Parse speed + ETA — only from lines with actual download speed.
                # N_m3u8DL-RE speed format: "9.53MBps" or "1.29MBps" (no space, no slash)
                # Stream header bitrate "105 Kbps" must NOT match — it has a space before Kbps.
                if speed_cb and (is_vid_line or is_aud_line):
                    # Match e.g. "9.53MBps" / "512KBps" — no space between number and unit
                    spd_m = re.search(r"(\d+(?:\.\d+)?)(MBps|KBps)", line)
                    # ETA: last HH:MM:SS on the line that isn't 00:00:00 (that's completed)
                    eta_candidates = re.findall(r"(\d{2}:\d{2}:\d{2})", line)
                    eta = next((e for e in reversed(eta_candidates) if e != "00:00:00"), "")
                    if spd_m:
                        unit = "MB/s" if spd_m.group(2) == "MBps" else "KB/s"
                        spd = f"{spd_m.group(1)} {unit}"
                        speed_cb(spd, eta)
            if cancel_flag and cancel_flag[0]:
                proc.kill()
                log_cb("[Cancelled]")
                return False, ""
        proc.wait()
        if proc.returncode == 0:
            # Find the output file
            for ext in (".mkv", ".mp4", ".ts", ".m4v"):
                p = os.path.join(out_dir, safe_name + ext)
                if os.path.exists(p):
                    return True, p
            # Also check for N_m3u8DL-RE output with _merged suffix
            for f in os.listdir(out_dir):
                if safe_name in f and f.endswith((".mkv", ".mp4", ".ts")):
                    return True, os.path.join(out_dir, f)
            return True, out_dir
        return False, ""
    except FileNotFoundError:
        log_cb(f"Command not found: {cmd[0]}")
        return False, ""
    except Exception as e:
        log_cb(f"Error running {cmd[0]}: {e}")
        return False, ""

# ──────────────────────────────────────────────────────────────────────────────
# GUI — THEME
# ──────────────────────────────────────────────────────────────────────────────

# Midnight dark palette — cleaner, higher contrast
BG          = "#0f0f17"      # deepest bg
BG2         = "#16161f"      # panels / sidebar
BG3         = "#1c1c28"      # card surfaces
BG4         = "#252535"      # input fields
BORDER      = "#2e2e42"      # subtle borders
FG          = "#e2e4f0"      # primary text
FG2         = "#9196b0"      # muted / labels
ACCENT      = "#7c6ef5"      # purple primary
ACCENT_H    = "#9d92f7"      # hover accent
ACCENT2     = "#5ba4cf"      # blue secondary
GREEN       = "#56d364"
YELLOW      = "#e3b341"
RED         = "#f85149"
TAG_BG      = "#1f1f32"

FONT_MAIN   = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_SB     = ("Segoe UI Semibold", 10)
FONT_TITLE  = ("Segoe UI", 13, "bold")
FONT_SMALL  = ("Segoe UI", 8)
FONT_MONO   = ("Consolas", 9)

# Keep legacy aliases so existing code still compiles
DARK_BG   = BG;   DARK_FG = FG;   ENTRY_BG = BG4
BTN_BG    = BG3;  BTN_HOVER = BORDER
SUCCESS   = GREEN; ERROR = RED; WARN = YELLOW; SUBTITLE = FG2


# ──────────────────────────────────────────────────────────────────────────────
# DOWNLOAD HISTORY helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_history() -> list:
    """Load download history from JSON file, return list of dicts."""
    try:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def save_history(history: list):
    """Persist download history (keep last 100 entries)."""
    try:
        HISTORY_FILE.write_text(
            json.dumps(history[-100:], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass

def add_history_entry(title: str, quality: str, path: str, size_mb: float = 0.0):
    """Append one completed download to history."""
    import datetime
    h = load_history()
    h.append({
        "title":   title,
        "quality": quality,
        "path":    path,
        "size_mb": round(size_mb, 2),
        "ts":      datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_history(h)


class SunnXTDownloader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SunnXT Downloader")
        self.geometry("960x680")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(820, 600)

        # State
        self._cancel_flag      = [False]
        self._dl_thread        = None
        self._stream_info      = {}
        self._cfg              = self._load_cfg()
        self._last_saved_path  = ""

        self._build_style()
        self._build_ui()
        self._restore_cfg()
        # Try to restore previous session on startup
        self.after(200, self._auto_load_session)

    # ── Style ─────────────────────────────────────────────────────────────────
    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("default")
        # Notebook
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0,0,0,0])
        s.configure("TNotebook.Tab",
            background=BG2, foreground=FG2, padding=[18, 8],
            font=FONT_SB, borderwidth=0)
        s.map("TNotebook.Tab",
            background=[("selected", BG3), ("active", BG3)],
            foreground=[("selected", FG), ("active", FG)])
        # Frames / labels
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=BG3, relief="flat")
        s.configure("TLabel", background=BG, foreground=FG, font=FONT_MAIN)
        s.configure("Sub.TLabel", background=BG, foreground=FG2, font=FONT_MAIN)
        s.configure("Card.TLabel", background=BG3, foreground=FG, font=FONT_MAIN)
        # Entry
        s.configure("TEntry",
            fieldbackground=BG4, foreground=FG,
            insertcolor=FG, font=FONT_MAIN, borderwidth=1,
            relief="flat")
        # Buttons
        s.configure("TButton",
            background=BG4, foreground=FG, font=FONT_SB,
            relief="flat", borderwidth=0, padding=[12, 6])
        s.map("TButton",
            background=[("active", BORDER), ("pressed", ACCENT)],
            foreground=[("active", FG)])
        s.configure("Accent.TButton",
            background=ACCENT, foreground="#fff", font=FONT_BOLD,
            relief="flat", borderwidth=0, padding=[14, 7])
        s.map("Accent.TButton",
            background=[("active", ACCENT_H), ("pressed", ACCENT2)],
            foreground=[("active", "#fff")])
        s.configure("Green.TButton",
            background=GREEN, foreground=BG, font=FONT_BOLD,
            relief="flat", borderwidth=0, padding=[14, 7])
        s.map("Green.TButton",
            background=[("active", "#6de67a"), ("pressed", GREEN)])
        s.configure("Red.TButton",
            background="#3d1a1a", foreground=RED, font=FONT_BOLD,
            relief="flat", borderwidth=0, padding=[12, 6])
        s.map("Red.TButton",
            background=[("active", "#4d2222")],
            foreground=[("active", RED)])
        # Combobox — visible selected text (bright purple bg, white fg)
        s.configure("TCombobox",
            fieldbackground=BG4, background=BG4,
            foreground=FG, selectbackground=ACCENT, selectforeground="#ffffff",
            font=FONT_MAIN, arrowcolor=ACCENT)
        s.map("TCombobox",
            fieldbackground=[("readonly", BG4)],
            foreground=[("readonly", FG)],
            selectbackground=[("readonly", ACCENT)],
            selectforeground=[("readonly", "#ffffff")])
        # Progressbar — beefed up height, green fill
        s.configure("Slim.Horizontal.TProgressbar",
            troughcolor=BG4, background=GREEN, thickness=14, borderwidth=0)
        s.configure("Horizontal.TProgressbar",
            troughcolor=BG4, background=GREEN, thickness=14, borderwidth=0)
        # Checkbutton
        s.configure("TCheckbutton",
            background=BG, foreground=FG2, font=FONT_MAIN)
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])

    # ── Main UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Top bar ────────────────────────────────────────────────────────
        topbar = tk.Frame(self, bg=BG2, height=48)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        tk.Label(topbar, text=" ⬇", font=("Segoe UI", 14, "bold"),
                 bg=BG2, fg=ACCENT).pack(side="left", padx=(16, 4))
        tk.Label(topbar, text="SunnXT Downloader", font=FONT_TITLE,
                 bg=BG2, fg=FG).pack(side="left")

        self._status_lbl = tk.Label(topbar, text="● Not logged in",
                                    font=FONT_SMALL, bg=BG2, fg=FG2)
        self._status_lbl.pack(side="right", padx=18)

        # thin accent stripe
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # ── Notebook ────────────────────────────────────────────────────────
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        self._tab_login    = ttk.Frame(nb, style="TFrame")
        self._tab_download = ttk.Frame(nb, style="TFrame")
        self._tab_settings = ttk.Frame(nb, style="TFrame")
        self._tab_history  = ttk.Frame(nb, style="TFrame")

        nb.add(self._tab_login,    text="  🔑  Login  ")
        nb.add(self._tab_download, text="  ⬇  Download  ")
        nb.add(self._tab_settings, text="  ⚙  Settings  ")
        nb.add(self._tab_history,  text="  🕘  History  ")

        self._build_login_tab()
        self._build_download_tab()
        self._build_settings_tab()
        self._build_history_tab()

    # ── LOGIN TAB ─────────────────────────────────────────────────────────────
    def _build_login_tab(self):
        f = self._tab_login
        f.columnconfigure(0, weight=1)
        f.rowconfigure(5, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(f, bg=BG)
        hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        tk.Label(hdr, text="🔑  Sign in to SunnXT", font=FONT_TITLE,
                 bg=BG, fg=FG).pack(side="left")

        # ── Form card ─────────────────────────────────────────────────────────
        card = tk.Frame(f, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        card.columnconfigure(1, weight=1)

        def _field(row, lbl, widget_factory):
            tk.Label(card, text=lbl, font=FONT_SMALL, bg=BG3, fg=FG2
                     ).grid(row=row, column=0, sticky="e", padx=(16, 8), pady=8)
            w = widget_factory(card)
            w.grid(row=row, column=1, sticky="ew", padx=(0, 16), pady=8)
            return w

        def _mk_entry(parent, show=""):
            e = tk.Entry(parent, font=FONT_MAIN, bg=BG4, fg=FG,
                         insertbackground=FG, relief="flat", bd=6, show=show)
            return e

        self._login_id = _field(0, "Email / Phone", lambda p: _mk_entry(p))
        self._login_id.bind("<Return>", lambda e: self._login_pass.focus())

        # Password row with eye toggle
        self._pass_lbl = tk.Label(card, text="Password", font=FONT_SMALL, bg=BG3, fg=FG2)
        self._pass_lbl.grid(row=1, column=0, sticky="e", padx=(16, 8), pady=8)
        pass_frame = tk.Frame(card, bg=BG4, highlightthickness=1,
                               highlightbackground=BORDER)
        pass_frame.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=8)
        self._login_pass = tk.Entry(pass_frame, font=FONT_MAIN, bg=BG4, fg=FG,
                                     insertbackground=FG, relief="flat", bd=6, show="●")
        self._login_pass.pack(side="left", fill="x", expand=True)
        self._login_pass.bind("<Return>", lambda e: self._do_login())
        self._pass_visible = False
        self._pass_eye_btn = tk.Button(
            pass_frame, text="👁", font=("Segoe UI", 10),
            bg=BG4, fg=FG2, activebackground=BORDER,
            activeforeground=FG, relief="flat", bd=0, padx=6, cursor="hand2",
            command=self._toggle_pass
        )
        self._pass_eye_btn.pack(side="right", padx=(0, 2))

        # ── Action buttons ────────────────────────────────────────────────────
        btn_frame = tk.Frame(f, bg=BG)
        btn_frame.grid(row=2, column=0, sticky="w", padx=18, pady=(4, 8))

        ttk.Button(btn_frame, text="  Login  ", style="Accent.TButton",
                   command=self._do_login).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Check Session",
                   command=self._check_session).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Logout",
                   command=self._do_logout).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="📂 Load Session",
                   command=self._load_session_file).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="🗑 Clear",
                   command=self._clear_session).pack(side="left")

        # ── Session status card ───────────────────────────────────────────────
        sess_panel = tk.Frame(f, bg=BG3, highlightthickness=1,
                               highlightbackground=BORDER)
        sess_panel.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 8))
        sess_panel.columnconfigure(0, weight=1)

        tk.Label(sess_panel, text="SESSION STATUS", font=FONT_SMALL,
                 bg=BG3, fg=FG2).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        tk.Frame(sess_panel, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew",
                                                        padx=12, pady=(0, 6))

        info_inner = tk.Frame(sess_panel, bg=BG3)
        info_inner.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))

        self._sess_user_lbl = tk.Label(info_inner, text="User: —",
            bg=BG3, fg=FG, font=FONT_MAIN)
        self._sess_user_lbl.grid(row=0, column=0, sticky="w", pady=2)

        self._sess_age_lbl = tk.Label(info_inner, text="Logged in: —",
            bg=BG3, fg=FG2, font=FONT_SMALL)
        self._sess_age_lbl.grid(row=1, column=0, sticky="w", pady=2)

        self._sess_validity_lbl = tk.Label(info_inner, text="Validity: —",
            bg=BG3, fg=FG2, font=FONT_SMALL)
        self._sess_validity_lbl.grid(row=2, column=0, sticky="w", pady=2)

        self._sess_file_lbl = tk.Label(info_inner,
            text=f"Session file: {SESSION_FILE}",
            bg=BG3, fg=FG2, font=("Segoe UI", 8))
        self._sess_file_lbl.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # ── Log ───────────────────────────────────────────────────────────────
        log_hdr = tk.Frame(f, bg=BG)
        log_hdr.grid(row=4, column=0, sticky="ew", padx=18, pady=(4, 2))
        tk.Label(log_hdr, text="Log", font=FONT_SMALL, bg=BG, fg=FG2).pack(side="left")

        log_wrap = tk.Frame(f, bg=BG4, highlightthickness=1,
                             highlightbackground=BORDER)
        log_wrap.grid(row=5, column=0, sticky="nsew", padx=18, pady=(0, 14))
        f.rowconfigure(5, weight=1)

        self._login_log = scrolledtext.ScrolledText(
            log_wrap, font=FONT_MONO, bg=BG4, fg=FG,
            insertbackground=FG, state="disabled", relief="flat",
            selectbackground=BORDER
        )
        self._login_log.pack(fill="both", expand=True, padx=2, pady=2)

    def _toggle_pass(self):
        self._pass_visible = not self._pass_visible
        self._login_pass.config(show="" if self._pass_visible else "●")
        self._pass_eye_btn.config(text="🙈" if self._pass_visible else "👁")

    def _do_login(self):
        uid = self._login_id.get().strip()
        pwd = self._login_pass.get().strip()
        if not uid:
            self._log_login("Enter email or phone number")
            return
        if not pwd:
            self._log_login("Enter password")
            return
        self._log_login(f"Logging in as {uid}…")
        def do():
            ok, msg = api_login_email(uid, pwd)
            self._log_login(f"{'✓' if ok else '✗'} {msg}")
            self._update_status()
        threading.Thread(target=do, daemon=True).start()

    def _check_session(self):
        self._log_login("Checking session…")
        def do():
            ok = api_check_login()
            if ok:
                self._log_login(f"✓ Active session: {_session_state['username']} (uid={_session_state['user_id']})")
            else:
                self._log_login("✗ No active session")
            self._update_status()
        threading.Thread(target=do, daemon=True).start()

    def _do_logout(self):
        api_logout()
        self._log_login("Logged out — session cleared from disk")
        self._update_status()

    def _load_session_file(self):
        """Let user manually pick a cookies .txt file to load."""
        path = filedialog.askopenfilename(
            title="Select session cookies file",
            filetypes=[("Netscape cookies", "*.txt"), ("All files", "*.*")],
            initialdir=str(Path.home()),
        )
        if not path:
            return
        self._log_login(f"Loading session from: {path}")
        def do():
            meta = load_session(cookie_path=path)
            if meta and meta.get("logged_in"):
                # Verify it's still active
                ok = api_check_login()
                if ok:
                    self._log_login(f"✓ Session restored: {_session_state['username']}")
                else:
                    self._log_login("⚠ Cookies loaded but session appears expired — try re-logging in")
            else:
                self._log_login("⚠ Could not read session from file")
            self._update_status()
        threading.Thread(target=do, daemon=True).start()

    def _clear_session(self):
        """Delete saved session files from disk."""
        try:
            SESSION_FILE.unlink(missing_ok=True)
            COOKIE_FILE.unlink(missing_ok=True)
            self._log_login("🗑 Saved session files deleted")
        except Exception as e:
            self._log_login(f"✗ Clear failed: {e}")
        self._update_status()

    def _auto_load_session(self):
        """Called on startup — tries to restore the last saved session silently."""
        def do():
            meta = load_session()
            if not meta or not meta.get("logged_in"):
                return
            # Fast-check the session
            ok = api_check_login()
            if ok:
                self._log_login(
                    f"✓ Auto-restored session: {_session_state['username']} "
                    f"(logged in {get_session_age_str(_session_state.get('login_time'))})"
                )
            else:
                self._log_login(
                    "⚠ Saved session found but server rejected it — please re-login"
                )
                _session_state["logged_in"] = False
            self._update_status()
        threading.Thread(target=do, daemon=True).start()

    def _log_login(self, msg: str):
        def _do():
            self._login_log.config(state="normal")
            self._login_log.insert("end", msg + "\n")
            self._login_log.see("end")
            self._login_log.config(state="disabled")
        self.after(0, _do)

    def _update_status(self):
        def _do():
            if _session_state["logged_in"]:
                name = _session_state.get("username") or "User"
                self._status_lbl.config(text=f"● {name}", fg=SUCCESS)
            else:
                self._status_lbl.config(text="● Not logged in", fg=SUBTITLE)
            self._update_session_panel()
        self.after(0, _do)

    def _update_session_panel(self):
        """Refresh the session info panel in the login tab."""
        lt = _session_state.get("login_time")
        if _session_state.get("logged_in") and lt:
            user = _session_state.get("username") or "Unknown"
            age  = get_session_age_str(lt)
            validity_txt, color_key = get_session_validity(lt)
            color_map = {"success": SUCCESS, "warn": WARN, "error": ERROR}
            vcol = color_map.get(color_key, DARK_FG)
            self._sess_user_lbl.config(text=f"User: {user}", fg=DARK_FG)
            self._sess_age_lbl.config(text=f"Logged in: {age}", fg=DARK_FG)
            self._sess_validity_lbl.config(text=f"Validity: {validity_txt}", fg=vcol)
            self._sess_file_lbl.config(
                text=f"Saved: {COOKIE_FILE.exists() and COOKIE_FILE or '(not saved yet)'}",
                fg=SUBTITLE
            )
        elif SESSION_FILE.exists():
            # Session file exists but not loaded
            try:
                m = json.loads(SESSION_FILE.read_text())
                user = m.get("username", "?")
                lt2  = m.get("login_time")
                age  = get_session_age_str(lt2) if lt2 else "?"
                self._sess_user_lbl.config(text=f"User: {user} (not active)", fg=SUBTITLE)
                self._sess_age_lbl.config(text=f"Last login: {age}", fg=SUBTITLE)
                validity_txt, color_key = get_session_validity(lt2) if lt2 else ("Unknown", "warn")
                color_map = {"success": SUCCESS, "warn": WARN, "error": ERROR}
                vcol = color_map.get(color_key, SUBTITLE)
                self._sess_validity_lbl.config(text=f"Validity: {validity_txt}", fg=vcol)
                self._sess_file_lbl.config(text=f"Saved: {SESSION_FILE}", fg=SUBTITLE)
            except Exception:
                self._sess_user_lbl.config(text="User: —", fg=SUBTITLE)
                self._sess_age_lbl.config(text="Logged in: —", fg=SUBTITLE)
                self._sess_validity_lbl.config(text="Validity: —", fg=SUBTITLE)
        else:
            self._sess_user_lbl.config(text="User: —", fg=SUBTITLE)
            self._sess_age_lbl.config(text="Logged in: —", fg=SUBTITLE)
            self._sess_validity_lbl.config(text="No saved session", fg=SUBTITLE)
            self._sess_file_lbl.config(text=f"Session file: {SESSION_FILE} (none)", fg=SUBTITLE)

    # ── DOWNLOAD TAB ──────────────────────────────────────────────────────────
    def _build_download_tab(self):
        f = self._tab_download
        f.configure(style="TFrame")

        # ── Left column (main) + right column (meta card) ─────────────────
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0, minsize=240)
        f.rowconfigure(6, weight=1)   # log expands

        # ── URL row ────────────────────────────────────────────────────────
        url_wrap = tk.Frame(f, bg=BG)
        url_wrap.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 4))

        tk.Label(url_wrap, text="URL or Content ID", font=FONT_SMALL,
                 bg=BG, fg=FG2).pack(anchor="w")

        url_inner = tk.Frame(url_wrap, bg=BG4, highlightthickness=1,
                              highlightbackground=BORDER)
        url_inner.pack(fill="x", pady=(3, 0))

        self._url_entry = tk.Entry(url_inner, font=FONT_MAIN,
                                   bg=BG4, fg=FG, insertbackground=FG,
                                   relief="flat", bd=6)
        self._url_entry.pack(side="left", fill="x", expand=True)
        self._url_entry.bind("<Return>", lambda e: self._do_fetch())

        self._debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(url_inner, text="Debug", variable=self._debug_var
                        ).pack(side="left", padx=6)

        self._fetch_btn = ttk.Button(url_inner, text="Fetch Stream",
                   style="Accent.TButton", command=self._do_fetch)
        self._fetch_btn.pack(side="left", padx=(0, 0), pady=0)

        # ── Options row ────────────────────────────────────────────────────
        opts = tk.Frame(f, bg=BG)
        opts.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(8, 4))

        # Quality
        qbox = tk.Frame(opts, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        qbox.pack(side="left", padx=(0, 10))
        tk.Label(qbox, text="  Quality", font=FONT_SMALL, bg=BG3, fg=FG2
                 ).pack(side="left", padx=(6,2))
        self._quality_var = tk.StringVar(value="Best")
        self._quality_combo = ttk.Combobox(qbox, textvariable=self._quality_var,
            values=["Best", "1080p", "720p", "576p", "480p", "360p", "240p", "144p"],
            width=8, state="readonly")
        self._quality_combo.pack(side="left", padx=(0, 6), pady=4)

        # Audio track selector
        abox = tk.Frame(opts, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        abox.pack(side="left", padx=(0, 10))
        tk.Label(abox, text="  Audio", font=FONT_SMALL, bg=BG3, fg=FG2
                 ).pack(side="left", padx=(6, 2))
        self._audio_var = tk.StringVar(value="All")
        self._audio_combo = ttk.Combobox(abox, textvariable=self._audio_var,
            values=["All"], width=18, state="readonly")
        self._audio_combo.pack(side="left", padx=(0, 6), pady=4)

        # Output dir
        obox = tk.Frame(opts, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        obox.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Label(obox, text="  Output Folder", font=FONT_SMALL, bg=BG3, fg=FG2
                 ).pack(side="left", padx=(6, 2))
        self._outdir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        tk.Entry(obox, textvariable=self._outdir_var, font=FONT_MAIN,
                 bg=BG4, fg=FG, insertbackground=FG, relief="flat", bd=4
                 ).pack(side="left", fill="x", expand=True, pady=4)
        ttk.Button(obox, text="…", width=3,
                   command=self._browse_out).pack(side="left", padx=(4, 6))

        # Filename
        nbox = tk.Frame(opts, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        nbox.pack(side="left", fill="x", expand=True)
        tk.Label(nbox, text="  Filename", font=FONT_SMALL, bg=BG3, fg=FG2
                 ).pack(side="left", padx=(6, 2))
        self._outname_var = tk.StringVar(value="")
        tk.Entry(nbox, textvariable=self._outname_var, font=FONT_MAIN,
                 bg=BG4, fg=FG, insertbackground=FG, relief="flat", bd=4
                 ).pack(side="left", fill="x", expand=True, padx=(0, 6), pady=4)

        # ── Stream info card (right side) ──────────────────────────────────
        card = tk.Frame(f, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=2, column=1, rowspan=3, sticky="nsew", padx=(0, 18), pady=(8, 8))

        hdr_row = tk.Frame(card, bg=BG3)
        hdr_row.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(hdr_row, text="STREAM INFO", font=FONT_SMALL, bg=BG3, fg=FG2).pack(side="left")
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=12)

        self._title_lbl = tk.Label(card, text="—", font=FONT_BOLD,
                                   bg=BG3, fg=FG, wraplength=200, justify="left")
        self._title_lbl.pack(anchor="w", padx=12, pady=(8, 2))

        self._drm_lbl = tk.Label(card, text="", font=FONT_SMALL, bg=BG3, fg=YELLOW)
        self._drm_lbl.pack(anchor="w", padx=12)

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=12, pady=6)

        # Stream URL rows — truncated display + copy button each
        def _url_row(label_text, attr_name):
            row = tk.Frame(card, bg=BG3)
            row.pack(fill="x", padx=8, pady=(0, 4))
            tk.Label(row, text=label_text, font=FONT_SMALL, bg=BG3, fg=FG2,
                     width=4, anchor="w").pack(side="left")
            lbl = tk.Label(row, text="—", font=("Consolas", 8), bg=BG3, fg=FG2,
                           anchor="w", width=16, cursor="arrow")
            lbl.pack(side="left", fill="x", expand=True)
            def _copy(a=attr_name):
                val = getattr(self, f"_{a}_full", "")
                if val:
                    self.clipboard_clear(); self.clipboard_append(val)
            btn = tk.Button(row, text="⎘", font=("Segoe UI", 8), bg=BG3, fg=FG2,
                            activebackground=BORDER, relief="flat", bd=0,
                            padx=4, cursor="hand2", command=_copy)
            btn.pack(side="right")
            setattr(self, f"_{attr_name}_lbl", lbl)

        _url_row("MPD", "mpd_url")
        _url_row("LIC", "lic_url")

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(4, 6))

        self._stream_info_text = tk.Text(
            card, font=("Consolas", 8), bg=BG3, fg=FG2,
            relief="flat", state="disabled", wrap="word",
            width=28, height=6, bd=0, padx=6, pady=4,
            cursor="arrow"
        )
        self._stream_info_text.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        # ── Download button + progress (left column) ───────────────────────
        dl_ctrl = tk.Frame(f, bg=BG)
        dl_ctrl.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 2))

        self._dl_btn = ttk.Button(dl_ctrl, text="⬇  Download",
            style="Green.TButton", command=self._do_download, state="disabled")
        self._dl_btn.pack(side="left", padx=(0, 6))

        self._cancel_btn = ttk.Button(dl_ctrl, text="✕  Cancel",
            style="Red.TButton", command=self._do_cancel, state="disabled")
        self._cancel_btn.pack(side="left", padx=(0, 6))

        # Open Folder button — hidden until a download completes
        self._open_folder_btn = ttk.Button(dl_ctrl, text="📂  Open Folder",
            command=self._do_open_folder, state="disabled")
        self._open_folder_btn.pack(side="left", padx=(0, 6))

        self._dl_status = tk.Label(dl_ctrl, text="", bg=BG, fg=FG2, font=FONT_MAIN)
        self._dl_status.pack(side="left", padx=8)

        # Progress bar row — bar + speed/ETA inline
        prog_wrap = tk.Frame(f, bg=BG)
        prog_wrap.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 2))
        prog_wrap.columnconfigure(0, weight=1)

        self._progress = ttk.Progressbar(prog_wrap, orient="horizontal",
            style="Slim.Horizontal.TProgressbar", mode="determinate")
        self._progress.grid(row=0, column=0, sticky="ew")

        # Speed / ETA — right of bar, same row
        self._speed_lbl = tk.Label(prog_wrap, text="", bg=BG, fg=ACCENT2,
                                   font=("Consolas", 9), width=10, anchor="e")
        self._speed_lbl.grid(row=0, column=1, padx=(6, 0))

        # Engine status line — row 4
        self._engine_status = tk.Label(f, text="", font=FONT_SMALL, bg=BG, fg=FG2)
        self._engine_status.grid(row=4, column=0, sticky="w", padx=20)

        # ── Manual Widevine Key (Vineless / CDM bypass) ─────────────── row 5
        # Hidden var — kept for DRM key logic compatibility, not shown in UI
        self._manual_key_var = tk.StringVar(value="")

        # ── Log ─────────────────────────────────────────────────────── row 5/6
        log_hdr = tk.Frame(f, bg=BG)
        log_hdr.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 2))


        tk.Label(log_hdr, text="Download Log", font=FONT_SMALL,
                 bg=BG, fg=FG2).pack(side="left")
        ttk.Button(log_hdr, text="Copy Log", command=self._copy_dl_log
                   ).pack(side="right", padx=(4, 0))
        ttk.Button(log_hdr, text="Clear", command=self._clear_dl_log
                   ).pack(side="right")

        log_wrap = tk.Frame(f, bg=BG4, highlightthickness=1,
                             highlightbackground=BORDER)
        log_wrap.grid(row=6, column=0, sticky="nsew", padx=18, pady=(0, 14))
        f.rowconfigure(6, weight=1)

        self._dl_log = scrolledtext.ScrolledText(
            log_wrap, font=("Consolas", 9), bg=BG4, fg=FG,
            insertbackground=FG, state="disabled", relief="flat",
            selectbackground=BORDER, spacing1=2, spacing3=2
        )
        self._dl_log.pack(fill="both", expand=True, padx=2, pady=2)
        # Color tags for log lines
        self._dl_log.tag_config("ok",     foreground=GREEN)
        self._dl_log.tag_config("err",    foreground=RED)
        self._dl_log.tag_config("warn",   foreground=YELLOW)
        self._dl_log.tag_config("key",    foreground=ACCENT2)
        self._dl_log.tag_config("normal", foreground=FG2)

        f.columnconfigure(0, weight=1)

    def _browse_out(self):
        d = filedialog.askdirectory(initialdir=self._outdir_var.get())
        if d:
            self._outdir_var.set(d)

    def _do_fetch(self):
        raw = self._url_entry.get().strip()
        if not raw:
            return

        # ── Direct MPD/M3U8 URL mode ───────────────────────────────────────
        # If user pastes an MPD/M3U8 URL directly (e.g. from browser Network tab
        # while Vineless is running), skip the BFF fetch entirely and use it as-is.
        if is_direct_stream_url(raw):
            self._log_dl(f"[Direct URL] Detected MPD/M3U8 URL — skipping BFF fetch")
            self._log_dl(f"[Direct URL] If DRM-encrypted, set CDM path in Settings.")
            is_mpd = ".mpd" in raw.lower() or "dash" in raw.lower()
            info = {
                "mpd_url": raw if is_mpd else None,
                "m3u8_url": raw if not is_mpd else None,
                "license_url": None,
                "title": "Direct URL",
                "drm": bool(self._manual_key_var.get().strip()),
                "quality_list": ["Best", "1080p", "720p", "480p", "360p"],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "error": None,
            }
            self._stream_info = info
            self.after(0, lambda: self._on_stream_fetched(info))
            return

        cid = extract_content_id(raw)
        if not cid:
            self._log_dl(f"Cannot extract content ID from: {raw}")
            self._log_dl(f"TIP: You can also paste an MPD URL directly (e.g. from browser Network tab)")
            return
        ctype = detect_content_type(raw)
        self._log_dl(f"Fetching stream for content_id={cid} (type={ctype})…")
        self._fetch_btn.config(state="disabled", text="Fetching…")
        self._dl_btn.config(state="disabled")
        debug = self._debug_var.get()

        def do():
            import xml.etree.ElementTree as _ET_fetch
            info = fetch_stream(cid, ctype, log_cb=self._log_dl, debug_cb=self._log_dl if debug else None)
            self._stream_info = info

            # ── Probe MPD for real quality list + audio tracks ──────────────
            mpd_url = info.get("mpd_url")
            if mpd_url:
                try:
                    _req_mpd = urllib.request.Request(mpd_url, headers={"User-Agent": UA})
                    with urllib.request.urlopen(_req_mpd, timeout=15) as _r:
                        _mpd_xml = _r.read().decode("utf-8", errors="replace")
                    _NS = "urn:mpeg:dash:schema:mpd:2011"
                    _root = _ET_fetch.fromstring(_mpd_xml)

                    # Unique video heights
                    _vid_heights = set()
                    for _rep in _root.iter(f"{{{_NS}}}Representation"):
                        _h = int(_rep.get("height", 0) or 0)
                        if _h > 0:
                            _vid_heights.add(_h)
                    if _vid_heights:
                        _qlabels = [f"{h}p" for h in sorted(_vid_heights)] + ["Best"]
                        info["quality_list"] = _qlabels
                        self._log_dl(f"[MPD] Video qualities: {', '.join(_qlabels)}")

                    # Audio tracks: lang + bitrate + channels
                    _audio_tracks = []
                    for _adapt in _root.iter(f"{{{_NS}}}AdaptationSet"):
                        _mime = (_adapt.get("mimeType") or "").lower()
                        _ct   = (_adapt.get("contentType") or "").lower()
                        if "audio" not in _mime and "audio" not in _ct:
                            # Check if any Representation has audio codec
                            _has_audio = any(
                                "mp4a" in (_r.get("codecs") or "")
                                for _r in _adapt
                            )
                            if not _has_audio:
                                continue
                        _lang = _adapt.get("lang") or _adapt.get("language") or "und"
                        for _rep in _adapt.iter(f"{{{_NS}}}Representation"):
                            _bw = int(_rep.get("bandwidth", 0) or 0)
                            _codecs = _rep.get("codecs") or _adapt.get("codecs") or "mp4a"
                            _ch_el = _rep.find(f".//{{{_NS}}}AudioChannelConfiguration") or \
                                     _adapt.find(f".//{{{_NS}}}AudioChannelConfiguration")
                            _ch = int(_ch_el.get("value", 2)) if _ch_el is not None else 2
                            _codec_name = "AAC" if "mp4a" in _codecs else _codecs.upper()
                            _audio_tracks.append({
                                "lang":    _lang,
                                "kbps":    _bw // 1000,
                                "channels": _ch,
                                "codec":   _codec_name,
                            })
                    if _audio_tracks:
                        info["audio_tracks"] = _audio_tracks
                        _at_str = ", ".join(
                            f"{LANG_NAMES.get(t['lang'], t['lang'])} {t['kbps']}kbps {t['channels']}CH"
                            for t in _audio_tracks
                        )
                        self._log_dl(f"[MPD] Audio tracks: {_at_str}")
                except Exception as _e_mpd_probe:
                    self._log_dl(f"[MPD] Probe warning: {_e_mpd_probe}")

            # ── HLS fallback quality probe ──────────────────────────────────
            m3u8 = info.get("m3u8_url")
            if m3u8 and not info.get("drm") and not info.get("quality_list"):
                try:
                    req = urllib.request.Request(m3u8, headers=_api_headers())
                    with urllib.request.urlopen(req, timeout=10) as r:
                        manifest = r.read().decode("utf-8", errors="replace")
                    resolutions = re.findall(r'RESOLUTION=\d+x(\d+)', manifest)
                    if resolutions:
                        heights = sorted(set(int(h) for h in resolutions))
                        labels = [f"{h}p" for h in heights] + ["Best"]
                        info["quality_list"] = labels
                        self._log_dl(f"[HLS] Real qualities: {', '.join(labels)}")
                except Exception:
                    pass

            self.after(0, lambda: self._on_stream_fetched(info))

        threading.Thread(target=do, daemon=True).start()

    def _on_stream_fetched(self, info: dict):
        # Always restore fetch button
        self._fetch_btn.config(state="normal", text="Fetch Stream")

        if info.get("error") and not info.get("mpd_url") and not info.get("m3u8_url"):
            self._log_dl(f"✗ {info['error']}")
            self._title_lbl.config(text="—", fg=FG2)
            self._drm_lbl.config(text="")
            return

        title = info.get("title", "unknown")
        self._title_lbl.config(text=title, fg=GREEN)

        drm = info.get("drm", False)
        if drm:
            self._drm_lbl.config(text="🔒 DRM", fg=YELLOW)
            self._log_dl(f"⚠ DRM detected — CDM path required in Settings")
        else:
            self._drm_lbl.config(text="🔓 No DRM", fg=GREEN)

        # ── Auto-populate quality dropdown ──────────────────────────────────
        qlist = info.get("quality_list") or ["144p", "240p", "360p", "480p", "576p", "720p", "1080p", "Best"]
        self._quality_combo.config(values=qlist)
        if "Best" in qlist:
            self._quality_var.set("Best")
        else:
            self._quality_var.set(qlist[-1])

        # ── Populate audio track dropdown ────────────────────────────────────
        audio_tracks = info.get("audio_tracks", [])
        audio_opts = ["All"]
        for t in audio_tracks:
            _lname = LANG_NAMES.get(t["lang"], t["lang"].upper())
            audio_opts.append(f"{_lname} ({t['kbps']}kbps {t['channels']}CH)")
        self._audio_combo.config(values=audio_opts)
        self._audio_var.set("All")

        # ── Build fancy auto filename ────────────────────────────────────────
        # Format: Title Langs TRUE WEB-DL - Quality - AVC - (AAC - NNNKbps)
        _safe_title = re.sub(r'[<>:"/\\|?*]', "_", title)
        if audio_tracks:
            # Unique lang names in order of appearance.
            # Prefer API metadata language over MPD lang codes — SunNXT CDN
            # frequently mis-tags Telugu/Hindi films as 'ta' in the MPD.
            _api_lang = info.get("api_languages", "")
            if _api_lang:
                # Use API language string directly (e.g. "Telugu", "Tamil,Telugu")
                _langs_str = " + ".join(
                    p.strip().title() for p in _api_lang.replace(";", ",").split(",") if p.strip()
                )
            else:
                _seen = []
                for t in audio_tracks:
                    _ln = LANG_NAMES.get(t["lang"], t["lang"].upper())
                    if _ln not in _seen:
                        _seen.append(_ln)
                _langs_str = " + ".join(_seen)
            _max_kbps = max(t["kbps"] for t in audio_tracks)
            _codec = audio_tracks[0]["codec"]
            _fancy_base = f"{_safe_title} {_langs_str} TRUE WEB-DL - {{quality}} - AVC - ({_codec} - {_max_kbps}Kbps)"
        else:
            _fancy_base = f"{_safe_title} TRUE WEB-DL - {{quality}} - AVC"
        # Store the template for use at download time
        info["_fancy_base"] = _fancy_base
        # Set filename field with quality placeholder resolved to current selection
        _q = self._quality_var.get()
        self._outname_var.set(_fancy_base.replace("{quality}", _q))

        # ── Show stream info card ────────────────────────────────────────────
        # Wire up the dedicated URL label rows with full URLs stored for copy
        _mpd = info.get("mpd_url") or info.get("m3u8_url") or ""
        _lic = info.get("license_url") or ""
        self._mpd_url_full = _mpd
        self._lic_url_full = _lic
        self._mpd_url_lbl.config(text=(_mpd[:20] + "…") if len(_mpd) > 20 else (_mpd or "—"))
        self._lic_url_lbl.config(text=(_lic[:20] + "…") if len(_lic) > 20 else (_lic or "—"))

        lines = []
        lines.append(f"Qualities: {', '.join(qlist)}")
        if audio_tracks:
            for t in audio_tracks:
                _ln = LANG_NAMES.get(t["lang"], t["lang"].upper())
                lines.append(f"Audio: {_ln} {t['kbps']}kbps {t['channels']}CH {t['codec']}")
        subtitle_tracks = info.get("subtitle_tracks", [])
        if subtitle_tracks:
            for st in subtitle_tracks:
                lines.append(f"Subtitle: {st['language']} (VTT)")
        lines.append("")
        lines.append("⚠ SunNXT watermark is embedded")
        lines.append("  server-side in all streams.")

        self._stream_info_text.config(state="normal")
        self._stream_info_text.delete("1.0", "end")
        self._stream_info_text.insert("end", "\n".join(lines))
        self._stream_info_text.config(state="disabled")

        # Update filename when quality changes
        def _on_quality_change(*_):
            _tmpl = self._stream_info.get("_fancy_base", "")
            if _tmpl and "{quality}" in _tmpl:
                self._outname_var.set(_tmpl.replace("{quality}", self._quality_var.get()))
        self._quality_combo.bind("<<ComboboxSelected>>", _on_quality_change)

        self._dl_btn.config(state="normal")
        self._log_dl(f"✓ Stream fetched: {title}")

    def _do_download(self):
        info = self._stream_info
        stream_url = info.get("mpd_url") or info.get("m3u8_url")
        if not stream_url:
            self._log_dl("No stream URL, fetch first")
            return

        out_dir = self._outdir_var.get().strip()
        quality  = self._quality_var.get()
        cfg      = self._build_cfg()

        # ── Resolve fancy filename (replace {quality} template) ──────────────
        _tmpl = info.get("_fancy_base", "")
        if _tmpl and "{quality}" in _tmpl:
            # Strip size suffix if user hasn't edited yet (it gets appended after)
            out_name = _tmpl.replace("{quality}", quality)
        else:
            out_name = self._outname_var.get().strip() or info.get("title", "output")
        # Remove any trailing " - X.X GB" so safe_name stays clean for N_m3u8DL-RE
        out_name = re.sub(r'\s*-\s*[\d.]+\s*(GB|MB)\s*$', '', out_name).strip()

        # ── Build audio filter from combo selection ───────────────────────────
        _audio_sel = self._audio_var.get()
        if _audio_sel and _audio_sel != "All":
            # Label format: "Tamil (105kbps 2CH)"  → extract lang code from stored tracks
            _audio_tracks = info.get("audio_tracks", [])
            _audio_filter = "all"
            for _t in _audio_tracks:
                _ln = LANG_NAMES.get(_t["lang"], _t["lang"].upper())
                if _audio_sel.startswith(_ln + " "):
                    # Match by lang code
                    _audio_filter = f"lang={_t['lang']}"
                    break
        else:
            _audio_filter = "all"

        self._cancel_flag = [False]
        self._dl_start_time = time.time()
        self._last_saved_path = ""
        self._dl_btn.config(state="disabled")
        self._cancel_btn.config(state="normal")
        self._open_folder_btn.config(state="disabled")
        self._speed_lbl.config(text="")
        self._progress["value"] = 0
        self._progress.config(mode="indeterminate")
        self._progress.start(12)
        self._dl_status.config(text="Starting…", fg=FG)
        self._log_dl(f"▶ Starting download: {out_name}")
        self._log_dl(f"  URL: {stream_url[:80]}{'…' if len(stream_url) > 80 else ''}")
        self._log_dl(f"  Quality: {quality}  Engine: {cfg.get('engine','auto')}")

        def do():
            try:
                drm_keys_local = []
                # ── DRM key resolution ─────────────────────────────────────────
                # Priority 1: manual key pasted by user (from Vineless or similar)
                manual_key_raw = self._manual_key_var.get().strip()
                if manual_key_raw:
                    # Parse "kid:key" or "kid1:key1,kid2:key2" formats
                    _parsed_keys = []
                    for _pair in manual_key_raw.replace(";", ",").split(","):
                        _pair = _pair.strip()
                        if ":" in _pair:
                            _kid, _key = _pair.split(":", 1)
                            _parsed_keys.append((_kid.strip(), _key.strip()))
                    if _parsed_keys:
                        drm_keys_local = _parsed_keys
                        self._log_dl(f"[DRM] Using {len(drm_keys_local)} manual key(s) from Widevine Key field")

                # Priority 2: CDM (.wvd) path — pywidevine auto-extraction
                if not drm_keys_local and info.get("drm") and info.get("mpd_url"):
                    cdm = cfg.get("cdm_path", "")
                    if cdm and os.path.exists(cdm):
                        self._log_dl("Extracting Widevine keys via CDM…")
                        try:
                            req = urllib.request.Request(info["mpd_url"])
                            with urllib.request.urlopen(req, timeout=20) as r:
                                mpd_text = r.read().decode("utf-8", errors="replace")
                            pssh = extract_pssh(mpd_text)
                            if pssh and info.get("license_url"):
                                drm_keys_local = get_widevine_keys(
                                    pssh, info["license_url"], cdm, self._log_dl
                                )
                                if drm_keys_local:
                                    self._log_dl(f"✓ Got {len(drm_keys_local)} key(s)")
                                else:
                                    self._log_dl("✗ No keys extracted — aborting (encrypted file would be unplayable)")
                                    self._log_dl("TIP: Use Vineless in browser → copy kid:key → paste in Widevine Key field above")
                                    self.after(0, lambda: self._on_download_done(False, "", out_name, quality, 0.0))
                                    return
                            else:
                                self._log_dl("⚠ No PSSH or license URL — cannot decrypt, aborting")
                                self.after(0, lambda: self._on_download_done(False, "", out_name, quality, 0.0))
                                return
                        except Exception as e:
                            self._log_dl(f"✗ DRM extraction error: {e} — aborting")
                            self.after(0, lambda: self._on_download_done(False, "", out_name, quality, 0.0))
                            return
                    else:
                        self._log_dl("✗ DRM content — no CDM path set in Settings AND no manual key provided")
                        self._log_dl("  → Option A: Set CDM (.wvd) path in Settings tab")
                        self._log_dl("  → Option B: Use Vineless in browser, copy kid:key, paste in Widevine Key field above")
                        self.after(0, lambda: self._on_download_done(False, "", out_name, quality, 0.0))
                        return

                def _speed_cb(spd, eta):
                    txt = "  ".join(x for x in [spd, f"ETA {eta}" if eta else ""] if x)
                    self.after(0, lambda t=txt: self._speed_lbl.config(text=t))

                def _status_cb(s):
                    self.after(0, lambda t=s: self._dl_status.config(text=t, fg=FG))

                ok, path = run_download(
                    stream_url, out_dir, out_name, quality, cfg,
                    progress_cb=lambda p: self.after(0, lambda v=p: self._set_progress(v)),
                    log_cb=self._log_dl,
                    cancel_flag=self._cancel_flag,
                    drm_keys=drm_keys_local,
                    speed_cb=_speed_cb,
                    status_cb=_status_cb,
                    audio_filter=_audio_filter,
                )
                # Tag MKV audio tracks with kbps labels for VLC
                if ok and path and path.endswith(".mkv"):
                    _audio_tracks_info = info.get("audio_tracks", [])
                    if _audio_tracks_info:
                        try:
                            import shutil as _shutil
                            _mkvpropedit = _shutil.which("mkvpropedit") or _shutil.which("mkvpropedit.exe")
                            if not _mkvpropedit:
                                # Try same dir as mkvmerge
                                _mkvmerge = cfg.get("mkvmerge_path") or _shutil.which("mkvmerge") or _shutil.which("mkvmerge.exe")
                                if _mkvmerge:
                                    _mkvpropedit = os.path.join(os.path.dirname(_mkvmerge), "mkvpropedit") or \
                                                   os.path.join(os.path.dirname(_mkvmerge), "mkvpropedit.exe")
                                    if not os.path.exists(_mkvpropedit):
                                        _mkvpropedit = None
                            if _mkvpropedit:
                                _prop_cmd = [_mkvpropedit, path]
                                for _ti, _t in enumerate(_audio_tracks_info, start=1):
                                    _ln = LANG_NAMES.get(_t["lang"], _t["lang"].upper())
                                    _track_name = f"{_ln} AAC {_t['kbps']}kbps {_t['channels']}CH"
                                    _prop_cmd += [
                                        "--edit", f"track:a{_ti}",
                                        "--set", f"name={_track_name}",
                                        "--set", f"language={_t['lang']}",
                                    ]
                                _prop_result = subprocess.run(_prop_cmd, capture_output=True, timeout=30)
                                if _prop_result.returncode == 0:
                                    self._log_dl(f"[mkvpropedit] ✓ Audio track names set")
                                else:
                                    self._log_dl(f"[mkvpropedit] ⚠ {_prop_result.stderr.decode(errors='replace')[:200]}")
                        except Exception as _e_prop:
                            self._log_dl(f"[mkvpropedit] skipped: {_e_prop}")

                # Download and embed external subtitle VTT files into MKV
                if ok and path and path.endswith(".mkv"):
                    _subtitle_tracks_info = info.get("subtitle_tracks", [])
                    if _subtitle_tracks_info:
                        try:
                            import shutil as _shutil2
                            import urllib.request as _ureq2
                            _mkvmerge_sub = (cfg.get("mkvmerge_path") or
                                             _shutil2.which("mkvmerge") or
                                             _shutil2.which("mkvmerge.exe"))
                            if _mkvmerge_sub:
                                _sub_files = []
                                _out_dir_sub = os.path.dirname(path)
                                for _si, _st in enumerate(_subtitle_tracks_info):
                                    _sub_url = _st["url"]
                                    _sub_lang_name = _st["language"]
                                    # ISO 639-2 code for mkvmerge (best effort)
                                    _lang_map = {
                                        "english": "eng", "tamil": "tam", "telugu": "tel",
                                        "malayalam": "mal", "kannada": "kan", "hindi": "hin",
                                        "bengali": "ben", "marathi": "mar",
                                    }
                                    _iso = _lang_map.get(_sub_lang_name.lower(), "und")
                                    _sub_fname = os.path.join(
                                        _out_dir_sub,
                                        f"_sub_{_si}_{_iso}.vtt"
                                    )
                                    self._log_dl(f"[Subtitle] Downloading {_sub_lang_name}: {_sub_url}")
                                    _sub_req = _ureq2.Request(
                                        _sub_url,
                                        headers={"User-Agent": UA}
                                    )
                                    with _ureq2.urlopen(_sub_req, timeout=30) as _sr:
                                        with open(_sub_fname, "wb") as _sf:
                                            _sf.write(_sr.read())
                                    self._log_dl(f"[Subtitle] ✓ Saved {os.path.basename(_sub_fname)}")
                                    _sub_files.append((_sub_fname, _iso, _sub_lang_name))

                                if _sub_files:
                                    # Remux MKV with subtitles embedded
                                    _tmp_path = path + ".sub_tmp.mkv"
                                    _sub_cmd = [_mkvmerge_sub, "-o", _tmp_path, path]
                                    for _sf_path, _sf_iso, _sf_name in _sub_files:
                                        _sub_cmd += [
                                            "--language", f"0:{_sf_iso}",
                                            "--track-name", f"0:{_sf_name}",
                                            _sf_path,
                                        ]
                                    self._log_dl(f"[Subtitle] Embedding into MKV…")
                                    _sub_result = subprocess.run(
                                        _sub_cmd, capture_output=True, timeout=120
                                    )
                                    if _sub_result.returncode == 0 and os.path.isfile(_tmp_path):
                                        os.remove(path)
                                        os.rename(_tmp_path, path)
                                        self._log_dl(f"[Subtitle] ✓ {len(_sub_files)} subtitle(s) embedded")
                                    else:
                                        _err_msg = _sub_result.stderr.decode(errors="replace")[:200]
                                        self._log_dl(f"[Subtitle] ⚠ mkvmerge failed: {_err_msg}")
                                        if os.path.isfile(_tmp_path):
                                            os.remove(_tmp_path)
                                    # Clean up temp VTT files
                                    for _sf_path, _, _ in _sub_files:
                                        try:
                                            os.remove(_sf_path)
                                        except Exception:
                                            pass
                            else:
                                self._log_dl("[Subtitle] ⚠ mkvmerge not found — subtitles not embedded")
                        except Exception as _e_sub:
                            self._log_dl(f"[Subtitle] ⚠ subtitle embed failed: {_e_sub}")

                # Measure output file size and rename with size suffix
                _size_mb = 0.0
                _final_path = path
                if ok and path and os.path.isfile(path):
                    try:
                        _bytes = os.path.getsize(path)
                        _size_mb = _bytes / (1024 * 1024)
                        # Build size label: use GB if ≥ 1000 MB
                        if _size_mb >= 1000:
                            _sz_str = f"{_size_mb / 1024:.1f} GB"
                        else:
                            _sz_str = f"{_size_mb:.0f} MB"
                        # Rename to add " - X.X GB" suffix before extension
                        _ext = os.path.splitext(path)[1]
                        _base_noext = os.path.splitext(path)[0]
                        # Strip any stale size suffix then add fresh one
                        _base_noext = re.sub(r'\s*-\s*[\d.]+\s*(GB|MB)\s*$', '', _base_noext).strip()
                        _new_path = f"{_base_noext} - {_sz_str}{_ext}"
                        if _new_path != path:
                            # Remove dest if exists (e.g. re-download)
                            if os.path.exists(_new_path):
                                os.remove(_new_path)
                            os.rename(path, _new_path)
                            _final_path = _new_path
                            self._log_dl(f"[Rename] → {os.path.basename(_new_path)}")
                    except Exception as _e_sz:
                        self._log_dl(f"[Rename] ⚠ size rename failed: {_e_sz}")
                self.after(0, lambda: self._on_download_done(ok, _final_path, out_name, quality, _size_mb))
            except Exception as ex:
                tb = traceback.format_exc()
                self._log_dl(f"[CRASH] Download thread crashed: {ex}")
                self._log_dl(tb)
                self.after(0, lambda: self._on_download_done(False, "", out_name, quality, 0.0))

        self._dl_thread = threading.Thread(target=do, daemon=True)
        self._dl_thread.start()

    def _do_cancel(self):
        self._cancel_flag[0] = True
        self._dl_status.config(text="Cancelling…", fg=WARN)

    def _set_progress(self, val: float):
        # Switch from indeterminate to determinate on first real progress value
        if self._progress.cget("mode") == "indeterminate":
            self._progress.stop()
            self._progress.config(mode="determinate")
        # Only advance — never let audio 100% drag the bar back to 100
        # if the video hasn't caught up yet. Use monotonic advance.
        current = self._progress["value"]
        new_val = min(val, 99.9)   # cap at 99.9 — 100 is set only by _on_download_done
        if new_val > current:
            self._progress["value"] = new_val
            self._dl_status.config(text=f"{new_val:.1f}%", fg=ACCENT)

    def _on_download_done(self, ok: bool, path: str, title: str = "", quality: str = "", size_mb: float = 0.0):
        self._dl_btn.config(state="normal")
        self._cancel_btn.config(state="disabled")
        # Compute elapsed time
        _elapsed = time.time() - getattr(self, "_dl_start_time", time.time())
        _mins = int(_elapsed // 60)
        _secs = int(_elapsed % 60)
        _elapsed_str = f"{_mins}m {_secs:02d}s" if _mins else f"{_secs}s"
        self._speed_lbl.config(text=f"Took {_elapsed_str}")
        # Stop indeterminate mode
        self._progress.stop()
        self._progress.config(mode="determinate")
        # Always enable Open Folder — even on failure files land in outdir
        _outdir = self._outdir_var.get().strip()
        self._last_saved_path = path if (path and os.path.exists(path)) else _outdir
        self._open_folder_btn.config(state="normal")
        if ok:
            self._progress["value"] = 100
            self._dl_status.config(text=f"✓ Done  ({_elapsed_str})", fg=GREEN)
            self._log_dl(f"✓ Saved: {path}")
            self._log_dl(f"⏱ Total time: {_elapsed_str}")
            # Save to history
            if title:
                add_history_entry(title, quality, path, size_mb)
                self._refresh_history()
            # Toast notification
            fname = os.path.basename(path) if path else title
            sz_txt = f"  {size_mb:.1f} MB" if size_mb > 0 else ""
            self._show_toast(f"✓  {fname}{sz_txt}  ({_elapsed_str})", GREEN)
        else:
            self._progress["value"] = 0
            self._dl_status.config(text="✗ Failed", fg=RED)
            self._log_dl("✗ Download failed — check log for details")
            self._log_dl(f"⏱ Time elapsed before failure: {_elapsed_str}")
            self._show_toast("✗  Download failed", RED)

    def _log_dl(self, msg: str):
        def _do():
            self._dl_log.config(state="normal")
            # Color-code log lines by prefix
            if msg.startswith("✓") or "[Direct URL]" in msg:
                tag = "ok"
            elif msg.startswith("✗") or "error" in msg.lower() or "fail" in msg.lower():
                tag = "err"
            elif msg.startswith("⚠") or "warn" in msg.lower() or "TIP:" in msg:
                tag = "warn"
            elif msg.startswith("[DRM]") or "key" in msg.lower():
                tag = "key"
            else:
                tag = "normal"
            self._dl_log.insert("end", msg + "\n", tag)
            self._dl_log.see("end")
            self._dl_log.config(state="disabled")
        self.after(0, _do)

    def _clear_dl_log(self):
        self._dl_log.config(state="normal")
        self._dl_log.delete("1.0", "end")
        self._dl_log.config(state="disabled")

    def _copy_dl_log(self):
        text = self._dl_log.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._show_toast("📋  Log copied to clipboard", ACCENT)

    def _do_open_folder(self):
        """Open the output folder in Explorer / Finder / Nautilus."""
        folder = self._outdir_var.get().strip()
        if not folder:
            return
        try:
            if sys.platform == "win32":
                os.startfile(os.path.normpath(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self._log_dl(f"[open folder] {e}")

    def _show_toast(self, msg: str, color: str = GREEN):
        """Show a brief bottom-right toast notification that fades out."""
        try:
            t = tk.Toplevel(self)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.attributes("-alpha", 0.92)
            # Position bottom-right of main window
            self.update_idletasks()
            wx = self.winfo_x() + self.winfo_width()
            wy = self.winfo_y() + self.winfo_height()
            t.geometry(f"+{wx - 360}+{wy - 80}")
            fr = tk.Frame(t, bg=BG3, highlightthickness=1, highlightbackground=color)
            fr.pack(fill="both", expand=True)
            tk.Label(fr, text=msg, font=FONT_MAIN, bg=BG3, fg=color,
                     padx=18, pady=12).pack()
            # Auto-destroy after 3.5 s
            t.after(3500, t.destroy)
        except Exception:
            pass  # toast is purely cosmetic, never crash on it

    def _refresh_history(self):
        """Reload history list widget if the History tab exists."""
        if not hasattr(self, "_hist_list"):
            return
        h = load_history()
        self._hist_list.config(state="normal")
        self._hist_list.delete("1.0", "end")
        for entry in reversed(h):
            sz = f"  {entry['size_mb']} MB" if entry.get("size_mb") else ""
            line = (f"[{entry.get('ts','')}]  {entry.get('title','?')}"
                    f"  [{entry.get('quality','')}]{sz}\n"
                    f"  {entry.get('path','')}\n\n")
            self._hist_list.insert("end", line)
        self._hist_list.config(state="disabled")

    # ── HISTORY TAB ───────────────────────────────────────────────────────────
    def _build_history_tab(self):
        f = self._tab_history
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        hdr = tk.Frame(f, bg=BG)
        hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        tk.Label(hdr, text="Download History", font=FONT_BOLD,
                 bg=BG, fg=ACCENT).pack(side="left")
        ttk.Button(hdr, text="Clear History",
                   command=self._clear_history).pack(side="right")

        wrap = tk.Frame(f, bg=BG4, highlightthickness=1, highlightbackground=BORDER)
        wrap.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))

        self._hist_list = scrolledtext.ScrolledText(
            wrap, font=FONT_MONO, bg=BG4, fg=FG, insertbackground=FG,
            state="disabled", relief="flat", selectbackground=BORDER
        )
        self._hist_list.pack(fill="both", expand=True, padx=2, pady=2)
        self._refresh_history()

    def _clear_history(self):
        try:
            HISTORY_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._refresh_history()

    # ── SETTINGS TAB ──────────────────────────────────────────────────────────
    def _build_settings_tab(self):
        f = self._tab_settings
        f.columnconfigure(0, weight=1)

        # ── Engine section ────────────────────────────────────────────────────
        eng_card = tk.Frame(f, bg=BG3, highlightthickness=1, highlightbackground=BORDER)
        eng_card.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        eng_card.columnconfigure(1, weight=1)

        tk.Label(eng_card, text="⚙  Download Engine", font=FONT_BOLD,
                 bg=BG3, fg=ACCENT).grid(row=0, column=0, columnspan=3,
                 sticky="w", padx=14, pady=(10, 6))
        tk.Frame(eng_card, bg=BORDER, height=1).grid(row=1, column=0, columnspan=3,
                  sticky="ew", padx=14, pady=(0, 8))

        tk.Label(eng_card, text="Preferred Engine", font=FONT_SMALL,
                 bg=BG3, fg=FG2).grid(row=2, column=0, sticky="e", padx=(14, 8), pady=6)
        self._engine_var = tk.StringVar(value="auto")
        ttk.Combobox(eng_card, textvariable=self._engine_var,
            values=["auto", "N_m3u8DL-RE", "yt-dlp", "ffmpeg"],
            width=18, state="readonly"
        ).grid(row=2, column=1, sticky="w", pady=6)

        tk.Label(eng_card, text="Threads", font=FONT_SMALL,
                 bg=BG3, fg=FG2).grid(row=3, column=0, sticky="e", padx=(14, 8), pady=6)
        self._threads_var = tk.StringVar(value="4")
        ttk.Spinbox(eng_card, from_=1, to=32, textvariable=self._threads_var,
                    width=6).grid(row=3, column=1, sticky="w", pady=6, padx=(0, 0))

        # ── Tool paths section ────────────────────────────────────────────────
        tk.Label(eng_card, text="Tool Paths", font=FONT_BOLD,
                 bg=BG3, fg=ACCENT).grid(row=4, column=0, columnspan=3,
                 sticky="w", padx=14, pady=(12, 4))
        tk.Frame(eng_card, bg=BORDER, height=1).grid(row=5, column=0, columnspan=3,
                  sticky="ew", padx=14, pady=(0, 6))

        paths = [
            ("N_m3u8DL-RE", "n_m3u8dl_path"),
            ("yt-dlp",      "ytdlp_path"),
            ("ffmpeg",      "ffmpeg_path"),
            ("mkvmerge",    "mkvmerge_path"),   # for MKV muxing
            ("CDM (.wvd)",  "cdm_path"),
        ]
        self._path_vars = {}
        for i, (label, key) in enumerate(paths):
            r = 6 + i
            tk.Label(eng_card, text=label, font=FONT_SMALL,
                     bg=BG3, fg=FG2).grid(row=r, column=0, sticky="e",
                     padx=(14, 8), pady=5)
            var = tk.StringVar()
            self._path_vars[key] = var
            ent = tk.Entry(eng_card, textvariable=var, font=FONT_MAIN,
                           bg=BG4, fg=FG, insertbackground=FG, relief="flat", bd=5)
            ent.grid(row=r, column=1, sticky="ew", pady=5)
            ttk.Button(eng_card, text="…", width=3,
                command=lambda k=key: self._browse_tool(k)
                ).grid(row=r, column=2, padx=(4, 14), pady=5)

        # status indicator column (all except CDM)
        self._tool_status_labels = {}
        for i, (label, key) in enumerate(paths[:-1]):   # skip CDM indicator
            r = 6 + i
            lbl = tk.Label(eng_card, text="?", font=FONT_SMALL, bg=BG3, fg=FG2, width=2)
            lbl.grid(row=r, column=3, padx=(0, 8))
            self._tool_status_labels[key] = lbl

        # ── Action buttons ────────────────────────────────────────────────────
        btn_frame = tk.Frame(f, bg=BG)
        btn_frame.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 8))
        ttk.Button(btn_frame, text="  Save Settings  ", style="Accent.TButton",
                   command=self._save_cfg).pack(side="left", padx=(0, 10))
        ttk.Button(btn_frame, text="🔍 Auto-Detect Tools",
                   command=self._auto_detect).pack(side="left")

        self._settings_log = tk.Label(f, text="", bg=BG, fg=GREEN, font=FONT_SMALL)
        self._settings_log.grid(row=2, column=0, sticky="w", padx=20)

        f.columnconfigure(0, weight=1)

    def _browse_tool(self, key: str):
        p = filedialog.askopenfilename()
        if p:
            self._path_vars[key].set(p)

    def _auto_detect(self):
        found = []
        for tool, key in [
            ("N_m3u8DL-RE",    "n_m3u8dl_path"),
            ("N_m3u8DL-RE.exe","n_m3u8dl_path"),
            ("yt-dlp",         "ytdlp_path"),
            ("yt-dlp.exe",     "ytdlp_path"),
            ("ffmpeg",         "ffmpeg_path"),
            ("ffmpeg.exe",     "ffmpeg_path"),
            ("mkvmerge",       "mkvmerge_path"),
            ("mkvmerge.exe",   "mkvmerge_path"),
        ]:
            p = shutil.which(tool)
            if p and not self._path_vars.get(key, tk.StringVar()).get():
                if key in self._path_vars:
                    self._path_vars[key].set(p)
                    found.append(f"{tool}")
        self._refresh_tool_status()
        if found:
            self._settings_log.config(text=f"✓ Found: {', '.join(found)}", fg=GREEN)
        else:
            self._settings_log.config(text="No tools found in PATH — set paths manually above", fg=YELLOW)

    def _refresh_tool_status(self):
        """Update ✓/✗ labels next to tool paths."""
        indicator_map = {
            "n_m3u8dl_path":  ("N_m3u8DL-RE",  "N_m3u8DL-RE.exe"),
            "ytdlp_path":     ("yt-dlp",         "yt-dlp.exe"),
            "ffmpeg_path":    ("ffmpeg",          "ffmpeg.exe"),
            "mkvmerge_path":  ("mkvmerge",        "mkvmerge.exe"),
        }
        for key, names in indicator_map.items():
            lbl = self._tool_status_labels.get(key)
            if not lbl:
                continue
            p = self._path_vars.get(key)
            p = p.get().strip() if p else ""
            if p and os.path.exists(p):
                lbl.config(text="✓", fg=GREEN)
            elif any(shutil.which(n) for n in names):
                lbl.config(text="✓ PATH", fg=GREEN)
            else:
                lbl.config(text="✗", fg=RED)

    def _build_cfg(self) -> dict:
        cfg = {"engine": self._engine_var.get(),
               "threads": int(self._threads_var.get() or 4)}
        for key, var in self._path_vars.items():
            cfg[key] = var.get().strip()
        return cfg

    # ── CONFIG PERSISTENCE ────────────────────────────────────────────────────
    def _cfg_path(self) -> Path:
        return Path.home() / ".sunnxt_downloader.json"

    def _load_cfg(self) -> dict:
        try:
            if self._cfg_path().exists():
                return json.loads(self._cfg_path().read_text())
        except Exception:
            pass
        return {}

    def _save_cfg(self):
        cfg = self._build_cfg()
        try:
            self._cfg_path().write_text(json.dumps(cfg, indent=2))
            self._settings_log.config(text="✓ Settings saved", fg=GREEN)
            self._refresh_tool_status()
        except Exception as e:
            self._settings_log.config(text=f"✗ {e}", fg=RED)

    def _restore_cfg(self):
        cfg = self._cfg
        if "engine" in cfg:
            self._engine_var.set(cfg["engine"])
        if "threads" in cfg:
            self._threads_var.set(str(cfg["threads"]))
        for key, var in self._path_vars.items():
            if key in cfg and cfg[key]:
                var.set(cfg[key])

# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = SunnXTDownloader()
    app.mainloop()


if __name__ == "__main__":
    main()
