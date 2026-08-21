"""Build a standalone Windows .exe with PyInstaller."""
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).parent
    src = root / "src"
    assets = src / "earnings_cal" / "assets"
    for d in ("build", "dist"):
        p = root / d
        if p.exists():
            shutil.rmtree(p)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "EarningsCalendar",
        "--icon", str(assets / "icon.ico"),
        "--paths", str(src),
        "--add-data", f"{assets};assets",
        "--collect-all", "yfinance",
        "--collect-all", "edgar",
        "--collect-submodules", "webview",
        str(src / "earnings_cal" / "desktop.py"),
    ]
    print(">>", " ".join(args))
    subprocess.check_call(args, cwd=root)
    out = root / "dist" / "EarningsCalendar.exe"
    print(f"\nBuilt: {out} ({out.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
