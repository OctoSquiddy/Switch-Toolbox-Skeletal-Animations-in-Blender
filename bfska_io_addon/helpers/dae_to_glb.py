#!/usr/bin/env python3
# =============================================================================
#  dae_to_glb.py  --  Collada (.dae) -> glTF 2.0 (.glb / .gltf) via AssimpNet
#  AssimpNet.dll + native assimp.dll are loaded from `../dlls/` (relative to
#  this script), so the addon is fully portable — no machine-specific paths.
#    py -3.11 dae_to_glb.py <input.dae> [output.glb]
# =============================================================================
from __future__ import annotations

import os
import sys

if len(sys.argv) < 2:
    sys.exit("usage: py dae_to_glb.py <input.dae> [output.glb]")
IN_PATH = os.path.abspath(sys.argv[1])
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(IN_PATH)[0] + ".glb"
OUT_PATH = os.path.abspath(OUT_PATH)

HERE = os.path.dirname(os.path.abspath(__file__))
# Bundled DLL location: <addon>/dlls/  (both AssimpNet.dll and native assimp.dll)
DLLDIR = os.path.normpath(os.path.join(HERE, "..", "dlls"))

if not os.path.isdir(DLLDIR):
    sys.exit(f"bundled DLL dir not found: {DLLDIR}\n"
             f"(expected ../dlls/ next to this script)")

# AssimpNet.dll loaded via sys.path; native assimp.dll loaded by AssimpNet via PATH.
sys.path.append(DLLDIR)
os.environ["PATH"] = DLLDIR + os.pathsep + os.environ.get("PATH", "")

from pythonnet import load
load("netfx")
import clr

clr.AddReference("AssimpNet")
from Assimp import AssimpContext, PostProcessSteps

print(f"Loading {IN_PATH}")
ctx = AssimpContext()
flags = (
    PostProcessSteps.Triangulate
    | PostProcessSteps.GenerateNormals
    | PostProcessSteps.JoinIdenticalVertices
    | PostProcessSteps.LimitBoneWeights
    | PostProcessSteps.CalculateTangentSpace
)
scene = ctx.ImportFile(IN_PATH, flags)

print(f"  meshes:    {scene.MeshCount}")
print(f"  materials: {scene.MaterialCount}")
print(f"  animations:{scene.AnimationCount}")
print(f"  textures:  {scene.TextureCount}")

fmts = list(ctx.GetSupportedExportFormats())
fmt_ids = {f.FormatId.lower(): f for f in fmts}

# Blender's glTF importer ONLY accepts glTF 2.0; prefer v2 IDs.
candidates = ("glb2", "gltf2", "glb", "gltf")
chosen = next((fid for fid in candidates if fid in fmt_ids), None)
if not chosen:
    sys.exit("No glTF exporter registered in this Assimp build.\n"
             "available: " + ", ".join(fmt_ids))

out_path = OUT_PATH
if chosen in ("gltf2", "gltf"):
    out_path = os.path.splitext(OUT_PATH)[0] + ".gltf"

print(f"Writing {out_path}  (format={chosen})")
ok = ctx.ExportFile(scene, out_path, chosen)
if not ok:
    for cand in candidates:
        if cand == chosen or cand not in fmt_ids: continue
        retry_path = (os.path.splitext(OUT_PATH)[0]
                      + (".gltf" if cand in ("gltf2", "gltf") else ".glb"))
        print(f"  retry with format={cand} -> {retry_path}")
        if ctx.ExportFile(scene, retry_path, cand):
            out_path = retry_path
            chosen   = cand
            ok = True
            break
    if not ok:
        sys.exit("Export failed (tried: " + ", ".join(candidates) + ").")


# POST-FIX: Assimp's glTF2 writes JOINTS_* as FLOAT (5126), invalid per the
# glTF 2.0 spec -> Blender's importer crashes.  Re-pack to UNSIGNED_SHORT.
def _gltf_fix_joint_types(gltf_path):
    import json as _json, struct as _struct
    if gltf_path.endswith(".glb"):
        return
    with open(gltf_path, "r", encoding="utf-8") as fp:
        g = _json.load(fp)
    accessors = g.get("accessors") or []
    buffer_views = g.get("bufferViews") or []
    buffers = g.get("buffers") or []
    if not (accessors and buffer_views and buffers):
        return
    bin_path = os.path.join(os.path.dirname(gltf_path), buffers[0]["uri"])
    if not os.path.isfile(bin_path):
        return
    with open(bin_path, "rb") as fp:
        bin_data = bytearray(fp.read())

    joint_accs = set()
    for mesh in g.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            for k, v in attrs.items():
                if k.startswith("JOINTS_") and isinstance(v, int):
                    joint_accs.add(v)
    if not joint_accs:
        return

    comp_count = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
                  "MAT2": 4, "MAT3": 9, "MAT4": 16}
    patched = 0
    for ai in sorted(joint_accs):
        acc = accessors[ai]
        if acc.get("componentType") != 5126:
            continue
        bv = buffer_views[acc["bufferView"]]
        n_comp = comp_count.get(acc.get("type", "VEC4"), 4)
        n_elem = acc["count"] * n_comp
        b_off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        floats = _struct.unpack_from("<%df" % n_elem, bin_data, b_off)
        ushorts = [max(0, min(65535, int(round(x)))) for x in floats]
        new_bytes = _struct.pack("<%dH" % n_elem, *ushorts)
        new_bv_off = len(bin_data)
        bin_data.extend(new_bytes)
        new_bv_idx = len(buffer_views)
        buffer_views.append({
            "buffer": bv["buffer"], "byteOffset": new_bv_off,
            "byteLength": len(new_bytes),
            "target": bv.get("target", 34962),
        })
        acc["bufferView"]  = new_bv_idx
        acc["byteOffset"] = 0
        acc["componentType"] = 5123
        for k in ("min", "max"): acc.pop(k, None)
        patched += 1

    if not patched:
        return
    buffers[0]["byteLength"] = len(bin_data)
    g["bufferViews"] = buffer_views
    g["buffers"] = buffers
    g["accessors"] = accessors
    with open(bin_path, "wb") as fp:
        fp.write(bin_data)
    with open(gltf_path, "w", encoding="utf-8") as fp:
        _json.dump(g, fp)
    print(f"  patched {patched} JOINTS_ accessor(s): float -> uint16")


_gltf_fix_joint_types(out_path)
print("Done.")
