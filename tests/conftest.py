import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bambu_mcp.config import Printer  # noqa: E402
from bambu_mcp.jobs import JobManager  # noqa: E402
from bambu_mcp.service import Service  # noqa: E402

CODE, SERIAL = "FAKECODE1234", "FAKESERIAL99"


def full_state(gcode_state="IDLE", pct=0, tray_type="PLA", tray_color="FFFFFFFF"):
    return {"print": {"gcode_state": gcode_state, "mc_percent": pct, "mc_remaining_time": 60,
                      "nozzle_temper": 25, "bed_temper": 24, "subtask_name": "x", "print_error": 0,
                      "ams": {"ams": [{"id": "0", "tray": [
                          {"id": "0", "tray_type": "PETG", "tray_color": "000000FF"},
                          {"id": "1"}, {"id": "2", "tray_type": tray_type, "tray_color": tray_color},
                          {"id": "3"}]}]}}}


class FakeMsg:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


class FakeClient:
    """In-memory stand-in for paho: records publishes, never opens a socket."""
    instances = []

    def __init__(self, state=None):
        self.published = []
        self.state = state if state is not None else full_state()
        self.auto_start = True
        FakeClient.instances.append(self)

    def username_pw_set(self, *a, **k): pass
    def tls_set(self, *a, **k): pass
    def tls_insecure_set(self, *a, **k): pass
    def connect(self, *a, **k): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, *a, **k): pass

    def loop_start(self):
        self.on_connect(self, None, {}, 0)

    def deliver(self, payload):
        self.on_message(self, None, FakeMsg(payload))

    def publish(self, topic, payload, qos=0):
        assert topic == f"device/{SERIAL}/request"
        d = json.loads(payload)
        self.published.append(d)
        pr = d.get("print", {})
        if d.get("pushing", {}).get("command") == "pushall":
            self.deliver(self.state)
        elif pr.get("command") == "project_file" and self.auto_start:
            self.state["print"]["gcode_state"] = "RUNNING"
            self.deliver({"print": {"command": "project_file", "result": "success",
                                    "gcode_state": "RUNNING"}})

    def commands(self):
        return [d for d in self.published if "pushing" not in d]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BAMBU_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BAMBU_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BAMBU_MCP_POLL_S", "0.1")
    monkeypatch.delenv("BAMBU_MCP_DRY_RUN", raising=False)
    monkeypatch.delenv("BAMBU_MCP_READONLY", raising=False)
    FakeClient.instances.clear()
    # fake curl: reads config from stdin, records argv, prints 226
    curl = tmp_path / "fakecurl"
    curl.write_text(f"#!/bin/sh\ncat > {tmp_path}/curl_stdin\necho \"$@\" > {tmp_path}/curl_argv\nprintf 226\n")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BAMBU_MCP_CURL", str(curl))
    return tmp_path


@pytest.fixture
def threemf(tmp_path):
    p = tmp_path / "part.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "; filament used [g] = 1.5\nG1 X0\n")
        z.writestr("3D/3dmodel.model", "<model/>")
    return p


@pytest.fixture
def svc(env):
    printers = {"a1mini": Printer("a1mini", "127.0.0.1", SERIAL, CODE),
                "p1s": Printer("p1s", "127.0.0.2", SERIAL, CODE)}
    mgr = JobManager(env / "state" / "events.jsonl")
    return Service(printers, mgr, client_factory=lambda: FakeClient())
