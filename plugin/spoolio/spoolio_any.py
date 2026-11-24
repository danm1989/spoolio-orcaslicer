# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Spoolio"
# description = "A Bambu Lab inspired inventory management overview for OrcaSlicer"
# author = "Dan J Moore"
# version = "0.13.0"
# ///

# Spoolio - filament management for OrcaSlicer
# Copyright (c) 2026 Dan J Moore
# Licensed under the PolyForm Shield License 1.0.0, see LICENSE.

import base64
import ftplib
import html
import http.client
import json
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import orca
import orca.pages

_SLICING = hasattr(getattr(orca, "slicing", None), "SlicingPipelineCapabilityBase")

PLUGIN_NAME = "Spoolio"
PLUGIN_VERSION = "0.13.0"
# Stamped per operating system by scripts/build.py.
BUILD_TARGET = "any"

DEFAULT_SPOOLMAN_URL = "http://raspberrypi:7912"
LOW_DEFAULT = 100  # grams

FEEDBACK_URL = "https://github.com/danm1989/spoolio-orcaslicer/issues"
RELEASE_API = "https://api.github.com/repos/danm1989/spoolio-orcaslicer/releases/latest"

# webbrowser can only open a URL; it can't use the browser's default search engine.
SEARCH_URL = "https://www.google.com/search?q={query}"

SETTINGS_FILENAME = "spoolio_settings.json"
LOG_FILENAME = "spoolio.log"
LOG_MAX = 256 * 1024

TIMEOUT = 5  # seconds
MAX_QUERY = 200
REFRESH_SECS = 60
DEFAULT_MARGIN = 10  # percent
TAIL_BYTES = 192 * 1024
CACHE_SECS = 120
REPEAT_SECS = 120
COLOUR_TOLERANCE = 40
NOTICE_MAX = 400
NOTICE_DELAY = 0.4  # seconds, so the slicing thread has returned before any UI call
FONT_STACK = (
    'Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "Helvetica Neue", '
    'Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif'
)

PLUGIN_DIR = Path(__file__).resolve().parent


def _config_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _data_dir() -> Path:
    """Settings, log and tab-icon folder, inside OrcaSlicer's data folder.

    Not the plugin folder: updates would reset it, and OrcaSlicer expects that folder to
    hold only the plugin file.
    """
    # Plugins live in <data folder>/orca_plugins, also for portable installs and --datadir.
    for parent in (PLUGIN_DIR, *PLUGIN_DIR.parents):
        if parent.name == "orca_plugins":
            return parent.parent / "spoolio"
    for parent in PLUGIN_DIR.parents:
        if parent.name == "OrcaSlicer":
            return parent / "spoolio"
    return _config_root() / "OrcaSlicer" / "spoolio"


def _copy_old(new: Path) -> None:
    old = _config_root() / "OrcaSlicer" / "spoolio"
    try:
        if new != old and not new.exists() and (old / SETTINGS_FILENAME).is_file():
            shutil.copytree(old, new)
    except OSError:
        pass


DATA_DIR = _data_dir()
_copy_old(DATA_DIR)
SETTINGS_FILE = DATA_DIR / SETTINGS_FILENAME
LOG_FILE = DATA_DIR / LOG_FILENAME
# Where earlier builds kept settings when OrcaSlicer's data folder wasn't found.
OLD_SETTINGS = PLUGIN_DIR / SETTINGS_FILENAME

log = logging.getLogger("spoolio")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(logging.NullHandler())


def setup_log(path: Path | None = None) -> None:
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    path = path or LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX, backupCount=1, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


# Started at import so anything that goes wrong while the plugin loads is captured.
setup_log()
log.info("%s %s (%s build) imported", PLUGIN_NAME, PLUGIN_VERSION, BUILD_TARGET)


_reported = {}
_spool_cache = {"url": "", "time": 0.0, "spools": []}


def _report(key: str, error: str | None, exc_info: bool = False) -> None:
    """Log a failure once, then its recovery, so the periodic refresh can't flood the log."""
    if error is None:
        if _reported.pop(key, None) is not None:
            log.info("%s recovered", key)
    elif _reported.get(key) != error:
        _reported[key] = error
        log.warning("%s failed: %s", key, error, exc_info=exc_info)


def _bg(fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("Background task %s failed", getattr(fn, "__name__", "?"))
    threading.Thread(target=run, daemon=True).start()


_cfg_lock = threading.RLock()


def _write(path: Path, text: str, private: bool = False) -> None:
    # Written beside the file, then swapped in, so a crash can't leave it half-written.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if private:
        os.chmod(tmp, 0o600)
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.1)


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        bad = path.with_name(path.name + ".bad")
        log.warning("%s is unreadable, moved to %s", path, bad.name)
        try:
            os.replace(path, bad)
        except OSError:
            pass
        raise


def cfg() -> dict:
    defaults = {
        "spoolman_url": "",
        "spool_source": "spoolman",
        "low_stock_grams": LOW_DEFAULT,
        "plate_check": True,
        "plate_margin_percent": DEFAULT_MARGIN,
        "weight_display": "both",
        "show_cart": False,
        "spool_columns": 1,
        "progress": {},
        "stats": {},
    }
    for path in (SETTINGS_FILE, OLD_SETTINGS):
        try:
            out = {**defaults, **_read(path)}
        except (OSError, ValueError):
            continue
        # No trailing slash, so cache lookups match.
        out["spoolman_url"] = clean_url(out["spoolman_url"]) if isinstance(out["spoolman_url"], str) else ""
        return out
    return defaults


def parse_low(value: object) -> float:
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return LOW_DEFAULT
    if grams < 0:
        return LOW_DEFAULT
    return int(grams) if grams == int(grams) else grams


def parse_margin(value: object) -> int:
    try:
        return min(50, max(0, round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MARGIN


def parse_flag(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def parse_weights(value: object) -> str:
    return value if value in ("grams", "percent", "both") else "both"


def weight_mode() -> str:
    return parse_weights(cfg().get("weight_display"))


def cart_on() -> bool:
    return parse_flag(cfg().get("show_cart"), default=False)


def parse_columns(value: object) -> int:
    return 2 if value in (2, "2") else 1


def spool_columns() -> int:
    return parse_columns(cfg().get("spool_columns"))


def parse_progress(value: object) -> dict:
    v = value if isinstance(value, dict) else {}
    return {"on": parse_flag(v.get("on")), "finish": parse_flag(v.get("finish")),
            "slot": parse_flag(v.get("slot")), "idle": "hide" if v.get("idle") == "hide" else "line"}


def progress_settings() -> dict:
    return parse_progress(cfg().get("progress"))


CURRENCIES = ("spoolman", "gbp", "eur", "usd")


def parse_stats(value: object) -> dict:
    v = value if isinstance(value, dict) else {}
    return {"on": parse_flag(v.get("on")), "cur": v.get("cur") if v.get("cur") in CURRENCIES else "spoolman"}


def stats_settings() -> dict:
    return parse_stats(cfg().get("stats"))


FONTS = ("def", "sans", "wide", "serif", "mono")
SIZES = (90, 100, 110, 120)


THEMES = {
    "light": {"accent": "#00897b", "accentFg": "#ffffff", "bg": "#f3f4f6", "card": "#ffffff", "border": "#d8dbe0",
              "fg": "#1f2328", "muted": "#626a74", "radius": 14},
    "dark": {"accent": "#009688", "accentFg": "#ffffff", "bg": "#2b2b2e", "card": "#333336", "border": "#45454a",
             "fg": "#e8e8e8", "muted": "#9a9a9e", "radius": 14},
}
SHOW = ("tag", "mat", "hex", "dia", "loc", "rfid", "cart")
LOOK_PICKS = {"theme": ("dark", "light", "custom"), "density": ("comfy", "compact", "spacious"),
              "cols": ("1", "2", "auto"), "swatch": ("circle", "spool", "square", "pill"),
              "units": ("g", "kg"), "wfmt": ("both", "w", "pct")}
WFMT = {"grams": "w", "percent": "pct", "both": "both"}


def parse_look(value: object, legacy: dict | None = None) -> dict:
    """Appearance settings; columns and weight display fall back to the older General settings."""
    v = value if isinstance(value, dict) else {}
    old = legacy or {}
    size = _num_or(v.get("size"), 100)
    size = {115: 110, 130: 120}.get(size, size)
    out = {"font": v.get("font") if v.get("font") in FONTS else "def", "size": size if size in SIZES else 100}
    dflt = {"cols": "2" if old.get("spool_columns") in (2, "2") else "1",
            "wfmt": WFMT.get(old.get("weight_display"), "both")}
    for k, opts in LOOK_PICKS.items():
        x = str(v.get(k)) if v.get(k) is not None else None
        out[k] = x if x in opts else dflt.get(k, opts[0])
    c = v.get("custom") if isinstance(v.get("custom"), dict) else {}
    out["custom"] = {k: (str(c[k]).lower() if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(c.get(k))) else d)
                     for k, d in THEMES["dark"].items() if k != "radius"}
    out["custom"]["radius"] = c.get("radius") if c.get("radius") in (4, 10, 14, 20) else 14
    s = v.get("show") if isinstance(v.get("show"), dict) else {}
    out["show"] = {k: parse_flag(s.get(k), default=k != "loc") for k in SHOW}
    out["motion"] = parse_flag(v.get("motion"), default=False)
    out["cb"] = parse_flag(v.get("cb"), default=False)
    return out


def plate_settings() -> tuple[bool, int]:
    settings = cfg()
    return (parse_flag(settings.get("plate_check")),
            parse_margin(settings.get("plate_margin_percent")))


def ui_cfg() -> dict:
    s = cfg()
    return {
        "source": parse_source(s.get("spool_source")),
        "url": s.get("spoolman_url", ""),
        "low_stock": parse_low(s.get("low_stock_grams")),
        "plate_check": parse_flag(s.get("plate_check")),
        "plate_margin": parse_margin(s.get("plate_margin_percent")),
        "weight_display": parse_weights(s.get("weight_display")),
        "show_cart": parse_flag(s.get("show_cart"), default=False),
        "spool_columns": parse_columns(s.get("spool_columns")),
        "progress": parse_progress(s.get("progress")),
        "stats": parse_stats(s.get("stats")),
        "appearance": parse_look(s.get("appearance"), s),
    }


def save_settings(settings: dict) -> bool:
    try:
        _write(SETTINGS_FILE, json.dumps(settings, indent=2))
        return True
    except OSError:
        log.exception("Could not write %s", SETTINGS_FILE)
        return False


def cfg_edit(fn) -> bool:
    with _cfg_lock:
        s = cfg()
        fn(s)
        return save_settings(s)


def get_spools(spoolman_url: str) -> dict:
    """Return ``{"ok": True, "spools": [...]}`` or ``{"ok": False, "error": "..."}``."""
    url = f"{spoolman_url.rstrip('/')}/api/v1/spool"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        error = f"Could not reach Spoolman at {spoolman_url} ({exc.reason})"
        _report("Spool list", error)
        return {"ok": False, "error": error}
    except Exception as exc:
        _report("Spool list", str(exc), exc_info=True)
        return {"ok": False, "error": str(exc)}
    if not isinstance(data, list) or not all(isinstance(sp, dict) for sp in data):
        error = f"Spoolman at {spoolman_url} sent a spool list Spoolio doesn't understand"
        _report("Spool list", error)
        return {"ok": False, "error": error}
    _report("Spool list", None)
    _spool_cache.update(url=spoolman_url, time=time.monotonic(), spools=data)
    return {"ok": True, "spools": data}


def clean_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def ping(spoolman_url: str) -> dict:
    """Ask Spoolman for its version, which doubles as a connection test.

    Returns ``{"ok": True, "version": "..."}`` or ``{"ok": False, "error": "..."}``.
    """
    if not spoolman_url:
        return {"ok": False, "error": "No Spoolman URL configured yet"}
    url = f"{spoolman_url.rstrip('/')}/api/v1/info"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        error = f"Could not reach Spoolman at {spoolman_url} ({exc.reason})"
        log.warning("Connection test failed: %s", error)
        return {"ok": False, "error": error}
    except Exception as exc:
        log.exception("Connection test failed")
        return {"ok": False, "error": str(exc)}
    version = data.get("version", "unknown")
    log.info("Connected to Spoolman %s at %s", version, spoolman_url)
    return {"ok": True, "version": version}


BAMBU_HOSTS = {"global": "https://api.bambulab.com", "china": "https://api.bambulab.cn"}
BAMBU_REGIONS = {"global": "Global", "china": "China"}
BAMBU_PATH = "/v1/design-user-service/my/filament/v2"
BAMBU_PAGE = 100
BAMBU_PAGES = 50
BAMBU_TIMEOUT = 10
BAMBU_CODE_TIMEOUT = 30
BAMBU_KEY = "bambu"
BAMBU_UA = f"BBL-Slicer/v02.08.01.51 (OrcaSlicer; {PLUGIN_NAME}/{PLUGIN_VERSION})"
AUTH_FILE = DATA_DIR / "bambu_auth.json"
TFA_HOSTS = {"global": "https://bambulab.com", "china": "https://bambulab.cn"}
LOGIN_PATH = "/v1/user-service/user/login"
CODE_PATH = "/v1/user-service/user/sendemail/code"
SIGN_IN = "Open Settings and sign in to Bambu Cloud."
_pending: dict = {}
_ams_logged: list = []


def parse_source(value: object) -> str:
    return value if value in ("spoolman", "bambu") else "spoolman"


def source() -> str:
    return parse_source(cfg().get("spool_source"))


def source_name() -> str:
    return "Bambu Cloud" if source() == "bambu" else "Spoolman"


def spool_key() -> str:
    return BAMBU_KEY if source() == "bambu" else cfg().get("spoolman_url", "")


def configured() -> bool:
    return bool(spool_key())


def parse_region(value: object) -> str:
    return value if value in BAMBU_HOSTS else "global"


def jwt_expiry(token: str) -> float | None:
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(payload["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def account_text(auth: dict) -> str:
    who = f"Signed in as {auth['user']}" if auth.get("user") else "Signed in"
    return f"{who} ({BAMBU_REGIONS.get(auth.get('region'), 'Global')})"


def read_auth() -> dict:
    """Sign-in result: ``{"ok": True, token, ...}`` or ``{"ok": False, "error", ...}``."""
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        token = data["access_token"]
    except FileNotFoundError:
        return {"ok": False, "account": "Signed out", "signin": True, "error": "Signed out"}
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("Could not read %s", AUTH_FILE)
        return {"ok": False, "account": "Signed out", "signin": True,
                "error": "Your saved Bambu sign-in is unreadable"}
    auth = {"token": token, "region": parse_region(data.get("region")),
            "user": str(data.get("account") or "")}
    auth["account"] = account_text(auth)
    expiry = jwt_expiry(token)
    if expiry is not None and expiry < time.time():
        return {**auth, "ok": False, "account": "Sign-in expired", "signin": True,
                "error": "Your Bambu sign-in has expired"}
    return {**auth, "ok": True}


class BambuError(Exception):
    pass


def _bambu_message(code: int, raw: str) -> str:
    try:
        text = str(json.loads(raw).get("message") or "")
    except (ValueError, AttributeError):
        text = ""
    if text and text.lower() != "success":
        return text
    return "Bambu blocked the request (HTTP 403)" if code == 403 else f"Bambu Cloud returned an error (HTTP {code})"


def _bambu_post(region: str, path: str, body: dict, host: str = "",
                timeout: float = BAMBU_TIMEOUT) -> tuple[dict, list[str]]:
    request = urllib.request.Request(
        (host or BAMBU_HOSTS[region]) + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": BAMBU_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw, cookies = resp.read().decode("utf-8", "replace"), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raw = exc.read(2000).decode("utf-8", "replace")
        log.warning("Bambu Cloud %s answered HTTP %s: %s", path, exc.code, raw[:200])
        raise BambuError(_bambu_message(exc.code, raw)) from None
    except urllib.error.URLError as exc:
        raise BambuError(f"Could not reach Bambu Cloud ({exc.reason})") from None
    except TimeoutError:
        log.warning("Bambu Cloud %s timed out", path)
        raise BambuError("Bambu Cloud didn't answer in time") from None
    except (OSError, http.client.HTTPException) as exc:
        log.warning("Bambu Cloud %s failed: %r", path, exc)
        raise BambuError(f"Could not reach Bambu Cloud ({exc})") from None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise BambuError("Bambu Cloud sent a reply Spoolio doesn't understand") from None
    return (data if isinstance(data, dict) else {}), cookies


def _send_code(region: str, account: str) -> None:
    _bambu_post(region, CODE_PATH, {"email": account, "type": "codeLogin"}, timeout=BAMBU_CODE_TIMEOUT)


def _after_login(region: str, account: str, reply: dict, cookies: list[str]) -> dict:
    token = str(reply.get("accessToken") or "")
    for cookie in cookies:
        if not token and cookie.startswith("token="):
            token = cookie[6:].split(";")[0]
    if token:
        _pending.clear()
        _write(AUTH_FILE, json.dumps({"access_token": token, "region": region, "account": account}), private=True)
        _spool_cache.update(url="", time=0.0, spools=[])
        log.info("Signed in to Bambu Cloud (%s)", region)
        return {"ok": True, "step": "done"}
    _pending.update(region=region, account=account)
    if reply.get("loginType") == "verifyCode":
        try:
            _send_code(region, account)
        except BambuError as exc:
            log.warning("Could not ask Bambu Cloud for a code: %s", exc)
            return {"ok": True, "step": "code", "warn": str(exc)}
        return {"ok": True, "step": "code"}
    if reply.get("loginType") == "tfa" or reply.get("tfaKey"):
        _pending["tfa_key"] = str(reply.get("tfaKey") or "")
        return {"ok": True, "step": "tfa"}
    raise BambuError("Bambu Cloud didn't sign you in")


def bambu_sign_in(data: dict) -> dict:
    """One sign-in step: password, then the 2FA code if asked."""
    action = data.get("action")
    try:
        if action == "sign_out":
            AUTH_FILE.unlink(missing_ok=True)
            _pending.clear()
            _spool_cache.update(url="", time=0.0, spools=[])
            return {"ok": True, "step": "out"}
        if action == "password":
            region = parse_region(data.get("region"))
            account = str(data.get("account") or "").strip()
            password = str(data.get("password") or "")
            if not account or not password:
                raise BambuError("Enter your Bambu email and password")
            reply, cookies = _bambu_post(region, LOGIN_PATH, {"account": account, "password": password})
            return _after_login(region, account, reply, cookies)
        if not _pending:
            raise BambuError("Your sign-in timed out. Enter your email and password again")
        region, account = _pending["region"], _pending["account"]
        if action == "resend":
            _send_code(region, account)
            return {"ok": True, "step": "code"}
        code = re.sub(r"\s", "", str(data.get("code") or ""))
        if not code:
            raise BambuError("Enter your Bambu Lab verification code")
        if action == "tfa":
            reply, cookies = _bambu_post(region, "/api/sign-in/tfa",
                                         {"tfaKey": _pending.get("tfa_key", ""), "tfaCode": code},
                                         TFA_HOSTS[region])
        else:
            reply, cookies = _bambu_post(region, LOGIN_PATH, {"account": account, "code": code})
        return _after_login(region, account, reply, cookies)
    except BambuError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        log.exception("Bambu sign-in failed")
        return {"ok": False, "error": "Sign in failed. The Spoolio log has the details"}


def _bambu_page(auth: dict, offset: int) -> dict:
    url = f"{BAMBU_HOSTS[auth['region']]}{BAMBU_PATH}?offset={offset}&limit={BAMBU_PAGE}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {auth['token']}",
        "Accept": "application/json",
        "User-Agent": BAMBU_UA,
    })
    with urllib.request.urlopen(request, timeout=BAMBU_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict) and "hits" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
        raise ValueError("Bambu Cloud sent a reply Spoolio doesn't understand")
    return data


def fetch_bambu(auth: dict) -> list[dict]:
    hits: list[dict] = []
    for _ in range(BAMBU_PAGES):
        data = _bambu_page(auth, len(hits))
        page = [item for item in data["hits"] if isinstance(item, dict)]
        hits += page
        total = data.get("total")
        if not page or not isinstance(total, int) or len(hits) >= total:
            break
    return hits


def _hex6(value: object) -> str:
    text = str(value or "").lstrip("#")
    return text[:6].upper() if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", text) else ""


def _grams(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def map_bambu(item: dict) -> dict | None:
    """A Bambu Cloud spool in Spoolman's shape."""
    remaining = _grams(item.get("netWeight"))
    if remaining is None:
        return None
    total = _grams(item.get("totalNetWeight"))
    colour = item.get("color")
    if not colour and isinstance(item.get("colors"), list) and item["colors"]:
        colour = item["colors"][0]
    colour = _hex6(colour)
    material = str(item.get("filamentType") or "").strip()
    name = str(item.get("filamentName") or "").strip() or material
    rfid = str(item.get("RFID") or "").strip()
    where = ""
    if item.get("inPrinter"):
        where = " ".join(part for part in (
            str(item.get("deviceName") or "").strip(),
            f"AMS {item['amsId']}" if item.get("amsId") not in (None, "") else "",
            f"slot {item['slotId']}" if item.get("slotId") not in (None, "") else "",
        ) if part)
    spool = {
        "id": item.get("id"),
        "remaining_weight": remaining,
        "archived": str(item.get("status") or "").lower() == "archived",
        "location": where or None,
        "tags": [rfid] if rfid.strip("0") else [],
        "filament": {
            "id": f"{item.get('filamentId') or name}|{colour}",
            "name": name,
            "material": material,
            "color_hex": colour,
            "diameter": 1.75,
            "vendor": {"name": str(item.get("filamentVendor") or "").strip()},
        },
    }
    if item.get("inPrinter") and item.get("slotId") not in (None, ""):
        spool["ams"] = {"printer": str(item.get("deviceName") or "").strip(),
                        "unit": item.get("amsId"), "slot": item.get("slotId")}
    if total is not None and total > 0:
        spool["initial_weight"] = total
        spool["used_weight"] = max(0.0, total - remaining)
        spool["filament"]["weight"] = total
    return spool


def bambu_fetch() -> dict:
    """``{"ok": True, "auth", "spools"}`` or ``{"ok": False, "error", "account"}``."""
    auth = read_auth()
    if not auth["ok"]:
        return auth
    try:
        hits = fetch_bambu(auth)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200).decode("utf-8", "replace")
        except Exception:
            pass
        log.warning("Bambu Cloud answered HTTP %s: %s", exc.code, body)
        if exc.code == 401:
            return {"ok": False, "error": "Bambu Cloud didn't accept your sign-in",
                    "account": auth["account"], "signin": True}
        return {"ok": False, "error": f"Bambu Cloud returned an error (HTTP {exc.code})",
                "account": auth["account"]}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"Could not reach Bambu Cloud ({exc.reason})",
                "account": auth["account"]}
    except Exception as exc:
        log.exception("Bambu Cloud request failed")
        return {"ok": False, "error": str(exc), "account": auth["account"]}
    spools = [spool for spool in map(map_bambu, hits) if spool]
    seen = sorted({(sp["ams"]["printer"], str(sp["ams"]["unit"]), str(sp["ams"]["slot"]))
                   for sp in spools if "ams" in sp})
    if seen != _ams_logged:
        _ams_logged[:] = seen
        log.info("Spools in printers (printer, ams, slot): %s", seen)
    if len(spools) != len(hits):
        log.info("Skipped %d Bambu Cloud spools with no remaining weight", len(hits) - len(spools))
    if hits and not spools:
        log.warning("No Bambu Cloud spool had a netWeight (keys: %s)", ", ".join(sorted(hits[0])))
    return {"ok": True, "auth": auth, "spools": spools}


def get_bambu_spools() -> dict:
    result = bambu_fetch()
    if not result["ok"]:
        error = f"{result['error']}. {SIGN_IN}" if result.get("signin") else result["error"]
        _report("Spool list", error)
        return {"ok": False, "error": error}
    _report("Spool list", None)
    _spool_cache.update(url=BAMBU_KEY, time=time.monotonic(), spools=result["spools"])
    return {"ok": True, "spools": result["spools"]}


def bambu_ping() -> dict:
    """Bambu Cloud connection test result."""
    result = bambu_fetch()
    if not result["ok"]:
        log.warning("Bambu Cloud test failed: %s", result["error"])
        return {"ok": False, "error": result["error"], "account": result.get("account", ""),
                "signin": bool(result.get("signin"))}
    log.info("Connected to Bambu Cloud, %d spools", len(result["spools"]))
    return {"ok": True, "count": len(result["spools"]), "account": result["auth"]["account"]}


def load_spools() -> dict:
    result = get_bambu_spools() if source() == "bambu" else get_spools(cfg().get("spoolman_url", ""))
    for sp in result.get("spools") or []:
        sp["rfid_tags"] = sorted(spool_tags(sp))
    return result

PRINTER_SLOTS = {"ams": 4, "ht": 1, "ext": 1}
KLIPPER_SLOTS = {"mmu": 16, "tool": 8}
NET = ("klipper", "prusalink", "octoprint")


def parse_printers(value: object) -> list[dict]:
    """Cleaned printers; a spool sits in at most one slot."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    printers = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        kl = item.get("type") in NET
        kinds = KLIPPER_SLOTS if kl else PRINTER_SLOTS
        units = []
        for unit in (item.get("units") if isinstance(item.get("units"), list) else [])[:8]:
            kind = unit.get("kind") if isinstance(unit, dict) else None
            if kind not in kinds or (kind in ("ext", "mmu", "tool") and any(u["kind"] == kind for u in units)):
                continue
            raw = unit.get("slots") if isinstance(unit.get("slots"), list) else []
            size = max(1, min(kinds[kind], len(raw))) if kl else kinds[kind]
            slots: list[str | None] = []
            for index in range(size):
                spool = str(raw[index])[:64] if index < len(raw) and raw[index] not in (None, "") else None
                if spool in seen:
                    spool = None
                if spool:
                    seen.add(spool)
                slots.append(spool)
            units.append({"kind": kind, "slots": slots})
        name = str(item.get("name") or "").strip()[:40] or "Printer"
        ip = str(item.get("ip") or "").strip()[:64]
        serial = re.sub(r"[^A-Za-z0-9]", "", str(item.get("serial") or ""))[:32]
        host = r"(https?://)?[A-Za-z0-9.\-]+(:\d{1,5})?" if kl else r"[A-Za-z0-9.\-]+"
        printers.append({"id": str(item.get("id") or f"p{len(printers) + 1}")[:32], "name": name, "units": units,
                         "ip": ip if re.fullmatch(host, ip) else "", "serial": "" if kl else serial,
                         "type": item["type"] if kl else "bambu",
                         "user": str(item.get("user") or "").strip()[:32] if item.get("type") == "prusalink" else "",
                         "orca": str(item.get("orca") or "").strip()[:120] if kl else ""})
    return printers


def printers_cfg() -> list[dict]:
    saved = cfg().get("printers")
    return parse_printers(saved.get(source())) if isinstance(saved, dict) else []


def save_printers(value: object) -> None:
    def put(s):
        saved = s.get("printers") if isinstance(s.get("printers"), dict) else {}
        saved[source()] = parse_printers(value)
        s["printers"] = saved
    cfg_edit(put)
    prune_codes()
    _local_sync()


def printer_state() -> dict:
    codes = read_codes()
    printers = [dict(p, has_code=bool(codes.get(_code_key(p["id"])))) for p in printers_cfg()]
    live = any(p["ip"] and (p["has_code"] or p["type"] == "klipper") for p in printers)
    return {"printers": printers, "live_ok": live, "usage": usage_state(), "progress": progress_settings(),
            "stats": stats_settings()}


LOCAL_PORT = 8883
LIVE_IDLE = 90.0
LIVE_PING = 15.0
LIVE_DEAD = 35.0
LIVE_JUMP = 10.0
LIVE_RETRY = (2.0, 5.0, 15.0)
_live: dict = {"seen": 0.0, "printers": {}, "links": {}, "keep": False}
_local: dict = {}
_live_lock = threading.Lock()


def _mqtt_len(size: int) -> bytes:
    out = bytearray()
    while True:
        digit, size = size % 128, size // 128
        out.append(digit | (128 if size else 0))
        if not size:
            return bytes(out)


def _mqtt_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def _mqtt_packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + _mqtt_len(len(body)) + body


def _mqtt_connect(user: str, password: str) -> bytes:
    body = _mqtt_str("MQTT") + b"\x04\xc2" + struct.pack("!H", 60)
    body += _mqtt_str("spoolio-" + os.urandom(4).hex()) + _mqtt_str(user) + _mqtt_str(password)
    return _mqtt_packet(0x10, body)


def _mqtt_subscribe(topic: str, packet_id: int) -> bytes:
    return _mqtt_packet(0x82, struct.pack("!H", packet_id) + _mqtt_str(topic) + b"\x00")


def _mqtt_publish(topic: str, payload: str) -> bytes:
    return _mqtt_packet(0x30, _mqtt_str(topic) + payload.encode("utf-8"))


def _mqtt_exact(sock, size: int) -> bytes:
    data = b""
    deadline = time.monotonic() + 15
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout:
            if time.monotonic() > deadline:
                raise
            continue
        if not chunk:
            raise ConnectionError("The printer closed the connection")
        data += chunk
    return data


def _mqtt_read(sock) -> tuple[int, bytes] | None:
    """Next packet as (type byte, body), or None on timeout."""
    try:
        first = sock.recv(1)
    except socket.timeout:
        return None
    if not first:
        raise ConnectionError("The printer closed the connection")
    size, shift = 0, 0
    while True:
        digit = _mqtt_exact(sock, 1)[0]
        size += (digit & 127) << shift
        shift += 7
        if not digit & 128 or shift > 21:
            break
    return first[0], _mqtt_exact(sock, size) if size else b""


def _int(value: object, default: int = -1) -> int:
    try:
        return int(str(value))
    except ValueError:
        return default


def live_apply(dev_id: str, name: str, doc: object, link: str = "") -> None:
    """Merge a printer report into the live state."""
    report = doc.get("print") if isinstance(doc, dict) else None
    if not isinstance(report, dict):
        return
    with _live_lock:
        state = _live["printers"].setdefault(dev_id, {"name": name, "units": {}, "ext": {}, "time": 0.0, "link": link})
        state["time"] = time.time()
        ams = report.get("ams")
        for unit in (ams.get("ams") if isinstance(ams, dict) else None) or []:
            if not isinstance(unit, dict) or _int(unit.get("id")) < 0:
                continue
            slots = state["units"].setdefault(_int(unit["id"]), {})
            for tray in unit.get("tray") or []:
                if not isinstance(tray, dict) or _int(tray.get("id")) < 0:
                    continue
                if set(tray) <= {"id"}:
                    slots.pop(_int(tray["id"]), None)
                else:
                    slots.setdefault(_int(tray["id"]), {}).update(tray)
        ext = report.get("vt_tray")
        if isinstance(ext, dict):
            if set(ext) <= {"id"}:
                state["ext"].clear()
            else:
                state["ext"].update(ext)
        job = {k: report[k] for k in ("gcode_state", "mc_percent", "subtask_name", "gcode_file") + JOB_KEYS
               if k in report}
        prog = state.setdefault("prog", {})
        was = str(prog.get("gcode_state") or "").upper()
        prog.update({k: report[k] for k in PROG_KEYS if k in report})
        if isinstance(ams, dict) and "tray_now" in ams:
            prog["tray_now"] = ams["tray_now"]
        dev = report.get("device")
        ex = dev.get("extruder") if isinstance(dev, dict) else None
        if isinstance(ex, dict):
            if "state" in ex:
                prog["ex_now"] = (_int(ex["state"], 0) >> 4) & 0xF
            for e in ex.get("info") or []:
                if isinstance(e, dict) and "snow" in e:
                    prog.setdefault("snow", {})[_int(e.get("id"), 0)] = _int(e["snow"])
        key = tray_key(prog)
        if key != prog.get("key"):
            prog["key"] = key
            log.info("Active tray: %s (tray_now %s, extruder %s, snow %s)", key, prog.get("tray_now"),
                     prog.get("ex_now"), prog.get("snow"))
        events = _note_tray(dev_id, state)
        if "gcode_state" in report:
            gs = str(report["gcode_state"]).upper()
            if gs in ("FINISH", "FAILED"):
                if was in LIVE_STATES:
                    prog["end"] = time.time()
            else:
                prog.pop("end", None)
        if job:
            state.setdefault("job", {}).update(job)
            events += usage_track(dev_id, state, "gcode_state" in report)
    for event in events:
        usage_event(*event)


def _live_tray(slot: int, tray: dict) -> dict:
    kind = str(tray.get("tray_type") or "").strip()
    colour = _hex6(tray.get("tray_color"))
    remain = tray.get("remain")
    uid = str(tray.get("tray_uuid") or tray.get("tag_uid") or "")
    return {
        "slot": slot,
        "empty": not kind and not colour,
        "type": kind,
        "name": str(tray.get("tray_sub_brands") or kind).strip(),
        "colour": colour,
        "remain": remain if isinstance(remain, (int, float)) and 0 <= remain <= 100 else None,
        "rfid": bool(uid.strip("0")),
    }


PROG_KEYS = ("gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num",
             "subtask_name", "gcode_file", "stg_cur", "gcode_start_time")
STAGES = {
    1: "Auto Bed Levelling", 2: "Heating Bed", 3: "Sweeping XY Mech Mode", 4: "Changing Filament",
    5: "Paused", 6: "Paused: Filament Runout", 7: "Heating Nozzle", 8: "Calibrating Extrusion",
    9: "Scanning Bed Surface", 10: "Inspecting First Layer", 11: "Identifying Build Plate",
    12: "Calibrating Micro Lidar", 13: "Homing Toolhead", 14: "Cleaning Nozzle Tip",
    15: "Checking Extruder Temperature", 16: "Paused by User", 17: "Paused: Front Cover Removed",
    18: "Calibrating Micro Lidar", 19: "Calibrating Extrusion Flow", 20: "Paused: Nozzle Temperature Fault",
    21: "Paused: Bed Temperature Fault", 22: "Unloading Filament", 23: "Paused: Skipped Step",
    24: "Loading Filament", 25: "Calibrating Motor Noise", 26: "Paused: AMS Lost",
    27: "Paused: Heat Break Fan", 28: "Paused: Chamber Temperature Fault", 29: "Cooling Chamber",
    30: "Paused by G-code", 31: "Motor Noise Showoff", 32: "Paused: Nozzle Covered",
    33: "Paused: Cutter Error", 34: "Paused: First Layer Error", 35: "Paused: Nozzle Clog",
}


def tray_key(p: dict) -> int:
    """Printer tray number feeding now: AMS unit*4+slot, AMS HT unit id, 254 external, -1 none.

    H2 printers report it per extruder as snow = ams_id << 8 | slot; others only have tray_now.
    """
    snow = p.get("snow") or {}
    if snow:
        n = snow.get(p.get("ex_now", 0), snow[min(snow)] if len(snow) == 1 else -1)
        unit, slot = n >> 8, n & 0xFF
        if n < 0 or unit == 255:
            return -1
        return 254 if unit == 254 else unit if unit >= 128 else unit * 4 + slot
    t = _int(p.get("tray_now"))
    return t if 0 <= t <= 254 else -1


def _live_prog(state: dict) -> dict | None:
    p = state.get("prog") or {}
    gs = str(p.get("gcode_state") or "").upper()
    if not gs:
        return None
    def num(key: str) -> int | None:
        value = _int(p.get(key))
        return value if value >= 0 else None

    tray = None
    t = tray_key(p)
    if state.get("kind") == "net":
        g, k = _int(p.get("gate")), _int(p.get("tool"))
        tray = {"unit": "mmu", "slot": g} if g >= 0 else {"unit": "tool", "slot": k} if k >= 0 else None
    elif 0 <= t < 128:
        tray = {"unit": t // 4, "slot": t % 4}
    elif 128 <= t < 254:
        tray = {"unit": t, "slot": 0}
    elif t == 254:
        tray = {"unit": "ext", "slot": 0}
    name = str(p.get("subtask_name") or "").strip() or Path(str(p.get("gcode_file") or "")).name
    start, end = num("gcode_start_time"), p.get("end")
    return {"state": gs, "pct": num("mc_percent"), "left": num("mc_remaining_time"), "layer": num("layer_num"),
            "layers": num("total_layer_num"), "name": name[:80],
            "stage": p.get("stage") if state.get("kind") == "net" else STAGES.get(_int(p.get("stg_cur"))),
            "tray": tray, "took": round((end - start) / 60) if end and start and end > start else None,
            "end": end}


def live_snapshot() -> dict:
    with _live_lock:
        printers = []
        for dev_id, state in _live["printers"].items():
            if state.get("kind") == "net":
                gates = state.get("gates")
                units = [{"id": "mmu", "kind": "mmu", "trays": [dict(_live_tray(i, g), spool_id=g["spool_id"],
                                                                     empty=g["empty"] and not g["spool_id"])
                                                                for i, g in enumerate(gates)]}] if gates else []
                printers.append({"id": dev_id, "link": state.get("link", ""), "name": state["name"], "kind": "net",
                                 "age": round(time.time() - state["time"]), "units": units, "ext": None,
                                 "active_spool": state.get("active"), "books": bool(state.get("books")),
                                 "job": _live_prog(state)})
                continue
            units = []
            for unit_id in sorted(state["units"]):
                slots = state["units"][unit_id]
                size = 1 if unit_id >= 128 else 4
                units.append({"id": unit_id, "kind": "ht" if unit_id >= 128 else "ams",
                              "trays": [_live_tray(i, slots.get(i, {})) for i in range(size)]})
            ext = _live_tray(0, state["ext"]) if state["ext"] else None
            printers.append({"id": dev_id, "link": state.get("link", ""), "name": state["name"], "age": round(time.time() - state["time"]),
                             "units": units, "ext": ext, "job": _live_prog(state)})
        return {"links": dict(_live["links"]), "printers": printers}


CODES_FILE = DATA_DIR / "printer_codes.json"


def _code_key(pid: str) -> str:
    return f"{source()}:{pid}"


def read_codes() -> dict:
    try:
        data = _read(CODES_FILE)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Could not read %s", CODES_FILE)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def write_codes(codes: dict) -> None:
    try:
        _write(CODES_FILE, json.dumps(codes), private=True)
    except OSError:
        log.exception("Could not write %s", CODES_FILE)


def save_printer_code(pid: object, code: object) -> None:
    """Store a printer access code privately; never sent back to the page."""
    pid = str(pid or "")[:32]
    if pid not in {p["id"] for p in printers_cfg()}:
        return
    code = re.sub(r"\s", "", str(code or ""))[:64]
    with _cfg_lock:
        codes = read_codes()
        if code:
            codes[_code_key(pid)] = code
        else:
            codes.pop(_code_key(pid), None)
        write_codes(codes)


def prune_codes() -> None:
    mine = {_code_key(p["id"]) for p in printers_cfg() if p.get("ip")}
    with _cfg_lock:
        codes = read_codes()
        kept = {k: v for k, v in codes.items() if not k.startswith(f"{source()}:") or k in mine}
        if kept != codes:
            write_codes(kept)


class AuthError(Exception):
    pass


class TopicError(Exception):
    pass


PUSHALL = '{"pushing": {"sequence_id": "0", "command": "pushall"}}'


def _mqtt_run(sock, user: str, password: str, topics: list[str], push: list[tuple[str, str]], on_report, keep) -> None:
    """One MQTT session: connect, subscribe, request status, feed reports to on_report."""
    sock.settimeout(5)
    sock.sendall(_mqtt_connect(user, password))
    packet = _mqtt_read(sock)
    if not packet or packet[0] >> 4 != 2 or len(packet[1]) < 2:
        raise ConnectionError("The printer didn't accept the live connection")
    if packet[1][1] in (4, 5):
        raise AuthError("Wrong access code")
    if packet[1][1] != 0:
        raise ConnectionError("The live connection was refused")
    for number, topic in enumerate(topics, 1):
        sock.sendall(_mqtt_subscribe(topic, number))
    for topic, payload in push:
        sock.sendall(_mqtt_publish(topic, payload))
    ping = heard = mark = time.monotonic()
    wall = time.time()
    while keep():
        packet = _mqtt_read(sock)
        now, clock = time.monotonic(), time.time()
        if (clock - wall) - (now - mark) > LIVE_JUMP:
            raise ConnectionError("The computer woke from sleep")
        mark, wall = now, clock
        if packet:
            heard = now
        elif now - heard > LIVE_DEAD:
            raise ConnectionError("The printer stopped answering")
        if packet and packet[0] >> 4 == 9 and 128 in packet[1][2:]:
            raise TopicError("The printer refused the status topic")
        if packet and packet[0] >> 4 == 3:
            body = packet[1]
            size = struct.unpack("!H", body[:2])[0]
            topic = body[2:2 + size].decode("utf-8", "replace")
            dev_id = topic.split("/")[1] if topic.count("/") >= 2 else ""
            try:
                doc = json.loads(body[2 + size:])
            except ValueError:
                continue
            for reply_topic, payload in on_report(dev_id, doc) or []:
                sock.sendall(_mqtt_publish(reply_topic, payload))
        if time.monotonic() - ping > LIVE_PING:
            sock.sendall(b"\xc0\x00")
            ping = time.monotonic()


def _live_active() -> bool:
    return time.monotonic() - _live["seen"] < LIVE_IDLE or _live["keep"]


def _cert_serial(sock) -> str:
    """Serial from the printer certificate's subject CN."""
    found = ""
    for part in (sock.getpeercert(binary_form=True) or b"").split(b"\x06\x03\x55\x04\x03")[1:]:
        if len(part) > 2 and part[1] < 128:
            val = part[2:2 + part[1]].decode("ascii", "ignore")
            if re.fullmatch(r"[A-Za-z0-9]{8,32}", val):
                found = val
    return found


_tls_ctx: list = []


def _tls() -> ssl.SSLContext:
    # Built once; printers use self-signed certificates, so nothing is verified.
    if not _tls_ctx:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _tls_ctx.append(ctx)
    return _tls_ctx[0]


def _local_session(pid: str, link: dict, stop: threading.Event) -> None:
    context = _tls()
    with socket.create_connection((link["ip"], LOCAL_PORT), timeout=8) as raw:
        with context.wrap_socket(raw, server_hostname=link["ip"]) as sock:
            serial = link["serial"]
            if not serial:
                try:
                    serial = _cert_serial(sock)
                except (OSError, ValueError):
                    serial = ""
                log.info("Printer serial %s", "read from its certificate" if serial else "not found, listening for any")
            topics = [f"device/{serial or '+'}/report"]
            push = [(f"device/{serial}/request", PUSHALL)] if serial else []
            asked: set[str] = set()

            def on_report(dev_id: str, doc: object):
                _live["links"][pid] = "live"
                live_apply(pid, link["name"], doc, link=pid)
                if not serial and dev_id and dev_id not in asked:
                    asked.add(dev_id)
                    return [(f"device/{dev_id}/request", PUSHALL)]
                return None

            _live["links"][pid] = "waiting"
            with _live_lock:
                if pid in _live["printers"]:
                    _live["printers"][pid]["fresh"] = True
            log.info("Live link to a printer on the local network connected")
            _mqtt_run(sock, "bblp", link["code"], topics, push, on_report,
                      lambda: _live_active() and not stop.is_set())


def _local_run(pid: str, link: dict, stop: threading.Event) -> None:
    fails = 0
    while _live_active() and not stop.is_set():
        _live["links"][pid] = "connecting"
        start = time.monotonic()
        try:
            NET_SESSIONS.get(link.get("type"), _local_session)(pid, link, stop)
        except AuthError:
            _live["links"][pid] = "auth"
            log.warning("A printer refused the access code or API key")
            return
        except TopicError:
            _live["links"][pid] = "serial"
            log.warning("A printer refused the status topic")
            return
        except Exception as exc:
            fails = 0 if time.monotonic() - start > LIVE_DEAD else fails + 1
            _live["links"][pid] = "unreachable" if fails else "connecting"
            log.warning("Live link to a printer stopped: %s", exc)
            stop.wait(LIVE_RETRY[min(fails, len(LIVE_RETRY) - 1)])
    _live["links"].pop(pid, None)


_sync_lock = threading.RLock()


def _local_sync() -> bool:
    """Match the local connections to the saved printers."""
    with _sync_lock:
        codes = read_codes()
        wanted = {}
        for printer in printers_cfg():
            code = codes.get(_code_key(printer["id"]), "")
            if printer.get("ip") and (code or printer["type"] == "klipper"):
                wanted[printer["id"]] = {"ip": printer["ip"], "serial": printer["serial"], "code": code,
                                         "name": printer["name"], "type": printer["type"], "user": printer["user"]}
        for pid in list(_local):
            entry = _local[pid]
            if wanted.get(pid) != entry["link"]:
                entry["stop"].set()
                del _local[pid]
                _live["links"].pop(pid, None)
                with _live_lock:
                    _live["printers"].pop(pid, None)
        for pid, link in wanted.items():
            entry = _local.get(pid)
            if entry and (entry["thread"].is_alive() or _live["links"].get(pid) in ("auth", "serial")):
                continue
            stop = threading.Event()
            thread = threading.Thread(target=_local_run, args=(pid, link, stop), daemon=True)
            _local[pid] = {"link": link, "stop": stop, "thread": thread}
            thread.start()
        _live["keep"] = bool(wanted) and tracking() and source() == "spoolman"
        return bool(wanted)


def live_poll() -> dict:
    """Keep live links alive while the Printer tab is open; never blocks."""
    _live["seen"] = time.monotonic()
    _local_sync()
    return live_snapshot()


NET_POLL = 3.0
NET_IDLE = 15.0
MR_SLOW = 30.0
MR_OBJECTS = "print_stats&virtual_sdcard&toolhead&mmu&print_task_config"
MR_STATES = {"standby": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
             "cancelled": "FAILED", "error": "FAILED"}
MR_ERRORS = (urllib.error.URLError, OSError, ValueError)


def mr_base(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if re.match(r"https?://", host) else "http://" + host


def mr_get(link: dict, path: str) -> dict:
    """One Moonraker GET; returns its result."""
    head = {"X-Api-Key": link["code"]} if link.get("code") else {}
    req = urllib.request.Request(mr_base(link["ip"]) + path, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_tls()) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError("Moonraker refused the API key") from exc
        raise
    res = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(res, dict):
        raise ValueError("Not a Moonraker reply")
    return res


def _mr_try(link: dict, path: str) -> dict:
    try:
        return mr_get(link, path)
    except AuthError:
        raise
    except MR_ERRORS:
        return {}


def _mr_colour(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            nums = [float(v) for v in value[:3]]
        except (TypeError, ValueError):
            return ""
        k = 255 if max(nums) <= 1 else 1
        return "".join(f"{max(0, min(255, round(n * k))):02X}" for n in nums)
    return _hex6(value)


def _mr_gates(mmu: dict) -> list[dict]:
    """Happy Hare gates, each with the tools that map to it."""
    def at(key: str, i: int):
        v = mmu.get(key)
        return v[i] if isinstance(v, list) and i < len(v) else None

    ttg = mmu.get("ttg_map") if isinstance(mmu.get("ttg_map"), list) else []
    count = _int(mmu.get("num_gates"), 0) or len(mmu.get("gate_status") or [])
    gates = []
    for i in range(min(count, KLIPPER_SLOTS["mmu"])):
        sid = _int(at("gate_spool_id", i))
        gates.append({"tray_type": str(at("gate_material", i) or "").strip(),
                      "tray_color": _mr_colour(at("gate_color_rgb", i)) or _hex6(at("gate_color", i)),
                      "tray_sub_brands": str(at("gate_filament_name", i) or "").strip(),
                      "spool_id": sid if sid > 0 else None, "empty": _int(at("gate_status", i)) == 0,
                      "tools": [t for t, g in enumerate(ttg) if _int(g) == i] if ttg else [i]})
    return gates


def _mr_tool(name: object) -> int:
    m = re.fullmatch(r"extruder(\d*)", str(name or ""))
    return (int(m.group(1)) if m.group(1) else 0) if m else -1


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def mr_apply(pid: str, link: dict, status: dict, extra: dict) -> None:
    """Merge a Moonraker status into the live state."""
    ps = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    vs = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    th = status.get("toolhead") if isinstance(status.get("toolhead"), dict) else {}
    mmu = status.get("mmu") if isinstance(status.get("mmu"), dict) else None
    ptc = status.get("print_task_config") if isinstance(status.get("print_task_config"), dict) else {}
    xmap = ptc.get("extruder_map_table")
    file = str(ps.get("filename") or "")
    pct = max(0, min(100, round((_num(vs.get("progress")) or 0) * 100)))
    info = ps.get("info") if isinstance(ps.get("info"), dict) else {}
    meta = extra.get("meta") or {}
    est, took = _num(meta.get("estimated_time")), _num(ps.get("print_duration")) or 0
    left = est - took if est else (took / (pct / 100) - took if pct >= 1 else None)
    action = str((mmu or {}).get("action") or "")
    hh = str((mmu or {}).get("spoolman_support") or "off").lower()
    net_put(pid, link, MR_STATES.get(str(ps.get("state") or "").lower(), "IDLE"), pct, Path(file).stem, file, left,
            extra.get("job") or "", gates=_mr_gates(mmu) if mmu else None, active=extra.get("spool"),
            books=bool(extra.get("sm") and (extra.get("spool") or hh in ("push", "pull"))),
            layer=info.get("current_layer"), layers=info.get("total_layer"),
            tool=_int((mmu or {}).get("tool")) if mmu else _mr_tool(th.get("extruder")),
            gate=_int(mmu.get("gate")) if mmu else -1, stage=action if action and action.lower() != "idle" else None,
            xmap=[_int(h) for h in xmap] if isinstance(xmap, list) and xmap else None)


def net_put(pid: str, link: dict, gs: str, pct: int, name: str, file: str, left: float | None, job: str,
            gates: list | None = None, active: int | None = None, books: bool = False, layer=None, layers=None,
            tool: int = -1, gate: int = -1, stage: str | None = None, xmap: list | None = None) -> None:
    """Merge one status from a non-Bambu printer into the live state."""
    with _live_lock:
        state = _live["printers"].setdefault(pid, {"name": link["name"], "units": {}, "ext": {}, "time": 0.0,
                                                  "link": pid, "kind": "net"})
        state.update(time=time.time(), gates=gates, active=active, books=books, xmap=xmap)
        prog = state.setdefault("prog", {})
        was = prog.get("gcode_state")
        if gs != was:
            state["changed"] = time.monotonic()
        prog.update(gcode_state=gs, mc_percent=pct, layer_num=layer, total_layer_num=layers, subtask_name=name,
                    gcode_file=file, mc_remaining_time=round(max(0.0, left) / 60) if left is not None else None,
                    tool=tool, gate=gate, stage=stage)
        if gs in ("FINISH", "FAILED"):
            if was in LIVE_STATES:
                prog["end"] = time.time()
        else:
            prog.pop("end", None)
        state.setdefault("job", {}).update(gcode_state=gs, mc_percent=pct, subtask_name=name, gcode_file=file,
                                           task_id=job)
        events = usage_track(pid, state, True)
    for event in events:
        usage_event(*event)


def _net_gap(pid: str) -> float:
    # Fast while printing, just after a change, or while the Printer tab is open.
    now = time.monotonic()
    with _live_lock:
        st = _live["printers"].get(pid) or {}
        busy = (st.get("prog") or {}).get("gcode_state") in LIVE_STATES or now - st.get("changed", 0.0) < 60
    return NET_POLL if busy or now - _live["seen"] < 10 else NET_IDLE


def mr_session(pid: str, link: dict, stop: threading.Event) -> None:
    """Poll Moonraker while the live link is wanted."""
    meta: dict = {}
    extra: dict = {}
    key, slow = None, 0.0
    mr_get(link, "/server/info")
    log.info("Live link to a Klipper printer connected")
    while _live_active() and not stop.is_set():
        status = mr_get(link, "/printer/objects/query?" + MR_OBJECTS).get("status") or {}
        ps = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
        file = str(ps.get("filename") or "")
        if file and file not in meta:
            meta[file] = _mr_try(link, "/server/files/metadata?filename=" + urllib.parse.quote(file))
        if (file, ps.get("state")) != key:
            key = (file, ps.get("state"))
            jobs = _mr_try(link, "/server/history/list?limit=1&order=desc").get("jobs") or []
            job = jobs[0] if jobs and isinstance(jobs[0], dict) else {}
            extra["job"] = str(job.get("job_id") or "") if file and job.get("filename") == file else ""
        if time.monotonic() - slow > MR_SLOW:
            slow = time.monotonic()
            sm = _mr_try(link, "/server/spoolman/status")
            extra["sm"] = bool(sm.get("spoolman_connected"))
            sid = _int(sm.get("spool_id"))
            extra["spool"] = sid if sid > 0 else None
        extra["meta"] = meta.get(file) or {}
        _live["links"][pid] = "live"
        mr_apply(pid, link, status, extra)
        stop.wait(_net_gap(pid))


def mr_items(meta: dict) -> list[dict] | None:
    """Grams per tool from Moonraker's file metadata."""
    weights = meta.get("filament_weights")
    if not isinstance(weights, list):
        total = _num(meta.get("filament_weight_total"))
        weights = [total] if total else []
    colours = meta.get("filament_colors")
    colours = colours if isinstance(colours, list) else split_list(colours)
    types = split_list(meta.get("filament_type"))
    rows = []
    for i, grams in enumerate(weights):
        grams = _num(grams) or 0.0
        if grams > 0:
            rows.append({"n": i + 1, "grams": round(grams, 2), "preset": "",
                         "colour": _hex6(colours[i]) if i < len(colours) else "",
                         "material": types[i] if i < len(types) else ""})
    return rows or None


def mr_fetch_job(link: dict, job: dict) -> list[dict] | None:
    file = job.get("file") or ""
    if not file:
        return None
    return mr_items(mr_get(link, "/server/files/metadata?filename=" + urllib.parse.quote(file)))


def _mr_loaded(state: dict) -> list[dict]:
    """Trays in tool order: Happy Hare gates, else the MMU or toolheads set up by hand."""
    cfg = next((p for p in printers_cfg() if p["id"] == state.get("link")), None)
    units = {u["kind"]: u["slots"] for u in (cfg or {}).get("units", [])}
    if state.get("gates"):
        return [dict(g, key=i) for i, g in enumerate(state["gates"])]
    if "mmu" in units:
        return [{"key": i, "spool_id": _int(s) if s else None, "tools": [i]} for i, s in enumerate(units["mmu"])]
    heads = units.get("tool") or [None]
    act = state.get("active")
    # Snapmaker U1: filament n prints from the head in extruder_map_table[n - 1].
    xmap = state.get("xmap")
    return [{"key": 100 + i, "head": True, "tools": [t for t, h in enumerate(xmap) if h == i] if xmap else [i],
             "spool_id": _int(s) if s else (act if len(heads) == 1 else None)} for i, s in enumerate(heads)]


def printer_test(data: dict) -> dict:
    pid = str(data.get("id") or "")[:32]
    code = re.sub(r"\s", "", str(data.get("code") or ""))[:64] or read_codes().get(_code_key(pid), "")
    host = str(data.get("host") or "").strip()[:64]
    if not re.fullmatch(r"(https?://)?[A-Za-z0-9.\-]+(:\d{1,5})?", host):
        return {"type": "printer_test", "id": pid, "ok": False, "text": "Enter the printer's address, such as voron.local"}
    kind = data.get("kind") if data.get("kind") in NET else "klipper"
    user = str(data.get("user") or "").strip()[:32]
    return {"type": "printer_test", "id": pid, **NET_TESTS[kind](host, code, user)}


def mr_test(host: str, code: str, user: str = "") -> dict:
    """What Spoolio finds at a Moonraker address."""
    link = {"ip": host, "code": code}
    try:
        info = mr_get(link, "/server/info")
    except AuthError:
        return {"ok": False, "text": "Moonraker refused the API key"}
    except MR_ERRORS as exc:
        return {"ok": False, "text": f"Can't reach Moonraker at {mr_base(host)} ({exc})"}
    parts = [f"Moonraker {info.get('moonraker_version') or ''}".strip()]
    klipper = _mr_try(link, "/printer/info").get("software_version")
    if klipper:
        parts.append(f"Klipper {str(klipper).split('-')[0]}")
    objs = _mr_try(link, "/printer/objects/list").get("objects") or []
    heads = len([o for o in objs if re.fullmatch(r"extruder\d*", str(o))])
    parts.append(f"{heads} Toolheads" if heads > 1 else "1 Toolhead")
    if "mmu" in objs:
        gates = _mr_gates((_mr_try(link, "/printer/objects/query?mmu").get("status") or {}).get("mmu") or {})
        parts.append(f"Happy Hare, {len(gates)} Gates")
    sm = _mr_try(link, "/server/spoolman/status")
    return {"ok": True, "text": " · ".join(parts), "books": bool(sm.get("spoolman_connected"))}


PL_STATES = {"PRINTING": "RUNNING", "PAUSED": "PAUSE", "ATTENTION": "PAUSE", "FINISHED": "FINISH",
             "STOPPED": "FAILED", "ERROR": "FAILED"}
OP_STATES = {"printing": "RUNNING", "printing from sd": "RUNNING", "starting": "RUNNING", "resuming": "RUNNING",
             "pausing": "PAUSE", "paused": "PAUSE", "finishing": "RUNNING", "cancelling": "FAILED"}
GCODE_TAIL = 262144


_openers: list = []


def net_open(link: dict, path: str, head: dict | None = None):
    """One GET to PrusaLink or OctoPrint; digest sign-in when a user is set, else the API key."""
    url = mr_base(link["ip"]) + path
    head = dict(head or {})
    if link.get("user"):
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, link["user"], link.get("code") or "")
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls()),
                                             urllib.request.HTTPDigestAuthHandler(mgr))
    else:
        if link.get("code"):
            head["X-Api-Key"] = link["code"]
        if not _openers:
            _openers.append(urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls())))
        opener = _openers[0]
    try:
        return opener.open(urllib.request.Request(url, headers=head), timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError("The printer refused the password or API key") from exc
        raise


def net_get(link: dict, path: str) -> dict:
    with net_open(link, path) as resp:
        body = resp.read()
    doc = json.loads(body.decode("utf-8")) if body.strip() else {}
    if not isinstance(doc, dict):
        raise ValueError("Not a JSON reply")
    return doc


def _net_try(link: dict, path: str) -> dict:
    try:
        return net_get(link, path)
    except AuthError:
        raise
    except MR_ERRORS:
        return {}


def _heads(pid: str) -> int:
    cfg = next((p for p in printers_cfg() if p["id"] == pid), None)
    return next((len(u["slots"]) for u in (cfg or {}).get("units", []) if u["kind"] == "tool"), 1)


def pl_session(pid: str, link: dict, stop: threading.Event) -> None:
    """Poll PrusaLink while the live link is wanted."""
    net_get(link, "/api/version")
    log.info("Live link to a PrusaLink printer connected")
    job: dict = {}
    while _live_active() and not stop.is_set():
        st = net_get(link, "/api/v1/status")
        pr = st.get("printer") if isinstance(st.get("printer"), dict) else {}
        jb = st.get("job") if isinstance(st.get("job"), dict) else {}
        jid = str(jb.get("id") or "")
        if jid and str(job.get("id") or "") != jid:
            job = _net_try(link, "/api/v1/job")
        f = job.get("file") if isinstance(job.get("file"), dict) else {}
        file = (str(f.get("path") or "").rstrip("/") + "/" + str(f.get("name") or "")) if f.get("name") else ""
        gs = PL_STATES.get(str(pr.get("state") or "").upper(), "IDLE")
        pct = max(0, min(100, round(_num(jb.get("progress")) or 0)))
        _live["links"][pid] = "live"
        net_put(pid, link, gs, pct, Path(str(f.get("display_name") or f.get("name") or "")).stem, file,
                _num(jb.get("time_remaining")), str(job.get("id") or ""), tool=0 if _heads(pid) == 1 else -1)
        stop.wait(_net_gap(pid))


def pl_fetch_job(link: dict, job: dict) -> list[dict] | None:
    file = job.get("file") or ""
    if not file:
        return None
    cur = _net_try(link, "/api/v1/job")
    f = cur.get("file") if isinstance(cur.get("file"), dict) else {}
    meta = f.get("meta") if f.get("name") and file.endswith("/" + str(f["name"])) else None
    if not isinstance(meta, dict) or "filament used [g] per tool" not in meta:
        meta = net_get(link, "/api/v1/files" + urllib.parse.quote(file)).get("meta") or {}
    return net_items(meta)


def net_items(meta: dict) -> list[dict] | None:
    """Grams per tool from slicer metadata, as PrusaLink or the G-code itself gives it."""
    weights = meta.get("filament used [g] per tool")
    if not isinstance(weights, list):
        weights = [float(v) for v in re.findall(r"[\d.]+", str(meta.get("filament used [g]") or ""))]
    types = meta.get("filament_type per tool")
    types = types if isinstance(types, list) else split_list(meta.get("filament_type"))
    colours = split_list(meta.get("filament_colour") or meta.get("extruder_colour"))
    rows = []
    for i, grams in enumerate(weights):
        grams = _num(grams) or 0.0
        if grams > 0:
            rows.append({"n": i + 1, "grams": round(grams, 2), "preset": "",
                         "colour": _hex6(colours[i]) if i < len(colours) else "",
                         "material": str(types[i]) if i < len(types) else ""})
    return rows or None


def pl_test(host: str, code: str, user: str = "") -> dict:
    link = {"ip": host, "code": code, "user": user or "maker"}
    try:
        ver = net_get(link, "/api/version")
    except AuthError:
        return {"ok": False, "text": "PrusaLink refused the username or password"}
    except MR_ERRORS as exc:
        return {"ok": False, "text": f"Can't reach PrusaLink at {mr_base(host)} ({exc})"}
    info = _net_try(link, "/api/v1/info")
    parts = [str(ver.get("text") or "PrusaLink"), str(info.get("name") or info.get("hostname") or "")]
    if info.get("mmu"):
        parts.append("MMU")
    return {"ok": True, "text": " · ".join(x for x in parts if x)}


def op_session(pid: str, link: dict, stop: threading.Event) -> None:
    """Poll OctoPrint while the live link is wanted."""
    net_get(link, "/api/version")
    log.info("Live link to an OctoPrint printer connected")
    last, job_id, file = "IDLE", "", ""
    while _live_active() and not stop.is_set():
        doc = net_get(link, "/api/job")
        jb = doc.get("job") if isinstance(doc.get("job"), dict) else {}
        pg = doc.get("progress") if isinstance(doc.get("progress"), dict) else {}
        f = jb.get("file") if isinstance(jb.get("file"), dict) else {}
        pct = max(0, min(100, int(_num(pg.get("completion")) or 0)))
        gs = OP_STATES.get(str(doc.get("state") or "").lower())
        if gs is None:
            gs = last if last in ("FINISH", "FAILED", "IDLE") else "FINISH" if pct >= 100 else "FAILED"
        if gs == "RUNNING" and last not in ("RUNNING", "PAUSE"):
            file = str(f.get("path") or f.get("name") or "")
            job_id = f"{file}@{int(time.time() - (_num(pg.get('printTime')) or 0)) // 60}"
        last = gs
        _live["links"][pid] = "live"
        net_put(pid, link, gs, pct, Path(file).stem, file, _num(pg.get("printTimeLeft")), job_id,
                tool=0 if _heads(pid) == 1 else -1)
        stop.wait(_net_gap(pid))


def op_fetch_job(link: dict, job: dict) -> list[dict] | None:
    """Grams per tool from the slicer's notes at the end of the G-code."""
    file = job.get("file") or ""
    if not file:
        return None
    tail = b""
    with net_open(link, "/downloads/files/local/" + urllib.parse.quote(file), {"Range": f"bytes=-{GCODE_TAIL}"}) as resp:
        while chunk := resp.read(65536):
            tail = (tail + chunk)[-GCODE_TAIL:]
    meta = {}
    for m in re.finditer(r"^;\s*([a-z_ ]+(?:\[g\])?)\s*=\s*(.*)$", tail.decode("utf-8", "replace"), re.M):
        meta.setdefault(m.group(1).strip(), m.group(2).strip())
    return net_items(meta)


def op_test(host: str, code: str, user: str = "") -> dict:
    link = {"ip": host, "code": code}
    if not code:
        return {"ok": False, "text": "Enter an OctoPrint API key"}
    try:
        ver = net_get(link, "/api/version")
        net_get(link, "/api/job")
    except AuthError:
        return {"ok": False, "text": "OctoPrint refused the API key"}
    except MR_ERRORS as exc:
        return {"ok": False, "text": f"Can't reach OctoPrint at {mr_base(host)} ({exc})"}
    plugins = (_net_try(link, "/plugin/pluginmanager/plugins").get("plugins") or [])
    books = any("spool" in str(x.get("key") or "").lower() and x.get("enabled") for x in plugins if isinstance(x, dict))
    return {"ok": True, "text": f"OctoPrint {ver.get('server') or ''}".strip(), "books": books}


NET_SESSIONS = {"klipper": mr_session, "prusalink": pl_session, "octoprint": op_session}
NET_FETCH = {"klipper": mr_fetch_job, "prusalink": pl_fetch_job, "octoprint": op_fetch_job}
NET_TESTS = {"klipper": mr_test, "prusalink": pl_test, "octoprint": op_test}


USAGE_FILE = DATA_DIR / "usage.json"
USAGE_MODES = ("ask", "auto", "off")
USAGE_PRINTS = 5
_usage: dict = {"plan": None}
_usage_lock = threading.RLock()


def usage_mode() -> str:
    value = cfg().get("book_usage")
    return value if value in USAGE_MODES else "ask"


def stats_only() -> bool:
    return usage_mode() == "off" and cfg().get("stats_when_off") is True


def tracking() -> bool:
    return usage_mode() != "off" or stats_only()


def usage_load() -> dict:
    try:
        data = _read(USAGE_FILE)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        log.exception("Could not read %s", USAGE_FILE)
        data = {}
    data = data if isinstance(data, dict) else {}
    if "prints" not in data and isinstance(data.get("pending"), list):
        data["prints"] = [dict(e, state="pending") for e in data["pending"] if isinstance(e, dict)][:USAGE_PRINTS]
    return {key: data.get(key) if isinstance(data.get(key), kind) else kind()
            for key, kind in (("prints", list), ("active", dict), ("last", dict), ("done", list))}


def usage_save(data: dict) -> None:
    try:
        _write(USAGE_FILE, json.dumps(data))
    except OSError:
        log.exception("Could not write %s", USAGE_FILE)


def usage_state() -> dict:
    enabled = source() == "spoolman"
    data = usage_load()
    return {"enabled": enabled, "mode": usage_mode(), "stats": cfg().get("stats_when_off") is True,
            "prints": data["prints"] if enabled else []}


def usage_plan(slots: list[dict]) -> None:
    """Store the latest slice plan for booking the print that follows."""
    if source() != "spoolman":
        return
    used = [{key: slot[key] for key in ("n", "grams", "preset", "colour", "material")}
            for slot in slots if slot["grams"] > 0]
    _usage["plan"] = {"slots": used, "time": time.time()} if used else None
    _local_sync()


def _tag_ids(value: object) -> set[str]:
    out = set()
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            item = item.get("uid") or item.get("id")
        text = str(item or "").strip()
        if text.startswith('"'):
            try:
                text = str(json.loads(text))
            except ValueError:
                pass
        text = re.sub(r"[\s:\-\"]", "", text).upper()
        if len(text) >= 8 and re.fullmatch(r"[0-9A-F]+", text) and text.strip("0"):
            out.add(text)
    return out


def spool_tags(spool: dict) -> set[str]:
    extra = spool.get("extra")
    first = _tag_ids(extra.get("tag") if isinstance(extra, dict) else None)
    if first:
        return first
    tags = _tag_ids(spool.get("tags"))
    filament = spool.get("filament")
    for holder in (spool, filament if isinstance(filament, dict) else {}):
        extra = holder.get("extra")
        for value in (extra.values() if isinstance(extra, dict) else []):
            tags |= _tag_ids(value)
    return tags


def match_spool(tray: dict | None, spools: list[dict]) -> dict | None:
    """The one spool whose RFID tag matches the tray, else None."""
    if not tray:
        return None
    if "tools" in tray:
        found = [sp for sp in spools if not sp.get("archived") and _int(sp.get("id")) == tray.get("spool_id")]
        return found[0] if found else None
    uids = _tag_ids([tray.get("tag_uid"), tray.get("tray_uuid")])
    found = [sp for sp in spools if not sp.get("archived") and uids & spool_tags(sp)]
    return found[0] if len(found) == 1 else None


def _loaded(state: dict) -> list[dict]:
    """Trays in slicer slot order, each with its printer tray number as key."""
    if state.get("kind") == "net":
        return _mr_loaded(state)
    trays = []
    for unit_id in sorted(state["units"], key=lambda u: (u >= 128, u)):
        slots = state["units"][unit_id]
        trays += [dict(slots.get(i, {}), key=unit_id if unit_id >= 128 else unit_id * 4 + i)
                  for i in range(1 if unit_id >= 128 else 4)]
    if state["ext"]:
        trays.append(dict(state["ext"], key=254, spare=bool(trays)))
    return trays


def _note_tray(dev_id: str, state: dict) -> list[tuple]:
    """Remember each tray that feeds the running print. Holds _live_lock."""
    run, p = state.get("run"), state.get("prog") or {}
    t = tray_key(p)
    if not run or not 0 <= t <= 254 or str(p.get("gcode_state") or "").upper() != "RUNNING":
        return []
    if _int(p.get("layer_num"), 0) < 1 or t in run.setdefault("used", []):
        return []
    run["used"].append(t)
    if t not in [tr.get("key") for tr in run["trays"]]:
        run["trays"] = _loaded(state)
    log.info("Print is using printer tray %d", t)
    return [("save", dev_id, dict(run))]


def _fits(item: dict, tray: dict) -> bool:
    if not _same_mat(item.get("material"), tray.get("tray_type")):
        return False
    gap = colour_diff(item.get("colour") or "", _hex6(tray.get("tray_color")))
    return gap is None or gap <= COLOUR_TOLERANCE


def run_tray(run: dict, item: dict, count: int) -> dict | None:
    """The tray a filament slot printed from: the trays seen feeding the print come first."""
    trays = run.get("trays") or []
    if any("tools" in tr for tr in trays):
        return next((tr for tr in trays if item["n"] - 1 in tr.get("tools", [])), None)
    keyed = {tr["key"]: tr for tr in trays if "key" in tr}
    used = [keyed[k] for k in run.get("used") or [] if k in keyed]
    if count == 1 and len(used) == 1:
        return used[0]
    slots = [tr for tr in trays if not tr.get("spare")]
    pos = slots[item["n"] - 1] if 0 < item["n"] <= len(slots) else None
    pool = used or trays
    fit = [tr for tr in pool if _fits(item, tr)]
    if len(fit) == 1:
        return fit[0]
    if pos is not None and (not used or pos in used) and (not fit or pos in fit):
        return pos
    return None


JOB_KEYS = ("task_id", "subtask_id", "gcode_start_time")
USAGE_DONE = 40
LIVE_STATES = ("PREPARE", "RUNNING", "PAUSE")


def job_ident(job: dict) -> dict:
    out = {k: str(job.get(k) or "") for k in JOB_KEYS}
    out.update(name=str(job.get("subtask_name") or ""), file=str(job.get("gcode_file") or ""))
    return out


def same_job(a: dict | None, b: dict | None) -> bool:
    """Same print, judged by name and file plus whichever ids both sides know."""
    if not a or not b or (a.get("name"), a.get("file")) != (b.get("name"), b.get("file")):
        return False
    known = [k for k in JOB_KEYS if a.get(k) not in ("", "0") and b.get(k) not in ("", "0")]
    return all(a[k] == b[k] for k in known)


def _ended(run: dict, printer: str, result: str, pct: int) -> dict:
    return {**run, "printer": printer, "result": result, "pct": 100 if result == "finished" else pct}


def usage_track(dev_id: str, state: dict, has_state: bool) -> list[tuple]:
    """Track a printer through a print; returns events to handle outside the lock. Holds _live_lock.

    The run is saved to disk while it lasts, so a dropped link or a restart picks it up again.
    """
    job = state.get("job") or {}
    gcode = job.get("gcode_state")
    if not gcode or source() != "spoolman" or not state.get("link") or not tracking():
        return []
    events: list[tuple] = []
    if not state.get("loaded"):
        state["loaded"] = True
        state["fresh"] = True
        with _usage_lock:
            store = usage_load()
        state["run"] = store["active"].get(dev_id)
        state["last"] = store["last"].get(dev_id)
        if state["run"] and not state["run"].get("file"):
            events.append(("fetch", dev_id, state["run"]))
    fresh = has_state and state.pop("fresh", False)
    pct = max(0, min(100, _int(job.get("mc_percent"), 0)))
    ident = job_ident(job)
    run = state.get("run")
    if fresh and run and not same_job(run["job"], ident):
        # The tracked print ended while the link was down and another one has run since.
        events.append(("end", dev_id, _ended(run, state["name"], "unknown", run["pct"])))
        state["run"] = run = None
    if run is None:
        if fresh and gcode in ("FINISH", "FAILED") and state.get("last") and not same_job(state["last"], ident):
            events.append(("missed", dev_id, {"job": ident, "printer": state["name"],
                                              "result": "finished" if gcode == "FINISH" else "cancelled",
                                              "pct": pct, "link": state["link"]}))
        if gcode in ("IDLE", "FINISH", "FAILED"):
            state["idle_seen"] = True
        elif gcode in ("PREPARE", "RUNNING") and (state.get("idle_seen") or pct <= 2 or fresh):
            new = state.get("idle_seen") or pct <= 2
            plan = _usage["plan"]["slots"] if new and _usage["plan"] else None
            state["run"] = {"id": f"{dev_id}-{int(time.time())}", "plan": plan, "file": None, "job": ident,
                            "name": str(job.get("subtask_name") or job.get("gcode_file") or "Print")[:80],
                            "trays": _loaded(state), "pct": pct, "late": not new,
                            "net": state.get("kind") == "net"}
            state["idle_seen"] = False
            events.append(("start", dev_id, dict(state["run"])))
        if not same_job(state.get("last"), ident) and (ident["name"] or ident["file"]):
            state["last"] = ident
            events.append(("last", dev_id, ident))
        return events
    if gcode in LIVE_STATES:
        step = pct // 5 != run["pct"] // 5 or run["job"] != ident
        run["pct"] = max(run["pct"], pct)
        run["job"] = ident
        if step:
            events.append(("save", dev_id, dict(run)))
        return events
    if gcode not in ("FINISH", "FAILED", "IDLE"):
        return events
    state["run"] = None
    state["idle_seen"] = True
    state["last"] = ident if ident["name"] or ident["file"] else state.get("last")
    result = "finished" if gcode == "FINISH" else "cancelled"
    events.append(("end", dev_id, _ended(run, state["name"], result, max(run["pct"], pct))))
    return events


FTP_PORT = 990
JOB_DIRS = ("/", "/cache", "/model", "/sdcard", "/sdcard/cache", "/sdcard/model")
JOB_MAX = 1024 * 1024 * 1024
JOB_TRIES = 6
JOB_RETRY = 30.0


class _Ftps(ftplib.FTP_TLS):
    """Implicit FTPS; data connections reuse the control TLS session, which Bambu printers require."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout=timeout)
        self.af = raw.family
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def pull(self, cmd: str, out, limit: int) -> None:
        # No TLS unwrap after the transfer: some printers never answer it.
        self.voidcmd("TYPE I")
        got = 0
        with self.transfercmd(cmd) as conn:
            while chunk := conn.recv(65536):
                got += len(chunk)
                if got > limit:
                    raise ValueError("The job file is too large")
                out.write(chunk)
        self.voidresp()


def _ftp_open(link: dict) -> _Ftps:
    ftp = _Ftps(context=_tls(), timeout=15)
    ftp.connect(link["ip"], FTP_PORT, 15)
    ftp.login("bblp", link["code"])
    ftp.prot_p()
    return ftp


def job_names(job: dict) -> list[str]:
    file, name = job.get("file") or "", job.get("name") or ""
    names = [file.rsplit("/", 1)[-1]] if file.lower().endswith(".3mf") else []
    names += [f"{name}.gcode.3mf", f"{name}.3mf"] if name else []
    return [n.lower() for n in names if n.strip(".")]


def _find_job(ftp: _Ftps, job: dict) -> str | None:
    wanted = job_names(job)
    if not wanted:
        return None
    for folder in JOB_DIRS:
        out = tempfile.SpooledTemporaryFile()
        try:
            ftp.pull(f"NLST {folder}", out, 4 * 1024 * 1024)
        except (ftplib.Error, OSError, ValueError):
            continue
        out.seek(0)
        found = {line.rsplit("/", 1)[-1].lower(): line.rsplit("/", 1)[-1]
                 for line in out.read().decode("utf-8", "replace").splitlines() if line.strip()}
        for name in wanted:
            if name in found:
                return folder.rstrip("/") + "/" + found[name]
        jobs = sorted(n for n in found.values() if n.lower().endswith(".3mf"))
        if jobs:
            log.info("Job files in %s: %s", folder, ", ".join(jobs[:20]))
    return None


CAL_RE = re.compile(r"calibration|flow ?rate|pressure advance|\bpa (line|pattern|tower)|temp(erature)? (test|tower)"
                    r"|volumetric speed|retraction (test|tower)|^retraction$|vfa test|input shaping|cornering test", re.I)


def is_cal(name: str) -> bool:
    return bool(CAL_RE.search((name or "").strip()))


def parse_slice(data: bytes, file: str = "") -> list[dict] | None:
    """Grams per filament slot for the printed plate, from Metadata/slice_info.config."""
    plates = []
    for plate in ET.fromstring(data).iter("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        cal = any(is_cal(o.get("name") or "") for o in plate.findall("object"))
        rows = []
        for item in plate.findall("filament"):
            try:
                grams = float(item.get("used_g") or 0)
            except ValueError:
                grams = 0.0
            n = _int(item.get("id"), 0)
            if n > 0 and grams > 0:
                rows.append({"n": n, "grams": round(grams, 2), "preset": str(item.get("tray_info_idx") or ""),
                             "colour": _hex6(item.get("color")), "material": str(item.get("type") or ""),
                             **({"cal": True} if cal else {})})
        if rows:
            plates.append((_int(meta.get("index"), 0), rows))
    hint = re.search(r"plate_(\d+)", file)
    if hint:
        match = [rows for index, rows in plates if index == int(hint.group(1))]
        if match:
            return match[0]
    return plates[0][1] if len(plates) == 1 else None


WASTE_SUPPORT = ("support",)
WASTE_PURGE = ("prime tower", "wipe tower", "flush", "purge")
FLUSH_RE = re.compile(r";\s*v?flush_(start|end)")


def parse_waste(lines) -> dict[int, tuple[float, float]]:
    """Support and purge share of each filament (1-based), from the plate G-code."""
    tool, kind, flush, rel, last = 0, "model", False, False, 0.0
    sums: dict[int, list[float]] = {}
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if line.startswith(";"):
            low = line.lower()
            if low.startswith((";type:", "; feature:")):
                name = low.split(":", 1)[1].strip()
                kind = ("support" if any(w in name for w in WASTE_SUPPORT)
                        else "purge" if any(w in name for w in WASTE_PURGE) else "model")
            else:
                m = FLUSH_RE.fullmatch(low.strip())
                if m:
                    flush = m.group(1) == "start"
            continue
        cmd = line.split(";", 1)[0].strip()
        if not cmd:
            continue
        head = cmd.split(None, 1)[0].upper()
        if head == "M83":
            rel = True
        elif head == "M82":
            rel = False
        elif re.fullmatch(r"T\d+", head) and int(head[1:]) < 255:
            tool = int(head[1:])
        elif head in ("G1", "G0", "G2", "G3", "G92"):
            m = re.search(r"\bE(-?\d*\.?\d+)", cmd, re.I)
            if not m:
                continue
            e = float(m.group(1))
            if head == "G92":
                last = e
                continue
            d = e if rel else e - last
            last = last if rel else e
            if d > 0:
                k = "purge" if flush else kind
                acc = sums.setdefault(tool + 1, [0.0, 0.0, 0.0])
                acc[(k == "support") + 2 * (k == "purge")] += d
    out = {}
    for n, (model, sup, pur) in sums.items():
        total = model + sup + pur
        if total > 0:
            out[n] = (round(sup / total, 4), round(pur / total, 4))
    return out


def _plate_gcode(box: zipfile.ZipFile, file: str) -> str | None:
    names = [n for n in box.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)]
    hint = re.search(r"plate_(\d+)", file)
    if hint and f"Metadata/plate_{hint.group(1)}.gcode" in names:
        return f"Metadata/plate_{hint.group(1)}.gcode"
    return names[0] if len(names) == 1 else None


def add_waste(items: list[dict], box: zipfile.ZipFile, file: str) -> None:
    name = _plate_gcode(box, file)
    if not name:
        return
    try:
        with box.open(name) as fh:
            waste = parse_waste(fh)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        log.info("Could not read waste from the job G-code: %s", exc)
        return
    for item in items:
        if item["n"] in waste:
            item["ws"], item["wp"] = waste[item["n"]]
    log.info("Waste from %s: %s", name, {n: waste[n] for n in sorted(waste)})


def fetch_job(link: dict, job: dict) -> list[dict] | None:
    """Download the printing job's file over FTPS and read its filament use."""
    ftp = _ftp_open(link)
    try:
        path = _find_job(ftp, job)
        if not path:
            log.info("The job file wasn't found on the printer (looked for %s in %s)",
                     ", ".join(job_names(job)) or "nothing", ", ".join(JOB_DIRS))
            return None
        log.info("Found the job file at %s", path)
        with tempfile.TemporaryFile() as out:
            ftp.pull(f"RETR {path}", out, JOB_MAX)
            out.seek(0)
            with zipfile.ZipFile(out) as box:
                info = box.getinfo("Metadata/slice_info.config")
                if info.file_size > 4 * 1024 * 1024:
                    return None
                items = parse_slice(box.read(info), job.get("file") or "")
                if items:
                    add_waste(items, box, job.get("file") or "")
    finally:
        try:
            ftp.quit()
        except (ftplib.Error, OSError):
            ftp.close()
    return items


def _try_fetch(dev_id: str, job: dict) -> list[dict] | None:
    link = (_local.get(dev_id) or {}).get("link")
    if not link:
        return None
    try:
        return NET_FETCH.get(link.get("type"), fetch_job)(link, job)
    except AuthError as exc:
        log.warning("Could not read the job from a printer: %s", exc)
        return None
    except (ftplib.Error, OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        log.warning("Could not read the job file from a printer: %s", exc)
        return None


def _store(change) -> None:
    with _usage_lock:
        data = usage_load()
        change(data)
        usage_save(data)


def _job_fetch(dev_id: str, run_id: str) -> None:
    """Fetch the job file while the print runs, retrying through link drops."""
    for _ in range(JOB_TRIES):
        with _live_lock:
            run = (_live["printers"].get(dev_id) or {}).get("run")
            job = dict(run["job"]) if run and run["id"] == run_id else None
        if job is None:
            return
        items = _try_fetch(dev_id, job)
        if items:
            with _live_lock:
                run = (_live["printers"].get(dev_id) or {}).get("run")
                if run and run["id"] == run_id:
                    run["file"] = items
                    saved = dict(run)
                else:
                    saved = None
            if saved:
                _store(lambda d: d["active"].__setitem__(dev_id, saved))
            log.info("Read the job file: %d slots", len(items))
            return
        time.sleep(JOB_RETRY)


def _missed(dev_id: str, info: dict) -> None:
    ident = info["job"]
    run_id = f"{dev_id}-" + re.sub(r"[^A-Za-z0-9]", "", "-".join(ident.values()))[:60]
    with _usage_lock:
        if run_id in usage_load()["done"]:
            return
    items = _try_fetch(dev_id, ident)
    if not items:
        log.info("A print ended while Spoolio was away and its job file couldn't be read")
        return
    with _live_lock:
        state = _live["printers"].get(dev_id)
        trays = _loaded(state) if state else []
    run = {"id": run_id, "plan": None, "file": items, "job": ident, "trays": trays, "late": True,
           "net": bool(state and state.get("kind") == "net"),
           "name": (ident["name"] or ident["file"] or "Print")[:80], "pct": info["pct"]}
    usage_finish(_ended(run, info["printer"], info["result"], info["pct"]), dev_id)


def _end(dev_id: str, run: dict) -> None:
    # Finish first: if Orca closes in between, the saved run is picked up and finished again.
    usage_finish(run, dev_id)

    def drop(d):
        if (d["active"].get(dev_id) or {}).get("id") == run["id"]:
            d["active"].pop(dev_id)
        if run["result"] != "unknown":
            d["last"][dev_id] = run["job"]
    _store(drop)


def usage_event(kind: str, dev_id: str, data: dict) -> None:
    if kind in ("start", "save"):
        _store(lambda d: d["active"].__setitem__(dev_id, data))
    if kind in ("start", "fetch"):
        _bg(_job_fetch, dev_id, data["id"])
    elif kind == "last":
        _store(lambda d: d["last"].__setitem__(dev_id, data))
    elif kind == "end":
        _bg(_end, dev_id, data)
    elif kind == "missed":
        _bg(_missed, dev_id, data)


def _spool_label(spool: dict) -> str:
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or ""
    return " ".join(part for part in (vendor, filament.get("name") or f"Spool {spool.get('id')}") if part)


def usage_finish(run: dict, dev_id: str = "") -> None:
    """Work out each slot's spool and grams, then ask or book. Each run is booked once."""
    with _usage_lock:
        if run["id"] in usage_load()["done"]:
            return
    items = run.get("file") or (_try_fetch(dev_id, run["job"]) if dev_id and run.get("job") else None)
    src = "file" if items else "slice"
    items = items or run.get("plan")
    if not items:
        log.info("A print ended but its filament use is unknown")
        return
    url = clean_url(cfg().get("spoolman_url"))
    spools = (plate_spools(url) if url else None) or []
    by_id = {sp.get("id"): sp for sp in spools}
    share = run["pct"] / 100 if run["result"] == "cancelled" else 1.0
    rows = []
    for item in items:
        tray = run_tray(run, item, len(items))
        spool = match_spool(tray, spools)
        grams = round(item["grams"] * share, 1)
        if grams <= 0:
            continue
        how = ("tool" if tray.get("head") else "gate") if tray and "tools" in tray else "rfid"
        where = (f"T{item['n'] - 1}" if tray.get("head") else f"Gate {tray['key']}") if how != "rfid" else \
            (f"T{item['n'] - 1}" if run.get("net") else "")
        rows.append({"slot": item["n"], "grams": grams, "preset": item["preset"], "material": item["material"],
                     "colour": item["colour"], "spool": spool["id"] if spool else None,
                     "how": how if spool else "", "label": _spool_label(by_id[spool["id"]]) if spool else "",
                     "state": "open", "error": "", "where": where, **{k: item[k] for k in ("ws", "wp") if k in item}})
    late = bool(run.get("late")) or run["result"] == "unknown"
    with _live_lock:
        books = bool((_live["printers"].get(dev_id) or {}).get("books"))
    entry = {"id": run["id"], "printer": run["printer"], "name": run["name"], "result": run["result"],
             "pct": run["pct"], "time": time.time(), "rows": rows, "late": late, "src": src, "state": "pending",
             "tracked": books and bool(run.get("net")),
             "cal": is_cal(run["name"]) or any(i.get("cal") for i in items)}
    nb = stats_only()
    with _usage_lock:
        data = usage_load()
        if run["id"] in data["done"]:
            return
        data["done"] = [run["id"]] + data["done"][:USAGE_DONE - 1]
        if nb:
            usage_save(data)
            if rows:
                hist_note(dict(entry, nb=True), by_id)
                log.info("A print ended (%s, %d%%): recorded in Stats only", run["result"], run["pct"])
            return
        if rows:
            prints = [entry] + [e for e in data["prints"] if e.get("id") != entry["id"]]
            data["prints"] = prints[:USAGE_PRINTS]
            # Unbooked prints that drop off go to Stats as Not Booked.
            for e in prints[USAGE_PRINTS:]:
                if e.get("state") == "pending" and e.get("rows") and \
                        not any(r.get("state") == "booked" for r in e["rows"]):
                    hist_note(dict(e, nb=True), by_id)
        usage_save(data)
    if not rows:
        return
    log.info("A print ended (%s, %d%%, grams from the %s): %d slots to book", run["result"], run["pct"],
             "job file" if src == "file" else "last slice", len(rows))
    if usage_mode() == "auto" and run["result"] == "finished" and not late and not run.get("net"):
        usage_book(entry["id"], None)


def spoolman_use(spool_id: int, grams: float) -> None:
    url = f"{clean_url(cfg().get('spoolman_url'))}/api/v1/spool/{int(spool_id)}/use"
    request = urllib.request.Request(url, data=json.dumps({"use_weight": round(grams, 1)}).encode("utf-8"),
                                     method="PUT", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass
    _spool_cache["time"] = 0.0


def spoolman_unuse(spool_id: int, grams: float) -> None:
    url = f"{clean_url(cfg().get('spoolman_url'))}/api/v1/spool/{int(spool_id)}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        used = float(json.loads(resp.read().decode("utf-8")).get("used_weight") or 0)
    body = json.dumps({"used_weight": round(max(0.0, used - grams), 1)}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass
    _spool_cache["time"] = 0.0


def _spoolman_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"Spoolman refused it (HTTP {exc.code})"
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach Spoolman ({exc.reason})"
    return str(exc)


def sm_req(method: str, path: str, body: object = None):
    url = f"{clean_url(cfg().get('spoolman_url'))}/api/v1/{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def _sm_err(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            msg = json.loads(exc.read().decode("utf-8")).get("message")
        except Exception:
            msg = None
        return f"Spoolman refused it: {msg}" if isinstance(msg, str) and msg else _spoolman_error(exc)
    return _spoolman_error(exc)


CUR_SIGNS = {"EUR": "€", "GBP": "£", "USD": "$"}
FIELD_TYPES = ("text", "integer", "integer_range", "float", "float_range", "datetime", "boolean", "choice")
_ext_cache: dict = {"url": "", "time": 0.0, "items": []}


def _num_or(value: object, default=None):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return int(n) if n == int(n) else n


def _first_hex(f: dict) -> str:
    c = f.get("color_hex")
    if not c:
        m = f.get("multi_color_hexes") or f.get("color_hexes") or ""
        c = (m if isinstance(m, list) else str(m).split(","))[0] if m else ""
    return _hex6(c)


def _fil(f: dict) -> dict:
    return {"id": f.get("id"), "v": (f.get("vendor") or {}).get("name") or "", "n": f.get("name") or "",
            "m": f.get("material") or "", "c": _first_hex(f), "w": f.get("weight"), "sw": f.get("spool_weight"),
            "d": f.get("density"), "dia": f.get("diameter"), "e": f.get("settings_extruder_temp"),
            "b": f.get("settings_bed_temp"), "t": _tvals(f.get("extra"))}


def _ext(f: dict) -> dict:
    return {"x": f.get("id"), "v": f.get("manufacturer") or "", "n": f.get("name") or "", "m": f.get("material") or "",
            "c": _first_hex(f), "w": f.get("weight"), "sw": f.get("spool_weight"), "d": f.get("density"),
            "dia": f.get("diameter"), "e": f.get("extruder_temp"), "b": f.get("bed_temp")}


def editor_data() -> dict:
    """Extra field definitions, manufacturers and filaments for the spool card."""
    try:
        fields = sm_req("GET", "field/spool") or []
        vendors = sm_req("GET", "vendor") or []
        fils = sm_req("GET", "filament") or []
    except Exception as exc:
        log.warning("Spool card data failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc)}
    code = spoolman_currency(clean_url(cfg().get("spoolman_url")))
    out = []
    for f in sorted(fields, key=lambda x: (x.get("order") or 0, x.get("name") or "")):
        if isinstance(f, dict) and f.get("key") and f.get("field_type") in FIELD_TYPES \
                and f["key"] not in TUNE_KEYS and f["key"] not in TUNE_OLD.values() and f["key"] != PA_BY:
            out.append({"key": f["key"], "name": f.get("name") or f["key"], "type": f["field_type"],
                        "unit": f.get("unit") or "", "choices": f.get("choices") or [],
                        "multi": bool(f.get("multi_choice"))})
    return {"ok": True, "fields": out, "cur": CUR_SIGNS.get(code, code),
            "vendors": sorted({v.get("name") for v in vendors if isinstance(v, dict) and v.get("name")}, key=str.lower),
            "filaments": [_fil(f) for f in fils if isinstance(f, dict) and not f.get("archived")], **_use_data()}


def _use_data() -> dict:
    """The printers a spool's presets can be limited to, when OrcaSlicer presets are on."""
    c = parse_presets(cfg().get("presets"))
    prs = [{"id": p["id"], "name": p["name"]} for p in printers_cfg() if c["printers"].get(p["id"], {}).get("on")]
    tn = c["on"] and c["tune"]["on"]
    return {"use_printers": prs if c["on"] else [], "use": c["use"],
            "tune": {"on": tn, "per": c["per"], "printers": [p["name"] for p in prs],
                     "keys": [{"k": t[0], "key": t[1], "l": t[2], "u": t[4]} for t in TUNE if c["tune"]["keys"][t[0]]]}}


def catalogue(query: object) -> dict:
    """Spoolman's own filament catalogue, searched here so any Spoolman version works."""
    url = clean_url(cfg().get("spoolman_url"))
    if _ext_cache["url"] != url or time.monotonic() - _ext_cache["time"] > 3600 or not _ext_cache["items"]:
        try:
            items = sm_req("GET", "external/filament") or []
        except Exception as exc:
            return {"ok": False, "error": _sm_err(exc), "items": []}
        _ext_cache.update(url=url, time=time.monotonic(), items=[_ext(f) for f in items if isinstance(f, dict)])
    words = str(query or "").lower().split()
    hits = [f for f in _ext_cache["items"] if all(w in f"{f['v']} {f['n']} {f['m']}".lower() for w in words)]
    return {"ok": True, "items": hits[:60], "more": len(hits) > 60}


def _vendor_id(name: str) -> int | None:
    name = name.strip()
    if not name:
        return None
    for v in sm_req("GET", "vendor") or []:
        if str(v.get("name") or "").strip().lower() == name.lower():
            return v["id"]
    return sm_req("POST", "vendor", {"name": name})["id"]


FIL_KEYS = {"name": "name", "material": "material", "color_hex": "color_hex", "diameter": "diameter",
            "density": "density", "ext": "settings_extruder_temp", "bed": "settings_bed_temp",
            "weight": "weight", "spool_weight": "spool_weight", "external_id": "external_id"}
FIL_NUM = ("diameter", "density", "ext", "bed", "weight", "spool_weight")
SPOOL_KEYS = ("remaining_weight", "initial_weight", "spool_weight", "location", "lot_nr", "price", "comment")


def _fil_body(f: dict) -> dict:
    body = {}
    for k, v in f.items():
        if k == "vendor":
            body["vendor_id"] = _vendor_id(str(v or ""))
        elif k in FIL_KEYS:
            if k in FIL_NUM:
                v = _num_or(v)
            elif k == "color_hex":
                v = _hex6(v) or None
            else:
                v = str(v).strip()[:256] if v not in (None, "") else None
            body[FIL_KEYS[k]] = v
    return body


def _extra_raw(sent: dict, raw: dict) -> dict:
    out = dict(raw)
    for k, v in sent.items():
        if v in ("", None, []) or (isinstance(v, list) and all(x in ("", None) for x in v)):
            if k in out:
                out[k] = None
        else:
            out[k] = json.dumps(v)
    return out


def sm_patch(path: str, body: dict):
    """PATCH; retried without nulls for Spoolman before 0.26."""
    try:
        return sm_req("PATCH", path, body)
    except urllib.error.HTTPError as exc:
        ex = body.get("extra")
        if exc.code != 422 or not isinstance(ex, dict) or None not in ex.values():
            raise
        return sm_req("PATCH", path, {**body, "extra": {k: v for k, v in ex.items() if v is not None}})


def spool_save(d: dict) -> dict:
    """Add a spool, or save edits to one, in Spoolman."""
    sid, fid, made = _int(d.get("id"), 0), _int(d.get("fid"), 0), False
    fil = d.get("fil") if isinstance(d.get("fil"), dict) else {}
    sp = d.get("spool") if isinstance(d.get("spool"), dict) else {}
    extra = d.get("extra") if isinstance(d.get("extra"), dict) else {}
    try:
        if extra.get("tag") and "tag" not in {f.get("key") for f in sm_req("GET", "field/spool") or []}:
            sm_req("POST", "field/spool/tag", {"name": "tag", "field_type": "text"})
        if fid <= 0:
            body = {k: v for k, v in _fil_body(fil).items() if v is not None}
            body.setdefault("density", 1.24)
            body.setdefault("diameter", 1.75)
            if not body.get("name"):
                return {"ok": False, "error": "The filament needs a name."}
            fid, made = sm_req("POST", "filament", body)["id"], True
        elif fil:
            sm_req("PATCH", f"filament/{fid}", _fil_body(fil))
        body = {}
        for k in SPOOL_KEYS:
            if k in sp:
                v = sp[k]
                body[k] = (_num_or(v) if k not in ("location", "lot_nr", "comment")
                           else str(v).strip()[:1024] if v not in (None, "") else None)
        if sid:
            raw = (sm_req("GET", f"spool/{sid}") or {}).get("extra") or {}
            if extra:
                body["extra"] = _extra_raw(extra, raw)
            if body:
                sm_patch(f"spool/{sid}", body)
        else:
            body = {k: v for k, v in body.items() if v is not None}
            body["filament_id"] = fid
            if extra:
                body["extra"] = _extra_raw(extra, {})
            sid = sm_req("POST", "spool", body)["id"]
    except Exception as exc:
        log.warning("Saving a spool to Spoolman failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc)}
    tn = d.get("tune") if isinstance(d.get("tune"), dict) else {}
    try:
        if isinstance(tn.get("fil"), dict) and fid:
            tune_save("filament", fid, tn["fil"])
        if isinstance(tn.get("spool"), dict) and sid:
            tune_save("spool", sid, tn["spool"])
    except Exception as exc:
        log.warning("Saving tuning to Spoolman failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc), "id": sid}
    if isinstance(d.get("use"), list):
        ids = [str(i)[:32] for i in d["use"][:8]]

        def put(s):
            c = s["presets"] = parse_presets(s.get("presets"))
            c["use"].pop(str(sid), None)
            if ids:
                c["use"][str(sid)] = ids
        cfg_edit(put)
    _spool_cache["time"] = 0.0
    log.info("Spool #%s %s in Spoolman%s", sid, "saved" if d.get("id") else "added", " (new filament)" if made else "")
    return {"ok": True, "id": sid, "made": made, "added": not d.get("id")}


def new_skip(uid: object) -> None:
    ids = _tag_ids(uid)
    if ids:
        def put(s):
            s["new_skip"] = (list(s.get("new_skip") or []) + sorted(ids))[-200:]
        cfg_edit(put)


def _where(unit_id: int, slot: int) -> str:
    if unit_id == 254:
        return "External Spool"
    if unit_id >= 128:
        return f"AMS HT {unit_id - 127}"
    return f"AMS {unit_id + 1} · Slot {slot + 1}"


PRESET_ID = "spoolio"
NOZZLE_RE = re.compile(r"\s*[(\-]?\s*\d+(?:\.\d+)?\s*(?:mm\s*)?nozzle\)?", re.I)
_pst: dict = {"n": 0, "time": "", "err": "", "skipped": []}
_pmap: dict = {}
_psync = threading.Lock()


def _skip_msg(skipped: list[dict], per: str) -> str:
    if not skipped:
        return ""
    noun = "spool" if per == "spool" else "filament"
    n = len(skipped)
    head = ", ".join(f"{x['name']} ({x['mat']})" for x in skipped[:3])
    more = f" (+{n - 3} more)" if n > 3 else ""
    return (
        f"No OrcaSlicer preset created for {n} {noun}{'' if n == 1 else 's'} "
        f"(no matching OrcaSlicer material): {head}{more}. "
        "Update the material in Spoolman (for example PLA+ to PLA) to include them."
    )


def _post_preset_warn(skipped: list[dict], per: str) -> None:
    try:
        _pwarn(skipped, per)
    except Exception:
        log.exception("Could not show the preset warning")


def _pwarn(skipped: list[dict], per: str) -> None:
    page = _plate["page"]
    if page is not None:
        key = "|".join(f"{x['name']}:{x['mat']}" for x in skipped)
        fids = (
            sorted({x["fid"] for x in skipped if x.get("fid") is not None})
            if per == "filament" else []
        )
        sids = (
            sorted({x["sid"] for x in skipped if x.get("sid") is not None})
            if per == "spool" else []
        )
        page.post_message({
            "type": "preset_warn",
            "msg": _skip_msg(skipped, per),
            "key": key,
            "fids": fids,
            "sids": sids,
        })


def parse_presets(value: object) -> dict:
    v = value if isinstance(value, dict) else {}
    pr = v.get("printers") if isinstance(v.get("printers"), dict) else {}
    bs = v.get("bases") if isinstance(v.get("bases"), dict) else {}
    us = v.get("use") if isinstance(v.get("use"), dict) else {}
    tk = v.get("take") if isinstance(v.get("take"), dict) else {}
    tn = v.get("tune") if isinstance(v.get("tune"), dict) else {}
    tnk = tn.get("keys") if isinstance(tn.get("keys"), dict) else {}
    return {"on": parse_flag(v.get("on"), default=False),
            "per": "spool" if v.get("per") == "spool" else "filament",
            "base": "pick" if v.get("base") == "pick" else "auto",
            "printers": {str(k)[:32]: {"on": parse_flag(x.get("on"), default=False),
                                       "group": str(x.get("group") or "")[:120]}
                         for k, x in list(pr.items())[:8] if isinstance(x, dict)},
            "bases": {str(k)[:24]: str(x)[:120] for k, x in list(bs.items())[:40] if isinstance(x, str)},
            "use": {str(k)[:16]: [str(i)[:32] for i in x[:8]] for k, x in list(us.items())[-500:]
                    if isinstance(x, list) and x},
            "take": {k: parse_flag(tk.get(k)) for k in TAKE},
            "extra_map": parse_flag(v.get("extra_map"), default=False),
            "tune": {"on": parse_flag(tn.get("on"), default=False),
                     "keys": {t[0]: parse_flag(tnk.get(t[0]), default=False) for t in TUNE}}}


def _papi():
    """OrcaSlicer's preset bundle and local bundle reload, or None before OrcaSlicer 2.5.0."""
    h = getattr(orca, "host", None)
    pb = getattr(h, "preset_bundle", None)
    rl = getattr(getattr(getattr(h, "preset", None), "bundles", None), "reload_local", None)
    return (pb(), rl) if callable(pb) and callable(rl) else None


def _reload():
    rl = getattr(getattr(getattr(getattr(orca, "host", None), "preset", None), "bundles", None), "reload_local", None)
    return rl if callable(rl) else None


def _strs(v: object) -> list[str]:
    """A serialized string-list setting, like '"PLA";"PETG"', as a list."""
    if v is None:
        return []
    out = []
    for m in re.findall(r'"((?:[^"\\]|\\.)*)"|([^;]+)', str(v)):
        x = (m[0] or m[1]).strip()
        if x:
            out.append(x)
    return out


def _all(c) -> list:
    return [c.preset(i) for i in range(c.size())]


def _groups(pb) -> dict[str, list[str]]:
    """Printer presets by printer, each with its nozzle sizes, 0.4 first."""
    g: dict[str, list[str]] = {}
    for p in _all(pb.printers):
        if not p.is_default and p.is_visible:
            g.setdefault(NOZZLE_RE.sub("", p.name).strip() or p.name, []).append(p.name)
    return {k: sorted(v, key=lambda n: ("0.4" not in n, n)) for k, v in sorted(g.items())}


def _pk(s: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("carbon", "c"))


def _guess(p: dict, groups: dict) -> str:
    if p.get("orca"):
        for k, names in groups.items():
            if p["orca"] in names:
                return k
    toks = [_pk(t) for t in re.split(r"[\s\-_/]+", p.get("name") or "") if re.search(r"\d", t)]
    hits = [k for k in groups if toks and all(t in _pk(k) for t in toks)]
    return min(hits, key=len) if hits else ""


def _pdir(pb) -> Path:
    root = DATA_DIR.parent / "user"
    for c in (pb.filaments, pb.printers, pb.prints):
        for p in _all(c):
            if p.is_system or p.is_default:
                continue
            try:
                parts = Path(p.file or "").resolve().relative_to(root.resolve()).parts
            except (ValueError, OSError):
                continue
            if len(parts) > 1 and not parts[0].startswith("_"):
                return root / parts[0] / "_local" / PRESET_ID
    return root / "default" / "_local" / PRESET_ID


_pfold: dict = {"d": None}


def _pfind(first: Path) -> Path:
    """The bundle folder in OrcaSlicer's active user folder, checked with list_ids when it can be."""
    if _pfold["d"]:
        return _pfold["d"]
    ids = getattr(getattr(getattr(getattr(orca, "host", None), "preset", None), "bundles", None), "list_ids", None)
    if not callable(ids):
        return first
    root = DATA_DIR.parent / "user"
    try:
        dirs = sorted((x for x in root.iterdir() if x.is_dir() and x.name[:1] not in "_."),
                      key=lambda x: -x.stat().st_mtime)
    except OSError:
        dirs = []
    seen, cands = set(), []
    for d in [first] + [x / "_local" / PRESET_ID for x in dirs] + [root / "default" / "_local" / PRESET_ID]:
        if str(d) not in seen:
            seen.add(str(d))
            cands.append(d)
    meta = json.dumps({"id": PRESET_ID, "name": PLUGIN_NAME, "version": PLUGIN_VERSION})
    for d in cands:
        made = not d.is_dir()
        try:
            _write(d / "bundle_metadata.json", meta)
            ok = PRESET_ID in (ids() or [])
        except Exception as exc:
            log.warning("Couldn't check preset folder %s: %s", d, exc)
            ok = False
        if ok:
            log.info("OrcaSlicer reads presets from %s", d.parent.parent.name)
            _pfold["d"] = d
            return d
        if made:
            shutil.rmtree(d, ignore_errors=True)
    log.warning("OrcaSlicer didn't list Spoolio's bundle in any user folder; using %s", first.parent.parent.name)
    return first


def _perr(exc: Exception) -> str:
    """A reload refusal from OrcaSlicer, in words the user can act on."""
    m = str(exc)
    if "depends on it:" in m:
        return (f"OrcaSlicer preset \"{m.split('depends on it:', 1)[1].strip()}\" is based on a Spoolio preset. "
                "Delete it, or save it without that parent, then select Sync Now.")
    if "modified" in m:
        return "Save or discard your unsaved preset edits in OrcaSlicer, then select Sync Now."
    if "invalid preset" in m:
        return "OrcaSlicer rejected one of Spoolio's presets. Check the base presets for each printer, then select Sync Now."
    if "does not exist" in m:
        return "Spoolio couldn't find OrcaSlicer's user preset folder. Restart OrcaSlicer, then select Sync Now."
    if "not initialized" in m or "FFF printer" in m:
        return "Select a filament printer and its presets in OrcaSlicer, then select Sync Now."
    return f"OrcaSlicer didn't load the presets: {m}"


_bcache: dict = {"n": -1, "v": []}


def _bases(pb) -> list[dict]:
    if _bcache["n"] == pb.filaments.size():
        return _bcache["v"]
    out = []
    for p in _all(pb.filaments):
        if not p.is_system or p.is_default or p.name.startswith("fdm_"):
            continue
        t = _strs(p.config_value("filament_type"))
        v = _strs(p.config_value("filament_vendor"))
        out.append({"n": p.name, "t": t[0].upper() if t else "", "v": v[0].lower() if v else "",
                    "cp": set(_strs(p.config_value("compatible_printers")))})
    _bcache.update(n=pb.filaments.size(), v=out)
    return out


# Workers read this copy, never OrcaSlicer's live presets, which its own thread moves.
_snap: dict = {"ok": False, "groups": {}, "bases": [], "dir": None, "vals": {}, "want": set()}


def _skeys() -> set:
    keys = {t[3] for t in TUNE} | set(_snap["want"])
    if parse_presets(cfg().get("presets"))["extra_map"]:
        for sp in _spool_cache["spools"]:
            for h in (sp, sp.get("filament")) if isinstance(sp, dict) else ():
                if isinstance(h, dict) and isinstance(h.get("extra"), dict):
                    keys.update(str(k) for k in h["extra"])
    return keys - PRESET_RESERVED_KEYS


def psnap() -> None:
    """Run on OrcaSlicer's own thread only (on_message)."""
    api = _papi()
    if not api:
        _snap["ok"] = False
        return
    try:
        pb = api[0]
        bases = _bases(pb)
        keys = _skeys()
        vals = _snap["vals"]
        for b in bases:
            have = vals.setdefault(b["n"], {})
            miss = [k for k in keys if k not in have]
            p = pb.filaments.find_preset(b["n"]) if miss else None
            for k in miss:
                try:
                    have[k] = p.config_value(k) if p else None
                except Exception:
                    have[k] = None
        _snap.update(ok=True, groups=_groups(pb), bases=bases, dir=_pdir(pb), want=set())
    except Exception:
        log.exception("Couldn't read OrcaSlicer's presets")


def _bval(base: str, key: str):
    have = _snap["vals"].get(base) or {}
    if key not in have:
        _snap["want"].add(key)
        return None
    return have[key]


def _base(bases: list[dict], f: dict, names: list[str], pick: str = "") -> str:
    mat = str(f.get("material") or "").upper()
    ven = str((f.get("vendor") or {}).get("name") or "").lower()
    words = [w for w in re.findall(r"[a-z+]{3,}", str(f.get("name") or "").lower()) if w != mat.lower()]
    best, top = "", None
    for b in bases:
        if b["t"] != mat or (b["cp"] and not b["cp"] & set(names)):
            continue
        if pick:
            if b["n"].split("@")[0].strip() == pick:
                return b["n"]
            continue
        s = (4 if b["cp"] else 0) + (3 if ven and b["v"] == ven else 0) + ("generic" in b["n"].lower())
        s += sum(w in b["n"].lower() for w in words)
        if top is None or (s, -len(b["n"])) > top:
            best, top = b["n"], (s, -len(b["n"]))
    return best if best or not pick else _base(bases, f, names)


def _kg_cost(sp: dict, f: dict):
    for price, w in ((sp.get("price"), sp.get("initial_weight")), (f.get("price"), f.get("weight"))):
        p, g = _num_or(price), _num_or(w)
        if p and g:
            return round(p / g * 1000, 2)
    return None


def _pname(name: str) -> str:
    """OrcaSlicer names presets after their file."""
    n = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name)).strip(" .")
    return n or "Filament"


def _in_use(spools: list) -> dict[str, set]:
    """Spool id to the printers it's loaded in, from the Printer tab and Live Link's RFID trays."""
    out: dict[str, set] = {}
    for p in printers_cfg():
        for u in p["units"]:
            for x in u["slots"]:
                if x:
                    out.setdefault(str(x), set()).add(p["id"])
    tags = {t: str(sp.get("id")) for sp in spools if isinstance(sp, dict) for t in spool_tags(sp)}
    if tags:
        with _live_lock:
            for st in _live["printers"].values():
                if st.get("kind") == "net":
                    continue
                trays = [t for u in st["units"].values() for t in u.values()] + ([st["ext"]] if st["ext"] else [])
                for t in trays:
                    for i in _tag_ids(t.get("tray_uuid")):
                        if i in tags and st.get("link"):
                            out.setdefault(tags[i], set()).add(st["link"])
    return out


def build_presets(c: dict, spools: list, only: str = "",
                  skipped: list | None = None) -> list[dict]:
    """The presets for the ticked printers: one per filament, or per spool."""
    groups = _snap["groups"]
    prs = [(p, groups[c["printers"][p["id"]]["group"]]) for p in printers_cfg()
           if c["printers"].get(p["id"], {}).get("on") and c["printers"][p["id"]]["group"] in groups
           and (not only or p["id"] == only)]
    live = [sp for sp in spools if isinstance(sp, dict) and isinstance(sp.get("filament"), dict)
            and not sp.get("archived") and (_num_or(sp.get("remaining_weight"), 1) or 0) > 0]
    picks: dict = {}
    loaded = _in_use(live)
    want: dict = {}
    for sp in sorted(live, key=lambda x: x.get("id") or 0):
        key = sp["id"] if c["per"] == "spool" else sp["filament"].get("id")
        picks.setdefault(key, sp)
        w = set(c["use"].get(str(sp["id"])) or []) or loaded.get(str(sp["id"]))
        if w:
            want.setdefault(key, set()).update(w)
    names: dict = {}
    for sp in picks.values():
        f = sp["filament"]
        n = str(f.get("name") or f.get("material") or "Filament").strip()
        names.setdefault(n, []).append(sp)
    label = {}
    for n, sps in names.items():
        for sp in sps:
            f = sp["filament"]
            x = n
            if len(sps) > 1 and c["per"] == "filament":
                vn = (f.get("vendor") or {}).get("name")
                x = f"{n} ({vn})" if vn and sum((s["filament"].get("vendor") or {}).get("name") == vn for s in sps) == 1 \
                    else f"{n} #{f.get('id')}"
            label[id(sp)] = f"{x} #{sp['id']}" if c["per"] == "spool" else x
    bases = _snap["bases"] if prs else []
    out = []
    used: set = set()
    matched: set = set()
    unmatched: dict = {}
    for p, nz in prs:
        for key, sp in picks.items():
            if key in want and p["id"] not in want[key]:
                continue
            f = sp["filament"]
            name = _pname(f"{label[id(sp)]} @ {p['name']}")
            n = 1
            while name.casefold() in used:
                n += 1
                name = _pname(f"{label[id(sp)]} ({n}) @ {p['name']}")
            base = _base(bases, f, nz, c["bases"].get(str(f.get("material") or "").upper(), "") if c["base"] == "pick" else "")
            if not base:
                if skipped is not None:
                    unmatched.setdefault(key, {
                        "name": label[id(sp)],
                        "mat": str(f.get("material") or "").strip() or "(none)",
                        "fid": f.get("id"),
                        "sid": sp.get("id"),
                    })
                continue
            matched.add(key)
            used.add(name.casefold())
            d = {"type": "filament", "name": name, "from": "User", "version": PLUGIN_VERSION, "instantiation": "true",
                 "inherits": base,
                 "filament_settings_id": [name], "compatible_printers": nz, "compatible_printers_condition": ""}
            tk = c["take"]
            hx = _first_hex(f)
            if hx and tk["colour"]:
                d["default_filament_colour"] = ["#" + hx.upper()]
            ven = (f.get("vendor") or {}).get("name")
            if ven:
                d["filament_vendor"] = [ven]
            for k, keys, on in (("settings_extruder_temp", ("nozzle_temperature", "nozzle_temperature_initial_layer"), tk["temps"]),
                                ("settings_bed_temp", BED_KEYS, tk["temps"]),
                                ("density", ("filament_density",), tk["dens"]), ("diameter", ("filament_diameter",), tk["dens"])):
                v = _num_or(f.get(k))
                if v and on:
                    d.update({x: [str(v)] for x in keys})
            cost = _kg_cost(sp, f) if tk["cost"] else None
            if cost:
                d["filament_cost"] = [str(cost)]
            exp, lvl, tv = {}, {}, {}
            if c["tune"]["on"]:
                tv, lvl = tune_of(sp, c["per"])
                for t in TUNE:
                    if not c["tune"]["keys"][t[0]]:
                        continue
                    v = tv["by"].get(p["name"], tv["pa"]) if t[0] == "pa" else tv[t[0]]
                    if v is not None:
                        d[t[3]] = [str(v)]
                        if t[0] == "pa":
                            d["enable_pressure_advance"] = ["1"]
                    exp[t[0]] = v if v is not None else _nums(_bval(base, t[3]))
            own = {t[3] for t in TUNE if c["tune"]["on"] and c["tune"]["keys"][t[0]]}
            if own & {"pressure_advance"}:
                own.add("enable_pressure_advance")
            extra_applied = _apply_orca_extras(d, sp, f, c["per"], base, own) if c["extra_map"] else {}
            out.append({"file": name + ".json", "json": d, "spool": sp, "printer": p, "cost": cost, "exp": exp,
                        "lvl": lvl, "tv": tv, "extra": extra_applied})
    if skipped is not None:
        skipped.extend(v for k, v in unmatched.items() if k not in matched)
    return out


def presets_sync(spools: list | None = None, force: bool = False) -> dict:
    """Writes Spoolio's local preset bundle and asks OrcaSlicer to reload it when anything changed."""
    # Sync Now waits for a running sync.
    if not (_psync.acquire(timeout=60) if force else _psync.acquire(blocking=False)):
        return dict(_pst)
    try:
        for _ in range(2):
            if _psync_once(spools, force):
                break
        return dict(_pst)
    except Exception as exc:
        log.exception("Preset sync failed")
        _pst.update(err=f"Preset sync failed: {exc}")
        return dict(_pst)
    finally:
        _psync.release()


def _psync_once(spools: list | None, force: bool) -> bool:
    """One sync; False when OrcaSlicer's user folder moved and it's worth another go."""
    c = parse_presets(cfg().get("presets"))
    reload = _reload()
    if not reload or not _snap["ok"]:
        if c["on"] and not reload:
            _pst.update(err="This version of OrcaSlicer can't add presets.")
        _pst["skipped"] = []
        _post_preset_warn([], c["per"])
        return True
    d = _pfind(_snap["dir"]) if c["on"] else (_pfold["d"] or _snap["dir"])
    fd = d / "filament"
    if not c["on"] and not fd.is_dir():
        _pst["skipped"] = []
        _post_preset_warn([], c["per"])
        return True
    files = {}
    skipped: list[dict] = []
    if c["on"] and source() == "spoolman":
        if spools is None:
            url = cfg().get("spoolman_url", "")
            if force or _spool_cache["url"] != url:
                res = get_spools(url)
                if not res.get("ok"):
                    _pst.update(err=res.get("error") or "Could not reach Spoolman.")
                    return True
                spools = res["spools"]
            else:
                spools = _spool_cache["spools"]
        es = build_presets(c, spools, skipped=skipped)
        if _snap["want"]:
            log.info("Preset sync waits for OrcaSlicer's values of %s", ", ".join(sorted(_snap["want"])))
            return True
        files = {e["file"]: json.dumps(e["json"], indent=2) + "\n" for e in es}
        _pmap.clear()
        _pmap.update({e["json"]["name"]: e for e in es})
        if skipped and skipped != _pst.get("skipped"):
            names = ", ".join(f"{x['name']} ({x['mat']})" for x in skipped)
            log.warning("Skipped %d OrcaSlicer preset(s) with no matching base material: %s",
                        len(skipped), names)
    _pst["skipped"] = skipped
    _post_preset_warn(skipped, c["per"])
    old = {p.name: p.read_text(encoding="utf-8") for p in fd.glob("*.json")} if fd.is_dir() else {}
    if files == old and not force and not _pst["err"]:
        return True
    _write(d / "bundle_metadata.json", json.dumps({"id": PRESET_ID, "name": PLUGIN_NAME, "version": PLUGIN_VERSION}))
    for name, text in files.items():
        if old.get(name) != text:
            _write(fd / name, text)
    for name in set(old) - set(files):
        (fd / name).unlink(missing_ok=True)
    fd.mkdir(parents=True, exist_ok=True)
    try:
        reload(PRESET_ID)
    except Exception as exc:
        log.warning("OrcaSlicer didn't reload Spoolio's presets from %s: %s", d, exc)
        _pst.update(err=_perr(exc))
        if "does not exist" in str(exc) and _pfold["d"]:
            _pfold["d"] = None
            return False
        return True
    log.info("Synced %d OrcaSlicer filament presets", len(files))
    _pst.update(n=len(files), time=time.strftime("%H:%M"), err="")
    return True


def presets_info(want: object = None) -> dict:
    """What the OrcaSlicer Presets card shows, for the saved or the unsaved settings."""
    saved = parse_presets(cfg().get("presets"))
    c = parse_presets({**want, "use": saved["use"]}) if isinstance(want, dict) else saved
    out = {"type": "presets_info", "cfg": c, "api": False, "printers": [], "groups": [], "materials": [],
           "status": dict(_pst), "sample": None, "skipped": [], "skip_msg": ""}
    if not _snap["ok"]:
        return out
    groups = _snap["groups"]
    out["api"] = True
    out["groups"] = list(groups)
    for p in printers_cfg():
        s = c["printers"].get(p["id"])
        g = s["group"] if s and s["group"] in groups else _guess(p, groups)
        c["printers"][p["id"]] = {"on": bool(s and s["on"]), "group": g}
        out["printers"].append({"id": p["id"], "name": p["name"], "group": g, "on": bool(s and s["on"]),
                                "nozzles": groups.get(g, [])})
    url = cfg().get("spoolman_url", "")
    spools = _spool_cache["spools"] if _spool_cache["url"] == url and url else []
    mats = sorted({str(sp["filament"].get("material") or "").upper() for sp in spools
                   if isinstance(sp.get("filament"), dict) and sp["filament"].get("material")})
    if c["base"] == "pick" and mats:
        bases = _snap["bases"]
        for m in mats:
            opts = sorted({b["n"].split("@")[0].strip() for b in bases if b["t"] == m})[:80]
            out["materials"].append({"m": m, "opts": opts, "pick": c["bases"].get(m, "")})
    first = next((p for p in out["printers"] if p["on"] and p["group"]), None)
    if first and spools:
        skipped: list[dict] = []
        es = build_presets(c, spools, first["id"], skipped=skipped)
        if c["on"]:
            out["skipped"] = skipped
            out["skip_msg"] = _skip_msg(skipped, c["per"])
        if es:
            e = es[0]
            sp = dict(e["spool"], rfid_tags=sorted(spool_tags(e["spool"])))
            j = e["json"]
            sign = CUR_SIGNS.get(_cur["code"], _cur["code"] + " " if _cur["code"] else "€")
            rows = [["Colour", (j.get("default_filament_colour") or ["From Base Preset"])[0]],
                    ["Type and Vendor", ", ".join(x for x in (str(e["spool"]["filament"].get("material") or ""),
                                                              (j.get("filament_vendor") or [""])[0]) if x) or "-"],
                    ["Nozzle Temperature", _temp_row(j, "nozzle_temperature", "nozzle_temperature_initial_layer")],
                    ["Bed Temperature", _bed_temp_row(j)],
                    ["Density and Diameter", ", ".join(x for x in (
                        f"{j['filament_density'][0]} g/cm³" if "filament_density" in j else "",
                        f"{j['filament_diameter'][0]} mm" if "filament_diameter" in j else "") if x) or "From Base Preset"],
                    ["Cost", f"{sign}{e['cost']:.2f} per kg" if e["cost"] else "From Base Preset"]]
            for t in TUNE:
                if c["tune"]["on"] and c["tune"]["keys"][t[0]]:
                    v = (j.get(t[3]) or [None])[0]
                    by = t[0] == "pa" and first["name"] in e["tv"].get("by", {})
                    txt = f"{v} {t[4]}".strip() + (f" ({first['name']})" if by else "")
                    rows.append([t[2], txt if v else "From Base Preset"])
            for k, vals in (e.get("extra") or {}).items():
                if k in TEMP_PREVIEW_KEYS:
                    continue
                txt = ", ".join(vals)
                rows.append([k, txt if len(txt) <= 60 else txt[:57] + "..."])
            rows.append(["Based On", j["inherits"]])
            out["sample"] = {"spool": sp, "name": j["name"], "printer": first["name"], "base": j["inherits"],
                             "colour": (j.get("default_filament_colour") or [""])[0], "rows": rows,
                             "list": [{"n": x["json"]["name"], "c": (x["json"].get("default_filament_colour") or [""])[0]}
                                      for x in es[:6]], "more": max(0, len(es) - 6)}
    return out


TUNE = (("flow", "filament_flow_ratio", "Flow Ratio", "filament_flow_ratio", ""),
        ("pa", "pressure_advance", "Pressure Advance", "pressure_advance", ""),
        ("mvs", "filament_max_volumetric_speed", "Max Volumetric Speed", "filament_max_volumetric_speed", "mm³/s"),
        ("rlen", "filament_retraction_length", "Retraction Length", "filament_retraction_length", "mm"),
        ("rspd", "filament_retraction_speed", "Retraction Speed", "filament_retraction_speed", "mm/s"))
TUNE_KEYS = {t[1] for t in TUNE}
TUNE_OLD = {"flow": "flow_ratio", "mvs": "max_volumetric_speed", "rlen": "retraction_length", "rspd": "retraction_speed"}
PA_BY = "pressure_advance_printers"
TAKE = ("colour", "temps", "dens", "cost")
PRESET_RESERVED_KEYS = (
    {"type", "name", "from", "version", "instantiation", "inherits",
     "filament_settings_id", "compatible_printers", "compatible_printers_condition", "tag"}
    | set(TUNE_OLD.values()) | {PA_BY}
)
BED_KEYS = tuple(f"{x}_plate_temp{y}" for x in ("supertack", "cool", "textured_cool", "eng", "hot", "textured")
                 for y in ("", "_initial_layer"))
TEMP_PREVIEW_KEYS = {"nozzle_temperature", "nozzle_temperature_initial_layer", *BED_KEYS}
_tune_q: dict = {}


def _temp_row(j: dict, main_key: str, init_key: str) -> str:
    main = (j.get(main_key) or [None])[0]
    init = (j.get(init_key) or [None])[0]
    if main and init and init != main:
        return f"{main} °C (1st layer {init} °C)"
    if main:
        return f"{main} °C"
    if init:
        return f"1st layer {init} °C"
    return "From Base Preset"


def _bed_temp_row(j: dict) -> str:
    main = (j.get("hot_plate_temp") or j.get("textured_plate_temp") or [None])[0]
    init = next((v for k in ("textured_plate_temp_initial_layer", "hot_plate_temp_initial_layer")
                 if (v := (j.get(k) or [None])[0]) and v != main), None)
    if main and init:
        return f"{main} °C (1st layer {init} °C)"
    if main:
        return f"{main} °C"
    init_only = (j.get("hot_plate_temp_initial_layer")
                 or j.get("textured_plate_temp_initial_layer") or [None])[0]
    return f"1st layer {init_only} °C" if init_only else "From Base Preset"


def _extra_orca_val(raw: object) -> list[str] | None:
    """Decode a Spoolman extra value into OrcaSlicer's string-list preset format."""
    if raw is None or raw == "":
        return None
    val = raw
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            val = json.loads(s)
        except ValueError:
            val = s
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return ["1" if val else "0"]
    if isinstance(val, (int, float)):
        if _fin(val) is None:
            return None
        n = int(val) if isinstance(val, float) and val == int(val) else val
        return [str(n)]
    if isinstance(val, list):
        if any(isinstance(x, float) and _fin(x) is None for x in val):
            return None
        items = [str(x) for x in val if x is not None and str(x) != ""]
        return items or None
    if isinstance(val, dict):
        return None
    return [str(val)]


def _fin(x: object):
    try:
        n = float(x)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def _orca_base(base: str, key: str):
    """The base preset's value for key, or None when key isn't an OrcaSlicer filament setting."""
    if not base or not key or key in PRESET_RESERVED_KEYS:
        return None
    return _bval(base, key)


def _orca_fits(base: object, val: list[str]) -> bool:
    """False when the base setting is numeric but val isn't, so one bad value can't spoil the bundle."""
    b = [x.strip() for x in re.split(r"[,;]", str(base).replace('"', "")) if x.strip()]
    if b and all(_fin(x) is not None for x in b):
        return all(_fin(x) is not None for x in val)
    return True


def _apply_orca_extras(d: dict, sp: dict, f: dict, per: str, bp: str, own: set = frozenset()) -> dict[str, list[str]]:
    """Apply Spoolman extra fields whose keys match OrcaSlicer filament preset keys."""
    ex: dict = {}
    if isinstance(f.get("extra"), dict):
        ex.update(f["extra"])
    if per == "spool" and isinstance(sp.get("extra"), dict):
        for k, v in sp["extra"].items():
            if v not in (None, ""):
                ex[k] = v
    applied: dict[str, list[str]] = {}
    for k in sorted(ex):
        if k in own or k in ("filament_id", "setting_id", "base_id"):
            log.warning("Spoolman extra field %s skipped: Spoolio sets this OrcaSlicer setting itself", k)
            continue
        base = _orca_base(bp, k)
        if base is None:
            continue
        val = _extra_orca_val(ex[k])
        if val is None:
            continue
        if not _orca_fits(base, val):
            log.warning("Spoolman extra field %s skipped: %r doesn't suit this OrcaSlicer setting", k, ex[k])
        else:
            d[k] = val
            applied[k] = val
    return applied


def _xv(ex: object, key: str):
    """A Spoolman extra value as a number, or for PA_BY a {printer name: number} map."""
    v = ex.get(key) if isinstance(ex, dict) else None
    for _ in range(2):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                break
    if key == PA_BY:
        return {str(k)[:40]: n for k, x in v.items() if (n := _num_or(x)) is not None} if isinstance(v, dict) else {}
    return _num_or(v) if not isinstance(v, bool) else None


def _tvals(ex: object) -> dict:
    out = {t[0]: _xv(ex, t[1]) for t in TUNE}
    for k, old in TUNE_OLD.items():
        if out[k] is None:
            out[k] = _xv(ex, old)
    out["by"] = _xv(ex, PA_BY)
    return out


def tune_of(sp: dict, per: str) -> tuple[dict, dict]:
    """Tuning for a spool: its own values first (one preset per spool only), then its filament's."""
    f = _tvals((sp.get("filament") or {}).get("extra"))
    s = _tvals(sp.get("extra")) if per == "spool" else {"by": {}}
    lvl = {}
    out = {}
    for t in TUNE:
        k = t[0]
        out[k], lvl[k] = (s[k], "spool") if s.get(k) is not None else (f[k], "fil")
    out["by"] = {**f["by"], **s["by"]}
    lvl["by"] = "spool" if s["by"] else "fil"
    return out, lvl


def _nums(v: object) -> list | None:
    ns = [n for x in re.split(r"[,;]", str(v if v is not None else "").replace('"', ""))
          if (n := _num_or(x.strip())) is not None]
    return ns or None


def _cells(v: object) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", str(v if v is not None else "").replace('"', ""))]


def _n1(v: object):
    return _num_or(re.split(r"[,;]", str(v if v is not None else "").replace('"', ""))[0].strip())


def _tune_fields(ent: str, keys: list[str]) -> None:
    have = {f.get("key") for f in sm_req("GET", f"field/{ent}") or [] if isinstance(f, dict)}
    names = {t[1]: t[2] for t in TUNE}
    names[PA_BY] = "Pressure Advance by Printer"
    for k in keys:
        if k not in have:
            sm_req("POST", f"field/{ent}/{k}", {"name": names[k], "field_type": "text" if k == PA_BY else "float"})


def tune_save(ent: str, eid: int, vals: dict) -> None:
    """Writes tuning values to a Spoolman filament or spool; an empty value removes it."""
    vals = {k: v for k, v in vals.items() if k in TUNE_KEYS or k == PA_BY}
    if not vals or not eid:
        return
    sent = {}
    for k, v in vals.items():
        if k == PA_BY:
            m = {str(p)[:40]: n for p, x in (v or {}).items() if (n := _num_or(x)) is not None} if isinstance(v, dict) else {}
            sent[k] = json.dumps(m) if m else ""
        else:
            n = _num_or(v)
            sent[k] = n if n is not None else ""
    _tune_fields(ent, [k for k, v in sent.items() if v != ""])
    raw = (sm_req("GET", f"{ent}/{eid}") or {}).get("extra") or {}
    sm_patch(f"{ent}/{eid}", {"extra": _extra_raw(sent, raw)})
    _spool_cache["time"] = 0.0


def tune_orca() -> dict:
    """Flow ratio and the rest from the filament preset being edited in OrcaSlicer."""
    api = _papi()
    if not api:
        return {"ok": False, "error": "This version of OrcaSlicer can't share its presets."}
    pb = api[0]
    try:
        p = pb.filaments.edited_preset()
        pr = pb.printers.selected_preset_name()
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't read OrcaSlicer's filament preset: {exc}"}
    vals = {t[0]: _n1(p.config_value(t[3])) for t in TUNE}
    groups = _groups(pb)
    c = parse_presets(cfg().get("presets"))
    who = next((x["name"] for x in printers_cfg()
                if pr in groups.get(c["printers"].get(x["id"], {}).get("group") or _guess(x, groups), [])), "")
    name = p.name.split("/")[-1] if p.name.startswith("_local/") else p.name
    return {"ok": True, "vals": vals, "printer": who, "preset": name}


def _tune_item(src: str, sp: dict, p: dict, k: str, old, new, lvl: str) -> dict | None:
    olds = [x for x in (old if isinstance(old, list) else [old]) if x is not None]
    if new is None or any(abs(float(new) - float(x)) < 1e-6 for x in olds):
        return None
    old = olds[0] if olds else None
    f = sp.get("filament") or {}
    iid = f"{f.get('id')}|{sp.get('id') if lvl == 'spool' else ''}|{p['id']}|{k}|{new}"
    if iid in (cfg().get("tune_skip") or []):
        return None
    lab = next(t[2] for t in TUNE if t[0] == k)
    return {"id": iid, "src": src, "fid": f.get("id"), "sid": sp.get("id"), "lvl": lvl, "k": k, "label": lab,
            "pid": p["id"], "printer": p["name"], "name": str(f.get("name") or f"Spool #{sp.get('id')}"),
            "c": _first_hex(f), "old": old, "new": new}


def tune_check(ctx) -> None:
    """After a slice: offers to save tuning values that differ from what Spoolio put in its preset."""
    c = parse_presets(cfg().get("presets"))
    if not (c["on"] and c["tune"]["on"] and _pmap):
        return
    names = split_list(ctx.config_value("filament_settings_id"))
    keys = [t for t in TUNE if c["tune"]["keys"][t[0]]]
    cols = {t[0]: _cells(ctx.config_value(t[3])) for t in keys}
    # H2D/H2C: one value per filament variant.
    owner = [_int(x, 0) for x in _cells(ctx.config_value("filament_self_index"))]
    owner = owner if all(o > 0 for o in owner) else []
    new = 0
    for i, n in enumerate(names):
        e = _pmap.get(n.split("/", 2)[-1] if n.startswith("_local/") else n)
        if not e:
            continue
        for t in keys:
            col = cols[t[0]]
            vals = [col[j] for j, o in enumerate(owner) if o == i + 1] if owner and len(col) == len(owner) else \
                [col[i]] if i < len(col) else []
            exp = e["exp"].get(t[0])
            exps = exp if isinstance(exp, list) else [exp]
            v = next((x for x in (_num_or(y.strip()) for y in vals)
                      if x is not None and not any(o is not None and abs(x - o) < 1e-6 for o in exps)), None)
            it = _tune_item("slice", e["spool"], e["printer"], t[0], e["exp"].get(t[0]), v,
                            "fil" if t[0] == "pa" else e["lvl"].get(t[0], "fil"))
            if it and it["id"] not in _tune_q:
                _tune_q[it["id"]] = it
                new += 1
    if new:
        _bg(_tune_push)


def _tune_push() -> None:
    time.sleep(NOTICE_DELAY)
    page = _plate["page"]
    if page is not None:
        page.post_message({"type": "tune", "items": tune_items()})


def live_tune() -> list[dict]:
    """Bambu AMS trays whose calibrated pressure advance (K) differs from Spoolman's."""
    c = parse_presets(cfg().get("presets"))
    url = cfg().get("spoolman_url", "")
    if not (c["on"] and c["tune"]["on"] and c["tune"]["keys"]["pa"]) or _spool_cache["url"] != url:
        return []
    tags = {t: sp for sp in _spool_cache["spools"] if isinstance(sp, dict) and not sp.get("archived")
            for t in spool_tags(sp)}
    prs = {p["id"]: p for p in printers_cfg()}
    out = []
    with _live_lock:
        states = [dict(st) for st in _live["printers"].values() if st.get("kind") != "net" and st.get("link") in prs]
    for st in states:
        trays = [t for u in st["units"].values() for t in u.values()] + ([st["ext"]] if st["ext"] else [])
        for t in trays:
            if _int(t.get("cali_idx"), -1) < 0:
                continue
            sp = next((tags[i] for i in _tag_ids(t.get("tray_uuid")) if i in tags), None)
            k = _num_or(t.get("k"))
            if not sp or k is None:
                continue
            p = prs[st["link"]]
            tv, _ = tune_of(sp, c["per"])
            it = _tune_item("printer", sp, p, "pa", tv["by"].get(p["name"], tv["pa"]), k, "fil")
            if it:
                out.append(it)
    return out


def _tnew(k: str) -> str:
    return next(t[1] for t in TUNE if t[0] == k)


def _tm_name(ent: str, e: dict) -> str:
    f = e.get("filament") if ent == "spool" else e
    f = f if isinstance(f, dict) else {}
    n = " ".join(x for x in ((f.get("vendor") or {}).get("name"), f.get("name") or f.get("material")) if x) or "Filament"
    return f"Spool #{e.get('id')} · {n}" if ent == "spool" else n


def tune_move_scan() -> dict:
    """Tuning values still under Spoolio's old Spoolman field names, and any clashes with the OrcaSlicer names."""
    rows = {k: {"k": k, "old": o, "new": _tnew(k), "fil": 0, "spool": 0}
            for k, o in TUNE_OLD.items()}
    moves, clash, fields = [], [], []
    kept = set(cfg().get("tune_kept") or [])
    for ent in ("filament", "spool"):
        have = {f.get("key") for f in sm_req("GET", f"field/{ent}") or [] if isinstance(f, dict)}
        fields += [{"ent": ent, "key": o} for o in TUNE_OLD.values() if o in have]
        for e in sm_req("GET", "spool?allow_archived=true" if ent == "spool" else ent) or []:
            ex = e.get("extra") if isinstance(e, dict) else None
            if not isinstance(ex, dict):
                continue
            for k, o in TUNE_OLD.items():
                ov = _xv(ex, o)
                if ov is None:
                    continue
                r = rows[k]
                nv = _xv(ex, r["new"])
                if nv is None:
                    moves.append({"ent": ent, "id": e.get("id"), "k": k, "v": ov})
                elif abs(float(nv) - float(ov)) > 1e-9 and f"{ent}:{e.get('id')}:{k}:{ov}:{nv}" not in kept:
                    clash.append({"id": f"{ent}:{e.get('id')}:{k}", "ent": ent, "eid": e.get("id"), "k": k,
                                  "name": _tm_name(ent, e), "label": next(t[2] for t in TUNE if t[0] == k),
                                  "old": o, "new": r["new"], "ov": ov, "nv": nv,
                                  "c": _first_hex(e.get("filament") if ent == "spool" else e)})
                else:
                    continue
                r["fil" if ent == "filament" else "spool"] += 1
    return {"ok": True, "rows": [r for r in rows.values() if r["fil"] or r["spool"]], "moves": moves,
            "clash": clash, "n": len(moves) + len(clash), "fields": fields}


def tune_move_apply(pick: object) -> dict:
    """Copies old-named tuning values into the OrcaSlicer-named fields; clashes follow the user's pick."""
    pick = pick if isinstance(pick, dict) else {}
    try:
        sc = tune_move_scan()
        todo: dict = {}
        for m in sc["moves"]:
            todo.setdefault((m["ent"], m["id"]), {})[_tnew(m["k"])] = m["v"]
        kept = []
        for x in sc["clash"]:
            if pick.get(x["id"], "old") == "old":
                todo.setdefault((x["ent"], x["eid"]), {})[x["new"]] = x["ov"]
            else:
                kept.append(f"{x['id']}:{x['ov']}:{x['nv']}")
        if kept:
            cfg_edit(lambda s: s.update(tune_kept=(list(s.get("tune_kept") or []) + kept)[-200:]))
        for (ent, eid), vals in todo.items():
            tune_save(ent, eid, vals)
        cfg_edit(lambda s: s.update(tune_move="done"))
        left = tune_move_scan()
    except Exception as exc:
        log.warning("Moving tuning values failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc)}
    n = sum(len(v) for v in todo.values())
    log.info("Moved %d tuning values to the OrcaSlicer field names", n)
    return {"ok": True, "n": n, "fields": left["fields"] if not left["n"] else [], "left": left["n"]}


def tune_move_delete() -> dict:
    """Deletes Spoolio's old tuning fields from Spoolman, only once every value is in the new fields."""
    try:
        sc = tune_move_scan()
        if sc["n"]:
            return {"ok": False, "error": "Some values haven't been moved yet, so the old fields were kept."}
        for f in sc["fields"]:
            sm_req("DELETE", f"field/{f['ent']}/{f['key']}")
    except Exception as exc:
        log.warning("Deleting the old tuning fields failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc)}
    _spool_cache["time"] = 0.0
    log.info("Deleted old tuning fields: %s", ", ".join(f"{f['ent']}.{f['key']}" for f in sc["fields"]))
    return {"ok": True, "n": len(sc["fields"])}


def tune_move_state(auto: bool) -> dict | None:
    c = parse_presets(cfg().get("presets"))
    if source() != "spoolman" or not (c["on"] and c["tune"]["on"]):
        return {"ok": True, "n": 0, "fields": [], "rows": [], "clash": [], "off": True}
    if auto and cfg().get("tune_move"):
        return None
    try:
        sc = tune_move_scan()
    except Exception as exc:
        log.warning("Checking for old tuning fields failed: %s", exc)
        return None
    sc.pop("moves")
    return sc


def tune_items() -> list[dict]:
    seen = set(cfg().get("tune_skip") or [])
    items = [x for x in _tune_q.values() if x["id"] not in seen]
    ids = {x["id"] for x in items}
    return (items + [x for x in live_tune() if x["id"] not in ids])[:12]


def tune_apply(ids: object, save: bool) -> dict:
    """Update Spoolman with the chosen new tuning values, or stop asking about them."""
    want = set(ids) if isinstance(ids, list) else set()
    items = [x for x in tune_items() if x["id"] in want]
    if not save:
        cfg_edit(lambda s: s.update(tune_skip=(list(s.get("tune_skip") or []) + [x["id"] for x in items])[-300:]))
        for x in items:
            _tune_q.pop(x["id"], None)
        return {"ok": True}
    try:
        for x in items:
            if x["k"] == "pa":
                ent, eid = "filament", x["fid"]
                # Per spool, the spool's own value wins.
                if x.get("sid") and parse_presets(cfg().get("presets"))["per"] == "spool":
                    sby = _xv((sm_req("GET", f"spool/{x['sid']}") or {}).get("extra"), PA_BY)
                    if x["printer"] in sby:
                        ent, eid = "spool", x["sid"]
                by = _xv((sm_req("GET", f"{ent}/{eid}") or {}).get("extra"), PA_BY)
                by[x["printer"]] = x["new"]
                tune_save(ent, eid, {PA_BY: by})
            else:
                key = next(t[1] for t in TUNE if t[0] == x["k"])
                tune_save("spool" if x["lvl"] == "spool" else "filament",
                          x["sid"] if x["lvl"] == "spool" else x["fid"], {key: x["new"]})
            _tune_q.pop(x["id"], None)
    except Exception as exc:
        log.warning("Saving tuning to Spoolman failed: %s", exc)
        return {"ok": False, "error": _sm_err(exc)}
    log.info("Saved %d tuning values to Spoolman", len(items))
    return {"ok": True, "n": len(items)}


def new_spools() -> list[dict]:
    """Bambu RFID spools in a live printer that no Spoolman spool has the tag of."""
    url = cfg().get("spoolman_url", "")
    if source() != "spoolman" or not url or _spool_cache["url"] != url:
        return []
    known = set()
    for sp in _spool_cache["spools"]:
        known |= spool_tags(sp)
    skip = set(cfg().get("new_skip") or [])
    out = []
    with _live_lock:
        for dev_id, st in _live["printers"].items():
            if st.get("kind") == "net":
                continue
            trays = [(u, i, t) for u in sorted(st["units"]) for i, t in sorted(st["units"][u].items())]
            if st["ext"]:
                trays.append((254, 0, st["ext"]))
            for unit_id, slot, t in trays:
                ids = _tag_ids(t.get("tray_uuid"))
                if not ids or ids & known or ids & skip or not str(t.get("tray_type") or "").strip():
                    continue
                lo, hi = _int(t.get("nozzle_temp_min"), 0), _int(t.get("nozzle_temp_max"), 0)
                remain = t.get("remain")
                out.append({"uid": sorted(ids)[0], "printer": st["name"], "where": _where(unit_id, slot),
                            "unit": "AMS HT" if 128 <= unit_id < 254 else f"AMS {unit_id + 1}" if unit_id < 128 else "",
                            "name": str(t.get("tray_sub_brands") or t.get("tray_type")).strip(),
                            "m": str(t.get("tray_type")).strip(), "c": _hex6(t.get("tray_color")),
                            "pct": remain if isinstance(remain, (int, float)) and 0 <= remain <= 100 else None,
                            "w": _int(t.get("tray_weight"), 0) or 1000,
                            "dia": _num_or(t.get("tray_diameter"), 1.75) or 1.75,
                            "e": round((lo + hi) / 2) if lo and hi else (hi or None),
                            "b": _int(t.get("bed_temp"), 0) or None})
    return out


_booking: set = set()


def _claim(entry_id: str, ok) -> dict | None:
    # Marked busy so a second click can't book it twice.
    with _usage_lock:
        entry = next((e for e in usage_load()["prints"] if e.get("id") == entry_id), None)
        if entry is None or entry_id in _booking or not ok(entry):
            return None
        _booking.add(entry_id)
        return entry


def _settle(entry_id: str, done: dict, by_id: dict, final) -> None:
    with _usage_lock:
        _booking.discard(entry_id)
        data = usage_load()
        entry = next((e for e in data["prints"] if e.get("id") == entry_id), None)
        if entry is None:
            log.warning("A print left the booking list while Spoolman was being updated")
            return
        for row in entry["rows"]:
            row.update(done.get(row["slot"], {}))
        final(entry)
        usage_save(data)
        hist_note(entry, by_id)


def usage_book(entry_id: str, chosen: list | None) -> None:
    """Book the open rows to their spools; chosen holds manual picks."""
    wanted = {}
    for item in chosen if isinstance(chosen, list) else []:
        if isinstance(item, dict):
            wanted[_int(item.get("slot"))] = item
    url = clean_url(cfg().get("spoolman_url"))
    by_id = {sp.get("id"): sp for sp in (plate_spools(url) if url else None) or []}
    entry = _claim(entry_id, lambda e: True)
    if entry is None:
        return
    done = {}
    try:
        for row in entry["rows"]:
            if row["state"] == "booked":
                continue
            pick = wanted.get(row["slot"], {})
            spool = _int(pick.get("spool")) if pick.get("spool") not in (None, "") else row["spool"]
            try:
                grams = float(pick["grams"]) if "grams" in pick else row["grams"]
            except (TypeError, ValueError):
                grams = 0.0
            if spool in (None, -1) or not 0 < grams <= 5000:
                continue
            try:
                spoolman_use(spool, grams)
            except Exception as exc:
                done[row["slot"]] = {"state": "error", "error": _spoolman_error(exc)}
                continue
            label = _spool_label(by_id[spool]) if spool in by_id else row.get("label") or f"spool {spool}"
            done[row["slot"]] = {"state": "booked", "error": "", "spool": spool, "grams": round(grams, 1), "label": label}
            log.info("Booked %.1f g to Spoolman spool %s", grams, spool)
    finally:
        _settle(entry_id, done, by_id, lambda e: e.update(
            state="booked" if all(r["state"] == "booked" for r in e["rows"]) else "pending"))


def usage_undo(entry_id: str) -> None:
    """Add a booked print's grams back to its spools."""
    entry = _claim(entry_id, lambda e: e.get("state") == "booked")
    if entry is None:
        return
    done = {}
    try:
        for row in entry["rows"]:
            if row["state"] != "booked":
                continue
            try:
                spoolman_unuse(row["spool"], row["grams"])
            except Exception as exc:
                done[row["slot"]] = {"error": _spoolman_error(exc)}
                continue
            done[row["slot"]] = {"state": "undone", "error": ""}
            log.info("Added %.1f g back to Spoolman spool %s", row["grams"], row["spool"])
    finally:
        _settle(entry_id, done, {}, lambda e: None if any(r["state"] == "booked" for r in e["rows"])
                else e.update(state="undone"))


def usage_action(data: dict) -> dict:
    kind = data.get("type")
    if kind == "usage_mode":
        mode = data.get("mode") if data.get("mode") in USAGE_MODES else "ask"
        cfg_edit(lambda s: s.update(book_usage=mode, **({"stats_when_off": data["stats"] is True} if "stats" in data else {})))
        _local_sync()
    elif kind == "usage_book":
        usage_book(str(data.get("run") or ""), data.get("rows"))
    elif kind == "usage_undo":
        usage_undo(str(data.get("run") or ""))
    return usage_state()


HISTORY_FILE = DATA_DIR / "history.json"
CUR_SIGNS = {"GBP": "£", "EUR": "€", "USD": "$"}
PERIODS = {"30": 30, "90": 90, "year": 0, "all": -1}
_cur = {"url": "", "code": "", "time": 0.0}


def hist_load() -> list[dict]:
    try:
        data = _read(HISTORY_FILE)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.exception("Could not read %s", HISTORY_FILE)
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _hex(sp: dict) -> str:
    f = sp.get("filament") or {}
    cs = [c for c in (_hex6(x) for x in str(f.get("multi_color_hexes") or "").split(",")) if c]
    return (cs[0] if len(cs) > 1 else _hex6(f.get("color_hex"))) or ""


def _cost(sp: dict, grams: float) -> float | None:
    f = sp.get("filament") or {}
    price = sp.get("price") if isinstance(sp.get("price"), (int, float)) else f.get("price")
    wt = sp.get("initial_weight") or f.get("weight")
    if not isinstance(price, (int, float)) or not isinstance(wt, (int, float)) or wt <= 0:
        return None
    return round(price / wt * grams, 4)


def hist_note(entry: dict, by_id: dict) -> None:
    """Keep the uncapped history in step with a print's booked rows. Holds _usage_lock."""
    hist = hist_load()
    old = next((e for e in hist if e.get("id") == entry["id"]), None)
    prev = {r.get("spool"): r for r in (old or {}).get("rows") or []}
    rows = []
    nb = bool(entry.get("nb"))
    for row in entry["rows"]:
        if row["state"] != "booked" and not nb:
            continue
        sp, p = by_id.get(row["spool"]), prev.get(row["spool"]) or {}
        if sp:
            f = sp.get("filament") or {}
            p = {"label": row.get("label") or _spool_label(sp), "material": f.get("material") or row.get("material") or "",
                 "colour": _hex(sp) or _hex6(row.get("colour")), "cost": _cost(sp, row["grams"])}
        rows.append({"spool": row["spool"], "grams": row["grams"],
                     "label": p.get("label") or row.get("label") or row.get("preset") or "",
                     "material": p.get("material") or row.get("material") or "",
                     "colour": p.get("colour") or _hex6(row.get("colour")), "cost": p.get("cost"),
                     **{k: row[k] for k in ("ws", "wp") if isinstance(row.get(k), (int, float))}})
    hist = [e for e in hist if e.get("id") != entry["id"]]
    if rows:
        hist.append({"id": entry["id"], "time": entry.get("time") or time.time(), "name": entry.get("name") or "",
                     "printer": entry.get("printer") or "", "result": entry.get("result") or "",
                     "pct": entry.get("pct"), "rows": rows, **({"cal": True} if entry.get("cal") else {}),
                     **({"nb": True} if nb else {})})
        hist.sort(key=lambda e: e.get("time") or 0)
    elif old is None:
        return
    try:
        _write(HISTORY_FILE, json.dumps(hist))
    except OSError:
        log.exception("Could not write %s", HISTORY_FILE)


def spoolman_currency(url: str) -> str:
    """Spoolman's own currency setting, cached; EUR is Spoolman's default."""
    if _cur["url"] == url and time.monotonic() - _cur["time"] < CACHE_SECS * 5:
        return _cur["code"]
    code = "EUR"
    try:
        with urllib.request.urlopen(f"{url}/api/v1/setting/currency", timeout=TIMEOUT) as resp:
            val = json.loads(resp.read().decode("utf-8")).get("value")
        val = json.loads(val) if isinstance(val, str) and val.startswith('"') else val
        if isinstance(val, str) and re.fullmatch(r"[A-Za-z]{3}", val.strip()):
            code = val.strip().upper()
    except Exception as exc:
        log.info("Could not read the currency from Spoolman: %s", exc)
    _cur.update(url=url, code=code, time=time.monotonic())
    return code


def _hkey(r: dict) -> object:
    return r["spool"] if r.get("spool") is not None else "?" + (r.get("label") or r.get("material") or "")


def stats_data(period: object) -> dict:
    st = stats_settings()
    url = clean_url(cfg().get("spoolman_url"))
    code = spoolman_currency(url) if st["cur"] == "spoolman" and url else st["cur"].upper()
    if st["cur"] == "spoolman" and not url:
        code = "EUR"
    with _usage_lock:
        hist = hist_load()
    now = time.time()
    lt = time.localtime(now)
    days = PERIODS.get(str(period), 30)
    if days > 0:
        start = now - days * 86400
    elif days == 0:
        start = time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, -1))
    else:
        start = 0.0
    sel = [e for e in hist if (e.get("time") or 0) >= start]
    total = sum(r["grams"] for e in sel for r in e["rows"])
    costs = [r["cost"] for e in sel for r in e["rows"] if isinstance(r.get("cost"), (int, float))]
    mats: dict = {}
    top: dict = {}
    for e in sel:
        for r in e["rows"]:
            m = (r.get("material") or "Other").strip() or "Other"
            mats[m] = mats.get(m, 0) + r["grams"]
            t = top.setdefault(_hkey(r), {"label": r["label"], "colour": r["colour"], "g": 0})
            t["g"] += r["grams"]
    week0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 0, 0, 0, 0, 0, -1))
    weeks = []
    for i in range(7, -1, -1):
        a = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday - 7 * i, 0, 0, 0, 0, 0, -1))
        b = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday - 7 * i + 7, 0, 0, 0, 0, 0, -1))
        parts: dict = {}
        for e in hist:
            if a <= (e.get("time") or 0) < b:
                for r in e["rows"]:
                    k = parts.setdefault(_hkey(r), {"label": r["label"], "colour": r["colour"], "g": 0})
                    k["g"] += r["grams"]
        weeks.append({"start": a, "now": a == week0,
                      "parts": sorted(parts.values(), key=lambda x: -x["g"])})
    low = parse_low(cfg().get("low_stock_grams"))
    rate: dict = {}
    for e in hist:
        if (e.get("time") or 0) >= now - 28 * 86400 and not e.get("nb"):
            for r in e["rows"]:
                rate[r["spool"]] = rate.get(r["spool"], 0) + r["grams"] / 4
    out, nlow = [], 0
    for sp in (plate_spools(url) if url else None) or []:
        left = sp.get("remaining_weight")
        if sp.get("archived") or not isinstance(left, (int, float)):
            continue
        is_low = left < low
        nlow += is_low
        wk = left / rate[sp.get("id")] if rate.get(sp.get("id")) else None
        if is_low or (wk is not None and wk <= 8):
            f = sp.get("filament") or {}
            full = sp.get("initial_weight") or f.get("weight") or 0
            out.append({"label": _spool_label(sp), "colour": _hex(sp), "left": left, "low": is_low,
                        "weeks": round(wk, 1) if wk is not None else None,
                        "pct": max(0, min(100, left / full * 100)) if full else None})
    out.sort(key=lambda x: (not x["low"], x["weeks"] if x["weeks"] is not None else 99, x["left"]))
    ws = {"model": 0.0, "support": 0.0, "purge": 0.0, "cal": 0.0, "cost": 0.0, "mcost": 0.0, "scost": 0.0, "pcost": 0.0,
          "ccost": 0.0, "prints": 0}
    worst = []
    for e in sel:
        if e.get("cal") or is_cal(e.get("name") or ""):
            ws["prints"] += 1
            ws["cal"] += sum(r["grams"] for r in e["rows"])
            ws["ccost"] += sum(r["cost"] for r in e["rows"] if isinstance(r.get("cost"), (int, float)))
            continue
        rows = [r for r in e["rows"] if "ws" in r]
        if not rows:
            continue
        ws["prints"] += 1
        g = c = sg = pg = 0.0
        for r in rows:
            s_, p_ = r["grams"] * r["ws"], r["grams"] * r["wp"]
            ws["support"] += s_
            ws["purge"] += p_
            ws["model"] += r["grams"] - s_ - p_
            sg, pg = sg + s_, pg + p_
            g += s_ + p_
            if isinstance(r.get("cost"), (int, float)):
                ws["scost"] += r["cost"] * r["ws"]
                ws["pcost"] += r["cost"] * r["wp"]
                ws["mcost"] += r["cost"] * (1 - r["ws"] - r["wp"])
                c += r["cost"] * (r["ws"] + r["wp"])
        if g > 0:
            worst.append({"name": e["name"], "g": round(g, 1), "cost": round(c, 2), "kind": "support" if sg >= pg else "purge"})
    ws["cost"] = ws["scost"] + ws["pcost"]
    ws = {k: round(v, 2) if isinstance(v, float) else v for k, v in ws.items()}
    return {"period": str(period), "since": hist[0]["time"] if hist else None, "code": code,
            "sign": CUR_SIGNS.get(code, code + " "), "grams": round(total, 1), "prints": len(sel),
            "cost": round(sum(costs), 2) if costs else None, "low": nlow,
            "mats": sorted(({"name": k, "g": round(v, 1)} for k, v in mats.items()), key=lambda x: -x["g"]),
            "top": sorted(top.values(), key=lambda x: -x["g"])[:5], "weeks": weeks, "soon": out[:5],
            "waste": ws if ws["prints"] else None, "worst": sorted(worst, key=lambda x: -x["g"])[:3],
            "history": [{"time": e["time"], "name": e["name"], "printer": e["printer"], "result": e["result"],
                         "pct": e.get("pct"), "rows": e["rows"], "nb": bool(e.get("nb")),
                         "cal": bool(e.get("cal") or is_cal(e.get("name") or ""))}
                        for e in reversed(sel)]}


def is_newer(latest: str, current: str) -> bool:
    def parts(version):
        return tuple(int(n) for n in re.findall(r"\d+", version)[:3])

    return parts(latest) > parts(current)


def get_release() -> dict:
    """Return ``{"ok": True, "version": ..., "url": ...}`` or ``{"ok": False, "error": ...}``."""
    request = urllib.request.Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Spoolio/{PLUGIN_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            error = "No published release found yet"
        else:
            error = f"GitHub returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        error = f"Could not reach GitHub ({exc.reason})"
    except Exception as exc:
        log.exception("Update check failed")
        return {"ok": False, "error": str(exc)}
    else:
        version = str(data.get("tag_name", "")).lstrip("v")
        log.info("Latest release is %s", version)
        return {"ok": True, "version": version, "url": data.get("html_url", "")}
    log.warning("Update check failed: %s", error)
    return {"ok": False, "error": error}


def open_url(url: str) -> bool:
    log.info("Opening %s", url)
    try:
        if webbrowser.open(url):
            return True
        # webbrowser reports "no browser available" (common on Linux) by returning
        # False rather than raising.
        log.warning("No web browser could be launched for %s", url)
        message = f"No web browser could be launched. Open this address yourself:\n{url}"
    except Exception as exc:
        log.exception("Could not open the browser")
        message = f"Could not open the browser: {exc}"
    orca.host.ui.message(message, title=PLUGIN_NAME, icon="error")
    return False


def open_search(query: object) -> None:
    if isinstance(query, str) and query.strip():
        terms = urllib.parse.quote_plus(query.strip()[:MAX_QUERY])
        open_url(SEARCH_URL.format(query=terms))


def read_tail(path: str) -> list[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        return handle.read().decode("utf-8", "replace").splitlines()


def parse_usage(lines: list[str]) -> list[float]:
    """Grams per filament slot, from the last ``; filament used [g] = ...`` footer line."""
    for line in reversed(lines):
        if line.startswith("; filament used [g] ="):
            return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", line.split("=", 1)[1])]
    return []


def split_list(value: object) -> list[str]:
    """Split a slicer list such as ``"a";"b"`` or ``#000;#fff`` into its items."""
    text = "" if value is None else str(value)
    if '"' in text:
        return [quoted or plain.strip() for quoted, plain in re.findall(r'"([^"]*)"|([^;]+)', text)]
    return [item.strip() for item in text.split(";")]


def plate_slots(ctx, grams: list[float]) -> list[dict]:
    """Every filament slot the slicer reports, with how much of it this plate uses."""
    names = split_list(ctx.config_value("filament_settings_id"))
    colours = split_list(ctx.config_value("filament_colour"))
    materials = split_list(ctx.config_value("filament_type"))
    vendors = split_list(ctx.config_value("filament_vendor"))
    ams = "bambu" in str(ctx.config_value("printer_model") or "").lower()

    def at(items: list[str], i: int) -> str:
        return items[i] if i < len(items) else ""

    return [
        {"n": i + 1, "grams": used, "preset": at(names, i), "colour": at(colours, i),
         "material": at(materials, i), "vendor": at(vendors, i), "ams": ams}
        for i, used in enumerate(grams)
    ]


def colour_diff(first: str, second: str) -> float | None:
    def rgb(value):
        digits = (value or "").strip().lstrip("#")[:6]
        if not re.fullmatch(r"[0-9a-fA-F]{6}", digits):
            return None
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))

    a, b = rgb(first), rgb(second)
    return None if a is None or b is None else sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9+]", "", str(text or "").lower())


def _same_mat(slicer: object, spool: object) -> bool:
    """Equal, or one is a variant of the other ("PLA" and "PLA Matte" or "PLA+")."""
    first, second = _norm(slicer), _norm(spool)
    return bool(first) and bool(second) and (first.startswith(second) or second.startswith(first))


def match_spools(slot: dict, spools: list[dict]) -> list[dict]:
    """Spools that could be what is loaded in a slot: same vendor and material, close colour."""
    wants_support = "support" in slot["preset"].lower()
    vendor = _norm(slot["vendor"])
    found = []
    for spool in spools:
        filament = spool.get("filament") or {}
        if spool.get("archived") or not isinstance(spool.get("remaining_weight"), (int, float)):
            continue
        if ("support" in str(filament.get("name", "")).lower()) != wants_support:
            continue
        if vendor and vendor != "generic":
            theirs = _norm((filament.get("vendor") or {}).get("name"))
            if not theirs or (vendor not in theirs and theirs not in vendor):
                continue
        if not _same_mat(slot["material"], filament.get("material")):
            continue
        distance = colour_diff(slot["colour"], filament.get("color_hex"))
        if distance is not None and distance <= COLOUR_TOLERANCE:
            exact = _norm(slot["material"]) == _norm(filament.get("material"))
            found.append((distance, exact, spool))
    # An exact colour beats a near one, and an exact material beats a variant.
    pool = [item for item in found if item[0] <= 1] or found
    return [spool for *_, spool in [item for item in pool if item[1]] or pool]


def group_slots(slots: list[dict]) -> list[dict]:
    """The groups of slots holding the same filament that this plate uses.

    The AMS moves on to another spool of the same filament when one runs out, so a group is
    one supply: its use is added together and compared with its spools' combined weight.
    """
    groups: dict[tuple, dict] = {}
    for slot in slots:
        key = (_norm(slot["preset"]), _norm(slot["vendor"]), _norm(slot["material"]),
               slot["colour"].lstrip("#").upper()[:6])
        group = groups.setdefault(key, {"slot": slot, "pool": 0, "used": []})
        group["pool"] += 1
        if slot["grams"] > 0:
            group["used"].append(slot)
    return [group for group in groups.values() if group["used"]]


def check_group(group: dict, spools: list[dict], margin: int) -> dict:
    """ok, mixed (depends which spools are loaded), barely, short, or unknown (no spool)."""
    slot, pool = group["slot"], group["pool"]
    matches = match_spools(slot, spools)
    need = sum(used["grams"] for used in group["used"])
    result = {"slot": slot, "slots": [used["n"] for used in group["used"]], "need": need,
              "margin": margin, "pool": pool, "matches": len(matches), "status": "unknown",
              "ams": bool(slot.get("ams")), "ids": [spool.get("id") for spool in matches]}
    if not matches:
        return result
    padded = need * (1 + margin / 100)
    weights = sorted((spool["remaining_weight"] for spool in matches), reverse=True)
    best, worst = sum(weights[:pool]), sum(weights[-pool:])
    if worst >= padded:
        status = "ok"
    elif best >= padded:
        status = "mixed"
    elif best >= need:
        status = "barely"
    else:
        status = "short"
    ranked = sorted(matches, key=lambda spool: spool["remaining_weight"], reverse=True)
    fullest = ranked[0]
    name = (fullest.get("filament") or {}).get("name") or ""
    total = sum(spool.get("initial_weight") or (spool.get("filament") or {}).get("weight") or 0
                for spool in ranked[:pool])
    return {**result, "status": status, "have": best, "worst": worst, "padded": padded,
            "name": name, "total": total, "spool_colour": (fullest.get("filament") or {}).get("color_hex")}


def fmt_grams(grams: float) -> str:
    # The epsilon makes a half such as 296.15 round up, matching the slicer's own figure.
    grams += 1e-9
    return f"{grams / 1000:.2f} kg" if grams >= 1000 else f"{grams:.1f} g"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def slot_label(result: dict) -> str:
    slot, numbers = result["slot"], result["slots"]
    preset = slot["preset"].split("@")[0].strip()
    named = preset or " ".join(part for part in (slot["vendor"], slot["material"]) if part)
    name = result.get("name") or " ".join(part for part in (named, slot["colour"]) if part)
    which = "slot " if len(numbers) == 1 else "slots "
    return f"{which}{_join([str(n) for n in numbers])} ({name})"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _need(result: dict) -> str:
    grams = fmt_grams(result["need"])
    if len(result["slots"]) == 1:
        return f"needs {grams} of filament"
    return f"need {grams} of filament together"


def _supply(result: dict) -> str:
    count, have = min(result["pool"], result["matches"]), fmt_grams(result["have"])
    if count == 1:
        return f"the spool has {have}"
    return f"the {count} spools have {have} between them"


def _tracked(result: dict) -> str:
    if result["matches"] >= result["pool"]:
        return ""
    return f" (only {result['matches']} of {result['pool']} matching spools are in {source_name()})"


def plate_notice(results: list[dict]) -> tuple[str, str] | None:
    """The one notification for a slice: ``("warning" | "info", text)``."""
    label, grams = slot_label, fmt_grams
    where = " in the AMS" if any(r["ams"] for r in results) else ""
    short = [r for r in results if r["status"] == "short"]
    mixed = [r for r in results if r["status"] == "mixed"]
    barely = [r for r in results if r["status"] == "barely"]

    def short_part(r: dict) -> str:
        return _sentence(f"{label(r)} {_need(r)} but {_supply(r)}{_tracked(r)}")

    def mixed_part(r: dict, prefix: str = "") -> str:
        count = min(r["pool"], r["matches"])
        if count == 1:
            body = (f"of {r['matches']} matching spools the fullest has {grams(r['have'])} and "
                    f"the smallest has {grams(r['worst'])}, so make sure the right one is loaded")
        else:
            body = (f"the {count} fullest of {r['matches']} matching spools hold "
                    f"{grams(r['have'])} and the {count} smallest hold {grams(r['worst'])}, "
                    "so make sure the right ones are loaded")
        return _sentence(f"{prefix}{label(r)} {_need(r)}; {body}")

    def barely_part(r: dict, prefix: str = "") -> str:
        return _sentence(f"{prefix}{label(r)} {_need(r)} ({grams(r['padded'])} with the "
                         f"{r['margin']}% margin) and {_supply(r)}{_tracked(r)}")

    if short:
        kind, parts = "warning", [f"not enough filament loaded{where}."]
        parts += [short_part(r) for r in short]
        parts += [mixed_part(r, "also check: ") for r in mixed]
        parts += [barely_part(r, "also barely enough: ") for r in barely]
    elif mixed:
        kind, parts = "warning", [f"not enough filament may be loaded{where}."]
        parts += [mixed_part(r) for r in mixed]
        parts += [barely_part(r, "also barely enough: ") for r in barely]
    elif barely:
        kind, parts = "warning", ["barely enough filament."] + [barely_part(r) for r in barely]
    else:
        kind, parts = "info", []
        fine = [r for r in results if r["status"] == "ok"]
        unknown = [r for r in results if r["status"] == "unknown"]
        if len(fine) > 3:
            numbers = sorted(n for r in fine for n in r["slots"])
            parts.append(f"quantity OK for slots {_join([str(n) for n in numbers])}.")
        elif fine:
            parts.append("quantity OK.")
            parts += [_sentence(f"{label(r)} {_need(r)}, {_supply(r)}") for r in fine]
        if unknown:
            many = sum(len(r["slots"]) for r in unknown) > 1
            outcome = "so they weren't checked" if many else "so it wasn't checked"
            names = _join([label(r) for r in unknown])
            sentence = _sentence(f"no spool found for {names}, {outcome}")
            parts.append(sentence if parts else sentence[:1].lower() + sentence[1:])
        if not parts:
            return None
    return kind, "Spoolio: " + " ".join(parts)


def fit_text(text: str, limit: int = NOTICE_MAX) -> str:
    """Whole sentences only: what doesn't fit is dropped and counted, never cut mid-sentence."""
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=\.) ", text)
    for keep in range(len(sentences) - 1, 0, -1):
        note = f" (+{len(sentences) - keep} more, see the log)"
        shown = " ".join(sentences[:keep])
        if len(shown) + len(note) <= limit:
            return shown + note
    return text[:limit - 1] + "\u2026"


_recent: dict[str, float] = {}
_plate: dict = {"payload": None, "slots": [], "margin": DEFAULT_MARGIN, "page": None}


def is_repeat(text: str) -> bool:
    """True if this exact notice was shown moments ago, so re-slicing doesn't stack warnings."""
    now = time.monotonic()
    expired = [key for key, shown in _recent.items() if now - shown > REPEAT_SECS]
    for old in expired:
        del _recent[old]
    if text in _recent:
        return True
    _recent[text] = now
    return False


def push_notice(kind: str, text: str) -> bool:
    text = fit_text(text)
    ui = orca.host.ui
    push = getattr(ui, "push_notification", None)
    level_name = "WarningNotificationLevel" if kind == "warning" else "RegularNotificationLevel"
    level = getattr(ui, level_name, None)
    if push is None or level is None:
        log.info("Notifications are not available in this build: %s", text)
        return False
    for call in (lambda: push(level, text), lambda: push(text, level)):
        try:
            call()
            return True
        except TypeError:
            continue
        except Exception:
            log.exception("push_notification failed")
            return False
    log.warning("push_notification accepted neither (level, text) nor (text, level)")
    return False


_fetch_lock = threading.Lock()


def plate_spools(url: str) -> list[dict] | None:
    """The spool list, from the cache when it is fresh; None if Spoolman can't be reached."""
    with _fetch_lock:
        age = time.monotonic() - _spool_cache["time"]
        if _spool_cache["url"] == url and age < CACHE_SECS:
            return _spool_cache["spools"]
        result = load_spools()
        return result["spools"] if result["ok"] else None


def plate_rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        slot, numbers, status = r["slot"], r["slots"], r["status"]
        many = len(numbers) > 1
        preset = slot["preset"].split("@")[0].strip()
        name = r.get("name") or preset or " ".join(p for p in (slot["vendor"], slot["material"]) if p)
        need, have, count = fmt_grams(r["need"]), r.get("have", 0), min(r["pool"], r["matches"])
        total = r.get("total") or max(have, r["need"])
        if status == "unknown":
            headline, detail = "Not checked", f"no spool found for {'these slots' if many else 'this slot'}"
        else:
            headline = {"ok": "Quantity OK", "barely": "Barely enough", "short": "Not enough",
                        "mixed": "Check the spool" + ("s" if count > 1 else "")}[status]
            if status == "mixed":
                detail = f"{need} needed \u00b7 matching spools hold {fmt_grams(r['worst'])} to {fmt_grams(have)}"
            elif count > 1:
                detail = f"{need} needed \u00b7 {fmt_grams(have)} across {count} spools"
            elif status == "ok" and r["matches"] == 1:
                detail = f"{need} needed \u00b7 leaves {fmt_grams(have - r['need'])}"
            elif status == "ok":
                detail = f"{need} needed \u00b7 every matching spool has at least {fmt_grams(r['worst'])}"
            else:
                detail = f"{need} needed \u00b7 {fmt_grams(have)} left"
        rows.append({
            "slots": ("Slots " if many else "Slot ") + " + ".join(str(n) for n in numbers),
            "first": min(numbers), "name": name,
            "meta": " \u00b7 ".join(p for p in (slot["vendor"], f"{len(numbers)} slots, pooled" if many else "") if p),
            "colour": slot["colour"], "status": status, "headline": headline, "detail": detail,
            "fill": round(min(1.0, have / total), 3), "need_pct": round(min(1.0, r["need"] / total), 3),
            "spool_colour": r.get("spool_colour") or slot["colour"],
            "ids": r["ids"],
        })
    return rows


def send_plate(payload: dict | None) -> None:
    page = _plate["page"]
    if page is None:
        return
    try:
        page.post_message(payload or {"type": "plate_clear"})
    except Exception:
        log.exception("Could not update the plate panel")


def show_plate(slots: list[dict], margin: int, results: list[dict] | None) -> None:
    payload = ({"type": "plate", "time": time.strftime("%H:%M"), "rows": plate_rows(results)}
               if results else None)
    _plate.update(payload=payload, slots=slots, margin=margin)
    send_plate(payload)


def clear_plate() -> None:
    _plate["payload"] = None
    send_plate(None)


def recheck() -> None:
    slots, margin = _plate["slots"], _plate["margin"]
    if not slots:
        return
    try:
        show_plate(slots, margin, check_plate(slots, margin, fresh=True)[1])
    except Exception:
        log.exception("Plate re-check failed")


def check_plate(slots: list[dict], margin: int, fresh: bool = False) -> tuple[tuple | None, list | None]:
    """The notice for a plate and its per-group results (None when the spool source couldn't be checked)."""
    key = spool_key()
    if fresh:
        _spool_cache["time"] = float("-inf")
    spools = plate_spools(key) if key else None
    if not key:
        return ("info", "Spoolio: no Spoolman address is set up yet, "
                        "so the filament quantity wasn't checked."), None
    if spools is None:
        return ("info", f"Spoolio: couldn't reach {source_name()}, "
                        "so the filament quantity wasn't checked."), None
    results = [check_group(group, spools, margin) for group in group_slots(slots)]
    log.info("Plate check: %s", ", ".join(
        f"slot {'+'.join(str(n) for n in r['slots'])} {r['status']}" for r in results))
    return plate_notice(results), results


_sliced: dict = {"preset": "", "slots": [], "payload": None}


def heads_fill(slots: list[dict], preset: str, pid: str = "") -> dict | None:
    """Put the sliced filaments on the toolheads, or MMU gates, of the non-Bambu printer they are for."""
    _sliced.update(preset=preset, slots=slots)
    cfg = printers_cfg()
    was = json.dumps(cfg)
    net = [p for p in cfg if p["type"] in NET and p["units"]]
    if not net or not preset or not slots or slots[0].get("ams"):
        return None
    if pid:
        for q in cfg:
            q["orca"] = preset if q["id"] == pid else "" if q["orca"] == preset else q["orca"]
    p = next((x for x in net if x["orca"] == preset), None)
    if p is None:
        if len(net) > 1 or net[0]["orca"]:
            return {"preset": preset, "choose": [x["id"] for x in net]}
        p = net[0]
        p["orca"] = preset
    with _live_lock:
        hh = bool((_live["printers"].get(p["id"]) or {}).get("gates"))
    unit = None if hh else next((u for u in p["units"] if u["kind"] == "mmu"), None) or \
        next((u for u in p["units"] if u["kind"] == "tool"), None)
    out = {"preset": preset, "printer": p["id"], "time": time.strftime("%H:%M"), "rows": []}
    spools = plate_spools(spool_key()) if spool_key() else None
    if unit is None or spools is None:
        if json.dumps(cfg) != was:
            save_printers(cfg)
        return dict(out, error="hh" if hh else "unit" if unit is None else "spools")
    placed: set[str] = set()
    for i, slot in enumerate(slots[:len(unit["slots"])]):
        ids = [str(x["id"]) for x in match_spools(slot, spools) if str(x["id"]) not in placed]
        cur = unit["slots"][i]
        state = "kept" if cur in ids else "set" if len(ids) == 1 else "pick" if ids else "none"
        if state == "set":
            for q in cfg:
                for u in q["units"]:
                    u["slots"] = [None if x == ids[0] else x for x in u["slots"]]
            unit["slots"][i] = ids[0]
        if state in ("set", "kept"):
            placed.add(unit["slots"][i])
        out["rows"].append({"n": i, "kind": unit["kind"], "state": state, "count": len(ids), "colour": slot["colour"],
                            "name": slot["preset"].split("@")[0].strip() or slot["material"]})
    if json.dumps(cfg) != was:
        save_printers(cfg)
    log.info("Filled %s from the slice: %s", unit["kind"], ", ".join(f"{r['n']} {r['state']}" for r in out["rows"]))
    return out


def run_heads(slots: list[dict], preset: str, pid: str = "") -> None:
    time.sleep(NOTICE_DELAY)
    try:
        _sliced["payload"] = heads_fill(slots, preset, pid)
        send_heads()
    except Exception:
        log.exception("Filling the toolheads failed")


def send_heads() -> None:
    page = _plate["page"]
    if page is None or _sliced["payload"] is None:
        return
    try:
        page.post_message({"type": "heads", "heads": _sliced["payload"], "printers": printer_state()["printers"]})
    except Exception:
        log.exception("Could not update the printer panel")


def run_check(slots: list[dict], margin: int) -> None:
    time.sleep(NOTICE_DELAY)
    try:
        notice, results = check_plate(slots, margin)
        show_plate(slots, margin, results)
        if notice and not is_repeat(notice[1]):
            log.info("Plate notice (%s): %s", *notice)
            push_notice(*notice)
    except Exception:
        log.exception("Plate check failed")


LOGO_DATA_URI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYg"
    "MjU2Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImItdGUiIHgxPSIwIiB5MT0iMCIgeDI9IjEi"
    "IHkyPSIxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM0QkUzQkMiLz48c3RvcCBvZmZz"
    "ZXQ9IjEiIHN0b3AtY29sb3I9IiMxN0E5OEEiLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFk"
    "aWVudCBpZD0iYi1mbCIgeDE9IjAiIHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0i"
    "MCIgc3RvcC1jb2xvcj0iI0VFRjBGNCIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzhG"
    "OTlBOCIvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiLWh1YiIgeDE9IjAi"
    "IHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzQ1NEU1"
    "RCIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzFFMjMyQiIvPjwvbGluZWFyR3JhZGll"
    "bnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiLWdyIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMCI+"
    "PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjNUVFQUI0Ii8+PHN0b3Agb2Zmc2V0PSIxIiBz"
    "dG9wLWNvbG9yPSIjMTBCOTgxIi8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PGcgdHJhbnNmb3Jt"
    "PSJyb3RhdGUoMTM1IDEyOCAxMjgpIiBmaWxsPSJub25lIiBzdHJva2Utd2lkdGg9IjEyIiBzdHJv"
    "a2UtbGluZWNhcD0icm91bmQiPjxjaXJjbGUgY3g9IjEyOCIgY3k9IjEyOCIgcj0iMTEyIiBzdHJv"
    "a2U9IiM4RTk4QTgiIHN0cm9rZS1vcGFjaXR5PSIuMyIgc3Ryb2tlLWRhc2hhcnJheT0iNTI3Ljgg"
    "NzAzLjciLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9IjExMiIgc3Ryb2tlPSJ1cmwoI2It"
    "Z3IpIiBzdHJva2UtZGFzaGFycmF5PSIzODAuMCA3MDMuNyIvPjwvZz48Y2lyY2xlIGN4PSIxMjgi"
    "IGN5PSIxMjgiIHI9Ijg0IiBmaWxsPSJ1cmwoI2ItZmwpIi8+PGNpcmNsZSBjeD0iMTI4IiBjeT0i"
    "MTI4IiByPSI4MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utb3BhY2l0eT0iLjQ1"
    "IiBzdHJva2Utd2lkdGg9IjIiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9IjU0IiBmaWxs"
    "PSJub25lIiBzdHJva2U9InVybCgjYi10ZSkiIHN0cm9rZS13aWR0aD0iNDAiLz48Y2lyY2xlIGN4"
    "PSIxMjgiIGN5PSIxMjgiIHI9IjQ1LjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tl"
    "LW9wYWNpdHk9Ii4xMyIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIx"
    "MjgiIHI9IjYyLjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tlLW9wYWNpdHk9Ii4x"
    "MyIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxMjgiIGN5PSIxMjgiIHI9Ijc0LjAi"
    "IGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwMCIgc3Ryb2tlLW9wYWNpdHk9Ii4yIiBzdHJva2Utd2lk"
    "dGg9IjIuNSIvPjxwYXRoIGZpbGwtcnVsZT0iZXZlbm9kZCIgZmlsbD0idXJsKCNiLWh1YikiIGQ9"
    "Ik05NCAxMjggQTM0IDM0IDAgMSAwIDE2MiAxMjggQTM0IDM0IDAgMSAwIDk0IDEyOCBaIE0xMTQg"
    "MTI4IEExNCAxNCAwIDEgMCAxNDIgMTI4IEExNCAxNCAwIDEgMCAxMTQgMTI4IFoiLz48Y2lyY2xl"
    "IGN4PSIxMjgiIGN5PSIxMjgiIHI9IjE0IiBmaWxsPSJub25lIiBzdHJva2U9IiMwMDAiIHN0cm9r"
    "ZS1vcGFjaXR5PSIuNDUiIHN0cm9rZS13aWR0aD0iMiIvPjwvc3ZnPg=="
)

LOGO_IMG = f'<img class="logo" src="{LOGO_DATA_URI}" alt="">'

# White line-art tab icon: a gauge arc around a wound spool. get_icon() must return the
# path of an image file; SVG markup or a data: URI shows no icon.
TAB_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="#FFFFFF" stroke-width="1.6" stroke-linecap="round">'
    '<path d="M5.636 18.364A9 9 0 1 1 18.364 18.364"/><circle cx="12" cy="12" r="5.5"/>'
    '<circle cx="12" cy="12" r="2"/>'
    '</svg>'
)
TAB_ICON_FILE = DATA_DIR / "spoolio_tab.svg"


def icon_path() -> str:
    try:
        if not TAB_ICON_FILE.exists() or TAB_ICON_FILE.read_text(encoding="utf-8") != TAB_ICON_SVG:
            TAB_ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
            TAB_ICON_FILE.write_text(TAB_ICON_SVG, encoding="utf-8")
    except OSError:
        log.exception("Could not write the tab icon to %s", TAB_ICON_FILE)
        return ""
    return str(TAB_ICON_FILE)


def _fill(template: str, **values: object) -> str:
    """Substitute ``__NAME__`` placeholders.

    Plain substitution rather than an f-string or str.format, so the CSS and
    JavaScript in the templates keep their normal single braces.
    """
    return re.sub(r"__([A-Z][A-Z_]*)__", lambda match: str(values[match.group(1)]), template)


# The pages and the tab set overscroll-behavior-x: none so a two-finger trackpad swipe
# can't navigate the embedded browser back or forward, which lands on a blank page.
MAIN_PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="__LOGODATA__">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { font-family: __FONT__; }
  html { overscroll-behavior-x: none; }
  body { margin: 0; padding: 12px; font-size: 13px; }
  .header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .header .title { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; }
  .header img.logo { width: 52px; height: 52px; flex-shrink: 0; }
  .header-actions { display: flex; gap: 6px; flex-shrink: 0; }
  h2 { font-size: 20px; font-weight: 700; margin: 0; color: var(--orca-fg); }
  #status { color: var(--orca-muted); margin-bottom: 8px; }
  #status.error { color: #d9534f; }
  button {
    padding: 4px 10px; cursor: pointer;
    border: 1px solid var(--orca-border); border-radius: 4px;
    background: transparent; color: var(--orca-fg);
  }
  button:hover { border-color: var(--orca-accent); }
  #settings-btn, #plate-recheck, #plate-dismiss, #filters-toggle {
    padding: 7px 16px; border-radius: 6px; font-size: 13px; font-weight: 700;
  }
  #settings-btn, .sort-dir { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  .filters { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .filters input, .filters select {
    flex: 1; min-width: 110px; padding: 4px 6px;
    border: 1px solid var(--orca-border); border-radius: 4px;
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .filters select option {
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .sort-dir { flex: 0 0 auto; min-width: 32px; }
  .empty { color: var(--orca-muted); padding: 12px 4px; }

  .spool-card {
    position: relative;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%);
    border: 1px solid var(--orca-border);
    border-radius: 14px;
    padding: 13px 14px 11px;
    margin-bottom: 9px;
  }
  .spool-tag {
    position: absolute; top: -8px; left: 14px;
    display: flex; align-items: center; gap: 4px;
    max-width: calc(100% - 28px);
    padding: 2px 8px 2px 6px;
    border-radius: 4px 9px 9px 0;
    background: var(--orca-accent);
    background: linear-gradient(135deg, color-mix(in srgb, var(--orca-accent) 80%, black), var(--orca-accent));
    color: var(--orca-accent-fg);
    font-size: 10px; font-weight: 700; letter-spacing: 0.02em;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
  }
  .spool-tag span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spool-tag svg { width: 9px; height: 9px; flex-shrink: 0; }
  .spool-body { display: flex; gap: 11px; align-items: flex-start; }
  .spool-swatch-col {
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    flex-shrink: 0; padding-top: 1px; position: relative;
  }
  .spool-swatch-col .spool-rfid { position: absolute; left: 50%; top: 100%; transform: translate(-50%, -55%); z-index: 1; }
  .spool-swatch {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    border: 2px solid rgba(255, 255, 255, 0.1);
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.2);
  }
  .spool-rfid {
    display: flex; align-items: center; gap: 3px; flex-shrink: 0;
    padding: 2px 5px; border-radius: 5px; color: #3a3d42;
    background: linear-gradient(135deg, #f4f5f7 0%, #cdd1d6 35%, #8b9099 55%, #cdd1d6 75%, #f4f5f7 100%);
    border: 1px solid rgba(0, 0, 0, 0.25);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.55), inset 0 -1px 0 rgba(0, 0, 0, 0.15), 0 1px 2px rgba(0, 0, 0, 0.25);
  }
  .spool-rfid span { font-size: 8px; font-weight: 800; letter-spacing: 0.04em; }
  .spool-rfid svg { width: 11px; height: 11px; flex-shrink: 0; }
  .spool-main { flex: 1; min-width: 0; }
  .spool-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .spool-title {
    font-size: 14px; font-weight: 700; color: var(--orca-fg);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .spool-weight { font-size: 13px; font-weight: 400; color: var(--orca-fg); flex-shrink: 0; display: flex; align-items: center; gap: 6px; }
  .spool-cart {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 2px 4px; cursor: pointer; border-radius: 5px; line-height: 0;
    background: transparent; color: #e0a030; border: 1px solid currentColor;
  }
  .spool-cart:hover { background: rgba(224, 160, 48, 0.18); }
  #list .spool-cart { margin: -2px 0; }
  .spool-tline { display: flex; align-items: baseline; min-width: 0; }
  .spool-tline .spool-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spool-tline .spool-dup { flex-shrink: 0; margin-top: -3px; margin-bottom: -3px; }
  .spool-edit {
    flex-shrink: 0; margin-left: 8px; padding: 0; border: 0; line-height: 0; cursor: pointer;
    background: none; color: var(--orca-muted);
  }
  .spool-edit:hover { background: none; color: var(--orca-accent); }
  .spool-edit svg { width: 14px; height: 14px; }
  #add-spool { margin-left: auto; padding: 7px 14px; border-radius: 6px; font-size: 13px; font-weight: 700; }
  .spool-cart svg { width: 13px; height: 13px; }
  .spool-bar-track {
    height: 5px; border-radius: 3px; margin: 6px 0 5px;
    background: rgba(127, 127, 127, 0.25); overflow: hidden;
  }
  .spool-bar-fill { height: 100%; border-radius: 3px; transition: width .6s ease; }
  .spool-pct { font-size: 12px; color: var(--orca-muted); }
  .spool-weight.low .spool-pct { color: inherit; opacity: 0.8; }
  .spool-dup {
    margin-left: 8px; padding: 1px 8px; border-radius: 99px; vertical-align: middle;
    border: 1px solid var(--orca-border); color: var(--orca-muted);
    font-size: 11px; font-weight: 700;
  }
  .spool-subtitle {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    font-size: 12px; color: var(--orca-muted); font-style: italic;
  }
  .sub-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .plate-chip {
    flex: none; margin: 5px 0 4px; padding: 3px 11px; border-radius: 99px;
    border: 1px solid var(--orca-accent); font-size: 11px; font-weight: 700; font-style: normal;
    background: color-mix(in srgb, var(--orca-accent) 25%, var(--orca-bg) 75%);
    color: color-mix(in srgb, var(--orca-accent) 55%, var(--orca-fg) 45%);
  }

  .plate { margin: 6px 0 2px; color: var(--orca-fg); }
  .divider { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin: 8px 0 4px; }
  .divider .line { flex: 1; height: 1px; min-width: 12px; background: var(--orca-border); }
  .divider-title { font-weight: 700; color: color-mix(in srgb, var(--orca-accent) 75%, var(--orca-fg) 25%); }
  .divider-sub { color: var(--orca-muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .plate-pill { padding: 2px 10px; border-radius: 99px; font-size: 12px; font-weight: 700; white-space: nowrap; }
  .plate-pill.attention { background: rgba(210, 153, 34, 0.22); color: #e0a030; }
  .plate-pill.fine { background: rgba(63, 185, 80, 0.18); color: #3fb950; }
  #plate-recheck { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  .plate-row { display: flex; flex-wrap: wrap; gap: 6px 11px; align-items: center; padding: 8px 15px; }
  .plate-who { flex: 1 1 200px; min-width: 0; }
  .plate-who .plate-name, .plate-who .plate-meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .plate-rows .plate-row + .plate-row { border-top: 1px solid var(--orca-border); }
  .plate-dot {
    flex: none; width: 16px; height: 16px; margin: 0 13px; border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.15); box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.2);
  }
  .plate-name { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .plate-meta { color: var(--orca-muted); font-size: 12px; margin-top: 1px; }
  .plate-result { flex: 1.4 1 190px; text-align: right; min-width: 0; }
  .plate-headline { font-weight: 700; }
  .plate-bar { flex: 3 1 120px; position: relative; height: 8px; border-radius: 4px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .plate-bar .have { position: absolute; top: 0; bottom: 0; left: 0; }
  .plate-bar .need {
    position: absolute; top: 0; bottom: 0; left: 0;
    background: repeating-linear-gradient(45deg, rgba(255, 255, 255, 0.38) 0, rgba(255, 255, 255, 0.38) 3px, rgba(0, 0, 0, 0.38) 3px, rgba(0, 0, 0, 0.38) 6px);
  }
  .listbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
  .listbar #status { margin-bottom: 0; }
  .st-ok .plate-headline { color: #3fb950; }
  .st-barely .plate-headline, .st-mixed .plate-headline { color: #e0a030; }
  .st-short .plate-headline { color: #d9534f; }
  .st-unknown .plate-headline { color: var(--orca-muted); }

  .group { margin-bottom: 4px; }
  .group-card {
    display: flex; align-items: center; gap: 10px; cursor: pointer;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 8%, var(--orca-bg) 92%);
    border: 1px solid var(--orca-border);
    border-radius: 10px; padding: 8px 12px; margin-bottom: 10px;
  }
  .group-card .arrow { width: 12px; flex-shrink: 0; color: var(--orca-muted); }
  .group-card .group-info { flex: 1; min-width: 0; }
  .group-card .group-name { font-weight: 700; font-size: 13px; }
  .group-card .group-meta { color: var(--orca-muted); font-size: 11px; margin-top: 1px; }
  .group-card .group-bar { width: 90px; flex-shrink: 0; }
  .group-card .group-bar-track { height: 5px; border-radius: 3px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .group-card .group-bar-fill { height: 100%; border-radius: 3px; }
  .group-card .group-bar-fill.high { background: #3fb950; }
  .group-card .group-bar-fill.mid { background: #d29922; }
  .group-card .group-bar-fill.low { background: #d9534f; }
  .group-items { padding-left: 4px; }
  .group.collapsed .group-items { display: none; }
  #list.cols-2, #list.cols-2 .group-items { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 10px; align-items: stretch; }
  #list.cols-2 .spool-card { height: calc(100% - 10px); box-sizing: border-box; }
  #list.cols-2 > .group, #list.cols-2 > .empty { grid-column: 1 / -1; }
  #list.cols-2 .group.collapsed .group-items { display: none; }
  @media (max-width: 640px) { #list.cols-2, #list.cols-2 .group-items { grid-template-columns: minmax(0, 1fr); } }
  .tabs { display: inline-flex; margin: 0 0 12px; border: 1px solid var(--orca-border); border-radius: 8px; overflow: hidden; }
  .tabs[hidden], #page-spools[hidden], #page-printer[hidden], #page-stats[hidden] { display: none; }
  .st-bar { display: flex; align-items: center; gap: 10px; margin: 0 0 12px; flex-wrap: wrap; }
  .st-bar select { padding: 5px 8px; border: 1px solid var(--orca-border); border-radius: 4px; background: var(--orca-bg); color: var(--orca-fg); }
  .st-bar select option { background: var(--orca-bg); color: var(--orca-fg); }
  .st-note { color: var(--orca-muted); font-size: 12px; margin-left: auto; }
  .st-tiles { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 10px; }
  .st-tile, .st-card { border: 1px solid var(--orca-border); border-radius: 8px; padding: 12px 14px; background: var(--orca-border); background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); min-width: 0; }
  .st-tile span { font-size: 12px; color: var(--orca-muted); }
  .st-tile b { display: block; font-size: 22px; font-weight: 700; margin-top: 4px; color: var(--orca-fg); }
  .st-tile.low b, .st-tile.w b { color: #e0a030; }
  .st-tile small { display: block; font-size: 11px; color: var(--orca-muted); margin-top: 2px; }
  .wi { grid-column: 1 / -1; }
  .wi-in { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 28px; align-items: start; }
  .wi-bar { display: flex; height: 22px; border-radius: 6px; overflow: hidden; gap: 2px; margin: 4px 0 12px; }
  .wi-bar i { display: block; }
  .wi-leg div { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 7px 0; }
  .wi-leg .k { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }
  .wi-leg .n { flex: 1; }
  .wi-leg .v { color: var(--orca-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .wi-note { font-size: 11px; color: var(--orca-muted); margin-top: 8px; }
  .wi h4 { margin: 0 0 8px; font-size: 11px; letter-spacing: .06em; color: var(--orca-muted); font-weight: 700; }
  .wi-top .st-row .v { width: 110px; }
  .ph td.wst { color: #e0a030; }
  .st-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
  .st-card h3 { margin: 0 0 10px; font-size: 14px; color: var(--orca-fg); }
  .st-empty { color: var(--orca-muted); font-size: 12px; padding: 6px 0; }
  .st-row { display: flex; align-items: center; gap: 10px; font-size: 13px; margin: 7px 0; }
  .st-row .l { width: 170px; flex-shrink: 1; min-width: 60px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .st-row .t { flex: 1; height: 10px; border-radius: 5px; background: color-mix(in srgb, var(--orca-fg) 10%, transparent); overflow: hidden; }
  .st-row .t i { display: block; height: 100%; background: var(--orca-accent); border-radius: 5px; }
  .st-row .t i.red { background: #d9534f; }
  .st-row .v { width: 70px; text-align: right; color: var(--orca-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .st-sw { width: 12px; height: 12px; border-radius: 50%; border: 1px solid var(--orca-border); flex-shrink: 0; }
  .st-weeks { display: flex; align-items: flex-end; gap: 8px; height: 150px; padding-top: 6px; }
  .st-weeks > div { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; font-size: 11px; color: var(--orca-muted); gap: 4px; white-space: nowrap; min-width: 0; }
  .st-weeks em { font-style: normal; color: var(--orca-fg); }
  .st-weeks .stk { display: flex; flex-direction: column; width: 100%; gap: 2px; border-radius: 4px 4px 0 0; overflow: hidden; }
  .st-weeks .stk i { display: block; min-height: 3px; box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .14); }
  .st-weeks .stk.now { opacity: .6; }
  .st-wk-note { font-size: 11px; color: var(--orca-muted); margin-top: 8px; }
  .st-donut { display: flex; align-items: center; gap: 22px; }
  .st-donut svg { width: 150px; height: 150px; flex-shrink: 0; }
  .st-donut .c1 { font-size: 6.5px; font-weight: 700; fill: var(--orca-fg); }
  .st-donut .c2 { font-size: 3.6px; fill: var(--orca-muted); }
  .st-leg { flex: 1; font-size: 13px; min-width: 0; }
  .st-leg div { display: flex; align-items: center; gap: 8px; margin: 8px 0; }
  .st-leg .k { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }
  .st-leg .n { flex: 1; }
  .st-leg .v { color: var(--orca-muted); font-variant-numeric: tabular-nums; }
  .ph { grid-column: 1 / -1; }
  .ph-h { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
  .ph-h h3 { margin: 0; }
  .ph-h .sum { color: var(--orca-muted); font-size: 12px; flex: 1; }
  .ph-h .chev { color: var(--orca-muted); transition: transform .15s; }
  .ph.open .chev { transform: rotate(180deg); }
  .ph-b { margin-top: 10px; overflow-x: auto; }
  .ph:not(.open) .ph-b { display: none; }
  .ph table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .ph th { text-align: left; font-weight: 600; font-size: 12px; color: var(--orca-muted); padding: 0 10px 8px; border-bottom: 1px solid var(--orca-border); }
  .ph td { padding: 9px 10px; border-bottom: 1px solid var(--orca-border); vertical-align: middle; }
  .ph tr:last-child td { border-bottom: 0; }
  .ph th.r, .ph td.r { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .ph td.d { color: var(--orca-muted); white-space: nowrap; }
  .ph .nm { font-weight: 600; }
  .ph .pr { color: var(--orca-muted); font-size: 12px; }
  .ph .mats { display: flex; flex-wrap: wrap; gap: 6px; }
  .ph .m { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px 2px 4px; border-radius: 99px; background: color-mix(in srgb, var(--orca-fg) 8%, transparent); font-size: 12px; white-space: nowrap; }
  .ph .m i { width: 10px; height: 10px; border-radius: 50%; border: 1px solid var(--orca-border); }
  .ph .fail { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 99px; background: rgba(217, 83, 79, .18); color: #ff8a85; margin-left: 6px; }
  .ph .cal { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 99px; background: rgba(126, 140, 224, .2); color: #a9b3f0; margin-left: 6px; }
  .ph .more { display: block; margin: 10px auto 0; }
  @media (max-width: 760px) { .st-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } .st-grid, .wi-in { grid-template-columns: minmax(0, 1fr); } }
  .tabs button { background: transparent; color: var(--orca-fg); border: 0; border-right: 1px solid var(--orca-border); border-radius: 0; padding: 7px 20px; font-weight: 600; cursor: pointer; }
  .tabs button:last-child { border-right: 0; }
  .tabs button.on { background: var(--orca-accent); color: var(--orca-accent-fg); }
  #tab-dot { display: inline-block; width: 8px; height: 8px; margin-left: 7px; border-radius: 50%; background: #e0a030; }
  #tab-dot[hidden] { display: none; }
  .ams { margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg) 96%); }
  .ams-h { color: var(--orca-fg); display: flex; align-items: baseline; gap: 10px; padding: 10px 14px; font-weight: 700; }
  .ams-h .sub { font-weight: 400; color: var(--orca-muted); font-size: 12px; }
  .ams-body { padding: 2px 14px 14px; }
  .ams-unit { display: flex; align-items: center; margin-top: 12px; font-size: 12px; color: var(--orca-muted); }
  .ams-h { flex-wrap: wrap; }
  .ams-h .bar { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
  .sbtn { background: transparent; color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 10px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .sbtn:hover { border-color: var(--orca-accent); }
  .sbtn.armed { border-color: #d9534f; color: #d9534f; }
  .sbtn.ghost { border-color: transparent; font-weight: 400; color: var(--orca-muted); }
  .ams-unit .sbtn { margin-left: auto; }
  .pname { background: transparent; border: 0; padding: 0; color: var(--orca-fg); font: inherit; font-weight: 700; cursor: pointer; }
  .pname-in { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-accent); border-radius: 6px; padding: 3px 8px; font: inherit; font-weight: 700; width: 180px; }
  .addp { margin-top: 2px; }
  .ams-assign { cursor: pointer; opacity: 1; }
  .ams-assign:hover { border-color: var(--orca-accent); }
  .ams-x { position: absolute; top: -10px; right: 34px; z-index: 3; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 50%; background: #555a5f; color: #fff; font-size: 10px; line-height: 18px; text-align: center; cursor: pointer; }
  .ams-x:hover { background: #d9534f; }
  .picker { position: absolute; z-index: 20; top: 100%; left: 0; margin-top: 6px; width: min(340px, 90vw); background: var(--orca-bg); border: 1px solid var(--orca-border); border-radius: 10px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45); padding: 10px; }
  .ams-cell:nth-child(4n) .picker { left: auto; right: 0; }
  .picker input { width: 100%; box-sizing: border-box; margin-bottom: 6px; background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 8px 10px; font: inherit; font-size: 13px; }
  .picker input:focus { outline: none; border-color: var(--orca-accent); }
  .pick-list { max-height: 280px; overflow-y: auto; }
  .pi { display: flex; align-items: center; gap: 10px; padding: 7px 8px; border-radius: 6px; color: var(--orca-fg); font-size: 13px; cursor: pointer; }
  .pi:hover { background: color-mix(in srgb, var(--orca-accent) 35%, transparent); }
  .pi .d { width: 18px; height: 18px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.25); flex: none; }
  .pi .m { margin-left: auto; color: var(--orca-muted); font-size: 12px; white-space: nowrap; }
  .pi small { display: block; color: var(--orca-muted); font-size: 11px; }
  .pnote { padding: 8px; font-size: 12px; color: var(--orca-muted); }
  .ams-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 22px 10px; margin-top: 20px; }
  .ams-cell { position: relative; display: flex; }
  .ams-cell .spool-card { flex: 1; margin: 0; box-sizing: border-box; min-width: 0; }
  .ams-n { position: absolute; top: -10px; right: 10px; z-index: 2; min-width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 9px; background: var(--orca-accent); color: var(--orca-accent-fg); font-size: 11px; font-weight: 800; }
  .ams-empty { border-style: dashed; opacity: 0.7; }
  .ams-dash { background: transparent !important; border: 2px dashed var(--orca-border) !important; box-shadow: none !important; }
  .ams-note { margin-top: 12px; font-size: 12px; color: var(--orca-muted); }
  .ams-note.heads { color: var(--orca-fg); }
  .ams-note.heads .sbtn { margin-left: 8px; }
  .plate.on-printer > .divider:last-child { display: none; }
  .live-dot { display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 50%; background: #3fb950; }
  .live-dot.stale { background: #e0a030; }
  .live-dot:not(.stale) { animation: live-pulse 2s ease-out infinite; }
  @keyframes live-pulse { 0% { box-shadow: 0 0 0 0 rgba(63, 185, 80, .55); } 70%, 100% { box-shadow: 0 0 0 6px rgba(63, 185, 80, 0); } }
  .usage { margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg) 96%); }
  .usage-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 10px 14px; color: var(--orca-fg); }
  .usage-bar b { font-weight: 700; }
  .usage-bar .sub { color: var(--orca-muted); font-size: 12px; margin-right: auto; }
  .usage select, .u-spool select { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px; max-width: 100%; }
  .u-card { border: 1px solid var(--orca-border); border-radius: 14px; background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); padding: 12px 14px; }
  .u-h { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; color: var(--orca-fg); margin-bottom: 6px; }
  .u-h .sub { color: var(--orca-muted); font-size: 12px; margin-right: auto; }
  .u-h .u-chip { margin-left: 0; }
  .u-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 0; color: var(--orca-fg); }
  .u-row .d { width: 14px; height: 14px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.15); flex: none; }
  .u-slot { color: var(--orca-muted); min-width: 110px; }
  .u-spool { flex: 1 1 220px; min-width: 0; }
  .u-g { width: 74px; background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px; }
  .u-chip { margin-left: 8px; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 99px; background: rgba(63, 185, 80, 0.18); color: #3fb950; }
  .u-chip.warn { background: rgba(210, 153, 34, 0.22); color: #e0a030; }
  .u-chip.off { background: color-mix(in srgb, var(--orca-fg) 12%, transparent); color: var(--orca-muted); }
  .u-err { flex-basis: 100%; color: #d9534f; font-size: 12px; }
  .u-amt { color: var(--orca-muted); white-space: nowrap; }
  .u-card.undone .u-row { color: var(--orca-muted); }
  .u-card.undone .u-spool, .u-card.undone .u-amt { text-decoration: line-through; }
  .rb-h { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-top: 1px solid var(--orca-border); color: var(--orca-fg); cursor: pointer; user-select: none; }
  .rb-h .arrow { width: 12px; color: var(--orca-muted); }
  .rb-h .sub { color: var(--orca-muted); font-size: 12px; margin-right: auto; }
  .rb-h .u-chip { margin-left: 0; }
  .rb-list { display: flex; flex-direction: column; gap: 10px; padding: 0 14px 14px; }
  .rb-mask { position: fixed; inset: 0; z-index: 200; display: flex; align-items: center; justify-content: center; padding: 16px; background: rgba(0, 0, 0, 0.55); }
  .rb-pop { width: min(460px, 100%); box-sizing: border-box; background: var(--orca-bg); border: 1px solid var(--orca-border); border-radius: 10px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45); padding: 16px; color: var(--orca-fg); font-size: 13px; }
  .rb-pop b.t { display: block; font-size: 15px; margin-bottom: 6px; }
  .rb-pop .sub { color: var(--orca-muted); font-size: 12px; }
  .rb-tbl { margin: 12px 0; border: 1px solid var(--orca-border); border-radius: 8px; }
  .rb-sp { padding: 8px 12px; }
  .rb-sp + .rb-sp { border-top: 1px solid var(--orca-border); }
  .rb-sp .n { display: flex; align-items: center; gap: 8px; font-weight: 700; margin-bottom: 4px; }
  .rb-sp .d { width: 12px; height: 12px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.15); flex: none; }
  .rb-sp .w { display: flex; gap: 6px; color: var(--orca-muted); font-size: 12px; padding-left: 22px; }
  .rb-sp .w b { color: #3fb950; }
  .rb-mask.dnt-in { animation: pc-fade .18s ease-out; }
  .rb-mask.dnt-in .rb-pop { animation: pc-pop .22s ease-out; }
  .dnt-warn .dnt-in { animation: pc-fade .2s ease-out; }
  @media (prefers-reduced-motion: reduce) { .dnt-in, .dnt-in .rb-pop { animation: none !important; } }
  .dnt-opt { display: flex; gap: 10px; align-items: center; margin-top: 12px; padding: 10px 12px; border: 1px solid var(--orca-border); border-radius: 8px; cursor: pointer; }
  .dnt-opt.on { border-color: var(--orca-accent); background: color-mix(in srgb, var(--orca-accent) 10%, transparent); }
  .dnt-opt input { margin: 0; accent-color: var(--orca-accent); }
  .dnt-opt b { display: block; }
  .dnt-warn { display: flex; gap: 10px; align-items: flex-start; margin-top: 10px; padding: 10px 12px; border: 1px solid rgba(210, 153, 34, 0.45); border-radius: 8px; background: rgba(210, 153, 34, 0.14); color: #e0a030; font-size: 12px; line-height: 1.45; }
  .dnt-warn svg { flex: none; width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .dnt-note { flex-basis: 100%; color: var(--orca-muted); font-size: 12px; margin-top: -4px; }
  .preset-warn { margin: 0 0 12px; align-items: center; }
  .preset-warn[hidden] { display: none; }
  .preset-warn .pw-txt { flex: 1; min-width: 0; }
  .preset-warn .pw-acts { display: flex; gap: 6px; flex: none; }
  .spool-card.spool-warn { border-color: #e0a030 !important; box-shadow: 0 0 0 1px rgba(224, 160, 48, 0.45); }
  .spool-card.spool-warn .spool-tag { left: 22px; }
  .spool-warn-badge {
    position: absolute; top: -8px; left: -6px; z-index: 3;
    display: flex; align-items: center; justify-content: center;
    width: 20px; height: 20px; pointer-events: auto;
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.45));
  }
  .spool-warn-badge svg { width: 20px; height: 20px; display: block; }
  .ph .nb { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 99px; background: rgba(210, 153, 34, 0.22); color: #e0a030; margin-left: 6px; }
  .rb-bar { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  #usage-slot:empty, #progress-slot:empty { display: none; }
  #usage-slot { margin-top: 14px; }
  .prog { margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg) 96%); font-size: 11px; }
  .prog b, .pj-name { font-size: 13px; }
  .prog-h { display: flex; align-items: baseline; gap: 10px; padding: 10px 14px 0; color: var(--orca-fg); }
  .prog-h .sub, .pj-idle .sub { color: var(--orca-muted); }
  .pj { padding: 12px 14px 13px; color: var(--orca-fg); }
  .pj + .pj, .pj + .pj-idle, .pj-idle + .pj-idle { border-top: 1px solid var(--orca-border); }
  .pj-top { display: flex; align-items: center; gap: 10px; }
  .pj-name { font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pj-who { color: var(--orca-muted); white-space: nowrap; }
  .pj-chip { font-weight: 700; padding: 2px 8px; border-radius: 99px; white-space: nowrap; background: color-mix(in srgb, var(--orca-accent) 22%, transparent); color: #3cc9b9; }
  .pj-chip.warn { background: rgba(210, 153, 34, 0.22); color: #e0a030; }
  .pj-chip.ok { background: rgba(63, 185, 80, 0.18); color: #3fb950; }
  .pj-chip.bad { background: rgba(217, 83, 79, 0.2); color: #e5605c; }
  .pj-chip.idle { background: rgba(127, 127, 127, 0.2); color: var(--orca-muted); }
  .pj-pct { margin-left: auto; font-size: 18px; font-weight: 800; font-variant-numeric: tabular-nums; }
  .pj-track { height: 10px; margin: 9px 0 8px; border-radius: 5px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .pj-fill { height: 100%; border-radius: 5px; background: var(--orca-accent); transition: width .9s ease; }
  .pj-fill.run { background: linear-gradient(90deg, var(--orca-accent) 0 40%, color-mix(in srgb, var(--orca-accent) 35%, #fff) 50%, var(--orca-accent) 60% 100%); background-size: 250% 100%; animation: pj-run 4.8s linear infinite; }
  @keyframes pj-run { from { background-position: 100% 0; } to { background-position: -150% 0; } }
  .pj-fill.warn { background: #d29922; }
  .pj-fill.ok { background: #3fb950; }
  .pj-fill.bad { background: #d9534f; }
  .pj-fill.prep { width: 30% !important; background: repeating-linear-gradient(-45deg, var(--orca-accent) 0 8px, color-mix(in srgb, var(--orca-accent) 55%, transparent) 8px 16px); animation: pj-prep 1.6s linear infinite; }
  @keyframes pj-prep { from { margin-left: -30%; } to { margin-left: 100%; } }
  .pj-meta { display: flex; flex-wrap: wrap; gap: 4px 16px; color: var(--orca-muted); }
  .pj-meta b { color: var(--orca-fg); font-weight: 600; font-size: 11px; }
  .pj-slot { display: inline-flex; align-items: center; gap: 6px; }
  .pj-slot .d { width: 10px; height: 10px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.18); }
  .pj-idle { display: flex; align-items: center; gap: 10px; padding: 10px 14px; color: var(--orca-fg); }
  .pj-idle .pj-who { margin-left: auto; }
  .ams-unit .ubar { display: flex; gap: 8px; margin-left: auto; }
  .ams-unit .ubar .sbtn { margin-left: 0; }
  .ams-unit .usub { margin-left: 8px; }
  .lseg { flex-basis: 100%; display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--orca-muted); }
  .kseg { display: inline-flex; border: 1px solid var(--orca-border); border-radius: 7px; overflow: hidden; }
  .kseg button { background: transparent; color: var(--orca-fg); border: 0; padding: 6px 12px; font: inherit; font-size: 12px; cursor: pointer; }
  .kseg button + button { border-left: 1px solid var(--orca-border); }
  .kseg button.on { background: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 700; }
  .kchip { margin-left: 8px; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 99px; background: rgba(88, 166, 255, 0.18); color: #79b8ff; white-space: nowrap; }
  .ktest { flex-basis: 100%; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 12px; }
  .ktest:empty { display: none; }
  .ktest .ok { color: #3fb950; font-weight: 800; }
  .ktest .bad { color: #e5605c; font-weight: 800; }
  .ams-cell.active .spool-card { border-color: var(--orca-accent) !important; box-shadow: 0 0 0 1px var(--orca-accent); }
  .linkform { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px 12px; padding: 12px 14px; border-bottom: 1px solid var(--orca-border); }
  .linkform label { display: flex; flex-direction: column; gap: 4px; flex: 1 1 180px; font-size: 12px; color: var(--orca-muted); }
  .linkform input { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 8px 10px; font: inherit; font-size: 13px; }
  .linkform input:focus { outline: none; border-color: var(--orca-accent); }
  .linkform .bar { display: flex; gap: 8px; }
  .sbtn.primary, .sbtn.primary:hover { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  @media (max-width: 760px) { .ams-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
</head>
<body>
  <!--header--><div class="header">
    <div class="title">__LOGO__<h2>__PLUGIN_NAME__</h2></div>
    <div class="header-actions">
      <button id="settings-btn" title="Settings &amp; about">Settings</button>
    </div>
  </div><!--/header-->
  <div id="tabs" class="tabs">
    <button data-tab="spools" class="on">Spools</button>
    <button data-tab="printer" id="tab-printer">Printer<span id="tab-dot" hidden></span></button>
    <button data-tab="stats" id="tab-stats" hidden>Stats</button>
  </div>
  <div id="page-spools">
  <div class="listbar">
    <div id="status">Loading...</div>
    <button id="add-spool" title="Add a Spool to Spoolman" hidden>+ Add Spool</button>
    <button id="filters-toggle" aria-expanded="false">Filters &#9662;</button>
  </div>
  <div id="filters-panel" hidden>
  <div class="filters">
    <input id="search" type="text" placeholder="Filter by name...">
    <select id="material-filter"><option value="">All Materials</option></select>
    <select id="vendor-filter"><option value="">All Manufacturers</option></select>
  </div>
  <div class="filters">
    <select id="group-key">
      <option value="none">Group: None</option>
      <option value="location">Group: Location</option>
      <option value="material">Group: Material</option>
      <option value="vendor">Group: Manufacturer</option>
    </select>
    <select id="sort-key">
      <option value="name">Sort: Name</option>
      <option value="material">Sort: Material</option>
      <option value="vendor">Sort: Manufacturer</option>
      <option value="remaining" selected>Sort: Remaining Weight</option>
      <option value="used">Sort: Used Weight</option>
      <option value="first_used">Sort: First Used</option>
      <option value="last_used">Sort: Last Used</option>
      <option value="plate" disabled>Sort: On This Plate</option>
    </select>
    <button class="sort-dir" id="sort-dir" title="Toggle ascending/descending">&#9650;</button>
  </div>
  </div>

  <div id="preset-warn" class="dnt-warn preset-warn" hidden>
    <svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
    <span class="pw-txt" id="pw-txt"></span>
    <div class="pw-acts"><button type="button" class="sbtn" id="pw-open">Open Spoolman</button><button type="button" class="sbtn ghost" id="pw-dim">Dismiss</button></div>
  </div>

  <div id="plate" class="plate" hidden>
    <div class="divider">
      <span class="line"></span>
      <span class="divider-title">⚖️ This Plate</span>
      <span class="divider-sub" id="plate-sub"></span>
      <span class="line"></span>
      <span class="plate-pill" id="plate-pill"></span>
      <button id="plate-recheck" title="Check again against your spool source">Re-check</button>
      <button id="plate-dismiss" title="Hide until the next slice">Dismiss</button>
    </div>
    <div class="plate-rows" id="plate-rows"></div>
    <div class="divider"><span class="line"></span><span class="divider-title">All Spools</span><span class="line"></span></div>
  </div>

  <div id="list"></div>
  </div>
  <div id="page-printer" hidden>
    <div id="progress-slot"></div>
    <div id="ams-list"></div>
    <div id="plate-slot"></div>
    <div id="usage-slot"></div>
  </div>
  <div id="page-stats" hidden></div>

  <script>
    const statusEl = document.getElementById("status");
    const listEl = document.getElementById("list");
    const searchEl = document.getElementById("search");
    const materialEl = document.getElementById("material-filter");
    const vendorEl = document.getElementById("vendor-filter");
    const groupKeyEl = document.getElementById("group-key");
    const sortKeyEl = document.getElementById("sort-key");
    const sortDirBtn = document.getElementById("sort-dir");
    const fToggle = document.getElementById("filters-toggle");
    const fPanel = document.getElementById("filters-panel");
    const plateEl = document.getElementById("plate");
    const plateList = document.getElementById("plate-rows");
    const plateSort = sortKeyEl.querySelector('option[value="plate"]');

    let allSpools = [];
    let lowStockGrams = __LOW_STOCK_DEFAULT__;
    let wMode = "both";
    let cart = false;
    let canEdit = false;
    let units = "g";
    let show = { mat: true, hex: true, dia: true, loc: false };
    let colsMode = "1";
    function setCols() {
      if (window.__spoolioAp) listEl.classList.toggle("cols-2", colsMode === "2" || (colsMode === "auto" && window.innerWidth >= 900));
    }
    window.addEventListener("resize", setCols);
    window.__spoolioRelook = function (a) {
      wMode = { w: "grams", pct: "percent", both: "both" }[a.wfmt] || "both";
      units = a.units; show = a.show; colsMode = a.cols;
      setCols();
      if (allSpools.length) applyFilters();
    };
    let sortDir = "asc";
    let plate = null;
    let plateSlots = new Map();
    let plateOrder = new Map();
    let dupes = new Map();
    let lastSort = sortKeyEl.value;
    const tabsEl = document.getElementById("tabs");
    const tabDot = document.getElementById("tab-dot");
    const amsList = document.getElementById("ams-list");
    const plateSlot = document.getElementById("plate-slot");
    let tab = "spools";
    let linked = [];
    let cfg = [];
    let showTab = true;
    let armed = "";
    let renaming = "";
    let liveOn = false;
    let usage = { enabled: false, mode: "ask", prints: [] };
    let usageEdits = {};
    let usageBooking = false;
    let usageUndoing = false;
    let rbUndo = null;
    let dnt = null;
    let rbOpen = false;
    try { rbOpen = localStorage.getItem("spoolio.bookings") === "1"; } catch (err) {}
    let plateAttn = false;
    const usageSlot = document.getElementById("usage-slot");
    const progSlot = document.getElementById("progress-slot");
    let progress = { on: true, finish: true, slot: true, idle: "line" };
    let linkOpen = "";
    let liveData = { status: "off", printers: [] };
    let liveKey = "";
    let picking = null;
    const KINDS = { ams: 4, ht: 1, ext: 1, mmu: 4, tool: 1 };
    const RANK = { ams: 0, ht: 1, ext: 2, mmu: 3, tool: 4 };
    const GROW = { mmu: 16, tool: 8 };
    const NET = { klipper: "Moonraker", prusalink: "PrusaLink", octoprint: "OctoPrint" };
    let heads = null;
    let tests = {};
    try { const t = localStorage.getItem("spoolio.tab"); if (t === "printer" || t === "stats") tab = t; } catch (err) {}
    const statsEl = document.getElementById("page-stats");
    const statsBtn = document.getElementById("tab-stats");
    const printerBtn = document.getElementById("tab-printer");
    let stats = { on: true, cur: "spoolman" };
    let stData = null;
    let stPeriod = "30";
    let phOpen = false;
    let phShown = 7;
    try { stPeriod = localStorage.getItem("spoolio.period") || "30"; phOpen = localStorage.getItem("spoolio.history") === "1"; } catch (err) {}
    const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const ST_COLS = ["var(--orca-accent)", "#5b8def", "#e0a030", "#9b6bd6", "#4fb3a9", "#d9534f", "#888"];

    function esc(v) {
      return String(v).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    function swatchHex(v) {
      const m = String(v || "").trim().replace(/^#/, "").match(/^[0-9a-f]{6}/i);
      return m ? "#" + m[0] : "";
    }

    function swatchCols(f) {
      return String(f.multi_color_hexes || "").split(",").map(swatchHex).filter(Boolean);
    }

    // One solid colour for the ring and bar; the first colour on multi-colour spools.
    function swatchMain(f) {
      const cs = swatchCols(f);
      return (cs.length < 2 && swatchHex(f.color_hex)) || cs[0] || "#888";
    }

    function swatch(f) {
      const cs = swatchCols(f);
      if (cs.length < 2) return swatchMain(f);
      if (f.multi_color_direction === "longitudinal") return "linear-gradient(135deg, " + cs.join(", ") + ")";
      const step = 360 / cs.length;
      return "conic-gradient(" + cs.map(function (c, i) {
        return c + " " + i * step + "deg " + (i + 1) * step + "deg";
      }).join(", ") + ")";
    }

    function savePrinters() {
      lastSpools = "";
      orca.postMessage({ type: "printers", printers: cfg });
    }

    function buildPrinters() {
      const byName = new Map();
      allSpools.forEach(function (s) {
        const a = s.ams;
        if (!a || s.archived) return;
        const name = a.printer || "Printer";
        if (!byName.has(name)) byName.set(name, new Map());
        const units = byName.get(name);
        const unit = Number(a.unit);
        const key = unit >= 128 ? "ext" : (Number.isFinite(unit) ? unit : 0);
        if (!units.has(key)) units.set(key, new Map());
        units.get(key).set(key === "ext" ? 0 : Number(a.slot), s);
      });
      linked = [...byName.entries()].map(function (e) {
        const units = [...e[1].entries()].sort(function (x, y) {
          return (x[0] === "ext" ? 999 : x[0]) - (y[0] === "ext" ? 999 : y[0]);
        });
        return { name: e[0], units: units };
      });
    }

    function plainCard(title, sub, attrs, cls) {
      return '<div class="spool-card ' + cls + '"' + attrs + '><div class="spool-body">' +
        '<div class="spool-swatch-col"><div class="spool-swatch ams-dash"></div></div>' +
        '<div class="spool-main"><div class="spool-top"><div class="spool-title">' + title + '</div></div>' +
        '<div class="spool-bar-track"></div>' +
        '<div class="spool-subtitle"><span class="sub-text">' + sub + '</span></div></div>' +
        '</div></div>';
    }

    function amsCellHtml(label, s) {
      const card = s ? spoolRowHtml(s) : plainCard("Empty", "No spool linked", "", "ams-empty");
      return '<div class="ams-cell"><div class="ams-n">' + label + '</div>' + card + '</div>';
    }

    function syncDot() {
      tabDot.hidden = !(plateAttn || (usage.enabled && usage.prints.some(function (p) { return p.state === "pending"; })));
    }

    function usageTime(t) {
      return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function usageGrams(g) {
      return (Math.round(g * 10) / 10) + " g";
    }

    function spoolOptions(row) {
      const mat = String(row.material || "").toLowerCase();
      const list = allSpools.filter(function (s) { return !s.archived; }).sort(function (x, y) {
        const mx = String((x.filament || {}).material || "").toLowerCase() === mat ? 0 : 1;
        const my = String((y.filament || {}).material || "").toLowerCase() === mat ? 0 : 1;
        return mx - my || ((x.filament || {}).name || "").localeCompare((y.filament || {}).name || "");
      }).slice(0, 300);
      return '<option value="">Choose a Spool...</option>' + list.map(function (s) {
        const f = s.filament || {};
        const label = [(f.vendor || {}).name, f.name || "Spool " + s.id, "#" + s.id, formatWeight(s.remaining_weight)].filter(Boolean).join(" \u00b7 ");
        return '<option value="' + esc(s.id) + '">' + esc(label) + '</option>';
      }).join("");
    }

    function usageRow(e, r) {
      const edit = (usageEdits[e.id] || {})[r.slot] || {};
      const grams = edit.grams !== undefined ? edit.grams : r.grams;
      const colour = r.colour ? "#" + String(r.colour).replace(/^#/, "") : "#888";
      let who;
      if (r.state === "booked") {
        who = esc(r.label) + '<span class="u-chip">Booked ' + usageGrams(r.grams) + '</span>';
      } else if (r.spool) {
        who = esc(r.label) + (r.how === "rfid" ? '<span class="u-chip">Matched by RFID</span>' :
          r.how === "gate" ? '<span class="u-chip">Matched by Gate</span>' : r.how === "tool" ? '<span class="u-chip">Set on Toolhead</span>' : "");
      } else {
        who = '<select data-usage="spool">' + spoolOptions(r).replace('value="' + esc(edit.spool || "") + '"', 'value="' + esc(edit.spool || "") + '" selected') + '</select>' +
          '<span class="u-chip warn">Not Identified</span>';
      }
      const input = r.state === "booked" ? "" : '<input class="u-g" data-usage="grams" type="number" min="0" step="0.1" value="' + esc(grams) + '"> g';
      return '<div class="u-row" data-slot="' + r.slot + '"><span class="d" style="background:' + esc(colour) + '"></span>' +
        '<span class="u-slot">' + (r.where ? esc(r.where) : "Slot " + r.slot) + (r.material ? " \u00b7 " + esc(r.material) : "") + '</span>' +
        '<span class="u-spool">' + who + '</span>' + input +
        (r.error ? '<div class="u-err">' + esc(r.error) + '</div>' : "") + '</div>';
    }

    function rbWhen(t) {
      const d = new Date(t * 1000);
      const hm = usageTime(t);
      const days = Math.round((new Date(new Date().toDateString()) - new Date(d.toDateString())) / 864e5);
      return days === 0 ? "Today " + hm : days === 1 ? "Yesterday " + hm :
        d.getDate() + " " + d.toLocaleString("en-GB", { month: "short" }) + " " + hm;
    }

    function rbTotal(p) {
      return p.rows.reduce(function (a, r) { return a + r.grams; }, 0);
    }

    function rbCard(p) {
      const result = p.result === "finished" ? "Finished" : p.result === "unknown" ? "Result Unknown" : "Stopped at " + p.pct + "%";
      const chip = p.state === "booked" ? '<span class="u-chip">Booked ' + usageGrams(rbTotal(p)) + '</span>' :
        p.state === "undone" ? '<span class="u-chip off">Undone</span>' : '<span class="u-chip warn">Not Booked</span>';
      const busy = usageBooking || usageUndoing;
      const btn = p.state === "booked" ? '<button class="sbtn" data-usage="undo"' + (busy ? " disabled" : "") + '>Undo</button>' :
        '<button class="sbtn primary" data-usage="book"' + (busy ? " disabled" : "") + '>' + (usageBooking === p.id ? "Booking..." : "Book") + '</button>';
      const rows = p.state === "pending" ? p.rows.map(function (r) { return usageRow(p, r); }).join("") :
        p.rows.map(function (r) {
          const colour = r.colour ? "#" + String(r.colour).replace(/^#/, "") : "#888";
          return '<div class="u-row"><span class="d" style="background:' + esc(colour) + '"></span>' +
            '<span class="u-slot">' + (r.where ? esc(r.where) : "Slot " + r.slot) + (r.material ? " · " + esc(r.material) : "") + '</span>' +
            '<span class="u-spool">' + esc(r.label) + '</span><span class="u-amt">' + usageGrams(r.grams) + '</span>' +
            (r.error ? '<div class="u-err">' + esc(r.error) + '</div>' : "") + '</div>';
        }).join("");
      const note = p.state !== "pending" ? "" :
        p.tracked ? "Moonraker is linked to Spoolman and may have booked this print already. Check Spoolman before booking it here." :
        p.late ? "Spoolio lost contact with the printer during this print, so check the grams before booking." :
        p.result === "finished" ? "" : "The print did not finish, so the grams are an estimate from its progress.";
      return '<div class="u-card' + (p.state === "undone" ? " undone" : "") + '" data-run="' + esc(p.id) + '"><div class="u-h"><b>' + esc(p.name) + '</b>' +
        '<span class="sub">' + esc(p.printer) + ' · ' + result + ' · ' + rbWhen(p.time) + '</span>' + chip + btn + '</div>' + rows +
        (note ? '<div class="ams-note" style="margin: 6px 0 0">' + note + '</div>' : "") + '</div>';
    }

    function rbPop() {
      const p = usage.prints.find(function (x) { return x.id === rbUndo; });
      if (!p) return "";
      const rows = p.rows.filter(function (r) { return r.state === "booked"; });
      const spools = rows.map(function (r) {
        const s = allSpools.find(function (x) { return x.id === r.spool; }) || {};
        const left = s.remaining_weight || 0;
        const colour = r.colour ? "#" + String(r.colour).replace(/^#/, "") : "#888";
        return '<div class="rb-sp"><div class="n"><span class="d" style="background:' + esc(colour) + '"></span>' + esc(r.label) + '</div>' +
          '<div class="w">' + usageGrams(left) + ' + ' + usageGrams(r.grams) + ' = <b>' + usageGrams(left + r.grams) + '</b></div></div>';
      }).join("");
      const many = rows.length > 1;
      return '<div class="rb-mask" data-usage="cancel"><div class="rb-pop"><b class="t">Undo This Booking?</b>' +
        '<div>' + esc(p.name) + ' used ' + usageGrams(rbTotal({ rows: rows })) + (many ? ' from ' + rows.length + ' spools' : '') +
        '. Spoolio will add it back to ' + (many ? 'each spool' : 'the spool') + ' in Spoolman.</div>' +
        '<div class="rb-tbl">' + spools + '</div>' +
        '<div class="sub">The print stays in Recent Bookings, so you can book it again later.</div>' +
        '<div class="rb-bar"><button class="sbtn ghost" data-usage="cancel">Cancel</button><button class="sbtn primary" data-usage="unbook"' +
        (usageUndoing ? " disabled" : "") + '>' + (usageUndoing ? "Adding Back..." : "Add Back to Spoolman") + '</button></div></div></div>';
    }

    function dntPop() {
      const fresh = dnt.fresh, warn = dnt.warn;
      dnt.fresh = dnt.warn = false;
      return '<div class="rb-mask' + (fresh ? ' dnt-in' : '') + '" data-usage="dnt-cancel"><div class="rb-pop"><b class="t">Stop Tracking Filament?</b>' +
        '<div class="dnt-warn"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/></svg>' +
        '<span>Spoolio will no longer consume filament from your spools in Spoolman when a print finishes.' +
        (dnt.stats ? '' : ' <span' + (warn ? ' class="dnt-in"' : '') + '>Turning off booking also stops Stats tracking unless Keep Tracking Stats is ticked.</span>') +
        '</span></div>' +
        '<label class="dnt-opt' + (dnt.stats ? ' on' : '') + '"><input type="checkbox" data-usage="dnt-stats"' + (dnt.stats ? ' checked' : '') + '>' +
        '<span><b>Keep Tracking Stats</b></span></label>' +
        '<div class="rb-bar"><button class="sbtn ghost" data-usage="dnt-cancel">Cancel</button><button class="sbtn primary" data-usage="dnt-ok">' +
        (dnt.stats ? 'Stop Booking Only' : 'Stop Tracking') + '</button></div></div></div>';
    }

    function renderUsage() {
      syncDot();
      if (!usage.enabled || (!liveOn && !usage.prints.length)) { usageSlot.innerHTML = ""; return; }
      const mode = '<label>When a Print Finishes <select data-usage="mode">' + [["ask", "Ask Me Before Booking"], ["auto", "Book Automatically"], ["off", "Do Not Track"]].map(function (m) {
        return '<option value="' + m[0] + '"' + (usage.mode === m[0] ? " selected" : "") + '>' + m[1] + '</option>';
      }).join("") + '</select></label>';
      const note = usage.mode === "off" ? '<div class="dnt-note">' + (usage.stats ? 'Prints are still recorded in Stats. Your spools in Spoolman are not changed.' :
        'Prints are not booked or recorded in Stats.') + '</div>' : "";
      const chip = usage.mode === "off" ? '<span class="u-chip' + (usage.stats ? '' : ' warn') + '">' + (usage.stats ? 'Stats Only' : 'Not Tracking') + '</span>' : "";
      const list = usage.prints.slice(0, 5);
      const todo = list.filter(function (p) { return p.state === "pending"; }).length;
      const head = list.length ? '<div class="rb-h" data-usage="toggle"><span class="arrow">' + (rbOpen ? "▾" : "▸") + '</span><b>Recent Bookings</b>' +
        '<span class="sub">Last ' + list.length + (list.length === 1 ? ' Print' : ' Prints') + '</span>' +
        (todo ? '<span class="u-chip warn">' + todo + ' To Book</span>' : "") + '</div>' : "";
      const body = list.length && rbOpen ? '<div class="rb-list">' + list.map(rbCard).join("") + '</div>' : "";
      usageSlot.innerHTML = '<div class="usage"><div class="usage-bar"><b>Filament Usage</b>' + chip + '<span class="sub"></span>' + mode + note + '</div>' +
        head + body + '</div>' + (rbUndo ? rbPop() : "") + (dnt ? dntPop() : "");
    }

    function setUsage(u, done) {
      usage = u;
      Object.keys(usageEdits).forEach(function (id) {
        if (!usage.prints.some(function (e) { return e.id === id && e.state === "pending"; })) delete usageEdits[id];
      });
      if (done && (usageBooking || usageUndoing)) {
        usageBooking = false;
        if (usageUndoing) { usageUndoing = false; rbUndo = null; }
        orca.postMessage({ type: "refresh" });
      }
      const at = document.activeElement;
      if (at && usageSlot.contains(at) && at.matches('[data-usage="spool"], [data-usage="grams"]')) syncDot(); else renderUsage();
    }

    usageSlot.addEventListener("change", function (e) {
      const kind = e.target.dataset.usage;
      const card = e.target.closest(".u-card");
      const row = e.target.closest(".u-row");
      if (kind === "dnt-stats") {
        dnt.stats = e.target.checked;
        dnt.warn = !dnt.stats;
        renderUsage();
      } else if (kind === "mode" && e.target.value === "off") {
        dnt = { from: usage.mode, stats: !!usage.stats, fresh: true };
        renderUsage();
      } else if (kind === "mode") {
        usage.mode = e.target.value;
        orca.postMessage({ type: "usage_mode", mode: e.target.value });
      } else if (kind === "spool" && card && row) {
        const ed = usageEdits[card.dataset.run] = usageEdits[card.dataset.run] || {};
        (ed[row.dataset.slot] = ed[row.dataset.slot] || {}).spool = e.target.value;
      }
    });

    usageSlot.addEventListener("input", function (e) {
      const card = e.target.closest(".u-card");
      const row = e.target.closest(".u-row");
      if (e.target.dataset.usage === "grams" && card && row) {
        const ed = usageEdits[card.dataset.run] = usageEdits[card.dataset.run] || {};
        (ed[row.dataset.slot] = ed[row.dataset.slot] || {}).grams = e.target.value;
      }
    });

    usageSlot.addEventListener("click", function (e) {
      const btn = e.target.closest("[data-usage]");
      if (!btn || btn.disabled) return;
      const kind = btn.dataset.usage;
      const card = e.target.closest(".u-card");
      if (kind === "dnt-cancel") {
        if (e.target !== btn) return;
        dnt = null;
      } else if (kind === "dnt-ok") {
        usage.mode = "off";
        usage.stats = dnt.stats;
        dnt = null;
        orca.postMessage({ type: "usage_mode", mode: "off", stats: usage.stats });
      } else if (kind === "dnt-stats") {
        return;
      } else if (kind === "toggle") {
        rbOpen = !rbOpen;
        try { localStorage.setItem("spoolio.bookings", rbOpen ? "1" : "0"); } catch (err) {}
      } else if (kind === "cancel") {
        if (e.target !== btn || usageUndoing) return;
        rbUndo = null;
      } else if (kind === "undo" && card) {
        rbUndo = card.dataset.run;
      } else if (kind === "unbook" && rbUndo && !usageUndoing) {
        usageUndoing = true;
        orca.postMessage({ type: "usage_undo", run: rbUndo });
      } else if (kind === "book" && card && !usageBooking) {
        const rows = [...card.querySelectorAll(".u-row")].map(function (row) {
          const sel = row.querySelector('select[data-usage="spool"]');
          const g = row.querySelector('input[data-usage="grams"]');
          if (!g) return null;
          const out = { slot: Number(row.dataset.slot), grams: Number(g.value) };
          if (sel && sel.value) out.spool = sel.value;
          return out;
        }).filter(Boolean);
        usageBooking = card.dataset.run;
        orca.postMessage({ type: "usage_book", run: card.dataset.run, rows: rows });
      } else {
        return;
      }
      renderUsage();
    });

    function liveByLink(id) {
      return liveData.printers.find(function (x) { return x.link === id; }) || null;
    }

    function linkStatus(p) {
      if (NET[p.type]) {
        if (!p.ip) return "";
        const kl = liveByLink(p.id);
        const ks = (liveData.links || {})[p.id];
        if (kl && ks === "live") {
          return '<span class="live-dot' + (kl.age > 180 ? " stale" : "") + '"></span>Live via ' + NET[p.type] +
            (kl.books ? '<span class="kchip" title="The printer is linked to Spoolman and books usage itself">Printer Books Usage</span>' : "");
        }
        if (ks === "auth") return NET[p.type] + (p.type === "prusalink" ? " refused the password" : " refused the API key");
        if (ks === "unreachable") return "Can't reach " + esc(p.ip) + " - is " + NET[p.type] + " running?";
        return "Connecting to " + esc(p.ip) + "...";
      }
      if (!p.ip || !p.has_code) return "";
      const lv = liveByLink(p.id);
      const st = (liveData.links || {})[p.id];
      if (lv && st === "live") {
        return '<span class="live-dot' + (lv.age > 180 ? " stale" : "") + '"></span>Live via LAN Mode';
      }
      if (st === "auth") return "Wrong access code";
      if (st === "serial") return "The printer needs its serial number - add it under Live Link";
      if (st === "unreachable") return "Can't reach " + esc(p.ip) + " - is the printer on and in LAN mode?";
      if (st === "waiting") return "Connected, waiting for the printer to report";
      return "Connecting to " + esc(p.ip) + "...";
    }

    function linkForm(p) {
      const t = p.draft || p.type || "bambu";
      const seg = '<div class="lseg"><span>Printer Type</span><div class="kseg">' + [["bambu", "Bambu Lab"], ["klipper", "Klipper (Moonraker)"], ["prusalink", "PrusaLink"], ["octoprint", "OctoPrint"]].map(function (x) {
        return '<button class="' + (t === x[0] ? "on" : "") + '" data-act="link-type" data-t="' + x[0] + '" data-p="' + esc(p.id) + '">' + x[1] + '</button>';
      }).join("") + '</div></div>';
      const bar = '<div class="bar"><button class="sbtn" data-act="link-save" data-p="' + esc(p.id) + '">Save</button>' +
        (NET[t] ? '<button class="sbtn ghost" data-act="link-test" data-p="' + esc(p.id) + '">Test</button>' : "") +
        '<button class="sbtn ghost" data-act="link-cancel" data-p="' + esc(p.id) + '">Cancel</button>' +
        (p.ip ? '<button class="sbtn ghost" data-act="link-remove" data-p="' + esc(p.id) + '">Remove Link</button>' : "") + '</div>';
      const saved = p.has_code && t === p.type ? "Saved - leave blank to keep" : "";
      const ip = t === p.type ? p.ip || "" : "";
      const note = function (s) { return '<div class="ams-note" style="flex-basis: 100%; margin: 0">' + s + '</div>'; };
      const test = '<div class="ktest">' + (tests[p.id] || "") + '</div>' + (p.orca ? '<div class="ktest">Fills its toolheads from slices for the OrcaSlicer printer &ldquo;' + esc(p.orca) + '&rdquo;' +
        '<button class="sbtn ghost" data-act="heads-unbind" data-p="' + esc(p.id) + '">Stop</button></div>' : "");
      if (t === "prusalink") {
        return '<div class="linkform" data-t="prusalink">' + seg +
          '<label>Printer Address<input data-f="ip" value="' + esc(ip) + '" placeholder="192.168.1.70" autocomplete="off"></label>' +
          '<label>Username<input data-f="user" value="' + esc(p.user || "maker") + '" autocomplete="off"></label>' +
          '<label>Password<input data-f="code" type="password" autocomplete="off" placeholder="' + (saved || "Settings > Network > PrusaLink") + '"></label>' +
          bar + test + note("The address and password are on the printer under Settings > Network > PrusaLink. " +
          "Spoolio only reads from the printer. PrusaLink doesn&#39;t say which toolhead is printing, so assign a spool to each toolhead here.") + '</div>';
      }
      if (t === "octoprint") {
        return '<div class="linkform" data-t="octoprint">' + seg +
          '<label>OctoPrint Address<input data-f="ip" value="' + esc(ip) + '" placeholder="octopi.local" autocomplete="off"></label>' +
          '<label>API Key<input data-f="code" type="password" autocomplete="off" placeholder="' + (saved || "Settings > Application Keys") + '"></label>' +
          bar + test + note("Make an application key for Spoolio in OctoPrint under Settings > Application Keys. Spoolio only reads from OctoPrint and never sends G-code. " +
          "If an OctoPrint Spoolman plugin is installed, it may book usage itself, so check before booking a print in Spoolio.") + '</div>';
      }
      if (t === "klipper") {
        return '<div class="linkform" data-t="klipper">' + seg +
          '<label>Moonraker Address<input data-f="ip" value="' + esc(ip) + '" placeholder="voron.local or 192.168.1.60:7125" autocomplete="off"></label>' +
          '<label>API Key<input data-f="code" type="password" autocomplete="off" placeholder="' + (saved || "Optional - only if Moonraker asks for one") + '"></label>' +
          bar + test + note("Use the address you open Mainsail or Fluidd with. Spoolio only reads from Moonraker and never sends G-code. " +
          "If Moonraker is linked to Spoolman, the printer may book its own usage, so check before booking a print in Spoolio.") + '</div>';
      }
      return '<div class="linkform" data-t="bambu">' + seg +
        '<label>IP Address<input data-f="ip" value="' + esc(p.ip || "") + '" placeholder="192.168.1.50" autocomplete="off"></label>' +
        '<label>Serial Number<input data-f="serial" value="' + esc(p.serial || "") + '" placeholder="Optional - found automatically" autocomplete="off"></label>' +
        '<label>Access Code<input data-f="code" type="password" autocomplete="off" placeholder="' + (saved || "Shown on the printer") + '"></label>' +
        bar +
        '<div class="ams-note" style="flex-basis: 100%; margin: 0">The IP address and access code are in the printer&#39;s network settings, with LAN mode on. ' +
        'Spoolio keeps the access code on this computer and only reads from the printer.</div></div>';
    }

    function liveCard(t) {
      const ks = t.spool_id ? allSpools.find(function (x) { return String(x.id) === String(t.spool_id); }) : null;
      if (ks) return spoolRowHtml(ks);
      if (t.empty) return plainCard("Empty", "Nothing loaded", "", "ams-empty");
      const color = t.colour ? "#" + t.colour : "#888";
      const pct = t.remain === null ? null : Math.round(t.remain);
      const sub = [t.type, t.colour ? "#" + t.colour : ""].filter(Boolean).join(" \u00b7 ");
      const rfid = t.rfid ? '<div class="spool-rfid" title="RFID spool"><span>RFID</span></div>' : "";
      return '<div class="spool-card"><div class="spool-body">' +
        '<div class="spool-swatch-col"><div class="spool-swatch" style="background:' + color + '"></div>' + rfid + '</div>' +
        '<div class="spool-main"><div class="spool-top"><div class="spool-title">' + esc(t.name || t.type) + '</div>' +
        '<div class="spool-weight">' + (pct === null ? "" : pct + "%") + '</div></div>' +
        '<div class="spool-bar-track" title="' + (pct === null ? "Remaining unknown" : pct + "% remaining") + '">' +
        '<div class="spool-bar-fill" style="width:' + (pct === null ? 0 : pct) + '%;background:' + color + '"></div></div>' +
        '<div class="spool-subtitle"><span class="sub-text">' + esc(sub) + '</span></div></div></div></div>';
    }

    const PROG_STATES = {
      RUNNING: ["Printing", "", "run"], PREPARE: ["Preparing", "", "prep"], SLICING: ["Preparing", "", "prep"],
      PAUSE: ["Paused", "warn", "warn"], FINISH: ["Finished", "ok", "ok"], FAILED: ["Failed", "bad", "bad"]
    };
    const PROG_LIVE = ["RUNNING", "PREPARE", "SLICING", "PAUSE"];

    function activeTray(lv) {
      const j = lv.job;
      return progress.on && progress.slot && j && j.tray && PROG_LIVE.indexOf(j.state) >= 0 ? j.tray : null;
    }

    function liveUnits(lv) {
      let ams = 0;
      const act = activeTray(lv);
      const unitHtml = function (label, trays, ext, uid) {
        return '<div class="ams-unit">' + label + '</div><div class="ams-grid">' + trays.map(function (t, i) {
          const on = act && act.unit === uid && act.slot === i;
          return '<div class="ams-cell' + (on ? " active" : "") + '"><div class="ams-n">' + (ext ? "E" : i + 1) + '</div>' + liveCard(t) + '</div>';
        }).join("") + '</div>';
      };
      let units = lv.units.map(function (u) {
        if (u.kind === "ht") return unitHtml("AMS HT", u.trays, false, u.id);
        ams += 1;
        return unitHtml("AMS " + ams, u.trays, false, u.id);
      }).join("");
      if (lv.ext) units += unitHtml("External Spool", [lv.ext], true, "ext");
      return units || '<div class="pnote">Waiting for the printer to report its slots...</div>';
    }

    function linkedPanel(p) {
      let count = 0;
      const units = p.units.map(function (u) {
        const ext = u[0] === "ext";
        count += u[1].size;
        const top = Math.max(3, ...[...u[1].keys()].filter(Number.isFinite));
        const slots = ext ? [0] : Array.from({ length: top + 1 }, function (_, i) { return i; });
        return '<div class="ams-unit">' + (ext ? "External Spool" : "AMS " + (u[0] + 1)) + '</div>' +
          '<div class="ams-grid">' + slots.map(function (i) {
            return amsCellHtml(ext ? "E" : i + 1, u[1].get(i));
          }).join("") + '</div>';
      }).join("");
      return '<div class="ams"><div class="ams-h"><span>' + esc(p.name) + '</span>' +
        '<span class="sub">' + count + (count === 1 ? " spool" : " spools") + ' linked from Bambu Cloud</span></div>' +
        '<div class="ams-body">' + units +
        '<div class="ams-note">A representative view only, built from Bambu Cloud. ' +
        'It can differ from what is physically loaded.</div></div></div>';
    }

    function unitLabel(p, u) {
      const same = p.units.filter(function (x) { return x.kind === u.kind; });
      const n = same.indexOf(u) + 1;
      if (u.kind === "ams") return "AMS " + n;
      if (u.kind === "ht") return same.length > 1 ? "AMS HT " + n : "AMS HT";
      if (u.kind === "mmu") return "MMU";
      if (u.kind === "tool") return u.slots.length > 1 ? "Toolheads" : "Toolhead";
      return "External Spool";
    }

    function manualCell(p, ui, si, byId, act) {
      const u = p.units[ui];
      const id = u.slots[si];
      const lv = NET[p.type] ? liveByLink(p.id) : null;
      const auto = !id && lv && u.kind === "tool" && u.slots.length === 1 && lv.active_spool ? byId.get(String(lv.active_spool)) : null;
      const s = id ? byId.get(String(id)) : auto;
      const where = ' data-p="' + esc(p.id) + '" data-u="' + ui + '" data-s="' + si + '"';
      let card;
      if (s) {
        card = spoolRowHtml(s);
      } else if (id) {
        card = plainCard("Spool not found", "It may have been deleted or archived", "", "ams-empty");
      } else {
        card = plainCard("+ Assign Spool", "No spool assigned", ' data-act="assign"' + where, "ams-empty ams-assign");
      }
      const x = id ? '<button class="ams-x" data-act="clear"' + where + ' title="Remove from slot">✕</button>' : "";
      const open = picking && picking.p === p.id && picking.u === ui && picking.s === si && !id;
      const n = u.kind === "ext" ? "E" : u.kind === "tool" ? "T" + si : u.kind === "mmu" ? si : si + 1;
      const on = act && act.unit === u.kind && act.slot === si;
      return '<div class="ams-cell' + (on ? " active" : "") + '"' + (auto ? ' title="The active spool in Moonraker"' : "") + '><div class="ams-n">' + n + '</div>' +
        x + card + (open ? pickerHtml() : "") + '</div>';
    }

    function takenIds() {
      const set = new Set();
      cfg.forEach(function (p) {
        p.units.forEach(function (u) { u.slots.forEach(function (id) { if (id) set.add(String(id)); }); });
      });
      return set;
    }

    function pickItems(query) {
      const taken = takenIds();
      const q = query.trim().toLowerCase();
      const items = allSpools.filter(function (s) {
        if (s.archived || taken.has(String(s.id))) return false;
        if (!q) return true;
        const f = s.filament || {};
        return [f.name, f.material, (f.vendor || {}).name, s.location, f.color_hex].join(" ").toLowerCase().includes(q);
      }).sort(function (x, y) {
        return ((x.filament || {}).name || "").localeCompare((y.filament || {}).name || "");
      }).slice(0, 200);
      if (!items.length) return '<div class="pnote">No spools left to assign.</div>';
      return items.map(function (s) {
        const f = s.filament || {};
        const sub = [(f.vendor || {}).name, typeof s.id === "number" ? "#" + s.id : "", s.location].filter(Boolean).join(" \u00b7 ");
        const color = swatch(f);
        return '<div class="pi" data-act="pick" data-id="' + esc(s.id) + '"><span class="d" style="background:' + esc(color) + '"></span>' +
          '<span>' + esc(f.name || "Spool " + s.id) + '<small>' + esc(sub) + '</small></span>' +
          '<span class="m">' + formatWeight(s.remaining_weight) + '</span></div>';
      }).join("");
    }

    function pickerHtml() {
      return '<div class="picker"><input type="text" placeholder="Search spools" autocomplete="off">' +
        '<div class="pick-list">' + pickItems("") + '</div>' +
        '<div class="pnote">Spools already in a slot are not listed.</div></div>';
    }

    function liveMmu(lv, act) {
      const mu = lv && lv.units.find(function (u) { return u.kind === "mmu"; });
      if (!mu) return "";
      return '<div class="ams-unit"><span>MMU</span><span class="usub">Happy Hare \u00b7 ' + mu.trays.length + ' Gates</span></div>' +
        '<div class="ams-grid">' + mu.trays.map(function (t, i) {
          const on = act && act.unit === "mmu" && act.slot === i;
          return '<div class="ams-cell' + (on ? " active" : "") + '"><div class="ams-n">' + i + '</div>' + liveCard(t) + '</div>';
        }).join("") + '</div>';
    }

    function headsNote(p) {
      if (!heads || !NET[p.type]) return "";
      const preset = "&ldquo;" + esc(heads.preset) + "&rdquo;";
      if (heads.choose) {
        return heads.choose.indexOf(p.id) < 0 ? "" : '<div class="ams-note heads">Your last slice was for the OrcaSlicer printer ' + preset + '. ' +
          '<button class="sbtn" data-act="heads-bind" data-p="' + esc(p.id) + '">Fill These Toolheads From It</button></div>';
      }
      if (heads.printer !== p.id) return "";
      const stop = '<button class="sbtn ghost" data-act="heads-unbind" data-p="' + esc(p.id) + '">Stop Filling From Slices</button>';
      const what = function (r) { return r.kind === "mmu" ? "Gate " + r.n : "T" + r.n; };
      if (heads.error) {
        return '<div class="ams-note heads">' + (heads.error === "hh" ? "Happy Hare sets the gates on this printer, so Spoolio left them alone." :
          heads.error === "unit" ? "Add a toolhead or MMU so Spoolio can fill it from your slices." :
          "Couldn&#39;t reach your spool source, so the toolheads weren&#39;t filled from the slice.") + stop + '</div>';
      }
      const set = heads.rows.filter(function (r) { return r.state === "set" || r.state === "kept"; }).map(what);
      const ask = heads.rows.filter(function (r) { return r.state === "pick"; }).map(function (r) { return what(r) + " (" + r.count + " spools match " + esc(r.name) + ")"; });
      const none = heads.rows.filter(function (r) { return r.state === "none"; }).map(function (r) { return what(r) + " (" + esc(r.name) + ")"; });
      return '<div class="ams-note heads">Filled from your slice at ' + esc(heads.time) + ' for ' + preset + '. ' +
        (set.length ? set.join(", ") + (set.length === 1 ? " is" : " are") + " set. " : "") +
        (ask.length ? "Pick the spool on " + ask.join(", ") + ". " : "") +
        (none.length ? "No free spool matches " + none.join(", ") + ". " : "") + stop + '</div>';
    }

    function manualPanel(p, byId) {
      let total = 0;
      let used = 0;
      const kl = !!NET[p.type];
      const klv = kl && (liveData.links || {})[p.id] === "live" ? liveByLink(p.id) : null;
      const hh = klv ? liveMmu(klv, activeTray(klv)) : "";
      const units = p.units.map(function (u, ui) {
        if (hh && (u.kind === "mmu" || (u.kind === "tool" && u.slots.length === 1))) return "";
        total += u.slots.length;
        used += u.slots.filter(Boolean).length;
        const label = unitLabel(p, u);
        const key = "u:" + p.id + ":" + ui;
        const rm = '<button class="sbtn ghost' + (armed === key ? " armed" : "") + '" data-act="remove-unit" data-p="' + esc(p.id) +
          '" data-u="' + ui + '">' + (armed === key ? "Click Again to Remove" : "Remove " + label) + '</button>';
        const one = u.kind === "mmu" ? "Gate" : "Toolhead";
        const size = GROW[u.kind] && kl ? '<button class="sbtn ghost" data-act="shrink" data-p="' + esc(p.id) + '" data-u="' + ui + '"' +
          (u.slots.length > 1 ? "" : " disabled") + '>\u2212 ' + one + '</button>' +
          '<button class="sbtn ghost" data-act="grow" data-p="' + esc(p.id) + '" data-u="' + ui + '"' +
          (u.slots.length < GROW[u.kind] ? "" : " disabled") + '>+ ' + one + '</button>' : "";
        const cells = u.slots.map(function (_, si) { return manualCell(p, ui, si, byId, klv ? activeTray(klv) : null); }).join("");
        return '<div class="ams-unit"><span>' + label + '</span><span class="ubar">' + size + rm + '</span></div><div class="ams-grid">' + cells + '</div>';
      }).join("") + hh;
      const hasExt = p.units.some(function (u) { return u.kind === "ext"; });
      const pk = "p:" + p.id;
      const name = renaming === p.id
        ? '<input class="pname-in" data-p="' + esc(p.id) + '" value="' + esc(p.name) + '" maxlength="40">'
        : '<button class="pname" data-act="rename" data-p="' + esc(p.id) + '" title="Rename">' + esc(p.name) + ' \u270e</button>';
      const lvl = liveByLink(p.id);
      const live = !!lvl && (liveData.links || {})[p.id] === "live";
      let sub = total ? used + " of " + total + " slots assigned" : kl ? "Add a toolhead or MMU to start" : "Add an AMS or external spool to start";
      const ls = linkStatus(p);
      if (ls) sub = ls;
      const bar = '<div class="bar">' +
        (live ? "" : '<button class="sbtn primary" data-act="link" data-p="' + esc(p.id) + '">Live Link</button>') +
        (kl ? (p.units.some(function (u) { return u.kind === "tool"; }) ? "" : '<button class="sbtn" data-act="add-unit" data-kind="tool" data-p="' + esc(p.id) + '">+ Toolhead</button>') +
        (hh || p.units.some(function (u) { return u.kind === "mmu"; }) ? "" : '<button class="sbtn" data-act="add-unit" data-kind="mmu" data-p="' + esc(p.id) + '">+ MMU</button>') :
        live ? "" : '<button class="sbtn" data-act="add-unit" data-kind="ams" data-p="' + esc(p.id) + '">+ AMS</button>' +
        '<button class="sbtn" data-act="add-unit" data-kind="ht" data-p="' + esc(p.id) + '">+ AMS HT</button>') +
        (hasExt || live || kl ? "" : '<button class="sbtn" data-act="add-unit" data-kind="ext" data-p="' + esc(p.id) + '">+ External Spool</button>') +
        '<button class="sbtn' + (armed === pk ? " armed" : "") + '" data-act="remove-printer" data-p="' + esc(p.id) + '">' +
        (armed === pk ? "Click Again to Remove" : "Remove Printer") + '</button></div>';
      return '<div class="ams"><div class="ams-h">' + name + '<span class="sub">' + sub + '</span>' + bar + '</div>' +
        (linkOpen === p.id ? linkForm(p) : "") +
        '<div class="ams-body">' + (live && !kl ? liveUnits(lvl) : units) + headsNote(p) +
        (kl && live ? '<div class="ams-note">' + (hh && !total ? "The gates and their spools come from Happy Hare." :
        (hh ? "The MMU gates come from Happy Hare. " : "") + 'Assign the spool on each toolhead' + (hh ? " here." : " or MMU gate here.") +
        ' Spoolio uses them to book each tool&#39;s filament.') + '</div>' :
        live ? '' : '<div class="ams-note">A representative view only: built from the spools you&#39;ve assigned. Use Live Link to read the printer itself.</div>') + '</div></div>';
    }

    function progDur(m) {
      const h = Math.floor(m / 60);
      return (h ? h + " h " : "") + String(m % 60).padStart(h ? 2 : 1, "0") + " min";
    }

    function progClock(ms) {
      return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function progTray(lv, t) {
      if (t.unit === "ext") return lv.ext ? { label: "External Spool", tray: lv.ext } : null;
      const u = lv.units.find(function (x) { return x.id === t.unit; });
      if (lv.kind === "net") return klTray(lv, t);
      if (!u || !u.trays[t.slot]) return null;
      const label = u.kind === "ht" ? "AMS HT" : "AMS " + (lv.units.filter(function (x) { return x.kind === "ams"; }).indexOf(u) + 1) + " Slot " + (t.slot + 1);
      return { label: label, tray: u.trays[t.slot] };
    }

    function spoolTray(id) {
      const s = id ? allSpools.find(function (x) { return String(x.id) === String(id); }) : null;
      if (!s) return null;
      const f = s.filament || {};
      return { colour: swatchMain(f).replace(/^#/, ""), name: f.name || "Spool " + s.id };
    }

    function klTray(lv, t) {
      const p = cfg.find(function (x) { return x.id === lv.link; });
      const unit = p && p.units.find(function (u) { return u.kind === t.unit; });
      if (t.unit === "mmu") {
        const mu = lv.units.find(function (u) { return u.kind === "mmu"; });
        const g = mu && mu.trays[t.slot];
        const tray = g ? spoolTray(g.spool_id) || (g.empty ? null : g) : unit && spoolTray(unit.slots[t.slot]);
        return tray ? { label: "Gate " + t.slot, tray: tray } : null;
      }
      const id = unit ? unit.slots[t.slot] || (unit.slots.length === 1 ? lv.active_spool : null) : lv.active_spool;
      const tray = spoolTray(id);
      return tray ? { label: "T" + t.slot, tray: tray } : null;
    }

    function progRow(lv) {
      const j = lv.job;
      const s = PROG_STATES[j.state];
      const live = PROG_LIVE.indexOf(j.state) >= 0;
      const pct = j.state === "FINISH" ? 100 : Math.max(0, Math.min(100, j.pct || 0));
      const meta = [];
      if (j.stage && j.state !== "FAILED") meta.push('<span><b>' + esc(j.stage) + '</b></span>');
      if (j.layers) meta.push(j.layer && s[2] !== "prep" ? '<span>Layer <b>' + j.layer + '</b> / ' + j.layers + '</span>' : '<span>' + j.layers + ' layers</span>');
      if (live && j.left !== null && j.left !== undefined) {
        meta.push(s[2] === "prep" ? '<span>About ' + progDur(j.left) + '</span>' : '<span><b>' + progDur(j.left) + '</b> left</span>');
        if (progress.finish && s[2] !== "prep") meta.push('<span>Done around ' + progClock(Date.now() + j.left * 60000) + '</span>');
      }
      if (j.state === "FINISH" && j.took) meta.push('<span>Took <b>' + progDur(j.took) + '</b></span>');
      if (j.state === "FINISH" && j.end) meta.push('<span>Finished at ' + progClock(j.end * 1000) + '</span>');
      if (j.state === "FAILED" && j.layer) meta.push('<span>Stopped at Layer ' + j.layer + '</span>');
      const t = live && j.tray ? progTray(lv, j.tray) : null;
      if (t) {
        const colour = t.tray.colour ? "#" + t.tray.colour : "#888";
        meta.push('<span class="pj-slot"><span class="d" style="background:' + colour + '"></span>' + t.label + (t.tray.name ? ' \u00b7 ' + esc(t.tray.name) : "") + '</span>');
      }
      return '<div class="pj"><div class="pj-top"><span class="pj-name">' + esc(j.name || "Print") + '</span><span class="pj-chip ' + s[1] + '">' + s[0] + '</span>' +
        progWho(lv) + '<span class="pj-pct">' + (s[2] === "prep" ? "" : pct + "%") + '</span></div>' +
        '<div class="pj-track"><div class="pj-fill ' + s[2] + '" data-k="' + esc(lv.link || lv.name) + '" style="width:' + pct + '%"></div></div>' +
        '<div class="pj-meta">' + meta.join("") + '</div></div>';
    }

    function progWho(lv) {
      return '<span class="pj-who"><span class="live-dot' + (lv.age > 180 ? " stale" : "") + '"></span>' + esc(lv.name) + '</span>';
    }

    function progSync(old, fresh) {
      const o = old.childNodes, n = fresh.childNodes;
      if (o.length !== n.length) { old.innerHTML = fresh.innerHTML; return; }
      for (let i = 0; i < o.length; i++) {
        const x = o[i], y = n[i];
        if (x.nodeType !== y.nodeType || x.nodeName !== y.nodeName) { old.replaceChild(y.cloneNode(true), x); continue; }
        if (x.nodeType === 3) { if (x.data !== y.data) x.data = y.data; continue; }
        Array.prototype.slice.call(x.attributes).forEach(function (a) { if (!y.hasAttribute(a.name)) x.removeAttribute(a.name); });
        Array.prototype.slice.call(y.attributes).forEach(function (a) { if (x.getAttribute(a.name) !== a.value) x.setAttribute(a.name, a.value); });
        progSync(x, y);
      }
    }

    function progPut(html) {
      if (!html || !progSlot.firstChild) { progSlot.innerHTML = html; return; }
      const t = document.createElement("div");
      t.innerHTML = html;
      progSync(progSlot, t);
    }

    function renderProgress() {
      const list = liveOn && progress.on ? liveData.printers.filter(function (lv) {
        return (liveData.links || {})[lv.link] === "live" && lv.job;
      }) : [];
      const hide = progress.idle === "hide";
      const busy = list.filter(function (lv) { return hide ? PROG_LIVE.indexOf(lv.job.state) >= 0 : PROG_STATES[lv.job.state]; });
      const idle = hide ? [] : list.filter(function (lv) { return !PROG_STATES[lv.job.state]; });
      if (!busy.length && !idle.length) { progSlot.innerHTML = ""; return; }
      const idleRow = function (lv, title) {
        return '<div class="pj-idle">' + (title ? "<b>Print Progress</b>" : "") + '<span class="pj-chip idle">Idle</span>' +
          '<span class="sub">No print running</span>' + progWho(lv) + '</div>';
      };
      if (!busy.length) { progPut('<div class="prog">' + idle.map(function (lv, i) { return idleRow(lv, i === 0); }).join("") + '</div>'); return; }
      progPut('<div class="prog"><div class="prog-h"><b>Print Progress</b><span class="sub"><span class="live-dot' + (busy.every(function (lv) { return lv.age > 180; }) ? " stale" : "") + '"></span>Live</span></div>' +
        busy.map(progRow).join("") + idle.map(function (lv) { return idleRow(lv, false); }).join("") + '</div>');
    }

    function renderPrinters() {
      renderProgress();
      const byId = new Map();
      allSpools.forEach(function (s) { byId.set(String(s.id), s); });
      let html = cfg.map(function (p) { return manualPanel(p, byId); }).join("") + linked.map(linkedPanel).join("");
      if (!cfg.length && !linked.length) {
        html += '<div class="empty">No printers yet. Add your printer, then choose the spool in each slot.</div>';
      }
      html += '<button class="sbtn addp" data-act="add-printer">+ Add Printer</button>';
      amsList.innerHTML = html;
      const field = amsList.querySelector(".pname-in") || amsList.querySelector(".picker input");
      if (field) { field.focus(); if (field.classList.contains("pname-in")) field.select(); }
    }

    function pollLive() {
      if (liveOn && showTab && tab === "printer") orca.postMessage({ type: "live" });
    }
    setInterval(pollLive, 5000);
    setInterval(function () {
      if (liveOn && (usage.enabled || canEdit) && tab !== "printer") orca.postMessage({ type: "live" });
    }, 30000);

    let lastView = "";
    function updateTabs() {
      const stOn = stats.on && usage.enabled;
      const view = tab === "stats" ? (stOn ? "stats" : "spools") : showTab && tab === "printer" ? "printer" : "spools";
      const order = ["spools", "printer", "stats"];
      if (lastView && view !== lastView) pgIn(document.getElementById("page-" + view), order.indexOf(view) < order.indexOf(lastView));
      lastView = view;
      const onPrinter = view === "printer";
      tabsEl.hidden = !showTab && !stOn;
      printerBtn.hidden = !showTab;
      statsBtn.hidden = !stOn;
      if (stOn) printerBtn.style.removeProperty("border-right"); else printerBtn.style.setProperty("border-right", "0", "important");
      document.getElementById("page-spools").hidden = view !== "spools";
      document.getElementById("page-printer").hidden = !onPrinter;
      statsEl.hidden = view !== "stats";
      if (showTab) plateSlot.appendChild(plateEl); else listEl.parentNode.insertBefore(plateEl, listEl);
      plateEl.classList.toggle("on-printer", showTab);
      if (onPrinter) pollLive();
      if (view === "stats") { if (!stData) renderStats(); askStats(); }
      tabsEl.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("on", b.dataset.tab === view);
      });
      if (window.glide) requestAnimationFrame(function () { glide(tabsEl); });
    }

    function askStats() {
      orca.postMessage({ type: "stats", period: stPeriod });
    }

    function stW(g) {
      return g >= 1000 ? (g / 1000).toFixed(2) + " kg" : Math.round(g) + " g";
    }

    function stMoney(v) {
      return typeof v === "number" ? esc(stData.sign) + v.toFixed(2) : "&ndash;";
    }

    function stCol(c) {
      return c ? "#" + c : "#888";
    }

    function stDate(t) {
      const d = new Date(t * 1000);
      const now = new Date();
      const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
      if (d.getTime() >= day) return "Today " + hm;
      if (d.getTime() >= day - 86400000) return "Yesterday " + hm;
      return d.getDate() + " " + MONTHS[d.getMonth()] + (d.getFullYear() !== now.getFullYear() ? " " + d.getFullYear() : "") + " " + hm;
    }

    function stWeeks(d) {
      const max = Math.max.apply(null, d.weeks.map(function (w) { return w.parts.reduce(function (a, p) { return a + p.g; }, 0); }).concat([0]));
      if (!max) return '<div class="st-empty">No filament booked in the last 8 weeks.</div>';
      return '<div class="st-weeks">' + d.weeks.map(function (w) {
        const g = w.parts.reduce(function (a, p) { return a + p.g; }, 0);
        const day = new Date(w.start * 1000);
        const parts = w.parts.map(function (p) {
          return '<i style="flex:' + p.g + ';background:' + stCol(p.colour) + '" title="' + esc(p.label) + ': ' + Math.round(p.g) + ' g"></i>';
        }).join("");
        return '<div>' + (g ? '<em>' + stW(g) + '</em>' : '') + '<span class="stk' + (w.now ? ' now' : '') + '" style="height:' + (g ? Math.max(3, g / max * 80) : 0) + '%">' + parts + '</span>' +
          (w.now ? 'This Week' : day.getDate() + ' ' + MONTHS[day.getMonth()]) + '</div>';
      }).join("") + '</div><div class="st-wk-note">Each bar is split by the filament used that week. Hover a section to see it.</div>';
    }

    function stDonut(d) {
      if (!d.mats.length) return '<div class="st-empty">Nothing booked in this period.</div>';
      const top = d.mats.slice(0, 6);
      const rest = d.mats.slice(6).reduce(function (a, m) { return a + m.g; }, 0);
      if (rest) top.push({ name: "Other", g: rest });
      let at = 0;
      const rings = top.map(function (m, i) {
        const len = m.g / d.grams * 100;
        const out = '<circle cx="21" cy="21" r="15.915" stroke="' + ST_COLS[i] + '" stroke-dasharray="' + Math.max(0, len - 0.5).toFixed(2) + ' ' + (100 - Math.max(0, len - 0.5)).toFixed(2) + '" stroke-dashoffset="' + (-at).toFixed(2) + '"/>';
        at += len;
        return out;
      }).join("");
      return '<div class="st-donut"><svg viewBox="0 0 42 42"><g transform="rotate(-90 21 21)" fill="none" stroke-width="6">' + rings + '</g>' +
        '<text x="21" y="21.6" text-anchor="middle" class="c1">' + stW(d.grams) + '</text><text x="21" y="26.4" text-anchor="middle" class="c2">' +
        d.mats.length + (d.mats.length === 1 ? ' Material' : ' Materials') + '</text></svg><div class="st-leg">' + top.map(function (m, i) {
          return '<div><span class="k" style="background:' + ST_COLS[i] + '"></span><span class="n">' + esc(m.name) + '</span><span class="v">' +
            stW(m.g) + ' &middot; ' + Math.round(m.g / d.grams * 100) + '%</span></div>';
        }).join("") + '</div></div>';
    }

    function stTop(d) {
      if (!d.top.length) return '<div class="st-empty">Nothing booked in this period.</div>';
      const max = d.top[0].g;
      return d.top.map(function (t) {
        return '<div class="st-row"><span class="st-sw" style="background:' + stCol(t.colour) + '"></span><span class="l">' + esc(t.label) + '</span>' +
          '<span class="t"><i style="width:' + (t.g / max * 100) + '%"></i></span><span class="v">' + stW(t.g) + '</span></div>';
      }).join("");
    }

    function stSoon(d) {
      if (!d.soon.length) return '<div class="st-empty">No spools are running low.</div>';
      return d.soon.map(function (s) {
        const wk = s.weeks === null ? "" : s.weeks < 1 ? "< 1 week" : "~" + Math.round(s.weeks) + (Math.round(s.weeks) === 1 ? " week" : " weeks");
        return '<div class="st-row"><span class="st-sw" style="background:' + stCol(s.colour) + '"></span><span class="l">' + esc(s.label) + '</span>' +
          '<span class="t"><i class="' + (s.low ? 'red' : '') + '" style="width:' + (s.pct === null ? 100 : s.pct) + '%"></i></span><span class="v">' +
          (s.low || !wk ? stW(s.left) : wk) + '</span></div>';
      }).join("");
    }

    const W_COLS = { model: "var(--orca-accent)", support: "#e0a030", purge: "#d9534f", cal: "#7e8ce0" };

    function stWaste(e) {
      const rows = e.rows.filter(function (r) { return typeof r.ws === "number"; });
      if (e.cal || !rows.length) return null;
      return rows.reduce(function (a, r) { return a + r.grams * (r.ws + r.wp); }, 0);
    }

    function stRatios(d) {
      const w = d.waste;
      if (!w) return '<div class="st-card wi"><h3>Usage Ratios</h3><div class="st-empty">Supports and purge show up for prints Spoolio reads from the printer&#39;s job file.</div></div>';
      const all = w.model + w.support + w.purge + w.cal;
      const pct = function (g) { return Math.round(g / all * 100) + '%'; };
      const parts = [["model", "Model", w.model, w.mcost], ["support", "Supports", w.support, w.scost], ["purge", "Purge and Prime Tower", w.purge, w.pcost], ["cal", "Calibration", w.cal, w.ccost]];
      const top = d.worst.length ? d.worst.map(function (x) {
        return '<div class="st-row"><span class="st-sw" style="background:' + W_COLS[x.kind] + '"></span><span class="l">' + esc(x.name || "Print") + '</span>' +
          '<span class="t"><i style="width:' + (x.g / d.worst[0].g * 100) + '%;background:' + W_COLS[x.kind] + '"></i></span><span class="v">' +
          stW(x.g) + (x.cost ? ' &middot; ' + stMoney(x.cost) : '') + '</span></div>';
      }).join("") : '<div class="st-empty">No waste in this period.</div>';
      return '<div class="st-card wi"><h3>Usage Ratios</h3><div class="wi-in"><div><div class="wi-bar">' + parts.map(function (x) {
        return x[2] > 0 ? '<i style="flex:' + x[2] + ';background:' + W_COLS[x[0]] + '" title="' + x[1] + ': ' + stW(x[2]) + '"></i>' : '';
      }).join("") + '</div><div class="wi-leg">' + parts.filter(function (x) { return x[0] !== "cal" || x[2] > 0; }).map(function (x) {
        return '<div><span class="k" style="background:' + W_COLS[x[0]] + '"></span><span class="n">' + x[1] + '</span><span class="v">' + stW(x[2]) +
          ' &middot; ' + pct(x[2]) + (w.cost || w.mcost ? ' &middot; ' + stMoney(x[3]) : '') + '</span></div>';
      }).join("") + '</div><div class="wi-note">An estimate from the print&#39;s .3mf job file on the printer. ' +
        (w.prints < d.prints ? w.prints + ' of ' + d.prints + ' prints had a job file to read.' : 'Only prints made since the update are counted.') +
        '</div></div><div class="wi-top"><h4>MOST WASTEFUL PRINTS</h4>' + top + '</div></div></div>';
    }

    function stHistory(d) {
      const list = d.history.slice(0, phShown);
      const sum = d.prints + (d.prints === 1 ? ' Print' : ' Prints') + ' &middot; ' + stW(d.grams) + (d.cost !== null ? ' &middot; ' + stMoney(d.cost) : '') +
        (d.waste ? ' &middot; ' + stW(d.waste.support + d.waste.purge) + ' wasted' : '');
      const rows = list.map(function (e) {
        const g = e.rows.reduce(function (a, r) { return a + r.grams; }, 0);
        const costs = e.rows.filter(function (r) { return typeof r.cost === "number"; });
        const cost = costs.length ? costs.reduce(function (a, r) { return a + r.cost; }, 0) : null;
        const wg = stWaste(e);
        const fail = (e.result === "cancelled" ? '<span class="fail">Failed at ' + (e.pct || 0) + '%</span>' : '') +
          (e.cal ? '<span class="cal">Calibration</span>' : '') + (e.nb ? '<span class="nb">Tracking Only</span>' : '');
        return '<tr><td class="d">' + stDate(e.time) + '</td><td><span class="nm">' + esc(e.name || "Print") + '</span>' + fail +
          (e.printer ? '<br><span class="pr">' + esc(e.printer) + '</span>' : '') + '</td><td><div class="mats">' + e.rows.map(function (r) {
            return '<span class="m" title="' + esc(r.label) + '"><i style="background:' + stCol(r.colour) + '"></i>' + esc(r.material || "") + ' ' + r.grams.toFixed(1) + ' g</span>';
          }).join("") + '</div></td><td class="r">' + g.toFixed(1) + ' g</td><td class="r wst">' + (wg === null ? '&ndash;' : wg.toFixed(1) + ' g') +
          '</td><td class="r">' + stMoney(cost) + '</td></tr>';
      }).join("");
      const body = list.length ? '<table><thead><tr><th>Date</th><th>Print</th><th>Material Used</th><th class="r">Weight</th><th class="r">Waste</th><th class="r">Cost</th></tr></thead><tbody>' +
        rows + '</tbody></table>' + (d.history.length > phShown ? '<button class="sbtn ghost more" data-st="more">Show More</button>' : '')
        : '<div class="st-empty">No prints booked in this period.</div>';
      return '<div class="st-card ph' + (phOpen ? ' open' : '') + '"><div class="ph-h" data-st="history"><h3>Print History</h3><span class="sum">' + sum +
        '</span><span class="chev">&#9662;</span></div><div class="ph-b">' + body + '</div></div>';
    }

    function renderStats() {
      const d = stData;
      if (!d) { statsEl.innerHTML = '<div class="st-empty">Loading stats...</div>'; return; }
      const since = d.since ? new Date(d.since * 1000) : null;
      const opts = [["30", "Last 30 Days"], ["90", "Last 90 Days"], ["year", "This Year"], ["all", "All Time"]].map(function (o) {
        return '<option value="' + o[0] + '"' + (stPeriod === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join("");
      statsEl.innerHTML = '<div class="st-bar"><select data-st="period">' + opts + '</select><span class="st-note">' +
        (since ? 'From prints Spoolio booked since ' + since.getDate() + ' ' + MONTHS[since.getMonth()] + ' ' + since.getFullYear()
          : 'Stats fill in as Spoolio books your prints to Spoolman.') + '</span></div>' +
        '<div class="st-tiles"><div class="st-tile"><span>Filament Used</span><b>' + stW(d.grams) + '</b></div>' +
        '<div class="st-tile"><span>Prints Booked</span><b>' + d.prints + '</b></div>' +
        '<div class="st-tile"><span>Filament Cost</span><b>' + stMoney(d.cost) + '</b></div>' +
        '<div class="st-tile' + (d.low ? ' low' : '') + '"><span>Low Stock</span><b>' + d.low + '</b></div>' +
        '<div class="st-tile w"><span>Wasted Filament</span><b>' + (d.waste ? stW(d.waste.support + d.waste.purge) : '&ndash;') + '</b><small>' +
        (d.waste ? (d.waste.cost ? stMoney(d.waste.cost) + ' &middot; ' : '') + Math.round((d.waste.support + d.waste.purge) / (d.waste.model + d.waste.support + d.waste.purge + d.waste.cal) * 100) + '% of all filament' : 'From job files on the printer') +
        '</small></div></div>' +
        '<div class="st-grid"><div class="st-card"><h3>Used per Week</h3>' + stWeeks(d) + '</div>' +
        '<div class="st-card"><h3>Materials Used</h3>' + stDonut(d) + '</div>' +
        '<div class="st-card"><h3>Most Used</h3>' + stTop(d) + '</div>' +
        '<div class="st-card"><h3>Running Low</h3>' + stSoon(d) + '</div>' + stRatios(d) + stHistory(d) + '</div>';
    }

    statsEl.addEventListener("change", function (e) {
      if (e.target.dataset.st !== "period") return;
      stPeriod = e.target.value;
      phShown = 7;
      try { localStorage.setItem("spoolio.period", stPeriod); } catch (err) {}
      askStats();
    });

    statsEl.addEventListener("click", function (e) {
      const el = e.target.closest("[data-st]");
      if (!el || !stData) return;
      if (el.dataset.st === "history") {
        phOpen = !phOpen;
        try { localStorage.setItem("spoolio.history", phOpen ? "1" : "0"); } catch (err) {}
      } else if (el.dataset.st === "more") {
        phShown += 20;
      } else {
        return;
      }
      renderStats();
    });

    tabsEl.addEventListener("click", function (e) {
      const b = e.target.closest("button");
      if (!b) return;
      tab = b.dataset.tab;
      try { localStorage.setItem("spoolio.tab", tab); } catch (err) {}
      updateTabs();
    });

    amsList.addEventListener("click", function (e) {
      const el = e.target.closest("[data-act]");
      if (!el) return;
      const act = el.dataset.act;
      const p = cfg.find(function (x) { return x.id === el.dataset.p; });
      const ui = Number(el.dataset.u);
      const si = Number(el.dataset.s);
      const before = armed;
      armed = "";
      let changed = false;
      if (act === "add-printer") {
        const id = "p" + Date.now().toString(36) + Math.floor(Math.random() * 1296).toString(36);
        cfg.push({ id: id, name: cfg.length ? "Printer " + (cfg.length + 1) : "Printer", units: [] });
        renaming = id;
        changed = true;
      } else if (p && act === "rename") {
        renaming = p.id;
      } else if (p && (act === "grow" || act === "shrink")) {
        const u = p.units[ui];
        if (u && GROW[u.kind]) {
          if (act === "grow" && u.slots.length < GROW[u.kind]) u.slots.push(null);
          if (act === "shrink" && u.slots.length > 1) u.slots.pop();
          picking = null;
          changed = true;
        }
      } else if (p && act === "add-unit") {
        const kind = el.dataset.kind;
        if (KINDS[kind] && !(["ext", "mmu", "tool"].indexOf(kind) >= 0 && p.units.some(function (u) { return u.kind === kind; }))) {
          p.units.push({ kind: kind, slots: new Array(KINDS[kind]).fill(null) });
          p.units.sort(function (x, y) { return RANK[x.kind] - RANK[y.kind]; });
          changed = true;
        }
      } else if (p && act === "remove-unit") {
        const key = "u:" + p.id + ":" + ui;
        if (before === key) { p.units.splice(ui, 1); picking = null; changed = true; } else { armed = key; }
      } else if (p && act === "remove-printer") {
        const key = "p:" + p.id;
        if (before === key) { cfg.splice(cfg.indexOf(p), 1); picking = null; changed = true; } else { armed = key; }
      } else if (p && act === "assign") {
        const same = picking && picking.p === p.id && picking.u === ui && picking.s === si;
        picking = same ? null : { p: p.id, u: ui, s: si };
      } else if (p && act === "clear") {
        p.units[ui].slots[si] = null;
        changed = true;
      } else if (p && act === "heads-bind") {
        orca.postMessage({ type: "heads_bind", id: p.id });
        return;
      } else if (p && act === "heads-unbind") {
        p.orca = "";
        if (heads && heads.printer === p.id) heads = null;
        changed = true;
      } else if (p && act === "link-type") {
        p.draft = el.dataset.t;
        tests[p.id] = "";
      } else if (p && act === "link-test") {
        const form = amsList.querySelector(".linkform");
        const get = function (f) { const i = form && form.querySelector('[data-f="' + f + '"]'); return i ? i.value.trim() : ""; };
        const box = form && form.querySelector(".ktest");
        if (box) box.innerHTML = tests[p.id] = "Testing...";
        orca.postMessage({ type: "printer_test", id: p.id, kind: form.dataset.t, host: get("ip"), code: get("code"), user: get("user") });
        return;
      } else if (p && act === "link") {
        delete p.draft;
        linkOpen = linkOpen === p.id ? "" : p.id;
      } else if (p && act === "link-cancel") {
        delete p.draft;
        linkOpen = "";
      } else if (p && (act === "link-save" || act === "link-remove")) {
        const form = amsList.querySelector(".linkform");
        const val = function (f) { const i = form && form.querySelector('[data-f="' + f + '"]'); return i ? i.value.trim() : ""; };
        const code = act === "link-save" ? val("code") : "";
        const kt = form && NET[form.dataset.t] ? form.dataset.t : "";
        p.ip = act === "link-save" ? val("ip") : "";
        p.serial = act === "link-save" && !kt ? val("serial") : "";
        if (act === "link-remove") p.has_code = false;
        if (act === "link-save") {
          p.type = kt || "bambu";
          p.user = kt === "prusalink" ? val("user") || "maker" : "";
          delete p.draft;
          p.units = p.units.filter(function (u) { return kt ? GROW[u.kind] : !GROW[u.kind]; });
          if (kt && !p.units.length) p.units.push({ kind: "tool", slots: [null] });
        }
        savePrinters();
        if (code || act === "link-remove") { orca.postMessage({ type: "printer_code", id: p.id, code: code }); if (code) p.has_code = true; }
        linkOpen = "";
        liveOn = liveOn || !!(p.ip && (p.has_code || p.type === "klipper"));
        liveKey = "";
        setTimeout(pollLive, 300);
      } else if (act === "pick" && picking) {
        const target = cfg.find(function (x) { return x.id === picking.p; });
        if (target) {
          target.units[picking.u].slots[picking.s] = el.dataset.id;
          changed = true;
        }
        picking = null;
      }
      if (changed) savePrinters();
      renderPrinters();
    });

    amsList.addEventListener("input", function (e) {
      if (!e.target.matches(".picker input")) return;
      amsList.querySelector(".pick-list").innerHTML = pickItems(e.target.value);
    });

    function commitName(input, keep) {
      const p = cfg.find(function (x) { return x.id === input.dataset.p; });
      renaming = "";
      if (p && keep) {
        p.name = input.value.trim().slice(0, 40) || p.name;
        savePrinters();
      }
      renderPrinters();
    }

    amsList.addEventListener("keydown", function (e) {
      if (!e.target.matches(".pname-in")) return;
      if (e.key === "Enter") commitName(e.target, true);
      else if (e.key === "Escape") commitName(e.target, false);
    });

    amsList.addEventListener("focusout", function (e) {
      if (renaming && e.target.matches(".pname-in")) commitName(e.target, true);
    });

    document.addEventListener("click", function (e) {
      if (picking && !e.target.closest(".picker") && !e.target.closest('[data-act="assign"]')) {
        picking = null;
        renderPrinters();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && picking) { picking = null; renderPrinters(); }
    });

    updateTabs();
    renderPrinters();

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function plateRowEl(row) {
      const node = el("div", "plate-row st-" + row.status);
      const dot = el("div", "plate-dot");
      dot.style.background = /^#?[0-9a-f]{6}/i.test(row.colour) ? "#" + row.colour.replace(/^#/, "").slice(0, 6) : "#888";
      const who = el("div", "plate-who");
      who.appendChild(el("div", "plate-name", row.slots + " \u00b7 " + row.name));
      who.appendChild(el("div", "plate-meta", row.meta));
      const bar = el("div", "plate-bar");
      if (row.status !== "unknown") {
        const have = el("i", "have");
        have.style.width = Math.round(row.fill * 100) + "%";
        have.style.background = "#" + String(row.spool_colour).replace(/^#/, "").slice(0, 6);
        bar.appendChild(have);
        const need = el("i", "need");
        need.style.width = Math.round(row.need_pct * 100) + "%";
        bar.appendChild(need);
      }
      const result = el("div", "plate-result");
      result.appendChild(el("div", "plate-headline", row.headline));
      result.appendChild(el("div", "plate-meta", row.detail));
      node.append(dot, who, bar, result);
      return node;
    }

    function setPlate(data) {
      plate = data && Array.isArray(data.rows) && data.rows.length ? data : null;
      plateSlots = new Map();
      plateOrder = new Map();
      plateList.innerHTML = "";
      if (plate) {
        plate.rows.forEach(function (row) {
          plateList.appendChild(plateRowEl(row));
          row.ids.forEach(function (id) {
            plateSlots.set(id, plateSlots.has(id) ? plateSlots.get(id) + ", " + row.slots : row.slots);
            if (!plateOrder.has(id) || row.first < plateOrder.get(id)) plateOrder.set(id, row.first);
          });
        });
        const attention = plate.rows.filter(function (r) {
          return r.status === "barely" || r.status === "mixed" || r.status === "short";
        }).length;
        const fine = plate.rows.some(function (r) { return r.status === "ok"; });
        const pill = document.getElementById("plate-pill");
        pill.className = "plate-pill " + (attention ? "attention" : "fine");
        pill.textContent = attention ? attention + (attention === 1 ? " needs" : " need") + " attention" : "Quantity OK";
        pill.hidden = !attention && !fine;
        plateAttn = attention;
        syncDot();
        document.getElementById("plate-sub").textContent =
          "Sliced " + (plate.time || "") + " \u00b7 striped = needed, solid = left \u00b7 clears on next slice";
      }
      plateEl.hidden = !plate;
      if (!plate) { plateAttn = false; syncDot(); }
      renderPrinters();
      plateSort.disabled = !plate;
      if (!plate && sortKeyEl.value === "plate") sortKeyEl.value = lastSort;
      applyFilters();
    }

    function computeDupes() {
      dupes = new Map();
      allSpools.forEach(function (s) {
        const id = (s.filament || {}).id;
        if (s.archived || id === undefined || typeof s.remaining_weight !== "number") return;
        const d = dupes.get(id) || { count: 0, total: 0 };
        d.count += 1;
        d.total += s.remaining_weight;
        dupes.set(id, d);
      });
    }

    function setFilters(open) {
      fPanel.hidden = !open;
      fToggle.setAttribute("aria-expanded", open ? "true" : "false");
      try { localStorage.setItem("spoolio.filters", open ? "1" : "0"); } catch (err) {}
      filterLabel();
    }

    function filterLabel() {
      const active = (searchEl.value.trim() ? 1 : 0) + (materialEl.value ? 1 : 0) +
        (vendorEl.value ? 1 : 0) + (groupKeyEl.value !== "none" ? 1 : 0);
      fToggle.innerHTML = "Filters" + (active ? " (" + active + ")" : "") +
        (fPanel.hidden ? " &#9662;" : " &#9652;");
    }

    function populateFilterOptions() {
      const materials = new Set();
      const vendors = new Set();
      allSpools.forEach(function (s) {
        const filament = s.filament || {};
        const vendor = filament.vendor || {};
        if (filament.material) materials.add(filament.material);
        if (vendor.name) vendors.add(vendor.name);
      });
      function fillOptions(select, values, currentValue) {
        const placeholder = select.options[0];
        select.innerHTML = "";
        select.appendChild(placeholder);
        [...values].sort().forEach(function (value) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value;
          select.appendChild(option);
        });
        select.value = values.has(currentValue) ? currentValue : "";
      }
      fillOptions(materialEl, materials, materialEl.value);
      fillOptions(vendorEl, vendors, vendorEl.value);
    }

    function progressClass(percent) {
      if (percent === null) return "mid";
      if (percent >= 50) return "high";
      if (percent >= 20) return "mid";
      return "low";
    }

    function formatWeight(grams) {
      if (typeof grams !== "number") return "?";
      if (units === "kg") return (grams / 1000).toFixed(Math.abs(grams) < 100 ? 3 : 2) + " kg";
      return Math.abs(grams) >= 1000
        ? (grams / 1000).toFixed(2) + " kg"
        : Math.round(grams) + " g";
    }

    function lowTint(grams) {
      if (typeof grams !== "number" || lowStockGrams <= 0 || grams >= lowStockGrams) return "";
      const t = Math.max(0, Math.min(1, (lowStockGrams - grams) / (lowStockGrams * 0.8)));
      const amber = [224, 160, 48], red = [217, 83, 79];
      return "rgb(" + amber.map(function (v, i) { return Math.round(v + (red[i] - v) * t); }).join(",") + ")";
    }

    function tagUids(s) {
      const tags = Array.isArray(s.rfid_tags) ? s.rfid_tags : s.tags;
      if (!Array.isArray(tags)) return [];
      return tags.map(function (t) {
        return typeof t === "string" ? t : (t && (t.uid || t.id)) || null;
      }).filter(Boolean);
    }

    function sortValue(s, key) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      switch (key) {
        case "name": return (filament.name || "").toLowerCase() || null;
        case "material": return (filament.material || "").toLowerCase() || null;
        case "vendor": return (vendor.name || "").toLowerCase() || null;
        case "remaining": return (typeof s.remaining_weight === "number") ? s.remaining_weight : null;
        case "used": return (typeof s.used_weight === "number") ? s.used_weight : null;
        case "first_used": return s.first_used ? Date.parse(s.first_used) : null;
        case "last_used": return s.last_used ? Date.parse(s.last_used) : null;
        case "plate": return plateOrder.has(s.id) ? plateOrder.get(s.id) : null;
        default: return null;
      }
    }

    // A spool missing the field (e.g. never used) always sorts last, even descending.
    function compareSpools(a, b, key, dir) {
      const av = sortValue(a, key);
      const bv = sortValue(b, key);
      const aNull = av === null || av === undefined;
      const bNull = bv === null || bv === undefined;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      if (av < bv) return dir === "asc" ? -1 : 1;
      if (av > bv) return dir === "asc" ? 1 : -1;
      return 0;
    }

    function originalWeight(s) {
      const filament = s.filament || {};
      return (s.initial_weight !== undefined && s.initial_weight !== null)
        ? s.initial_weight : filament.weight;
    }

    window.__spoolioCard = function (s) { return spoolRowHtml(s); };
    function spoolRowHtml(s) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      const color = swatch(filament);
      const main = swatchMain(filament);
      const ring = swatchCols(filament).length > 1 ? ";border-color:" + main : "";
      const name = filament.name || ("Spool #" + s.id);
      const material = filament.material || "";
      const vendorName = vendor.name || "";
      const subtitleParts = [];
      if (material && show.mat) subtitleParts.push(material);
      if (filament.color_hex && show.hex) subtitleParts.push("#" + filament.color_hex.replace(/^#/, "").toUpperCase());
      if (typeof filament.diameter === "number" && show.dia) subtitleParts.push(filament.diameter + "mm");
      if (s.location && show.loc) subtitleParts.push("📍 " + s.location);
      const subtitle = subtitleParts.join(" \\u00b7 ");
      const tags = tagUids(s);
      const original = originalWeight(s);
      const remaining = s.remaining_weight;
      let percent = null;
      if (typeof original === "number" && original > 0 && typeof remaining === "number") {
        percent = Math.max(0, Math.min(100, Math.round((remaining / original) * 100)));
      }
      const remainingLabel = formatWeight(remaining);
      const wHtml = percent === null || wMode === "grams" ? remainingLabel
        : wMode === "percent" ? percent + '%'
        : '<span class="spool-pct">' + percent + '% \u00b7</span>' + remainingLabel;
      const dupe = dupes.get(filament.id);
      const dupeHtml = dupe && dupe.count > 1
        ? '<span class="spool-dup">\u00d7' + dupe.count + ' \u00b7 ' + formatWeight(dupe.total) + ' together</span>'
        : '';
      const plateLabel = plateSlots.get(s.id);
      const chipHtml = plateLabel ? '<span class="plate-chip">On This Plate \u00b7 ' + plateLabel + '</span>' : '';
      const percentTitle = percent === null ? "Remaining unknown" : percent + "% remaining";
      const tagHtml = vendorName
        ? '<div class="spool-tag">' +
            '<svg viewBox="0 0 12 12" fill="currentColor">' +
              '<rect x="0" y="0" width="5" height="5" rx="1"></rect>' +
              '<rect x="7" y="0" width="5" height="5" rx="1"></rect>' +
              '<rect x="0" y="7" width="5" height="5" rx="1"></rect>' +
              '<rect x="7" y="7" width="5" height="5" rx="1"></rect>' +
            '</svg>' +
            '<span>' + vendorName + '</span>' +
          '</div>'
        : '';
      const tip = [name, vendorName, remainingLabel + (typeof original === "number" ? " of " + formatWeight(original) : "") + " left"].filter(Boolean).join(" \u00b7 ");
      const tint = lowTint(remaining);
      const searchQuery = [vendorName, s.lot_nr || name].filter(Boolean).join(" ");
      const cartHtml = (cart && typeof remaining === "number" && remaining < lowStockGrams)
        ? '<button class="spool-cart" data-q="' + encodeURIComponent(searchQuery) + '" ' +
            'title="Low stock - search online for ' + searchQuery.replace(/"/g, "") + '">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
              '<circle cx="9" cy="20" r="1.4"></circle><circle cx="18" cy="20" r="1.4"></circle>' +
              '<path d="M2 3h3l2.4 12.2a1.5 1.5 0 0 0 1.5 1.2h8.6a1.5 1.5 0 0 0 1.5-1.1L21 8H6"></path>' +
            '</svg></button>'
        : '';
      const editHtml = canEdit
        ? '<button class="spool-edit" data-id="' + s.id + '" title="Edit Spool" aria-label="Edit Spool">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
              '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z"></path><path d="M13.5 6.5l4 4"></path>' +
            '</svg></button>'
        : '';
      const rfidHtml = tags.length
        ? '<div class="spool-rfid" title="RFID: ' + tags.join(", ") + '">' +
            '<span>RFID</span>' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">' +
              '<circle cx="5" cy="19" r="1.8" fill="currentColor" stroke="none"></circle>' +
              '<path d="M5,14 A5,5 0 0 1 10,19"></path>' +
              '<path d="M5,10.5 A8.5,8.5 0 0 1 13.5,19"></path>' +
              '<path d="M5,7 A12,12 0 0 1 17,19"></path>' +
            '</svg>' +
          '</div>'
        : '';
      const isWarn = !s.archived && (typeof remaining !== "number" || remaining > 0) &&
        (warnSids.has(s.id) || (filament.id != null && warnFids.has(filament.id)));
      const warnTitle = "No OrcaSlicer preset created (no matching OrcaSlicer material" + (material ? ": " + material : "") + ")";
      const warnBadge = isWarn
        ? '<div class="spool-warn-badge" title="' + esc(warnTitle) + '">' +
            '<svg viewBox="0 0 24 24">' +
              '<path d="M12 2L1 21h22L12 2z" fill="#f5c518" stroke="#111" stroke-width="1.5" stroke-linejoin="round"></path>' +
              '<rect x="11" y="9" width="2" height="6" rx="1" fill="#111"></rect>' +
              '<circle cx="12" cy="17.8" r="1.2" fill="#111"></circle>' +
            '</svg></div>'
        : '';
      return (
        '<div class="spool-card' + (isWarn ? ' spool-warn' : '') + '" title="' + tip.replace(/"/g, "&quot;") + '">' +
          warnBadge +
          tagHtml +
          '<div class="spool-body">' +
            '<div class="spool-swatch-col">' +
              '<div class="spool-swatch" style="background:' + color + ring + '"></div>' +
              rfidHtml +
            '</div>' +
            '<div class="spool-main">' +
              '<div class="spool-top">' +
                '<div class="spool-title spool-tline"><span class="spool-name">' + name + '</span>' + dupeHtml + editHtml + '</div>' +
                '<div class="spool-weight' + (tint ? ' low" style="color:' + tint : '') + '">' + wHtml + cartHtml + '</div>' +
              '</div>' +
              '<div class="spool-bar-track" title="' + percentTitle + '">' +
                '<div class="spool-bar-fill" style="width:' + (percent === null ? 0 : percent) + '%;background:' + main + '"></div>' +
              '</div>' +
              '<div class="spool-subtitle"><span class="sub-text">' + subtitle + '</span>' + chipHtml + '</div>' +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }

    function groupKeyFor(s, mode) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      if (mode === "material") {
        const label = filament.material || "Unknown material";
        return { key: label, label: label };
      }
      if (mode === "vendor") {
        const label = vendor.name || "Unknown manufacturer";
        return { key: label, label: label };
      }
      const label = s.location || "No location";
      return { key: label, label: label };
    }

    function groupRowHtml(group, collapsed) {
      let totalOriginal = 0, totalRemaining = 0, hasOriginal = false, hasRemaining = false;
      group.items.forEach(function (s) {
        const original = originalWeight(s);
        if (typeof original === "number") { totalOriginal += original; hasOriginal = true; }
        if (typeof s.remaining_weight === "number") { totalRemaining += s.remaining_weight; hasRemaining = true; }
      });
      const percent = (hasOriginal && hasRemaining && totalOriginal > 0)
        ? Math.max(0, Math.min(100, Math.round((totalRemaining / totalOriginal) * 100))) : null;
      const remainingLabel = hasRemaining ? formatWeight(totalRemaining) : "?";
      const percentLabel = percent === null ? "?" : percent + "%";
      const count = group.items.length + " spool" + (group.items.length === 1 ? "" : "s");
      const wLabel = wMode === "grams" ? remainingLabel
        : wMode === "percent" ? (percent === null ? remainingLabel : percentLabel)
        : remainingLabel + ' \\u00b7 ' + percentLabel;
      const subLabel = [group.sub, count].filter(Boolean).join(" \\u00b7 ");
      return (
        '<div class="group' + (collapsed ? " collapsed" : "") + '" data-key="' + group.key + '">' +
          '<div class="group-card">' +
            '<div class="arrow">' + (collapsed ? "\\u25b8" : "\\u25be") + '</div>' +
            '<div class="group-info"><div class="group-name">' + group.label + '</div><div class="group-meta">' + subLabel + '</div></div>' +
            '<div class="group-bar">' +
              '<div class="group-meta" style="text-align:right;margin-bottom:2px;">' + wLabel + '</div>' +
              '<div class="group-bar-track"><div class="group-bar-fill ' + progressClass(percent) +
                '" style="width:' + (percent === null ? 0 : percent) + '%"></div></div>' +
            '</div>' +
          '</div>' +
          '<div class="group-items">' + group.items.map(spoolRowHtml).join("") + '</div>' +
        '</div>'
      );
    }

    const collapsedGroups = new Set();

    function renderGrouped(spools, mode) {
      const groups = new Map();
      spools.forEach(function (s) {
        const info = groupKeyFor(s, mode);
        if (!groups.has(info.key)) groups.set(info.key, { key: info.key, label: info.label, sub: info.sub, items: [] });
        groups.get(info.key).items.push(s);
      });
      const ordered = [...groups.values()].sort(function (a, b) {
        if (mode === "location" && b.items.length !== a.items.length) {
          return b.items.length - a.items.length;
        }
        return a.label.toLowerCase().localeCompare(b.label.toLowerCase());
      });
      listEl.innerHTML = ordered.map(function (group) {
        return groupRowHtml(group, collapsedGroups.has(group.key));
      }).join("");
      listEl.querySelectorAll(".group-card").forEach(function (header) {
        header.addEventListener("click", function () {
          const groupEl = header.closest(".group");
          const key = groupEl.dataset.key;
          if (collapsedGroups.has(key)) collapsedGroups.delete(key); else collapsedGroups.add(key);
          groupEl.classList.toggle("collapsed");
          header.querySelector(".arrow").innerHTML = collapsedGroups.has(key) ? "&#9656;" : "&#9662;";
        });
      });
    }

    function applyFilters() {
      filterLabel();
      const query = searchEl.value.trim().toLowerCase();
      const material = materialEl.value;
      const vendor = vendorEl.value;
      const filtered = allSpools.filter(function (s) {
        const filament = s.filament || {};
        const vendorObj = filament.vendor || {};
        if (material && filament.material !== material) return false;
        if (vendor && vendorObj.name !== vendor) return false;
        if (query) {
          const haystack = [filament.name, filament.material, vendorObj.name]
            .filter(Boolean).join(" ").toLowerCase();
          if (!haystack.includes(query)) return false;
        }
        return true;
      });
      statusEl.className = "";
      statusEl.textContent = filtered.length + " of " + allSpools.length + " spool" +
        (allSpools.length === 1 ? "" : "s");
      filtered.sort(function (a, b) {
        return compareSpools(a, b, sortKeyEl.value, sortDir) ||
          (sortKeyEl.value === "plate" ? compareSpools(a, b, "remaining", "asc") : 0);
      });
      if (filtered.length === 0) {
        listEl.innerHTML = '<div class="empty">No spools match this filter.</div>';
        return;
      }
      const groupMode = groupKeyEl.value;
      if (groupMode === "none") {
        listEl.innerHTML = filtered.map(spoolRowHtml).join("");
      } else {
        renderGrouped(filtered, groupMode);
      }
    }

    let lastSpools = "";
    function render(payload) {
      const key = payload.ok ? JSON.stringify(payload) : "";
      if (key && key === lastSpools) return;
      lastSpools = key;
      if (!payload.ok) {
        statusEl.textContent = payload.error || "Unknown error";
        statusEl.className = "error";
        listEl.innerHTML = "";
        return;
      }
      allSpools = payload.spools || [];
      computeDupes();
      if (typeof payload.low_stock_grams === "number") lowStockGrams = payload.low_stock_grams;
      if (typeof payload.weight_display === "string" && !window.__spoolioAp) wMode = payload.weight_display;
      if (typeof payload.show_cart === "boolean") cart = payload.show_cart;
      canEdit = payload.can_edit === true;
      document.getElementById("add-spool").hidden = !canEdit;
      if (payload.spool_columns && !window.__spoolioAp) listEl.classList.toggle("cols-2", payload.spool_columns === 2);
      if (Array.isArray(payload.printers)) cfg = payload.printers;
      if (typeof payload.show_printer_tab === "boolean") showTab = payload.show_printer_tab;
      liveOn = payload.live_ok === true;
      if (payload.usage) setUsage(payload.usage);
      if (payload.progress) progress = payload.progress;
      if (payload.stats) stats = payload.stats;
      if (liveOn) pollLive();
      populateFilterOptions();
      buildPrinters();
      updateTabs();
      renderPrinters();
      applyFilters();
    }

    orca.onMessage(function (data) {
      if (data && data.type === "spools") {
        render(data);
      } else if (data && data.type === "plate") {
        setPlate(data);
      } else if (data && data.type === "plate_clear") {
        setPlate(null);
      } else if (data && data.type === "usage") {
        setUsage(data.usage, true);
        if (!statsEl.hidden) askStats();
      } else if (data && data.type === "stats") {
        if (data.stats.period === stPeriod) { stData = data.stats; renderStats(); }
      } else if (data && data.type === "printer_test") {
        tests[data.id] = '<span class="' + (data.ok ? "ok" : "bad") + '">' + (data.ok ? "\u2713" : "\u2715") + '</span> ' + esc(data.text || "") +
          (data.books ? '<span class="kchip">' + (data.text.indexOf("OctoPrint") === 0 ? "Spoolman Plugin Found" : "Moonraker Is Linked to Spoolman") + '</span>' : "");
        const box = amsList.querySelector('.linkform .ktest');
        if (box && linkOpen === data.id) box.innerHTML = tests[data.id];
      } else if (data && data.type === "heads") {
        heads = data.heads;
        if (Array.isArray(data.printers) && !picking && !renaming) cfg = data.printers;
        renderPrinters();
      } else if (data && data.type === "live") {
        const key = JSON.stringify(data.live, function (k, v) {
          return k === "age" ? v > 180 : k === "job" && v ? [v.state, v.tray] : v;
        });
        liveData = data.live;
        if (data.usage) setUsage(data.usage);
        if (key !== liveKey && !picking && !renaming && !linkOpen) { liveKey = key; renderPrinters(); }
        renderProgress();
      } else if (data && data.type === "preset_warn") {
        setPresetWarn(data.msg || "", data.key || "", data.fids, data.sids);
      }
    });

    let presetWarnKey = "";
    let presetWarnDismissed = "";
    let warnFids = new Set();
    let warnSids = new Set();
    function setPresetWarn(msg, key, fids, sids) {
      const box = document.getElementById("preset-warn");
      const txt = document.getElementById("pw-txt");
      presetWarnKey = key || "";
      const nextFids = new Set(Array.isArray(fids) ? fids : []);
      const nextSids = new Set(Array.isArray(sids) ? sids : []);
      const changed = nextFids.size !== warnFids.size || nextSids.size !== warnSids.size ||
        [...nextFids].some(function (id) { return !warnFids.has(id); }) ||
        [...nextSids].some(function (id) { return !warnSids.has(id); });
      warnFids = nextFids;
      warnSids = nextSids;
      if (changed && allSpools.length) applyFilters();
      if (!msg || (presetWarnKey && presetWarnKey === presetWarnDismissed)) {
        box.hidden = true;
        txt.textContent = "";
        return;
      }
      txt.textContent = msg;
      box.hidden = false;
    }

    listEl.addEventListener("click", function (e) {
      const btn = e.target.closest(".spool-cart");
      if (btn) {
        orca.postMessage({ type: "order", query: decodeURIComponent(btn.dataset.q) });
      }
      const ed = e.target.closest(".spool-edit");
      const sp = ed && allSpools.find(function (s) { return String(s.id) === ed.dataset.id; });
      if (sp && window.__spoolioEdit) window.__spoolioEdit(sp);
    });
    document.getElementById("add-spool").addEventListener("click", function () {
      if (window.__spoolioEdit) window.__spoolioEdit(null);
    });

    searchEl.addEventListener("input", applyFilters);
    materialEl.addEventListener("change", applyFilters);
    vendorEl.addEventListener("change", applyFilters);
    groupKeyEl.addEventListener("change", applyFilters);
    sortKeyEl.addEventListener("change", function () {
      if (sortKeyEl.value !== "plate") lastSort = sortKeyEl.value;
      applyFilters();
    });
    sortDirBtn.addEventListener("click", function () {
      sortDir = sortDir === "asc" ? "desc" : "asc";
      sortDirBtn.innerHTML = sortDir === "asc" ? "&#9650;" : "&#9660;";
      applyFilters();
    });

    document.getElementById("settings-btn").addEventListener("click", function () {
      orca.postMessage({ type: "settings" });
    });
    document.getElementById("pw-open").addEventListener("click", function () {
      orca.postMessage({ type: "open_spoolman" });
    });
    document.getElementById("pw-dim").addEventListener("click", function () {
      presetWarnDismissed = presetWarnKey;
      setPresetWarn("", presetWarnKey);
    });
    fToggle.addEventListener("click", function () { setFilters(fPanel.hidden); });
    try { setFilters(localStorage.getItem("spoolio.filters") === "1"); } catch (err) { setFilters(false); }
    document.getElementById("plate-recheck").addEventListener("click", function () {
      orca.postMessage({ type: "plate_recheck" });
    });
    document.getElementById("plate-dismiss").addEventListener("click", function () {
      setPlate(null);
      orca.postMessage({ type: "plate_dismiss" });
    });

    // Ask for data on load rather than relying on a push landing in time.
    orca.postMessage({ type: "ready" });

    let missed = false;
    function away() {
      return document.hidden || !!document.querySelector("#view-main.view-hidden");
    }
    setInterval(function () {
      if (away()) { missed = true; return; }
      orca.postMessage({ type: "refresh" });
    }, __REFRESH_MS__);
    setInterval(function () {
      if (missed && !away()) { missed = false; orca.postMessage({ type: "refresh" }); }
    }, 5000);
  </script>
</body>
</html>
"""

SETTINGS_PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="__LOGODATA__">
<style>
  * { font-family: __FONT__; }
  html { overscroll-behavior-x: none; }
  html, body { height: 100%; }
  body {
    margin: 0; padding: 24px 28px; box-sizing: border-box; font-size: 13px;
    display: flex; flex-direction: column; line-height: 1.4;
    background: var(--orca-bg); color: var(--orca-fg);
  }
  .header {
    display: flex; align-items: center; gap: 12px;
    padding-bottom: 14px; margin-bottom: 18px;
    border-bottom: 1px solid var(--orca-border);
  }
  .header img.logo { width: 52px; height: 52px; flex-shrink: 0; }
  h2 { margin: 0; font-size: 20px; font-weight: 700; color: var(--orca-fg); }
  .step { display: flex; gap: 12px; margin-bottom: 16px; }
  .step-num {
    flex-shrink: 0; width: 22px; height: 22px; margin-top: 1px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--orca-accent); color: var(--orca-accent-fg); font-size: 12px; font-weight: 700;
  }
  .step h4 { margin: 0 0 3px 0; font-size: 14px; font-weight: 700; color: var(--orca-accent); }
  .step p { margin: 0; color: var(--orca-muted); text-align: justify; }
  .step strong { color: var(--orca-fg); }
  .divider { border-top: 1px solid var(--orca-border); margin: 4px 0 16px 0; }
  h3 { margin: 0 0 8px 0; font-size: 14px; font-weight: 700; color: var(--orca-accent); }
  h3 .emoji { margin-right: 6px; font-size: 22px; vertical-align: -3px; display: inline-block; }
  label { display: block; margin-bottom: 8px; }
  input {
    width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 13px;
    background: var(--orca-bg); color: var(--orca-fg);
    border: 1px solid var(--orca-border); border-radius: 6px;
  }
  input:focus { outline: none; border-color: var(--orca-accent); }
  .status { margin-top: 8px; font-size: 12px; }
  .divider.spaced { margin-top: 18px; }
  .hint { margin-top: 6px; font-size: 12px; color: var(--orca-muted); }
  .check-row { display: flex; align-items: center; gap: 8px; margin: 0 0 12px 0; cursor: pointer; }
  .check-row input {
    -webkit-appearance: none; appearance: none; flex: none; width: 17px; height: 17px; margin: 0;
    border: 1.5px solid var(--orca-muted); border-radius: 4px; background: transparent; cursor: pointer;
  }
  .check-row input:hover { border-color: var(--orca-accent); }
  .check-row input:checked {
    border-color: #2fbfa8;
    background: transparent url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M3.5 8.5l3 3 6-7' fill='none' stroke='%232fbfa8' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / 13px no-repeat;
  }
  .margin-row { display: flex; align-items: center; gap: 12px; }
  .margin-row label { margin: 0; }
  .margin-row input { width: 90px; }
  .seg { display: inline-flex; margin: 6px 0 14px; border: 1px solid var(--orca-border); border-radius: 6px; overflow: hidden; }
  .seg label { margin: 0; padding: 6px 18px; cursor: pointer; border-right: 1px solid var(--orca-border); }
  .seg label:last-child { border-right: 0; }
  .seg label.on { background: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 700; }
  .seg input { position: absolute; opacity: 0; pointer-events: none; }
  .ws-card {
    display: flex; gap: 11px; align-items: flex-start; padding: 13px 14px 11px;
    border: 1px solid var(--orca-border); border-radius: 14px;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%);
  }
  .ws-swatch {
    flex: none; width: 42px; height: 42px; border-radius: 50%; background: #1a1a1a;
    border: 2px solid rgba(255, 255, 255, 0.15); box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.2);
  }
  .ws-main { flex: 1; min-width: 0; }
  .ws-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .ws-name { font-weight: 700; }
  .ws-pct { color: var(--orca-muted); font-size: 12px; }
  .ws-bar { height: 5px; margin: 6px 0 5px; border-radius: 3px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .ws-bar i { display: block; height: 100%; width: 64%; background: #1a1a1a; }
  .ws-sub { font-size: 12px; color: var(--orca-muted); font-style: italic; }
  .lay-prev { display: grid; gap: 8px; }
  .lay-prev.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lay-prev .ws-card { padding: 9px 11px 8px; gap: 9px; }
  .lay-prev .ws-swatch { width: 26px; height: 26px; }
  .lay-prev .ws-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lay-prev .ws-top span:last-child { font-size: 12px; flex: none; }
  .lay-prev .ws-bar { margin: 5px 0 0; }
  #prog-opts { padding-left: 25px; }
  .pp-card { padding: 12px 14px; border: 1px solid var(--orca-border); border-radius: 14px; background: var(--orca-border); background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); }
  .pp-top { display: flex; align-items: center; gap: 10px; }
  .pp-name { font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pp-chip { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 99px; background: color-mix(in srgb, var(--orca-accent) 22%, transparent); color: #3cc9b9; }
  .pp-pct { margin-left: auto; font-size: 18px; font-weight: 800; }
  .pp-bar { height: 8px; margin: 8px 0 7px; border-radius: 4px; background: rgba(127, 127, 127, 0.25); overflow: hidden; }
  .pp-bar i { display: block; height: 100%; width: 37%; background: var(--orca-accent); }
  .pp-meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--orca-muted); }
  .pp-meta b { color: var(--orca-fg); }
  #pp-slot { display: inline-flex; align-items: center; gap: 6px; }
  #pp-slot .d { width: 10px; height: 10px; border-radius: 50%; background: #ae835b; border: 2px solid rgba(255, 255, 255, 0.18); }
  .pp-off { display: none; color: var(--orca-muted); font-style: italic; }
  .prog-off #prog-opts, .prog-off #pp-card { display: none; }
  .cur-prev { display: flex; flex-direction: column; gap: 10px; max-width: 340px; }
  .cur-tile, .cur-row { border: 1px solid var(--orca-border); border-radius: 10px; padding: 10px 14px; background: var(--orca-border); background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); }
  .cur-tile span { font-size: 12px; color: var(--orca-muted); }
  .cur-tile b { display: block; font-size: 20px; margin-top: 3px; }
  .cur-tile b .cur { font-size: inherit; color: inherit; }
  .cur-row { display: flex; gap: 14px; font-size: 13px; }
  .cur-row .nm { flex: 1; font-weight: 600; }
  #cur-seg label { white-space: nowrap; padding: 6px 12px; }
  .st-off { display: none; color: var(--orca-muted); font-style: italic; }
  .stats-off #stats-opts, .stats-off #cur-prev { display: none; }
  .stats-off #stats-off { display: block; }
  .prog-off .pp-off { display: block; }
  input[type="checkbox"] { width: 16px; height: 16px; padding: 0; margin: 0; accent-color: var(--orca-accent); }
  .status.ok { color: #3fb950; }
  .status.error { color: #d9534f; }
  .footer {
    margin-top: auto; padding-top: 14px; display: flex; align-items: center;
    gap: 10px; justify-content: flex-end; border-top: 1px solid var(--orca-border);
  }
  .about { margin-top: 16px; font-size: 11px; color: var(--orca-muted); line-height: 1.7; }
  .about .logpath { word-break: break-all; user-select: text; }
  .version-row { margin-right: auto; min-width: 0; font-size: 11px; line-height: 1.7; color: var(--orca-muted); }
  .header h2 { flex: 1; min-width: 0; }
  button {
    padding: 9px 18px; cursor: pointer; border-radius: 6px; font-size: 13px;
    font-weight: 700; border: 1px solid transparent;
    background: transparent; color: var(--orca-fg);
  }
  button.primary, button.test { background: var(--orca-accent); color: var(--orca-accent-fg); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .url-row { display: flex; gap: 8px; }
  .url-row input { flex: 1; min-width: 0; }
  button.test { white-space: nowrap; }
  .status.pending { color: var(--orca-muted); }
  .src-hidden { display: none; }
  .hint.warn { color: #e0a030; }
  #src-bambu .field-label { margin-top: 4px; }
  #bambu-resend { color: var(--orca-accent); }
  select { padding: 10px 12px; font-size: 13px; font-family: inherit; background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; }
  select:focus { outline: none; border-color: var(--orca-accent); }
  select option { background: var(--orca-bg); color: var(--orca-fg); }
  button.primary:hover, button.test:hover { filter: brightness(1.08); }
  button.icon-btn { padding: 6px; line-height: 0; }
  button.link { padding: 0 0 0 6px; border: none; font-size: 11px; font-weight: 400; text-decoration: underline; }
  /* The host's own button:hover styling would otherwise repaint these; keep them static. */
  button.icon-btn, button.icon-btn:hover, button.icon-btn:focus, button.icon-btn:active {
    color: var(--orca-accent-fg) !important; background: var(--orca-accent) !important;
    border-color: var(--orca-accent) !important; box-shadow: none !important;
  }
  button.link, button.link:hover, button.link:focus, button.link:active {
    color: var(--orca-accent) !important; background: none !important; box-shadow: none !important;
  }
</style>
</head>
<body>
  <!--header--><div class="header">
    __LOGO__<h2>__PLUGIN_NAME__</h2>
    <button class="icon-btn" id="feedback" title="Send feedback or report a bug" aria-label="Send feedback">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
    </button>
  </div><!--/header-->

  <!--steps--><div class="step">
    <div class="step-num">1</div>
    <div>
      <h4>Pick Your Source</h4>
      <p>Use your self-hosted Spoolman server, or Bambu Cloud.</p>
    </div>
  </div>
  <div class="step">
    <div class="step-num">2</div>
    <div>
      <h4>Browse Your Spools</h4>
      <p>Each card shows the filament color, remaining weight, and an RFID badge where a
      tag is linked. Filter, sort, or group the list by material, manufacturer, or location.</p>
    </div>
  </div><!--/steps-->
  <div class="divider"></div>

  <!--reorder--><h3><span class="emoji">🛒</span>Configure Filament Reorder</h3>
  <label class="check-row"><input id="show-cart" type="checkbox" __CART_CHECKED__>Show a Cart Button for Reordering Low-Stock Spools</label>
  <label for="low-stock" class="field-label">Low Stock Warning (Grams):</label>
  <input id="low-stock" type="number" min="0" step="10" value="__LOW_STOCK__">
  <div class="hint">Below this weight, a spool's remaining weight turns amber, then red as it runs out.</div><!--/reorder-->

  <div class="divider spaced"></div>

  <!--check--><h3><span class="emoji">\u26a0\ufe0f</span>Configure Filament Check</h3>
  <label class="check-row"><input id="plate-check" type="checkbox" __PLATE_CHECKED__>Show Filament Check Notifications</label>
  <div class="margin-row" title="Warns when a plate needs more than a spool has left, or would leave less than this margin spare.">
    <label for="plate-margin">Safety Margin (%):</label>
    <input id="plate-margin" type="number" min="0" max="50" step="5" value="__PLATE_MARGIN__">
  </div>
<!--/check-->
  <!--check-hint--><div class="hint">OrcaSlicer also has to run the check: switch on Spoolio Slice Sync in your process settings, under <span style="white-space: nowrap">Others &gt; Slicing Pipeline Plugin</span>.</div><!--/check-hint-->

  <div class="divider spaced"></div>

  <!--weights--><h3><span class="emoji">\u2696\ufe0f</span>Configure Spool Weights</h3>
  <div class="field-label">Show the remaining weight on spool cards as:</div>
  <div class="seg" id="weight-seg">
    <label><input type="radio" name="weight-display" value="grams">Grams</label>
    <label><input type="radio" name="weight-display" value="percent">Percentage</label>
    <label><input type="radio" name="weight-display" value="both">Both</label>
  </div><!--/weights-->
  <!--weights-preview--><div class="ws-card">
    <div class="ws-swatch"></div>
    <div class="ws-main">
      <div class="ws-top"><span class="ws-name">PLA Matte - Black</span><span id="weight-sample"></span></div>
      <div class="ws-bar"><i></i></div>
      <div class="ws-sub">PLA \u00b7 #1A1A1A \u00b7 1.75mm</div>
    </div>
  </div><!--/weights-preview-->

  <div class="divider spaced"></div>

  <!--layout--><h3><span class="emoji">\U0001f4cb</span>Configure Spool List</h3>
  <div class="field-label">Show spools on the Spools tab as:</div>
  <div class="seg" id="cols-seg">
    <label><input type="radio" name="spool-cols" value="1">Single Column</label>
    <label><input type="radio" name="spool-cols" value="2">Two Columns</label>
  </div>
  <div class="hint">In a narrow window, two columns fall back to a single column so names stay readable.</div><!--/layout-->
  <!--layout-preview--><div class="lay-prev" id="lay-prev">
    <div class="ws-card"><div class="ws-swatch" style="background:#1a1a1a"></div><div class="ws-main"><div class="ws-top"><span class="ws-name">PLA Matte - Black</span><span>640 g</span></div><div class="ws-bar"><i style="width:64%;background:#1a1a1a"></i></div></div></div>
    <div class="ws-card"><div class="ws-swatch" style="background:#C12E1F"></div><div class="ws-main"><div class="ws-top"><span class="ws-name">PLA Basic - Red</span><span>530 g</span></div><div class="ws-bar"><i style="width:53%;background:#C12E1F"></i></div></div></div>
    <div class="ws-card"><div class="ws-swatch" style="background:#2850A8"></div><div class="ws-main"><div class="ws-top"><span class="ws-name">PETG HF - Cobalt Blue</span><span>770 g</span></div><div class="ws-bar"><i style="width:77%;background:#2850A8"></i></div></div></div>
    <div class="ws-card"><div class="ws-swatch" style="background:#3F8E43"></div><div class="ws-main"><div class="ws-top"><span class="ws-name">PLA Basic - Bambu Green</span><span>700 g</span></div><div class="ws-bar"><i style="width:70%;background:#3F8E43"></i></div></div></div>
  </div><!--/layout-preview-->

  <div class="divider spaced"></div>

  <!--printer--><h3><span class="emoji">🧵</span>Configure Printer</h3>
  <label class="check-row"><input id="show-progress" type="checkbox">Show Print Progress on the Printer Tab</label>
  <div id="prog-opts">
    <label class="check-row"><input id="prog-finish" type="checkbox">Show the Estimated Finish Time</label>
    <label class="check-row"><input id="prog-slot" type="checkbox">Highlight the AMS Slot in Use</label>
    <div class="field-label">When no print is running:</div>
    <div class="seg" id="idle-seg">
      <label><input type="radio" name="progress-idle" value="line">Keep Print Progress Visible</label>
      <label><input type="radio" name="progress-idle" value="hide">Hide Print Progress</label>
    </div>
  </div>
  <div class="hint">Print progress requires a Live Link connection to the printer(s).</div><!--/printer-->
  <!--printer-preview--><div class="pp-card" id="pp-card">
    <div class="pp-top"><span class="pp-name">Desk_Organiser_Plate_1</span><span class="pp-chip">Printing</span><span class="pp-pct">37%</span></div>
    <div class="pp-bar"><i></i></div>
    <div class="pp-meta"><span>Layer <b>142</b> / 380</span><span><b>2 h 04 min</b> left</span><span id="pp-finish">Done around 11:24</span><span id="pp-slot"><span class="d"></span>AMS 1 Slot 2</span></div>
  </div>
  <div class="pp-card pp-off" id="pp-off">Print Progress is hidden. The Printer tab starts with your printers.</div><!--/printer-preview-->

  <div class="divider spaced"></div>

  <!--server--><h3><span class="emoji">\U0001F310</span>Choose Your Source</h3>
  <div class="field-label">Where should Spoolio read your spools from?</div>
  <div class="seg" id="src-seg">
    <label><input type="radio" name="spool-source" value="spoolman">Spoolman</label>
    <label><input type="radio" name="spool-source" value="bambu">Bambu Cloud</label>
  </div>
  <div id="src-spoolman">
    <label for="url">Enter the URL of your self-hosted Spoolman server:</label>
    <div class="url-row">
      <input id="url" type="text" value="__URL__" placeholder="__DEFAULT_URL__">
      <button class="test" id="test">Test</button>
    </div>
    <div class="status __STATUS_CLASS__" id="conn-status">__STATUS_TEXT__</div>
  </div>
  <div id="src-bambu" class="src-hidden">
    <div id="bambu-in" class="src-hidden">
      <div class="field-label">Bambu Cloud Account</div>
      <div class="url-row">
        <input id="bambu-account" type="text" readonly value="__BAMBU_ACCOUNT__">
        <button class="test" id="bambu-test">Test Connection</button>
        <button class="test" id="bambu-out">Sign Out</button>
      </div>
    </div>
    <div id="bambu-login">
      <div class="field-label">Sign in with your Bambu Cloud account</div>
      <input id="bambu-email" type="text" placeholder="Email" autocomplete="off">
      <div class="url-row" style="margin-top: 8px">
        <input id="bambu-pass" type="password" placeholder="Password" autocomplete="off">
        <select id="bambu-region"><option value="global">Global</option><option value="china">China</option></select>
        <button class="test" id="bambu-signin">Sign In</button>
      </div>
    </div>
    <div id="bambu-code" class="src-hidden">
      <div class="field-label" id="bambu-code-label">Enter your Bambu Lab verification code</div>
      <div class="url-row">
        <input id="bambu-code-input" type="text" autocomplete="off" placeholder="Code">
        <button class="test" id="bambu-verify">Verify</button>
        <button class="test" id="bambu-back">Back</button>
      </div>
      <div class="hint"><a href="#" id="bambu-resend">Send a New Code</a></div>
    </div>
    <div class="status pending" id="bambu-status">__BAMBU_STATUS__</div>
    <div class="hint warn"><strong>!</strong> Experimental: this uses Bambu's unofficial cloud
    service, which Bambu can change at any time. Spoolio never stores your password.</div>
  </div><!--/server-->

  <div class="divider spaced"></div>

  <!--stats--><h3><span class="emoji">📊</span>Configure Stats</h3>
  <label class="check-row"><input id="show-stats" type="checkbox">Show the Stats Tab</label>
  <div id="stats-opts">
    <div class="field-label">Show filament costs in:</div>
    <div class="seg" id="cur-seg">
      <label><input type="radio" name="stats-cur" value="spoolman">From Spoolman (<span id="cur-code">__CUR_CODE__</span>)</label>
      <label><input type="radio" name="stats-cur" value="gbp">£ GBP</label>
      <label><input type="radio" name="stats-cur" value="eur">€ EUR</label>
      <label><input type="radio" name="stats-cur" value="usd">$ USD</label>
    </div>
    <div class="hint">Costs come from the spool prices in Spoolman. From Spoolman uses the currency set in Spoolman's own settings. Picking another only changes the symbol, it doesn't convert prices.</div>
  </div>
  <div class="hint">Spoolio keeps recording print history while the tab is hidden, so your stats are complete if you turn it on later.</div><!--/stats-->
  <!--stats-preview--><div class="cur-prev" id="cur-prev">
    <div class="cur-tile"><span>Filament Cost</span><b><span class="cur">£</span>24.10</b></div>
    <div class="cur-row"><span class="nm">Headphone_Stand.3mf</span><span>94.3 g</span><span><span class="cur">£</span>2.08</span></div>
  </div>
  <div class="pp-card st-off" id="stats-off">The Stats tab is hidden. Print history is still recorded.</div><!--/stats-preview-->

  <!--about--><div class="about">
    <div class="logpath">Log file: __LOG_PATH__</div>
  </div><!--/about-->

  <!--footer--><div class="footer">
    <div class="version-row">
      <span>Plugin version __VERSION__</span>
      <button class="link" id="check-update">Check for Updates</button>
      <span id="update-status"></span>
    </div>
    <button id="cancel">Cancel</button>
    <button class="primary" id="save" disabled>Save &amp; Close</button>
  </div><!--/footer-->
  <script>
    document.getElementById("cancel").addEventListener("click", function () {
      orca.postMessage({ type: "cancel" });
    });
    const urlEl = document.getElementById("url");
    const saveEl = document.getElementById("save");
    const statusEl = document.getElementById("conn-status");
    const norm = function (u) { return u.trim().replace(/\\/+$/, ""); };
    const srcRadios = document.querySelectorAll('input[name="spool-source"]');
    function srcPick() {
      const picked = document.querySelector('input[name="spool-source"]:checked');
      return picked ? picked.value : "spoolman";
    }
    let verifiedBambu = __VERIFIED_BAMBU__;

    // Save stays disabled until the URL passes a test. An already-connected saved URL
    // counts as passed, so other settings can be changed on their own.
    let verifiedUrl = __VERIFIED_URL__;
    let verifiedText = statusEl.textContent;

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }
    function refreshSave() {
      if (srcPick() === "bambu") {
        saveEl.disabled = !verifiedBambu;
        return;
      }
      const url = norm(urlEl.value);
      saveEl.disabled = !(url && url === verifiedUrl);
    }

    urlEl.addEventListener("input", function () {
      if (norm(urlEl.value) === verifiedUrl) {
        setStatus(verifiedText, "ok");
      } else {
        setStatus("Press Test to check the connection before saving.", "pending");
      }
      refreshSave();
    });

    document.getElementById("test").addEventListener("click", function () {
      setStatus("Testing connection...", "pending");
      orca.postMessage({ type: "test", source: "spoolman", url: urlEl.value.trim() });
    });

    const bambuStatus = document.getElementById("bambu-status");
    const bambuAccount = document.getElementById("bambu-account");
    function setBambu(text, cls) {
      bambuStatus.textContent = text;
      bambuStatus.className = "status " + cls;
    }
    function checkBambu() {
      setBambu("Connecting to Bambu Cloud...", "pending");
      orca.postMessage({ type: "test", source: "bambu" });
    }
    document.getElementById("bambu-test").addEventListener("click", checkBambu);
    const bambuPanes = ["in", "login", "code"];
    function bambuPane(name) {
      bambuPanes.forEach(function (p) {
        document.getElementById(p === "in" ? "bambu-in" : "bambu-" + p).classList.toggle("src-hidden", p !== name);
      });
    }
    let bambuStep = "password";
    function bambuSend(action, extra) {
      setBambu({ password: "Signing in...", sign_out: "Signing out...", resend: "Sending a new code..." }[action] ||
               "Verifying your code...", "pending");
      orca.postMessage(Object.assign({ type: "bambu_auth", action: action }, extra || {}));
    }
    document.getElementById("bambu-signin").addEventListener("click", function () {
      const passEl = document.getElementById("bambu-pass");
      bambuSend("password", {
        account: document.getElementById("bambu-email").value,
        password: passEl.value,
        region: document.getElementById("bambu-region").value,
      });
      passEl.value = "";
    });
    document.getElementById("bambu-verify").addEventListener("click", function () {
      bambuSend(bambuStep, { code: document.getElementById("bambu-code-input").value });
    });
    document.getElementById("bambu-code-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") document.getElementById("bambu-verify").click();
    });
    document.getElementById("bambu-resend").addEventListener("click", function (e) {
      e.preventDefault();
      bambuSend("resend");
    });
    document.getElementById("bambu-back").addEventListener("click", function () {
      bambuPane("login");
      setBambu("Signed out", "pending");
    });
    document.getElementById("bambu-out").addEventListener("click", function () {
      verifiedBambu = false;
      bambuSend("sign_out");
    });
    function showSource(event) {
      const mode = srcPick();
      srcRadios.forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
      document.getElementById("src-spoolman").classList.toggle("src-hidden", mode !== "spoolman");
      document.getElementById("src-bambu").classList.toggle("src-hidden", mode !== "bambu");
      if (mode === "bambu" && !verifiedBambu && event && event.isTrusted) checkBambu();
      refreshSave();
    }
    srcRadios.forEach(function (r) { r.addEventListener("change", showSource); });
    document.querySelector('input[name="spool-source"][value=' + __SOURCE__ + ']').checked = true;
    showSource();
    if (srcPick() === "bambu") checkBambu();

    document.getElementById("feedback").addEventListener("click", function () {
      orca.postMessage({ type: "feedback" });
    });
    const wRadios = document.querySelectorAll('input[name="weight-display"]');
    const wSample = document.getElementById("weight-sample");
    function wPick() {
      const picked = document.querySelector('input[name="weight-display"]:checked');
      return picked ? picked.value : "both";
    }
    function showWeight() {
      const mode = wPick();
      wRadios.forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
      wSample.innerHTML = mode === "grams" ? "640 g"
        : mode === "percent" ? "64%"
        : '<span class="ws-pct">64% \u00b7</span> 640 g';
    }
    wRadios.forEach(function (r) { r.addEventListener("change", showWeight); });
    document.querySelector('input[name="weight-display"][value=' + __WEIGHT_MODE__ + ']').checked = true;
    showWeight();
    const cRadios = document.querySelectorAll('input[name="spool-cols"]');
    function cPick() {
      const picked = document.querySelector('input[name="spool-cols"]:checked');
      return picked ? Number(picked.value) : 1;
    }
    function showCols() {
      cRadios.forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
      document.getElementById("lay-prev").classList.toggle("two", cPick() === 2);
    }
    cRadios.forEach(function (r) { r.addEventListener("change", showCols); });
    document.querySelector('input[name="spool-cols"][value="' + __COLUMNS__ + '"]').checked = true;
    showCols();
    const progOn = document.getElementById("show-progress");
    const progFinish = document.getElementById("prog-finish");
    const progSlot = document.getElementById("prog-slot");
    const idleRadios = document.querySelectorAll('input[name="progress-idle"]');
    function progPick() {
      const idle = document.querySelector('input[name="progress-idle"]:checked');
      return { on: progOn.checked, finish: progFinish.checked, slot: progSlot.checked, idle: idle ? idle.value : "line" };
    }
    function showProgress() {
      (progOn.closest(".srow") || document.body).classList.toggle("prog-off", !progOn.checked);
      document.getElementById("pp-finish").hidden = !progFinish.checked;
      document.getElementById("pp-slot").hidden = !progSlot.checked;
      idleRadios.forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
    }
    [progOn, progFinish, progSlot].concat(Array.from(idleRadios)).forEach(function (el) {
      el.addEventListener("change", showProgress);
    });
    (function (p) {
      progOn.checked = p.on;
      progFinish.checked = p.finish;
      progSlot.checked = p.slot;
      document.querySelector('input[name="progress-idle"][value="' + p.idle + '"]').checked = true;
    })(__PROGRESS__);
    showProgress();
    const statsOn = document.getElementById("show-stats");
    const curRadios = document.querySelectorAll('input[name="stats-cur"]');
    const CUR = { gbp: "£", eur: "€", usd: "$" };
    function statsPick() {
      const c = document.querySelector('input[name="stats-cur"]:checked');
      return { on: statsOn.checked, cur: c ? c.value : "spoolman" };
    }
    function showStats() {
      const st = statsPick();
      (statsOn.closest(".srow") || document.body).classList.toggle("stats-off", !st.on);
      curRadios.forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
      const code = st.cur === "spoolman" ? document.getElementById("cur-code").textContent : st.cur.toUpperCase();
      const sign = CUR[code.toLowerCase()] || code + " ";
      document.querySelectorAll("#cur-prev .cur").forEach(function (e) { e.textContent = sign; });
    }
    [statsOn].concat(Array.from(curRadios)).forEach(function (el) { el.addEventListener("change", showStats); });
    (function (st) {
      statsOn.checked = st.on;
      document.querySelector('input[name="stats-cur"][value="' + st.cur + '"]').checked = true;
    })(__STATS__);
    showStats();
    const updateEl = document.getElementById("update-status");
    document.getElementById("check-update").addEventListener("click", function () {
      updateEl.textContent = "Checking...";
      orca.postMessage({ type: "check_update" });
    });
    function showUpdate(data) {
      updateEl.textContent = "";
      if (!data.ok) {
        updateEl.textContent = data.error;
      } else if (data.newer) {
        const link = document.createElement("button");
        link.className = "link";
        link.textContent = "Version " + data.version + " is available - view release";
        link.addEventListener("click", function () { orca.postMessage({ type: "release" }); });
        updateEl.appendChild(link);
      } else {
        updateEl.textContent = "You're up to date.";
      }
    }

    orca.onMessage(function (data) {
      if (data && data.type === "update_result") {
        showUpdate(data);
        return;
      }
      if (data && data.type === "bambu_auth_result") {
        if (!data.ok) {
          setBambu(data.error || "Sign in failed", "error");
        } else if (data.step === "done") {
          document.getElementById("bambu-code-input").value = "";
          checkBambu();
        } else if (data.step === "out") {
          bambuPane("login");
          bambuAccount.value = "Signed out";
          setBambu("Signed out", "pending");
          refreshSave();
        } else {
          bambuStep = data.step;
          document.getElementById("bambu-code-label").textContent = "Enter your Bambu Lab verification code";
          document.getElementById("bambu-resend").parentNode.classList.toggle("src-hidden", data.step === "tfa");
          bambuPane("code");
          document.getElementById("bambu-code-input").focus();
          setBambu(data.warn ? "Bambu didn't confirm it sent a code (" + data.warn + "). Check your email, or use Send a New Code"
                   : data.step === "tfa" ? "Enter the code from your authenticator app" : "Enter the code Bambu Lab emailed you", data.warn ? "error" : "pending");
        }
        return;
      }
      if (!data || data.type !== "test_result") return;
      if (data.source === "bambu") {
        bambuAccount.value = data.account || "Signed out";
        verifiedBambu = !!data.ok;
        bambuPane(data.ok || !data.signin ? "in" : "login");
        if (data.ok) {
          setBambu("Connected to Bambu Cloud", "ok");
        } else {
          const why = data.error || "connection failed";
          setBambu("Not connected - " + why.charAt(0).toLowerCase() + why.slice(1), "error");
        }
        refreshSave();
        return;
      }
      if (data.ok) {
        verifiedUrl = norm(data.url || "");
        verifiedText = "Connected - Spoolman v" + data.version;
        if (norm(urlEl.value) === verifiedUrl) setStatus(verifiedText, "ok");
      } else {
        setStatus(data.error || "Connection failed", "error");
      }
      refreshSave();
    });

    // Confirms the message listener above is registered before Python sends
    // anything unprompted; see _on_settings's "ready" handler.
    orca.postMessage({ type: "ready" });

    saveEl.addEventListener("click", function () {
      const url = urlEl.value.trim();
      const lowStock = document.getElementById("low-stock").value;
      orca.postMessage({
        type: "save", source: srcPick(), url: url, low_stock: lowStock,
        plate_check: document.getElementById("plate-check").checked,
        plate_margin: document.getElementById("plate-margin").value,
        weight_display: wPick(),
        show_cart: document.getElementById("show-cart").checked,
        spool_columns: cPick(),
        progress: progPick(),
        stats: statsPick(),
        appearance: window.__spoolioLook ? window.__spoolioLook() : null,
        presets: window.__spoolioPresets ? window.__spoolioPresets() : null,
      });
    });
    refreshSave();
  </script>
</body>
</html>
"""


def main_html() -> str:
    return _fill(
        MAIN_PAGE,
        LOGO=LOGO_IMG,
        LOGODATA=LOGO_DATA_URI,
        FONT=FONT_STACK,
        PLUGIN_NAME="Spoolio",
        LOW_STOCK_DEFAULT=LOW_DEFAULT,
        REFRESH_MS=REFRESH_SECS * 1000,
    )


def settings_html(c: dict) -> str:
    return _fill(
        SETTINGS_PAGE,
        LOGO=LOGO_IMG,
        LOGODATA=LOGO_DATA_URI,
        FONT=FONT_STACK,
        PLUGIN_NAME="Spoolio",
        VERSION=PLUGIN_VERSION,
        DEFAULT_URL=html.escape(DEFAULT_SPOOLMAN_URL),
        URL=html.escape(c["url"] or DEFAULT_SPOOLMAN_URL),
        LOW_STOCK=c["low_stock"],
        PLATE_CHECKED="checked" if c["plate_check"] else "",
        PLATE_MARGIN=c["plate_margin"],
        WEIGHT_MODE=json.dumps(c["weight_display"]),
        CART_CHECKED="checked" if c["show_cart"] else "",
        COLUMNS=c["spool_columns"],
        PROGRESS=json.dumps(c["progress"]),
        STATS=json.dumps(c["stats"]),
        CUR_CODE=_cur["code"] or "EUR",
        LOG_PATH=html.escape(str(LOG_FILE)),
        STATUS_TEXT="Checking connection...",
        STATUS_CLASS="pending",
        VERIFIED_URL='""',
        SOURCE=json.dumps(c["source"]),
        VERIFIED_BAMBU="false",
        BAMBU_ACCOUNT="",
        BAMBU_STATUS="",
    )


def _split_page(page: str) -> tuple[str, str, str]:
    def between(start: str, end: str, begin: int = 0) -> str:
        a = page.index(start, begin) + len(start)
        return page[a:page.index(end, a)]

    body_start = page.index("<body>")
    return (
        between("<style>", "</style>"),
        between("<body>", "<script>"),
        between("<script>", "</script>", body_start),
    )


# The page templates wrap their sections in <!--name--> ... <!--/name--> markers, so the
# tab can pull them apart and rearrange them.
def _take_header(body: str) -> tuple[str, str]:
    start = body.index("<!--header-->")
    end = body.index("<!--/header-->") + len("<!--/header-->")
    return body[:start] + body[end:], body[start:end]


def _section(body: str, name: str) -> str:
    start = body.index(f"<!--{name}-->") + len(f"<!--{name}-->")
    return body[start:body.index(f"<!--/{name}-->")]


def _hidden(condition: bool) -> str:
    return "view-hidden" if condition else ""


def _button(header: str, button_id: str) -> str:
    return re.search(rf'<button[^>]*id="{button_id}".*?</button>', header, re.S).group(0)


def _scope_css(css: str, root: str) -> str:
    # Both pages style bare elements (body, button, h2...), so each page's rules are
    # prefixed with its view's id to stop them leaking into the other view.
    def scope(selector: str) -> str:
        selector = selector.strip()
        if re.fullmatch(r"from|to|[\d.]+%", selector):
            return selector
        if selector == "*":
            return f"{root}, {root} *"
        if selector in ("html", "body"):
            return root
        for element in ("html ", "body "):
            if selector.startswith(element):
                return f"{root} {selector[len(element):]}"
        return f"{root} {selector}"

    def rule(match: re.Match) -> str:
        selectors = ", ".join(scope(sel) for sel in match.group(2).split(","))
        return f"{match.group(1)}{selectors} {{{match.group(3)}}}"

    return re.sub(r"(\s*)([^{}]+?)\s*\{([^{}]*)\}", rule, css)


# Pinned to the top and outside both views, so the logo and title never move when
# switching. !important stops OrcaSlicer's own button styling repainting the buttons.
TAB_HEADER_CSS = """
  #app-header, #app-header * { font-family: __FONT__; }
  #app-header {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 76px; box-sizing: border-box;
    display: flex; align-items: center; gap: 12px; padding: 0 12px;
    background: var(--orca-bg); border-bottom: 1px solid var(--orca-border);
  }
  #app-header img.logo { width: 52px; height: 52px; flex-shrink: 0; }
  #app-header h2 {
    flex: 1; min-width: 0; margin: 0; font-size: 20px; font-weight: 700; color: var(--orca-fg);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #app-header .header-actions { display: flex; flex-shrink: 0; }
  #app-header button, #app-header button:hover, #app-header button:focus, #app-header button:active {
    color: var(--orca-accent-fg) !important; background: var(--orca-accent) !important;
    border: 1px solid var(--orca-accent) !important; box-shadow: none !important;
    border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 700;
  }
  #app-header h2 { flex: 0 1 auto; }
  #app-header .header-actions { margin-left: auto; }
  #app-header .tabs { display: inline-flex; margin: 0 0 0 20px; border: 1px solid var(--orca-border); border-radius: 8px; overflow: hidden; }
  #app-header .tabs[hidden] { display: none; }
  #app-header .tabs button, #app-header .tabs button:hover, #app-header .tabs button:focus, #app-header .tabs button:active {
    color: var(--orca-fg) !important; background: transparent !important; border: 0 !important;
    border-right: 1px solid var(--orca-border) !important; border-radius: 0 !important;
    padding: 7px 20px; font-weight: 600;
  }
  #app-header .tabs button:last-child { border-right: 0 !important; }
  #app-header .tabs button.on, #app-header .tabs button.on:hover, #app-header .tabs button.on:focus, #app-header .tabs button.on:active {
    color: var(--orca-accent-fg) !important; background: var(--orca-accent) !important;
  }
  #app-header #tab-dot { display: inline-block; width: 8px; height: 8px; margin-left: 7px; border-radius: 50%; background: #e0a030; }
  #app-header #tab-dot[hidden] { display: none; }
  #app-header #settings-btn { padding: 7px 16px; }
  #app-header #feedback { padding: 6px; line-height: 0; }
"""


# Each topic is a card: the settings on the left, beside their preview or guide on the right.
# Narrow windows stack the two halves.
TAB_SETTINGS_CSS = """
  #view-settings { margin: 0 auto; }
  #view-settings .srow {
    margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 12px;
    background: color-mix(in srgb, var(--orca-fg) 3%, var(--orca-bg) 97%);
  }
  #view-settings .srow > h3 {
    display: flex; align-items: center; margin: 0; padding: 14px 18px 0;
  }
  #view-settings .srow-body {
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 28px; padding: 12px 18px 16px;
  }
  #view-settings .srow-r .label {
    margin-bottom: 8px; font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    color: var(--orca-muted);
  }
  #view-settings .srow-r > .hint { margin-top: 0; }
  #view-settings .srow .step { margin-bottom: 10px; }
  #view-settings .srow .step:last-child { margin-bottom: 0; }
  #view-settings .srow .seg:last-child { margin-bottom: 0; }
  #view-settings .srow .check-row:last-child { margin-bottom: 0; }
  #view-settings .footer { border-top: none; padding-top: 6px; }
  #view-settings .foot-diag { margin-right: auto; min-width: 0; font-size: 11px; line-height: 1.6; color: var(--orca-muted); }
  #view-settings .foot-diag .version-row, #view-settings .foot-diag .about { margin: 0; }
  #view-settings .preview {
    padding: 14px 16px; border: 1px solid var(--orca-border); border-radius: 10px;
    background: var(--orca-border);
    background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%);
  }
  #view-settings .preview-count { font-weight: 700; }
  #view-settings .preview-list { list-style: none; margin: 8px 0 0; padding: 0; }
  #view-settings .preview-list li {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 6px 0; border-top: 1px solid var(--orca-border);
  }
  #view-settings .preview-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #view-settings .preview-weight { flex-shrink: 0; color: #e0a030; }
  #view-settings .preview-note { color: var(--orca-muted); }
  #view-settings .preview-list:not(:empty) + .preview-note:not(:empty) { margin-top: 6px; }
  @media (max-width: 900px) {
    #view-settings .srow-body { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  }
"""

TAB_PREVIEW_HTML = """
    <div class="preview" aria-live="polite">
      <div class="preview-count" id="preview-count"></div>
      <ul class="preview-list" id="preview-list"></ul>
      <div class="preview-note" id="preview-note"></div>
    </div>
"""

# Reads the spool list the main view already receives, so the preview needs no requests.
TAB_PREVIEW_JS = """
  var input = document.getElementById("low-stock");
  var countEl = document.getElementById("preview-count");
  var listEl = document.getElementById("preview-list");
  var noteEl = document.getElementById("preview-note");
  var MAX_LISTED = 5;
  var spools = null;

  function formatWeight(grams) {
    return Math.abs(grams) >= 1000 ? (grams / 1000).toFixed(2) + " kg" : Math.round(grams) + " g";
  }

  function update() {
    var grams = parseFloat(input.value);
    listEl.innerHTML = "";
    countEl.textContent = "";
    noteEl.textContent = "";
    if (spools === null) {
      noteEl.textContent = "Connect a spool source to preview which spools would trigger a reorder.";
      return;
    }
    if (!(grams >= 0)) {
      noteEl.textContent = "Enter a weight in grams to preview.";
      return;
    }
    // Same rule the spool cards use for the reorder cart.
    var low = spools.filter(function (s) {
      return typeof s.remaining_weight === "number" && s.remaining_weight < grams;
    }).sort(function (a, b) { return a.remaining_weight - b.remaining_weight; });
    countEl.textContent = (low.length || "No") + (low.length === 1 ? " spool is" : " spools are") +
      " under " + grams + " g";
    low.slice(0, MAX_LISTED).forEach(function (s) {
      var item = document.createElement("li");
      var name = document.createElement("span");
      var weight = document.createElement("span");
      name.className = "preview-name";
      name.textContent = (s.filament && s.filament.name) || ("Spool #" + s.id);
      weight.className = "preview-weight";
      weight.textContent = formatWeight(s.remaining_weight);
      item.appendChild(name);
      item.appendChild(weight);
      listEl.appendChild(item);
    });
    if (low.length > MAX_LISTED) noteEl.textContent = "+ " + (low.length - MAX_LISTED) + " more";
  }

  input.addEventListener("input", update);
  bridge.onMessage(function (data) {
    if (!data) return;
    if (data.type === "spools") {
      spools = data.ok ? (data.spools || []) : null;
      update();
    } else if (data.type === "show_view" && data.view === "settings") {
      update();
    }
  });
  update();
"""


# Loaded before either view's script. Each view gets its own `orca` object: the
# settings view tags what it sends (both views send "ready"), and a single real
# onMessage handler fans incoming messages out to both views.
TAB_BRIDGE = """
(function () {
  var host = window.orca;
  var handlers = [];
  host.onMessage(function (data) {
    handlers.forEach(function (handler) {
      try { handler(data); } catch (err) { console.error(err); }
    });
  });
  function bridge(tag) {
    return {
      postMessage: function (message) {
        host.postMessage(tag ? Object.assign({}, message, { view: tag }) : message);
      },
      onMessage: function (handler) { handlers.push(handler); },
    };
  }
  window.__spoolio = { main: bridge(null), settings: bridge("settings") };

  var pgT = new WeakMap();
  window.pgIn = function (el, back) {
    if (!el) return;
    clearTimeout(pgT.get(el));
    el.classList.remove("pg-r", "pg-l");
    Array.prototype.forEach.call(el.children, function (c, i) { c.classList.add("pg-s"); c.style.setProperty("--i", Math.min(i, 10)); });
    void el.offsetWidth;
    el.classList.add(back ? "pg-l" : "pg-r");
    pgT.set(el, setTimeout(function () { el.classList.remove("pg-r", "pg-l"); }, 900));
  };

  window.glide = function (bar) {
    if (!bar || !bar.offsetWidth) return;
    var on = bar.querySelector("button.on:not([hidden])"), ind = bar.querySelector(".ind");
    if (!ind) {
      ind = document.createElement("span");
      ind.className = "ind";
      bar.insertBefore(ind, bar.firstChild);
      if (window.ResizeObserver) new ResizeObserver(function () { glide(bar); }).observe(bar);
    }
    bar.classList.add("glide");
    ind.hidden = !on;
    if (!on) return;
    ind.style.left = on.offsetLeft + "px";
    ind.style.width = on.offsetWidth + "px";
    if (!bar.classList.contains("glide-on")) { void ind.offsetWidth; bar.classList.add("glide-on"); }
  };

  handlers.push(function (data) {
    if (!data || data.type !== "show_view") return;
    var settings = data.view === "settings";
    if (settings === document.getElementById("view-settings").classList.contains("view-hidden"))
      pgIn(document.getElementById(settings ? "view-settings" : "view-main"), !settings);
    document.getElementById("view-main").classList.toggle("view-hidden", settings);
    document.getElementById("view-settings").classList.toggle("view-hidden", !settings);
    document.getElementById("actions-main").classList.toggle("view-hidden", settings);
    document.getElementById("tabs-main").classList.toggle("view-hidden", settings);
    document.getElementById("actions-settings").classList.toggle("view-hidden", !settings);
    if (!settings) return;
    // Reset the form to the saved values each time Settings is opened, so
    // edits abandoned with Cancel don't linger.
    var url = document.getElementById("url");
    url.value = data.url || url.placeholder;
    url.dispatchEvent(new Event("input"));
    document.getElementById("low-stock").value = data.low_stock;
    document.getElementById("plate-check").checked = data.plate_check !== false;
    if (data.plate_margin !== undefined) document.getElementById("plate-margin").value = data.plate_margin;
    var src = document.querySelector('input[name="spool-source"][value="' + (data.source || "spoolman") + '"]');
    if (src) { src.checked = true; src.dispatchEvent(new Event("change")); }
    var mode = document.querySelector('input[name="weight-display"][value="' + (data.weight_display || "both") + '"]');
    if (mode) { mode.checked = true; mode.dispatchEvent(new Event("change")); }
    document.getElementById("show-cart").checked = data.show_cart === true;
    var cols = document.querySelector('input[name="spool-cols"][value="' + (data.spool_columns === 2 ? 2 : 1) + '"]');
    if (cols) { cols.checked = true; cols.dispatchEvent(new Event("change")); }
    var prog = data.progress || {};
    document.getElementById("prog-finish").checked = prog.finish !== false;
    document.getElementById("prog-slot").checked = prog.slot !== false;
    var idle = document.querySelector('input[name="progress-idle"][value="' + (prog.idle === "hide" ? "hide" : "line") + '"]');
    if (idle) idle.checked = true;
    var progOn = document.getElementById("show-progress");
    progOn.checked = prog.on !== false;
    progOn.dispatchEvent(new Event("change"));
    var st = data.stats || {};
    if (data.cur_code) document.getElementById("cur-code").textContent = data.cur_code;
    var cur = document.querySelector('input[name="stats-cur"][value="' + (st.cur || "spoolman") + '"]');
    if (cur) cur.checked = true;
    var stOn = document.getElementById("show-stats");
    stOn.checked = st.on !== false;
    stOn.dispatchEvent(new Event("change"));
    document.getElementById("update-status").textContent = "";
  });
})();
"""


EDITOR_CSS = """
  #pc, #pc *, #pt, #pt * { font-family: __FONT__; }
  #pc { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 16px; box-sizing: border-box; }
  #pc[hidden], #pt[hidden] { display: none; }
  #pc .card { position: relative; width: 860px; max-width: 100%; height: 600px; max-height: 100%; display: flex; flex-direction: column; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg)); border: 1px solid var(--orca-border); border-radius: 14px; color: var(--orca-fg); font-size: 13px; line-height: 1.4; box-shadow: 0 18px 50px rgba(0,0,0,.45); overflow: hidden; }
  #pc .top { display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--orca-border); }
  #pc .top h2 { margin: 0; font-size: 16px; font-weight: 700; }
  #pc .top h2 span { color: var(--orca-muted); font-weight: 500; margin-left: 6px; font-size: 13px; }
  #pc .x { margin-left: auto; background: none; border: 0; color: var(--orca-muted); font-size: 20px; cursor: pointer; padding: 2px 6px; line-height: 1; }
  #pc .mid { flex: 1; min-height: 0; display: flex; }
  #pc .rail { width: 210px; flex-shrink: 0; border-right: 1px solid var(--orca-border); padding: 12px 10px; display: flex; flex-direction: column; gap: 2px; background: color-mix(in srgb, var(--orca-fg) 2%, transparent); }
  #pc .st { display: flex; gap: 10px; align-items: flex-start; padding: 9px 10px; border-radius: 8px; cursor: pointer; border: 0; background: none; color: inherit; font-size: 13px; text-align: left; width: 100%; }
  #pc .st:hover { background: color-mix(in srgb, var(--orca-fg) 6%, transparent); }
  #pc .st.lock { opacity: .5; cursor: not-allowed; }
  #pc .st.lock:hover { background: none; }
  #pc .st.on { background: color-mix(in srgb, var(--orca-accent) 18%, transparent); }
  #pc .st .n { width: 22px; height: 22px; flex-shrink: 0; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11.5px; font-weight: 700; border: 1.5px solid var(--orca-border); color: var(--orca-muted); }
  #pc .st.on .n { background: var(--orca-accent); border-color: var(--orca-accent); color: var(--orca-accent-fg); }
  #pc .st.done .n { border-color: var(--orca-accent); color: #4fd1c5; }
  #pc .st.chg .n { border-color: #e0a030; color: #f0b545; }
  #pc .st b { display: block; font-size: 13px; font-weight: 600; }
  #pc .st.on b { color: #4fd1c5; }
  #pc .st small { display: block; color: var(--orca-muted); font-size: 11.5px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px; }
  #pc .mini { margin-top: auto; padding: 10px; border-radius: 10px; border: 1px solid var(--orca-border); display: flex; gap: 10px; align-items: center; }
  #pc .mini .d { width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0; border: 1px solid rgba(255,255,255,.12); }
  #pc .mini b { display: block; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 135px; }
  #pc .mini span { font-size: 11px; color: var(--orca-muted); }
  #pc .body { flex: 1; min-width: 0; overflow-y: auto; padding: 18px 22px 22px; }
  #pc .body h3 { margin: 0 0 4px; font-size: 15px; font-weight: 700; color: var(--orca-accent); display: flex; align-items: center; gap: 8px; }
  #pc .body h3 .emoji { font-size: 19px; }
  #pc .body .lead { margin: 0 0 14px; color: var(--orca-muted); font-size: 12.5px; }
  #pc .f2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 14px; }
  #pc .f3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 14px; }
  #pc .fl { display: block; margin: 0 0 5px; color: color-mix(in srgb, var(--orca-fg) 82%, var(--orca-bg)); font-size: 12.5px; }
  #pc .in { position: relative; display: flex; align-items: center; }
  #pc .in input, #pc .in select, #pc textarea { width: 100%; box-sizing: border-box; padding: 9px 11px; font-size: 13px; background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; }
  #pc .in input:focus, #pc .in select:focus, #pc textarea:focus { outline: none; border-color: var(--orca-accent); }
  #pc .in .u { position: absolute; right: 11px; color: var(--orca-muted); font-size: 12px; pointer-events: none; }
  #pc .in input[disabled], #pc .in select[disabled] { opacity: .5; }
  #pc .chg .in input, #pc .chg .in select, #pc .chg textarea { border-color: #e0a030; box-shadow: 0 0 0 1px rgba(224,160,48,.35); }
  #pc .chg .fl::after { content: "Changed"; margin-left: 6px; font-size: 10px; font-weight: 700; color: #f0b545; }
  #pc textarea { min-height: 70px; resize: vertical; line-height: 1.45; margin-top: 0; }
  #pc .seg { display: inline-flex; margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 6px; overflow: hidden; }
  #pc .seg button { margin: 0; padding: 6px 14px; cursor: pointer; border: 0; border-right: 1px solid var(--orca-border); background: transparent; color: var(--orca-fg); font-size: 12.5px; border-radius: 0; }
  #pc .seg button:last-child { border-right: 0; }
  #pc .seg button.on { background: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 700; }
  #pc .ams { display: flex; gap: 12px; align-items: center; padding: 10px 12px; border-radius: 10px; border: 1px solid color-mix(in srgb, var(--orca-accent) 55%, transparent); background: color-mix(in srgb, var(--orca-accent) 9%, transparent); margin-bottom: 14px; }
  #pc .ams .d { width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; }
  #pc .ams span:not(.d) { flex: 1; font-size: 12.5px; }
  #pc .srch { margin-bottom: 10px; }
  #pc .ls { border: 1px solid var(--orca-border); border-radius: 10px; overflow: hidden; }
  #pc .li { display: flex; align-items: center; gap: 12px; padding: 9px 12px; border-top: 1px solid var(--orca-border); cursor: pointer; }
  #pc .li:first-child { border-top: 0; }
  #pc .li:hover { background: color-mix(in srgb, var(--orca-fg) 5%, transparent); }
  #pc .li.on { background: color-mix(in srgb, var(--orca-accent) 15%, transparent); box-shadow: inset 3px 0 0 var(--orca-accent); }
  #pc .li .d { width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0; border: 1px solid rgba(255,255,255,.12); }
  #pc .li span:not(.d) { flex: 1; min-width: 0; }
  #pc .li small { display: block; color: var(--orca-muted); font-size: 11.5px; }
  #pc .li i { font-style: normal; font-size: 11px; color: var(--orca-muted); }
  #pc .li.on i { color: #4fd1c5; font-weight: 700; }
  #pc .note { margin-top: 12px; padding: 9px 12px; border-radius: 8px; background: color-mix(in srgb, var(--orca-fg) 5%, transparent); color: var(--orca-muted); font-size: 12px; line-height: 1.5; }
  #pc .note.amb { background: rgba(224,160,48,.08); border: 1px solid rgba(224,160,48,.4); color: color-mix(in srgb, var(--orca-fg) 85%, var(--orca-bg)); }
  #pc .note.bad { background: rgba(217,83,79,.1); border: 1px solid rgba(217,83,79,.45); color: var(--orca-fg); }
  #pc .lock { display: flex; gap: 10px; align-items: center; }
  #pc .lock .sb { margin-left: auto; }
  #pc .col { display: flex; gap: 8px; align-items: center; }
  #pc .col .sw { width: 36px; height: 36px; flex-shrink: 0; border-radius: 50%; border: 1px solid var(--orca-border); }
  #pc .meter { height: 8px; border-radius: 4px; background: color-mix(in srgb, var(--orca-fg) 10%, transparent); overflow: hidden; margin-top: 14px; }
  #pc .meter i { display: block; height: 100%; border-radius: 4px; }
  #pc .calc { margin-top: 8px; font-size: 12px; color: var(--orca-muted); }
  #pc .calc b { color: var(--orca-fg); }
  #pc .tg { display: flex; align-items: center; gap: 10px; padding: 7px 0; cursor: pointer; }
  #pc .ucs { display: flex; flex-wrap: wrap; gap: 6px; }
  #pc .uc { border: 1px solid var(--orca-border); background: transparent; color: var(--orca-fg); border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 12px; cursor: pointer; }
  #pc .uc.on { background: var(--orca-accent); border-color: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 600; }
  #pc .tog { width: 36px; height: 20px; border-radius: 10px; background: #55555b; position: relative; flex-shrink: 0; }
  #pc .tog::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: #e8e8e8; transition: left .15s; }
  #pc .tog.on { background: var(--orca-accent); }
  #pc .tog.on::after { left: 19px; }
  #pc .mono { font-family: ui-monospace, Consolas, Menlo, "DejaVu Sans Mono", monospace !important; font-size: 12px; }
  #pc .sep { height: 1px; background: var(--orca-border); margin: 16px 0; }
  #pc .xh { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--orca-muted); margin-bottom: 10px; }
  #pc .pv .spool-card { margin: 0 0 14px; }
  #pc .kv { display: grid; grid-template-columns: 130px minmax(0, 1fr); gap: 0; font-size: 12.5px; border: 1px solid var(--orca-border); border-radius: 10px; overflow: hidden; margin: 0; }
  #pc .kv > * { padding: 7px 12px; border-top: 1px solid var(--orca-border); margin: 0; overflow-wrap: anywhere; }
  #pc .kv > :nth-child(-n+2) { border-top: 0; }
  #pc .kv dt { color: var(--orca-muted); }
  #pc .kv dd a { color: var(--orca-accent); cursor: pointer; float: right; font-size: 11.5px; margin-left: 8px; }
  #pc .kv .o { color: var(--orca-muted); text-decoration: line-through; }
  #pc .kv .nw { color: #f0b545; font-weight: 600; }
  #pc .kv .fil { font-size: 10px; font-weight: 700; color: #f0b545; margin-left: 4px; }
  #pc .bot { display: flex; align-items: center; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--orca-border); }
  #pc .bot .pg { color: var(--orca-muted); font-size: 12px; margin-right: auto; }
  #pc .dots { display: inline-flex; gap: 5px; margin-left: 10px; vertical-align: middle; }
  #pc .dots i { width: 7px; height: 7px; border-radius: 50%; background: var(--orca-border); }
  #pc .dots i.on { background: var(--orca-accent); width: 18px; border-radius: 4px; }
  #pc .sb { display: inline-flex; align-items: center; gap: 6px; background: transparent; color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 8px 16px; font-size: 12.5px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  #pc .sb.pri { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  #pc .sb.amb { background: #e0a030; border-color: #e0a030; color: #1d1d1f; }
  #pc .sb.ghost { border-color: transparent; color: var(--orca-muted); }
  #pc .sb[disabled] { opacity: .45; cursor: default; }
  #pc .ask { position: absolute; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; border-radius: 14px; }
  #pc .ask .box { width: 360px; max-width: calc(100% - 32px); background: color-mix(in srgb, var(--orca-fg) 8%, var(--orca-bg)); border: 1px solid var(--orca-border); border-radius: 12px; padding: 16px 18px; }
  #pc .ask h4 { margin: 0 0 6px; font-size: 15px; }
  #pc .ask p { margin: 0 0 14px; color: color-mix(in srgb, var(--orca-fg) 80%, var(--orca-bg)); }
  #pc .row { display: flex; justify-content: flex-end; gap: 8px; }
  #pc .dcard { width: 440px; max-width: 100%; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg)); border: 1px solid var(--orca-border); border-radius: 14px; padding: 18px 20px; color: var(--orca-fg); font-size: 13px; line-height: 1.45; box-shadow: 0 18px 50px rgba(0,0,0,.45); }
  #pc .dh { display: flex; align-items: center; gap: 10px; font-size: 16px; font-weight: 700; margin-bottom: 14px; }
  #pc .pulse { width: 10px; height: 10px; border-radius: 50%; background: #3fb950; box-shadow: 0 0 0 4px rgba(63,185,80,.2); animation: live-pulse 2s ease-out infinite; }
  #pc:not([hidden]) { animation: pc-fade .18s ease-out; }
  #pc.fresh .card, #pc .dcard { animation: pc-pop .22s ease-out; }
  #pt:not([hidden]) { animation: pt-up .25s ease-out; }
  @keyframes pc-fade { from { opacity: 0; } }
  @keyframes pc-pop { from { opacity: 0; transform: translateY(10px) scale(.98); } }
  @keyframes pt-up { from { opacity: 0; transform: translate(-50%, 12px); } }
  #pc .dsp { display: flex; gap: 12px; align-items: center; padding: 10px 12px; border: 1px solid var(--orca-border); border-radius: 10px; }
  #pc .dsp .d { width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0; }
  #pc .dsp small { display: block; color: var(--orca-muted); font-size: 11.5px; }
  #pc .tls { display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow: auto; }
  #pc .tls .dsp .d { width: 24px; height: 24px; }
  #pc .tls small b { color: var(--orca-fg); }
  #pc .tfo { display: flex; align-items: center; gap: 12px; }
  #pc .tfo span { color: var(--orca-muted); font-size: 12px; }
  #pc .dcard p { margin: 12px 0; color: color-mix(in srgb, var(--orca-fg) 82%, var(--orca-bg)); }
  #pc .dtag { display: flex; flex-direction: column; gap: 4px; padding: 9px 12px; border-radius: 8px; background: color-mix(in srgb, var(--orca-fg) 5%, transparent); margin-bottom: 16px; }
  #pc .dtag span { font-size: 11px; color: var(--orca-muted); font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
  #pc .dtag code { font-family: ui-monospace, Consolas, Menlo, "DejaVu Sans Mono", monospace !important; font-size: 12.5px; word-break: break-all; color: #4fd1c5; }
  #pt { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); background: color-mix(in srgb, var(--orca-fg) 10%, var(--orca-bg)); border: 1px solid var(--orca-border); color: var(--orca-fg); border-radius: 8px; padding: 10px 16px; font-size: 13px; box-shadow: 0 6px 20px rgba(0,0,0,.35); z-index: 1001; }
  #pt b { color: #3fb950; }
  @media (max-width: 760px) {
    #pc .mid { flex-direction: column; }
    #pc .rail { width: auto; flex-direction: row; border-right: 0; border-bottom: 1px solid var(--orca-border); padding: 8px; overflow-x: auto; gap: 4px; }
    #pc .st { width: auto; padding: 7px 9px; }
    #pc .st small, #pc .mini { display: none; }
    #pc .f3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    #pc .card { height: 100%; }
  }
"""

# The Add Spool / Edit Spool card and the New Spool Detected pop-up.
EDITOR_JS = r"""
  var D = null, spools = [], canEdit = false, seen = {}, cat = { q: null, items: [], busy: false }, ct;
  var mode, step, S, M, src, q, pick, unlock, ams, cur, ask, used, busy, err, auto, raw0, tg, tmsg, tseen = {};
  var RF = '<div class="spool-rfid"><span>RFID</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="5" cy="19" r="1.8" fill="currentColor" stroke="none"></circle><path d="M5,14 A5,5 0 0 1 10,19"></path><path d="M5,10.5 A8.5,8.5 0 0 1 13.5,19"></path><path d="M5,7 A12,12 0 0 1 17,19"></path></svg></div>';
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var nv = function (a, b) { return a == null || a === "" ? (b == null ? "" : b) : a; };
  var r1 = function (n) { return Math.round(n * 10) / 10; };
  var r2 = function (n) { return typeof n === "number" ? Math.round(n * 100) / 100 : n; };
  var cl = function (n) { return typeof n === "number" ? Number(n.toPrecision(12)) : Array.isArray(n) ? n.map(cl) : n; };
  var g = function (n) { return (n === "" || n == null || isNaN(n)) ? "-" : r1(Number(n)).toLocaleString("en-GB") + " g"; };
  var hex = function (c) { c = String(c || "").replace("#", ""); return /^[0-9a-fA-F]{6}/.test(c) ? c.slice(0, 6).toUpperCase() : ""; };
  var dec = function (v) { if (typeof v !== "string") return v; try { return JSON.parse(v); } catch (e) { return v; } };
  var plural = function (n, w) { return n + " " + w + (n === 1 ? "" : "s"); };
  var STEPS = {
    add: [["fil", "Filament", "🧵"], ["amt", "Amount", "⚖️"], ["sto", "Storage", "📍"], ["tag", "RFID & Extras", "🏷️"], ["rev", "Review", "✅"]],
    edit: [["amt", "Amount", "⚖️"], ["sto", "Storage", "📍"], ["tag", "RFID & Extras", "🏷️"], ["fil", "Filament", "🧵"], ["rev", "Review", "✅"]]
  };
  var TX = { flow: "filament_flow_ratio", pa: "pressure_advance", mvs: "filament_max_volumetric_speed", rlen: "filament_retraction_length", rspd: "filament_retraction_speed" };
  var TO = { flow: "flow_ratio", mvs: "max_volumetric_speed", rlen: "retraction_length", rspd: "retraction_speed" };
  var TS = { flow: "Flow", pa: "PA", mvs: "Max Speed", rlen: "Retract", rspd: "Retract Speed" };
  var tks = function () { return D && D.tune && D.tune.on ? D.tune.keys : []; };
  var perSp = function () { return !!(D && D.tune && D.tune.per === "spool"); };
  var tset = function () { return perSp() && tg === "spool" ? "ts" : "tf"; };
  var tblank = function () { var o = { by: {} }; Object.keys(TX).forEach(function (k) { o[k] = ""; }); return o; };
  function tv(ex) {
    ex = ex || {};
    var o = { by: {} }, by = dec(dec(ex.pressure_advance_printers));
    Object.keys(TX).forEach(function (k) { var v = dec(dec(ex[TX[k]])); if (typeof v !== "number" && TO[k]) v = dec(dec(ex[TO[k]])); o[k] = typeof v === "number" ? cl(v) : ""; });
    if (by && typeof by === "object") Object.keys(by).forEach(function (p) { if (typeof by[p] === "number") o.by[p] = cl(by[p]); });
    return o;
  }
  function tfrom(t) {
    var o = tblank();
    if (t) { Object.keys(TX).forEach(function (k) { o[k] = t[k] == null ? "" : cl(t[k]); }); Object.keys(t.by || {}).forEach(function (p) { o.by[p] = cl(t.by[p]); }); }
    return o;
  }
  function steps() {
    var s = STEPS[mode].slice();
    if (tks().length) s.splice(s.map(function (x) { return x[0]; }).indexOf("tag") + 1, 0, ["tune", "Tuning", "🎛️"]);
    return s;
  }
  document.body.insertAdjacentHTML("beforeend", '<div id="pc" hidden></div><div id="pt" hidden></div>');
  var pc = document.getElementById("pc"), pt = document.getElementById("pt"), tt;
  function toast(t) { clearTimeout(tt); pt.innerHTML = t; pt.hidden = false; tt = setTimeout(function () { pt.hidden = true; }, 4000); }
  var fields = function () { return (D && D.fields || []).filter(function (f) { return f.key !== "tag"; }); };
  function fils() {
    if (D && D.filaments) return D.filaments;
    var o = {}, a = [];
    spools.forEach(function (s) {
      var f = s.filament || {};
      if (f.id == null || o[f.id]) return;
      o[f.id] = 1;
      a.push({ id: f.id, v: (f.vendor || {}).name || "", n: f.name || "", m: f.material || "", c: hex(f.color_hex), w: f.weight, sw: f.spool_weight, d: f.density, dia: f.diameter, e: f.settings_extruder_temp, b: f.settings_bed_temp });
    });
    return a;
  }
  var cnt = function (fid) { return spools.filter(function (s) { return (s.filament || {}).id === fid; }).length; };
  function blank() {
    return { fid: null, ext_id: "", name: "", vendor: "", material: "", color_hex: "", diameter: 1.75, density: 1.24, ext: "", bed: "",
      initial_weight: 1000, spool_weight: 250, remaining_weight: 1000, location: "", lot_nr: "", price: "", comment: "", tag: "", extra: {},
      tf: tblank(), ts: tblank(), tf0: tblank() };
  }
  function fillX() {
    fields().forEach(function (f) {
      if (!(f.key in S.extra)) {
        var v = cl(dec((raw0 || {})[f.key]));
        if (v == null) v = f.type === "boolean" ? false : "";
        S.extra[f.key] = v;
        if (M) M.extra[f.key] = v;
      }
    });
  }
  function fromFil(f, how) {
    S.fid = how === "mine" ? f.id : null; S.ext_id = how === "cat" ? f.x : "";
    S.name = f.n || ""; S.vendor = f.v || ""; S.material = f.m || ""; S.color_hex = hex(f.c);
    S.diameter = cl(nv(f.dia, 1.75)); S.density = cl(nv(f.d, 1.24)); S.ext = cl(nv(f.e)); S.bed = cl(nv(f.b));
    S.initial_weight = r2(nv(f.w, 1000)); S.spool_weight = r2(nv(f.sw, 250));
    if (!used) S.remaining_weight = S.initial_weight;
    if (ams) S.remaining_weight = ams.pct == null ? S.initial_weight : Math.round(S.initial_weight * ams.pct / 100);
    pick = how + ":" + (how === "mine" ? f.id : how === "cat" ? f.x : f.n);
    S.tf = tfrom(how === "mine" ? f.t : null); S.tf0 = tfrom(how === "mine" ? f.t : null);
  }
  function load() { orca.postMessage({ type: "editor" }); }
  function find(qq) { cat.busy = true; cat.q = qq; orca.postMessage({ type: "catalogue", q: qq }); }
  function reset() { step = 0; ask = ""; busy = false; err = ""; auto = null; ams = null; q = ""; pick = ""; raw0 = {}; tg = "fil"; tmsg = ""; }
  function openAdd() { reset(); mode = "add"; S = blank(); M = null; src = "mine"; unlock = true; used = false; cur = null; fillX(); load(); draw(); }
  function openEdit(s) {
    reset();
    var f = s.filament || {};
    mode = "edit"; cur = s.id; unlock = false; used = "rem"; raw0 = s.extra || {};
    S = { fid: f.id, ext_id: "", name: f.name || "", vendor: (f.vendor || {}).name || "", material: f.material || "", color_hex: hex(f.color_hex),
      diameter: cl(nv(f.diameter)), density: cl(nv(f.density)), ext: cl(nv(f.settings_extruder_temp)), bed: cl(nv(f.settings_bed_temp)),
      initial_weight: r2(nv(s.initial_weight, f.weight)), spool_weight: r2(nv(s.spool_weight, f.spool_weight)), remaining_weight: r2(nv(s.remaining_weight)),
      location: s.location || "", lot_nr: s.lot_nr || "", price: r2(nv(s.price)), comment: s.comment || "",
      tag: String(nv(dec(raw0.tag))), extra: {}, use: D ? (D.use[s.id] || []).slice() : null, tf: tv(f.extra), ts: tv(s.extra) };
    M = JSON.parse(JSON.stringify(S)); fillX(); load(); draw();
  }
  window.__spoolioEdit = function (s) { if (s) openEdit(s); else openAdd(); };
  var LBL = { name: "Name", vendor: "Manufacturer", material: "Material", color_hex: "Colour", diameter: "Diameter", density: "Density", ext: "Nozzle Temp", bed: "Bed Temp",
    initial_weight: "Filament Weight", spool_weight: "Empty Spool", remaining_weight: "Remaining", location: "Location", lot_nr: "Lot Number", price: "Price", comment: "Note", tag: "RFID Tag", use: "Use With" };
  var unit = function (k) { return { initial_weight: " g", spool_weight: " g", remaining_weight: " g", price: " " + (D ? D.cur : "€"), diameter: " mm", density: " g/cm³", ext: " °C", bed: " °C" }[k] || ""; };
  var FILK = ["name", "vendor", "material", "color_hex", "diameter", "density", "ext", "bed"];
  var SPK = ["remaining_weight", "initial_weight", "spool_weight", "location", "lot_nr", "price", "comment"];
  var STEPK = { amt: ["remaining_weight", "initial_weight", "spool_weight"], sto: ["location", "lot_nr", "price", "comment", "use"], tag: ["tag"], fil: FILK };
  var same = function (a, b) { return JSON.stringify(a == null ? "" : a) === JSON.stringify(b == null ? "" : b) || String(a) === String(b); };
  function fmt(k, v) {
    if (k === "use") return useTxt(v);
    if (v === "" || v == null || (Array.isArray(v) && !v.join(""))) return "Empty";
    if (k === "color_hex") return "#" + String(v).toUpperCase();
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (Array.isArray(v)) return v.join(" - ");
    return String(v) + unit(k);
  }
  function diffs() {
    if (!M) return [];
    var d = [];
    Object.keys(LBL).forEach(function (k) { if (!same(S[k], M[k])) d.push({ k: k, l: LBL[k], o: fmt(k, M[k]), n: fmt(k, S[k]), fil: FILK.indexOf(k) >= 0 }); });
    fields().forEach(function (f) { if (!same(S.extra[f.key], M.extra[f.key])) d.push({ k: "x:" + f.key, l: f.name, o: fmt("", M.extra[f.key]), n: fmt("", S.extra[f.key]) }); });
    (perSp() ? ["tf", "ts"] : ["tf"]).forEach(function (w) {
      var a = S[w], b = M[w], tl = w === "ts" ? " (This Spool)" : "";
      tks().forEach(function (t) {
        if (!same(a[t.k], b[t.k])) d.push({ k: "t:" + w + ":" + t.k, l: t.l + tl, o: fmt("", b[t.k]), n: fmt("", a[t.k]), fil: w === "tf" });
        if (t.k !== "pa") return;
        var ps = Object.keys(a.by).concat(Object.keys(b.by).filter(function (p) { return !(p in a.by); }));
        ps.forEach(function (p) { if (!same(a.by[p], b.by[p])) d.push({ k: "t:" + w + ":by:" + p, l: "Pressure Advance on " + p + tl, o: fmt("", b.by[p]), n: fmt("", a.by[p]), fil: w === "tf" }); });
      });
    });
    return d;
  }
  var chgStep = function (id) { var ks = STEPK[id] || []; return diffs().some(function (x) { return ks.indexOf(x.k) >= 0 || (id === "tag" && x.k.indexOf("x:") === 0) || (id === "tune" && x.k.indexOf("t:") === 0); }); };
  function sum(id) {
    if (id === "fil") return S.name ? S.name : "Not Chosen Yet";
    if (id === "amt") return g(S.remaining_weight) + " Left";
    if (id === "sto") return S.location || "No Location";
    if (id === "tag") return S.tag ? "Tag " + S.tag.slice(0, 8) + "…" : "No Tag";
    if (id === "tune") {
      var o = S[perSp() && tg === "spool" ? "ts" : "tf"], a = tks().filter(function (t) { return o[t.k] !== ""; }).map(function (t) { return TS[t.k] + " " + o[t.k]; });
      var nb = Object.keys(o.by).length;
      if (nb) a.push(plural(nb, "Printer PA"));
      return a.length ? a.slice(0, 2).join(" · ") + (a.length > 2 ? " +" + (a.length - 2) : "") : "Not Set";
    }
    if (id === "rev") return mode === "edit" ? (diffs().length ? diffs().length + " to Save" : "No Changes") : "Check and Add";
  }
  var ok = function (id) { return id !== "fil" || !!String(S.name).trim(); };
  function inp(k, u, o) {
    o = o || {};
    var ch = M && !same(S[k], M[k]);
    return '<div class="' + (ch ? "chg" : "") + '"' + (o.span ? ' style="grid-column:span ' + o.span + '"' : "") + '><label class="fl">' + (o.l || LBL[k]) + (o.rf ? RF : "") + '</label><div class="in"><input data-k="' + k + '" value="' + esc(k === "color_hex" ? String(S[k]).toUpperCase() : S[k]) + '"' +
      (o.dis ? " disabled" : "") + (o.ph ? ' placeholder="' + o.ph + '"' : "") + (o.list ? ' list="' + o.list + '"' : "") + (o.cls ? ' class="' + o.cls + '"' : "") +
      (u ? ' style="padding-right:' + (16 + u.length * 7) + 'px"' : "") + '>' + (u ? '<span class="u">' + u + '</span>' : "") + '</div></div>';
  }
  function card() {
    var col = "#" + (S.color_hex || "555555"), rem = Number(S.remaining_weight), ini = Number(S.initial_weight) || 1, pct = Math.max(0, Math.min(100, Math.round(rem / ini * 100)));
    return '<div class="spool-card">' + (S.vendor ? '<div class="spool-tag"><svg viewBox="0 0 12 12" fill="currentColor"><rect x="0" y="0" width="5" height="5" rx="1"></rect><rect x="7" y="0" width="5" height="5" rx="1"></rect><rect x="0" y="7" width="5" height="5" rx="1"></rect><rect x="7" y="7" width="5" height="5" rx="1"></rect></svg><span>' + esc(S.vendor) + '</span></div>' : "") +
      '<div class="spool-body"><div class="spool-swatch-col"><div class="spool-swatch" style="background:' + col + '"></div>' + (S.tag ? RF : "") + '</div><div class="spool-main"><div class="spool-top"><div class="spool-title">' + esc(S.name || "New Spool") + '</div>' +
      '<div class="spool-weight"><span class="spool-pct">' + pct + '% ·</span>' + g(rem) + '</div></div>' +
      '<div class="spool-bar-track"><div class="spool-bar-fill" style="width:' + pct + '%;background:' + col + '"></div></div>' +
      '<div class="spool-subtitle"><span class="sub-text">' + esc(S.material || "-") + ' · ' + col.toUpperCase() + ' · ' + esc(S.diameter) + 'mm' + (S.location ? ' · ' + esc(S.location) : "") + '</span></div></div></div></div>';
  }
  var amsTxt = function () { return esc(ams.printer + " · " + ams.where); };
  function pFil() {
    if (mode === "edit") {
      var n = cnt(S.fid);
      return '<div class="note' + (unlock ? " amb" : "") + ' lock" style="margin:0 0 14px">' + (unlock ? "⚠️ Applies to " + plural(n, "Spool") + "." :
        "🔒 Shared Filament Fields.") + (unlock ? "" : '<button class="sb" data-a="unlock">Edit Filament</button>') + '</div>' + filForm(!unlock);
    }
    var h = ams ? '<div class="ams"><span class="d" style="background:#' + (ams.c || "555555") + '"></span><span><b>✓ Filled In From ' + amsTxt() + '</b></span></div>' : "";
    h += '<div class="seg">' + [["mine", "My Filaments"], ["cat", "Spoolman Catalogue"], ["new", "New Filament"]].map(function (x) { return '<button data-src="' + x[0] + '" class="' + (src === x[0] ? "on" : "") + '">' + x[1] + '</button>'; }).join("") + '</div>';
    if (src === "new") return h + filForm(false);
    var rows, empty = "Nothing Matches. Try the Catalogue or Add a New Filament.";
    if (src === "mine") {
      rows = fils().map(function (f) { var c = cnt(f.id); return { k: "mine:" + f.id, c: f.c, n: f.n, s: [f.v, f.m, plural(c, "Spool")].filter(Boolean).join(" · "), f: f }; });
      rows = rows.filter(function (r) { return !q || (r.n + " " + r.s).toLowerCase().indexOf(q.toLowerCase()) >= 0; });
    } else {
      if (cat.q !== q && !cat.busy) find(q);
      rows = cat.items.map(function (f) { return { k: "cat:" + f.x, c: f.c, n: f.n, s: [f.v, f.m, f.w ? f.w + " g" + (f.sw ? " on a " + f.sw + " g Spool" : "") : ""].filter(Boolean).join(" · "), f: f }; });
      if (cat.busy) empty = "Searching…"; else if (cat.err) empty = esc(cat.err);
    }
    h += '<div class="in srch"><input data-q="1" value="' + esc(q) + '" placeholder="' + (src === "mine" ? "Search Your Filaments" : "Search the Catalogue, e.g. Polymaker PLA") + '"></div>';
    h += '<div class="ls">' + (rows.length ? rows.map(function (r, i) { return '<div class="li' + (pick === r.k ? " on" : "") + '" data-pick="' + i + '"><span class="d" style="background:#' + (r.c || "555555") + '"></span><span>' + esc(r.n) + '<small>' + esc(r.s) + '</small></span><i>' + (pick === r.k ? "Selected" : "Choose") + '</i></div>'; }).join("") : '<div class="li"><span>' + empty + '</span></div>') + '</div>';
    if (src === "cat" && cat.more) h += '<div class="note">Showing the First 60. Type More to Narrow It Down.</div>';
    pFil.rows = rows;
    return h;
  }
  function filForm(dis) {
    var col = "#" + (S.color_hex || "555555");
    return '<div class="f3">' + inp("name", "", { dis: dis, span: 2, ph: "e.g. PLA Matte - Black" }) +
      inp("vendor", "", { dis: dis, list: "pc-vend", ph: "Pick or Type a New One" }) +
      inp("material", "", { dis: dis, list: "pc-mats", ph: "PLA" }) +
      '<div class="' + (M && !same(S.color_hex, M.color_hex) ? "chg" : "") + '"><label class="fl">Colour</label><div class="col"><span class="sw" style="background:' + col + '"></span><div class="in" style="flex:1"><input data-k="color_hex" value="' + esc(String(S.color_hex).toUpperCase()) + '"' + (dis ? " disabled" : "") + ' placeholder="000000"></div></div></div>' +
      inp("diameter", "mm", { dis: dis }) + inp("density", "g/cm³", { dis: dis }) + inp("ext", "°C", { dis: dis }) + inp("bed", "°C", { dis: dis }) + '</div>' +
      '<datalist id="pc-vend">' + (D ? D.vendors : []).map(function (v) { return '<option value="' + esc(v) + '">'; }).join("") + '</datalist><datalist id="pc-mats"><option value="PLA"><option value="PETG"><option value="ABS"><option value="ASA"><option value="TPU"><option value="PA"><option value="PC"></datalist>';
  }
  function pAmt() {
    var rem = Number(S.remaining_weight), ini = Number(S.initial_weight), col = "#" + (S.color_hex || "555555"), h = "";
    var opts = mode === "add" ? [[false, "New and Sealed"], [true, "Already Used"]] : [["rem", "Grams Left"], ["use", "Grams Used"]];
    h += '<div class="seg">' + opts.map(function (x) { return '<button data-used="' + x[0] + '" class="' + (used === x[0] ? "on" : "") + '">' + x[1] + '</button>'; }).join("") + '</div>';
    var main = used === "use" ? '<div class="' + (!same(S.remaining_weight, M.remaining_weight) ? "chg" : "") + '"><label class="fl">Used</label><div class="in"><input data-k="usedg" value="' + r2(ini - rem) + '" style="padding-right:24px"><span class="u">g</span></div></div>'
      : (mode === "add" && !used) ? '<div><label class="fl">Remaining</label><div class="in"><input value="' + esc(S.initial_weight) + '" disabled style="padding-right:24px"><span class="u">g</span></div></div>'
      : inp("remaining_weight", "g");
    h += '<div class="f3">' + main + inp("initial_weight", "g", { l: "Filament Weight" }) + inp("spool_weight", "g", { l: "Empty Spool Weight" }) + '</div>';
    h += '<div class="meter"><i style="width:' + Math.max(0, Math.min(100, rem / (ini || 1) * 100)) + '%;background:' + col + '"></i></div><div class="calc"><b>' + g(rem) + '</b> Left of ' + g(ini) + ' · ' + g(ini - rem) + ' Used</div>';
    return h;
  }
  function useTxt(v) {
    if (!v || !v.length) return "Automatic";
    var ps = D ? D.use_printers : [];
    return v.map(function (i) { var p = ps.filter(function (x) { return x.id === i; })[0]; return p ? p.name : i; }).join(", ");
  }
  function pUse() {
    var ps = D ? D.use_printers : [];
    if (!ps.length || S.use == null) return "";
    var c = M && !same(S.use, M.use) ? "chg" : "";
    var chip = function (id, name, on) { return '<button type="button" class="uc' + (on ? " on" : "") + '" data-use="' + esc(id) + '">' + esc(name) + '</button>'; };
    return '<div style="margin-top:14px" class="' + c + '"><label class="fl">Use With</label><div class="ucs">' + chip("", "Automatic", !S.use.length) +
      ps.map(function (p) { return chip(p.id, p.name, S.use.indexOf(p.id) >= 0); }).join("") + '</div>' +
      '<div class="note">Picks which printers get an OrcaSlicer preset for this spool. Automatic follows the printer it\'s loaded in, or all your printers when it isn\'t loaded.</div></div>';
  }
  function pSto() {
    var locs = []; spools.forEach(function (x) { if (x.location && locs.indexOf(x.location) < 0) locs.push(x.location); });
    return '<div class="f3">' + inp("location", "", { list: "pc-locs", ph: "e.g. Shelf" }) + inp("lot_nr", "", { ph: "Optional" }) + inp("price", (D ? D.cur : "€"), { ph: "Optional" }) + '</div>' +
      '<datalist id="pc-locs">' + locs.map(function (l) { return '<option value="' + esc(l) + '">'; }).join("") + '</datalist>' +
      '<div style="margin-top:14px" class="' + (M && !same(S.comment, M.comment) ? "chg" : "") + '"><label class="fl">Note</label><textarea data-k="comment" placeholder="Optional">' + esc(S.comment) + '</textarea></div>' + pUse();
  }
  function xf(f) {
    var val = S.extra[f.key], c = M && !same(val, M.extra[f.key]) ? "chg" : "", lb = '<label class="fl">' + esc(f.name) + '</label>';
    if (f.type === "boolean") return '<div class="' + c + '">' + lb + '<div class="tg" data-xb="' + esc(f.key) + '"><div class="tog' + (val ? " on" : "") + '"></div><span>' + (val ? "Yes" : "No") + '</span></div></div>';
    if (f.type === "choice" && !f.multi) return '<div class="' + c + '">' + lb + '<div class="in"><select data-x="' + esc(f.key) + '"><option value="">None</option>' + f.choices.map(function (o) { return '<option' + (o === val ? " selected" : "") + '>' + esc(o) + '</option>'; }).join("") + '</select></div></div>';
    var shown = Array.isArray(val) ? val.join(f.type.indexOf("range") > 0 ? " - " : ", ") : val;
    var ph = f.type.indexOf("range") > 0 ? "Min - Max" : f.multi ? f.choices.join(", ") : f.type === "datetime" ? "YYYY-MM-DD HH:MM" : "";
    return '<div class="' + c + '">' + lb + '<div class="in"><input data-x="' + esc(f.key) + '" value="' + esc(shown) + '" placeholder="' + esc(ph) + '"' + (f.unit ? ' style="padding-right:' + (16 + f.unit.length * 7) + 'px"' : "") + '>' + (f.unit ? '<span class="u">' + esc(f.unit) + '</span>' : "") + '</div></div>';
  }
  function xval(f, t) {
    if (f.type === "integer" || f.type === "float") { var n = parseFloat(t.replace(",", ".")); return t.trim() === "" ? "" : isNaN(n) ? t : (f.type === "integer" ? Math.round(n) : n); }
    if (f.type.indexOf("range") > 0) { var p = t.split(/\s+-\s+|\s*[–,;]\s*/).map(function (x) { var n = parseFloat(x); return isNaN(n) ? "" : n; }); return t.trim() === "" ? "" : [nv(p[0]), nv(p[1])]; }
    if (f.multi) return t.trim() === "" ? "" : t.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    return t;
  }
  function pTag() {
    var xs = fields();
    return '<div class="f2">' + inp("tag", "", { l: " ", rf: 1, cls: "mono", span: 2, ph: "Empty if the Spool Has No Tag" }) + '</div>' +
      (mode === "add" && ams ? '<div class="note">Read From ' + amsTxt() + '.</div>' : "") +
      '<div class="sep"></div><div class="xh">Your Spoolman Extra Fields</div>' +
      (!D ? '<div class="note">Loading…</div>' : xs.length ? '<div class="f2">' + xs.map(xf).join("") + '</div>' : '<div class="note">No Extra Fields Set Up in Spoolman.</div>');
  }
  function tinp(w, k, l, u, ph) {
    var o = S[w], by = k.indexOf("by:") === 0, v = by ? o.by[k.slice(3)] : o[k], m0 = M ? (by ? M[w].by[k.slice(3)] : M[w][k]) : null;
    var ch = M && !same(v, m0);
    return '<div class="' + (ch ? "chg" : "") + '"><label class="fl">' + esc(l) + '</label><div class="in"><input data-t="' + esc(k) + '" value="' + esc(v == null ? "" : v) + '"' +
      (ph !== "" && ph != null ? ' placeholder="' + esc(ph) + '"' : "") + (u ? ' style="padding-right:' + (16 + u.length * 7) + 'px"' : "") + '>' + (u ? '<span class="u">' + esc(u) + '</span>' : "") + '</div></div>';
  }
  function pTune() {
    var w = tset(), sp = w === "ts", f = S.tf, ks = tks(), h = "";
    if (perSp()) {
      h += '<label class="fl">Save Values To</label><div class="seg">' + [["fil", S.name || "The Filament"], ["spool", "This Spool Only"]].map(function (x) { return '<button data-tg="' + x[0] + '" class="' + (tg === x[0] ? "on" : "") + '">' + esc(x[1]) + '</button>'; }).join("") + '</div>' +
        '<div class="note" style="margin:-4px 0 14px">' + (sp ? "These override the filament's values for this spool only. Leave a box empty to use the filament's value." : "Shared by every spool of this filament.") + '</div>';
    } else {
      h += '<div class="note" style="margin:0 0 14px">Saved to the filament, so every spool of it shares them. To set values per spool, choose one preset per spool in Settings.</div>';
    }
    var ph = function (k) { return sp ? (k.indexOf("by:") === 0 ? nv(f.by[k.slice(3)], nv(S.ts.pa, f.pa)) : f[k]) : ""; };
    var rest = ks.filter(function (t) { return t.k !== "pa"; }), pa = ks.filter(function (t) { return t.k === "pa"; })[0];
    if (rest.length) h += '<div class="f3">' + rest.map(function (t) { return tinp(w, t.k, t.l, t.u, ph(t.k)); }).join("") + '</div>';
    if (pa) {
      h += (rest.length ? '<div class="sep"></div>' : "") + '<div class="xh">Pressure Advance</div><div class="f3">' + tinp(w, "pa", "All Printers", "", ph("pa")) +
        (D.tune.printers || []).map(function (p) { return tinp(w, "by:" + p, p, "", ph("by:" + p) !== "" ? ph("by:" + p) : "Same as All"); }).join("") + '</div>' +
        '<div class="note">A printer value is used on that printer instead of All Printers.</div>';
    }
    h += '<div class="sep"></div><div class="tfo"><button class="sb" data-a="tune-orca">Save From OrcaSlicer</button><span>' + esc(tmsg || "Fills these in from the filament preset selected in OrcaSlicer, unsaved changes included.") + '</span></div>';
    return h;
  }
  function pRev() {
    var h = '<div class="pv">' + card() + '</div>';
    if (mode === "edit") {
      var d = diffs();
      if (!d.length) return h + '<div class="note">No Changes Yet.</div>';
      return h + '<dl class="kv">' + d.map(function (x) { return '<dt>' + esc(x.l) + (x.fil ? '<span class="fil">Filament</span>' : "") + '</dt><dd><span class="o">' + esc(x.o) + '</span> → <span class="nw">' + esc(x.n) + '</span></dd>'; }).join("") + '</dl>';
    }
    var newf = pick.indexOf("mine:") !== 0, c = D ? D.cur : "€";
    var rows = [["Filament", (S.name || "Not Chosen") + (newf && S.name ? " (New in Spoolman)" : ""), 0], ["Manufacturer", S.vendor || "-", 0], ["Remaining", g(S.remaining_weight) + " of " + g(S.initial_weight), 1],
      ["Empty Spool", g(S.spool_weight), 1], ["Location", S.location || "-", 2], ["Lot / Price", (S.lot_nr || "-") + " · " + (S.price !== "" ? S.price + " " + c : "-"), 2], ["RFID Tag", S.tag || "None", 3]];
    if (D && D.use_printers.length) rows.splice(6, 0, ["Use With", useTxt(S.use), 2]);
    if (tks().length) rows.push(["Tuning", sum("tune") + (perSp() && tg === "spool" ? " (This Spool)" : ""), 4]);
    return h + '<dl class="kv">' + rows.map(function (r) { return '<dt>' + r[0] + '</dt><dd>' + esc(r[1]) + '<a data-go="' + r[2] + '">Change</a></dd>'; }).join("") + '</dl>';
  }
  function draw() {
    var st = steps(), id = st[step][0], d = diffs(), n = d.length;
    var pages = { fil: pFil, amt: pAmt, sto: pSto, tag: pTag, tune: pTune, rev: pRev };
    var lead = { fil: mode === "add" ? "Choose an Existing Filament or Create a New Filament." : "Shared by Every Spool of This Filament.",
      amt: "How Much Is on the Spool.", sto: "Where It's Kept.", tag: "Tag and Your Spoolman Fields.", tune: "Your Calibrated Values for OrcaSlicer Presets.",
      rev: mode === "add" ? "Check, Then Add." : "Your Changes." };
    var last = step === st.length - 1, nc = "Save " + n + (n === 1 ? " Change" : " Changes");
    var main = last ? (mode === "add" ? '<button class="sb pri" data-a="save"' + (S.name && !busy ? "" : " disabled") + '>' + (busy ? "Adding…" : "Add Spool to Spoolman") + '</button>'
        : '<button class="sb pri" data-a="save"' + (n && !busy ? "" : " disabled") + '>' + (busy ? "Saving…" : n ? nc : "Save Changes") + '</button>')
      : '<button class="sb pri" data-a="next"' + (ok(id) ? "" : " disabled") + '>Next: ' + st[step + 1][1] + ' ›</button>';
    var quick = mode === "edit" && !last && n ? '<button class="sb" data-a="save"' + (busy ? " disabled" : "") + '>' + (busy ? "Saving…" : nc) + '</button>' : "";
    var top = pc.querySelector(".body"), y = top ? top.scrollTop : 0, same0 = pc.dataset.at === mode + step;
    pc.classList.toggle("fresh", pc.hidden);
    pc.innerHTML = '<div class="card">' +
      '<div class="top"><h2>' + (mode === "add" ? "Add Spool" : esc(M.name || "Spool") + '<span>Spool #' + esc(cur) + '</span>') + '</h2><button class="x" data-a="close" title="Close">×</button></div>' +
      '<div class="mid"><nav class="rail">' + st.map(function (x, i) {
        var cls = (i === step ? " on" : "") + (mode === "edit" && chgStep(x[0]) ? " chg" : (i < step && ok(x[0]) ? " done" : ""));
        var lk = i > 0 && !ok("fil") ? " lock" : "";
        return '<button class="st' + cls + lk + '" data-go="' + i + '"' + (lk ? ' aria-disabled="true"' : "") + '><span class="n">' + (cls.indexOf("done") >= 0 ? "✓" : i + 1) + '</span><span><b>' + x[1] + '</b><small>' + esc(sum(x[0])) + '</small></span></button>';
      }).join("") +
      '<div class="mini"><span class="d" style="background:#' + (S.color_hex || "555555") + '"></span><span><b>' + esc(S.name || "New Spool") + '</b><span>' + g(S.remaining_weight) + (S.location ? " · " + esc(S.location) : "") + '</span></span></div></nav>' +
      '<div class="body"><h3><span class="emoji">' + st[step][2] + '</span>' + st[step][1] + '</h3><p class="lead">' + lead[id] + '</p>' +
      (err ? '<div class="note bad" style="margin:0 0 14px">' + esc(err) + '</div>' : "") + pages[id]() + '</div></div>' +
      '<div class="bot"><span class="pg">Step ' + (step + 1) + ' of ' + st.length + '<span class="dots">' + st.map(function (x, i) { return '<i class="' + (i === step ? "on" : "") + '"></i>'; }).join("") + '</span></span>' +
      (step ? '<button class="sb" data-a="back">‹ Back</button>' : '<button class="sb ghost" data-a="close">Cancel</button>') + quick + main + '</div>' +
      (ask ? '<div class="ask"><div class="box">' + ask + '</div></div>' : "") + '</div>';
    pc.dataset.at = mode + step;
    if (same0) pc.querySelector(".body").scrollTop = y;
    pc.hidden = false;
  }
  function num(x) { var n = parseFloat(String(x).replace(",", ".")); return isNaN(n) ? x : n; }
  var NUM = ["initial_weight", "spool_weight", "remaining_weight", "price", "diameter", "density", "ext", "bed"];
  function keep(fn) {
    var a = document.activeElement, sel = null, pos = a && a.selectionStart, raw = a && a.value;
    if (a && a.dataset) { if (a.dataset.k) sel = '[data-k="' + a.dataset.k + '"]'; else if (a.dataset.x) sel = '[data-x="' + a.dataset.x + '"]'; else if (a.dataset.q) sel = '[data-q="1"]'; else if (a.dataset.t) sel = '[data-t="' + a.dataset.t + '"]'; }
    fn(); draw();
    if (sel) {
      var b = pc.querySelector(sel);
      if (b) {
        if (typeof raw === "string" && b.value !== raw && /^\s*-?\d*[.,]?\d*\s*$/.test(raw) && num(raw) === num(b.value)) b.value = raw;
        b.focus(); try { b.setSelectionRange(pos, pos); } catch (e) {}
      }
    }
  }
  pc.addEventListener("input", function (e) {
    var t = e.target, d = t.dataset;
    if (t.tagName === "SELECT") return;
    keep(function () {
      if (d.k === "usedg") S.remaining_weight = r2(Number(S.initial_weight) - num(t.value));
      else if (d.k) {
        S[d.k] = NUM.indexOf(d.k) >= 0 && t.value !== "" ? num(t.value) : (d.k === "color_hex" ? t.value.replace("#", "").toUpperCase() : t.value);
        if (d.k === "initial_weight" && mode === "add" && !used) S.remaining_weight = S.initial_weight;
        if (mode === "add" && FILK.indexOf(d.k) >= 0 && src === "new") { pick = "new"; S.fid = null; S.ext_id = ""; }
      }
      if (d.t) {
        var o = S[tset()], v = t.value.trim() === "" ? "" : num(t.value);
        if (d.t.indexOf("by:") === 0) { if (v === "") delete o.by[d.t.slice(3)]; else o.by[d.t.slice(3)] = v; } else o[d.t] = v;
      }
      if (d.x) { var f = fields().filter(function (x) { return x.key === d.x; })[0]; if (f) S.extra[d.x] = xval(f, t.value); }
      if (d.q) { q = t.value; if (src === "cat") { clearTimeout(ct); ct = setTimeout(function () { find(q); draw(); }, 350); } }
    });
  });
  pc.addEventListener("change", function (e) { var d = e.target.dataset; if (d.x && e.target.tagName === "SELECT") { S.extra[d.x] = e.target.value; draw(); } });
  function done() { pc.hidden = true; pc.innerHTML = ""; ask = ""; cur = null; mode = null; }
  function close() {
    if (busy) return;
    var n = diffs().length, dirty = mode === "edit" ? n : (S.name || S.location || S.tag);
    if (!dirty) return done();
    ask = '<h4>' + (mode === "edit" ? "Discard " + n + (n === 1 ? " Change" : " Changes") + "?" : "Discard This Spool?") + '</h4><p>' + (mode === "edit" ? "Your Edits to " + esc(M.name) + " Haven't Been Saved to Spoolman." : "The Spool Hasn't Been Added to Spoolman Yet.") +
      '</p><div class="row"><button class="sb" data-a="stay">Keep Editing</button><button class="sb amb" data-a="drop">Discard</button></div>'; draw();
  }
  function save() {
    if (busy) return;
    var msg = { type: "spool_save", id: mode === "edit" ? cur : null, fid: S.fid, spool: {}, fil: {}, extra: {} };
    if (mode === "edit") {
      diffs().forEach(function (x) {
        if (SPK.indexOf(x.k) >= 0) msg.spool[x.k] = S[x.k];
        else if (FILK.indexOf(x.k) >= 0) msg.fil[x.k] = S[x.k];
        else if (x.k === "tag") msg.extra.tag = S.tag.trim();
        else if (x.k === "use") msg.use = S.use;
        else if (x.k.indexOf("t:") === 0) tput(msg, x.k.slice(2, 4), x.k.slice(5));
        else msg.extra[x.k.slice(2)] = S.extra[x.k.slice(2)];
      });
    } else {
      if (pick.indexOf("mine:") !== 0) {
        msg.fid = null;
        FILK.forEach(function (k) { msg.fil[k] = S[k]; });
        msg.fil.weight = S.initial_weight; msg.fil.spool_weight = S.spool_weight;
        if (S.ext_id) msg.fil.external_id = S.ext_id;
      }
      SPK.forEach(function (k) { msg.spool[k] = k === "remaining_weight" && !used ? S.initial_weight : S[k]; });
      fields().forEach(function (f) { var v = S.extra[f.key]; if (v !== "" && v !== false && v != null) msg.extra[f.key] = v; });
      if (S.tag.trim()) msg.extra.tag = S.tag.trim();
      if (S.use && S.use.length) msg.use = S.use;
      tks().forEach(function (t) {
        if (!same(S.tf[t.k], S.tf0[t.k])) tput(msg, "tf", t.k);
        if (perSp() && S.ts[t.k] !== "") tput(msg, "ts", t.k);
      });
      if (!same(S.tf.by, S.tf0.by)) tput(msg, "tf", "by:");
      if (perSp() && Object.keys(S.ts.by).length) tput(msg, "ts", "by:");
    }
    busy = true; err = ""; draw();
    orca.postMessage(msg);
  }
  function tput(msg, w, k) {
    msg.tune = msg.tune || {};
    var o = msg.tune[w === "ts" ? "spool" : "fil"] = msg.tune[w === "ts" ? "spool" : "fil"] || {};
    if (k.indexOf("by:") === 0) o.pressure_advance_printers = S[w].by; else o[TX[k]] = S[w][k];
  }
  function tunePrompt(items) {
    items = (items || []).filter(function (x) { return !tseen[x.id]; });
    if (!items.length) return;
    items.forEach(function (x) { tseen[x.id] = 1; });
    pc.innerHTML = '<div class="dcard"><div class="dh"><span class="pulse"></span>New Tuning Values</div><div class="tls">' +
      items.map(function (x) {
        return '<div class="dsp"><span class="d" style="background:#' + (x.c || "555555") + '"></span><span><b>' + esc(x.name) + '</b><small>' + esc(x.label) + ': ' +
          esc(x.old == null ? "Not Set" : x.old) + ' → <b>' + esc(x.new) + '</b> · ' + esc((x.src === "slice" ? "From your slice on " : "Calibrated on ") + x.printer) + '</small></span></div>';
      }).join("") + '</div><p>Save ' + (items.length === 1 ? "this" : "these") + ' to Spoolman so your presets keep ' + (items.length === 1 ? "it" : "them") + '?</p>' +
      '<div class="row"><button class="sb ghost" data-a="tune-skip">Not Now</button><button class="sb pri" data-a="tune-save">Update Spoolman</button></div></div>';
    pc.tune = items.map(function (x) { return x.id; }); pc.det = null; mode = null; pc.dataset.at = ""; pc.hidden = false;
  }
  function saved(r) {
    if (!busy) return;
    busy = false;
    if (!r.ok) { err = r.error || "Spoolman Didn't Accept It."; draw(); return; }
    var n = diffs().length, nm = S.name;
    done();
    if (r.added) toast('<b>✓</b> Added Spool #' + esc(r.id) + ', ' + esc(nm) + ', to Spoolman' + (r.made ? " (New Filament Created)" : "") + '.');
    else toast('<b>✓</b> Saved ' + n + (n === 1 ? " Change" : " Changes") + ' to Spoolman.');
  }
  function prefill(n) {
    ams = n; used = n.pct != null && n.pct < 100;
    S.name = n.name; S.vendor = "Bambu Lab"; S.material = n.m; S.color_hex = n.c; S.diameter = n.dia; S.ext = nv(n.e); S.bed = nv(n.b);
    S.initial_weight = n.w; S.spool_weight = 250; S.remaining_weight = n.pct == null ? n.w : Math.round(n.w * n.pct / 100);
    S.tag = n.uid; S.location = n.printer + (n.unit ? " " + n.unit : "");
    var mine = fils().filter(function (f) { return /bambu/i.test(f.v) && f.m === n.m && f.c === n.c; })[0];
    if (mine) { src = "mine"; fromFil(mine, "mine"); }
    else { src = "cat"; q = "Bambu " + n.name; auto = n.c; pick = "new"; find(q); }
  }
  function det(n) {
    seen[n.uid] = 1;
    pc.innerHTML = '<div class="dcard"><div class="dh"><span class="pulse"></span>New Spool Detected</div>' +
      '<div class="dsp"><span class="d" style="background:#' + (n.c || "555555") + '"></span><span><b>Bambu Lab ' + esc(n.name) + '</b><small>' + esc(n.printer + " · " + n.where + " · " + n.m) + (n.pct != null ? " · " + n.pct + "% Left" : "") + '</small></span></div>' +
      '<p>Not in Spoolman Yet. Add It?</p>' +
      '<div class="dtag"><span>RFID Tag</span><code>' + esc(n.uid) + '</code></div>' +
      '<div class="row"><button class="sb ghost" data-a="det-later">Not Now</button><button class="sb pri" data-a="det-add">Add to Spoolman</button></div></div>';
    pc.det = n; mode = null; pc.dataset.at = ""; pc.hidden = false;
  }
  pc.addEventListener("click", function (e) {
    if (e.target === pc) { if (!mode) { pc.hidden = true; return; } return close(); }
    var t = e.target.closest("[data-a],[data-go],[data-src],[data-pick],[data-used],[data-xb],[data-use],[data-tg]"); if (!t) return;
    var d = t.dataset;
    if (d.go != null) { if (+d.go > 0 && !ok("fil")) return; step = +d.go; ask = ""; draw(); pc.querySelector(".body").scrollTop = 0; return; }
    if (d.src) { src = d.src; q = ""; draw(); return; }
    if (d.pick != null) { var r = pFil.rows[+d.pick]; if (r) { fromFil(r.f, src); draw(); } return; }
    if (d.used != null) {
      used = d.used === "true" ? true : d.used === "false" ? false : d.used;
      if (mode === "add" && !used) S.remaining_weight = S.initial_weight;
      draw(); return;
    }
    if (d.xb) { S.extra[d.xb] = !S.extra[d.xb]; draw(); return; }
    if (d.tg) { tg = d.tg; draw(); return; }
    if (d.use != null) {
      var u = (S.use || []).slice(), i = u.indexOf(d.use);
      if (!d.use) u = []; else if (i >= 0) u.splice(i, 1); else u.push(d.use);
      S.use = u; draw(); return;
    }
    var a = d.a;
    if (a === "next") { step++; draw(); pc.querySelector(".body").scrollTop = 0; }
    if (a === "back") { step--; draw(); }
    if (a === "close") close();
    if (a === "stay") { ask = ""; draw(); }
    if (a === "drop") done();
    if (a === "save") save();
    if (a === "unlock") { unlock = true; draw(); }
    if (a === "det-later") { orca.postMessage({ type: "new_skip", uid: pc.det.uid }); pc.hidden = true; toast("Won't Ask Again for This Spool."); }
    if (a === "det-add") { var n = pc.det; openAdd(); prefill(n); draw(); }
    if (a === "tune-orca") { tmsg = "Reading OrcaSlicer…"; draw(); orca.postMessage({ type: "tune_orca" }); }
    if (a === "tune-skip" || a === "tune-save") { orca.postMessage({ type: "tune_apply", ids: pc.tune, save: a === "tune-save" }); pc.hidden = true; pc.innerHTML = ""; }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || pc.hidden) return;
    if (!mode) pc.hidden = true; else if (ask) { ask = ""; draw(); } else close();
  });
  orca.onMessage(function (m) {
    if (!m) return;
    if (m.type === "spools" && m.ok) {
      spools = m.spools || [];
      if (m.can_edit && !canEdit && !D) load();
      canEdit = m.can_edit === true;
    } else if (m.type === "editor") {
      if (m.ok) D = m;
      if (D && S && (S.use == null || (mode === "edit" && M && same(S.use, M.use)))) { S.use = mode === "edit" ? (D.use[cur] || []).slice() : []; if (M) M.use = S.use.slice(); }
      if (mode) { fillX(); draw(); }
    } else if (m.type === "catalogue") {
      if (m.q !== cat.q) return;
      cat.busy = false; cat.items = m.items || []; cat.more = !!m.more; cat.err = m.ok ? "" : m.error;
      if (auto && mode === "add") {
        var hit = cat.items.filter(function (f) { return /bambu/i.test(f.v) && f.c === auto; })[0];
        auto = null;
        if (hit) fromFil(hit, "cat"); else src = "new";
      }
      if (mode === "add") draw();
    } else if (m.type === "spool_saved") {
      saved(m);
    } else if (m.type === "live" && pc.hidden && !document.getElementById("view-main").classList.contains("view-hidden")) {
      var n = canEdit && Array.isArray(m["new"]) ? m["new"].filter(function (x) { return !seen[x.uid]; })[0] : null;
      if (n) det(n); else if (Array.isArray(m.tune)) tunePrompt(m.tune);
    } else if (m.type === "tune" && pc.hidden) {
      tunePrompt(m.items);
    } else if (m.type === "tune_orca" && mode) {
      if (!m.ok) tmsg = m.error || "Couldn't read OrcaSlicer.";
      else {
        var o = S[tset()], ps = D.tune.printers || [];
        tks().forEach(function (t) {
          var v = m.vals[t.k];
          if (v == null) return;
          if (t.k === "pa" && m.printer && ps.indexOf(m.printer) >= 0) o.by[m.printer] = v; else o[t.k] = v;
        });
        tmsg = "Filled in from " + m.preset + (m.printer ? " on " + m.printer : "") + ". Check them, then save.";
      }
      if (steps()[step] && steps()[step][0] === "tune") draw();
    } else if (m.type === "tune_done") {
      if (!m.ok) toast("Couldn't Update Spoolman: " + esc(m.error || ""));
      else toast(m.save ? '<b>✓</b> Saved ' + plural(m.n || 0, "Value") + ' to Spoolman.' : "Won't Ask Again for These Values.");
    }
  });
"""


MOVE_CSS = """
  #tm, #tm * { font-family: __FONT__; }
  #tm { position: fixed; inset: 0; z-index: 1100; display: flex; align-items: center; justify-content: center; padding: 16px; box-sizing: border-box; background: rgba(0, 0, 0, 0.55); animation: pc-fade .18s ease-out; }
  #tm[hidden] { display: none; }
  #tm .pop { width: min(540px, 100%); max-height: 100%; overflow: auto; box-sizing: border-box; background: var(--orca-bg); border: 1px solid var(--orca-border); border-radius: 10px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45); padding: 16px; color: var(--orca-fg); font-size: 13px; animation: pc-pop .22s ease-out; }
  #tm .t { display: block; font-size: 15px; margin-bottom: 6px; }
  .tm-warn, #tm .ok { display: flex; gap: 10px; align-items: flex-start; margin-top: 10px; padding: 10px 12px; border-radius: 8px; font-size: 12px; line-height: 1.45; }
  .tm-warn { border: 1px solid rgba(210, 153, 34, 0.45); background: rgba(210, 153, 34, 0.14); color: #e0a030; }
  .tm-warn a { color: inherit; font-weight: 700; }
  #tm .ok { border: 1px solid color-mix(in srgb, var(--orca-accent) 50%, transparent); background: color-mix(in srgb, var(--orca-accent) 12%, transparent); }
  .tm-warn svg, #tm .ok svg { flex: none; width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  #tm .ok svg { stroke: var(--orca-accent); stroke-width: 2.4; }
  #tm table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
  #tm th { text-align: left; color: var(--orca-muted); font-weight: 400; padding: 4px 6px; border-bottom: 1px solid var(--orca-border); }
  #tm td { padding: 7px 6px; border-bottom: 1px solid var(--orca-border); }
  #tm td.n, #tm th.n { text-align: right; color: var(--orca-muted); }
  #tm td.a { color: var(--orca-accent); padding: 0 2px; }
  #tm code, #tm td.c { font-family: ui-monospace, Consolas, monospace; }
  #tm .cf { margin-top: 12px; border: 1px solid rgba(210, 153, 34, 0.45); border-radius: 8px; padding: 10px 12px; }
  #tm .cf b { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  #tm .cf .d { width: 12px; height: 12px; border-radius: 50%; border: 1px solid var(--orca-border); flex: none; }
  #tm .note { color: var(--orca-muted); font-size: 12px; line-height: 1.45; margin-top: 10px; }
  #tm .err { color: #d9534f; font-size: 12px; margin-top: 10px; }
  #tm .seg { display: inline-flex; margin-top: 8px; border: 1px solid var(--orca-border); border-radius: 6px; overflow: hidden; }
  #tm .seg button { border: 0; border-right: 1px solid var(--orca-border); border-radius: 0; background: transparent; color: var(--orca-fg); padding: 5px 12px; font-size: 12px; cursor: pointer; }
  #tm .seg button:last-child { border-right: 0; }
  #tm .seg button.on { background: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 700; }
  #tm .bar { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  #tm .bar button { background: transparent; color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 10px; font-size: 12px; font-weight: 600; cursor: pointer; }
  #tm .bar button.ghost { border-color: transparent; font-weight: 400; color: var(--orca-muted); }
  #tm .bar button.pri { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: var(--orca-accent); }
  #tm .bar button.del { border-color: #d9534f; color: #d9534f; }
  #tm .bar button:disabled { opacity: .5; cursor: default; }
  @media (prefers-reduced-motion: reduce) { #tm, #tm .pop { animation: none; } }
"""

MOVE_JS = r"""
  var T = null, st = "", pick = {}, busy = false, err = "", res = null, auto = false;
  var box = document.createElement("div");
  box.id = "tm"; box.hidden = true; document.body.appendChild(box);
  var W = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/></svg>';
  var OK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var pl = function (n, w) { return n + " " + w + (n === 1 ? "" : "s"); };
  var olds = function () { var o = []; (T.fields || []).forEach(function (f) { if (o.indexOf(f.key) < 0) o.push(f.key); }); return o; };
  var codes = function (a) { a = a.map(function (k) { return "<code>" + esc(k) + "</code>"; }); return a.length > 1 ? a.slice(0, -1).join(", ") + " and " + a[a.length - 1] : a.join(""); };
  function note() {
    var n = document.getElementById("pm-tm");
    if (!n) return;
    var on = T && !T.off && (T.n || (T.fields || []).length);
    n.hidden = !on;
    n.innerHTML = !on ? "" : '<div class="tm-warn">' + W + '<span>' + (T.n
      ? pl(T.n, "tuning value") + (T.n === 1 ? " still uses" : " still use") + ' the old field names. Spoolio reads them for now. <a href="#" data-tm="open">Move Values</a>'
      : 'The old tuning fields are still in Spoolman. Spoolio no longer uses them. <a href="#" data-tm="open">Delete Old Fields</a>') + '</span></div>';
  }
  function bar(b) { return '<div class="bar">' + b + '</div>'; }
  function draw() {
    if (!st) { box.hidden = true; box.innerHTML = ""; return; }
    var h = "", e = err ? '<div class="err">' + esc(err) + '</div>' : "", dis = busy ? " disabled" : "";
    if (st === "ask") {
      h = '<b class="t">Move Tuning Values to OrcaSlicer Names</b><div class="tm-warn">' + W + '<span>Advanced Tuning now uses OrcaSlicer\'s own setting names in Spoolman, so Map Matching and tuning share one field. ' +
        pl(T.n, "value") + (T.n === 1 ? " is" : " are") + ' still under the old names.</span></div>' +
        '<table><tr><th>Old Field</th><th></th><th>OrcaSlicer Name</th><th class="n">Used By</th></tr>' + T.rows.map(function (r) {
          var u = [r.fil ? pl(r.fil, "filament") : "", r.spool ? pl(r.spool, "spool") : ""].filter(Boolean).join(", ");
          return '<tr><td class="c">' + esc(r.old) + '</td><td class="a">&rarr;</td><td class="c">' + esc(r["new"]) + '</td><td class="n">' + u + '</td></tr>';
        }).join("") + '</table>' + T.clash.map(function (x) {
          var p = pick[x.id] || "old";
          return '<div class="cf"><b><span class="d" style="background:#' + esc(x.c || "555555") + '"></span>' + esc(x.name) + ' has both</b><div class="note" style="margin:0"><code>' + esc(x.old) + '</code> is ' + esc(x.ov) +
            ' (calibrated in Spoolio). <code>' + esc(x["new"]) + '</code> is ' + esc(x.nv) + ' (set in Spoolman). Which ' + esc(x.label) + ' should Spoolio keep?</div><div class="seg">' +
            '<button data-tp="' + esc(x.id) + '" data-v="old" class="' + (p === "old" ? "on" : "") + '">Keep ' + esc(x.ov) + '</button>' +
            '<button data-tp="' + esc(x.id) + '" data-v="new" class="' + (p === "new" ? "on" : "") + '">Keep ' + esc(x.nv) + '</button></div></div>';
        }).join("") + '<div class="note">Moving copies each value into the new field. Your old fields stay in Spoolman, untouched, until you delete them. Until you move them, Spoolio keeps reading the old fields, so nothing is lost.</div>' + e +
        bar('<button class="ghost" data-tm="later"' + dis + '>Not Now</button><button class="pri" data-tm="move"' + dis + '>' + (busy ? "Moving…" : "Move Values") + '</button>');
    } else if (st === "moved") {
      var f = olds();
      h = '<b class="t">Tuning Values Moved</b><div class="ok">' + OK + '<span>' + pl(res.n || 0, "value") + ' copied to the OrcaSlicer names. Spoolio and Map Matching now read the same fields.</span></div>' +
        (f.length ? '<div class="note">The old fields ' + codes(f) + ' are still in Spoolman. Spoolio no longer uses them, so you can delete them now, or later from Settings.</div>' : "") + e +
        bar(f.length ? '<button class="ghost" data-tm="close">Keep Old Fields</button><button class="del" data-tm="ask-del">Delete Old Fields</button>' : '<button class="pri" data-tm="close">Done</button>');
    } else if (st === "del") {
      h = '<b class="t">Delete Old Fields?</b><div class="tm-warn">' + W + '<span>This deletes ' + codes(olds()) + ' from Spoolman, along with their values on every filament and spool. It can\'t be undone.</span></div>' +
        '<div class="note">Your tuning is already in the OrcaSlicer-named fields, so Spoolio won\'t lose anything.</div>' + e +
        bar('<button class="ghost" data-tm="close"' + dis + '>Cancel</button><button class="del" data-tm="delete"' + dis + '>' + (busy ? "Deleting…" : "Delete Fields") + '</button>');
    } else if (st === "gone") {
      h = '<b class="t">Old Fields Deleted</b><div class="ok">' + OK + '<span>Removed ' + pl(res.n || 0, "field") + ' from Spoolman.</span></div>' + bar('<button class="pri" data-tm="close">Done</button>');
    }
    box.innerHTML = '<div class="pop" role="dialog" aria-modal="true">' + h + '</div>';
    box.hidden = false;
  }
  function open(s) { st = s; err = ""; busy = false; draw(); }
  function shut() {
    if (st === "ask" && !busy) orca.postMessage({ type: "tm_later" });
    if (!busy) { st = ""; draw(); }
  }
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest ? ev.target.closest("[data-tm],[data-tp]") : null;
    if (ev.target === box) { shut(); return; }
    if (!t) return;
    if (t.dataset.tp) { pick[t.dataset.tp] = t.dataset.v; draw(); return; }
    var a = t.dataset.tm;
    if (a === "open") { ev.preventDefault(); if (T) open(T.n ? "ask" : "del"); }
    else if (a === "later" || a === "close") shut();
    else if (a === "move") { busy = true; err = ""; draw(); orca.postMessage({ type: "tm_apply", pick: pick }); }
    else if (a === "ask-del") open("del");
    else if (a === "delete") { busy = true; err = ""; draw(); orca.postMessage({ type: "tm_delete" }); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && st) shut(); });
  orca.onMessage(function (m) {
    if (!m) return;
    if (m.type === "spools" && m.ok && !auto) { auto = true; orca.postMessage({ type: "tm_scan", auto: true }); }
    else if (m.type === "show_view" && m.view === "settings") orca.postMessage({ type: "tm_scan" });
    else if (m.type === "tm") { T = m; note(); if (m.auto && m.n && !st) open("ask"); }
    else if (m.type === "tm_moved" && st) {
      busy = false;
      if (!m.ok) { err = m.error || "Spoolman didn't accept it."; draw(); return; }
      res = m; T.n = m.left || 0; T.fields = m.fields || []; T.rows = []; T.clash = []; note(); st = "moved"; draw();
    } else if (m.type === "tm_deleted" && st) {
      busy = false;
      if (!m.ok) { err = m.error || "Spoolman didn't accept it."; draw(); return; }
      res = m; T.fields = []; note(); st = "gone"; draw();
    }
  });
"""


EMOJI = '"Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji"'
LOOK_FONTS = [
    ("def", "Default", "Spoolio's current font", "Roboto · Segoe UI · San Francisco · Noto Sans", FONT_STACK),
    ("sans", "Classic Sans", "Plain and familiar", "Arial · Helvetica · Liberation Sans",
     f'Arial, Helvetica, "Liberation Sans", "Nimbus Sans", {EMOJI}, sans-serif'),
    ("wide", "Easy Read", "Wider letters, made for screens", "Verdana · DejaVu Sans",
     f'Verdana, "DejaVu Sans", "Bitstream Vera Sans", {EMOJI}, sans-serif'),
    ("serif", "Serif", "Book-style letters", "Georgia · Times New Roman · DejaVu Serif",
     f'Georgia, "Times New Roman", "DejaVu Serif", "Liberation Serif", {EMOJI}, serif'),
    ("mono", "Monospace", "Every letter the same width", "Consolas · Menlo · DejaVu Sans Mono",
     f'Consolas, Menlo, "DejaVu Sans Mono", "Liberation Mono", {EMOJI}, monospace'),
]
LOOK_SIZES = [(90, "Small"), (100, "Default"), (110, "Large"), (120, "Extra Large")]

LOOK_CSS = """
  html.apf * { font-family: var(--apf) !important; }
  #view-settings .ap-pane { display: none; }
  #view-settings.ap-on > .srow { display: none; }
  #view-settings.ap-on > .ap-pane { display: block; }
  #view-settings .fo-list { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
  #view-settings .fo { display: flex; align-items: center; gap: 14px; padding: 10px 14px; border: 1px solid var(--orca-border); border-radius: 10px; cursor: pointer; background: color-mix(in srgb, var(--orca-fg) 2%, var(--orca-bg) 98%); }
  #view-settings .fo:hover { border-color: color-mix(in srgb, var(--orca-accent) 50%, var(--orca-border) 50%); }
  #view-settings .fo.on { border-color: var(--orca-accent); background: color-mix(in srgb, var(--orca-accent) 12%, var(--orca-bg) 88%); box-shadow: 0 0 0 1px var(--orca-accent); }
  #view-settings .fo-aa { width: 44px; text-align: center; font-size: 24px; font-weight: 700; flex: none; }
  #view-settings .fo-t { min-width: 0; flex: 1; }
  #view-settings .fo-n { font-weight: 700; font-size: 14px; }
  #view-settings .fo-s { font-size: 12px; color: var(--orca-muted); margin-top: 1px; }
  #view-settings .fo-w { font-size: 11px; color: var(--orca-muted); margin-top: 3px; opacity: .85; }
  #view-settings .fo-ck { width: 18px; height: 18px; border-radius: 50%; border: 2px solid var(--orca-border); flex: none; box-sizing: border-box; }
  #view-settings .fo.on .fo-ck { border: 5px solid var(--orca-accent); background: var(--orca-accent-fg); }
  #view-settings .ap-prev { padding: 14px 16px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); }
  #view-settings .ap-prev + .ap-prev, #view-settings .ws-card + .ap-prev { margin-top: 10px; }
  #view-settings .ap-h { font-size: 15px; font-weight: 700; }
  #view-settings .ap-nums { display: flex; justify-content: space-between; font-size: 13px; color: var(--orca-muted); }
  #view-settings .ap-nums b { color: var(--orca-fg); font-size: 18px; }
  #view-settings .ap-bar { height: 6px; border-radius: 3px; background: var(--orca-border); margin: 8px 0 6px; overflow: hidden; }
  #view-settings .ap-bar i { display: block; height: 100%; width: 37%; background: var(--orca-accent); }
  #view-settings .ap-note { display: flex; gap: 8px; align-items: flex-start; margin-top: 12px; padding: 9px 12px; border-radius: 8px; font-size: 12px; color: var(--orca-muted); background: color-mix(in srgb, var(--orca-accent) 9%, var(--orca-bg) 91%); }
  #view-settings .ap-note b { color: var(--orca-fg); }
  #view-settings .ap-reset { background: none; border: 0; padding: 0; color: var(--orca-accent); cursor: pointer; text-decoration: underline; font-size: 12px; }
  #view-settings .sz-row { display: flex; align-items: flex-end; gap: 18px; margin-top: 4px; }
  #view-settings .sz-row span { color: var(--orca-muted); font-size: 12px; text-align: center; }
  #view-settings .sz-row span.on { color: var(--orca-accent); font-weight: 700; }
  #view-settings .sz-row span i { display: block; font-style: normal; font-weight: 700; color: var(--orca-fg); line-height: 1.1; margin-bottom: 4px; }
  #view-settings .sz-row span.on i { color: var(--orca-accent); }
  :root { --sp-card: color-mix(in srgb, var(--orca-fg) 5%, var(--orca-bg) 95%); --sp-rad: 14px; --sp-sw: 42px; }
  html.th body { background: var(--orca-bg); color: var(--orca-fg); }
  html.th #view-main .spool-card, html.th #view-settings .srow, html.th #view-settings .ws-card, html.th #view-settings .ap-prev, html.th #view-settings .preview, html.th #view-main .group-card, html.th #view-main .pp-card, html.th #view-main .st-card { background: var(--sp-card) !important; }
  html.lt #view-main .spool-swatch, html.lt .tp-sw { border-color: rgba(0,0,0,.14) !important; }
  html.lt #view-main .spool-tag { box-shadow: 0 1px 2px rgba(0,0,0,.18); }
  #view-main .spool-card { border-radius: var(--sp-rad); }
  #view-settings .srow { border-radius: min(var(--sp-rad), 14px); }
  #view-main #list .spool-swatch { width: var(--sp-sw); height: var(--sp-sw); }
  html.dn-c #view-main .spool-card { padding: 9px 11px 7px; margin-bottom: 6px; }
  html.dn-c #view-main .spool-title { font-size: 13px; }
  html.dn-c #view-main .spool-bar-track { margin: 4px 0 3px; height: 4px; }
  html.dn-c #view-main .spool-body { gap: 9px; }
  html.dn-c #view-main #list.cols-2 .spool-card { height: calc(100% - 6px); }
  html.dn-s #view-main .spool-card { padding: 18px 18px 15px; margin-bottom: 14px; }
  html.dn-s #view-main .spool-title { font-size: 15px; }
  html.dn-s #view-main .spool-bar-track { margin: 9px 0 7px; height: 7px; }
  html.dn-s #view-main .spool-body { gap: 14px; }
  html.dn-s #view-main #list.cols-2 .spool-card { height: calc(100% - 14px); }
  .tp-sw.spool { background-image: radial-gradient(circle, var(--sp-card) 0 19%, rgba(0,0,0,.4) 20% 25%, rgba(255,255,255,.12) 26% 30%, transparent 31% 60%, rgba(0,0,0,.16) 61% 64%, transparent 65%) !important; }
  html.sw-spool #view-main .spool-swatch:not(.ams-dash), html.sw-spool #pc .spool-swatch { position: relative; }
  html.sw-spool #view-main .spool-swatch:not(.ams-dash)::after, html.sw-spool #pc .spool-swatch::after { content: ""; position: absolute; inset: 0; border-radius: 50%; background: radial-gradient(circle, var(--sp-card) 0 19%, rgba(0,0,0,.4) 20% 25%, rgba(255,255,255,.12) 26% 30%, transparent 31% 60%, rgba(0,0,0,.16) 61% 64%, transparent 65%); }
  html.sw-square #view-main .spool-swatch, html.sw-square #pc .spool-swatch, .tp-sw.square { border-radius: 22% !important; }
  html.sw-pill #view-main .spool-swatch, html.sw-pill #pc .spool-swatch, .tp-sw.pill { width: 10px !important; border-radius: 5px !important; margin: 0 13px; }
  html.hd-tag #view-main #list .spool-tag, html.hd-rfid #view-main #list .spool-rfid, html.hd-cart #view-main #list .spool-cart { display: none !important; }
  .pg-r { animation: pg-r .34s cubic-bezier(.2, .7, .2, 1) backwards; }
  .pg-l { animation: pg-l .34s cubic-bezier(.2, .7, .2, 1) backwards; }
  .pg-r > .pg-s, .pg-l > .pg-s { animation: pg-up .38s cubic-bezier(.2, .7, .2, 1) backwards; animation-delay: calc(var(--i, 0) * 35ms + 60ms); }
  @keyframes pg-r { from { opacity: 0; transform: translateX(18px); } }
  @keyframes pg-l { from { opacity: 0; transform: translateX(-18px); } }
  @keyframes pg-up { from { opacity: 0; transform: translateY(8px); } }
  .tabs.glide { position: relative; }
  .tabs.glide .ind { position: absolute; top: 0; bottom: 0; left: 0; width: 0; z-index: 0; background: var(--orca-accent); pointer-events: none; }
  .tabs.glide-on .ind { transition: left .32s cubic-bezier(.3, .7, .2, 1), width .32s cubic-bezier(.3, .7, .2, 1); }
  .tabs.glide button { position: relative; z-index: 1; transition: color .2s; }
  #app-header .tabs.glide button.on, #app-header .tabs.glide button.on:hover, #app-header .tabs.glide button.on:focus,
  #app-header .tabs.glide button.on:active, .tabs.glide button.on { background: transparent !important; }
  @media (prefers-reduced-motion: reduce) { .pg-r, .pg-l, .pg-s { animation: none !important; } .tabs.glide .ind { transition: none !important; } }
  html.rm *, html.rm *::before, html.rm *::after { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
  html.cb #view-main .spool-weight.low { color: #e69f00 !important; }
  html.cb #view-main .spool-weight.low::before { content: "▼"; font-size: 9px; margin-right: -2px; }
  html.cb #view-main .live-dot, html.cb .tp-ok i { background: #56b4e9 !important; }
  html.cb #view-main .plate-pill.fine, html.cb .tp-chip.ok { background: rgba(86,180,233,.2) !important; color: #56b4e9 !important; }
  html.cb #view-main .group-card .group-bar-fill.low { background: #d55e00 !important; }
  html.cb .tp-chip.bad { background: rgba(213,94,0,.2) !important; color: #e8792b !important; }
  html.cb .tp-chip.warn { background: rgba(230,159,0,.2) !important; color: #e69f00 !important; }
  html.cb .tp-chip.bad::before { content: "▼ "; } html.cb .tp-chip.warn::before { content: "▲ "; } html.cb .tp-chip.ok::before { content: "● "; }
  #view-settings > .srow.moved { display: none !important; }
  #view-settings .th-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 12px 18px 6px; }
  #view-settings .th { cursor: pointer; border: 1px solid var(--orca-border); border-radius: 12px; padding: 8px; background: var(--orca-bg); }
  #view-settings .th:hover { border-color: color-mix(in srgb, var(--orca-accent) 55%, var(--orca-border)); }
  #view-settings .th.on { border-color: var(--orca-accent); box-shadow: 0 0 0 1px var(--orca-accent); }
  #view-settings .th-m { height: 92px; border-radius: 8px; overflow: hidden; position: relative; border: 1px solid rgba(127,127,127,.25); }
  #view-settings .th-m .bar { height: 14px; display: flex; gap: 4px; align-items: center; padding: 0 6px; }
  #view-settings .th-m .bar i { width: 18px; height: 6px; border-radius: 3px; }
  #view-settings .th-m .cd { margin: 6px 8px 0; height: 24px; border-radius: 6px; display: flex; align-items: center; gap: 6px; padding: 0 6px; border: 1px solid; }
  #view-settings .th-m .cd b { width: 12px; height: 12px; border-radius: 50%; flex: none; }
  #view-settings .th-m .cd s { flex: 1; height: 4px; border-radius: 2px; text-decoration: none; }
  #view-settings .th-f { display: flex; align-items: center; gap: 8px; margin-top: 8px; padding: 0 2px; }
  #view-settings .th-n { font-weight: 700; font-size: 13px; flex: 1; }
  #view-settings .th-s { font-size: 11px; color: var(--orca-muted); padding: 2px; min-height: 28px; }
  #view-settings .th .fo-ck { width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--orca-border); box-sizing: border-box; flex: none; }
  #view-settings .th.on .fo-ck { border: 5px solid var(--orca-accent); background: var(--orca-accent-fg); }
  #view-settings .th-pane .hint { padding: 0 18px 16px; margin: 0; }
  #view-settings .tb-from { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 14px; }
  #view-settings .tb-from button { padding: 5px 10px 5px 6px; border: 1px solid var(--orca-border); border-radius: 99px; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; font-weight: 600; background: transparent; color: var(--orca-fg); cursor: pointer; }
  #view-settings .tb-from button:hover { border-color: var(--orca-accent); }
  #view-settings .tb-from button i { width: 14px; height: 14px; border-radius: 50%; display: inline-block; border: 3px solid; box-sizing: border-box; }
  #view-settings .tb-cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px 14px; }
  #view-settings .tb-c { display: flex; align-items: center; gap: 10px; padding: 7px 8px; border: 1px solid var(--orca-border); border-radius: 9px; background: var(--orca-bg); min-width: 0; margin: 0; }
  #view-settings .tb-c input[type=color] { width: 34px; height: 34px; padding: 0; border: 1px solid var(--orca-border); border-radius: 8px; background: none; cursor: pointer; flex: none; }
  #view-settings .tb-c input[type=color]::-webkit-color-swatch-wrapper { padding: 2px; }
  #view-settings .tb-c input[type=color]::-webkit-color-swatch { border: 0; border-radius: 6px; }
  #view-settings .tb-c div { flex: 1; min-width: 0; }
  #view-settings .tb-c b { display: block; font-size: 12.5px; }
  #view-settings .tb-c span { display: block; font-size: 11px; color: var(--orca-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #view-settings .tb-c input[type=text] { width: 68px; flex: none; padding: 5px 6px; border: 1px solid var(--orca-border); border-radius: 6px; background: transparent; color: var(--orca-fg); font: 12px Consolas, Menlo, "DejaVu Sans Mono", monospace !important; text-transform: uppercase; }
  #view-settings .tb-c input.bad { border-color: #d9534f; }
  #view-settings .tb-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
  #view-settings .tb-row .seg { margin: 0; }
  #view-settings .tb-acts { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
  #view-settings .tb-acts button, #view-settings .tb-paste button { padding: 6px 12px; border: 1px solid var(--orca-border); border-radius: 6px; font-size: 12px; background: transparent; color: var(--orca-fg); cursor: pointer; }
  #view-settings .tb-acts button.rs { color: var(--orca-accent); border-color: var(--orca-accent); }
  #view-settings .tb-paste { display: flex; gap: 8px; margin-top: 10px; }
  #view-settings .tb-paste[hidden] { display: none; }
  #view-settings .tb-paste input { flex: 1; padding: 6px 10px; font-size: 12px; }
  #view-settings .tp { padding: 14px; border-radius: 10px; border: 1px solid var(--orca-border); background: var(--orca-bg); }
  #view-settings .tp-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
  #view-settings .tp-bar b { flex: 1; font-size: 14px; }
  #view-settings .tp-bar span { padding: 5px 11px; border-radius: 6px; font-size: 12px; font-weight: 700; background: var(--orca-accent); color: var(--orca-accent-fg); }
  #view-settings .tp-bar em { padding: 5px 11px; border-radius: 6px; font-size: 12px; font-style: normal; border: 1px solid var(--orca-border); }
  #view-settings .tp-card { position: relative; display: flex; gap: 11px; padding: 13px 13px 10px; border-radius: var(--sp-rad); border: 1px solid var(--orca-border); background: var(--sp-card); margin-top: 10px; }
  #view-settings .tp-tag { position: absolute; top: -8px; left: 13px; padding: 1px 8px; border-radius: 4px 9px 9px 0; font-size: 10px; font-weight: 700; background: var(--orca-accent); color: var(--orca-accent-fg); }
  #view-settings .tp-sw { width: 38px; height: 38px; border-radius: 50%; flex: none; border: 2px solid rgba(255,255,255,.1); box-sizing: border-box; }
  #view-settings .tp-sc { position: relative; flex: none; display: flex; align-items: flex-start; }
  #view-settings .tp-rf { position: absolute; left: 50%; top: 100%; transform: translate(-50%, -60%); font-size: 7.5px; font-weight: 800; padding: 1px 4px; border-radius: 4px; color: #3a3d42; background: linear-gradient(135deg, #f4f5f7, #cdd1d6 35%, #8b9099 55%, #cdd1d6 75%, #f4f5f7); border: 1px solid rgba(0,0,0,.25); }
  #view-settings .tp-cart { font-style: normal; display: inline-flex; border: 1px solid #e0a030; border-radius: 5px; padding: 0 3px; font-size: 10px; margin-left: 4px; vertical-align: 1px; }
  #view-settings .tp-mn { flex: 1; min-width: 0; }
  #view-settings .tp-tp { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
  #view-settings .tp-tp b { font-size: 13.5px; }
  #view-settings .tp-tr { height: 5px; border-radius: 3px; background: rgba(127,127,127,.25); margin: 6px 0 5px; overflow: hidden; }
  #view-settings .tp-tr i { display: block; height: 100%; }
  #view-settings .tp-sb { font-size: 11.5px; font-style: italic; color: var(--orca-muted); }
  #view-settings .tp-ck { margin-top: 12px; display: grid; gap: 6px; }
  #view-settings .tp-ck div { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; padding: 6px 10px; border-radius: 7px; border: 1px solid var(--orca-border); }
  #view-settings .tp-ck b { font-size: 11px; padding: 2px 8px; border-radius: 99px; white-space: nowrap; }
  #view-settings .tp-ck b.ok { background: rgba(63,185,80,.18); color: #3fb950; }
  #view-settings .tp-ck b.no { background: rgba(217,83,79,.18); color: #e5534b; }
  #view-settings .tp-ck b.mid { background: rgba(224,160,48,.2); color: #e0a030; }
  #view-settings .tp-chips { display: flex; flex-wrap: wrap; gap: 6px; }
  #view-settings .tp-chip { padding: 3px 10px; border-radius: 99px; font-size: 12px; font-weight: 700; }
  #view-settings .tp-chip.ok { background: rgba(63,185,80,.18); color: #3fb950; }
  #view-settings .tp-chip.warn { background: rgba(210,153,34,.22); color: #e0a030; }
  #view-settings .tp-chip.bad { background: rgba(217,83,79,.2); color: #e5534b; }
  #view-settings .tp-ok { display: flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 10px; }
  #view-settings .tp-ok i { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; }
  #view-settings .tp-anim { height: 6px; border-radius: 3px; margin-top: 12px; background: linear-gradient(90deg, var(--orca-accent) 0 40%, color-mix(in srgb, var(--orca-accent) 35%, #fff) 50%, var(--orca-accent) 60% 100%); background-size: 250% 100%; animation: tpmove 1.6s linear infinite; }
  @keyframes tpmove { from { background-position: 100% 0; } to { background-position: -150% 0; } }
  #view-settings .ck-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 2px 14px; margin-top: 6px; }
  #view-settings .ck-grid .check-row { margin: 0 0 8px; }
  #view-settings .sw-opts { display: flex; gap: 8px; margin: 6px 0 14px; flex-wrap: wrap; }
  #view-settings .sw-o { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 9px 12px 7px; min-width: 64px; border: 1px solid var(--orca-border); border-radius: 10px; cursor: pointer; font-size: 12px; background: var(--orca-bg); }
  #view-settings .sw-o.on { border-color: var(--orca-accent); box-shadow: 0 0 0 1px var(--orca-accent); color: var(--orca-accent); font-weight: 700; }
  #view-settings .sw-o .tp-sw { width: 30px; height: 30px; margin: 0; }
  #view-settings .sw-o .tp-sw.pill { width: 10px !important; }
  #th-toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); z-index: 1001; padding: 9px 16px; border-radius: 8px; background: var(--orca-accent); color: var(--orca-accent-fg); font-weight: 700; font-size: 13px; display: none; }
  #th-toast.on { display: block; }
  @media (max-width: 900px) { #view-settings .th-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } #view-settings .tb-cols { grid-template-columns: minmax(0, 1fr); } }
"""


def look_html() -> str:
    fo = "".join(
        f'<div class="fo" data-f="{k}"><div class="fo-aa" style=\'font-family:{css} !important\'>Aa</div>'
        f'<div class="fo-t"><div class="fo-n" style=\'font-family:{css} !important\'>{n}</div>'
        f'<div class="fo-s" style=\'font-family:{css} !important\'>{html.escape(s)}</div>'
        f'<div class="fo-w">{w}</div></div><div class="fo-ck"></div></div>'
        for k, n, s, w, css in LOOK_FONTS)
    seg = "".join(f'<label><input type="radio" name="ap-size" value="{v}">{n}</label>' for v, n in LOOK_SIZES)
    row = "".join(f'<span data-z="{v}"><i style="font-size:{round(13 * v / 100)}px">Aa</i>{v}%</span>' for v, _ in LOOK_SIZES)
    return (
        '<div class="srow ap-pane"><h3><span class="emoji">🔤</span>Configure Font</h3><div class="srow-body"><div class="srow-l">'
        f'<div class="field-label">Choose the Font for the Spoolio Tab:</div><div class="fo-list" id="fo-list">{fo}</div>'
        '<div class="hint">Each option only uses fonts that come with Windows, macOS and Linux, so nothing is downloaded. '
        'If one is missing, the next in its list is used.</div>'
        '</div><div class="srow-r"><div class="label">Preview</div>'
        '<div class="ap-prev"><div class="ap-h">Desk_Organiser_Plate_1</div><div class="ap-bar"><i></i></div>'
        '<div class="ap-nums"><span>Layer 142 / 380 · 2 h 04 min left</span><b>37%</b></div></div>'
        '<div class="ws-card" style="margin-top:10px"><div class="ws-swatch" style="background:#2850a8"></div><div class="ws-main">'
        '<div class="ws-top"><span class="ws-name">PETG HF - Cobalt Blue</span><span>77% · 770 g</span></div>'
        '<div class="ws-bar"><i style="width:77%;background:#2850a8"></i></div><div class="ws-sub">PETG · #2850A8 · 1.75mm</div></div></div>'
        '<div class="ap-prev">The quick brown fox jumps over the lazy dog. 0123456789 g · €24.10</div>'
        '<div class="ap-note"><span>✨</span><span><b>Live Preview.</b> The whole tab changes as you pick. '
        'Cancel puts it back, Save &amp; Close keeps it.</span></div>'
        '</div></div></div>'
        '<div class="srow ap-pane"><h3><span class="emoji">🔎</span>Configure Text Size</h3><div class="srow-body"><div class="srow-l">'
        f'<div class="field-label">Show Text and Controls At:</div><div class="seg" id="ap-seg">{seg}</div>'
        '<div class="hint" style="margin-top:14px"><button class="ap-reset" id="ap-reset">Reset Font and Text Size to Default</button></div>'
        f'</div><div class="srow-r"><div class="label">Preview</div><div class="ap-prev"><div class="sz-row" id="sz-row">{row}</div></div>'
        '<div class="hint" style="margin-top:10px">Only the Spoolio tab changes. OrcaSlicer\'s own windows and menus keep their size.</div>'
        '</div></div></div>'
    )


# Font and text size: applied live, put back on Cancel, kept on Save & Close.
LOOK_JS = r"""
  var F = __FONTS__, P = __THEMES__, saved = __LOOK__, KEYS = ["accent", "accentFg", "bg", "card", "border", "fg", "muted"];
  var C = [["accent", "Accent", "Buttons and tabs"], ["accentFg", "Text on Accent", "On accent buttons"], ["bg", "Background", "Behind everything"],
    ["card", "Cards", "Spools and panels"], ["border", "Borders", "Edges and lines"], ["fg", "Text", "Main text"], ["muted", "Muted Text", "Helper text"]];
  var cp = function (o) { return JSON.parse(JSON.stringify(o)); }, cur = cp(saved);
  var DEF = { font: "def", size: 100, theme: "dark", custom: cp(P.dark), density: "comfy", cols: saved.cols, swatch: "circle",
    show: { tag: true, mat: true, hex: true, dia: true, loc: false, rfid: true, cart: true }, units: "g", wfmt: "both", motion: false, cb: false };
  var $ = function (s) { return document.querySelector(s); }, $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };
  var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); };
  function hx(h) { h = h.replace("#", ""); return [0, 2, 4].map(function (i) { return parseInt(h.substr(i, 2), 16); }); }
  function lum(h) { var c = hx(h).map(function (v) { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }); return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]; }
  function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); }
  var pal = function (a) { return a.theme === "custom" ? a.custom : P[a.theme]; };
  window.__spoolioAp = function () { return cur; };

  function apply(a) {
    var r = document.documentElement, p = pal(a), map = { accent: "accent", accentFg: "accent-fg", bg: "bg", border: "border", fg: "fg", muted: "muted" };
    Object.keys(map).forEach(function (k) { r.style.setProperty("--orca-" + map[k], p[k]); });
    r.style.setProperty("--sp-card", p.card);
    r.style.setProperty("--sp-rad", p.radius + "px");
    r.style.background = p.bg; r.style.color = p.fg;
    r.classList.add("th");
    r.classList.toggle("lt", lum(p.bg) > .4);
    r.classList.toggle("dn-c", a.density === "compact"); r.classList.toggle("dn-s", a.density === "spacious");
    r.style.setProperty("--sp-sw", { compact: "32px", comfy: "42px", spacious: "52px" }[a.density]);
    ["circle", "spool", "square", "pill"].forEach(function (s) { r.classList.toggle("sw-" + s, a.swatch === s); });
    r.classList.toggle("hd-tag", !a.show.tag); r.classList.toggle("hd-rfid", !a.show.rfid); r.classList.toggle("hd-cart", !a.show.cart);
    r.classList.toggle("rm", a.motion); r.classList.toggle("cb", a.cb);
    var css = F[a.font] || "";
    r.style.setProperty("--apf", css);
    r.classList.toggle("apf", !!css && a.font !== "def");
    r.style.zoom = a.size === 100 ? "" : a.size / 100;
    if (window.__spoolioRelook) window.__spoolioRelook(a);
  }

  function wt(g) {
    if (cur.units === "kg") return (g / 1000).toFixed(g < 100 ? 3 : 2) + " kg";
    return Math.round(g).toLocaleString("en-GB") + " g";
  }
  var sw = function (cls, c) { return '<div class="tp-sw ' + cls + '" style="background-color:' + c + '"></div>'; };
  function card(nm, br, col, g, pc, sub, low) {
    var a = cur, ws = a.wfmt === "pct" ? pc + "%" : (a.wfmt === "both" ? '<span style="color:var(--orca-muted);font-size:12px">' + pc + '% · </span>' : "") + wt(g);
    return '<div class="tp-card">' + (a.show.tag ? '<span class="tp-tag">' + br + '</span>' : "") + '<div class="tp-sc">' + sw(a.swatch, col) + (a.show.rfid ? '<span class="tp-rf">RFID</span>' : "") + '</div>' +
      '<div class="tp-mn"><div class="tp-tp"><b>' + nm + '</b><span' + (low ? ' style="color:' + (a.cb ? "#e69f00" : "#da5f4a") + '"' : "") + '>' + (low && a.cb ? "▼ " : "") + ws +
      (low && a.show.cart ? ' <i class="tp-cart">🛒</i>' : "") + '</span></div>' +
      '<div class="tp-tr"><i style="width:' + pc + '%;background:' + col + '"></i></div><div class="tp-sb">' + sub + '</div></div></div>';
  }
  function sub(m, h, d, l) { var a = cur.show; return [a.mat && m, a.hex && h, a.dia && d, a.loc && "📍 " + l].filter(Boolean).join(" · "); }
  function thumb(q) {
    return '<div class="th-m"><div style="background:' + q.bg + ';position:absolute;inset:0"><div class="bar" style="background:' + q.card + ';border-bottom:1px solid ' + q.border + '"><i style="background:' + q.accent + '"></i><i style="background:' + q.border + '"></i></div>' +
      ["#c12e1f", "#2850a8"].map(function (c, i) {
        return '<div class="cd" style="background:' + q.card + ';border-color:' + q.border + '"><b style="background:' + c + '"></b><s style="background:' + q.fg + ';opacity:.7;max-width:' + (i ? 50 : 70) + '%"></s><s style="background:' + q.accent + ';max-width:18%"></s></div>';
      }).join("") + '</div></div>';
  }
  function themes() {
    $("#th-grid").innerHTML = [["light", "Light", "Bright and clean"], ["dark", "Dark", "Easy on the eyes"], ["custom", "Custom", "Your own colours"]].map(function (t) {
      return '<div class="th" data-th="' + t[0] + '">' + thumb(t[0] === "custom" ? cur.custom : P[t[0]]) + '<div class="th-f"><span class="th-n">' + t[1] + '</span><span class="fo-ck"></span></div><div class="th-s">' + t[2] + '</div></div>';
    }).join("");
  }
  function pane() {
    var from = ["light", "dark"].map(function (k) {
      return '<button data-from="' + k + '"><i style="background:' + P[k].bg + ';border-color:' + P[k].accent + '"></i>' + (k === "light" ? "Light" : "Dark") + '</button>';
    }).join("");
    var cc = C.map(function (c) {
      return '<label class="tb-c"><input type="color" data-c="' + c[0] + '"><div><b>' + c[1] + '</b><span>' + c[2] + '</span></div><input type="text" maxlength="7" spellcheck="false" data-hx="' + c[0] + '"></label>';
    }).join("");
    var seg = function (n, o) { return '<div class="seg" data-seg="' + n + '">' + o.map(function (x) { return '<label><input type="radio" name="th-' + n + '" value="' + x[0] + '">' + x[1] + '</label>'; }).join("") + '</div>'; };
    var ck = function (k, t) { return '<label class="check-row"><input type="checkbox" data-show="' + k + '">' + t + '</label>'; };
    var h =
      '<div class="srow ap-pane th-pane"><h3><span class="emoji">🎨</span>Choose a Theme</h3><div class="th-grid" id="th-grid"></div>' +
        '</div>' +
      '<div class="srow ap-pane" id="tb"><h3><span class="emoji">🖌️</span>Build Your Theme</h3><div class="srow-body"><div class="srow-l">' +
        '<div class="field-label">Start From:</div><div class="tb-from">' + from + '</div>' +
        '<div class="tb-cols">' + cc + '</div>' +
        '<div class="tb-row"><span class="field-label" style="margin:0">Card Corners:</span>' + seg("rad", [[4, "Sharp"], [10, "Soft"], [14, "Rounded"], [20, "Round"]]) + '</div>' +
        '<div class="tb-acts"><button class="rs" id="tb-reset">↺ Reset Custom Theme</button><button id="tb-copy">Copy Theme Code</button><button id="tb-paste">Paste Theme Code</button></div>' +
        '<div class="tb-paste" id="tb-pbox" hidden><input type="text" id="tb-code" spellcheck="false" placeholder="Paste a Theme Code"><button id="tb-apply">Apply</button></div>' +
        '<div class="hint">Theme codes let you share a theme with other Spoolio users.</div>' +
      '</div><div class="srow-r"><div class="label">Preview</div><div class="tp" id="tp"></div>' +
        '<div class="label" style="margin-top:14px">Readability</div><div class="tp-ck" id="tp-ck"></div></div></div></div>';
    var h2 =
      '<div class="srow ap-pane"><h3><span class="emoji">📐</span>Configure Layout</h3><div class="srow-body"><div class="srow-l">' +
        '<div class="field-label">Spacing:</div>' + seg("density", [["compact", "Compact"], ["comfy", "Comfortable"], ["spacious", "Spacious"]]) +
        '<div class="hint" style="margin:-6px 0 14px">Compact fits more spools on screen.</div>' +
        '<div class="field-label">Spool List Columns:</div>' + seg("cols", [["1", "One"], ["2", "Two"], ["auto", "Auto"]]) +
        '<div class="hint">Auto uses two columns when the window is wide enough.</div>' +
      '</div><div class="srow-r"><div class="label">Preview</div><div id="dn-prev"></div></div></div></div>' +
      '<div class="srow ap-pane"><h3><span class="emoji">🧵</span>Configure Spool Cards</h3><div class="srow-body"><div class="srow-l">' +
        '<div class="field-label">Colour Swatch:</div><div class="sw-opts">' +
          [["circle", "Circle"], ["spool", "Spool"], ["square", "Square"], ["pill", "Pill"]].map(function (s) { return '<div class="sw-o" data-sw="' + s[0] + '">' + sw(s[0], "#2850a8") + s[1] + '</div>'; }).join("") + '</div>' +
        '<div class="field-label">Show on Each Card:</div><div class="ck-grid">' +
          ck("tag", "Manufacturer Tag") + ck("mat", "Material") + ck("hex", "Colour Code") + ck("dia", "Diameter") + ck("loc", "Location") + ck("rfid", "RFID Chip") + ck("cart", "Cart Button") + '</div>' +
        '<div class="hint" style="margin-top:4px">The cart button only shows on low spools.</div>' +
      '</div><div class="srow-r"><div class="label">Preview</div><div id="cd-prev"></div></div></div></div>' +
      '<div class="srow ap-pane"><h3><span class="emoji">⚖️</span>Configure Weights</h3><div class="srow-body"><div class="srow-l">' +
        '<div class="field-label">Units:</div>' + seg("units", [["g", "Grams"], ["kg", "Kilograms"]]) +
        '<div class="field-label">Show Remaining As:</div>' + seg("wfmt", [["w", "Weight"], ["pct", "Percent"], ["both", "Both"]]) +
        '<div class="hint">Spoolman still stores grams. Only the display changes.</div>' +
      '</div><div class="srow-r"><div class="label">Preview</div><div class="tp" id="wt-prev"></div></div></div></div>' +
      '<div class="srow ap-pane"><h3><span class="emoji">♿</span>Configure Accessibility</h3><div class="srow-body"><div class="srow-l">' +
        '<label class="check-row"><input type="checkbox" id="ac-motion">Reduce Motion</label><div class="hint" style="margin:-6px 0 14px 24px">Stops moving bars, fades and slides.</div>' +
        '<label class="check-row"><input type="checkbox" id="ac-cb">Colour-Blind Friendly Status Colours</label><div class="hint" style="margin:-6px 0 14px 24px">Uses blue and orange, plus a shape on each status.</div>' +
        '<div class="hint ap-all"><button class="ap-reset" id="ap-all">Reset All Appearance Settings</button></div>' +
      '</div><div class="srow-r"><div class="label">Preview</div><div class="tp"><div class="tp-chips"><span class="tp-chip ok">Enough Filament</span><span class="tp-chip warn">Running Short</span><span class="tp-chip bad">Low Stock</span></div>' +
        '<div class="tp-ok"><i></i>Live Link Connected</div><div class="tp-anim"></div></div></div></div></div>';
    var vs = $("#view-settings"), fp = $$("#view-settings > .ap-pane")[0], ft = $("#view-settings > .footer");
    var d = document.createElement("div");
    d.innerHTML = h; while (d.firstChild) vs.insertBefore(d.firstChild, fp);
    d.innerHTML = h2; while (d.firstChild) vs.insertBefore(d.firstChild, ft);
    document.body.insertAdjacentHTML("beforeend", '<div id="th-toast"></div>');
  }
  function toast(s) { var t = $("#th-toast"); t.textContent = s; t.classList.add("on"); clearTimeout(t._t); t._t = setTimeout(function () { t.classList.remove("on"); }, 2600); }
  function code() { return "SPOOLIO-THEME:" + btoa(JSON.stringify(cur.custom)); }
  function copy() {
    var c = code(), done = function () { toast("✓ Theme Code Copied"); };
    var old = function () {
      var t = document.createElement("textarea"); t.value = c; document.body.appendChild(t); t.select();
      try { document.execCommand("copy"); done(); } catch (e) { toast("Couldn't Copy. Select the Code in the Box Instead."); }
      t.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(c).then(done, old); else old();
  }
  function paste(t) {
    try {
      var o = JSON.parse(atob(String(t).trim().replace(/^SPOOLIO-THEME:/i, "")));
      KEYS.forEach(function (k) { if (!/^#[0-9a-f]{6}$/i.test(o[k])) throw 0; });
      cur.custom = { radius: [4, 10, 14, 20].indexOf(o.radius) >= 0 ? o.radius : 14 };
      KEYS.forEach(function (k) { cur.custom[k] = o[k].toLowerCase(); });
      cur.theme = "custom"; $("#tb-pbox").hidden = true; sync(); toast("✓ Theme Applied");
    } catch (e) { toast("That Isn't a Spoolio Theme Code."); }
  }
  function prev() {
    var p = pal(cur);
    $("#tp").innerHTML = '<div class="tp-bar"><b>Spools</b><em>Filters ▾</em><span>+ Add Spool</span></div>' +
      card("PETG HF - Cobalt Blue", "Bambu Lab", "#2850a8", 770, 77, sub("PETG", "#2850A8", "1.75mm", "AMS 1")) +
      card("PLA Basic - Jade White", "Bambu Lab", "#f2f2ec", 32, 3, sub("PLA", "#F2F2EC", "1.75mm", "Shelf"), 1);
    var r = function (n, a, b) { var v = ratio(a, b); return '<div><span>' + n + '</span><b class="' + (v >= 4.5 ? "ok" : v >= 3 ? "mid" : "no") + '">' + v.toFixed(1) + ' : 1 · ' + (v >= 4.5 ? "Easy to Read" : v >= 3 ? "Hard to Read" : "Too Faint") + '</b></div>'; };
    $("#tp-ck").innerHTML = r("Text on Cards", p.fg, p.card) + r("Helper Text", p.muted, p.card) + r("Accent Buttons", p.accentFg, p.accent);
    var pad = { compact: "7px 10px 5px", comfy: "12px 13px 9px", spacious: "17px 16px 14px" }[cur.density];
    $("#dn-prev").innerHTML = '<div class="tp" style="padding:4px 12px 10px">' + [["PLA Matte - Black", "#111111", 640, 64], ["PLA Basic - Red", "#c12e1f", 530, 53], ["PETG HF - Cobalt Blue", "#2850a8", 770, 77]].map(function (s) {
      return card(s[0], "Bambu Lab", s[1], s[2], s[3], sub("PLA", s[1].toUpperCase(), "1.75mm", "AMS 1")).replace('class="tp-card"', 'class="tp-card" style="padding:' + pad + '"');
    }).join("") + '</div>';
    $("#cd-prev").innerHTML = '<div class="tp" style="padding-top:4px">' + card("PLA Basic - Jade White", "Bambu Lab", "#f2f2ec", 32, 3, sub("PLA", "#F2F2EC", "1.75mm", "Shelf"), 1) + '</div>';
    $("#wt-prev").innerHTML = [["PLA Matte - Black", "#111111", 640, 64], ["PLA Basic - Jade White", "#f2f2ec", 32, 3]].map(function (s, i) {
      return card(s[0], "Bambu Lab", s[1], s[2], s[3], sub("PLA", s[1].toUpperCase(), "1.75mm", "AMS 1"), i);
    }).join("");
  }
  function sync(src) {
    themes();
    $$("#th-grid .th").forEach(function (o) { o.classList.toggle("on", o.dataset.th === cur.theme); });
    $("#tb").style.display = cur.theme === "custom" ? "" : "none";
    KEYS.forEach(function (k) {
      var c = $('[data-c="' + k + '"]'), x = $('[data-hx="' + k + '"]');
      if (c !== src) c.value = cur.custom[k];
      if (x !== src) { x.value = cur.custom[k].toUpperCase(); x.classList.remove("bad"); }
    });
    var segv = { rad: String(cur.custom.radius), density: cur.density, cols: cur.cols, units: cur.units, wfmt: cur.wfmt };
    Object.keys(segv).forEach(function (n) { $$('input[name="th-' + n + '"]').forEach(function (i) { i.checked = i.value === segv[n]; i.parentNode.classList.toggle("on", i.checked); }); });
    $$(".sw-o").forEach(function (o) { o.classList.toggle("on", o.dataset.sw === cur.swatch); });
    $$("[data-show]").forEach(function (i) { i.checked = !!cur.show[i.dataset.show]; });
    $("#ac-motion").checked = cur.motion; $("#ac-cb").checked = cur.cb;
    $$("#fo-list .fo").forEach(function (o) { o.classList.toggle("on", o.dataset.f === cur.font); });
    $$("#ap-seg label").forEach(function (l) { var i = l.querySelector("input"); i.checked = +i.value === cur.size; l.classList.toggle("on", i.checked); });
    $$("#sz-row span").forEach(function (s) { s.classList.toggle("on", +s.dataset.z === cur.size); });
    apply(cur); prev();
  }
  function tab(t) {
    $$("#tabs-set button").forEach(function (b) { b.classList.toggle("on", b.dataset.st === t); });
    if (window.glide) glide($("#tabs-set .tabs"));
    $("#view-settings").classList.toggle("ap-on", t === "appearance");
    window.scrollTo(0, 0);
  }
  window.__spoolioLook = function () { return cp(cur); };
  pane();
  $("#tabs-set").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    if (!b.classList.contains("on")) pgIn($("#view-settings"), b.dataset.st === "general");
    tab(b.dataset.st);
  });
  var vs = $("#view-settings");
  vs.addEventListener("click", function (e) {
    var x, id = e.target.id;
    if ((x = e.target.closest(".fo"))) { cur.font = x.dataset.f; sync(); }
    if ((x = e.target.closest("[data-th]"))) { cur.theme = x.dataset.th; sync(); }
    if ((x = e.target.closest("[data-from]"))) { cur.custom = cp(P[x.dataset.from]); cur.theme = "custom"; sync(); }
    if ((x = e.target.closest(".sw-o"))) { cur.swatch = x.dataset.sw; sync(); }
    if (id === "ap-reset") { cur.font = "def"; cur.size = 100; sync(); }
    if (id === "tb-reset") { cur.custom = cp(P.dark); sync(); }
    if (id === "ap-all") { cur = cp(DEF); sync(); }
    if (id === "tb-copy") copy();
    if (id === "tb-paste") { $("#tb-pbox").hidden = false; $("#tb-code").value = ""; $("#tb-code").focus(); }
    if (id === "tb-apply") paste($("#tb-code").value);
  });
  vs.addEventListener("input", function (e) {
    var k = e.target.dataset.c || e.target.dataset.hx; if (!k) return;
    var v = e.target.value.trim(); if (v[0] !== "#") v = "#" + v;
    var ok = /^#[0-9a-f]{6}$/i.test(v); e.target.classList.toggle("bad", !ok);
    if (!ok) return;
    cur.custom[k] = v.toLowerCase(); cur.theme = "custom"; sync(e.target);
  });
  vs.addEventListener("change", function (e) {
    var n = e.target.name || "", sh = e.target.dataset.show;
    if (n === "ap-size") { cur.size = +e.target.value; sync(); }
    if (n.indexOf("th-") === 0) { var k = n.slice(3), v = e.target.value; if (k === "rad") { cur.custom.radius = +v; cur.theme = "custom"; } else cur[k] = v; sync(); }
    if (sh) { cur.show[sh] = e.target.checked; sync(); }
    if (e.target.id === "ac-motion") { cur.motion = e.target.checked; sync(); }
    if (e.target.id === "ac-cb") { cur.cb = e.target.checked; sync(); }
  });
  bridge.onMessage(function (d) {
    if (!d || d.type !== "show_view") return;
    var on = d.view === "settings";
    $("#tabs-set").classList.toggle("view-hidden", !on);
    if (d.appearance) saved = d.appearance;
    cur = cp(saved);
    sync();
    if (on) tab("general");
  });
  sync();
"""


PRESET_HTML = """<div class="srow" id="pm-card"><h3><span class="emoji">🧩</span>Configure OrcaSlicer Presets<label class="pm-sw pm-msw"><span>Add Spools as Filament Presets</span><input id="pm-on" type="checkbox"><i></i></label></h3><div class="srow-body"><div class="srow-l">
  <div class="hint warn" id="pm-warn" hidden><strong>!</strong> <span id="pm-wt"></span></div>
  <div class="hint warn" id="pm-skip" hidden><strong>!</strong> <span id="pm-skt"></span></div>
  <div id="pm-opts"><div class="pm-lab">Settings</div><div class="pm-acc">
<div class="pm-sec" data-s="pr"><div class="pm-sh" role="button" tabindex="0" aria-expanded="false"><span class="pm-ic"><svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="12" rx="2"/><path d="M8 21h8M12 15v6"/></svg></span><span class="pm-tx"><span class="pm-tt">Presets and Printers</span><span class="pm-sm" id="pm-s-pr"></span></span><span class="pm-chev">&#9656;</span></div><div class="pm-sb"><div class="field-label">Create one preset per:</div><div class="seg"><label><input type="radio" name="pm-per" value="filament">Filament</label><label><input type="radio" name="pm-per" value="spool">Spool</label></div><div class="hint">Spool gives each spool its own preset, named with its ID.</div><div class="field-label">Make presets for these printers:</div><div id="pm-prs"></div><div class="hint">Named Filament @ Printer. A spool loaded in a printer only gets that printer's preset. Use With in Edit Spool changes this.</div></div></div>
<div class="pm-sec" data-s="base"><div class="pm-sh" role="button" tabindex="0" aria-expanded="false"><span class="pm-ic"><svg viewBox="0 0 24 24"><path d="M12 3 3 8l9 5 9-5-9-5z"/><path d="m3 13 9 5 9-5"/></svg></span><span class="pm-tx"><span class="pm-tt">Base Presets</span><span class="pm-sm" id="pm-s-base"></span></span><span class="pm-chev">&#9656;</span></div><div class="pm-sb"><div class="seg"><label><input type="radio" name="pm-base" value="auto">Printer's Own Preset</label><label><input type="radio" name="pm-base" value="pick">Choose Per Material</label></div><div id="pm-mats"></div><div class="hint">Anything Spoolio doesn't set comes from this preset, or Generic if there is none.</div></div></div>
<div class="pm-sec" data-s="sm"><div class="pm-sh" role="button" tabindex="0" aria-expanded="false"><span class="pm-ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/></svg></span><span class="pm-tx"><span class="pm-tt">Values From Spoolman</span><span class="pm-sm" id="pm-s-sm"></span></span><span class="pm-chev">&#9656;</span></div><div class="pm-sb"><div class="pm-ck"><label class="check-row"><input type="checkbox" data-take="colour">Colour</label><label class="check-row"><input type="checkbox" data-take="temps">Nozzle and Bed Temperatures</label><label class="check-row"><input type="checkbox" data-take="dens">Density and Diameter</label><label class="check-row"><input type="checkbox" data-take="cost">Cost</label></div><label class="check-row pm-tn"><input id="pm-extra" type="checkbox">Map Matching Spoolman Extra Fields</label><div class="hint">Extra fields named like an OrcaSlicer filament setting override it (spool fields too, with one preset per spool).</div></div></div>
<div class="pm-sec" data-s="tn"><div class="pm-sh" role="button" tabindex="0" aria-expanded="false"><span class="pm-ic"><svg viewBox="0 0 24 24"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg></span><span class="pm-tx"><span class="pm-tt">Advanced Tuning</span><span class="pm-sm" id="pm-s-tn"></span></span><label class="pm-sw" title="Use Advanced Tuning From Spoolman"><input id="pm-tune" type="checkbox"><i></i></label><span class="pm-chev">&#9656;</span></div><div class="pm-sb"><div id="pm-tm" hidden></div><div class="pm-ck" id="pm-tks"><label class="check-row"><input type="checkbox" data-tk="flow">Flow Ratio</label><label class="check-row"><input type="checkbox" data-tk="pa">Pressure Advance</label><label class="check-row"><input type="checkbox" data-tk="mvs">Max Volumetric Speed</label><label class="check-row"><input type="checkbox" data-tk="rlen">Retraction Length</label><label class="check-row"><input type="checkbox" data-tk="rspd">Retraction Speed</label></div><div class="hint" id="pm-tkh">Adds a Tuning step to Edit Spool and offers to update Spoolman when a value changes. Blank values come from the base preset.</div></div></div>
  </div></div>
</div><div class="srow-r" id="pm-prev"></div></div><div class="pm-sync" id="pm-sync"><span class="pm-dot"></span><span id="pm-st"></span><button type="button" class="pm-btn" id="pm-now">Sync Now</button></div></div>"""

PRESET_CSS = """
  #pm-card .pm-sq { display: inline-block; width: 12px; height: 12px; border-radius: 3px; border: 1px solid var(--orca-border); margin-right: 8px; vertical-align: -2px; flex: none; }
  #pm-card > h3 { gap: 10px; }
  #pm-card .pm-lab { margin-bottom: 8px; font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--orca-muted); }
  #pm-card .pm-sw { position: relative; display: inline-flex; align-items: center; gap: 10px; cursor: pointer; flex: none; }
  #pm-card .pm-msw { margin-left: auto; font-size: 12px; font-weight: 400; color: var(--orca-muted); }
  #pm-card .pm-sw input { position: absolute; opacity: 0; width: 1px; height: 1px; margin: 0; }
  #pm-card .pm-sw i { position: relative; width: 34px; height: 20px; border-radius: 999px; background: var(--orca-border); flex: none; transition: background .2s; }
  #pm-card .pm-sw i::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition: left .2s; }
  #pm-card .pm-sw input:checked + i { background: var(--orca-accent); }
  #pm-card .pm-sw input:checked + i::after { left: 17px; }
  #pm-card .pm-sw input:focus-visible + i { outline: 2px solid var(--orca-accent); outline-offset: 2px; }
  #pm-card .pm-acc { border: 1px solid var(--orca-border); border-radius: 10px; overflow: hidden; background: color-mix(in srgb, var(--orca-fg) 2%, transparent); }
  #pm-card .pm-sec + .pm-sec { border-top: 1px solid var(--orca-border); }
  #pm-card .pm-sh { display: flex; align-items: center; gap: 12px; padding: 12px 14px; cursor: pointer; user-select: none; }
  #pm-card .pm-sh:focus-visible { outline: 2px solid var(--orca-accent); outline-offset: -2px; }
  #pm-card .pm-ic { width: 28px; height: 28px; border-radius: 7px; flex: none; display: flex; align-items: center; justify-content: center; color: var(--orca-accent); background: color-mix(in srgb, var(--orca-accent) 16%, transparent); }
  #pm-card .pm-ic svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  #pm-card .pm-tx { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  #pm-card .pm-tt { font-weight: 600; }
  #pm-card .pm-sm { font-size: 12px; color: var(--orca-muted); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #pm-card .pm-chev { color: var(--orca-muted); width: 14px; font-size: 15px; text-align: center; transition: transform .2s; }
  #pm-card .pm-sec.open .pm-chev { transform: rotate(90deg); }
  #pm-card .pm-sec.open .pm-sh { background: color-mix(in srgb, var(--orca-fg) 4%, transparent); }
  #pm-card .pm-sb { display: none; padding: 4px 14px 16px 54px; border-top: 1px solid var(--orca-border); }
  #pm-card .pm-sec.open .pm-sb { display: block; animation: pm-open .2s ease-out; }
  @keyframes pm-open { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
  #pm-card.pm-tnoff .pm-sec[data-s="tn"] .pm-sb, #pm-card.pm-tnoff .pm-sec[data-s="tn"] .pm-chev { display: none; }
  #pm-card .pm-sb .field-label { margin-top: 12px; }
  #pm-card .pm-sb > .seg:first-child, #pm-card .pm-sb > .pm-ck:first-child { margin-top: 10px; }
  #pm-card .pm-sb .hint { margin-top: 6px; }
  #pm-card .hint.warn { margin: 0 0 12px; }
  #pm-card .pm-pr { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
  #pm-card .pm-pr .check-row { margin: 0; flex: 1; min-width: 0; }
  #pm-card .pm-pr select { width: 55%; }
  #pm-card .pm-pr select, #pm-card .pm-mat select { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 4px 6px; font: inherit; font-size: 12px; }
  #pm-card .pm-nz { margin: -2px 0 6px 24px; font-size: 11.5px; color: var(--orca-muted); }
  #pm-card .pm-mat { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
  #pm-card .pm-mat span { width: 70px; font-weight: 600; }
  #pm-card .pm-mat select { max-width: none; flex: 1; }
  #pm-card .pm-sync { display: flex; align-items: center; gap: 8px; margin: 0 18px 16px; padding-top: 12px; border-top: 1px solid var(--orca-border); font-size: 12px; color: var(--orca-muted); }
  #pm-card .pm-dot { width: 8px; height: 8px; border-radius: 50%; background: #2e9d5b; flex: none; }
  #pm-card .pm-sync.bad .pm-dot { background: #d9534f; }
  #pm-card .pm-sync.idle .pm-dot { background: var(--orca-muted); }
  #pm-card .pm-sync.bad #pm-st { color: #d9534f; }
  #pm-card .pm-btn { margin-left: auto; border: 1px solid var(--orca-border); background: transparent; color: var(--orca-fg); border-radius: 6px; padding: 5px 12px; font: inherit; font-size: 12px; cursor: pointer; }
  #pm-card .pm-dd { border: 1px solid var(--orca-border); border-radius: 6px; overflow: hidden; font-size: 12.5px; }
  #pm-card .pm-dd-top { display: flex; align-items: center; padding: 7px 10px; border-bottom: 1px solid var(--orca-border); }
  #pm-card .pm-car { margin-left: auto; color: var(--orca-muted); }
  #pm-card .pm-grp { padding: 6px 10px 3px; font-size: 11px; font-weight: 600; color: var(--orca-muted); }
  #pm-card .pm-it { display: flex; align-items: center; padding: 5px 10px; }
  #pm-card .pm-it.sel { background: var(--orca-accent); color: var(--orca-accent-fg); }
  #pm-card .pm-it.dim { color: var(--orca-muted); }
  #pm-card .pm-tb { width: 100%; border-collapse: collapse; font-size: 12px; }
  #pm-card .pm-tb td { padding: 4px 0; border-bottom: 1px solid var(--orca-border); }
  #pm-card .pm-tb td:first-child { color: var(--orca-muted); width: 45%; }
  #pm-card .pm-l { margin-top: 14px; }
  #pm-card.pm-vals .pm-pv-dd, #pm-card:not(.pm-vals) .pm-pv-tb { display: none; }
  #pm-card .pm-note { font-size: 12px; color: var(--orca-muted); border: 1px dashed var(--orca-border); border-radius: 8px; padding: 14px; }
  #pm-card .pm-ck { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 2px 12px; margin-top: 6px; }
  #pm-card .pm-ck .check-row { margin: 4px 0; }
  #pm-card .pm-tn { margin: 12px 0 0; }
  #pm-card.pm-off #pm-opts { opacity: .45; pointer-events: none; }
  #pm-card.pm-off .pm-sync { display: none; }
  #pm-card.pm-na #pm-opts, #pm-card.pm-na .pm-msw, #pm-card.pm-na .pm-sync { opacity: .45; pointer-events: none; }
  #pm-real .spool-card { margin: 0; }
  #pm-real .spool-edit, #pm-real .spool-cart { display: none; }
  @media (max-width: 900px) { #pm-card .pm-msw span { display: none; } }
"""

PRESET_JS = r"""
  var $ = function (id) { return document.getElementById(id); };
  var card = $("pm-card"), I = null, C = null, t = 0;
  if (!card) return;
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function radio(n) { var x = document.querySelector('input[name="' + n + '"]:checked'); return x ? x.value : ""; }
  function setRadio(n, v) { var x = document.querySelector('input[name="' + n + '"][value="' + v + '"]'); if (x) x.checked = true; }
  function sq(c) { return '<span class="pm-sq" style="background:' + esc(c || "transparent") + '"></span>'; }
  function cur() {
    var pr = {}, bs = Object.assign({}, C ? C.bases : {});
    document.querySelectorAll("#pm-prs [data-p]").forEach(function (r) { pr[r.dataset.p] = { on: r.querySelector("input").checked, group: r.querySelector("select").value }; });
    document.querySelectorAll("#pm-mats select").forEach(function (s) { if (s.value) bs[s.dataset.m] = s.value; else delete bs[s.dataset.m]; });
    var tk = {}, tn = {};
    document.querySelectorAll("#pm-card [data-take]").forEach(function (x) { tk[x.dataset.take] = x.checked; });
    document.querySelectorAll("#pm-card [data-tk]").forEach(function (x) { tn[x.dataset.tk] = x.checked; });
    return { on: $("pm-on").checked, per: radio("pm-per") || "filament", base: radio("pm-base") || "auto", printers: pr, bases: bs,
      take: tk, extra_map: $("pm-extra").checked, tune: { on: $("pm-tune").checked, keys: tn } };
  }
  function spoolman() { var x = document.querySelector('input[name="spool-source"]:checked'); return !x || x.value === "spoolman"; }
  function look() {
    document.querySelectorAll("#pm-card .seg input").forEach(function (r) { r.parentNode.classList.toggle("on", r.checked); });
    var na = !I || !I.api || !spoolman();
    card.classList.toggle("pm-na", na);
    card.classList.toggle("pm-off", !$("pm-on").checked);
    card.classList.toggle("pm-tnoff", !$("pm-tune").checked);
    $("pm-wt").textContent = I && !I.api ? "This version of OrcaSlicer can't add presets." : !spoolman() ? "Only available with Spoolman as your spool source." : "";
    $("pm-warn").hidden = !$("pm-wt").textContent;
    $("pm-skt").textContent = !na && $("pm-on").checked && I ? (I.skip_msg || "") : "";
    $("pm-skip").hidden = !$("pm-skt").textContent;
    sums();
  }
  var OPEN = [];
  try { OPEN = JSON.parse(localStorage.getItem("spoolio.pmopen") || "[]") || []; } catch (e) { OPEN = []; }
  function lst(a) { return a.length > 1 ? a.slice(0, -1).join(", ") + " and " + a[a.length - 1] : a.join(""); }
  function sums() {
    var c = cur(), pr = [];
    document.querySelectorAll("#pm-prs [data-p]").forEach(function (r) { if (r.querySelector("input").checked) pr.push(r.querySelector(".check-row").textContent.trim()); });
    $("pm-s-pr").textContent = (c.per === "spool" ? "One per spool" : "One per filament") + " · " + (pr.length ? pr.join(", ") : "no printers ticked");
    $("pm-s-base").textContent = c.base === "pick" ? "Chosen per material" : "Printer's own preset for each material";
    var tk = [["colour", "colour"], ["temps", "temperatures"], ["dens", "density"], ["cost", "cost"]].filter(function (x) { return c.take[x[0]]; }).map(function (x) { return x[1]; });
    var sm = tk.length ? lst(tk) : "nothing";
    $("pm-s-sm").textContent = sm.charAt(0).toUpperCase() + sm.slice(1) + (c.extra_map ? " · extra fields" : "");
    var tn = [["flow", "flow ratio"], ["pa", "pressure advance"], ["mvs", "max volumetric speed"], ["rlen", "retraction length"], ["rspd", "retraction speed"]].filter(function (x) { return c.tune.keys[x[0]]; }).map(function (x) { return x[1]; });
    var tt = !c.tune.on ? "Off" : tn.length ? lst(tn) : "Nothing ticked yet";
    $("pm-s-tn").textContent = tt.charAt(0).toUpperCase() + tt.slice(1);
    document.querySelectorAll("#pm-card .pm-sec").forEach(function (x) {
      var o = OPEN.indexOf(x.dataset.s) >= 0 && !(x.dataset.s === "tn" && !c.tune.on);
      x.classList.toggle("open", o); x.querySelector(".pm-sh").setAttribute("aria-expanded", o ? "true" : "false");
    });
    card.classList.toggle("pm-vals", OPEN.some(function (k) { return k === "sm" || (k === "tn" && c.tune.on); }));
  }
  function fold(sec) {
    var k = sec.dataset.s, i = OPEN.indexOf(k);
    if (k === "tn" && !$("pm-tune").checked) return;
    if (i >= 0) OPEN.splice(i, 1); else OPEN.push(k);
    try { localStorage.setItem("spoolio.pmopen", JSON.stringify(OPEN)); } catch (e) {}
    sums();
  }
  function ask() { clearTimeout(t); t = setTimeout(function () { orca.postMessage({ type: "presets_info", cfg: cur() }); }, 150); }
  function nz(names) {
    var s = (names || []).map(function (n) { var m = /(\d+(?:\.\d+)?)\s*(?:mm\s*)?nozzle/i.exec(n); return m ? m[1] : ""; }).filter(Boolean);
    return s.length ? "Nozzles: " + s.join(", ") + " mm" : (names || []).length ? names.length + " printer preset" + (names.length > 1 ? "s" : "") : "";
  }
  function status(d) {
    var el = $("pm-sync"), st = d.status || {}, txt;
    el.classList.remove("bad", "idle");
    if (st.err) { el.classList.add("bad"); txt = st.err; }
    else if (st.time) txt = st.n + " preset" + (st.n === 1 ? "" : "s") + " up to date, synced at " + st.time;
    else { el.classList.add("idle"); txt = "Not synced yet. Save to create your presets."; }
    $("pm-st").textContent = txt;
  }
  function prev(d) {
    var s = d.sample, h;
    if (!d.api) h = '<div class="pm-note">This version of OrcaSlicer can\'t add presets.</div>';
    else if (!s) h = '<div class="pm-note">' + (d.printers.some(function (p) { return p.on && p.group; }) ? "Your presets show here once Spoolio has loaded your spools." : "Tick a printer to see the presets Spoolio will add.") + '</div>';
    else {
      h = '<div class="label">Your Spool</div><div id="pm-real">' + (window.__spoolioCard ? window.__spoolioCard(s.spool) : "") + '</div>' +
        '<div class="pm-pv-dd"><div class="label pm-l">In OrcaSlicer, With ' + esc(s.printer) + ' Selected</div>' +
        '<div class="pm-dd"><div class="pm-dd-top">' + sq(s.colour) + esc(s.name) + '<span class="pm-car">▾</span></div><div class="pm-dd-list"><div class="pm-grp">Spoolio</div>' +
        s.list.map(function (x) { return '<div class="pm-it' + (x.n === s.name ? " sel" : "") + '">' + sq(x.c) + esc(x.n) + '</div>'; }).join("") +
        (s.more ? '<div class="pm-it dim">+ ' + s.more + ' more</div>' : "") +
        '<div class="pm-grp">System</div><div class="pm-it dim">' + sq("#888") + esc(s.base) + '</div></div></div></div>' +
        '<div class="pm-pv-tb"><div class="label pm-l">Copied From Spoolman</div><table class="pm-tb">' +
        s.rows.map(function (r) { return '<tr><td>' + esc(r[0]) + '</td><td>' + (r[0] === "Colour" && r[1].charAt(0) === "#" ? sq(r[1]) : "") + esc(r[1]) + '</td></tr>'; }).join("") + '</table></div>';
    }
    $("pm-prev").innerHTML = h;
  }
  function draw(d, full) {
    I = d; C = d.cfg;
    if (full) {
      $("pm-on").checked = C.on; setRadio("pm-per", C.per); setRadio("pm-base", C.base); $("pm-extra").checked = !!C.extra_map; $("pm-tune").checked = C.tune.on;
      document.querySelectorAll("#pm-card [data-take]").forEach(function (x) { x.checked = C.take[x.dataset.take] !== false; });
      document.querySelectorAll("#pm-card [data-tk]").forEach(function (x) { x.checked = !!C.tune.keys[x.dataset.tk]; });
    }
    var opts = function (g) { return '<option value="">Choose a Printer Preset</option>' + d.groups.map(function (x) { return '<option' + (x === g ? " selected" : "") + '>' + esc(x) + '</option>'; }).join(""); };
    $("pm-prs").innerHTML = d.printers.length ? d.printers.map(function (p) {
      return '<div class="pm-pr" data-p="' + esc(p.id) + '"><label class="check-row"><input type="checkbox"' + (p.on ? " checked" : "") + '>' + esc(p.name) + '</label><select>' + opts(p.group) + '</select></div>' +
        '<div class="pm-nz">' + esc(p.group ? nz(p.nozzles) : "No OrcaSlicer printer preset matched. Choose one.") + '</div>';
    }).join("") : '<div class="hint">Add your printers on the Printer tab first.</div>';
    $("pm-mats").innerHTML = radio("pm-base") === "pick" ? (d.materials.length ? d.materials.map(function (m) {
      if (!m.opts.length) return '<div class="pm-mat"><span>' + esc(m.m) + '</span><select data-m="' + esc(m.m) + '" disabled><option value="">No OrcaSlicer preset matches ' + esc(m.m) + '</option></select></div>';
      return '<div class="pm-mat"><span>' + esc(m.m) + '</span><select data-m="' + esc(m.m) + '"><option value="">Printer\'s Own Preset</option>' +
        m.opts.map(function (o) { return '<option' + (o === m.pick ? " selected" : "") + '>' + esc(o) + '</option>'; }).join("") + '</select></div>';
    }).join("") : '<div class="hint">Spoolio lists your materials here once it has loaded your spools.</div>') : "";
    status(d); prev(d); look();
  }
  card.addEventListener("change", function () { look(); ask(); });
  card.addEventListener("click", function (e) {
    var h = e.target.closest(".pm-sh");
    if (h && !e.target.closest(".pm-sw")) fold(h.parentNode);
  });
  card.addEventListener("keydown", function (e) {
    var h = e.target.closest(".pm-sh");
    if (h && e.target === h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fold(h.parentNode); }
  });
  document.querySelectorAll('input[name="spool-source"]').forEach(function (r) { r.addEventListener("change", look); });
  $("pm-now").addEventListener("click", function () { $("pm-st").textContent = "Syncing…"; orca.postMessage({ type: "presets_sync", cfg: cur() }); });
  window.__spoolioPresets = function () { return I && I.api ? cur() : null; };
  orca.onMessage(function (d) {
    if (!d) return;
    if (d.type === "presets_info" && d.view === "settings") draw(d, !I || d.full);
    else if (d.type === "show_view" && d.view === "settings") { I = null; orca.postMessage({ type: "presets_info" }); }
  });
  look();
  orca.postMessage({ type: "presets_info" });
"""


def tab_html(initial_view: str, c: dict) -> str:
    """The spool list and Settings as two views of one page, under a shared header."""
    main_css, main_body, main_js = _split_page(main_html())
    settings_css, settings_body, settings_js = _split_page(settings_html(c))
    main_body, main_header = _take_header(main_body)
    tabs_at = main_body.index('<div id="tabs"')
    tabs_end = main_body.index("</div>", tabs_at) + len("</div>")
    tabs_bar = main_body[tabs_at:tabs_end]
    main_body = main_body[:tabs_at] + main_body[tabs_end:]
    settings_body, settings_header = _take_header(settings_body)
    on_main = initial_view == "main"
    settings_button = _button(main_header, "settings-btn")
    feedback_button = _button(settings_header, "feedback")
    sec = {name: _section(settings_body, name) for name in (
        "steps", "reorder", "check", "check-hint", "server", "about", "weights", "weights-preview",
        "layout", "layout-preview", "stats", "stats-preview", "printer", "printer-preview", "footer")}
    # The version, update check and log path sit beside the footer buttons.
    footer = sec["footer"]
    version_row = re.search(r'<div class="version-row">.*?</div>', footer, re.S).group(0)
    footer = footer.replace(version_row, f'<div class="foot-diag">{version_row}{sec["about"]}</div>')

    def card(left: str, label: str, right: str) -> str:
        title, rest = re.match(r"\s*(<h3>.*?</h3>)(.*)", left, re.S).groups()
        return (f'<div class="srow">{title}<div class="srow-body"><div class="srow-l">{rest}</div>'
                f'<div class="srow-r"><div class="label">{label}</div>{right}</div></div></div>')

    settings_view = "\n".join([
        card(sec["server"], "How It Works", sec["steps"]),
        card(sec["reorder"], "Preview", TAB_PREVIEW_HTML),
        card(sec["weights"], "Preview", sec["weights-preview"]).replace('class="srow"', 'class="srow moved"', 1),
        card(sec["layout"], "Preview", sec["layout-preview"]).replace('class="srow"', 'class="srow moved"', 1),
        card(sec["check"], "Before You Start", sec["check-hint"]),
        card(sec["printer"], "Preview", sec["printer-preview"]),
        card(sec["stats"], "Preview", sec["stats-preview"]),
        PRESET_HTML,
        look_html(),
        footer,
    ])
    main_scoped = _scope_css(main_css, "#view-main")
    # The card's preview and RFID label reuse the spool list's own card styles.
    pv = "".join(m.group(0).replace("#view-main", "#pc .pv") + "\n"
                 for m in re.finditer(r"#view-main \.spool[^{}]*\{[^}]*\}", main_scoped))
    pv += "".join(m.group(0).replace("#view-main", "#pc .fl") + "\n"
                  for m in re.finditer(r"#view-main \.spool-rfid[^{}]*\{[^}]*\}", main_scoped))
    pv += "#pc .fl .spool-rfid { display: inline-flex; vertical-align: middle; margin: 0; }\n"
    pv += "".join(m.group(0).replace("#view-main", "#pm-real") + "\n"
                  for m in re.finditer(r"#view-main \.spool[^{}]*\{[^}]*\}", main_scoped))
    look_js = (LOOK_JS.replace("__FONTS__", json.dumps({k: css for k, _, _, _, css in LOOK_FONTS}))
               .replace("__THEMES__", json.dumps(THEMES))
               .replace("__LOOK__", json.dumps(c.get("appearance") or parse_look(None))))
    set_tabs = ('<div class="tabs"><button data-st="general" class="on">General</button>'
                '<button data-st="appearance">Appearance</button></div>')
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="{LOGO_DATA_URI}">
<style>
  html, body {{ height: 100%; margin: 0; background: var(--orca-bg); overscroll-behavior-x: none; }}
  body {{ box-sizing: border-box; padding-top: 76px; color: var(--orca-fg); }}
  .view-hidden {{ display: none !important; }}
  #view-settings {{ min-height: 100%; max-width: 1100px; }}
{_fill(TAB_HEADER_CSS, FONT=FONT_STACK)}
{main_scoped}
{_scope_css(settings_css, "#view-settings")}
{TAB_SETTINGS_CSS}
{LOOK_CSS}
{_fill(EDITOR_CSS, FONT=FONT_STACK)}
{_fill(MOVE_CSS, FONT=FONT_STACK)}
{pv}
{PRESET_CSS}
</style>
</head>
<body>
<div id="app-header">
  {LOGO_IMG}<h2>Spoolio</h2>
  <span id="tabs-main" class="{_hidden(not on_main)}">{tabs_bar}</span>
  <span id="tabs-set" class="{_hidden(on_main)}">{set_tabs}</span>
  <div class="header-actions">
    <span id="actions-main" class="{_hidden(not on_main)}">{settings_button}</span>
    <span id="actions-settings" class="{_hidden(on_main)}">{feedback_button}</span>
  </div>
</div>
<div id="view-main" class="view {_hidden(not on_main)}">{main_body}</div>
<div id="view-settings" class="view {_hidden(on_main)}">{settings_view}</div>
<script>{TAB_BRIDGE}</script>
<script>(function (orca) {{{main_js}}})(window.__spoolio.main);</script>
<script>(function (orca) {{{settings_js}}})(window.__spoolio.settings);</script>
<script>(function (bridge) {{{TAB_PREVIEW_JS}}})(window.__spoolio.settings);</script>
<script>(function (bridge) {{{look_js}}})(window.__spoolio.settings);</script>
<script>(function (orca) {{{EDITOR_JS}}})(window.__spoolio.main);</script>
<script>(function (orca) {{{MOVE_JS}}})(window.__spoolio.main);</script>
<script>(function (orca) {{{PRESET_JS}}})(window.__spoolio.settings);</script>
</body>
</html>
"""


class SpoolioPage(orca.pages.PagesPluginCapabilityBase):
    """The Spoolio tab, with Settings as a second view inside it."""

    def __init__(self):
        super().__init__()
        self._verified_url = ""
        self._verified_bambu = False
        self._release_url = ""
        self._refreshing = False
        self._testing = False
        self._checking_update = False
        _plate["page"] = self

    def get_name(self):
        return PLUGIN_NAME

    def get_type(self):
        return orca.PluginType.Pages

    def get_icon(self):
        return icon_path()

    def get_ui(self):
        try:
            return tab_html("main" if configured() else "settings", ui_cfg())
        except Exception:
            log.exception("get_ui() raised")
            raise

    def on_message(self, arg0):
        # The base class hints "arg0: str", but a parsed dict arrives; a string is still accepted.
        try:
            data = json.loads(arg0) if isinstance(arg0, str) else arg0
            if not isinstance(data, dict):
                return
            if data.get("type") in ("ready", "refresh", "save", "presets_info", "presets_sync"):
                psnap()
            if data.get("view") == "settings":
                self._on_settings(data)
                return
            msg_type = data.get("type")
            if msg_type in ("ready", "refresh"):
                self._push_data()
                if msg_type == "ready" and _plate["payload"]:
                    send_plate(_plate["payload"])
                if msg_type == "ready":
                    send_heads()
            elif msg_type == "settings":
                self._open_settings()
            elif msg_type == "open_spoolman":
                url = clean_url(self._spoolman_url())
                if url:
                    open_url(url)
            elif msg_type == "order":
                open_search(data.get("query"))
            elif msg_type == "printers":
                save_printers(data.get("printers"))
            elif msg_type == "heads_bind" and _sliced["slots"]:
                _bg(run_heads, _sliced["slots"], _sliced["preset"], str(data.get("id") or ""))
            elif msg_type == "live":
                self.post_message({"type": "live", "live": live_poll(), "usage": usage_state(), "new": new_spools(),
                                   "tune": tune_items()})
            elif msg_type == "editor":
                _bg(lambda: self.post_message({"type": "editor", **editor_data()}))
            elif msg_type == "catalogue":
                _bg(lambda: self.post_message({"type": "catalogue", "q": data.get("q") or "", **catalogue(data.get("q"))}))
            elif msg_type == "spool_save":
                _bg(self._spool_save, data)
            elif msg_type == "new_skip":
                new_skip(data.get("uid"))
            elif msg_type == "tune_orca":
                self.post_message({"type": "tune_orca", **tune_orca()})
            elif msg_type == "tune_apply":
                _bg(self._tune_apply, data)
            elif msg_type == "tm_scan":
                _bg(self._tm_scan, data.get("auto") is True)
            elif msg_type == "tm_later":
                cfg_edit(lambda s: s.update(tune_move=s.get("tune_move") or "later"))
            elif msg_type == "tm_apply":
                _bg(self._tm_do, "tm_moved", lambda: tune_move_apply(data.get("pick")))
            elif msg_type == "tm_delete":
                _bg(self._tm_do, "tm_deleted", tune_move_delete)
            elif msg_type == "printer_code":
                save_printer_code(data.get("id"), data.get("code"))
            elif msg_type == "printer_test":
                _bg(lambda: self.post_message(printer_test(data)))
            elif msg_type in ("usage_mode", "usage_book", "usage_undo"):
                _bg(lambda: self.post_message({"type": "usage", "usage": usage_action(data)}))
            elif msg_type == "stats":
                _bg(lambda: self.post_message({"type": "stats", "stats": stats_data(data.get("period"))}))
            elif msg_type == "plate_recheck":
                _bg(recheck)
            elif msg_type == "plate_dismiss":
                _plate.update(payload=None, slots=[])
        except Exception:
            log.exception("on_message() raised (arg0=%r)", arg0)

    def _spoolman_url(self):
        return cfg().get("spoolman_url", "")

    def _tm_scan(self, auto):
        r = tune_move_state(auto)
        if r is not None:
            self.post_message({"type": "tm", "auto": auto, **r})

    def _tm_do(self, kind, fn):
        r = fn()
        self.post_message({"type": kind, **r})
        if r["ok"]:
            self._push_data()

    def _tune_apply(self, data):
        save = data.get("save") is True
        result = tune_apply(data.get("ids"), save)
        self.post_message({"type": "tune_done", "save": save, **result})
        if save and result["ok"]:
            self._push_data()

    def _spool_save(self, data):
        result = spool_save(data)
        self.post_message({"type": "spool_saved", **result})
        if result["ok"]:
            self._push_data()

    def _push_data(self):
        url = self._spoolman_url()
        if source() == "spoolman" and not url:
            self.post_message({
                "type": "spools",
                "ok": False,
                "error": "No Spoolman URL configured yet - click Settings to set one.",
            })
            return
        if self._refreshing:
            return
        self._refreshing = True
        _local_sync()
        low_stock_grams = parse_low(cfg().get("low_stock_grams"))

        def worker():
            try:
                result = load_spools()
            except Exception as exc:
                log.exception("Spool refresh failed")
                result = {"ok": False, "error": str(exc)}
            finally:
                self._refreshing = False
            self.post_message({"type": "spools", "low_stock_grams": low_stock_grams,
                               "weight_display": weight_mode(), "show_cart": cart_on(),
                               "spool_columns": spool_columns(), "can_edit": source() == "spoolman",
                               **printer_state(), **result})
            if result.get("ok"):
                presets_sync(result["spools"] if source() == "spoolman" else None)

        _bg(worker)

    def _save_settings(self, url, low_stock, plate_check=True,
                       plate_margin=DEFAULT_MARGIN, weight="both", cart=False, src="spoolman",
                       progress=None, columns=None, stats=None, look=None, presets=None):
        settings = {}

        def put(s):
            s["spool_source"] = parse_source(src)
            if s["spool_source"] == "spoolman":
                s["spoolman_url"] = clean_url(url)
            s["low_stock_grams"] = parse_low(low_stock)
            s["plate_check"] = parse_flag(plate_check)
            s["plate_margin_percent"] = parse_margin(plate_margin)
            s["weight_display"] = parse_weights(weight)
            s["show_cart"] = parse_flag(cart, default=False)
            if columns is not None:
                s["spool_columns"] = parse_columns(columns)
            if progress is not None:
                s["progress"] = parse_progress(progress)
            if stats is not None:
                s["stats"] = parse_stats(stats)
            if presets is not None:
                old = parse_presets(s.get("presets"))
                s["presets"] = parse_presets({**presets, "use": old["use"]}) if isinstance(presets, dict) else old
            if look is not None:
                ap = s["appearance"] = parse_look(look, s)
                s["weight_display"] = {v: k for k, v in WFMT.items()}[ap["wfmt"]]
                if ap["cols"] != "auto":
                    s["spool_columns"] = int(ap["cols"])
            settings.update(s)

        if cfg_edit(put):
            log.info("Settings saved (url=%s, low stock=%s g, filament check=%s, margin=%s%%)",
                     url, settings["low_stock_grams"], settings["plate_check"],
                     settings["plate_margin_percent"])
            return True
        orca.host.ui.message(
            f"Could not write settings to {SETTINGS_FILE}. Check that this folder is writable.",
            title=PLUGIN_NAME,
            icon="error",
        )
        return False

    def _after_save(self):
        self._push_data()

    def _open_settings(self):
        c = ui_cfg()
        self.post_message({"type": "show_view", "view": "settings", **c,
                           "cur_code": _cur["code"] or "EUR"})
        self._check_connection(c["url"])

    def _settings_post(self, data):
        self.post_message({**data, "view": "settings"})

    def _settings_close(self):
        self.post_message({"type": "show_view", "view": "main", "appearance": ui_cfg()["appearance"]})

    def _check_connection(self, url, src=None):
        if self._testing:
            return
        self._testing = True
        src = parse_source(src or source())

        def worker():
            info = bambu_ping() if src == "bambu" else ping(url)
            self._testing = False
            if src == "bambu":
                self._verified_bambu = bool(info.get("ok"))
            elif info.get("ok"):
                self._verified_url = clean_url(url)
            self._settings_post({"type": "test_result", "source": src, "url": url, **info})

        _bg(worker)

    def _bambu_sign_in(self, data):
        if data.get("action") == "sign_out":
            self._verified_bambu = False

        def worker():
            result = bambu_sign_in(data)
            self._settings_post({"type": "bambu_auth_result", **result})

        _bg(worker)

    def _on_settings(self, data):
        msg_type = data.get("type")
        if msg_type == "ready":
            self._check_connection(self._spoolman_url())
        elif msg_type == "test":
            url = (data.get("url") or "").strip()
            self._check_connection(url, data.get("source"))
        elif msg_type == "save":
            url = (data.get("url") or "").strip()
            src = parse_source(data.get("source"))
            verified = (self._verified_bambu if src == "bambu"
                        else clean_url(url) == self._verified_url)
            if not verified:
                self._settings_post({
                    "type": "test_result",
                    "source": src,
                    "ok": False,
                    "url": url,
                    "error": "Test the connection successfully before saving.",
                })
                return
            saved = self._save_settings(url, data.get("low_stock"), data.get("plate_check"),
                                        data.get("plate_margin"), data.get("weight_display"),
                                        data.get("show_cart"), src, data.get("progress"),
                                        data.get("spool_columns"), data.get("stats"), data.get("appearance"),
                                        data.get("presets"))
            if saved:
                self._after_save()
                self._settings_close()
        elif msg_type == "bambu_auth":
            self._bambu_sign_in(data)
        elif msg_type == "presets_info":
            _bg(lambda: self._settings_post(presets_info(data.get("cfg"))))
        elif msg_type == "presets_sync":
            _bg(lambda: (presets_sync(force=True), self._settings_post(presets_info(data.get("cfg")))))
        elif msg_type == "feedback":
            open_url(FEEDBACK_URL)
        elif msg_type == "check_update":
            if self._checking_update:
                return
            self._checking_update = True

            def worker():
                result = get_release()
                self._checking_update = False
                if result["ok"]:
                    self._release_url = result["url"]
                    result["newer"] = is_newer(result["version"], PLUGIN_VERSION)
                self._settings_post({"type": "update_result", **result})

            _bg(worker)
        elif msg_type == "release" and self._release_url.startswith("https://"):
            open_url(self._release_url)
        elif msg_type == "cancel":
            self._settings_close()

    def on_load(self):
        log.info(
            "%s %s loaded as a native tab (Python %s, %s)",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            platform.python_version(),
            platform.platform(),
        )

    def on_unload(self):
        log.info("Unloading")


if _SLICING:

    class SpoolioSliceSync(orca.slicing.SlicingPipelineCapabilityBase):
        """After each slice: checks the plate's filament, plans the booking and fills non-Bambu toolheads."""

        def get_name(self):
            return "Spoolio Slice Sync"

        def execute(self, ctx):
            try:
                if ctx.step == getattr(orca.slicing.Step, "posSlice", None):
                    if _plate["payload"]:
                        _bg(clear_plate)
                    return orca.ExecutionResult.success()
                if ctx.step != orca.slicing.Step.psGCodePostProcess:
                    return orca.ExecutionResult.success()
                enabled, margin = plate_settings()
                slots = plate_slots(ctx, parse_usage(read_tail(ctx.gcode_path)))
                usage_plan(slots)
                try:
                    tune_check(ctx)
                except Exception:
                    log.exception("Tuning check failed")
                _bg(run_heads, slots, str(ctx.config_value("printer_settings_id") or ""))
                if enabled:
                    if any(slot["grams"] > 0 for slot in slots):
                        # Off the slicing thread: it may fetch the spool list, and UI calls from
                        # here can deadlock while the GUI waits for slicing to finish.
                        _bg(run_check, slots, margin)
            except Exception:
                log.exception("Filament check failed")
            return orca.ExecutionResult.success()


@orca.plugin
class SpoolioPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(SpoolioPage)
        if _SLICING:
            orca.register_capability(SpoolioSliceSync)
