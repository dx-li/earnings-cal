from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import yfinance as yf


@dataclass(frozen=True)
class Call:
    ticker: str
    side: int

    @property
    def side_label(self) -> str:
        return "L" if self.side == 1 else "S"


@dataclass(frozen=True)
class AnalysisConfig:
    start_date: date
    end_date: date
    earnings_json: Path


def parse_calls(text: str) -> list[Call]:
    """Parse ticker/side lines such as `SHW L` or `$SHW - L`."""
    calls: list[Call] = []
    pending_ticker: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = (
            line.replace("$", "")
            .replace("-", " ")
            .replace("?", "")
            .replace(",", " ")
            .split()
        )
        if not parts:
            continue

        if len(parts) == 1:
            token = parts[0].upper()
            if token in {"L", "LONG", "S", "SHORT"} and pending_ticker:
                calls.append(Call(pending_ticker, _parse_side(token)))
                pending_ticker = None
            else:
                pending_ticker = token
            continue

        ticker = parts[0].upper()
        side_token = next(
            (p.upper() for p in parts[1:] if p.upper() in {"L", "LONG", "S", "SHORT"}),
            None,
        )
        if side_token is None:
            pending_ticker = ticker
            continue
        calls.append(Call(ticker, _parse_side(side_token)))
        pending_ticker = None

    return calls


def _parse_side(token: str) -> int:
    token = token.upper()
    if token in {"L", "LONG"}:
        return 1
    if token in {"S", "SHORT"}:
        return -1
    raise ValueError(f"Unknown side: {token}")


def load_earnings(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text())
    return {str(row["ticker"]).upper(): row for row in data}


