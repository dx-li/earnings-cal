"""Research Lab settings with Windows DPAPI-protected provider secrets."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


DEFAULTS = {
    "provider": "openai", "model": "gpt-5-mini", "start_year": 2000, "max_cost_usd": 10.0,
    "confidence_threshold": 0.75, "documents_per_run": 25,
    "transcript_provider": "manual", "openai_api_key_configured": False,
    "deepseek_api_key_configured": False,
    "transcript_api_key_configured": False,
}


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(raw: bytes) -> str:
    if os.name != "nt":
        return base64.b64encode(raw).decode()
    buf = ctypes.create_string_buffer(raw); src = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))); out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "Earnings Research Lab", None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try: return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally: ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(value: str) -> bytes:
    raw = base64.b64decode(value)
    if os.name != "nt": return raw
    buf = ctypes.create_string_buffer(raw); src = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))); out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try: return ctypes.string_at(out.pbData, out.cbData)
    finally: ctypes.windll.kernel32.LocalFree(out.pbData)


class ResearchConfig:
    def __init__(self, root: Path):
        self.settings_path = root / "settings.json"; self.secrets_path = root / "secrets.dpapi"

    def load(self) -> dict:
        try: settings = {**DEFAULTS, **json.loads(self.settings_path.read_text())}
        except Exception: settings = dict(DEFAULTS)
        secrets = self.secrets()
        openai_configured = bool(secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"))
        deepseek_configured = bool(secrets.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY"))
        settings["openai_api_key_saved"] = openai_configured
        settings["openai_api_key_configured"] = deepseek_configured if settings.get("provider") == "deepseek" else openai_configured
        settings["deepseek_api_key_configured"] = deepseek_configured
        settings["transcript_api_key_configured"] = bool(secrets.get("transcript_api_key"))
        return settings

    def secrets(self) -> dict:
        try: return json.loads(_unprotect(self.secrets_path.read_text()).decode())
        except Exception: return {}

    def save(self, values: dict) -> dict:
        current = self.load()
        allowed = {k for k in DEFAULTS if not k.endswith("_configured")}
        settings = {k: values.get(k, current.get(k)) for k in allowed}
        self.settings_path.write_text(json.dumps(settings, indent=2))
        secrets = self.secrets()
        for key in ("openai_api_key", "deepseek_api_key", "transcript_api_key"):
            if values.get(key): secrets[key] = str(values[key]).strip()
            if values.get(f"clear_{key}"): secrets.pop(key, None)
        self.secrets_path.write_text(_protect(json.dumps(secrets).encode()))
        return self.load()
