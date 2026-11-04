import pytest

from bambu_mcp import mqtt as M
from bambu_mcp.mqtt import PrinterError
from conftest import CODE, FakeClient, full_state


def test_project_file_payload_matches_proven_shape():
    p = M.project_file_payload(subtask_name="job", remote_file="job.3mf", md5="ABC", trays=[2])["print"]
    assert p["command"] == "project_file" and p["param"] == "Metadata/plate_1.gcode"
    assert p["url"] == "file:///sdcard/job.3mf" and p["md5"] == "ABC"
    assert p["ams_mapping"] == [2] and p["ams_mapping2"] == [{"ams_id": 0, "slot_id": 2}]
    assert p["use_ams"] is True and p["timelapse"] is False and p["bed_leveling"] is True
    assert (p["profile_id"], p["project_id"], p["subtask_id"], p["task_id"]) == ("0",) * 4


def test_ams_mapping2_second_unit():
    p = M.project_file_payload(subtask_name="j", remote_file="j.3mf", md5="x", trays=[5, 0])["print"]
    assert p["ams_mapping2"] == [{"ams_id": 1, "slot_id": 1}, {"ams_id": 0, "slot_id": 0}]


def test_simple_command_payloads():
    assert M.pause_payload()["print"]["command"] == "pause"
    assert M.resume_payload()["print"]["command"] == "resume"
    assert M.stop_payload()["print"]["command"] == "stop"
    l = M.light_payload(False, "work_light")["system"]
    assert (l["command"], l["led_node"], l["led_mode"]) == ("ledctrl", "work_light", "off")
    sp = M.speed_payload(3)["print"]
    assert sp["command"] == "print_speed" and sp["param"] == "3"
    assert M.gcode_line_payload("G28")["print"]["param"] == "G28\n"


def test_preflight():
    st = full_state()
    assert M.preflight(st, [2], "PLA", "FFFFFFFF") == []
    assert M.preflight(st, [2], "pla", "ffffff") == []
    assert "IDLE or FINISH" in M.preflight(full_state("RUNNING"), [2], None, None)[0]
    assert "expected 'PETG'" in M.preflight(st, [2], "PETG", None)[0]
    assert "colour" in M.preflight(st, [2], None, "FF0000")[0]
    assert "not present" in M.preflight(st, [7], None, None)[0]


def test_summarize_trays():
    s = M.summarize("a1mini", full_state())
    assert s["state"] == "IDLE"
    assert {"tray": 2, "ams": 0, "slot": 3, "type": "PLA", "color": "FFFFFFFF", "brand": None} in s["ams_trays"]


def test_partial_report_merge(svc):
    c = svc.conn("a1mini")
    c.ensure()
    FakeClient.instances[-1].deliver({"print": {"mc_percent": 42}})
    st = c.state()["print"]
    assert st["mc_percent"] == 42 and st["gcode_state"] == "IDLE" and st["ams"]["ams"]


def test_command_dry_run_never_publishes(svc):
    r = svc.command("a1mini", M.pause_payload(), dry_run=True)
    assert r["dry_run"] and not r["sent"] and r["payload"]["print"]["command"] == "pause"
    assert FakeClient.instances == []   # not even connected


def test_command_env_dry_run(svc, monkeypatch):
    monkeypatch.setenv("BAMBU_MCP_DRY_RUN", "1")
    assert svc.command("p1s", M.stop_payload(), None)["sent"] is False
    assert FakeClient.instances == []


def test_command_published_payload(svc):
    r = svc.command("p1s", M.pause_payload(), dry_run=False)
    assert r["sent"]
    cmds = FakeClient.instances[-1].commands()
    assert [c["print"]["command"] for c in cmds] == ["pause"]


def test_readonly_blocks(svc, monkeypatch):
    monkeypatch.setenv("BAMBU_MCP_READONLY", "a1mini")
    with pytest.raises(PrinterError, match="read-only"):
        svc.command("a1mini", M.pause_payload(), dry_run=False)
    assert FakeClient.instances == []
    monkeypatch.setenv("BAMBU_MCP_READONLY", "*")
    with pytest.raises(PrinterError):
        svc.command("p1s", M.light_payload(True), dry_run=False)


def test_server_confirm_gates(env):
    from bambu_mcp import server
    with pytest.raises(PrinterError, match="confirm"):
        server.stop("p1s", confirm=False, dry_run=False)
    with pytest.raises(PrinterError, match="confirm"):
        server.send_gcode("p1s", "G28", confirm=False, dry_run=False)


def test_scrub():
    from bambu_mcp.config import _SECRETS, scrub
    _SECRETS.add(CODE)
    assert CODE not in scrub(f"error with {CODE}")
