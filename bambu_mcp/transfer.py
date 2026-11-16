"""3MF inspection, FTPS upload (curl) and OrcaSlicer CLI slicing."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .config import Printer, scrub

ORCA_BIN = os.environ.get("BAMBU_ORCA_BIN", "~/projects/orca-pr12998/build/src/orca-slicer")


def curl_bin() -> str:
    return os.environ.get("BAMBU_MCP_CURL", "curl")


def md5_file(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 (protocol requires md5)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def inspect_3mf(path: Path, plate: int = 1) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"3mf not found: {path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path.name} is not a valid 3mf (zip) file")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    gc = f"Metadata/plate_{plate}.gcode"
    if gc not in names:
        raise ValueError(f"{path.name} has no {gc} (is it a sliced .gcode.3mf? entries: "
                         f"{[n for n in names if n.endswith('.gcode')]})")
    return {"size": path.stat().st_size, "md5": md5_file(path), "gcode_entry": gc}


def safe_remote_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "job"
    return n if n.lower().endswith(".3mf") else n + ".3mf"


def _run_cancellable(proc: subprocess.Popen, cancel, timeout: float) -> tuple[str, str]:
    deadline = time.time() + timeout
    while True:
        try:
            return proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill(); proc.communicate()
                from .jobs import JobCancelled
                raise JobCancelled()
            if time.time() > deadline:
                proc.kill(); proc.communicate()
                raise TimeoutError(f"timed out after {timeout:.0f}s")


def ftps_upload(printer: Printer, path: Path, remote_name: str, cancel=None,
                timeout: float = 600) -> None:
    """Implicit FTPS (port 990) upload. The access code goes to curl through a config on stdin
    so it never appears in the process list."""
    cfg = f'user = "bblp:{printer.code}"\n'
    cmd = [curl_bin(), "-sS", "--ssl-reqd", "-k", "--ftp-pasv", "--max-time", str(int(timeout)),
           "-K", "-", "-T", str(path), f"ftps://{printer.ip}:990/{remote_name}",
           "-w", "%{http_code}"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    assert proc.stdin
    proc.stdin.write(cfg); proc.stdin.close()
    out, err = _run_cancellable(proc, cancel, timeout + 10)
    if proc.returncode != 0 or not out.strip().endswith("226"):
        raise RuntimeError(f"FTPS upload failed (curl rc={proc.returncode}, "
                           f"code={out.strip()[-3:]!r}): {scrub(err.strip())[:300]}")


def _curl_ftps(printer: Printer, url: str, extra=(), timeout: float = 60) -> bytes:
    cfg = f'user = "bblp:{printer.code}"\n'
    r = subprocess.run([curl_bin(), "-sS", "--ssl-reqd", "-k", "--ftp-pasv", "--max-time", str(int(timeout)),
                        "-K", "-", *extra, url], input=cfg.encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FTPS failed (curl rc={r.returncode}): "
                           f"{scrub(r.stderr.decode(errors='replace').strip())[:300]}")
    return r.stdout


def ftps_list(printer: Printer) -> list[str]:
    """File names in the printer's FTPS root (the SD card)."""
    out = _curl_ftps(printer, f"ftps://{printer.ip}:990/", ["--list-only"])
    return [n.strip() for n in out.decode(errors="replace").splitlines() if n.strip()]


def ftps_download(printer: Printer, remote_name: str, dest: Path, timeout: float = 300) -> Path:
    data = _curl_ftps(printer, f"ftps://{printer.ip}:990/{urllib.parse.quote(remote_name)}", timeout=timeout)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def slice_flags(xform: Optional[dict]) -> list[str]:
    """Validate {scale, rotate, rotate_x, rotate_y, orient, arrange} and return the OrcaSlicer CLI flags.

    Only orient/arrange are CLI flags: scale/rotate* are applied to the mesh beforehand (meshtool), because
    this Orca build segfaults on --scale/--rotate*. (--arrange 0 and --repetitions also fail there.)"""
    x = {k: v for k, v in (xform or {}).items() if v is not None}
    unknown = set(x) - {"scale", "rotate", "rotate_x", "rotate_y", "orient", "arrange"}
    if unknown:
        raise ValueError(f"unknown slice option(s): {sorted(unknown)}")
    if "scale" in x and not 0 < float(x["scale"]) <= 100:
        raise ValueError("scale must be a factor in (0, 100], e.g. 0.33 for 33%")
    flags: list[str] = []
    for k in ("orient", "arrange"):
        if x.get(k):
            flags += [f"--{k}", "1"]
    return flags


def used_grams(text: str) -> Optional[list[float]]:
    m = re.search(r";\s*filament used \[g\]\s*=\s*([0-9.,\s]+)", text)
    if not m:
        return None
    return [float(x) for x in m.group(1).replace(",", " ").split()]


def slice_model(model: Path, profile_dir: Path, name: str, outdir: Path, cancel=None,
                timeout: float = 1800, log: Optional[Callable[[str], None]] = None,
                xform: Optional[dict] = None) -> dict:
    """Run OrcaSlicer CLI with flattened profiles found in profile_dir."""
    for f in ("machine_flat.json", "process_flat.json", "filament_flat.json"):
        if not (profile_dir / f).is_file():
            raise FileNotFoundError(f"{f} missing in {profile_dir}")
    if not model.is_file():
        raise FileNotFoundError(f"model not found: {model}")
    outdir.mkdir(parents=True, exist_ok=True)
    flags = slice_flags(xform)                      # validates before any work
    from .meshtool import prepare_model
    model = prepare_model(model, xform, outdir / "pre")
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/tmp/xdg-{os.environ.get('USER', 'user')}")
    Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700, exist_ok=True)
    cmd = ["xvfb-run", "-a", str(Path(ORCA_BIN).expanduser()),
           "--load-settings", "machine_flat.json;process_flat.json",
           "--load-filaments", "filament_flat.json", *flags, "--slice", "1",
           "--export-3mf", f"{name}.3mf", "--outputdir", str(outdir), str(model)]
    proc = subprocess.Popen(cmd, cwd=profile_dir, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    out, _ = _run_cancellable(proc, cancel, timeout)
    (outdir / "slice.log").write_text(out or "")
    threemf = outdir / f"{name}.3mf"
    if proc.returncode != 0 or not threemf.is_file():
        raise RuntimeError(f"slicing failed (rc={proc.returncode}); log: {outdir / 'slice.log'}; "
                           f"tail: {(out or '')[-300:]}")
    grams = None
    gc = outdir / "plate_1.gcode"
    if gc.is_file():
        grams = used_grams(gc.read_text(errors="ignore"))
    else:
        with zipfile.ZipFile(threemf) as z:
            if "Metadata/plate_1.gcode" in z.namelist():
                grams = used_grams(z.read("Metadata/plate_1.gcode").decode(errors="ignore"))
    return {"threemf": str(threemf), "filament_used_g": grams, "log": str(outdir / "slice.log")}
