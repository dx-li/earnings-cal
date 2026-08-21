"""Build the isolated Earnings Research Lab executable."""
import subprocess, sys
from pathlib import Path

root=Path(__file__).parent; src=root/"src"; assets=src/"earnings_cal"/"research_assets"; out=root/"dist-research"; work=root/"build-research"
args=[sys.executable,"-m","PyInstaller","--noconfirm","--onefile","--windowed","--name","EarningsResearchLab","--distpath",str(out),"--workpath",str(work),"--specpath",str(work),"--paths",str(src),"--add-data",f"{assets};research_assets","--collect-submodules","webview",str(src/"earnings_cal"/"research_desktop.py")]
print(">>"," ".join(args)); subprocess.check_call(args,cwd=root); exe=out/"EarningsResearchLab.exe"; print(f"Built: {exe} ({exe.stat().st_size/1e6:.1f} MB)")
