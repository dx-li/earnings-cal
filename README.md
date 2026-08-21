# Earnings Tracker

Desktop earnings calendar and auditable forecasting journal. Forecast submissions and edits are stored as append-only, hash-chained revisions in `%APPDATA%\earnings-cal\forecast-audit.jsonl`; the Tracker tab verifies and displays the chain.

Economic releases are loaded from official BEA, BLS, Eurostat, UK ONS, Brazil
IBGE, and China NBS calendars. The dedicated Economic tab is filterable by
region; the earnings-calendar overlay is optional and off by default.

## Development

```powershell
uv sync --all-groups
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m earnings_cal.app
```

The Flask development server runs on `http://127.0.0.1:5057`.

The Company tab can generate a cached, source-linked latest-earnings brief. Put
`DEEPSEEK_API_KEY=...` in `.env`; filings are retrieved through EdgarTools and
the bounded agent searches for a public earnings transcript when needed. Set an
SEC-compliant identity with `EDGAR_IDENTITY="Your Name you@example.com"`.

Research data is stored outside Windows AppData under `data/research/`:

- `earnings-research.sqlite3` catalogs cached briefs, runs, and source lineage.
- `lake/bronze/` retains immutable tool/source snapshots.
- `lake/curated/earnings-briefs/` retains human-readable brief JSON.
- `lake/operational/` stores the ticker universe, current earnings snapshot,
  accumulating event archive, settings, forecast journal, and notes journal.

Set `EARNINGS_DATA_ROOT` in `.env` to move the database and lake to another
drive. Existing AppData brief JSON is copied into this store on first use; the
legacy files are left untouched.

On first launch, the operational files are also copied from `%APPDATA%\earnings-cal`
into the lake and registered in SQLite's `data_assets` catalog. The application
then reads and writes the lake copies; legacy files remain as a rollback copy.

## Desktop App

```powershell
.venv\Scripts\python.exe -m earnings_cal.desktop
```

## Build

```powershell
.venv\Scripts\python.exe build.py
```

The standalone executable is written to `dist\EarningsCalendar.exe`.

## Earnings Call Analysis

The reusable analysis CLI computes hit rate, equal-weight returns, and Sharpe
for a long/short calls file. It uses the same calendar convention as the app:
`D-1`, `D0`, and `D+1`, with `D0` on the next trading day for AMC reports.

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m earnings_cal.call_analysis `
  --calls-file data\calls\materials_may_2026.txt `
  --earnings-json data\raw\e.json `
  --start-date 2026-04-01 `
  --end-date 2026-05-12 `
  --summary-csv data\analysis\materials_may_2026_summary.csv `
  --detail-csv data\analysis\materials_may_2026_detail.csv
```

Calls files can use either compact lines such as `SHW L` or the pasted format
with tickers and `L`/`S` on separate lines.

## Project Layout

```text
src/earnings_cal/        Python package
src/earnings_cal/assets/ Bundled HTML, default tickers, and icons
data/calls/              Saved long/short call lists for analysis
tools/                   Developer utilities
data/raw/                Local raw data exports, ignored by git
```

## Runtime Data

Editable ticker lists, snapshots, and the earnings archive are stored in:

```text
%APPDATA%\earnings-cal
```
