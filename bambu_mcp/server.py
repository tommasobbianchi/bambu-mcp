"""MCP server (stdio) for Bambu Lab printers over the LAN (MQTT/TLS + FTPS) with a background job manager.

Run:  ~/projects/bambu_mcp/.venv/bin/python -m bambu_mcp   (cwd = ~/projects/bambu_mcp)
Env:  BAMBU_MCP_DRY_RUN=1        build payloads but never publish/upload
      BAMBU_MCP_READONLY=a1mini  (comma list or '*') refuse every control command for those printers
      BAMBU_MCP_CONFIG_DIR / BAMBU_MCP_STATE_DIR / BAMBU_ORCA_BIN / BAMBU_MCP_POLL_S

Safety model: stop and gcode_line require confirm=true (only after the user approved). Starting a print
requires the printer to be IDLE/FINISH and the AMS tray to match the expected filament/colour.
Long actions (slice_and_print, print_3mf, watch_print, slice_model) return a job_id: poll with
job_status or job_wait.
"""
from __future__ import annotations

import functools
import json
import threading
from typing import Any, Optional

import anyio

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import mqtt as M
from .config import events_path, load_printers
from .jobs import JobManager
from .mqtt import PrinterError
from .service import Service

mcp = _Server(
    "bambu-lan",
    instructions=(
        "Controls Bambu Lab printers on the LAN. Start with list_printers and printer_status. Long work "
        "(slice_and_print, print_3mf, watch_print) runs as a background job and returns a job_id: poll "
        "with job_status or block with job_wait(job_id, timeout_s). Use dry_run=true to preview payloads. "
        "Never pass confirm=true unless the user explicitly approved that action."
    ),
)

_svc: Optional[Service] = None
_lock = threading.Lock()


def svc() -> Service:
    global _svc
    with _lock:
        if _svc is None:
            _svc = Service(load_printers(), JobManager(events_path()))
            threading.Thread(target=_svc.reattach, daemon=True, name="reattach").start()
        return _svc


def tool(fn):
    """Register a sync function as an async MCP tool (blocking work runs in a worker thread)."""
    @functools.wraps(fn)
    async def wrapper(*a, **k):
        return await anyio.to_thread.run_sync(functools.partial(fn, *a, **k))
    mcp.tool()(wrapper)
    return fn


def _need(confirm: bool, what: str) -> None:
    if confirm is not True:
        raise PrinterError(f"Refused: {what}. Ask the user for approval, then call again with confirm=true.")


def _trays(tray: int | list[int]) -> list[int]:
    t = [tray] if isinstance(tray, int) else list(tray)
    if not t or any((not isinstance(x, int)) or not 0 <= x <= 15 for x in t):
        raise PrinterError("tray must be an int 0-15 (ams_id*4 + slot_index; slot 3 of AMS 0 = 2) or a list")
    return t


def _opts(use_ams, timelapse, bed_leveling, flow_cali, vibration_cali) -> dict:
    return {"use_ams": use_ams, "timelapse": timelapse, "bed_leveling": bed_leveling,
            "flow_cali": flow_cali, "vibration_cali": vibration_cali}


# ------------------------------------------------------------------ read-only
@tool
def list_printers() -> dict:
    """Configured printers (name, label, ip, whether control commands are blocked). No secrets."""
    return {"printers": [p.public() for p in svc().printers.values()]}


@tool
def printer_status(printer: str) -> dict:
    """Read-only compact status: gcode_state, job, progress %, remaining min, layers, temperatures,
    print_error and AMS trays (tray index = ams_id*4+slot_index, used by print_3mf)."""
    return svc().status(printer)


@tool
def ams_info(printer: str) -> dict:
    """Read-only: AMS tray contents (tray index, slot number, filament type, colour RRGGBBAA, brand)."""
    return {"printer": printer, "trays": svc().status(printer)["ams_trays"]}


@tool
def recent_events(limit: int = 30, printer: Optional[str] = None) -> dict:
    """Last entries of the persistent event log (state changes, progress milestones, errors)."""
    path = events_path()
    rows = []
    if path.exists():
        for line in path.read_text().splitlines()[-2000:]:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "job" and (printer is None or r.get("printer") == printer):
                rows.append(r)
    return {"events": rows[-max(1, min(limit, 500)):]}


# ------------------------------------------------------------------ jobs
@tool
def print_3mf(printer: str, path: str, tray: int | list[int] = 0, expect_filament: Optional[str] = None,
              expect_color: Optional[str] = None, plate: int = 1, name: Optional[str] = None,
              watch: bool = True, dry_run: Optional[bool] = None, use_ams: bool = True,
              timelapse: bool = False, bed_leveling: bool = True, flow_cali: bool = False,
              vibration_cali: bool = False) -> dict:
    """Upload a SLICED .3mf (must contain Metadata/plate_N.gcode) via FTPS and start it. Refuses unless
    the printer is IDLE/FINISH and each tray holds expect_filament (e.g. PLA) / expect_color (RRGGBBAA or
    RRGGBB). tray = AMS tray index (slot 3 -> 2). Returns a job_id immediately; with watch=true the job
    keeps following the print until FINISH/FAILED. dry_run=true: validates and returns the payload only
    (synchronous, nothing uploaded or published)."""
    s, t, o = svc(), _trays(tray), _opts(use_ams, timelapse, bed_leveling, flow_cali, vibration_cali)
    from .config import env_dry_run
    if env_dry_run() if dry_run is None else dry_run:
        return s.print_dry_run(printer, path, t, expect_filament, expect_color, plate, name, o)
    return s.submit_print(printer, path, t, expect_filament, expect_color, plate, name, o, watch).summary()


