import json
import math
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from curl_cffi import requests as browser_requests
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

from earnings_cal.audit import AuditJournal
from earnings_cal.notes import NotesJournal
from earnings_cal.season_analytics import summarize_seasons
from earnings_cal.earnings_brief import EarningsBriefHarness
from earnings_cal.research_repository import ResearchRepository


def _load_environment() -> None:
    """Load an external .env in development and beside/above a frozen build."""
    candidates = [Path.cwd() / ".env"]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend([executable_dir / ".env", executable_dir.parent / ".env"])
    else:
        candidates.append(Path(__file__).resolve().parents[2] / ".env")
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


_load_environment()


def _bundle_root() -> Path:
    """Where packaged assets live (frozen or source)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent


def _data_dir() -> Path:
    """Persistent per-user data dir for the editable tickers list."""
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "earnings-cal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _research_data_dir() -> Path:
    configured = os.environ.get("EARNINGS_DATA_ROOT")
    if configured:
        root = Path(configured).expanduser()
    elif getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent.parent / "data" / "research"
    else:
        root = Path(__file__).resolve().parents[2] / "data" / "research"
    root.mkdir(parents=True, exist_ok=True)
    return root


ASSETS_DIR = _bundle_root() / "assets"
LEGACY_DATA_DIR = _data_dir()
data_repository = ResearchRepository(_research_data_dir())
TICKERS_FILE = data_repository.operational_path("ticker_universe", "tickers.json", LEGACY_DATA_DIR / "tickers.json")
SNAPSHOT_FILE = data_repository.operational_path("earnings_snapshot", "snapshot.json", LEGACY_DATA_DIR / "snapshot.json")
ARCHIVE_FILE = data_repository.operational_path("earnings_archive", "archive.json", LEGACY_DATA_DIR / "archive.json")
SETTINGS_FILE = data_repository.operational_path("app_settings", "settings.json", LEGACY_DATA_DIR / "settings.json")
FORECAST_AUDIT_FILE = data_repository.operational_path("forecast_journal", "forecast-audit.jsonl", LEGACY_DATA_DIR / "forecast-audit.jsonl")
NOTES_AUDIT_FILE = data_repository.operational_path("notes_journal", "notes-audit.jsonl", LEGACY_DATA_DIR / "notes-audit.jsonl")
forecast_journal = AuditJournal(FORECAST_AUDIT_FILE)
notes_journal = NotesJournal(NOTES_AUDIT_FILE)
DEFAULT_TICKERS_VERSION = 2
if not TICKERS_FILE.exists():
    seed = ASSETS_DIR / "tickers.json"
    if seed.exists():
        shutil.copy(seed, TICKERS_FILE)
    else:
        TICKERS_FILE.write_text("[]")

app = Flask(__name__, static_folder=None)

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 60 * 30
MAX_WORKERS = 12
SNAPSHOT_TOUCH_INTERVAL = 60 * 60 * 6
BENCHMARK_TICKER = "XLB"
CALENDAR_BENCHMARKS = ("XLB", "XLI", "SPY")
BENCHMARK_CACHE_TTL = 60 * 60 * 6
NASDAQ_FALLBACK_DAYS_BACK = 7
NASDAQ_FALLBACK_DAYS_FORWARD = 7
NASDAQ_CACHE_TTL = 60 * 60 * 6
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/earnings",
}
ECONOMIC_CACHE_TTL = 60 * 60 * 6
_economic_cache: tuple[float, list[dict]] | None = None
_bls_cache: tuple[float, list[dict]] | None = None
_economic_range_cache: dict[tuple[date, date], tuple[float, list[dict]]] = {}
_sec_cik_by_ticker: dict[str, int] | None = None
_sec_revenue_cache: dict[str, tuple[float, dict[str, float]]] = {}
_sec_request_lock = threading.Lock()
_sec_last_request = 0.0
SEC_HEADERS = {
    "User-Agent": "earnings-calendar/1.0 earnings-calendar@example.com",
    "Accept-Encoding": "gzip, deflate",
}
SEC_CACHE_TTL = 60 * 60 * 6
MARKET_TZ = ZoneInfo("America/New_York")
RECALCULATED_EVENT_FIELDS = {
    "move_before",
    "move_day",
    "move_after",
    "reaction_date",
    "xlb_reaction_date",
    "xlb_move_before",
    "xlb_move_day",
    "xlb_move_after",
    "relative_move_before",
    "relative_move_day",
    "relative_move_after",
}


def _json_dumps(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"))


def _load_snapshot() -> dict[str, dict]:
    try:
        return json.loads(SNAPSHOT_FILE.read_text())
    except Exception:
        return {}


def _save_snapshot(snap: dict[str, dict]) -> None:
    try:
        tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json_dumps(snap))
        tmp.replace(SNAPSHOT_FILE)
    except Exception:
        pass


_snapshot = _load_snapshot()
_snapshot_lock = threading.Lock()
_benchmark_hist_cache: dict[str, tuple[float, object]] = {}
_benchmark_hist_lock = threading.Lock()
_tape_cache: dict[str, tuple[float, dict]] = {}
_tape_cache_lock = threading.Lock()
_nasdaq_cache: dict[tuple[str, str], tuple[float, dict[str, list[dict]]]] = {}
_nasdaq_lock = threading.Lock()


def _load_archive() -> dict[str, dict[str, dict]]:
    """Persistent accumulating archive of past earnings events.

    Shape: {ticker: {YYYY-MM-DD: <entry>}}. Never overwrites a non-null field
    with null, so once an event is recorded it survives even if yfinance later
    drops it from its history.
    """
    try:
        return json.loads(ARCHIVE_FILE.read_text())
    except Exception:
        return {}


def _save_archive(arc: dict) -> None:
    try:
        tmp = ARCHIVE_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json_dumps(arc))
        tmp.replace(ARCHIVE_FILE)
    except Exception:
        pass


_archive = _load_archive()
_archive_lock = threading.Lock()


def _merge_archive(ticker: str, past_entries: list[dict]) -> bool:
    """Merge freshly-fetched entries into the archive without losing fields."""
    if not past_entries:
        return False
    changed = False
    with _archive_lock:
        bucket = _archive.setdefault(ticker, {})
        for e in past_entries:
            key = (e.get("date") or "")[:10]
            if not key:
                continue
            existing = bucket.get(key)
            if existing is None:
                merged = dict(e)
            else:
                # Field-level merge: newest non-null values win for data that may
                # disappear from APIs, while recalculated return fields can clear
                # stale values when a next trading session is not available yet.
                merged = dict(existing)
                for k, v in e.items():
                    if v is not None or k in RECALCULATED_EVENT_FIELDS:
                        merged[k] = v
            if existing != merged:
                bucket[key] = merged
                changed = True
        if changed:
            _save_archive(_archive)
    return changed


def _store_snapshot_if_needed(ticker: str, data: dict, saved_at: float) -> bool:
    """Store snapshot only when payload changed or freshness is meaningfully stale."""
    with _snapshot_lock:
        existing = _snapshot.get(ticker) or {}
        existing_data = existing.get("data")
        existing_saved_at = float(existing.get("saved_at") or 0)
        if existing_data == data and saved_at - existing_saved_at < SNAPSHOT_TOUCH_INTERVAL:
            return False
        _snapshot[ticker] = {"data": data, "saved_at": saved_at}
        _save_snapshot(_snapshot)
    return True


def _empty_earnings(ticker: str) -> dict:
    return {
        "ticker": ticker, "name": None, "next_date": None, "next_session": None,
        "eps_estimate": None, "revenue_estimate": None,
        "upcoming": [], "past": [], "error": None,
    }


def cached_earnings(ticker: str) -> dict:
    now = time.time()
    with _snapshot_lock:
        snap = _snapshot.get(ticker)
    if snap and snap.get("data"):
        data = dict(snap["data"])
        data["from_snapshot"] = True
        data["loading"] = True
        data["snapshot_age_seconds"] = max(0, int(now - snap.get("saved_at", now)))
        return data

    data = _empty_earnings(ticker)
    data["loading"] = True
    data["cached_missing"] = True
    data["from_snapshot"] = False
    data["snapshot_age_seconds"] = None
    return data


def _calendar_earnings_dates(cal: dict) -> set[str]:
    """Return date keys from yfinance calendar's Earnings Date field."""
    raw = cal.get("Earnings Date")
    if raw is None:
        return set()
    vals = raw if isinstance(raw, (list, tuple, set)) else [raw]
    out = set()
    for v in vals:
        try:
            d = v.date() if hasattr(v, "date") else v
            out.add(d.isoformat())
        except Exception:
            continue
    return out


