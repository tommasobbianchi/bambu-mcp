"""Orchestration (no MCP decorators, so it is unit-testable with a fake broker)."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

from . import mqtt as M
from .config import Printer, env_dry_run, is_readonly, state_dir
from .jobs import Job, JobManager
from .mqtt import PrinterConn, PrinterError
from .transfer import ftps_download, ftps_list, ftps_upload, inspect_3mf, safe_remote_name, slice_model

ACTIVE = ("RUNNING", "PREPARE", "PAUSE", "SLICING", "INIT")


def poll_s() -> float:
    return float(os.environ.get("BAMBU_MCP_POLL_S", "5"))


class Service:
    def __init__(self, printers: dict[str, Printer], mgr: JobManager,
                 client_factory: Optional[Callable] = None):
        self.printers = printers
        self.mgr = mgr
        self._factory = client_factory
        self._conns: dict[str, PrinterConn] = {}

    # ------------------------------------------------------------------ basics
    def conn(self, name: str) -> PrinterConn:
        if name not in self.printers:
            raise PrinterError(f"unknown printer {name!r}; known: {sorted(self.printers)}")
        if name not in self._conns:
            self._conns[name] = PrinterConn(self.printers[name], self._factory)
        return self._conns[name]

    def status(self, name: str) -> dict:
        c = self.conn(name)
        c.ensure()
        c.pushall()
        time.sleep(0.5)
        return M.summarize(name, c.state(), c.age())

    def command(self, name: str, payload: dict, dry_run: Optional[bool]) -> dict:
        """The single gate for control commands: dry-run and readonly policy applied here."""
        dry = env_dry_run() if dry_run is None else dry_run
        if dry:
            return {"dry_run": True, "sent": False, "printer": name, "payload": payload}
        if is_readonly(name):
            raise PrinterError(f"{name} is marked read-only (BAMBU_MCP_READONLY); command refused")
        c = self.conn(name)
        c.send_command(payload)
        return {"dry_run": False, "sent": True, "printer": name, "command": _cmd_name(payload)}

    # ------------------------------------------------------------------ print preparation
    def prepare_print(self, name: str, path: str, trays: list[int], expect_filament, expect_color,
                      plate: int, job_name: Optional[str], opts: dict, check_state: bool = True,
                      remote_name: Optional[str] = None):
        p = Path(path).expanduser()
        info = inspect_3mf(p, plate)
        remote = remote_name or safe_remote_name(job_name or p.stem)
        opts = dict(opts)
        style = opts.pop("style", "auto")
        if style == "auto":
            style = M.payload_style_for(self.printers[name].serial)
        payload = M.project_file_payload(
            subtask_name=remote[:-4], remote_file=remote, md5=info["md5"], plate=plate,
            trays=trays, style=style, **opts)
        problems: list[str] = []
        if check_state:
            try:
                c = self.conn(name)
                c.ensure()
                c.pushall(); time.sleep(0.5)
                problems = M.preflight(c.state(), trays, expect_filament, expect_color)
            except PrinterError as e:
                problems = [f"cannot read printer state: {e}"]
        return p, remote, info, payload, problems

    def print_dry_run(self, name, path, trays, expect_filament, expect_color, plate, job_name, opts):
        _, remote, info, payload, problems = self.prepare_print(
            name, path, trays, expect_filament, expect_color, plate, job_name, opts)
        return {"dry_run": True, "sent": False, "uploaded": False, "ok": not problems,
                "problems": problems, "remote_file": remote, "md5": info["md5"],
                "size": info["size"], "payload": payload}

    # ------------------------------------------------------------------ job bodies
    def _start_and_watch(self, job: Job, name, path, trays, expect_filament, expect_color,
                         plate, job_name, opts, watch: bool, upload: bool = True,
                         remote_name: Optional[str] = None):
        if is_readonly(name):
            raise PrinterError(f"{name} is marked read-only (BAMBU_MCP_READONLY); print refused")
        job.set(phase="preflight", message="checking file, printer state and AMS trays", snapshot=True)
        p, remote, info, payload, problems = self.prepare_print(
            name, path, trays, expect_filament, expect_color, plate, job_name, opts,
            remote_name=remote_name)
        if problems:
            raise PrinterError("preflight refused: " + "; ".join(problems))
        job.check_cancel()
        if upload:
            job.set(phase="uploading", message=f"uploading {remote} ({info['size']} bytes) via FTPS",
                    snapshot=True)
            ftps_upload(self.printers[name], p, remote, cancel=job.cancel_event)
        job.check_cancel()
        c = self.conn(name)
        job.set(phase="starting", message="sending project_file", snapshot=True)
        c.last_reply = {}
        t0 = time.time()
        c.send_command(payload)
        deadline = t0 + 120
        while True:
            job.sleep(1.0)
            rep = c.last_reply
            if rep.get("command") == "project_file" and rep.get("result") not in (None, "success"):
                raise PrinterError(f"printer rejected project_file: {rep}")
            gs = c.state().get("print", {}).get("gcode_state")
            if gs in ("PREPARE", "RUNNING"):
                break
            if time.time() > deadline:
                raise PrinterError(f"print did not start within 120s (state={gs})")
            if int(time.time() - t0) % 10 == 9:
                c.pushall()
        job.event("print_started", file=remote)
        if not watch:
            return {"started": True, "remote_file": remote}
        return self._watch(job, name, announce_start=True)

    def _watch(self, job: Job, name: str, announce_start: bool = False):
        c = self.conn(name)
        c.ensure()
        job.set(phase="watching", message="following print", snapshot=True)
        last_state, last_err, last_ms, seen_active = None, None, -1, announce_start
        last_push = 0.0
        while True:
            job.check_cancel()
            now = time.time()
            if now - last_push > 30:
                try:
                    c.pushall()
                except PrinterError:
                    pass
                last_push = now
            pr = c.state().get("print", {})
            gs, err = pr.get("gcode_state"), pr.get("print_error")
            pct, rem = pr.get("mc_percent"), pr.get("mc_remaining_time")
            job.set(progress=pct, remaining_min=rem,
                    message=f"{gs} {pct}% rem {rem} min layer {pr.get('layer_num')}/{pr.get('total_layer_num')}")
            if gs != last_state:
                job.event("state", gcode_state=gs, percent=pct)
                last_state = gs
            if err and err != last_err:
                job.event("print_error", print_error=err)
            last_err = err
            if isinstance(pct, int) and pct // 10 != last_ms:
                last_ms = pct // 10
                job.event("progress", percent=pct, remaining_min=rem)
            if gs in ACTIVE:
                seen_active = True
            if gs == "FINISH":
                if not seen_active:
                    return {"final_state": "FINISH", "note": "nothing to watch: last print already finished"}
                job.event("print_finished", subtask=pr.get("subtask_name"))
                return {"final_state": "FINISH", "subtask": pr.get("subtask_name")}
            if gs == "FAILED":
                job.event("print_failed", print_error=err)
                raise PrinterError(f"print FAILED (print_error={err})")
            if gs == "IDLE" and seen_active:
                job.event("print_stopped")
                return {"final_state": "IDLE", "note": "print ended without FINISH (stopped/cancelled?)"}
            if gs == "IDLE" and not seen_active:
                return {"final_state": "IDLE", "note": "nothing to watch: printer is idle"}
            job.sleep(poll_s())

    # ------------------------------------------------------------------ job submitters
    def submit_print(self, name, path, trays, expect_filament, expect_color, plate, job_name,
                     opts, watch=True) -> Job:
        params = {"file": path, "trays": trays, "plate": plate, "watch": watch}
        return self.mgr.submit(
            "print_3mf", name, params,
            lambda j: self._start_and_watch(j, name, path, trays, expect_filament, expect_color,
                                            plate, job_name, opts, watch))

    def submit_watch(self, name: str) -> Job:
        return self.mgr.submit("watch_print", name, {}, lambda j: self._watch(j, name))

    def sd_list(self, name: str) -> list[str]:
        if name not in self.printers:
            raise PrinterError(f"unknown printer {name!r}; known: {sorted(self.printers)}")
        return ftps_list(self.printers[name])

    def _fetch_sd(self, name: str, remote_file: str) -> Path:
        files = self.sd_list(name)
        if remote_file not in files:
            raise PrinterError(f"{remote_file!r} is not on {name}'s SD card (see sd_list)")
        dest = state_dir() / "sd" / name / safe_remote_name(remote_file)
        return ftps_download(self.printers[name], remote_file, dest)

    def print_sd_dry_run(self, name, remote_file, trays, expect_filament, expect_color, plate, opts):
        local = self._fetch_sd(name, remote_file)
        _, remote, info, payload, problems = self.prepare_print(
            name, str(local), trays, expect_filament, expect_color, plate, None, opts, remote_name=remote_file)
        return {"dry_run": True, "sent": False, "uploaded": False, "ok": not problems,
                "problems": problems, "remote_file": remote, "md5": info["md5"],
                "size": info["size"], "payload": payload}

    def submit_print_sd(self, name, remote_file, trays, expect_filament, expect_color, plate, opts,
                        watch=True) -> Job:
        """Start a sliced .3mf that is already on the printer's SD card (no upload)."""
        def body(job: Job):
            job.set(phase="preflight", message=f"fetching {remote_file} from the SD card", snapshot=True)
            local = self._fetch_sd(name, remote_file)
            return self._start_and_watch(job, name, str(local), trays, expect_filament, expect_color,
                                         plate, None, opts, watch, upload=False, remote_name=remote_file)
        return self.mgr.submit("print_sd", name, {"file": remote_file, "trays": trays, "plate": plate,
                                                  "watch": watch}, body)

    def submit_slice(self, model, profile_dir, name_, printer=None, xform=None) -> Job:
        def body(job: Job):
            job.set(phase="slicing", message=f"slicing {Path(model).name}", snapshot=True)
            out = state_dir() / "slices" / job.id
            return slice_model(Path(model).expanduser(), Path(profile_dir).expanduser(), name_, out,
                               cancel=job.cancel_event, xform=xform)
        return self.mgr.submit("slice", printer, {"model": model, "profile_dir": profile_dir,
                                                  **({"xform": xform} if xform else {})}, body)

    def submit_slice_and_print(self, name, model, profile_dir, trays, expect_filament, expect_color,
                               plate, job_name, opts, watch=True, xform=None) -> Job:
        base = job_name or Path(model).stem

        def body(job: Job):
            job.set(phase="slicing", message=f"slicing {Path(model).name}", snapshot=True)
            out = state_dir() / "slices" / job.id
            sl = slice_model(Path(model).expanduser(), Path(profile_dir).expanduser(), base, out,
                             cancel=job.cancel_event, xform=xform)
            job.set(message=f"sliced, filament used g={sl['filament_used_g']}", snapshot=True)
            res = self._start_and_watch(job, name, sl["threemf"], trays, expect_filament,
                                        expect_color, plate, base, opts, watch)
            return {**(res or {}), "slice": sl}
        return self.mgr.submit("slice_and_print", name, {"model": model, "trays": trays}, body)

    # ------------------------------------------------------------------ restart recovery
    def reattach(self) -> list[str]:
        """After a restart: for printers that are mid-print and have no live watcher, start one."""
        started = []
        live = {j.printer for j in self.mgr.jobs.values()
                if j.state == "running" and j.kind in ("watch_print", "print_3mf", "slice_and_print")}
        interrupted = {r.get("printer") for r in self.mgr.history.values()
                       if r.get("state") == "interrupted"
                       and r.get("kind") in ("watch_print", "print_3mf", "slice_and_print")}
        for n in interrupted - live - {None}:
            try:
                c = self.conn(n)
                c.ensure()
                if c.state().get("print", {}).get("gcode_state") in ACTIVE:
                    started.append(self.submit_watch(n).id)
            except Exception:  # noqa: BLE001
                pass
        return started


def _cmd_name(payload: dict) -> str:
    for v in payload.values():
        if isinstance(v, dict) and "command" in v:
            return v["command"]
    return "?"
