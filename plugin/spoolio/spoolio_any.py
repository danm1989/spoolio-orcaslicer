# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Spoolio"
# description = "A Bambu Lab inspired inventory management overview for OrcaSlicer"
# author = "Dan J Moore"
# version = "0.5.0"
# ///

import base64
import html
import json
import logging
import os
import platform
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import orca

try:
    import orca.pages  # noqa: F401
    _PAGES = True
except (ImportError, AttributeError):
    _PAGES = False

_SLICING = hasattr(getattr(orca, "slicing", None), "SlicingPipelineCapabilityBase")

PLUGIN_NAME = "Spoolio"
PLUGIN_VERSION = "0.5.0"
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
MAIN_SIZE = (380, 600)
SETTINGS_SIZE = (560, 820)

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
    for parent in PLUGIN_DIR.parents:
        if parent.name == "OrcaSlicer":
            return parent / "spoolio"
    return _config_root() / "OrcaSlicer" / "spoolio"


DATA_DIR = _data_dir()
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
log.info(
    "%s %s (%s build) imported; orca.pages %s",
    PLUGIN_NAME,
    PLUGIN_VERSION,
    BUILD_TARGET,
    "available" if _PAGES else "not available",
)


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


def get_settings() -> dict:
    defaults = {
        "spoolman_url": "",
        "spool_source": "spoolman",
        "low_stock_grams": LOW_DEFAULT,
        "plate_check": True,
        "plate_margin_percent": DEFAULT_MARGIN,
        "weight_display": "both",
        "show_cart": False,
    }
    for path in (SETTINGS_FILE, OLD_SETTINGS):
        try:
            return {**defaults, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            continue
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
    return parse_weights(get_settings().get("weight_display"))


def cart_on() -> bool:
    return parse_flag(get_settings().get("show_cart"), default=False)


def plate_settings() -> tuple[bool, int]:
    settings = get_settings()
    return (parse_flag(settings.get("plate_check")),
            parse_margin(settings.get("plate_margin_percent")))


def save_settings(settings: dict) -> bool:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return True
    except OSError:
        log.exception("Could not write %s", SETTINGS_FILE)
        return False


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
    return parse_source(get_settings().get("spool_source"))


def source_name() -> str:
    return "Bambu Cloud" if source() == "bambu" else "Spoolman"


def spool_key() -> str:
    return BAMBU_KEY if source() == "bambu" else get_settings().get("spoolman_url", "")


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


def _bambu_post(region: str, path: str, body: dict, host: str = "") -> tuple[dict, list[str]]:
    request = urllib.request.Request(
        (host or BAMBU_HOSTS[region]) + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": BAMBU_UA})
    try:
        with urllib.request.urlopen(request, timeout=BAMBU_TIMEOUT) as resp:
            raw, cookies = resp.read().decode("utf-8", "replace"), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raw = exc.read(2000).decode("utf-8", "replace")
        log.warning("Bambu Cloud %s answered HTTP %s: %s", path, exc.code, raw[:200])
        raise BambuError(_bambu_message(exc.code, raw)) from None
    except urllib.error.URLError as exc:
        raise BambuError(f"Could not reach Bambu Cloud ({exc.reason})") from None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise BambuError("Bambu Cloud sent a reply Spoolio doesn't understand") from None
    return (data if isinstance(data, dict) else {}), cookies


def _send_code(region: str, account: str) -> None:
    _bambu_post(region, CODE_PATH, {"email": account, "type": "codeLogin"})


def _after_login(region: str, account: str, reply: dict, cookies: list[str]) -> dict:
    token = str(reply.get("accessToken") or "")
    for cookie in cookies:
        if not token and cookie.startswith("token="):
            token = cookie[6:].split(";")[0]
    if token:
        _pending.clear()
        AUTH_FILE.write_text(json.dumps({"access_token": token, "region": region, "account": account}),
                             encoding="utf-8")
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass
        _spool_cache.update(url="", time=0.0, spools=[])
        log.info("Signed in to Bambu Cloud (%s)", region)
        return {"ok": True, "step": "done"}
    _pending.update(region=region, account=account)
    if reply.get("loginType") == "verifyCode":
        _send_code(region, account)
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
        code = str(data.get("code") or "").strip()
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
    result = get_bambu_spools() if source() == "bambu" else get_spools(get_settings().get("spoolman_url", ""))
    for sp in result.get("spools") or []:
        sp["rfid_tags"] = sorted(spool_tags(sp))
    return result

PRINTER_SLOTS = {"ams": 4, "ht": 1, "ext": 1}


def parse_printers(value: object) -> list[dict]:
    """Cleaned printers; a spool sits in at most one slot."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    printers = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        units = []
        for unit in (item.get("units") if isinstance(item.get("units"), list) else [])[:8]:
            kind = unit.get("kind") if isinstance(unit, dict) else None
            if kind not in PRINTER_SLOTS or (kind == "ext" and any(u["kind"] == "ext" for u in units)):
                continue
            raw = unit.get("slots") if isinstance(unit.get("slots"), list) else []
            slots: list[str | None] = []
            for index in range(PRINTER_SLOTS[kind]):
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
        printers.append({"id": str(item.get("id") or f"p{len(printers) + 1}")[:32], "name": name, "units": units,
                         "ip": ip if re.fullmatch(r"[A-Za-z0-9.\-]+", ip) else "", "serial": serial})
    return printers


def printers_cfg() -> list[dict]:
    saved = get_settings().get("printers")
    return parse_printers(saved.get(source())) if isinstance(saved, dict) else []


def save_printers(value: object) -> None:
    settings = get_settings()
    saved = settings.get("printers") if isinstance(settings.get("printers"), dict) else {}
    saved[source()] = parse_printers(value)
    settings["printers"] = saved
    save_settings(settings)
    prune_codes()
    _local_sync()


def printer_state() -> dict:
    codes = read_codes()
    printers = [dict(p, has_code=bool(codes.get(_code_key(p["id"])))) for p in printers_cfg()]
    live = any(p["ip"] and p["has_code"] for p in printers)
    return {"printers": printers, "live_ok": live, "usage": usage_state()}


LOCAL_PORT = 8883
LIVE_IDLE = 90.0
LIVE_PING = 30.0
LIVE_RETRY = 15.0
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
        job = {k: report[k] for k in ("gcode_state", "mc_percent", "subtask_name", "gcode_file") if k in report}
        finished = None
        if job:
            state.setdefault("job", {}).update(job)
            finished = usage_track(dev_id, state)
    if finished:
        usage_finish(finished)


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


def live_snapshot() -> dict:
    with _live_lock:
        printers = []
        for dev_id, state in _live["printers"].items():
            units = []
            for unit_id in sorted(state["units"]):
                slots = state["units"][unit_id]
                size = 1 if unit_id >= 128 else 4
                units.append({"id": unit_id, "kind": "ht" if unit_id >= 128 else "ams",
                              "trays": [_live_tray(i, slots.get(i, {})) for i in range(size)]})
            ext = _live_tray(0, state["ext"]) if state["ext"] else None
            printers.append({"id": dev_id, "link": state.get("link", ""), "name": state["name"], "age": round(time.time() - state["time"]),
                             "units": units, "ext": ext})
        return {"links": dict(_live["links"]), "printers": printers}


CODES_FILE = DATA_DIR / "printer_codes.json"


def _code_key(pid: str) -> str:
    return f"{source()}:{pid}"


def read_codes() -> dict:
    try:
        data = json.loads(CODES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Could not read %s", CODES_FILE)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def write_codes(codes: dict) -> None:
    try:
        CODES_FILE.write_text(json.dumps(codes), encoding="utf-8")
        os.chmod(CODES_FILE, 0o600)
    except OSError:
        log.exception("Could not write %s", CODES_FILE)


def save_printer_code(pid: object, code: object) -> None:
    """Store a printer access code privately; never sent back to the page."""
    pid = str(pid or "")[:32]
    if pid not in {p["id"] for p in printers_cfg()}:
        return
    codes = read_codes()
    code = re.sub(r"\s", "", str(code or ""))[:32]
    if code:
        codes[_code_key(pid)] = code
    else:
        codes.pop(_code_key(pid), None)
    write_codes(codes)


def prune_codes() -> None:
    mine = {_code_key(p["id"]) for p in printers_cfg() if p.get("ip")}
    codes = read_codes()
    kept = {k: v for k, v in codes.items() if not k.startswith(f"{source()}:") or k in mine}
    if kept != codes:
        write_codes(kept)


class AuthError(Exception):
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
    ping = time.monotonic()
    while keep():
        packet = _mqtt_read(sock)
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


def _local_session(pid: str, link: dict, stop: threading.Event) -> None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    serial = link["serial"]
    with socket.create_connection((link["ip"], LOCAL_PORT), timeout=8) as raw:
        with context.wrap_socket(raw, server_hostname=link["ip"]) as sock:
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
            log.info("Live link to a printer on the local network connected")
            _mqtt_run(sock, "bblp", link["code"], topics, push, on_report,
                      lambda: _live_active() and not stop.is_set())


def _local_run(pid: str, link: dict, stop: threading.Event) -> None:
    while _live_active() and not stop.is_set():
        _live["links"][pid] = "connecting"
        try:
            _local_session(pid, link, stop)
        except AuthError:
            _live["links"][pid] = "auth"
            log.warning("A printer refused the access code")
            return
        except Exception as exc:
            _live["links"][pid] = "unreachable"
            log.warning("Live link to a printer stopped: %s", exc)
            stop.wait(LIVE_RETRY)
    _live["links"].pop(pid, None)


def _local_sync() -> bool:
    """Match the local connections to the saved printers."""
    codes = read_codes()
    wanted = {}
    for printer in printers_cfg():
        code = codes.get(_code_key(printer["id"]))
        if printer.get("ip") and code:
            wanted[printer["id"]] = {"ip": printer["ip"], "serial": printer["serial"], "code": code,
                                     "name": printer["name"]}
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
        if entry and (entry["thread"].is_alive() or _live["links"].get(pid) == "auth"):
            continue
        stop = threading.Event()
        thread = threading.Thread(target=_local_run, args=(pid, link, stop), daemon=True)
        _local[pid] = {"link": link, "stop": stop, "thread": thread}
        thread.start()
    _live["keep"] = bool(wanted) and usage_mode() != "off" and source() == "spoolman"
    return bool(wanted)


def live_poll() -> dict:
    """Keep live links alive while the Printer tab is open; never blocks."""
    _live["seen"] = time.monotonic()
    _local_sync()
    return live_snapshot()



USAGE_FILE = DATA_DIR / "usage.json"
USAGE_MODES = ("ask", "auto", "off")
USAGE_RECENT = 8
_usage: dict = {"plan": None}
_usage_lock = threading.RLock()


def usage_mode() -> str:
    value = get_settings().get("book_usage")
    return value if value in USAGE_MODES else "ask"


def usage_load() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        log.exception("Could not read %s", USAGE_FILE)
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"pending": data.get("pending") if isinstance(data.get("pending"), list) else [],
            "recent": data.get("recent") if isinstance(data.get("recent"), list) else []}


def usage_save(data: dict) -> None:
    try:
        USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.exception("Could not write %s", USAGE_FILE)


def usage_state() -> dict:
    enabled = source() == "spoolman"
    with _usage_lock:
        data = usage_load()
    return {"enabled": enabled, "mode": usage_mode(),
            "pending": data["pending"] if enabled else [], "recent": data["recent"] if enabled else []}


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
    uids = _tag_ids([tray.get("tag_uid"), tray.get("tray_uuid")])
    found = [sp for sp in spools if not sp.get("archived") and uids & spool_tags(sp)]
    return found[0] if len(found) == 1 else None


def _loaded(state: dict) -> list[dict]:
    """Trays in slicer slot order."""
    trays = []
    for unit_id in sorted(state["units"], key=lambda u: (u >= 128, u)):
        slots = state["units"][unit_id]
        trays += [dict(slots.get(i, {})) for i in range(1 if unit_id >= 128 else 4)]
    if not trays and state["ext"]:
        trays.append(dict(state["ext"]))
    return trays


def usage_track(dev_id: str, state: dict) -> dict | None:
    """Track a printer through a print; returns the run when it ends. Holds _live_lock."""
    job = state.get("job") or {}
    gcode = job.get("gcode_state")
    if not gcode or source() != "spoolman" or not state.get("link") or usage_mode() == "off":
        return None
    pct = max(0, min(100, _int(job.get("mc_percent"), 0)))
    run = state.get("run")
    if run is None:
        if gcode in ("IDLE", "FINISH", "FAILED"):
            state["idle_seen"] = True
        elif gcode in ("PREPARE", "RUNNING") and (state.get("idle_seen") or pct <= 2) and _usage["plan"]:
            state["run"] = {"id": f"{dev_id}-{int(time.time())}", "plan": _usage["plan"]["slots"],
                            "name": str(job.get("subtask_name") or job.get("gcode_file") or "Print")[:80],
                            "trays": _loaded(state), "pct": pct}
        return None
    if gcode in ("PREPARE", "RUNNING", "PAUSE"):
        run["pct"] = max(run["pct"], pct)
        return None
    if gcode not in ("FINISH", "FAILED", "IDLE"):
        return None
    state["run"] = None
    state["idle_seen"] = True
    done = gcode == "FINISH"
    return {**run, "printer": state["name"], "result": "finished" if done else "cancelled",
            "pct": 100 if done else run["pct"]}


def _spool_label(spool: dict) -> str:
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or ""
    return " ".join(part for part in (vendor, filament.get("name") or f"Spool {spool.get('id')}") if part)


def usage_finish(run: dict) -> None:
    """Work out each slot's spool and grams, then ask or book."""
    url = clean_url(get_settings().get("spoolman_url"))
    spools = (plate_spools(url) if url else None) or []
    by_id = {sp.get("id"): sp for sp in spools}
    share = 1.0 if run["result"] == "finished" else run["pct"] / 100
    rows = []
    for item in run["plan"]:
        tray = run["trays"][item["n"] - 1] if 0 < item["n"] <= len(run["trays"]) else None
        spool = match_spool(tray, spools)
        grams = round(item["grams"] * share, 1)
        if grams <= 0:
            continue
        rows.append({"slot": item["n"], "grams": grams, "preset": item["preset"], "material": item["material"],
                     "colour": item["colour"], "spool": spool["id"] if spool else None,
                     "how": "rfid" if spool else "", "label": _spool_label(by_id[spool["id"]]) if spool else "",
                     "state": "open", "error": ""})
    if not rows:
        return
    entry = {"id": run["id"], "printer": run["printer"], "name": run["name"], "result": run["result"],
             "pct": run["pct"], "time": time.time(), "rows": rows}
    with _usage_lock:
        data = usage_load()
        data["pending"] = [e for e in data["pending"] if e.get("id") != entry["id"]] + [entry]
        usage_save(data)
    log.info("A print ended (%s, %d%%): %d slots to book", run["result"], run["pct"], len(rows))
    if usage_mode() == "auto" and run["result"] == "finished":
        usage_book(entry["id"], None)


def spoolman_use(spool_id: int, grams: float) -> None:
    url = f"{clean_url(get_settings().get('spoolman_url'))}/api/v1/spool/{int(spool_id)}/use"
    request = urllib.request.Request(url, data=json.dumps({"use_weight": round(grams, 1)}).encode("utf-8"),
                                     method="PUT", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass
    _spool_cache["time"] = 0.0


def usage_book(entry_id: str, chosen: list | None) -> None:
    """Book the open rows to their spools; chosen holds manual picks."""
    wanted = {}
    for item in chosen if isinstance(chosen, list) else []:
        if isinstance(item, dict):
            wanted[_int(item.get("slot"))] = item
    url = clean_url(get_settings().get("spoolman_url"))
    by_id = {sp.get("id"): sp for sp in (plate_spools(url) if url else None) or []}
    with _usage_lock:
        data = usage_load()
        entry = next((e for e in data["pending"] if e.get("id") == entry_id), None)
        if entry is None:
            return
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
            except urllib.error.HTTPError as exc:
                row.update(state="error", error=f"Spoolman refused it (HTTP {exc.code})")
                continue
            except urllib.error.URLError as exc:
                row.update(state="error", error=f"Could not reach Spoolman ({exc.reason})")
                continue
            except Exception as exc:
                row.update(state="error", error=str(exc))
                continue
            label = _spool_label(by_id[spool]) if spool in by_id else f"spool {spool}"
            row.update(state="booked", error="", spool=spool, grams=round(grams, 1), label=label)
            data["recent"].insert(0, {"time": time.time(), "printer": entry["printer"], "name": entry["name"],
                                      "grams": row["grams"], "label": label})
            log.info("Booked %.1f g to Spoolman spool %s", grams, spool)
        if all(row["state"] == "booked" for row in entry["rows"]):
            data["pending"].remove(entry)
        data["recent"] = data["recent"][:USAGE_RECENT]
        usage_save(data)


def usage_action(data: dict) -> dict:
    kind = data.get("type")
    if kind == "usage_mode":
        settings = get_settings()
        settings["book_usage"] = data.get("mode") if data.get("mode") in USAGE_MODES else "ask"
        save_settings(settings)
        _local_sync()
    elif kind == "usage_book":
        usage_book(str(data.get("run") or ""), data.get("rows"))
    elif kind == "usage_dismiss":
        with _usage_lock:
            store = usage_load()
            store["pending"] = [e for e in store["pending"] if e.get("id") != str(data.get("run") or "")]
            usage_save(store)
    return usage_state()


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


def plate_spools(url: str) -> list[dict] | None:
    """The spool list, from the cache when it is fresh; None if Spoolman can't be reached."""
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
    flex-shrink: 0; padding-top: 1px;
  }
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
  .spool-cart svg { width: 13px; height: 13px; }
  .spool-bar-track {
    height: 5px; border-radius: 3px; margin: 6px 0 5px;
    background: rgba(127, 127, 127, 0.25); overflow: hidden;
  }
  .spool-bar-fill { height: 100%; border-radius: 3px; }
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
  .tabs { display: inline-flex; margin: 0 0 12px; border: 1px solid var(--orca-border); border-radius: 8px; overflow: hidden; }
  .tabs[hidden], #page-spools[hidden], #page-printer[hidden] { display: none; }
  .tabs button { background: transparent; color: var(--orca-fg); border: 0; border-right: 1px solid var(--orca-border); border-radius: 0; padding: 7px 20px; font-weight: 600; cursor: pointer; }
  .tabs button:last-child { border-right: 0; }
  .tabs button.on { background: var(--orca-accent); color: var(--orca-accent-fg); }
  #tab-dot { display: inline-block; width: 8px; height: 8px; margin-left: 7px; border-radius: 50%; background: #e0a030; }
  #tab-dot[hidden] { display: none; }
  .ams { margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg) 96%); }
  .ams-h { color: var(--orca-fg); display: flex; align-items: baseline; gap: 10px; padding: 10px 14px; font-weight: 700; border-bottom: 1px solid var(--orca-border); }
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
  .plate.on-printer > .divider:last-child { display: none; }
  .live-dot { display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 50%; background: #3fb950; }
  .live-dot.stale { background: #e0a030; }
  .usage { margin: 0 0 14px; border: 1px solid var(--orca-border); border-radius: 10px; background: color-mix(in srgb, var(--orca-fg) 4%, var(--orca-bg) 96%); }
  .usage-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 10px 14px; color: var(--orca-fg); }
  .usage-bar b { font-weight: 700; }
  .usage-bar .sub { color: var(--orca-muted); font-size: 12px; margin-right: auto; }
  .usage select, .u-spool select { background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px; max-width: 100%; }
  .u-card { border-top: 1px solid var(--orca-border); padding: 10px 14px; }
  .u-h { color: var(--orca-fg); margin-bottom: 6px; }
  .u-h .sub { color: var(--orca-muted); font-size: 12px; }
  .u-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 0; color: var(--orca-fg); }
  .u-row .d { width: 14px; height: 14px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.15); flex: none; }
  .u-slot { color: var(--orca-muted); min-width: 110px; }
  .u-spool { flex: 1 1 220px; min-width: 0; }
  .u-g { width: 74px; background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 13px; }
  .u-chip { margin-left: 8px; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 99px; background: rgba(63, 185, 80, 0.18); color: #3fb950; }
  .u-chip.warn { background: rgba(210, 153, 34, 0.22); color: #e0a030; }
  .u-err { flex-basis: 100%; color: #d9534f; font-size: 12px; }
  .u-foot { display: flex; gap: 8px; margin-top: 8px; }
  .u-recent { padding: 4px 14px 10px; font-size: 12px; color: var(--orca-muted); }
  #usage-slot:empty { display: none; }
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
    <button data-tab="printer">Printer<span id="tab-dot" hidden></span></button>
  </div>
  <div id="page-spools">
  <div class="listbar">
    <div id="status">Loading...</div>
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
    <div id="usage-slot"></div>
    <div id="ams-list"></div>
    <div id="plate-slot"></div>
  </div>

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
    let usage = { enabled: false, mode: "ask", pending: [], recent: [] };
    let usageEdits = {};
    let usageBooking = false;
    let plateAttn = false;
    const usageSlot = document.getElementById("usage-slot");
    let linkOpen = "";
    let liveData = { status: "off", printers: [] };
    let liveKey = "";
    let picking = null;
    const KINDS = { ams: 4, ht: 1, ext: 1 };
    const RANK = { ams: 0, ht: 1, ext: 2 };
    try { if (localStorage.getItem("spoolio.tab") === "printer") tab = "printer"; } catch (err) {}

    function esc(v) {
      return String(v).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    function savePrinters() {
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
      tabDot.hidden = !(plateAttn || (usage.enabled && usage.pending.length > 0));
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
        who = esc(r.label) + '<span class="u-chip">Matched by RFID</span>';
      } else {
        who = '<select data-usage="spool">' + spoolOptions(r).replace('value="' + esc(edit.spool || "") + '"', 'value="' + esc(edit.spool || "") + '" selected') + '</select>' +
          '<span class="u-chip warn">Not Identified</span>';
      }
      const input = r.state === "booked" ? "" : '<input class="u-g" data-usage="grams" type="number" min="0" step="0.1" value="' + esc(grams) + '"> g';
      return '<div class="u-row" data-slot="' + r.slot + '"><span class="d" style="background:' + esc(colour) + '"></span>' +
        '<span class="u-slot">Slot ' + r.slot + (r.material ? " \u00b7 " + esc(r.material) : "") + '</span>' +
        '<span class="u-spool">' + who + '</span>' + input +
        (r.error ? '<div class="u-err">' + esc(r.error) + '</div>' : "") + '</div>';
    }

    function renderUsage() {
      syncDot();
      if (!usage.enabled || (!liveOn && !usage.pending.length && !usage.recent.length)) { usageSlot.innerHTML = ""; return; }
      const mode = '<label>When a Print Finishes <select data-usage="mode">' + [["ask", "Ask Me Before Booking"], ["auto", "Book Automatically"], ["off", "Do Not Track"]].map(function (m) {
        return '<option value="' + m[0] + '"' + (usage.mode === m[0] ? " selected" : "") + '>' + m[1] + '</option>';
      }).join("") + '</select></label>';
      const cards = usage.pending.map(function (e) {
        const result = e.result === "finished" ? "finished" : "stopped at " + e.pct + "%";
        return '<div class="u-card" data-run="' + esc(e.id) + '"><div class="u-h"><b>' + esc(e.name) + '</b> <span class="sub">' + esc(e.printer) + ' \u00b7 ' + result + ' \u00b7 ' + usageTime(e.time) + '</span></div>' +
          e.rows.map(function (r) { return usageRow(e, r); }).join("") +
          '<div class="u-foot"><button class="sbtn primary" data-usage="book"' + (usageBooking ? " disabled" : "") + '>' + (usageBooking ? "Booking..." : "Book Usage") + '</button>' +
          '<button class="sbtn ghost" data-usage="dismiss">Dismiss</button></div>' +
          (e.result === "finished" ? "" : '<div class="ams-note" style="margin: 6px 0 0">The print did not finish, so the grams are an estimate from its progress.</div>') + '</div>';
      }).join("");
      const recent = usage.recent.slice(0, 3).map(function (x) {
        return '<div class="u-recent">Booked ' + usageGrams(x.grams) + ' to ' + esc(x.label) + ' \u00b7 ' + esc(x.printer) + ' \u00b7 ' + usageTime(x.time) + '</div>';
      }).join("");
      usageSlot.innerHTML = '<div class="usage"><div class="usage-bar"><b>Filament Usage</b><span class="sub">Booked to Spoolman. Spools are matched by RFID tag.</span>' + mode + '</div>' + cards + recent + '</div>';
    }

    function setUsage(u, done) {
      const was = usage.recent.length ? usage.recent[0].time : 0;
      usage = u;
      Object.keys(usageEdits).forEach(function (id) {
        if (!usage.pending.some(function (e) { return e.id === id; })) delete usageEdits[id];
      });
      if (done && usageBooking) { usageBooking = false; orca.postMessage({ type: "refresh" }); }
      const at = document.activeElement;
      if (at && usageSlot.contains(at) && at.matches('[data-usage="spool"], [data-usage="grams"]')) syncDot(); else renderUsage();
    }

    usageSlot.addEventListener("change", function (e) {
      const kind = e.target.dataset.usage;
      const card = e.target.closest(".u-card");
      const row = e.target.closest(".u-row");
      if (kind === "mode") {
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
      const btn = e.target.closest("button[data-usage]");
      const card = e.target.closest(".u-card");
      if (!btn || !card) return;
      if (btn.dataset.usage === "dismiss") {
        orca.postMessage({ type: "usage_dismiss", run: card.dataset.run });
      } else if (btn.dataset.usage === "book" && !usageBooking) {
        const rows = [...card.querySelectorAll(".u-row")].map(function (row) {
          const sel = row.querySelector('select[data-usage="spool"]');
          const g = row.querySelector('input[data-usage="grams"]');
          if (!g) return null;
          const out = { slot: Number(row.dataset.slot), grams: Number(g.value) };
          if (sel && sel.value) out.spool = sel.value;
          return out;
        }).filter(Boolean);
        usageBooking = true;
        renderUsage();
        orca.postMessage({ type: "usage_book", run: card.dataset.run, rows: rows });
      }
    });

    function liveByLink(id) {
      return liveData.printers.find(function (x) { return x.link === id; }) || null;
    }

    function linkStatus(p) {
      if (!p.ip || !p.has_code) return "";
      const lv = liveByLink(p.id);
      const st = (liveData.links || {})[p.id];
      if (lv && st === "live") {
        return '<span class="live-dot' + (lv.age > 180 ? " stale" : "") + '"></span>Live \u00b7 updated ' + liveWhen(lv);
      }
      if (st === "auth") return "Wrong access code";
      if (st === "unreachable") return "Can't reach " + esc(p.ip) + " - is the printer on and in LAN mode?";
      if (st === "waiting") return "Connected, waiting for the printer to report";
      return "Connecting to " + esc(p.ip) + "...";
    }

    function linkForm(p) {
      return '<div class="linkform">' +
        '<label>IP Address<input data-f="ip" value="' + esc(p.ip || "") + '" placeholder="192.168.1.50" autocomplete="off"></label>' +
        '<label>Serial Number<input data-f="serial" value="' + esc(p.serial || "") + '" placeholder="Optional" autocomplete="off"></label>' +
        '<label>Access Code<input data-f="code" type="password" autocomplete="off" placeholder="' + (p.has_code ? "Saved - leave blank to keep" : "Shown on the printer") + '"></label>' +
        '<div class="bar"><button class="sbtn" data-act="link-save" data-p="' + esc(p.id) + '">Save</button>' +
        '<button class="sbtn ghost" data-act="link-cancel" data-p="' + esc(p.id) + '">Cancel</button>' +
        (p.ip ? '<button class="sbtn ghost" data-act="link-remove" data-p="' + esc(p.id) + '">Remove Link</button>' : "") + '</div>' +
        '<div class="ams-note" style="flex-basis: 100%; margin: 0">The IP address and access code are in the printer&#39;s network settings, with LAN mode on. ' +
        'Spoolio keeps the access code on this computer and only reads from the printer.</div></div>';
    }

    function liveCard(t) {
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

    function liveUnits(lv) {
      let ams = 0;
      const unitHtml = function (label, trays, ext) {
        return '<div class="ams-unit">' + label + '</div><div class="ams-grid">' + trays.map(function (t, i) {
          return '<div class="ams-cell"><div class="ams-n">' + (ext ? "E" : i + 1) + '</div>' + liveCard(t) + '</div>';
        }).join("") + '</div>';
      };
      let units = lv.units.map(function (u) {
        if (u.kind === "ht") return unitHtml("AMS HT", u.trays, false);
        ams += 1;
        return unitHtml("AMS " + ams, u.trays, false);
      }).join("");
      if (lv.ext) units += unitHtml("External Spool", [lv.ext], true);
      return units || '<div class="pnote">Waiting for the printer to report its slots...</div>';
    }

    function liveWhen(lv) {
      return lv.age < 10 ? "just now" : lv.age < 120 ? lv.age + " s ago" : Math.round(lv.age / 60) + " min ago";
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
      return "External Spool";
    }

    function manualCell(p, ui, si, byId) {
      const u = p.units[ui];
      const id = u.slots[si];
      const s = id ? byId.get(String(id)) : null;
      const where = ' data-p="' + esc(p.id) + '" data-u="' + ui + '" data-s="' + si + '"';
      let card;
      if (s) {
        card = spoolRowHtml(s);
      } else if (id) {
        card = plainCard("Spool not found", "It may have been deleted or archived", "", "ams-empty");
      } else {
        card = plainCard("+ Assign Spool", "No spool assigned", ' data-act="assign"' + where, "ams-empty ams-assign");
      }
      const x = id ? '<button class="ams-x" data-act="clear"' + where + ' title="Remove from slot">\u2715</button>' : "";
      const open = picking && picking.p === p.id && picking.u === ui && picking.s === si && !id;
      return '<div class="ams-cell"><div class="ams-n">' + (u.kind === "ext" ? "E" : si + 1) + '</div>' +
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
        const color = f.color_hex ? "#" + f.color_hex.replace(/^#/, "") : "#888";
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

    function manualPanel(p, byId) {
      let total = 0;
      let used = 0;
      const units = p.units.map(function (u, ui) {
        total += u.slots.length;
        used += u.slots.filter(Boolean).length;
        const label = unitLabel(p, u);
        const key = "u:" + p.id + ":" + ui;
        const rm = '<button class="sbtn ghost' + (armed === key ? " armed" : "") + '" data-act="remove-unit" data-p="' + esc(p.id) +
          '" data-u="' + ui + '">' + (armed === key ? "Click Again to Remove" : "Remove " + label) + '</button>';
        const cells = u.slots.map(function (_, si) { return manualCell(p, ui, si, byId); }).join("");
        return '<div class="ams-unit"><span>' + label + '</span>' + rm + '</div><div class="ams-grid">' + cells + '</div>';
      }).join("");
      const hasExt = p.units.some(function (u) { return u.kind === "ext"; });
      const pk = "p:" + p.id;
      const name = renaming === p.id
        ? '<input class="pname-in" data-p="' + esc(p.id) + '" value="' + esc(p.name) + '" maxlength="40">'
        : '<button class="pname" data-act="rename" data-p="' + esc(p.id) + '" title="Rename">' + esc(p.name) + ' \u270e</button>';
      const lvl = liveByLink(p.id);
      const live = !!lvl && (liveData.links || {})[p.id] === "live";
      let sub = total ? used + " of " + total + " slots assigned" : "Add an AMS or external spool to start";
      const ls = linkStatus(p);
      if (ls) sub = ls;
      const bar = '<div class="bar">' +
        (live ? "" : '<button class="sbtn primary" data-act="link" data-p="' + esc(p.id) + '">Live Link</button>') +
        (live ? "" : '<button class="sbtn" data-act="add-unit" data-kind="ams" data-p="' + esc(p.id) + '">+ AMS</button>' +
        '<button class="sbtn" data-act="add-unit" data-kind="ht" data-p="' + esc(p.id) + '">+ AMS HT</button>') +
        (hasExt || live ? "" : '<button class="sbtn" data-act="add-unit" data-kind="ext" data-p="' + esc(p.id) + '">+ External Spool</button>') +
        '<button class="sbtn' + (armed === pk ? " armed" : "") + '" data-act="remove-printer" data-p="' + esc(p.id) + '">' +
        (armed === pk ? "Click Again to Remove" : "Remove Printer") + '</button></div>';
      return '<div class="ams"><div class="ams-h">' + name + '<span class="sub">' + sub + '</span>' + bar + '</div>' +
        (linkOpen === p.id ? linkForm(p) : "") +
        '<div class="ams-body">' + (live ? liveUnits(lvl) : units) +
        (live ? '' : '<div class="ams-note">A representative view only: built from the spools you&#39;ve assigned. Use Live Link to read the printer itself.</div>') + '</div></div>';
    }

    function renderPrinters() {
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
      if (liveOn && usage.enabled && tab !== "printer") orca.postMessage({ type: "live" });
    }, 30000);

    function updateTabs() {
      const onPrinter = showTab && tab === "printer";
      tabsEl.hidden = !showTab;
      document.getElementById("page-spools").hidden = onPrinter;
      document.getElementById("page-printer").hidden = !onPrinter;
      if (showTab) plateSlot.appendChild(plateEl); else listEl.parentNode.insertBefore(plateEl, listEl);
      plateEl.classList.toggle("on-printer", showTab);
      if (onPrinter) pollLive();
      tabsEl.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("on", b.dataset.tab === (onPrinter ? "printer" : "spools"));
      });
    }

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
      } else if (p && act === "add-unit") {
        const kind = el.dataset.kind;
        if (KINDS[kind] && !(kind === "ext" && p.units.some(function (u) { return u.kind === "ext"; }))) {
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
      } else if (p && act === "link") {
        linkOpen = linkOpen === p.id ? "" : p.id;
      } else if (p && act === "link-cancel") {
        linkOpen = "";
      } else if (p && (act === "link-save" || act === "link-remove")) {
        const form = amsList.querySelector(".linkform");
        const val = function (f) { const i = form && form.querySelector('[data-f="' + f + '"]'); return i ? i.value.trim() : ""; };
        const code = act === "link-save" ? val("code") : "";
        p.ip = act === "link-save" ? val("ip") : "";
        p.serial = act === "link-save" ? val("serial") : "";
        if (act === "link-remove") p.has_code = false;
        savePrinters();
        if (code || act === "link-remove") { orca.postMessage({ type: "printer_code", id: p.id, code: code }); if (code) p.has_code = true; }
        linkOpen = "";
        liveOn = liveOn || !!(p.ip && p.has_code);
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

    function spoolRowHtml(s) {
      const filament = s.filament || {};
      const vendor = filament.vendor || {};
      const color = filament.color_hex ? "#" + filament.color_hex.replace(/^#/, "") : "#888";
      const name = filament.name || ("Spool #" + s.id);
      const material = filament.material || "";
      const vendorName = vendor.name || "";
      const subtitleParts = [];
      if (material) subtitleParts.push(material);
      if (filament.color_hex) subtitleParts.push("#" + filament.color_hex.replace(/^#/, "").toUpperCase());
      if (typeof filament.diameter === "number") subtitleParts.push(filament.diameter + "mm");
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
      return (
        '<div class="spool-card" title="' + tip.replace(/"/g, "&quot;") + '">' +
          tagHtml +
          '<div class="spool-body">' +
            '<div class="spool-swatch-col">' +
              '<div class="spool-swatch" style="background:' + color + '"></div>' +
              rfidHtml +
            '</div>' +
            '<div class="spool-main">' +
              '<div class="spool-top">' +
                '<div class="spool-title">' + name + dupeHtml + '</div>' +
                '<div class="spool-weight' + (tint ? ' low" style="color:' + tint : '') + '">' + wHtml + cartHtml + '</div>' +
              '</div>' +
              '<div class="spool-bar-track" title="' + percentTitle + '">' +
                '<div class="spool-bar-fill" style="width:' + (percent === null ? 0 : percent) + '%;background:' + color + '"></div>' +
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

    function render(payload) {
      if (!payload.ok) {
        statusEl.textContent = payload.error || "Unknown error";
        statusEl.className = "error";
        listEl.innerHTML = "";
        return;
      }
      allSpools = payload.spools || [];
      computeDupes();
      if (typeof payload.low_stock_grams === "number") lowStockGrams = payload.low_stock_grams;
      if (typeof payload.weight_display === "string") wMode = payload.weight_display;
      if (typeof payload.show_cart === "boolean") cart = payload.show_cart;
      if (Array.isArray(payload.printers)) cfg = payload.printers;
      if (typeof payload.show_printer_tab === "boolean") showTab = payload.show_printer_tab;
      liveOn = payload.live_ok === true;
      if (payload.usage) setUsage(payload.usage);
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
      } else if (data && data.type === "live") {
        const key = JSON.stringify(data.live);
        liveData = data.live;
        if (data.usage) setUsage(data.usage);
        if (key !== liveKey && !picking && !renaming && !linkOpen) { liveKey = key; renderPrinters(); }
      }
    });

    listEl.addEventListener("click", function (e) {
      const btn = e.target.closest(".spool-cart");
      if (btn) {
        orca.postMessage({ type: "order", query: decodeURIComponent(btn.dataset.q) });
      }
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

    setInterval(function () {
      orca.postMessage({ type: "refresh" });
    }, __REFRESH_MS__);
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
  <div class="hint">OrcaSlicer also has to run the check: switch on Spoolio Filament Check in your process settings, under <span style="white-space: nowrap">Others &gt; Slicing Pipeline Plugin</span>.</div><!--/check-->

  <div class="divider spaced"></div>

  <!--weights--><h3><span class="emoji">\u2696\ufe0f</span>Configure Spool Weights</h3>
  <div class="field-label">Show the remaining weight on spool cards as:</div>
  <div class="seg" id="weight-seg">
    <label><input type="radio" name="weight-display" value="grams">Grams</label>
    <label><input type="radio" name="weight-display" value="percent">Percentage</label>
    <label><input type="radio" name="weight-display" value="both">Both</label>
  </div>
  <div class="ws-card">
    <div class="ws-swatch"></div>
    <div class="ws-main">
      <div class="ws-top"><span class="ws-name">PLA Matte - Black</span><span id="weight-sample"></span></div>
      <div class="ws-bar"><i></i></div>
      <div class="ws-sub">PLA \u00b7 #1A1A1A \u00b7 1.75mm</div>
    </div>
  </div>
  <div class="hint">The percentage is what is left of the spool's original weight.</div><!--/weights-->

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
          setBambu("Enter your Bambu Lab verification code", "pending");
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


def settings_html(current_url: str, spoolman_info: dict, low_stock_grams: float,
                  plate_check: bool = True, plate_margin: int = DEFAULT_MARGIN,
                  weight: str = "both", cart: bool = False, source: str = "spoolman") -> str:
    """``spoolman_info`` describes the saved URL, not whatever is typed in the field."""
    verified_url = clean_url(current_url) if spoolman_info.get("ok") else ""
    if spoolman_info.get("ok"):
        status_text = f"Connected - Spoolman v{spoolman_info['version']}"
        status_class = "ok"
    elif spoolman_info.get("pending"):
        status_text = spoolman_info.get("error", "Checking connection...")
        status_class = "pending"
    else:
        status_text = spoolman_info.get("error", "Not connected")
        status_class = "error"
    return _fill(
        SETTINGS_PAGE,
        LOGO=LOGO_IMG,
        LOGODATA=LOGO_DATA_URI,
        FONT=FONT_STACK,
        PLUGIN_NAME="Spoolio",
        VERSION=PLUGIN_VERSION,
        DEFAULT_URL=html.escape(DEFAULT_SPOOLMAN_URL),
        URL=html.escape(current_url or DEFAULT_SPOOLMAN_URL),
        LOW_STOCK=low_stock_grams,
        PLATE_CHECKED="checked" if plate_check else "",
        PLATE_MARGIN=plate_margin,
        WEIGHT_MODE=json.dumps(parse_weights(weight)),
        CART_CHECKED="checked" if cart else "",
        LOG_PATH=html.escape(str(LOG_FILE)),
        STATUS_TEXT=html.escape(status_text),
        STATUS_CLASS=status_class,
        # "</" is escaped so a URL can't close the script tag.
        VERIFIED_URL=json.dumps(verified_url).replace("</", "<\\/"),
        SOURCE=json.dumps(parse_source(source)),
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


# Wide windows put each setting beside its guide, preview or diagnostics in a grid. Narrow
# windows stack the cells, guide first.
TAB_SETTINGS_CSS = """
  #view-settings { margin: 0 auto; }
  #view-settings .settings-grid {
    display: grid; grid-template-columns: minmax(0, 9fr) minmax(0, 11fr);
  }
  #view-settings .cell { padding: 14px 0; }
  /* Fixed heading height, content centred: paired headings stay level whatever the emoji size. */
  #view-settings .cell h3 { display: flex; align-items: center; height: 30px; }
  #view-settings .cell-guide, #view-settings .cell-preview, #view-settings .cell-weights, #view-settings .cell-diag {
    grid-column: 1; padding-right: 32px;
  }
  #view-settings .cell-server, #view-settings .cell-reorder, #view-settings .cell-check {
    grid-column: 2; padding-left: 32px;
  }
  #view-settings .cell-guide, #view-settings .cell-server { grid-row: 1; padding-top: 0; }
  #view-settings .cell-preview, #view-settings .cell-reorder { grid-row: 2; }
  #view-settings .cell-weights, #view-settings .cell-check { grid-row: 3; }
  #view-settings .cell-diag { grid-row: 4; padding-bottom: 0; }
  #view-settings .footer { border-top: none; }
  #view-settings .cell-guide h3 { margin-bottom: 16px; }
  #view-settings .cell-guide .step:last-child { margin-bottom: 0; }
  #view-settings .cell-diag .about { margin-top: 0; }
  #view-settings .about .version-row { margin-bottom: 8px; }
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
    #view-settings .settings-grid { grid-template-columns: minmax(0, 1fr); }
    #view-settings .cell { grid-column: auto; grid-row: auto; padding: 14px 0; }
    #view-settings .cell-guide { padding-top: 0; }
  }
"""

TAB_PREVIEW_HTML = """
    <h3><span class="emoji">📦</span>Filament Reorder Preview</h3>
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

  handlers.push(function (data) {
    if (!data || data.type !== "show_view") return;
    var settings = data.view === "settings";
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
    document.getElementById("update-status").textContent = "";
  });
})();
"""


def tab_html(initial_view: str, current_url: str, low_stock_grams: float,
             plate_check: bool = True, plate_margin: int = DEFAULT_MARGIN,
             weight: str = "both", cart: bool = False, source: str = "spoolman") -> str:
    """The spool list and Settings as two views of one page, under a shared header.

    Built from the standalone pages, which SpoolioWindow still shows as separate windows.
    """
    main_css, main_body, main_js = _split_page(main_html())
    settings_css, settings_body, settings_js = _split_page(settings_html(
        current_url, {"ok": False, "pending": True}, low_stock_grams, plate_check, plate_margin,
        weight, cart, source,
    ))
    main_body, main_header = _take_header(main_body)
    tabs_at = main_body.index('<div id="tabs"')
    tabs_end = main_body.index("</div>", tabs_at) + len("</div>")
    tabs_bar = main_body[tabs_at:tabs_end]
    main_body = main_body[:tabs_at] + main_body[tabs_end:]
    settings_body, settings_header = _take_header(settings_body)
    on_main = initial_view == "main"
    settings_button = _button(main_header, "settings-btn")
    feedback_button = _button(settings_header, "feedback")
    steps, reorder = _section(settings_body, "steps"), _section(settings_body, "reorder")
    check, server = _section(settings_body, "check"), _section(settings_body, "server")
    about = _section(settings_body, "about")
    weights = _section(settings_body, "weights")
    footer = _section(settings_body, "footer")
    # The version and update check move out of the footer into the Diagnostics section.
    version_row = re.search(r'<div class="version-row">.*?</div>', footer, re.S).group(0)
    footer = footer.replace(version_row, "")
    about = about.replace('<div class="logpath">', version_row + '<div class="logpath">', 1)
    wrench = "\U0001F6E0\uFE0F"
    settings_view = f"""<div class="settings-grid">
  <div class="cell cell-guide"><h3><span class="emoji">🚀</span>Getting Started</h3>{steps}</div>
  <div class="cell cell-server">{server}</div>
  <div class="cell cell-reorder">{reorder}</div>
  <div class="cell cell-preview">{TAB_PREVIEW_HTML}</div>
  <div class="cell cell-check">{check}</div>
  <div class="cell cell-weights">{weights}</div>
  <div class="cell cell-diag"><h3><span class="emoji">{wrench}</span>Diagnostics</h3>{about}</div>
</div>
{footer}"""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="icon" href="{LOGO_DATA_URI}">
<style>
  html, body {{ height: 100%; margin: 0; background: var(--orca-bg); overscroll-behavior-x: none; }}
  body {{ box-sizing: border-box; padding-top: 76px; }}
  .view-hidden {{ display: none !important; }}
  #view-settings {{ min-height: 100%; max-width: 1100px; }}
{_fill(TAB_HEADER_CSS, FONT=FONT_STACK)}
{_scope_css(main_css, "#view-main")}
{_scope_css(settings_css, "#view-settings")}
{TAB_SETTINGS_CSS}
</style>
</head>
<body>
<div id="app-header">
  {LOGO_IMG}<h2>Spoolio</h2>
  <span id="tabs-main" class="{_hidden(not on_main)}">{tabs_bar}</span>
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
</body>
</html>
"""


class SpoolioWindow(orca.script.ScriptPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        self._panel = None
        self._settings_window = None
        self._verified_url = ""
        self._verified_bambu = False
        self._release_url = ""
        self._refreshing = False
        self._testing = False
        self._checking_update = False

    def get_name(self):
        return PLUGIN_NAME

    def _spoolman_url(self):
        return get_settings().get("spoolman_url", "")

    def _save_settings(self, url, low_stock, plate_check=True,
                       plate_margin=DEFAULT_MARGIN, weight="both", cart=False, src="spoolman"):
        settings = get_settings()
        settings["spool_source"] = parse_source(src)
        if settings["spool_source"] == "spoolman":
            settings["spoolman_url"] = url
        settings["low_stock_grams"] = parse_low(low_stock)
        settings["plate_check"] = parse_flag(plate_check)
        settings["plate_margin_percent"] = parse_margin(plate_margin)
        settings["weight_display"] = parse_weights(weight)
        settings["show_cart"] = parse_flag(cart, default=False)
        if save_settings(settings):
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
        self._open_panel()
        self._push_data()

    def _open_settings(self):
        if self._settings_window is not None and self._settings_window.is_open():
            return
        saved_url = self._spoolman_url()
        self._settings_window = orca.host.ui.create_window(
            html=settings_html(
                saved_url,
                spoolman_info={"ok": False, "pending": True},
                source=source(),
                low_stock_grams=parse_low(get_settings().get("low_stock_grams")),
                plate_check=plate_settings()[0],
                plate_margin=plate_settings()[1],
                weight=weight_mode(),
                cart=cart_on(),
            ),
            title=f"{PLUGIN_NAME} - Settings & About",
            width=SETTINGS_SIZE[0],
            height=SETTINGS_SIZE[1],
            on_message=self._on_settings,
            on_close=self._on_settings_close,
        )

    def _check_connection(self, url, window, src=None):
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
            if window.is_open():
                window.post({"type": "test_result", "source": src, "url": url, **info})

        threading.Thread(target=worker, daemon=True).start()

    def _bambu_sign_in(self, data, window):
        if data.get("action") == "sign_out":
            self._verified_bambu = False

        def worker():
            result = bambu_sign_in(data)
            if window.is_open():
                window.post({"type": "bambu_auth_result", **result})

        threading.Thread(target=worker, daemon=True).start()

    def _on_settings(self, data):
        msg_type = data.get("type") if isinstance(data, dict) else None
        if msg_type == "ready":
            # Only check the connection once the page confirms its message
            # listener is registered, so the result can never arrive too early.
            if self._settings_window is not None:
                self._check_connection(self._spoolman_url(), self._settings_window)
        elif msg_type == "test":
            url = (data.get("url") or "").strip()
            if self._settings_window is not None:
                self._check_connection(url, self._settings_window, data.get("source"))
        elif msg_type == "save":
            # The page already gates Save; enforce it here too.
            url = (data.get("url") or "").strip()
            src = parse_source(data.get("source"))
            verified = self._verified_bambu if src == "bambu" else clean_url(url) == self._verified_url
            if not verified:
                if self._settings_window is not None:
                    self._settings_window.post({
                        "type": "test_result",
                        "source": src,
                        "ok": False,
                        "url": url,
                        "error": "Test the connection successfully before saving.",
                    })
                return
            saved = self._save_settings(url, data.get("low_stock"), data.get("plate_check"),
                                        data.get("plate_margin"), data.get("weight_display"),
                                        data.get("show_cart"), src)
            if saved:
                self._after_save()
                if self._settings_window is not None:
                    self._settings_window.close()
        elif msg_type == "bambu_auth":
            if self._settings_window is not None:
                self._bambu_sign_in(data, self._settings_window)
        elif msg_type == "feedback":
            open_url(FEEDBACK_URL)
        elif msg_type == "check_update":
            if self._checking_update or self._settings_window is None:
                return
            self._checking_update = True
            window = self._settings_window

            def worker():
                result = get_release()
                self._checking_update = False
                if result["ok"]:
                    self._release_url = result["url"]
                    result["newer"] = is_newer(result["version"], PLUGIN_VERSION)
                if window.is_open():
                    window.post({"type": "update_result", **result})

            threading.Thread(target=worker, daemon=True).start()
        elif msg_type == "release" and self._release_url.startswith("https://"):
            open_url(self._release_url)
        elif msg_type == "cancel" and self._settings_window is not None:
            self._settings_window.close()

    def _on_settings_close(self):
        self._settings_window = None

    def on_unload(self):
        # Without this, open windows would outlive OrcaSlicer's shutdown.
        log.info("Unloading")
        if self._panel is not None and self._panel.is_open():
            self._panel.close()
        if self._settings_window is not None and self._settings_window.is_open():
            self._settings_window.close()

    def on_message(self, data):
        msg_type = data.get("type") if isinstance(data, dict) else None
        if msg_type in ("ready", "refresh"):
            self._push_data()
        elif msg_type == "settings":
            self._open_settings()
        elif msg_type == "order":
            open_search(data.get("query"))
        elif msg_type == "printers":
            save_printers(data.get("printers"))
        elif msg_type == "live":
            self._panel.post({"type": "live", "live": live_poll(), "usage": usage_state()})
        elif msg_type == "printer_code":
            save_printer_code(data.get("id"), data.get("code"))
        elif msg_type in ("usage_mode", "usage_book", "usage_dismiss"):
            threading.Thread(target=lambda: self._panel.post({"type": "usage", "usage": usage_action(data)}), daemon=True).start()

    def _push_data(self):
        if self._panel is None or not self._panel.is_open():
            return
        url = self._spoolman_url()
        if source() == "spoolman" and not url:
            self._panel.post({
                "type": "spools",
                "ok": False,
                "error": "No Spoolman URL configured yet - click Settings to set one.",
            })
            return
        if self._refreshing:
            return
        self._refreshing = True
        _local_sync()
        panel = self._panel
        low_stock_grams = parse_low(get_settings().get("low_stock_grams"))

        def worker():
            result = load_spools()
            self._refreshing = False
            if panel.is_open():
                panel.post({"type": "spools", "low_stock_grams": low_stock_grams,
                            "weight_display": weight_mode(), "show_cart": cart_on(), **printer_state(), **result})

        threading.Thread(target=worker, daemon=True).start()

    def _on_panel_close(self):
        self._panel = None

    def _open_panel(self):
        if self._panel is not None and self._panel.is_open():
            self._push_data()
            return
        self._panel = orca.host.ui.create_window(
            html=main_html(),
            title=PLUGIN_NAME,
            width=MAIN_SIZE[0],
            height=MAIN_SIZE[1],
            on_message=self.on_message,
            on_close=self._on_panel_close,
        )

    def _open(self):
        if not configured():
            self._open_settings()
            return
        self._open_panel()

    def on_load(self):
        log.info(
            "%s %s loaded (Python %s, %s)",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            platform.python_version(),
            platform.platform(),
        )
        self._open()

    def execute(self):
        self._open()
        return orca.ExecutionResult.success(f"Opened {PLUGIN_NAME}")



class _SettingsView:
    """Stands in for a settings window when Settings is a view inside the tab.

    Same post / is_open / close calls as a host window, so the settings code is shared.
    """

    def __init__(self, page):
        self._page = page

    def post(self, data):
        self._page.post_message({**data, "view": "settings"})

    def is_open(self):
        return True

    def close(self):
        self._page.post_message({"type": "show_view", "view": "main"})


if _PAGES:

    class SpoolioPage(orca.pages.PagesPluginCapabilityBase):
        """The Spoolio tab, with Settings as a second view inside it.

        Used instead of SpoolioWindow when orca.pages exists. Its settings methods duplicate
        SpoolioWindow's rather than share a mixin, to avoid multiple inheritance with a
        pybind11 base class.
        """

        def __init__(self):
            super().__init__()
            self._settings_window = _SettingsView(self)
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
                url = self._spoolman_url()
                return tab_html(
                    source=source(),
                    initial_view="main" if configured() else "settings",
                    current_url=url,
                    low_stock_grams=parse_low(get_settings().get("low_stock_grams")),
                    plate_check=plate_settings()[0],
                    plate_margin=plate_settings()[1],
                    weight=weight_mode(),
                    cart=cart_on(),
                )
            except Exception:
                log.exception("get_ui() raised")
                raise

        def on_message(self, arg0):
            # The base class hints "arg0: str", but a parsed dict arrives (as with
            # create_window). A string is still accepted in case that changes.
            try:
                data = json.loads(arg0) if isinstance(arg0, str) else arg0
                if not isinstance(data, dict):
                    return
                if data.get("view") == "settings":
                    self._on_settings(data)
                    return
                msg_type = data.get("type")
                if msg_type in ("ready", "refresh"):
                    self._push_data()
                    if msg_type == "ready" and _plate["payload"]:
                        send_plate(_plate["payload"])
                elif msg_type == "settings":
                    self._open_settings()
                elif msg_type == "order":
                    open_search(data.get("query"))
                elif msg_type == "printers":
                    save_printers(data.get("printers"))
                elif msg_type == "live":
                    self.post_message({"type": "live", "live": live_poll(), "usage": usage_state()})
                elif msg_type == "printer_code":
                    save_printer_code(data.get("id"), data.get("code"))
                elif msg_type in ("usage_mode", "usage_book", "usage_dismiss"):
                    threading.Thread(target=lambda: self.post_message({"type": "usage", "usage": usage_action(data)}), daemon=True).start()
                elif msg_type == "plate_recheck":
                    threading.Thread(target=recheck, daemon=True).start()
                elif msg_type == "plate_dismiss":
                    _plate.update(payload=None, slots=[])
            except Exception:
                log.exception("on_message() raised (arg0=%r)", arg0)

        def _spoolman_url(self):
            return get_settings().get("spoolman_url", "")

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
            low_stock_grams = parse_low(get_settings().get("low_stock_grams"))

            def worker():
                result = load_spools()
                self._refreshing = False
                self.post_message({"type": "spools", "low_stock_grams": low_stock_grams,
                                   "weight_display": weight_mode(), "show_cart": cart_on(), **printer_state(), **result})

            threading.Thread(target=worker, daemon=True).start()

        def _save_settings(self, url, low_stock, plate_check=True,
                           plate_margin=DEFAULT_MARGIN, weight="both", cart=False, src="spoolman"):
            settings = get_settings()
            settings["spool_source"] = parse_source(src)
            if settings["spool_source"] == "spoolman":
                settings["spoolman_url"] = url
            settings["low_stock_grams"] = parse_low(low_stock)
            settings["plate_check"] = parse_flag(plate_check)
            settings["plate_margin_percent"] = parse_margin(plate_margin)
            settings["weight_display"] = parse_weights(weight)
            settings["show_cart"] = parse_flag(cart, default=False)
            if save_settings(settings):
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
            saved_url = self._spoolman_url()
            self.post_message({
                "type": "show_view",
                "view": "settings",
                "source": source(),
                "url": saved_url,
                "low_stock": parse_low(get_settings().get("low_stock_grams")),
                "plate_check": plate_settings()[0],
                "plate_margin": plate_settings()[1],
                "weight_display": weight_mode(),
                "show_cart": cart_on(),
            })
            self._check_connection(saved_url, self._settings_window)

        def _check_connection(self, url, window, src=None):
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
                if window.is_open():
                    window.post({"type": "test_result", "source": src, "url": url, **info})

            threading.Thread(target=worker, daemon=True).start()

        def _bambu_sign_in(self, data, window):
            if data.get("action") == "sign_out":
                self._verified_bambu = False

            def worker():
                result = bambu_sign_in(data)
                if window.is_open():
                    window.post({"type": "bambu_auth_result", **result})

            threading.Thread(target=worker, daemon=True).start()

        def _on_settings(self, data):
            msg_type = data.get("type")
            if msg_type == "ready":
                self._check_connection(self._spoolman_url(), self._settings_window)
            elif msg_type == "test":
                url = (data.get("url") or "").strip()
                self._check_connection(url, self._settings_window, data.get("source"))
            elif msg_type == "save":
                url = (data.get("url") or "").strip()
                src = parse_source(data.get("source"))
                verified = (self._verified_bambu if src == "bambu"
                            else clean_url(url) == self._verified_url)
                if not verified:
                    self._settings_window.post({
                        "type": "test_result",
                        "source": src,
                        "ok": False,
                        "url": url,
                        "error": "Test the connection successfully before saving.",
                    })
                    return
                saved = self._save_settings(url, data.get("low_stock"), data.get("plate_check"),
                                            data.get("plate_margin"), data.get("weight_display"),
                                            data.get("show_cart"), src)
                if saved:
                    self._after_save()
                    self._settings_window.close()
            elif msg_type == "bambu_auth":
                if self._settings_window is not None:
                    self._bambu_sign_in(data, self._settings_window)
            elif msg_type == "feedback":
                open_url(FEEDBACK_URL)
            elif msg_type == "check_update":
                if self._checking_update:
                    return
                self._checking_update = True
                window = self._settings_window

                def worker():
                    result = get_release()
                    self._checking_update = False
                    if result["ok"]:
                        self._release_url = result["url"]
                        result["newer"] = is_newer(result["version"], PLUGIN_VERSION)
                    window.post({"type": "update_result", **result})

                threading.Thread(target=worker, daemon=True).start()
            elif msg_type == "release" and self._release_url.startswith("https://"):
                open_url(self._release_url)
            elif msg_type == "cancel":
                self._settings_window.close()

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

    class SpoolioFilamentCheck(orca.slicing.SlicingPipelineCapabilityBase):
        """After each slice, checks the plate's filament against the spools in Spoolman."""

        def get_name(self):
            return "Spoolio Filament Check"

        def execute(self, ctx):
            try:
                if ctx.step == getattr(orca.slicing.Step, "posSlice", None):
                    if _plate["payload"]:
                        threading.Thread(target=clear_plate, daemon=True).start()
                    return orca.ExecutionResult.success()
                if ctx.step != orca.slicing.Step.psGCodePostProcess:
                    return orca.ExecutionResult.success()
                enabled, margin = plate_settings()
                slots = plate_slots(ctx, parse_usage(read_tail(ctx.gcode_path)))
                usage_plan(slots)
                if enabled:
                    if any(slot["grams"] > 0 for slot in slots):
                        # Off the slicing thread: it may fetch the spool list, and UI calls from
                        # here can deadlock while the GUI waits for slicing to finish.
                        threading.Thread(target=run_check, args=(slots, margin),
                                         daemon=True).start()
            except Exception:
                log.exception("Filament check failed")
            return orca.ExecutionResult.success()


@orca.plugin
class SpoolioPlugin(orca.base):
    def register_capabilities(self):
        if _PAGES:
            orca.register_capability(SpoolioPage)
        else:
            orca.register_capability(SpoolioWindow)
        if _SLICING:
            orca.register_capability(SpoolioFilamentCheck)
