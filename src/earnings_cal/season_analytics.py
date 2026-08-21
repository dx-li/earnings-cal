"""Pure calculations for calendar-quarter earnings season analytics."""

from __future__ import annotations

from datetime import datetime
from statistics import median


def _quarter(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{dt.year} Q{(dt.month - 1) // 3 + 1}"


def _outcome(estimate, reported) -> str | None:
    if estimate is None or reported is None:
        return None
    if reported > estimate:
        return "beat"
    if reported < estimate:
        return "miss"
    return "inline"


def _stats(values) -> dict:
    clean = [float(value) for value in values if value is not None]
    return {
        "n": len(clean),
        "average": sum(clean) / len(clean) if clean else None,
        "median": median(clean) if clean else None,
        "positive_pct": 100 * sum(value > 0 for value in clean) / len(clean) if clean else None,
    }


def _rate(events: list[dict], field: str, label: str) -> dict:
    eligible = [event for event in events if event[field] is not None]
    return {
        "n": len(eligible),
        "count": sum(event[field] == label for event in eligible),
        "pct": 100 * sum(event[field] == label for event in eligible) / len(eligible) if eligible else None,
    }


def _season(events: list[dict], quarter: str) -> dict:
    rows = [event for event in events if event["quarter"] == quarter]
    both = [event for event in rows if event["eps_outcome"] and event["revenue_outcome"]]

    def reactions(field: str, label: str) -> dict:
        return _stats(event["move_day"] for event in rows if event[field] == label)

    def window(field: str, label: str) -> dict:
        matching = [event for event in rows if event[field] == label]
        return {
            "before": _stats(event["move_before"] for event in matching),
            "day": _stats(event["move_day"] for event in matching),
            "after": _stats(event["move_after"] for event in matching),
        }

    oracle = []
    for event in rows:
        if event["move_day"] is None:
            continue
        if event["eps_outcome"] == "beat":
            oracle.append(event["move_day"])
        elif event["eps_outcome"] == "miss":
            oracle.append(-event["move_day"])

    return {
        "quarter": quarter,
        "events": len(rows),
        "eps": {label: _rate(rows, "eps_outcome", label) for label in ("beat", "miss", "inline")},
        "revenue": {label: _rate(rows, "revenue_outcome", label) for label in ("beat", "miss", "inline")},
        "double_beat": _rate(both, "combo", "double beat"),
        "double_miss": _rate(both, "combo", "double miss"),
        "reaction": {
            "all": _stats(event["move_day"] for event in rows),
            "eps_beat": reactions("eps_outcome", "beat"),
            "eps_miss": reactions("eps_outcome", "miss"),
            "revenue_beat": reactions("revenue_outcome", "beat"),
            "revenue_miss": reactions("revenue_outcome", "miss"),
            "double_beat": reactions("combo", "double beat"),
            "double_miss": reactions("combo", "double miss"),
        },
        "windows": {
            "all": {
                "before": _stats(event["move_before"] for event in rows),
                "day": _stats(event["move_day"] for event in rows),
                "after": _stats(event["move_after"] for event in rows),
            },
            "eps_beat": window("eps_outcome", "beat"),
            "eps_miss": window("eps_outcome", "miss"),
            "revenue_beat": window("revenue_outcome", "beat"),
            "revenue_miss": window("revenue_outcome", "miss"),
            "double_beat": window("combo", "double beat"),
            "double_miss": window("combo", "double miss"),
        },
        "relative_reaction": _stats(event["relative_move_day"] for event in rows),
        "pre_drift": {
            "eps_beat": _stats(event["move_before"] for event in rows if event["eps_outcome"] == "beat"),
            "eps_miss": _stats(event["move_before"] for event in rows if event["eps_outcome"] == "miss"),
        },
        "follow_through": {
            "eps_beat": _stats(event["move_after"] for event in rows if event["eps_outcome"] == "beat"),
            "eps_miss": _stats(event["move_after"] for event in rows if event["eps_outcome"] == "miss"),
        },
        "oracle_eps": _stats(oracle),
    }


def summarize_seasons(companies: list[dict], selected: str | None = None) -> dict:
    events = []
    for company in companies:
        for raw in company.get("past", []):
            if not raw.get("date"):
                continue
            eps = _outcome(raw.get("eps_estimate"), raw.get("eps_reported"))
            revenue = _outcome(raw.get("revenue_estimate"), raw.get("revenue_reported"))
            combo = None
            if eps and revenue:
                combo = "double beat" if eps == revenue == "beat" else "double miss" if eps == revenue == "miss" else "mixed"
            events.append({
                "quarter": _quarter(raw["date"]), "eps_outcome": eps,
                "revenue_outcome": revenue, "combo": combo,
                "move_day": raw.get("move_day"), "move_before": raw.get("move_before"),
                "move_after": raw.get("move_after"),
                "relative_move_day": raw.get("relative_move_day"),
            })
    quarters = sorted({event["quarter"] for event in events}, reverse=True)
    if not quarters:
        return {"quarters": [], "current": None, "previous": None, "history": []}
    chosen = selected if selected in quarters else quarters[0]
    index = quarters.index(chosen)
    previous = quarters[index + 1] if index + 1 < len(quarters) else None
    return {
        "quarters": quarters,
        "current": _season(events, chosen),
        "previous": _season(events, previous) if previous else None,
        "history": [_season(events, quarter) for quarter in reversed(quarters)],
    }
