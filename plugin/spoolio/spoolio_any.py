# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Spoolio"
# description = "A Bambu Lab inspired inventory management overview for OrcaSlicer"
# author = "Dan J Moore"
# version = "0.4.0"
# ///

import html
import json
import logging
import os
import platform
import re
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
PLUGIN_VERSION = "0.4.0"
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
    return f" (only {result['matches']} of {result['pool']} matching spools are in Spoolman)"


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
    result = get_spools(url)
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
    """The notice for a plate and its per-group results (None when Spoolman couldn't be checked)."""
    url = get_settings().get("spoolman_url", "")
    if fresh:
        _spool_cache["time"] = float("-inf")
    spools = plate_spools(url) if url else None
    if not url:
        return ("info", "Spoolio: no Spoolman address is set up yet, "
                        "so the filament quantity wasn't checked."), None
    if spools is None:
        return ("info", "Spoolio: couldn't reach Spoolman, "
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
</style>
</head>
<body>
  <!--header--><div class="header">
    <div class="title">__LOGO__<h2>__PLUGIN_NAME__</h2></div>
    <div class="header-actions">
      <button id="settings-btn" title="Settings &amp; about">Settings</button>
    </div>
  </div><!--/header-->
  <div class="listbar">
    <div id="status">Loading...</div>
    <button id="filters-toggle" aria-expanded="false">Filters &#9662;</button>
  </div>
  <div id="filters-panel" hidden>
  <div class="filters">
    <input id="search" type="text" placeholder="Filter by name...">
    <select id="material-filter"><option value="">All materials</option></select>
    <select id="vendor-filter"><option value="">All manufacturers</option></select>
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
      <option value="remaining" selected>Sort: Remaining weight</option>
      <option value="used">Sort: Used weight</option>
      <option value="first_used">Sort: First used</option>
      <option value="last_used">Sort: Last used</option>
      <option value="plate" disabled>Sort: On this plate</option>
    </select>
    <button class="sort-dir" id="sort-dir" title="Toggle ascending/descending">&#9650;</button>
  </div>
  </div>

  <div id="plate" class="plate" hidden>
    <div class="divider">
      <span class="line"></span>
      <span class="divider-title">⚖️ This plate</span>
      <span class="divider-sub" id="plate-sub"></span>
      <span class="line"></span>
      <span class="plate-pill" id="plate-pill"></span>
      <button id="plate-recheck" title="Check again against Spoolman">Re-check</button>
      <button id="plate-dismiss" title="Hide until the next slice">Dismiss</button>
    </div>
    <div class="plate-rows" id="plate-rows"></div>
    <div class="divider"><span class="line"></span><span class="divider-title">All spools</span><span class="line"></span></div>
  </div>

  <div id="list"></div>

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
        document.getElementById("plate-sub").textContent =
          "Sliced " + (plate.time || "") + " \u00b7 striped = needed, solid = left \u00b7 clears on next slice";
      }
      plateEl.hidden = !plate;
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
      const tags = s.tags;
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
      const chipHtml = plateLabel ? '<span class="plate-chip">On this plate \u00b7 ' + plateLabel + '</span>' : '';
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
        '<div class="spool-card">' +
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
      populateFilterOptions();
      applyFilters();
    }

    orca.onMessage(function (data) {
      if (data && data.type === "spools") {
        render(data);
      } else if (data && data.type === "plate") {
        setPlate(data);
      } else if (data && data.type === "plate_clear") {
        setPlate(null);
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
  .step p { margin: 0; color: var(--orca-muted); }
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
    __LOGO__<h2>__PLUGIN_NAME__ Settings</h2>
    <button class="icon-btn" id="feedback" title="Send feedback or report a bug" aria-label="Send feedback">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
    </button>
  </div><!--/header-->

  <!--steps--><div class="step">
    <div class="step-num">1</div>
    <div>
      <h4>Connect to Spoolman</h4>
      <p>Enter the address of your self-hosted Spoolman server below. The plugin only
      <strong>reads</strong> your spool list from Spoolman and doesn't make any changes.</p>
    </div>
  </div>
  <div class="step">
    <div class="step-num">2</div>
    <div>
      <h4>Browse your Spools</h4>
      <p>Each card shows the filament color, remaining weight, and an RFID badge where a
      tag is linked. Filter, sort, or group the list by material, manufacturer, or location.</p>
    </div>
  </div><!--/steps-->
  <div class="divider"></div>

  <!--reorder--><h3><span class="emoji">🛒</span>Configure Filament Reorder</h3>
  <label class="check-row"><input id="show-cart" type="checkbox" __CART_CHECKED__>Show a cart button for reordering low-stock spools</label>
  <label for="low-stock" class="field-label">Low stock warning (grams):</label>
  <input id="low-stock" type="number" min="0" step="10" value="__LOW_STOCK__">
  <div class="hint">Below this weight, a spool's remaining weight turns amber, then red as it runs out.</div><!--/reorder-->

  <div class="divider spaced"></div>

  <!--check--><h3><span class="emoji">\u26a0\ufe0f</span>Configure Filament Check</h3>
  <label class="check-row"><input id="plate-check" type="checkbox" __PLATE_CHECKED__>Show filament check notifications</label>
  <div class="margin-row" title="Warns when a plate needs more than a spool has left, or would leave less than this margin spare.">
    <label for="plate-margin">Safety margin (%):</label>
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

  <!--server--><h3><span class="emoji">🔌</span>Configure Spoolman Server</h3>
  <label for="url">Enter the URL of your self-hosted Spoolman server:</label>
  <div class="url-row">
    <input id="url" type="text" value="__URL__" placeholder="__DEFAULT_URL__">
    <button class="test" id="test">Test</button>
  </div>
  <div class="status __STATUS_CLASS__" id="conn-status">__STATUS_TEXT__</div><!--/server-->

  <!--about--><div class="about">
    <div class="logpath">Log file: __LOG_PATH__</div>
  </div><!--/about-->

  <!--footer--><div class="footer">
    <div class="version-row">
      <span>Plugin version __VERSION__</span>
      <button class="link" id="check-update">Check for updates</button>
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

    // Save stays disabled until the URL passes a test. An already-connected saved URL
    // counts as passed, so other settings can be changed on their own.
    let verifiedUrl = __VERIFIED_URL__;
    let verifiedText = statusEl.textContent;

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }
    function refreshSave() {
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
      orca.postMessage({ type: "test", url: urlEl.value.trim() });
    });

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
      if (!data || data.type !== "test_result") return;
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
        type: "save", url: url, low_stock: lowStock,
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
        PLUGIN_NAME=PLUGIN_NAME,
        LOW_STOCK_DEFAULT=LOW_DEFAULT,
        REFRESH_MS=REFRESH_SECS * 1000,
    )


