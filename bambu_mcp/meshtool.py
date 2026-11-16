"""Pure-python model pre-transform (scale / rotation) used before slicing.

The installed OrcaSlicer CLI build crashes (SIGSEGV) on --scale / --rotate*, so these transforms are applied
to the mesh here and the result is handed to the slicer as a binary STL (Orca then centres it on the plate).
Inputs: binary/ASCII STL, or 3MF (all <mesh> objects are merged; build-item transforms/components ignored).
"""
from __future__ import annotations

import math
import re
import struct
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

Vec = tuple[float, float, float]
TRANSFORM_KEYS = ("scale", "rotate", "rotate_x", "rotate_y")


def needs_pretransform(xform: Optional[dict]) -> bool:
    return any((xform or {}).get(k) is not None for k in TRANSFORM_KEYS)


def load_mesh(path: Path) -> tuple[list[Vec], list[tuple[int, int, int]]]:
    suffix = path.suffix.lower()
    if suffix == ".3mf":
        return _load_3mf(path)
    if suffix == ".stl":
        return _load_stl(path)
    raise ValueError(f"cannot transform {path.name}: only .stl and .3mf are supported")


def _load_stl(path: Path):
    data = path.read_bytes()
    if len(data) >= 84 and 84 + 50 * struct.unpack("<I", data[80:84])[0] == len(data):
        n = struct.unpack("<I", data[80:84])[0]
        verts: list[Vec] = []
        tris = []
        for i in range(n):
            f = struct.unpack("<9f", data[84 + 50 * i + 12: 84 + 50 * i + 48])
            base = len(verts)
            verts += [f[0:3], f[3:6], f[6:9]]
            tris.append((base, base + 1, base + 2))
        return verts, tris
    nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    if not nums or len(nums) % 3:
        raise ValueError(f"{path.name} is not a valid STL")
    verts = [(float(a), float(b), float(c)) for a, b, c in nums]
    return verts, [(i, i + 1, i + 2) for i in range(0, len(verts), 3)]


def _load_3mf(path: Path):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".model")]
        verts: list[Vec] = []
        tris: list[tuple[int, int, int]] = []
        for n in names:
            root = ET.fromstring(z.read(n))
            for mesh in root.iterfind(".//{*}mesh"):
                base = len(verts)
                for v in mesh.iterfind("{*}vertices/{*}vertex"):
                    verts.append((float(v.get("x")), float(v.get("y")), float(v.get("z"))))
                for t in mesh.iterfind("{*}triangles/{*}triangle"):
                    tris.append((base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3"))))
    if not verts or not tris:
        raise ValueError(f"{path.name} contains no mesh")
    return verts, tris


def _rot(v: Vec, rx: float, ry: float, rz: float) -> Vec:
    x, y, z = v
    if rx:
        c, s = math.cos(math.radians(rx)), math.sin(math.radians(rx)); y, z = y * c - z * s, y * s + z * c
    if ry:
        c, s = math.cos(math.radians(ry)), math.sin(math.radians(ry)); x, z = x * c + z * s, -x * s + z * c
    if rz:
        c, s = math.cos(math.radians(rz)), math.sin(math.radians(rz)); x, y = x * c - y * s, x * s + y * c
    return x, y, z


def transform_mesh(verts: list[Vec], xform: dict) -> list[Vec]:
    """Rotate (X, then Y, then Z, degrees) and scale about the bbox centre; then sit on z=0, XY centred at 0."""
    scale = float(xform.get("scale") if xform.get("scale") is not None else 1.0)
    rx, ry, rz = (float(xform.get(k) or 0.0) for k in ("rotate_x", "rotate_y", "rotate"))
    c = tuple((min(v[i] for v in verts) + max(v[i] for v in verts)) / 2 for i in range(3))
    out = [tuple(k * scale for k in _rot((v[0] - c[0], v[1] - c[1], v[2] - c[2]), rx, ry, rz)) for v in verts]
    zmin = min(v[2] for v in out)
    cx = (min(v[0] for v in out) + max(v[0] for v in out)) / 2
    cy = (min(v[1] for v in out) + max(v[1] for v in out)) / 2
    return [(v[0] - cx, v[1] - cy, v[2] - zmin) for v in out]


def write_stl(path: Path, verts: list[Vec], tris) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(b"bambu_mcp pre-transformed".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            A, B, C = verts[a], verts[b], verts[c]
            u = (B[0] - A[0], B[1] - A[1], B[2] - A[2]); w = (C[0] - A[0], C[1] - A[1], C[2] - A[2])
            n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
            ln = math.sqrt(sum(k * k for k in n)) or 1.0
            f.write(struct.pack("<12fH", n[0] / ln, n[1] / ln, n[2] / ln, *A, *B, *C, 0))


def prepare_model(model: Path, xform: Optional[dict], outdir: Path) -> Path:
    """Return `model` unchanged, or a transformed binary STL in `outdir` when scale/rotation was asked."""
    if not needs_pretransform(xform):
        return model
    verts, tris = load_mesh(model)
    out = outdir / f"{model.stem}_xform.stl"
    write_stl(out, transform_mesh(verts, xform or {}), tris)
    return out