def fetch_adjusted_closes(
    tickers: Iterable[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Fetch adjusted close prices. yfinance end dates are exclusive."""
    symbols = list(dict.fromkeys(tickers))
    download = yf.download(
        symbols,
        start=(start - timedelta(days=10)).isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    if download.empty:
        raise RuntimeError("No price data returned by yfinance")

    if isinstance(download.columns, pd.MultiIndex):
        closes = pd.DataFrame({ticker: download[(ticker, "Close")] for ticker in symbols})
    else:
        closes = pd.DataFrame({symbols[0]: download["Close"]})

    return closes.dropna(how="all")


def run_analysis(calls: list[Call], config: AnalysisConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    earnings = load_earnings(config.earnings_json)
    tickers = [call.ticker for call in calls]
    closes = fetch_adjusted_closes(tickers, config.start_date, config.end_date)

    start_idx = closes.index[closes.index >= pd.Timestamp(config.start_date)]
    if len(start_idx) == 0:
        raise RuntimeError(f"No trading date on or after {config.start_date}")
    start_ts = start_idx[0]
    latest_ts = closes.index[closes.index <= pd.Timestamp(config.end_date)][-1]

    rows: list[dict] = []
    active_returns = pd.DataFrame(0.0, index=closes.pct_change().index, columns=tickers)

    for call in calls:
        if call.ticker not in earnings:
            raise KeyError(f"Missing earnings data for {call.ticker} in {config.earnings_json}")

        prices = closes[call.ticker].dropna()
        start_price = prices.loc[start_ts]
        latest_price = prices.loc[latest_ts]
        underlying_period_return = latest_price / start_price - 1.0
        signed_period_return = call.side * underlying_period_return

        event = _event_window(prices, earnings[call.ticker], latest_ts)
        event_return = None
        event_hit = None
        if event is not None:
            event_return = call.side * (prices.loc[event["exit"]] / prices.loc[event["entry"]] - 1.0)
            event_hit = event_return > 0
            pct = prices.pct_change()
            mask = (pct.index > event["entry"]) & (pct.index <= event["exit"])
            active_returns.loc[mask, call.ticker] = call.side * pct.loc[mask]

        rows.append(
            {
                "ticker": call.ticker,
                "side": call.side_label,
                "earnings_date": _earnings_date(earnings[call.ticker]).isoformat(),
                "session": earnings[call.ticker].get("next_session"),
                "period_return": signed_period_return,
                "period_hit": signed_period_return > 0,
                "event_entry": event["entry"].date().isoformat() if event is not None else None,
                "event_exit": event["exit"].date().isoformat() if event is not None else None,
                "event_return": event_return,
                "event_hit": event_hit,
            }
        )

    detail = pd.DataFrame(rows)
    summary = summarize(detail, closes, calls, start_ts, latest_ts, active_returns)
    return summary, detail


def _earnings_date(row: dict) -> date:
    return datetime.fromisoformat(row["next_date"]).date()


def _event_window(prices: pd.Series, earnings_row: dict, latest_ts: pd.Timestamp) -> dict | None:
    event_date = pd.Timestamp(_earnings_date(earnings_row))
    session = earnings_row.get("next_session")
    dates = list(prices.index)

    if session == "AMC":
        event_idx = next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= event_date), None)
        d0_idx = next((i for i, ts in enumerate(dates) if ts > event_date), None)
        if event_idx is None or d0_idx is None or event_idx - 1 < 0 or d0_idx + 1 >= len(dates):
            return None
        entry = dates[event_idx - 1]
        exit_ = dates[d0_idx + 1]
    else:
        d0_idx = next((i for i, ts in enumerate(dates) if ts >= event_date), None)
        if d0_idx is None or d0_idx - 2 < 0 or d0_idx + 1 >= len(dates):
            return None
        entry = dates[d0_idx - 2]
        exit_ = dates[d0_idx + 1]

    if exit_ > latest_ts:
        return None
    return {"entry": entry, "exit": exit_}


def summarize(
    detail: pd.DataFrame,
    closes: pd.DataFrame,
    calls: list[Call],
    start_ts: pd.Timestamp,
    latest_ts: pd.Timestamp,
    active_returns: pd.DataFrame,
) -> pd.DataFrame:
    side = pd.Series({call.ticker: call.side for call in calls})
    tickers = [call.ticker for call in calls]
    daily_returns = closes[tickers].pct_change()

    held_rebalanced = daily_returns.loc[daily_returns.index > start_ts].mul(side, axis=1).mean(axis=1)
    nav = 1 + closes.loc[(closes.index >= start_ts) & (closes.index <= latest_ts), tickers].div(
        closes.loc[start_ts, tickers]
    ).sub(1).mul(side, axis=1).mean(axis=1)
    held_buy_hold = nav.pct_change().dropna()

    event_trade_returns = detail["event_return"].dropna()
    active_counts = (active_returns != 0).sum(axis=1)
    event_active_daily = (active_returns.sum(axis=1) / active_counts.replace(0, pd.NA)).dropna()
    event_fixed_daily = pd.Series(dtype=float)
    if len(event_trade_returns):
        dated = detail.dropna(subset=["event_entry", "event_exit"])
        first = pd.Timestamp(dated["event_entry"].min())
        last = pd.Timestamp(dated["event_exit"].max())
        event_fixed_daily = active_returns.sum(axis=1).loc[first:last] / len(event_trade_returns)

    rows = [
        _summary_row("period_buy_hold", detail["period_return"], detail["period_hit"], held_buy_hold, True),
        _summary_row("period_daily_rebalanced", detail["period_return"], detail["period_hit"], held_rebalanced, True),
        _summary_row("event_trade_level", event_trade_returns, detail["event_hit"].dropna(), None, False),
        _summary_row("event_active_daily", event_trade_returns, detail["event_hit"].dropna(), event_active_daily, True),
        _summary_row("event_fixed_capital_daily", event_trade_returns, detail["event_hit"].dropna(), event_fixed_daily, True),
    ]
    return pd.DataFrame(rows)


def _summary_row(
    name: str,
    returns: pd.Series,
    hits: pd.Series,
    daily_stream: pd.Series | None,
    annualized: bool,
) -> dict:
    returns = pd.Series(returns).dropna()
    hits = pd.Series(hits).dropna().astype(bool)
    stream = pd.Series(daily_stream).dropna() if daily_stream is not None else returns
    sharpe = _sharpe(stream, annualized=annualized)
    if name == "event_trade_level" and len(stream) > 0 and not math.isnan(sharpe):
        scaled_sharpe = sharpe * math.sqrt(len(stream))
    else:
        scaled_sharpe = None
    return {
        "strategy": name,
        "n": int(len(returns)),
        "hit_rate": float(hits.mean()) if len(hits) else None,
        "avg_return": float(returns.mean()) if len(returns) else None,
        "median_return": float(returns.median()) if len(returns) else None,
        "sharpe": sharpe,
        "scaled_trade_sharpe": scaled_sharpe,
    }


def _sharpe(returns: pd.Series, annualized: bool) -> float:
    returns = pd.Series(returns).dropna()
    if len(returns) < 2:
        return float("nan")
    std = returns.std(ddof=1)
    if std == 0 or pd.isna(std):
        return float("nan")
    scale = math.sqrt(252) if annualized else 1.0
    return float(returns.mean() / std * scale)


def _format_pct(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _print_report(summary: pd.DataFrame, detail: pd.DataFrame) -> None:
    printable_summary = summary.copy()
    for col in ("hit_rate", "avg_return", "median_return"):
        printable_summary[col] = printable_summary[col].map(_format_pct)
    for col in ("sharpe", "scaled_trade_sharpe"):
        printable_summary[col] = printable_summary[col].map(
            lambda v: "" if v is None or pd.isna(v) else f"{float(v):.2f}"
        )
    print("\nSummary")
    print(printable_summary.to_string(index=False))

    printable_detail = detail.copy()
    for col in ("period_return", "event_return"):
        printable_detail[col] = printable_detail[col].map(_format_pct)
    print("\nDetail")
    print(printable_detail.to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze long/short earnings calls.")
    parser.add_argument("--calls-file", required=True, type=Path)
    parser.add_argument("--earnings-json", type=Path, default=Path("data/raw/e.json"))
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 4, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--summary-csv", type=Path)
    parser.add_argument("--detail-csv", type=Path)
    args = parser.parse_args(argv)

    calls = parse_calls(args.calls_file.read_text())
    if not calls:
        raise SystemExit("No calls parsed from calls file")

    summary, detail = run_analysis(
        calls,
        AnalysisConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            earnings_json=args.earnings_json,
        ),
    )
    _print_report(summary, detail)

    if args.summary_csv:
        args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(args.summary_csv, index=False)
    if args.detail_csv:
        args.detail_csv.parent.mkdir(parents=True, exist_ok=True)
        detail.to_csv(args.detail_csv, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
