"""Background job manager: threads in memory + append-only JSONL event log.

Every job snapshot (on phase change / end) and every print event is appended to events.jsonl,
so job history survives a server restart. Jobs that were in flight at restart are marked
'interrupted' (see server.reattach for watchers).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .config import scrub

TERMINAL = ("done", "failed", "cancelled", "interrupted")


class JobCancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str, printer: Optional[str], params: dict):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.printer, self.params = kind, printer, params
        self.state = "running"
        self.phase = "queued"
        self.progress: Optional[float] = None
        self.remaining_min: Optional[float] = None
        self.message = ""
        self.result: Any = None
        self.error: Optional[str] = None
        self.created = self.updated = time.time()
        self.cancel_event = threading.Event()
        self._mgr: "JobManager" = None  # type: ignore

    def summary(self) -> dict:
        return {"job_id": self.id, "kind": self.kind, "printer": self.printer, "state": self.state,
                "phase": self.phase, "progress_pct": self.progress, "remaining_min": self.remaining_min,
                "message": self.message, "error": self.error, "result": self.result,
                "created": round(self.created), "updated": round(self.updated),
                "params": self.params}

    # -- used by job bodies
    def set(self, *, snapshot: bool = False, **kw) -> None:
        with self._mgr.cond:
            for k, v in kw.items():
                setattr(self, k, scrub(v) if k in ("message", "error") and v is not None else v)
            self.updated = time.time()
            self._mgr.cond.notify_all()
        if snapshot:
            self._mgr.snapshot(self)

    def event(self, type_: str, **data) -> None:
        self._mgr.log({"type": type_, "job_id": self.id, "printer": self.printer, **data})

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def sleep(self, s: float) -> None:
        """Cancellable sleep."""
        if self.cancel_event.wait(s):
            raise JobCancelled()


class JobManager:
    def __init__(self, events_file: Path):
        self.events_file = Path(events_file)
        self.cond = threading.Condition()
        self.jobs: dict[str, Job] = {}
        self.history: dict[str, dict] = {}   # snapshots loaded from the log (previous runs)
        self._wlock = threading.Lock()
        self._load()

    # -- persistence
    def log(self, rec: dict) -> None:
        rec = {"ts": round(time.time(), 1), **rec}
        line = scrub(json.dumps(rec, default=str))
        with self._wlock:
            self.events_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_file, "a") as f:
                f.write(line + "\n")

    def snapshot(self, job: Job) -> None:
        self.log({"type": "job", **job.summary()})

    def _load(self) -> None:
        if not self.events_file.exists():
            return
        for line in self.events_file.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "job" and r.get("job_id"):
                self.history[r["job_id"]] = r
        for jid, r in self.history.items():
            if r.get("state") not in TERMINAL:
                r["state"] = "interrupted"
                r["message"] = "server restarted while this job was running"
                self.log({**r, "type": "job"})

    # -- api
    def submit(self, kind: str, printer: Optional[str], params: dict,
               body: Callable[[Job], Any]) -> Job:
        job = Job(kind, printer, params)
        job._mgr = self

        def run():
            try:
                res = body(job)
                job.set(state="done", phase="done", result=res, snapshot=True)
            except JobCancelled:
                job.set(state="cancelled", phase="cancelled", message="cancelled", snapshot=True)
            except Exception as e:  # noqa: BLE001
                job.set(state="failed", error=f"{type(e).__name__}: {e}", snapshot=True)

        with self.cond:
            self.jobs[job.id] = job
        self.snapshot(job)
        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> dict:
        j = self.jobs.get(job_id)
        if j:
            return j.summary()
        if job_id in self.history:
            return self.history[job_id]
        raise KeyError(f"unknown job_id {job_id}")

    def wait(self, job_id: str, timeout_s: float) -> dict:
        """Long-poll: returns when the job reaches a terminal state or timeout elapses."""
        j = self.jobs.get(job_id)
        if j is None:
            return self.get(job_id)
        deadline = time.time() + max(0.0, timeout_s)
        with self.cond:
            while j.state not in TERMINAL:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(min(left, 5.0))
        return j.summary()

    def list(self, only_active: bool = False) -> list[dict]:
        out = {jid: r for jid, r in self.history.items()}
        out.update({jid: j.summary() for jid, j in self.jobs.items()})
        rows = sorted(out.values(), key=lambda r: r.get("created", 0), reverse=True)
        if only_active:
            rows = [r for r in rows if r.get("state") not in TERMINAL]
        return [{k: r.get(k) for k in ("job_id", "kind", "printer", "state", "phase",
                                       "progress_pct", "remaining_min", "message", "error")}
                for r in rows]

    def cancel(self, job_id: str) -> dict:
        j = self.jobs.get(job_id)
        if j is None:
            return {**self.get(job_id), "note": "job not active in this server process"}
        j.cancel_event.set()
        return self.wait(job_id, 5.0)
