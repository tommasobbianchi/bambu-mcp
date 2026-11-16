"""Bambu LAN MQTT: payload builders (pure functions) and a persistent per-printer connection.

Payload builders never touch the network, so they can be asserted in tests.
The connection takes an injectable `client_factory` so tests use a fake broker.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import re
import ssl
import threading
import time
from typing import Any, Callable, Optional

from .config import Printer, scrub

_seq = itertools.count(30003)


def next_seq() -> str:
    return str(next(_seq))


# ----------------------------------------------------------------------------- payload builders
def pushall_payload() -> dict:
    return {"pushing": {"sequence_id": "1", "command": "pushall"}}


def tray_index(ams_id: int, slot_id: int) -> int:
    """Global tray index used in ams_mapping: ams_id*4+slot; AMS HT units (id >= 128) use their id."""
    return ams_id if ams_id >= 128 else ams_id * 4 + slot_id


def tray_ams_slot(tray: int) -> tuple[int, int]:
    return (tray, 0) if tray >= 128 else (tray // 4, tray % 4)


def payload_style_for(serial: str) -> str:
    """'legacy' (file:///sdcard/..) for A1 / A1 mini (serials 030..., 039...), 'orca' (the OrcaSlicer/
    Bambu Studio form, ftp://<file> plus the extra cali fields) for P1/X1 firmware, which on a P1S with
    firmware 01.10 answers the legacy form with HMS 0500-4003 ("unable to parse the file")."""
    return "legacy" if (serial or "")[:3] in ("030", "039") else "orca"


def project_file_payload(*, subtask_name: str, remote_file: str, md5: str, plate: int = 1,
                         trays: list[int], use_ams: bool = True, timelapse: bool = False,
                         bed_leveling: bool = True, flow_cali: bool = False,
                         vibration_cali: bool = False, layer_inspect: Optional[bool] = None,
                         bed_type: Optional[str] = None, style: str = "legacy") -> dict:
    if style == "orca":
        return {"print": {
            "sequence_id": next_seq(), "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "subtask_name": re.sub(r"(\.gcode)?\.3mf$", "", remote_file, flags=re.I),
            "url": f"ftp://{remote_file}", "file": remote_file, "md5": md5,
            "timelapse": timelapse, "bed_type": bed_type or "textured_plate",
            "bed_leveling": bed_leveling, "auto_bed_leveling": 1 if bed_leveling else 0,
            "flow_cali": flow_cali, "extrude_cali_flag": 1 if flow_cali else 2,
            "nozzle_offset_cali": 2, "cfg": "0", "vibration_cali": vibration_cali,
            "layer_inspect": True if layer_inspect is None else layer_inspect,
            "use_ams": use_ams,
            "ams_mapping": list(trays),
            "ams_mapping2": [{"ams_id": tray_ams_slot(t)[0], "slot_id": tray_ams_slot(t)[1]} for t in trays],
            "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0"}}
    layer_inspect = bool(layer_inspect)
    bed_type = bed_type or "auto"
    return {"print": {
        "sequence_id": next_seq(), "command": "project_file",
        "param": f"Metadata/plate_{plate}.gcode",
        "subtask_name": subtask_name, "url": f"file:///sdcard/{remote_file}", "md5": md5,
        "timelapse": timelapse, "bed_type": bed_type, "bed_leveling": bed_leveling,
        "flow_cali": flow_cali, "vibration_cali": vibration_cali, "layer_inspect": layer_inspect,
        "use_ams": use_ams,
        "ams_mapping": list(trays),
        "ams_mapping2": [{"ams_id": tray_ams_slot(t)[0], "slot_id": tray_ams_slot(t)[1]} for t in trays],
        "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0"}}


def pause_payload() -> dict:
    return {"print": {"sequence_id": next_seq(), "command": "pause"}}


def resume_payload() -> dict:
    return {"print": {"sequence_id": next_seq(), "command": "resume"}}


def stop_payload() -> dict:
    return {"print": {"sequence_id": next_seq(), "command": "stop"}}


def light_payload(on: bool, node: str = "chamber_light") -> dict:
    return {"system": {"sequence_id": next_seq(), "command": "ledctrl", "led_node": node,
                       "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}}


SPEED_LEVELS = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}


def speed_payload(level: int) -> dict:
    return {"print": {"sequence_id": next_seq(), "command": "print_speed", "param": str(int(level))}}


def gcode_line_payload(gcode: str) -> dict:
    if not gcode.endswith("\n"):
        gcode += "\n"
    return {"print": {"sequence_id": next_seq(), "command": "gcode_line", "param": gcode}}


# ----------------------------------------------------------------------------- state helpers
def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def trays_of(state: dict) -> list[dict]:
    out = []
    for unit in (state.get("print", {}).get("ams", {}) or {}).get("ams", []) or []:
        uid = int(unit.get("id", 0))
        for t in unit.get("tray", []) or []:
            sid = int(t.get("id", 0))
            out.append({"tray": tray_index(uid, sid), "ams": uid, "slot": sid + 1,
                        "type": t.get("tray_type") or None, "color": t.get("tray_color") or None,
                        "brand": t.get("tray_sub_brands") or None})
    return out


def summarize(name: str, state: dict, age_s: Optional[float] = None) -> dict:
    p = state.get("print", {})
    rem = p.get("mc_remaining_time")
    return {
        "printer": name,
        "state": p.get("gcode_state"),
        "job": p.get("subtask_name") or None,
        "progress_pct": p.get("mc_percent"),
        "remaining_min": rem,
        "layer": p.get("layer_num"), "total_layers": p.get("total_layer_num"),
        "nozzle_c": p.get("nozzle_temper"), "nozzle_target_c": p.get("nozzle_target_temper"),
        "bed_c": p.get("bed_temper"), "bed_target_c": p.get("bed_target_temper"),
        "print_error": p.get("print_error"), "stage": p.get("stg_cur"),
        "print_error_hex": hex(p["print_error"]) if isinstance(p.get("print_error"), int) and p["print_error"] else None,
        "hms": p.get("hms") or [], "ams_status": p.get("ams_status"),
        "ams_units": [{"id": int(u.get("id", 0)), "humidity": u.get("humidity"), "temp": u.get("temp"),
                       "trays": len(u.get("tray", []) or [])}
                      for u in (p.get("ams", {}) or {}).get("ams", []) or []],
        "ams_trays": trays_of(state),
        "report_age_s": None if age_s is None else round(age_s, 1),
    }


def _color_ok(have: Optional[str], want: str) -> bool:
    have = (have or "").upper().lstrip("#")
    want = want.upper().lstrip("#")
    if len(want) == 6:
        return have[:6] == want
    return have == want


def preflight(state: dict, trays: list[int], expect_filament: Optional[str],
              expect_color: Optional[str]) -> list[str]:
    """Return a list of human-readable problems (empty = OK to start)."""
    problems = []
    pr = state.get("print", {})
    gs = pr.get("gcode_state")
    failed_clear = gs == "FAILED" and not pr.get("print_error") and not pr.get("hms")
    if gs not in ("IDLE", "FINISH") and not failed_clear:
        problems.append(f"printer state is {gs!r}; must be IDLE or FINISH "
                        f"(FAILED is accepted only with print_error=0 and no HMS)")
    have = {t["tray"]: t for t in trays_of(state)}
    for t in trays:
        tr = have.get(t)
        if tr is None:
            problems.append(f"tray {t} not present in AMS report")
            continue
        if expect_filament and (tr["type"] or "").upper() != expect_filament.upper():
            problems.append(f"tray {t} holds {tr['type']!r}, expected {expect_filament!r}")
        if expect_color and not _color_ok(tr["color"], expect_color):
            problems.append(f"tray {t} colour is {tr['color']!r}, expected {expect_color!r}")
    return problems


# ----------------------------------------------------------------------------- connection
class PrinterError(RuntimeError):
    pass


def _paho_factory() -> Any:
    import paho.mqtt.client as mqtt
    cid = f"bambu_mcp_{os.getpid()}_{int(time.time()) % 100000}"
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)


class PrinterConn:
    def __init__(self, printer: Printer, client_factory: Optional[Callable[[], Any]] = None):
        self.p = printer
        self._factory = client_factory or _paho_factory
        self._client = None
        self._lock = threading.RLock()
        self._state: dict = {}
        self._last_report = 0.0
        self.last_reply: dict = {}
        self.connected = threading.Event()
        self._got_full = threading.Event()

    # -- callbacks
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(f"device/{self.p.serial}/report")
            self.connected.set()
            client.publish(f"device/{self.p.serial}/request", json.dumps(pushall_payload()))
        else:
            self.connected.clear()

    def _on_disconnect(self, client, userdata, rc):
        self.connected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            d = json.loads(msg.payload)
        except Exception:
            return
        with self._lock:
            for k, v in d.items():
                if isinstance(v, dict):
                    deep_merge(self._state.setdefault(k, {}), v)
            self._last_report = time.time()
            pr = d.get("print")
            if isinstance(pr, dict):
                if "command" in pr and "result" in pr:
                    self.last_reply = {"command": pr["command"], "result": pr["result"],
                                       "reason": pr.get("reason"), "ts": time.time()}
                if "gcode_state" in self._state.get("print", {}):
                    self._got_full.set()

    # -- public
    def ensure(self, timeout: float = 15.0) -> None:
        with self._lock:
            if self._client is None:
                c = self._factory()
                c.username_pw_set("bblp", self.p.code)
                c.tls_set(cert_reqs=ssl.CERT_NONE)
                c.tls_insecure_set(True)
                c.on_connect, c.on_message, c.on_disconnect = (
                    self._on_connect, self._on_message, self._on_disconnect)
                try:
                    c.connect(self.p.ip, 8883, keepalive=60)
                except Exception as e:
                    raise PrinterError(f"{self.p.name}: MQTT connect failed: {scrub(e)}") from None
                c.loop_start()
                self._client = c
        if not self.connected.wait(timeout):
            raise PrinterError(f"{self.p.name}: MQTT not connected after {timeout:.0f}s")
        if not self._got_full.wait(timeout):
            self.pushall()
            if not self._got_full.wait(timeout):
                raise PrinterError(f"{self.p.name}: no state report received")

    def pushall(self) -> None:
        self._raw_publish(pushall_payload())

    def _raw_publish(self, payload: dict) -> None:
        if self._client is None:
            raise PrinterError("not connected")
        self._client.publish(f"device/{self.p.serial}/request", json.dumps(payload), qos=0)

    def send_command(self, payload: dict) -> None:
        """Publish a control command. Callers must have applied readonly/dry-run policy."""
        self.ensure()
        self._raw_publish(payload)

    def state(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def age(self) -> Optional[float]:
        return (time.time() - self._last_report) if self._last_report else None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self.connected.clear()
        self._got_full.clear()