def _fiscal_year_end_month(info: dict) -> int:
    """Return fiscal year-end month from yfinance metadata, defaulting to December."""
    raw = info.get("lastFiscalYearEnd")
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc).month
        if raw:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).month
    except (TypeError, ValueError, OSError):
        pass
    return 12


def _reported_fiscal_period(earnings_dt: datetime, fiscal_year_end_month: int) -> str:
    """Infer the quarter being reported, rather than the quarter of the call date."""
    # Results normally arrive weeks after quarter-end. Requiring a two-week lag
    # prevents a late-March call from being assigned to the still-open quarter.
    cutoff = earnings_dt.date() - timedelta(days=14)
    quarter_months = [((fiscal_year_end_month + step * 3 - 1) % 12) + 1 for step in (1, 2, 3, 4)]
    candidates = []
    for year in range(cutoff.year - 1, cutoff.year + 1):
        for month in quarter_months:
            next_month = date(year + (month == 12), month % 12 + 1, 1)
            quarter_end = next_month - timedelta(days=1)
            if quarter_end <= cutoff:
                candidates.append(quarter_end)
    quarter_end = max(candidates)
    quarter = quarter_months.index(quarter_end.month) + 1
    fiscal_year = quarter_end.year + (1 if quarter_end.month > fiscal_year_end_month else 0)
    return f"FY{fiscal_year} Q{quarter}"


def _event_is_past_for_returns(dt: datetime, session: str | None, now_utc: datetime) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if session in {"BMO", "DMH", "AMC"}:
        return dt <= now_utc
    return dt < now_utc - timedelta(hours=6)


def _archived_past(ticker: str, cutoff: datetime | None = None) -> list[dict]:
    with _archive_lock:
        entries = [dict(v) for v in _archive.get(ticker, {}).values()]
    if cutoff is None:
        return entries

    out = []
    for entry in entries:
        try:
            dt = datetime.fromisoformat(entry.get("date") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        # Upcoming estimates are archived so their consensus data can later be
        # merged into the actual event.  A projected date may be moved, though;
        # crossing its timestamp alone does not prove that earnings occurred.
        # Only surface archived entries as history once a reported result made
        # the event verifiable.
        has_reported_result = (
            entry.get("eps_reported") is not None
            or entry.get("eps_surprise_pct") is not None
        )
        if has_reported_result and _event_is_past_for_returns(
            dt, entry.get("session"), cutoff + timedelta(hours=6)
        ):
            out.append(entry)
    return out


def _union_past(live: list[dict], archived: list[dict]) -> list[dict]:
    """Union by date-key (YYYY-MM-DD); live wins ties (latest moves data)."""
    seen: dict[str, dict] = {}
    for src in (archived, live):
        for e in src:
            key = (e.get("date") or "")[:10]
            if not key:
                continue
            if key in seen:
                merged = dict(seen[key])
                for k, v in e.items():
                    if v is not None or k in RECALCULATED_EVENT_FIELDS:
                        merged[k] = v
                seen[key] = merged
            else:
                seen[key] = dict(e)
    out = list(seen.values())
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def load_tickers() -> list[str]:
    if not TICKERS_FILE.exists():
        return []
    return json.loads(TICKERS_FILE.read_text())


def _load_default_tickers() -> list[str]:
    try:
        return json.loads((ASSETS_DIR / "tickers.json").read_text())
    except Exception:
        return []


def save_tickers(tickers: list[str]) -> None:
    cleaned = [t.strip().upper() for t in tickers if t.strip()]
    seen, out = set(), []
    for t in cleaned:
        if t not in seen:
            seen.add(t)
            out.append(t)
    TICKERS_FILE.write_text(json.dumps(out, indent=2))


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _save_settings(settings: dict) -> None:
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json_dumps(settings))
        tmp.replace(SETTINGS_FILE)
    except Exception:
        pass


def _apply_default_ticker_migrations() -> None:
    settings = _load_settings()
    if int(settings.get("default_tickers_version") or 0) >= DEFAULT_TICKERS_VERSION:
        return

    defaults = [t.strip().upper() for t in _load_default_tickers() if t.strip()]
    current = load_tickers()
    current_set = set(current)
    default_set = set(defaults)
    ordered = defaults + [t for t in current if t not in default_set]
    if ordered != current or any(t not in current_set for t in defaults):
        save_tickers(ordered)
    settings["default_tickers_version"] = DEFAULT_TICKERS_VERSION
    _save_settings(settings)


_apply_default_ticker_migrations()


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except Exception:
        return None