@tool
def slice_and_print(printer: str, model_path: str, profile_dir: str, tray: int | list[int] = 0,
                    expect_filament: Optional[str] = None, expect_color: Optional[str] = None,
                    plate: int = 1, name: Optional[str] = None, watch: bool = True,
                    use_ams: bool = True, timelapse: bool = False, bed_leveling: bool = True,
                    flow_cali: bool = False, vibration_cali: bool = False) -> dict:
    """Background job: slice an STL/3MF with OrcaSlicer CLI (profile_dir must contain machine_flat.json,
    process_flat.json, filament_flat.json), then run the print_3mf flow and watch to the end. Returns a
    job_id. To preview without printing use slice_model. (No dry-run: use slice_model + print_3mf dry_run.)"""
    from .config import env_dry_run
    if env_dry_run():
        raise PrinterError("BAMBU_MCP_DRY_RUN=1: slice_and_print refused; use slice_model then print_3mf dry_run")
    return svc().submit_slice_and_print(
        printer, model_path, profile_dir, _trays(tray), expect_filament, expect_color, plate, name,
        _opts(use_ams, timelapse, bed_leveling, flow_cali, vibration_cali), watch).summary()


@tool
def slice_model(model_path: str, profile_dir: str, name: str) -> dict:
    """Background job: slice only (no printer contact). Result has threemf path and filament_used_g."""
    return svc().submit_slice(model_path, profile_dir, name).summary()


@tool
def watch_print(printer: str) -> dict:
    """Background job: follow the current print of a printer to FINISH/FAILED, logging events to
    ~/.local/state/bambu_mcp/events.jsonl. Use to (re)attach to a print started elsewhere."""
    return svc().submit_watch(printer).summary()


@tool
def job_status(job_id: str) -> dict:
    """Current state of a job: state, phase, progress_pct, remaining_min, message, error, result."""
    return svc().mgr.get(job_id)


@tool
def job_wait(job_id: str, timeout_s: float = 30) -> dict:
    """Long-poll: returns when the job reaches a terminal state or after timeout_s (max 240), with the
    latest progress. Call repeatedly while state is 'running'."""
    return svc().mgr.wait(job_id, min(max(timeout_s, 0), 240))


@tool
def job_list(only_active: bool = False) -> dict:
    """All known jobs (including those from previous server runs, from the event log)."""
    return {"jobs": svc().mgr.list(only_active)}


@tool
def job_cancel(job_id: str) -> dict:
    """Cancel the server-side job (stops slicing/upload/watching). Does NOT stop a print that already
    started: use stop(printer, confirm=true) for that."""
    return svc().mgr.cancel(job_id)


# ------------------------------------------------------------------ control commands
@tool
def pause(printer: str, dry_run: Optional[bool] = None) -> dict:
    """Pause the current print."""
    return svc().command(printer, M.pause_payload(), dry_run)


@tool
def resume(printer: str, dry_run: Optional[bool] = None) -> dict:
    """Resume a paused print."""
    return svc().command(printer, M.resume_payload(), dry_run)


@tool
def stop(printer: str, confirm: bool = False, dry_run: Optional[bool] = None) -> dict:
    """STOP (abort) the current print - irreversible. Requires confirm=true after explicit user approval."""
    if not env_dr(dry_run):
        _need(confirm, f"stop aborts the current print on {printer}")
    return svc().command(printer, M.stop_payload(), dry_run)


@tool
def set_light(printer: str, on: bool, node: Optional[str] = None, dry_run: Optional[bool] = None) -> dict:
    """Turn a light on/off. node defaults to chamber_light (P1S) or work_light (A1 series); UNVERIFIED on hardware."""
    node = node or ("work_light" if printer.lower().startswith("a1") else "chamber_light")
    return svc().command(printer, M.light_payload(on, node), dry_run)


@tool
def set_speed(printer: str, level: str = "standard", dry_run: Optional[bool] = None) -> dict:
    """Print speed profile: silent | standard | sport | ludicrous."""
    if level not in M.SPEED_LEVELS:
        raise PrinterError(f"level must be one of {sorted(M.SPEED_LEVELS)}")
    return svc().command(printer, M.speed_payload(M.SPEED_LEVELS[level]), dry_run)


@tool
def send_gcode(printer: str, gcode: str, confirm: bool = False, dry_run: Optional[bool] = None) -> dict:
    """Send raw G-code line(s) to the printer. DANGEROUS (can crash the toolhead, heat things).
    Requires confirm=true after explicit user approval."""
    if not env_dr(dry_run):
        _need(confirm, f"send_gcode executes raw G-code on {printer}")
    return svc().command(printer, M.gcode_line_payload(gcode), dry_run)


def env_dr(dry_run: Optional[bool]) -> bool:
    from .config import env_dry_run
    return env_dry_run() if dry_run is None else dry_run


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