def settings_html(current_url: str, spoolman_info: dict, low_stock_grams: float,
                  plate_check: bool = True, plate_margin: int = DEFAULT_MARGIN,
                  weight: str = "both", cart: bool = False) -> str:
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
        PLUGIN_NAME=PLUGIN_NAME,
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
      noteEl.textContent = "Connect to Spoolman to preview which spools would trigger a reorder.";
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
    document.getElementById("actions-settings").classList.toggle("view-hidden", !settings);
    document.getElementById("title-suffix").classList.toggle("view-hidden", !settings);
    if (!settings) return;
    // Reset the form to the saved values each time Settings is opened, so
    // edits abandoned with Cancel don't linger.
    var url = document.getElementById("url");
    url.value = data.url || url.placeholder;
    url.dispatchEvent(new Event("input"));
    document.getElementById("low-stock").value = data.low_stock;
    document.getElementById("plate-check").checked = data.plate_check !== false;
    if (data.plate_margin !== undefined) document.getElementById("plate-margin").value = data.plate_margin;
    var mode = document.querySelector('input[name="weight-display"][value="' + (data.weight_display || "both") + '"]');
    if (mode) { mode.checked = true; mode.dispatchEvent(new Event("change")); }
    document.getElementById("show-cart").checked = data.show_cart === true;
    document.getElementById("update-status").textContent = "";
  });
})();
"""


def tab_html(initial_view: str, current_url: str, low_stock_grams: float,
             plate_check: bool = True, plate_margin: int = DEFAULT_MARGIN,
             weight: str = "both", cart: bool = False) -> str:
    """The spool list and Settings as two views of one page, under a shared header.

    Built from the standalone pages, which SpoolioWindow still shows as separate windows.
    """
    main_css, main_body, main_js = _split_page(main_html())
    settings_css, settings_body, settings_js = _split_page(settings_html(
        current_url, {"ok": False, "pending": True}, low_stock_grams, plate_check, plate_margin,
        weight, cart,
    ))
    main_body, main_header = _take_header(main_body)
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
  {LOGO_IMG}<h2>{PLUGIN_NAME}<span id="title-suffix" class="{_hidden(on_main)}"> Settings</span></h2>
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
        self._release_url = ""
        self._refreshing = False
        self._testing = False
        self._checking_update = False

    def get_name(self):
        return PLUGIN_NAME

    def _spoolman_url(self):
        return get_settings().get("spoolman_url", "")

    def _save_settings(self, url, low_stock, plate_check=True,
                       plate_margin=DEFAULT_MARGIN, weight="both", cart=False):
        settings = get_settings()
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

    def _check_connection(self, url, window):
        if self._testing:
            return
        self._testing = True

        def worker():
            info = ping(url)
            self._testing = False
            if info.get("ok"):
                self._verified_url = clean_url(url)
            if window.is_open():
                window.post({"type": "test_result", "url": url, **info})

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
                self._check_connection(url, self._settings_window)
        elif msg_type == "save":
            # The page already gates Save; enforce it here too.
            url = (data.get("url") or "").strip()
            if clean_url(url) != self._verified_url:
                if self._settings_window is not None:
                    self._settings_window.post({
                        "type": "test_result",
                        "ok": False,
                        "url": url,
                        "error": "Test the connection successfully before saving.",
                    })
                return
            saved = self._save_settings(url, data.get("low_stock"), data.get("plate_check"),
                                        data.get("plate_margin"), data.get("weight_display"),
                                        data.get("show_cart"))
            if saved:
                self._after_save()
                if self._settings_window is not None:
                    self._settings_window.close()
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

    def _push_data(self):
        if self._panel is None or not self._panel.is_open():
            return
        url = self._spoolman_url()
        if not url:
            self._panel.post({
                "type": "spools",
                "ok": False,
                "error": "No Spoolman URL configured yet - click Settings to set one.",
            })
            return
        if self._refreshing:
            return
        self._refreshing = True
        panel = self._panel
        low_stock_grams = parse_low(get_settings().get("low_stock_grams"))

        def worker():
            result = get_spools(url)
            self._refreshing = False
            if panel.is_open():
                panel.post({"type": "spools", "low_stock_grams": low_stock_grams,
                            "weight_display": weight_mode(), "show_cart": cart_on(), **result})

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
        if not self._spoolman_url():
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
                    initial_view="main" if url else "settings",
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
            if not url:
                self.post_message({
                    "type": "spools",
                    "ok": False,
                    "error": "No Spoolman URL configured yet - click Settings to set one.",
                })
                return
            if self._refreshing:
                return
            self._refreshing = True
            low_stock_grams = parse_low(get_settings().get("low_stock_grams"))

            def worker():
                result = get_spools(url)
                self._refreshing = False
                self.post_message({"type": "spools", "low_stock_grams": low_stock_grams,
                                   "weight_display": weight_mode(), "show_cart": cart_on(), **result})

            threading.Thread(target=worker, daemon=True).start()

        def _save_settings(self, url, low_stock, plate_check=True,
                           plate_margin=DEFAULT_MARGIN, weight="both", cart=False):
            settings = get_settings()
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
                "url": saved_url,
                "low_stock": parse_low(get_settings().get("low_stock_grams")),
                "plate_check": plate_settings()[0],
                "plate_margin": plate_settings()[1],
                "weight_display": weight_mode(),
                "show_cart": cart_on(),
            })
            self._check_connection(saved_url, self._settings_window)

        def _check_connection(self, url, window):
            if self._testing:
                return
            self._testing = True

            def worker():
                info = ping(url)
                self._testing = False
                if info.get("ok"):
                    self._verified_url = clean_url(url)
                if window.is_open():
                    window.post({"type": "test_result", "url": url, **info})

            threading.Thread(target=worker, daemon=True).start()

        def _on_settings(self, data):
            msg_type = data.get("type")
            if msg_type == "ready":
                self._check_connection(self._spoolman_url(), self._settings_window)
            elif msg_type == "test":
                url = (data.get("url") or "").strip()
                self._check_connection(url, self._settings_window)
            elif msg_type == "save":
                url = (data.get("url") or "").strip()
                if clean_url(url) != self._verified_url:
                    self._settings_window.post({
                        "type": "test_result",
                        "ok": False,
                        "url": url,
                        "error": "Test the connection successfully before saving.",
                    })
                    return
                saved = self._save_settings(url, data.get("low_stock"), data.get("plate_check"),
                                            data.get("plate_margin"), data.get("weight_display"),
                                            data.get("show_cart"))
                if saved:
                    self._after_save()
                    self._settings_window.close()
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
                if enabled:
                    slots = plate_slots(ctx, parse_usage(read_tail(ctx.gcode_path)))
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