def _money_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return _num(v)
    s = str(v).strip()
    if not s or s.upper() in {"N/A", "NA", "--"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    mult = 1.0
    if s[-1:].upper() == "B":
        mult = 1e9
        s = s[:-1]
    elif s[-1:].upper() == "M":
        mult = 1e6
        s = s[:-1]
    n = _num(s)
    if n is None:
        return None
    return (-n if neg else n) * mult


def _classify_session(dt: datetime) -> str | None:
    """BMO (before market open), AMC (after market close), or DMH (during market hours)."""
    h = dt.hour + dt.minute / 60
    if h < 9.5:
        return "BMO"
    if h >= 16:
        return "AMC"
    if h == 0:  # midnight = unspecified by Yahoo
        return None
    return "DMH"


def _reported_revenue_by_quarter(t: yf.Ticker) -> dict[str, float]:
    """Return {YYYY-MM: total_revenue} from quarterly income statement."""
    out = {}
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            df = getattr(t, attr)
            if df is None or df.empty:
                continue
            for label in ("Total Revenue", "TotalRevenue", "Revenue"):
                if label in df.index:
                    row = df.loc[label]
                    for col, val in row.items():
                        d = col.to_pydatetime() if hasattr(col, "to_pydatetime") else col
                        key = f"{d.year}-{d.month:02d}"
                        v = _num(val)
                        if v is not None:
                            out[key] = v
                    break
            if out:
                return out
        except Exception:
            continue
    return out


def _sec_get_json(url: str) -> dict:
    """Read one SEC JSON endpoint while respecting its fair-access rate limit."""
    global _sec_last_request
    with _sec_request_lock:
        wait = 0.12 - (time.monotonic() - _sec_last_request)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(url, headers=SEC_HEADERS, timeout=12)
        _sec_last_request = time.monotonic()
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _sec_cik(ticker: str) -> int | None:
    global _sec_cik_by_ticker
    if _sec_cik_by_ticker is None:
        payload = _sec_get_json("https://www.sec.gov/files/company_tickers.json")
        _sec_cik_by_ticker = {
            str(row.get("ticker") or "").upper(): int(row["cik_str"])
            for row in payload.values()
            if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None
        }
    return _sec_cik_by_ticker.get(ticker.upper())


def _sec_reported_revenue_by_quarter(ticker: str) -> dict[str, float]:
    """Return quarterly revenue from official SEC Company Facts when available."""
    now = time.time()
    cached = _sec_revenue_cache.get(ticker)
    if cached and now - cached[0] < SEC_CACHE_TTL:
        return cached[1]
    try:
        cik = _sec_cik(ticker)
        if cik is None:
            return {}
        payload = _sec_get_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        )
        namespaces = payload.get("facts") or {}
        candidates: dict[str, tuple[str, int, float]] = {}
        tags = (
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "SalesRevenueNet"),
            ("us-gaap", "Revenues"),
            ("ifrs-full", "Revenue"),
        )
        for namespace, tag in tags:
            units = (((namespaces.get(namespace) or {}).get(tag) or {}).get("units") or {})
            for unit_name, facts in units.items():
                if unit_name != "USD":
                    continue
                for fact in facts if isinstance(facts, list) else []:
                    try:
                        start_day = date.fromisoformat(fact["start"])
                        end_day = date.fromisoformat(fact["end"])
                        duration = (end_day - start_day).days
                        value = float(fact["val"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if fact.get("form") not in {"10-Q", "10-K", "8-K", "6-K", "20-F"}:
                        continue
                    if not 60 <= duration <= 120:
                        continue
                    key = f"{end_day.year}-{end_day.month:02d}"
                    rank = (str(fact.get("filed") or ""), -abs(duration - 91), value)
                    if key not in candidates or rank[:2] > candidates[key][:2]:
                        candidates[key] = rank
        result = {key: ranked[2] for key, ranked in candidates.items()}
    except Exception:
        result = {}
    _sec_revenue_cache[ticker] = (now, result)
    return result


def _trading_day_moves(hist, earnings_dt: datetime, session: str | None = None) -> dict:
    """Return % moves around the first trading session that can react.

    BMO/DMH/unknown reports use the report-date trading session as D0. AMC
    reports use the next trading session as D0 because the market is closed
    when the company releases results.
    """
    out = {
        "move_before": None,
        "move_day": None,
        "move_after": None,
        "reaction_date": None,
    }
    if hist is None or hist.empty:
        return out
    try:
        closes = hist["Close"]
        # normalize: index is timezone-aware (yfinance), match by date.
        event_date = earnings_dt.date()
        raw_dates = [d.date() if hasattr(d, "date") else d for d in closes.index]
        keep = [i for i, d in enumerate(raw_dates) if _is_completed_trading_date(d)]
        idx_dates = [raw_dates[i] for i in keep]
        vals = closes.values[keep]

        n = len(vals)
        if n == 0:
            return out

        def pct(a, b):
            try:
                if a is None or b is None or a == 0:
                    return None
                return float((b - a) / a * 100.0)
            except Exception:
                return None

        if session == "AMC":
            i_event = None
            i_day = None
            for i, d in enumerate(idx_dates):
                if d <= event_date:
                    i_event = i
                if d > event_date:
                    i_day = i
                    break

            # For AMC, D-1 is the report-date trading session. This can exist
            # before the next reaction session appears in history.
            if i_event is not None and i_event - 1 >= 0:
                out["move_before"] = pct(vals[i_event - 1], vals[i_event])
            if i_day is None:
                return out
            out["reaction_date"] = idx_dates[i_day].isoformat()
            if i_event is not None:
                out["move_day"] = pct(vals[i_event], vals[i_day])
            if i_day + 1 < n:
                out["move_after"] = pct(vals[i_day], vals[i_day + 1])
        else:
            i_prev = None
            i_day = None
            for i, d in enumerate(idx_dates):
                if d < event_date:
                    i_prev = i
                if d >= event_date:
                    i_day = i
                    break

            # For BMO/DMH, D-1 can exist before the report-date close is final.
            if i_prev is not None and i_prev - 1 >= 0:
                out["move_before"] = pct(vals[i_prev - 1], vals[i_prev])
            if i_day is None:
                return out
            out["reaction_date"] = idx_dates[i_day].isoformat()
            if out["move_before"] is None and i_day - 2 >= 0:
                out["move_before"] = pct(vals[i_day - 2], vals[i_day - 1])
            if i_day - 1 >= 0:
                out["move_day"] = pct(vals[i_day - 1], vals[i_day])
            if i_day + 1 < n:
                out["move_after"] = pct(vals[i_day], vals[i_day + 1])
    except Exception:
        pass
    return out


def _is_completed_trading_date(day: date) -> bool:
    now_market = datetime.now(MARKET_TZ)
    if day < now_market.date():
        return True
    if day > now_market.date():
        return False
    return now_market.hour > 16 or (now_market.hour == 16 and now_market.minute >= 10)


def _benchmark_history(ticker: str = BENCHMARK_TICKER):
    """Cached price history for a supported calendar benchmark."""
    ticker = ticker.upper()
    if ticker not in CALENDAR_BENCHMARKS:
        return None
    now = time.time()
    with _benchmark_hist_lock:
        cached = _benchmark_hist_cache.get(ticker)
        if cached is not None and now - cached[0] < BENCHMARK_CACHE_TTL:
            return cached[1]

    try:
        hist = yf.Ticker(ticker).history(
            period="3y",
            interval="1d",
            auto_adjust=False,
        )
    except Exception:
        hist = None

    with _benchmark_hist_lock:
        _benchmark_hist_cache[ticker] = (now, hist)
    return hist


def _benchmark_daily_returns(start: date, end: date, ticker: str = BENCHMARK_TICKER) -> dict[str, float]:
    hist = _benchmark_history(ticker)
    out: dict[str, float] = {}
    if hist is None or hist.empty:
        return out
    try:
        closes = hist["Close"]
        idx_dates = [d.date() if hasattr(d, "date") else d for d in closes.index]
        vals = closes.values
        for i in range(1, len(vals)):
            day = idx_dates[i]
            if day < start or day > end:
                continue
            prev = vals[i - 1]
            cur = vals[i]
            if prev is None or cur is None or prev == 0:
                continue
            out[day.isoformat()] = float((cur - prev) / prev * 100.0)
    except Exception:
        pass
    return out


def _add_benchmark_moves(entry: dict, benchmark_hist) -> None:
    """Add XLB returns and stock-minus-XLB relative returns to an event."""
    try:
        dt = datetime.fromisoformat(entry.get("date") or "")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return

    xlb = _trading_day_moves(benchmark_hist, dt, entry.get("session"))
    entry["xlb_reaction_date"] = xlb.get("reaction_date")
    for move_key, xlb_key, rel_key in (
        ("move_before", "xlb_move_before", "relative_move_before"),
        ("move_day", "xlb_move_day", "relative_move_day"),
        ("move_after", "xlb_move_after", "relative_move_after"),
    ):
        xlb_val = xlb.get(move_key)
        if xlb_val is not None:
            entry[xlb_key] = xlb_val
        stock_val = entry.get(move_key)
        if stock_val is not None and xlb_val is not None:
            entry[rel_key] = float(stock_val - xlb_val)


def _match_reported_revenue(earnings_dt: datetime, rev_map: dict[str, float]) -> float | None:
    """Earnings are reported for the previous quarter — find the closest quarter-end on or before."""
    if not rev_map:
        return None
    # sort quarter-ends descending
    keys = sorted(rev_map.keys(), reverse=True)
    for k in keys:
        y, m = map(int, k.split("-"))
        # quarter end is last day of month m
        q_end = datetime(y, m, 28, tzinfo=earnings_dt.tzinfo)
        if q_end <= earnings_dt and (earnings_dt - q_end).days < 120:
            return rev_map[k]
    return None


def _nasdaq_session_and_hour(time_code: str) -> tuple[str | None, int]:
    code = (time_code or "").lower()
    if "pre" in code or "before" in code:
        return "BMO", 6
    if "after" in code:
        return "AMC", 16
    return None, 0


def _fetch_nasdaq_day(day: date) -> list[dict]:
    try:
        resp = requests.get(
            "https://api.nasdaq.com/api/calendar/earnings",
            params={"date": day.isoformat()},
            headers=NASDAQ_HEADERS,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        rows = data.get("rows") or []
        if isinstance(rows, list):
            return rows
    except Exception:
        pass
    return []


def _nasdaq_window(start: date, end: date) -> dict[str, list[dict]]:
    key = (start.isoformat(), end.isoformat())
    now = time.time()
    with _nasdaq_lock:
        # Keep discovery and population under one lock. Without this, every
        # concurrently refreshed ticker starts the same 15-day HTTP fan-out.
        cached = _nasdaq_cache.get(key)
        if cached and now - cached[0] < NASDAQ_CACHE_TTL:
            return cached[1]

        by_ticker: dict[str, list[dict]] = {}
        days = []
        day = start
        while day <= end:
            days.append(day)
            day += timedelta(days=1)

        with ThreadPoolExecutor(max_workers=min(6, len(days) or 1)) as ex:
            day_rows = list(ex.map(lambda d: (d, _fetch_nasdaq_day(d)), days))

        for day, rows in day_rows:
            for row in rows:
                ticker = (row.get("symbol") or "").strip().upper()
                if not ticker:
                    continue
                session, hour = _nasdaq_session_and_hour(row.get("time") or "")
                entry = {
                    "date": datetime(day.year, day.month, day.day, hour, tzinfo=MARKET_TZ).isoformat(),
                    "session": session,
                    "eps_estimate": _money_num(row.get("epsForecast")),
                    "eps_reported": _money_num(row.get("eps")),
                    "eps_surprise_pct": _num(row.get("surprise")),
                    "revenue_estimate": None,
                    "revenue_reported": None,
                    "source": "Nasdaq earnings calendar",
                }
                by_ticker.setdefault(ticker, []).append(entry)

        _nasdaq_cache[key] = (now, by_ticker)
        return by_ticker


def _needs_nasdaq_fallback(result: dict, now_utc: datetime) -> bool:
    if result.get("upcoming"):
        return False
    recent_cutoff = now_utc - timedelta(days=NASDAQ_FALLBACK_DAYS_BACK)
    for entry in result.get("past") or []:
        try:
            dt = datetime.fromisoformat(entry.get("date") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= recent_cutoff:
                return False
        except Exception:
            continue
    return True


def _merge_event_lists(result: dict, entries: list[dict], now_utc: datetime) -> None:
    upcoming_by_date = {(e.get("date") or "")[:10]: e for e in result.get("upcoming") or []}
    past_by_date = {(e.get("date") or "")[:10]: e for e in result.get("past") or []}

    for entry in entries:
        key = (entry.get("date") or "")[:10]
        if not key:
            continue
        try:
            dt = datetime.fromisoformat(entry["date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        existing = upcoming_by_date.get(key) or past_by_date.get(key)
        if existing is not None:
            for k, v in entry.items():
                if k in RECALCULATED_EVENT_FIELDS:
                    existing[k] = v
                elif v is not None and existing.get(k) is None:
                    existing[k] = v
            continue

        if _event_is_past_for_returns(dt, entry.get("session"), now_utc):
            result["past"].append(dict(entry))
        else:
            result["upcoming"].append(dict(entry))


def _apply_nasdaq_fallback(result: dict, now_utc: datetime, hist=None, benchmark_hist=None) -> None:
    start = now_utc.date() - timedelta(days=NASDAQ_FALLBACK_DAYS_BACK)
    end = now_utc.date() + timedelta(days=NASDAQ_FALLBACK_DAYS_FORWARD)
    entries = _nasdaq_window(start, end).get((result.get("ticker") or "").upper()) or []
    for entry in entries:
        try:
            dt = datetime.fromisoformat(entry.get("date") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if _event_is_past_for_returns(dt, entry.get("session"), now_utc):
            entry.update(_trading_day_moves(hist, dt, entry.get("session")))
            _add_benchmark_moves(entry, benchmark_hist)
    if entries:
        _merge_event_lists(result, entries, now_utc)


def fetch_earnings(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    now = time.time()
    now_utc = datetime.now(timezone.utc)
    cached = _cache.get(ticker)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    result: dict = _empty_earnings(ticker)
    hist = None
    benchmark_hist = None
    should_store_snapshot = False
    try:
        t = yf.Ticker(ticker)
        try:
            info = t.info or {}
            result["name"] = info.get("shortName") or info.get("longName")
            result["sector"] = info.get("sector")
            result["industry"] = info.get("industry")
        except Exception:
            info = {}
        fiscal_year_end_month = _fiscal_year_end_month(info)

        # reported revenue map (quarterly)
        rev_map = _reported_revenue_by_quarter(t)

        # daily price history for trading-day moves around earnings
        try:
            hist = t.history(period="3y", interval="1d", auto_adjust=False)
        except Exception:
            hist = None
        benchmark_hist = _benchmark_history()

        # calendar: consensus estimates for upcoming
        cal_rev_est = None
        cal_eps_est = None
        cal_earnings_dates: set[str] = set()
        try:
            cal = t.calendar
            if isinstance(cal, dict):
                cal_rev_est = _num(cal.get("Revenue Average") or cal.get("Revenue Estimate Avg"))
                cal_eps_est = _num(cal.get("Earnings Average") or cal.get("EPS Estimate Avg"))
                cal_earnings_dates = _calendar_earnings_dates(cal)
        except Exception:
            pass

        df = None
        try:
            df = t.get_earnings_dates(limit=16)
        except Exception as e:
            result["error"] = f"earnings_dates: {e}"

        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                dt = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                eps_est = _num(row.get("EPS Estimate"))
                eps_rep = _num(row.get("Reported EPS"))
                surprise = _num(row.get("Surprise(%)"))

                entry = {
                    "date": dt.isoformat(),
                    "fiscal_period": _reported_fiscal_period(dt, fiscal_year_end_month),
                    "session": _classify_session(dt),
                    "eps_estimate": eps_est,
                    "eps_reported": eps_rep,
                    "eps_surprise_pct": surprise,
                    "revenue_estimate": None,
                    "revenue_reported": None,
                }
                matches_calendar_date = dt.date().isoformat() in cal_earnings_dates

                if not _event_is_past_for_returns(dt, entry["session"], now_utc):
                    # upcoming
                    use_calendar_estimate = matches_calendar_date or (
                        not cal_earnings_dates and not result["upcoming"]
                    )
                    entry["revenue_estimate"] = cal_rev_est if use_calendar_estimate else None
                    if eps_est is None and cal_eps_est is not None and use_calendar_estimate:
                        entry["eps_estimate"] = cal_eps_est
                    result["upcoming"].append(entry)
                else:
                    if matches_calendar_date:
                        entry["revenue_estimate"] = cal_rev_est
                    entry["revenue_reported"] = _match_reported_revenue(dt, rev_map)
                    if entry["revenue_reported"] is not None:
                        entry["revenue_source"] = "Yahoo Finance quarterly statement"
                    moves = _trading_day_moves(hist, dt, entry["session"])
                    entry.update(moves)
                    _add_benchmark_moves(entry, benchmark_hist)
                    result["past"].append(entry)

            result["upcoming"].sort(key=lambda r: r["date"])
            result["past"].sort(key=lambda r: r["date"], reverse=True)
            if result["upcoming"]:
                nxt = result["upcoming"][0]
                result["next_date"] = nxt["date"]
                result["next_session"] = nxt["session"]
                result["eps_estimate"] = nxt["eps_estimate"]
                result["revenue_estimate"] = nxt["revenue_estimate"]
    except Exception as e:
        result["error"] = str(e)

    _apply_nasdaq_fallback(result, now_utc, hist, benchmark_hist)
    for entry in (result.get("upcoming") or []) + (result.get("past") or []):
        if not entry.get("fiscal_period"):
            try:
                period_dt = datetime.fromisoformat(entry.get("date") or "")
                entry["fiscal_period"] = _reported_fiscal_period(period_dt, fiscal_year_end_month)
            except (TypeError, ValueError):
                pass
    result["upcoming"].sort(key=lambda r: r["date"])
    result["past"].sort(key=lambda r: r["date"], reverse=True)
    if result["upcoming"]:
        nxt = result["upcoming"][0]
        result["next_date"] = nxt["date"]
        result["next_session"] = nxt["session"]
        result["eps_estimate"] = nxt["eps_estimate"]
        result["revenue_estimate"] = nxt["revenue_estimate"]

    # Treat the fetch as successful if we got either upcoming or past data.
    fetch_ok = bool(result["upcoming"] or result["past"])

    if fetch_ok:
        # 1. Persist into accumulating archive (history grows forever).
        # Also archive upcoming entries — that's the only chance to capture the
        # consensus revenue estimate before it disappears (yfinance only exposes
        # estimates for the *current* upcoming quarter).
        _merge_archive(ticker, result["upcoming"] + result["past"])
        should_store_snapshot = True
        result["from_snapshot"] = False
        result["snapshot_age_seconds"] = 0
    else:
        # Live fetch failed; fall back to last known good snapshot for upcoming/name.
        with _snapshot_lock:
            snap = _snapshot.get(ticker)
        if snap and snap.get("data"):
            data = dict(snap["data"])
            data["from_snapshot"] = True
            data["snapshot_age_seconds"] = max(0, int(now - snap.get("saved_at", now)))
            data["error"] = result["error"]
            result = data
        else:
            result["from_snapshot"] = False
            result["snapshot_age_seconds"] = None

    # Always union past with the on-disk archive - this is what guarantees
    # historical earnings/returns are preserved even if yfinance later drops them.
    # Upcoming events are archived too, but they must not be surfaced through
    # "past" until they have actually crossed the same cutoff used above.
    archived = _archived_past(ticker, datetime.now(timezone.utc) - timedelta(hours=6))
    if archived:
        result["past"] = _union_past(result.get("past") or [], archived)

    # Backfill the complete retained history after the archive union. Yahoo's
    # earnings history is bounded, but SEC Company Facts often has many more
    # years of quarterly revenue available.
    missing_revenue = []
    for entry in result["past"]:
        if entry.get("eps_reported") is None or entry.get("revenue_reported") is not None:
            continue
        try:
            event_dt = datetime.fromisoformat(entry["date"])
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        missing_revenue.append((entry, event_dt))
    if missing_revenue:
        sec_rev_map = _sec_reported_revenue_by_quarter(ticker)
        for entry, event_dt in missing_revenue:
            reported = _match_reported_revenue(event_dt, sec_rev_map)
            if reported is not None:
                entry["revenue_reported"] = reported
                entry["revenue_source"] = "SEC Company Facts"

    for entry in result["past"]:
        try:
            dt = datetime.fromisoformat(entry.get("date") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            entry.update(_trading_day_moves(hist, dt, entry.get("session")))
        except Exception:
            pass
        if any(entry.get(k) is None for k in ("xlb_move_before", "xlb_move_day", "xlb_move_after")):
            _add_benchmark_moves(entry, benchmark_hist)
    # Compute revenue_surprise_pct anywhere both fields are present.
    for entry in result["past"]:
        est = entry.get("revenue_estimate")
        rep = entry.get("revenue_reported")
        if est and rep is not None and est != 0:
            entry["revenue_surprise_pct"] = float((rep - est) / est * 100.0)
        else:
            entry["revenue_surprise_pct"] = entry.get("revenue_surprise_pct")
    _merge_archive(ticker, result["past"])
    result["archive_count"] = len(_archive.get(ticker, {}))
    if should_store_snapshot:
        _store_snapshot_if_needed(ticker, result, now)

    _cache[ticker] = (now, result)
    return result


@app.route("/")
def index():
    return send_from_directory(ASSETS_DIR, "index.html")


@app.route("/api/tickers", methods=["GET", "POST"])
def api_tickers():
    if request.method == "POST":
        data = request.get_json() or {}
        tickers = data.get("tickers", [])
        save_tickers(tickers)
        _cache.clear()
    return jsonify(load_tickers())


@app.route("/api/benchmark/xlb")
def api_benchmark_xlb():
    return api_benchmark("XLB")


@app.route("/api/benchmark/<ticker>")
def api_benchmark(ticker: str):
    ticker = ticker.upper()
    if ticker not in CALENDAR_BENCHMARKS:
        return jsonify({"ticker": ticker, "returns": {}, "error": "Unsupported benchmark"}), 404
    try:
        start = date.fromisoformat(request.args.get("start", ""))
        end = date.fromisoformat(request.args.get("end", ""))
    except Exception:
        return jsonify({"ticker": ticker, "returns": {}, "error": "Invalid date range"}), 400
    if end < start or (end - start).days > 370:
        return jsonify({"ticker": ticker, "returns": {}, "error": "Invalid date range"}), 400
    return jsonify({"ticker": ticker, "returns": _benchmark_daily_returns(start, end, ticker)})


@app.route("/api/earnings")
def api_earnings():
    if request.args.get("refresh") == "1":
        _cache.clear()
    tickers = load_tickers()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        data = list(ex.map(fetch_earnings, tickers))
    return jsonify(data)


@app.route("/api/earnings/cached")
def api_earnings_cached():
    return jsonify([cached_earnings(t) for t in load_tickers()])


@app.route("/api/earnings/ticker/<ticker>")
def api_earnings_ticker(ticker: str):
    ticker = ticker.strip().upper()
    if request.args.get("refresh") == "1":
        _cache.pop(ticker, None)
    return jsonify(fetch_earnings(ticker))


@app.route("/api/earnings/ticker/<ticker>/brief", methods=["GET", "POST"])
def api_earnings_brief(ticker: str):
    ticker = ticker.strip().upper()
    repository = data_repository
    body = request.get_json(silent=True) or {}
    release_date = str(request.args.get("release_date") or body.get("release_date") or "")[:10]
    fiscal_period = str(request.args.get("fiscal_period") or body.get("fiscal_period") or "")
    cached = repository.brief_for_event(ticker, release_date, fiscal_period) if release_date and fiscal_period else repository.cached_brief(ticker)
    if request.method == "GET":
        return (jsonify(cached), 200) if cached else (jsonify({}), 404)
    if not release_date or not fiscal_period:
        return jsonify({"error": "release_date and fiscal_period are required"}), 400
    event = next((row for row in cached_earnings(ticker).get("past", [])
                  if str(row.get("date") or "")[:10] == release_date and row.get("fiscal_period") == fiscal_period), None)
    if not event or (event.get("eps_reported") is None and event.get("revenue_reported") is None):
        return jsonify({"error": "A completed matching earnings release was not found"}), 409
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return jsonify({"error": "DEEPSEEK_API_KEY is not configured"}), 503
    harness = EarningsBriefHarness(repository, key, os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    try:
        company = cached_earnings(ticker)
        return jsonify(harness.run(ticker, company.get("name"), release_date=release_date, fiscal_period=fiscal_period))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/earnings/ticker/<ticker>/briefs")
def api_earnings_briefs(ticker: str):
    return jsonify(data_repository.list_briefs(ticker.strip().upper()))


_brief_worker_lock = threading.Lock()
_brief_worker: threading.Thread | None = None


def _completed_brief_event(ticker: str) -> tuple[dict, dict] | None:
    company = cached_earnings(ticker)
    today = datetime.now(MARKET_TZ).date().isoformat()
    eligible = [event for event in company.get("past", [])
                if str(event.get("date") or "")[:10] <= today
                and event.get("fiscal_period")
                and (event.get("eps_reported") is not None or event.get("revenue_reported") is not None)]
    return (company, max(eligible, key=lambda row: str(row.get("date") or ""))) if eligible else None


def _brief_worker_loop() -> None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return
    while True:
        job = data_repository.next_brief_job()
        if not job:
            return
        try:
            EarningsBriefHarness(data_repository, key, os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")).run(
                job["ticker"], job.get("company_name"), release_date=job["release_date"], fiscal_period=job["fiscal_period"])
            data_repository.finish_brief_job(job)
        except Exception as exc:
            data_repository.finish_brief_job(job, str(exc))


def start_brief_backfill() -> dict:
    global _brief_worker
    queued = 0
    for ticker in load_tickers():
        match = _completed_brief_event(ticker)
        if not match:
            continue
        company, event = match
        if data_repository.enqueue_brief(ticker, str(event["date"])[:10], event["fiscal_period"], company.get("name")):
            queued += 1
    with _brief_worker_lock:
        if os.environ.get("DEEPSEEK_API_KEY") and (_brief_worker is None or not _brief_worker.is_alive()):
            _brief_worker = threading.Thread(target=_brief_worker_loop, name="earnings-brief-worker", daemon=True)
            _brief_worker.start()
    return {"queued_now": queued, "jobs": data_repository.brief_job_counts(), "running": bool(_brief_worker and _brief_worker.is_alive())}


@app.route("/api/research/brief-backfill", methods=["GET", "POST"])
def api_brief_backfill():
    if request.method == "POST":
        return jsonify(start_brief_backfill())
    return jsonify({"jobs": data_repository.brief_job_counts(), "running": bool(_brief_worker and _brief_worker.is_alive())})


@app.route("/api/research/storage")
def api_research_storage():
    repository = data_repository
    with repository.connect() as db:
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("earnings_briefs", "research_runs", "source_artifacts", "data_assets")}
    return jsonify({"root": str(repository.root), "database": str(repository.db_path), "counts": counts})


def _normalized_close_series(hist) -> dict[str, float]:
    if hist is None or hist.empty:
        return {}
    try:
        closes = hist["Close"].dropna()
        if closes.empty or not closes.iloc[0]:
            return {}
        base = float(closes.iloc[0])
        return {
            (idx.date() if hasattr(idx, "date") else idx).isoformat(): float(value) / base * 100.0
            for idx, value in closes.items()
        }
    except Exception:
        return {}


@app.route("/api/tape/<ticker>")
def api_tape(ticker: str):
    ticker = ticker.strip().upper()
    period = request.args.get("period", "3y")
    if ticker not in load_tickers() or period not in {"ytd", "1y", "3y", "5y"}:
        return jsonify({"error": "Invalid ticker or period"}), 400
    key = f"{ticker}:{period}"
    now = time.time()
    with _tape_cache_lock:
        cached = _tape_cache.get(key)
        if cached and now - cached[0] < BENCHMARK_CACHE_TTL:
            return jsonify(cached[1])
    company = fetch_earnings(ticker)
    try:
        stock = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
    except Exception:
        stock = None
    series = {ticker: _normalized_close_series(stock)}
    for benchmark in CALENDAR_BENCHMARKS:
        try:
            hist = yf.Ticker(benchmark).history(period=period, interval="1d", auto_adjust=True)
        except Exception:
            hist = None
        series[benchmark] = _normalized_close_series(hist)
    payload = {
        "ticker": ticker, "name": company.get("name"),
        "sector": company.get("sector"), "industry": company.get("industry"),
        "series": series,
        "events": [
            {key: event.get(key) for key in (
                "date", "fiscal_period", "eps_surprise_pct", "revenue_surprise_pct",
                "move_before", "move_day", "move_after",
            )}
            for event in company.get("past", [])
        ],
    }
    with _tape_cache_lock:
        _tape_cache[key] = (now, payload)
    return jsonify(payload)


@app.route("/api/analytics/season")
def api_season_analytics():
    companies = [cached_earnings(ticker) for ticker in load_tickers()]
    return jsonify(summarize_seasons(companies, request.args.get("quarter")))


def _optional_number(value, field: str, *, minimum=None, maximum=None):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return number


def _calendar_quarter(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{dt.year} Q{(dt.month - 1) // 3 + 1}"


def _fetch_bea_releases() -> list[dict]:
    """Fetch official BEA releases without requiring an API key."""
    global _economic_cache
    now = time.time()
    if _economic_cache and now - _economic_cache[0] < ECONOMIC_CACHE_TTL:
        return _economic_cache[1]
    releases: list[dict] = []
    try:
        from lxml import html

        response = requests.get(
            "https://www.bea.gov/news/schedule/",
            headers={"User-Agent": NASDAQ_HEADERS["User-Agent"]},
            timeout=10,
        )
        response.raise_for_status()
        tree = html.fromstring(response.content)
        heading = " ".join(tree.xpath("//table[1]//tr[1]//text()"))
        year_match = __import__("re").search(r"\b(20\d{2})\b", heading)
        schedule_year = int(year_match.group(1)) if year_match else datetime.now().year
        for row in tree.xpath("//table[1]//tr[position()>1]"):
            cells = [" ".join(c.xpath(".//text()")).strip() for c in row.xpath("./th|./td")]
            cells = [" ".join(c.split()) for c in cells]
            if len(cells) < 2:
                continue
            date_match = __import__("re").match(
                r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{1,2}:\d{2}\s+[AP]M)", cells[0]
            )
            if not date_match:
                continue
            parsed = datetime.strptime(
                f"{date_match.group(1)} {date_match.group(2)} {schedule_year} {date_match.group(3)}",
                "%B %d %Y %I:%M %p",
            ).replace(tzinfo=MARKET_TZ)
            title = cells[-1]
            releases.append({
                "id": f"BEA:{parsed.isoformat()}:{title}",
                "date": parsed.isoformat(),
                "title": title,
                "source": "U.S. Bureau of Economic Analysis",
                "source_url": "https://www.bea.gov/news/schedule/",
                "source_link_type": "calendar",
                "category": "economic",
                "country": "United States",
                "period": "",
            })
    except Exception:
        if _economic_cache:
            return _economic_cache[1]
    _economic_cache = (now, releases)
    return releases


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _fetch_bls_releases() -> list[dict]:
    """Fetch the official BLS calendar using browser-compatible TLS."""
    global _bls_cache
    now = time.time()
    if _bls_cache and now - _bls_cache[0] < ECONOMIC_CACHE_TTL:
        return _bls_cache[1]
    releases: list[dict] = []
    try:
        response = browser_requests.get(
            "https://www.bls.gov/schedule/news_release/bls.ics",
            impersonate="chrome",
            timeout=10,
        )
        response.raise_for_status()
        # RFC 5545 continuation lines begin with a space or tab.
        unfolded = __import__("re").sub(r"\r?\n[ \t]", "", response.text)
        for block in unfolded.split("BEGIN:VEVENT")[1:]:
            block = block.split("END:VEVENT", 1)[0]
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key.split(";", 1)[0]] = value
            raw_date = fields.get("DTSTART", "")
            summary = _unescape_ics(fields.get("SUMMARY", ""))
            if not raw_date or not summary:
                continue
            parsed = datetime.strptime(raw_date.rstrip("Z"), "%Y%m%dT%H%M%S")
            if raw_date.endswith("Z"):
                parsed = parsed.replace(tzinfo=timezone.utc).astimezone(MARKET_TZ)
            else:
                parsed = parsed.replace(tzinfo=MARKET_TZ)
            releases.append({
                "id": f"BLS:{fields.get('UID') or parsed.isoformat() + ':' + summary}",
                "date": parsed.isoformat(),
                "title": summary,
                "source": "U.S. Bureau of Labor Statistics",
                "source_url": "https://www.bls.gov/schedule/",
                "source_link_type": "calendar",
                "category": "economic",
                "country": "United States",
                "period": "",
            })
    except Exception:
        if _bls_cache:
            return _bls_cache[1]
    _bls_cache = (now, releases)
    return releases


def _fetch_eurostat_releases(start: date, end: date) -> list[dict]:
    """Fetch Eurostat's official euro-indicator calendar JSON."""
    try:
        response = requests.get(
            "https://ec.europa.eu/eurostat/o/calendars/eventsJson",
            params={
                "start": f"{start.isoformat()}T00:00:00Z",
                "end": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
                "theme": "",
                "category": "",
                "keywords": "",
                "isEuroindicator": "true",
            },
            headers={"User-Agent": NASDAQ_HEADERS["User-Agent"]},
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return []
    events = []
    for row in rows if isinstance(rows, list) else []:
        try:
            dt = datetime.fromisoformat(str(row.get("start") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        events.append({
            "id": f"EUROSTAT:{row.get('recordid') or dt.isoformat() + ':' + title}",
            "date": dt.isoformat(),
            "title": title,
            "period": str(row.get("period") or ""),
            "source": "Eurostat",
            "source_url": "https://ec.europa.eu/eurostat/en/news/release-calendar",
            "source_link_type": "calendar",
            "category": "economic",
            # Eurostat's euro-indicator releases generally publish both euro-area
            # and EU aggregates; labeling them as EU-only is misleading.
            "country": "Euro area / European Union",
            "dataset_codes": [
                code.strip() for code in str(row.get("datasetCodes") or "").split(",") if code.strip()
            ],
        })
    return events


def _fetch_ons_releases(start: date, end: date) -> list[dict]:
    """Fetch the official UK ONS release calendar for a bounded range."""
    try:
        from lxml import html

        trees = []
        for release_type in ("type-upcoming", "type-published"):
            response = requests.get(
                "https://www.ons.gov.uk/releasecalendar",
                params={
                "release-type": release_type,
                "after-day": start.day,
                "after-month": start.month,
                "after-year": start.year,
                "before-day": end.day,
                "before-month": end.month,
                "before-year": end.year,
                "limit": 100,
                },
                headers={"User-Agent": NASDAQ_HEADERS["User-Agent"]},
                timeout=10,
            )
            response.raise_for_status()
            trees.append(html.fromstring(response.content))
    except Exception:
        return []
    events = []
    seen = set()
    economic_terms = (
        "gross domestic product", "gdp", "inflation", "consumer price",
        "producer price", "retail sales", "trade", "labour market",
        "employment", "unemployment", "earnings", "wages", "productivity",
        "industrial production", "manufacturing", "business investment",
        "public sector finances", "balance of payments",
    )
    for link in [node for tree in trees for node in tree.xpath("//a[@data-gtm-release-date]")]:
        raw_date = link.get("data-gtm-release-date") or ""
        raw_time = link.get("data-gtm-release-time") or "07:00"
        try:
            dt = datetime.strptime(raw_date + raw_time, "%Y%m%d%H:%M").replace(
                tzinfo=ZoneInfo("Europe/London")
            )
        except ValueError:
            continue
        if not start <= dt.date() <= end:
            continue
        title = " ".join(" ".join(link.xpath(".//text()")).split())
        if not any(term in title.lower() for term in economic_terms):
            continue
        href = link.get("href") or ""
        if href in seen:
            continue
        seen.add(href)
        events.append({
            "id": f"ONS:{href or dt.isoformat() + ':' + title}",
            "date": dt.isoformat(),
            "title": title,
            "period": "",
            "source": "UK Office for National Statistics",
            "source_url": f"https://www.ons.gov.uk{href}",
            "source_link_type": "release",
            "category": "economic",
            "country": "United Kingdom",
        })
    return events


def _fetch_ibge_releases(start: date, end: date) -> list[dict]:
    """Fetch future releases from Brazil's official IBGE calendar."""
    try:
        from lxml import html

        response = browser_requests.get(
            "https://www.ibge.gov.br/en/calendario-de-divulgacoes-novoportal-2.html",
            impersonate="chrome",
            timeout=10,
        )
        response.raise_for_status()
        tree = html.fromstring(response.content)
    except Exception:
        return []
    events = []
    for date_node in tree.xpath(
        '//*[contains(concat(" ", normalize-space(@class), " "), " calendario-data ")]'
    ):
        raw_date = " ".join(date_node.xpath(".//text()")).strip()
        try:
            event_day = datetime.strptime(raw_date, "%m/%d/%Y").date()
        except ValueError:
            continue
        if not start <= event_day <= end:
            continue
        parent = date_node.getparent()
        name = " ".join(
            " ".join(parent.xpath(
                './/*[contains(concat(" ", normalize-space(@class), " "), " calendario-nome ")]//text()'
            )).split()
        )
        abbreviation = " ".join(
            " ".join(parent.xpath(
                './/*[contains(concat(" ", normalize-space(@class), " "), " calendario-sigla ")]//text()'
            )).split()
        )
        if not name:
            continue
        title = f"{abbreviation} — {name}" if abbreviation else name
        dt = datetime.combine(event_day, datetime.min.time().replace(hour=9), ZoneInfo("America/Sao_Paulo"))
        events.append({
            "id": f"IBGE:{event_day.isoformat()}:{title}",
            "date": dt.isoformat(),
            "title": title,
            "period": "",
            "source": "Brazilian Institute of Geography and Statistics",
            "source_url": "https://www.ibge.gov.br/en/calendario-de-divulgacoes-novoportal-2.html",
            "source_link_type": "calendar",
            "category": "economic",
            "country": "Brazil",
        })
    return events


def _fetch_china_nbs_releases(start: date, end: date) -> list[dict]:
    """Parse China's official annual NBS press-release calendar."""
    if start.year != end.year:
        years = range(start.year, end.year + 1)
    else:
        years = (start.year,)
    known_urls = {
        2026: "https://www.stats.gov.cn/english/PressRelease/ReleaseCalendar/202512/t20251226_1962154.html",
    }
    events = []
    try:
        from lxml import html

        for year in years:
            url = known_urls.get(year)
            if not url:
                continue
            response = requests.get(url, headers={"User-Agent": NASDAQ_HEADERS["User-Agent"]}, timeout=10)
            response.raise_for_status()
            rows = html.fromstring(response.content).xpath("//table//tr")
            for index, row in enumerate(rows[1:], start=1):
                cells = [" ".join(" ".join(cell.xpath(".//text()")).split()) for cell in row.xpath("./th|./td")]
                if len(cells) != 14 or not cells[0].isdigit():
                    continue
                title = cells[1]
                time_cells = []
                if index + 1 < len(rows):
                    time_cells = __import__("re").findall(
                        r"\b\d{1,2}:\d{2}\b", " ".join(rows[index + 1].xpath(".//text()"))
                    )
                time_index = 0
                for month, raw_day in enumerate(cells[2:], start=1):
                    day_match = __import__("re").match(r"(\d{1,2})/", raw_day)
                    if not day_match:
                        continue
                    clock = time_cells[time_index] if time_index < len(time_cells) else "10:00"
                    time_index += 1
                    event_day = date(year, month, int(day_match.group(1)))
                    if not start <= event_day <= end:
                        continue
                    hour, minute = map(int, clock.split(":"))
                    dt = datetime(year, month, event_day.day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
                    events.append({
                        "id": f"CHINA-NBS:{event_day.isoformat()}:{title}",
                        "date": dt.isoformat(),
                        "title": title,
                        "period": "",
                        "source": "National Bureau of Statistics of China",
                        "source_url": url,
                        "source_link_type": "calendar",
                        "source_timezone": "Asia/Shanghai",
                        "category": "economic",
                        "country": "China",
                    })
    except Exception:
        return events
    return events


@app.route("/api/forecasts", methods=["GET", "POST"])
def api_forecasts():
    if request.method == "GET":
        event_id = request.args.get("event_id")
        records = forecast_journal.for_event(event_id) if event_id else forecast_journal.latest()
        return jsonify({"records": records, "integrity": forecast_journal.verify()})

    body = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker") or "").strip().upper()
    event_date = str(body.get("event_date") or "").strip()
    call = str(body.get("call") or "").strip().lower()
    side = str(body.get("paper_side") or "none").strip().lower()
    if not ticker or not event_date:
        return jsonify({"error": "ticker and event_date are required"}), 400
    if call not in {"beat", "miss", "inline"}:
        return jsonify({"error": "call must be beat, miss, or inline"}), 400
    if side not in {"long", "short", "none"}:
        return jsonify({"error": "paper_side must be long, short, or none"}), 400
    try:
        fiscal_period = str(body.get("fiscal_period") or "").strip() or None
        event_id = forecast_journal.event_id(ticker, event_date, fiscal_period)
        data = {
            "call": call,
            "eps_estimate": _optional_number(body.get("eps_estimate"), "eps_estimate"),
            "revenue_estimate": _optional_number(body.get("revenue_estimate"), "revenue_estimate"),
            "confidence": _optional_number(body.get("confidence"), "confidence", minimum=0, maximum=100),
            "paper_side": side,
            "paper_size": _optional_number(body.get("paper_size"), "paper_size", minimum=0),
            "notes": str(body.get("notes") or "")[:5000],
            "consensus_snapshot": body.get("consensus_snapshot") or {},
        }
        record = forecast_journal.append(
            event_id=event_id, ticker=ticker, event_date=event_date, data=data
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"record": record, "integrity": forecast_journal.verify()}), 201


@app.route("/api/forecasts/integrity")
def api_forecast_integrity():
    return jsonify(forecast_journal.verify())


@app.route("/api/notes", methods=["GET", "POST"])
def api_notes():
    if request.method == "GET":
        note_id = request.args.get("note_id")
        records = notes_journal.history(note_id) if note_id else notes_journal.latest()
        ticker = (request.args.get("ticker") or "").upper()
        quarter = request.args.get("quarter") or ""
        if ticker:
            records = [r for r in records if r["data"].get("ticker") == ticker]
        if quarter:
            records = [r for r in records if r["data"].get("calendar_quarter") == quarter]
        return jsonify({"records": records, "integrity": notes_journal.verify()})

    body = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker") or "").strip().upper()
    event_date = str(body.get("event_date") or "").strip()
    title = str(body.get("title") or "").strip()
    note_body = str(body.get("body") or "").strip()
    if not ticker or not event_date or not title or not note_body:
        return jsonify({"error": "ticker, event_date, title, and body are required"}), 400
    try:
        data = {
            "ticker": ticker,
            "event_date": event_date,
            "fiscal_period": str(body.get("fiscal_period") or ""),
            "calendar_quarter": _calendar_quarter(event_date),
            "title": title[:300],
            "body": note_body[:20000],
            "tags": [str(tag).strip()[:60] for tag in (body.get("tags") or []) if str(tag).strip()][:20],
        }
        record = notes_journal.append(data, str(body.get("note_id") or "") or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"record": record, "integrity": notes_journal.verify()}), 201


@app.route("/api/economic-releases")
def api_economic_releases():
    global _economic_cache, _bls_cache
    try:
        start = date.fromisoformat(request.args.get("start", ""))
        end = date.fromisoformat(request.args.get("end", ""))
    except ValueError:
        return jsonify({"events": [], "error": "Invalid date range"}), 400
    if end < start or (end - start).days > 370:
        return jsonify({"events": [], "error": "Invalid date range"}), 400
    refresh = request.args.get("refresh") == "1"
    cache_key = (start, end)
    if refresh:
        _economic_cache = None
        _bls_cache = None
        _economic_range_cache.clear()

    cached = _economic_range_cache.get(cache_key)
    cache_hit = bool(cached and time.time() - cached[0] < ECONOMIC_CACHE_TTL)
    if cache_hit:
        events = cached[1]
    else:
        fetchers = (
            _fetch_bea_releases,
            _fetch_bls_releases,
            lambda: _fetch_eurostat_releases(start, end),
            lambda: _fetch_ons_releases(start, end),
            lambda: _fetch_ibge_releases(start, end),
            lambda: _fetch_china_nbs_releases(start, end),
        )
        with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
            provider_events = list(executor.map(lambda fetch: fetch(), fetchers))
        events = [
            event
            for provider in provider_events
            for event in provider
            if start <= datetime.fromisoformat(event["date"]).date() <= end
        ]
        events.sort(key=lambda event: (
            datetime.fromisoformat(event["date"]).astimezone(timezone.utc),
            event["title"],
        ))
        _economic_range_cache[cache_key] = (time.time(), events)
        if len(_economic_range_cache) > 12:
            oldest_key = min(_economic_range_cache, key=lambda key: _economic_range_cache[key][0])
            _economic_range_cache.pop(oldest_key, None)
    return jsonify({
        "events": events,
        "cached": cache_hit,
        "sources": [
            "U.S. Bureau of Economic Analysis",
            "U.S. Bureau of Labor Statistics",
            "Eurostat",
            "UK Office for National Statistics",
            "Brazilian Institute of Geography and Statistics",
            "National Bureau of Statistics of China",
        ],
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False, threaded=True)
