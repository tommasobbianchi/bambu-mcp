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


# --- OrcaSlicer-style payload (P1S firmware 01.10 rejects the legacy form with HMS 0500-4003) ----------
def test_orca_style_payload_matches_recorded_orcaslicer_message():
    p = M.project_file_payload(subtask_name="x", remote_file="Filament Junction Lever 3_4-B.gcode.3mf",
                               md5="85121F6B33AA197F62472DFD86C5AC4A", trays=[4], style="orca")["print"]
    assert p["url"] == "ftp://Filament Junction Lever 3_4-B.gcode.3mf"
    assert p["file"] == "Filament Junction Lever 3_4-B.gcode.3mf"
    assert p["subtask_name"] == "Filament Junction Lever 3_4-B"
    assert p["ams_mapping"] == [4] and p["ams_mapping2"] == [{"ams_id": 1, "slot_id": 0}]
    assert (p["auto_bed_leveling"], p["cfg"], p["extrude_cali_flag"], p["nozzle_offset_cali"]) == (1, "0", 2, 2)
    assert p["bed_type"] == "textured_plate" and p["layer_inspect"] is True and p["flow_cali"] is False
    assert M.project_file_payload(subtask_name="x", remote_file="a.3mf", md5="m", trays=[0], flow_cali=True,
                                  style="orca", bed_type="supertack_plate")["print"]["extrude_cali_flag"] == 1


def test_payload_style_for_serial():
    assert M.payload_style_for("01P00C4C1300919") == "orca"      # P1S
    assert M.payload_style_for("0309DA000000000") == "legacy"    # A1 mini
    assert M.payload_style_for("039000000000000") == "legacy"    # A1


def test_ams_ht_tray_index_and_mapping():
    assert M.tray_index(1, 0) == 4 and M.tray_index(0, 3) == 3 and M.tray_index(128, 0) == 128
    p = M.project_file_payload(subtask_name="j", remote_file="j.3mf", md5="x", trays=[128], style="orca")["print"]
    assert p["ams_mapping"] == [128] and p["ams_mapping2"] == [{"ams_id": 128, "slot_id": 0}]


def test_preflight_failed_state_rules():
    ok = {"print": {"gcode_state": "FAILED", "print_error": 0, "hms": []}}
    assert M.preflight(ok, [], None, None) == []
    assert M.preflight({"print": {"gcode_state": "FAILED", "print_error": 5}}, [], None, None)
    assert M.preflight({"print": {"gcode_state": "PAUSE", "print_error": 0}}, [], None, None)


def test_summarize_exposes_hms_error_and_ams_units():
    st = full_state()
    st["print"]["print_error"] = 134184978
    st["print"]["hms"] = [{"attr": 1, "code": 2}]
    s = M.summarize("p1s", st)
    assert s["print_error_hex"] == "0x7ff8012" and s["hms"] == [{"attr": 1, "code": 2}]
    assert isinstance(s["ams_units"], list)


def test_slice_flags():
    from bambu_mcp.transfer import slice_flags
    assert slice_flags(None) == []
    assert slice_flags({"scale": 0.33, "rotate": 90}) == []                       # mesh pre-transform, not CLI
    assert slice_flags({"orient": True, "arrange": True}) == ["--orient", "1", "--arrange", "1"]
    assert slice_flags({"orient": False, "arrange": False}) == []
    for bad in ({"scale": 0}, {"scale": 500}, {"nope": 1}, {"repetitions": 2}):
        with pytest.raises(ValueError):
            slice_flags(bad)


def test_meshtool_scale_rotate_and_sit_on_bed(tmp_path):
    from bambu_mcp import meshtool as T
    box = [(x, y, z) for x in (0, 30) for y in (0, 30) for z in (0, 30)]
    tris = [(0, 1, 3), (0, 3, 2), (4, 5, 7), (4, 7, 6)]
    src = tmp_path / "box.stl"; T.write_stl(src, box, tris)
    out = T.prepare_model(src, {"scale": 0.33}, tmp_path / "pre")
    v, _ = T.load_mesh(out)
    size = [max(p[i] for p in v) - min(p[i] for p in v) for i in range(3)]
    assert [round(s, 2) for s in size] == [9.9, 9.9, 9.9] and min(p[2] for p in v) == 0
    assert T.prepare_model(src, {"orient": True}, tmp_path / "pre2") == src        # no transform -> untouched
    v2, _ = T.load_mesh(T.prepare_model(src, {"rotate_x": 90}, tmp_path / "pre3"))
    assert round(max(p[2] for p in v2), 3) == 30.0 and round(min(p[2] for p in v2), 3) == 0.0
