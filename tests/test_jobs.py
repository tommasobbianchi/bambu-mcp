import hashlib
import time

import pytest

from bambu_mcp.jobs import JobManager
from conftest import CODE, FakeClient, full_state

OPTS = {"use_ams": True, "timelapse": False, "bed_leveling": True,
        "flow_cali": False, "vibration_cali": False}


def wait_state(svc, job, st, t=10):
    r = svc.mgr.wait(job.id, t)
    assert r["state"] == st, r
    return r


def test_print_flow_upload_start_watch_finish(svc, threemf, env):
    j = svc.submit_print("a1mini", str(threemf), [2], "PLA", "FFFFFFFF", 1, "mount_test", OPTS)
    for _ in range(100):
        if svc.mgr.get(j.id)["phase"] == "watching":
            break
        time.sleep(0.1)
    cl = FakeClient.instances[-1]
    cl.deliver({"print": {"mc_percent": 50, "mc_remaining_time": 12}})
    time.sleep(0.5)
    s = svc.mgr.get(j.id)
    assert s["state"] == "running" and s["progress_pct"] == 50 and s["remaining_min"] == 12
    cl.deliver({"print": {"gcode_state": "FINISH", "mc_percent": 100}})
    r = wait_state(svc, j, "done")
    assert r["result"]["final_state"] == "FINISH"
    cmds = cl.commands()
    assert len(cmds) == 1
    pf = cmds[0]["print"]
    assert pf["command"] == "project_file" and pf["url"] == "file:///sdcard/mount_test.3mf"
    assert pf["md5"] == hashlib.md5(threemf.read_bytes()).hexdigest().upper()
    assert pf["ams_mapping"] == [2]
    # secrets: code only through the stdin config, never argv or the event log
    assert CODE not in (env / "curl_argv").read_text()
    assert CODE in (env / "curl_stdin").read_text()
    ev = (env / "state" / "events.jsonl").read_text()
    assert CODE not in ev and "print_finished" in ev


def test_print_refused_when_tray_mismatch(svc, threemf):
    j = svc.submit_print("a1mini", str(threemf), [2], "PETG", None, 1, "x", OPTS)
    r = wait_state(svc, j, "failed")
    assert "preflight refused" in r["error"] and "PETG" in r["error"]
    assert FakeClient.instances[-1].commands() == []


def test_print_refused_when_busy(svc, threemf):
    svc._factory = lambda: FakeClient(full_state("RUNNING"))
    j = svc.submit_print("a1mini", str(threemf), [2], None, None, 1, "x", OPTS)
    r = wait_state(svc, j, "failed")
    assert "must be IDLE or FINISH" in r["error"]
    assert FakeClient.instances[-1].commands() == []


def test_print_refused_readonly(svc, threemf, monkeypatch):
    monkeypatch.setenv("BAMBU_MCP_READONLY", "*")
    j = svc.submit_print("a1mini", str(threemf), [2], None, None, 1, "x", OPTS)
    r = wait_state(svc, j, "failed")
    assert "read-only" in r["error"] and FakeClient.instances == []


def test_print_dry_run(svc, threemf):
    r = svc.print_dry_run("p1s", str(threemf), [2], "PLA", "FFFFFFFF", 1, "dry", OPTS)
    assert r["ok"] and r["sent"] is False and r["payload"]["print"]["subtask_name"] == "dry"
    assert FakeClient.instances[-1].commands() == []


def test_bad_3mf(svc, tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_text("nope")
    with pytest.raises(ValueError):
        svc.print_dry_run("p1s", str(bad), [2], None, None, 1, None, OPTS)


def test_job_wait_timeout_and_cancel(svc):
    svc._factory = lambda: FakeClient(full_state("RUNNING", 10))
    j = svc.submit_watch("a1mini")
    t0 = time.time()
    r = svc.mgr.wait(j.id, 0.5)
    assert r["state"] == "running" and 0.4 < time.time() - t0 < 3
    r = svc.mgr.cancel(j.id)
    assert r["state"] == "cancelled"
    assert any(x["job_id"] == j.id for x in svc.mgr.list())
    assert svc.mgr.list(only_active=True) == []


def test_watch_failed(svc):
    svc._factory = lambda: FakeClient(full_state("RUNNING", 10))
    j = svc.submit_watch("a1mini")
    time.sleep(0.5)
    FakeClient.instances[-1].deliver({"print": {"gcode_state": "FAILED", "print_error": 5}})
    r = wait_state(svc, j, "failed")
    assert "FAILED" in r["error"]


def test_watch_idle_nothing(svc):
    j = svc.submit_watch("a1mini")
    r = wait_state(svc, j, "done")
    assert "nothing to watch" in r["result"]["note"]


def test_restart_persistence_and_reattach(svc, env):
    svc._factory = lambda: FakeClient(full_state("RUNNING", 30))
    j = svc.submit_watch("a1mini")
    time.sleep(0.5)
    mgr2 = JobManager(env / "state" / "events.jsonl")   # "restart": same log
    assert mgr2.get(j.id)["state"] == "interrupted"
    from bambu_mcp.service import Service
    s2 = Service(svc.printers, mgr2, client_factory=lambda: FakeClient(full_state("RUNNING", 30)))
    ids = s2.reattach()
    assert len(ids) == 1 and mgr2.get(ids[0])["kind"] == "watch_print"
    mgr2.cancel(ids[0])
    svc.mgr.cancel(j.id)


def test_failure_message_scrubbed(svc):
    from bambu_mcp.config import _SECRETS
    _SECRETS.add(CODE)

    def boom(job):
        raise RuntimeError(f"leak {CODE}")
    j = svc.mgr.submit("x", None, {}, boom)
    r = wait_state(svc, j, "failed")
    assert CODE not in r["error"]
