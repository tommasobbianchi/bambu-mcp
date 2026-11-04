"""Paths, printer credentials (cached outside the repo, mode 0600), safety switches.

Secrets (access codes, serials) never leave this module except to the MQTT/FTPS layers.
`scrub()` removes them from any text that is logged or returned.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SSH_HOST = os.environ.get("BAMBU_MCP_SSH_HOST", "streamer-officina")
SSH_REMOTE_FILE = os.environ.get("BAMBU_MCP_SSH_FILE", "/home/tommaso/mmu_say/bambu.json")

_SECRETS: set[str] = set()


def config_dir() -> Path:
    return Path(os.environ.get("BAMBU_MCP_CONFIG_DIR", "~/.config/bambu_mcp")).expanduser()


def state_dir() -> Path:
    d = Path(os.environ.get("BAMBU_MCP_STATE_DIR", "~/.local/state/bambu_mcp")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_path() -> Path:
    return state_dir() / "events.jsonl"


def env_dry_run() -> bool:
    return os.environ.get("BAMBU_MCP_DRY_RUN", "").lower() in ("1", "true", "yes")


def readonly_set() -> set[str]:
    """Printers (or '*') that must never receive control commands (BAMBU_MCP_READONLY=a1mini,p1s)."""
    return {x.strip() for x in os.environ.get("BAMBU_MCP_READONLY", "").split(",") if x.strip()}


def is_readonly(name: str) -> bool:
    ro = readonly_set()
    return "*" in ro or name in ro


@dataclass
class Printer:
    name: str
    ip: str
    serial: str = field(repr=False)
    code: str = field(repr=False)
    label: str = ""

    def public(self) -> dict:
        return {"name": self.name, "label": self.label or self.name, "ip": self.ip,
                "readonly": is_readonly(self.name)}


def scrub(text) -> str:
    s = str(text)
    for sec in _SECRETS:
        if sec and len(sec) >= 4:
            s = s.replace(sec, "***")
    return s


def _fetch_remote(dest: Path) -> None:
    """One-off fetch of the credentials JSON over ssh (hard timeout). Result cached at `dest`."""
    r = subprocess.run(
        ["timeout", "25", "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_HOST,
         f"cat {SSH_REMOTE_FILE}"],
        capture_output=True, text=True, timeout=35, stdin=subprocess.DEVNULL)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"cannot fetch printer credentials from {SSH_HOST} (rc={r.returncode})")
    json.loads(r.stdout)  # validate
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(r.stdout)
    os.chmod(dest, 0o600)


def load_printers() -> dict[str, Printer]:
    path = config_dir() / "printers.json"
    if not path.exists():
        _fetch_remote(path)
    raw = json.loads(path.read_text())
    out: dict[str, Printer] = {}
    for key, v in raw.items():
        if not isinstance(v, dict) or not {"ip", "serial", "code"} <= set(v):
            continue
        p = Printer(name=key, ip=v["ip"], serial=v["serial"], code=v["code"], label=v.get("name", ""))
        out[key] = p
        _SECRETS.update({p.serial, p.code})
    return out
