# =============================================================================
#  bfska_io_addon/__init__.py  --  Splatoon-3 .bfska / model I/O for Blender
#
#  PORTABLE folder-based Blender addon.  Adds entries under the standard
#       File > Import     :   Splatoon Model (.dae / .glb)
#                             Splatoon Animation (.bfska)
#       File > Export     :   Splatoon Model (.fbx)
#                             Splatoon Animation (.bfska)
#
#  Layout (everything self-contained — no machine-specific paths):
#       bfska_io_addon/
#         __init__.py             <- this file (registers operators, menus)
#         helpers/
#           bfska_to_json.py      <- .bfska -> JSON  (subprocess, pythonnet)
#           make_bfska.py         <- JSON  -> .bfska (subprocess, pythonnet)
#           dae_to_glb.py         <- .dae  -> .glb   (subprocess, pythonnet)
#         dlls/                   <- bundled BfresLibrary + AssimpNet + deps
#           BfresLibrary.dll, Newtonsoft.Json.dll, Syroot.*.dll,
#           AssimpNet.dll, assimp.dll (native)
#
#  Blender's bundled Python cannot load .NET, so helpers are spawned in
#  system Python (py -3.11) via subprocess.  They look for the bundled DLLs
#  with a path relative to themselves (../dlls), so the whole addon folder
#  is fully relocatable.
#
#  Requirements on the host machine (one-time, per user):
#    - py.exe launcher with Python 3.11  (Microsoft Store / python.org)
#    - pythonnet:        py -3.11 -m pip install pythonnet
#    (No Toolbox install / build needed — DLLs are bundled.)
# =============================================================================

bl_info = {
    "name": "Splatoon BFSKA I/O",
    "author": "Blender MCP project",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "File > Import / Export",
    "description": "Import Splatoon-3 .dae/.glb models + .bfska animations; export animations back to .bfska and models to .fbx.",
    "category": "Import-Export",
}

import bpy, os, sys, subprocess, json, math, tempfile
from bpy.types import Operator
from bpy.props import StringProperty, CollectionProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
from mathutils import Matrix, Vector, Euler, Quaternion


# ============================================================================
#  Path constants — resolved relative to this __init__.py at import time.
#  Means the whole addon folder is relocatable: copy/move it anywhere and
#  the helpers + DLLs are still found correctly.
# ============================================================================
ADDON_DIR     = os.path.dirname(os.path.abspath(__file__))
HELPERS_DIR   = os.path.join(ADDON_DIR, "helpers")
DLLS_DIR      = os.path.join(ADDON_DIR, "dlls")
DAE_TO_GLB    = os.path.join(HELPERS_DIR, "dae_to_glb.py")
BFSKA_TO_JSON = os.path.join(HELPERS_DIR, "bfska_to_json.py")
MAKE_BFSKA    = os.path.join(HELPERS_DIR, "make_bfska.py")


# ============================================================================
#  Embedded helper scripts.  Each is a verbatim copy of the standalone
#  tools/bfska/*.py / tools/bfres/*.py originals.  They run in system
#  Python (py -3.11) because Blender's bundled Python has no .NET.
# ============================================================================

# ============================================================================
#  Materialise embedded helpers to TEMP on first use.  Per-content hash so
#  each addon version (or edit) lands in its own dir and isn't accidentally
#  shadowed by a stale copy.
# ============================================================================
# ============================================================================
#  Subprocess plumbing
# ============================================================================
def _system_python():
    """Locate a system Python (with pythonnet) for the .NET helpers.
    Tries `py -3.11`, `py`, `python3.11`, `python` in that order."""
    candidates = [
        ["py", "-3.11"], ["py.exe", "-3.11"],
        ["python3.11"], ["python3.11.exe"],
        ["py"], ["py.exe"],
        ["python"], ["python.exe"],
    ]
    for c in candidates:
        try:
            r = subprocess.run(c + ["-c", "import sys; print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return c
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _run(cmd, label):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return False, f"{label} failed (exit {r.returncode}):\n{(r.stderr or r.stdout)[-2000:]}"
        return True, r.stdout or ""
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out"
    except Exception as e:
        return False, f"{label}: {e}"


# ============================================================================
#  Parse the .glb/.gltf produced by dae_to_glb to recover ORIGINAL bone
#  matrices.  Why: Blender's glTF importer applies a "bone heuristic" that
#  REORIENTS bones to point along their Y axis (head-to-tail).  Blender's
#  skinning compensates so the model deforms correctly visually, but
#  pb.matrix / pb.bone.matrix_local then differ from the source file's
#  matrices.  At BFSKA export time we sample pb.matrix and write it
#  verbatim -> the game (which has the ORIGINAL rest in its model file)
#  sees the wrong orientations -> animation looks "rotated".
#  Fix: stash the source matrices on each bone at import time; apply a
#  per-bone correction at export time.  The Blender display stays the
#  way Blender wants it.
# ============================================================================
def _glb_extract_json(glb_path):
    """Pull the JSON chunk out of a binary .glb."""
    import json as _json, struct as _struct
    with open(glb_path, "rb") as fp:
        magic = fp.read(4)
        if magic != b"glTF":
            raise ValueError("not a .glb")
        _ver, _len = _struct.unpack("<II", fp.read(8))
        chunk_len, chunk_type = _struct.unpack("<II", fp.read(8))
        if chunk_type != 0x4E4F534A:    # 'JSON'
            raise ValueError("first chunk is not JSON")
        return _json.loads(fp.read(chunk_len).decode("utf-8"))


def _gltf_read_arm_rests(gltf_path):
    """Return  dict bone_name -> 4x4 ARMATURE-SPACE rest matrix  in the
    glTF / source file's convention (i.e. the matrices Blender would have
    used if it didn't reorient bones).  Returns {} if anything is missing."""
    import json as _json
    if not gltf_path or not os.path.isfile(gltf_path):
        return {}
    try:
        if gltf_path.lower().endswith(".glb"):
            g = _glb_extract_json(gltf_path)
        else:
            with open(gltf_path, "r", encoding="utf-8") as fp:
                g = _json.load(fp)
    except Exception:
        return {}

    nodes = g.get("nodes") or []
    skins = g.get("skins") or []
    if not nodes or not skins:
        return {}

    def _local_mat(node):
        # glTF: node has either "matrix" (column-major 16 floats) or TRS.
        if "matrix" in node:
            m = node["matrix"]
            # Column-major -> Blender's Matrix takes rows; transpose.
            return Matrix(((m[0], m[4], m[8],  m[12]),
                           (m[1], m[5], m[9],  m[13]),
                           (m[2], m[6], m[10], m[14]),
                           (m[3], m[7], m[11], m[15])))
        T = Vector(node.get("translation", (0.0, 0.0, 0.0)))
        rq = node.get("rotation", (0.0, 0.0, 0.0, 1.0))   # glTF: x,y,z,w
        R = Quaternion((rq[3], rq[0], rq[1], rq[2]))        # bpy:  w,x,y,z
        S = Vector(node.get("scale", (1.0, 1.0, 1.0)))
        # Compose T*R*S into a 4x4 (Matrix.LocRotScale was added in 3.0+).
        try:
            return Matrix.LocRotScale(T, R, S)
        except AttributeError:
            M = Matrix.Translation(T) @ R.to_matrix().to_4x4()
            M = M @ Matrix.Diagonal(Vector((S.x, S.y, S.z, 1.0)))
            return M

    locals_by_idx = {i: _local_mat(nodes[i]) for i in range(len(nodes))}

    # Skin root: skin.skeleton if set, else any joint not referenced as a child.
    skin = skins[0]
    joints = list(skin.get("joints") or [])
    if not joints:
        return {}
    root_idx = skin.get("skeleton")
    if root_idx is None:
        children_of_any = set()
        for n in nodes:
            for c in (n.get("children") or []):
                children_of_any.add(c)
        # First joint that is not a child of any other node.
        root_idx = next((j for j in joints if j not in children_of_any), joints[0])

    # The skin's skeleton root might NOT itself be a joint (e.g. an "Armature"
    # holder node).  Walk from skeleton root regardless and only record names
    # that correspond to joints (since only those are bones).
    joint_set = set(joints)
    result = {}
    def visit(idx, parent_arm):
        local = locals_by_idx.get(idx)
        if local is None: return
        arm = parent_arm @ local
        if idx in joint_set:
            nm = nodes[idx].get("name") or f"node_{idx}"
            result[nm] = arm
        for c in (nodes[idx].get("children") or []):
            visit(c, arm)

    visit(root_idx, Matrix.Identity(4))
    # Also visit the skeleton root itself if it's a joint (covered above).
    return result


def _stash_orig_arm_rests(arm_obj, name_to_mat):
    """Save 4x4 matrices as 16-float lists on the Bone custom-property dict.
    Stored on `bone.bone` (the Bone in armature.data) so they survive save/load.

    The stash MUST be in the same coordinate space as `bone.matrix_local`,
    otherwise the export-time formula `pb.matrix @ A_blend^-1 @ A_orig` would
    mix mismatched spaces and rotate the root bone by 90 deg.  But Blender
    versions differ:
      - Blender 5.x glTF importer: applies the Y-up->Z-up conversion to each
        BONE matrix; armature.matrix_world stays identity, bones in Z-up.
      - Blender 4.1 glTF importer: applies the conversion to the ARMATURE
        OBJECT's matrix_world; bones stay in Y-up internally.
    We auto-detect which one happened by comparing bone.matrix_local against
    both (raw Y-up) and (R_x(+90) @ raw); whichever is closer wins.  Then
    stash in that same space.  This way the export math is correct without
    any version checks."""
    stashed = 0
    if not name_to_mat:
        return 0
    Y_UP_TO_Z_UP = Matrix.Rotation(math.radians(90), 4, 'X')

    # Per-bone name lookup with the same fallbacks the importer's rename uses.
    available = dict(name_to_mat)
    lc = {k.lower(): k for k in available}

    def _lookup(n):
        for k in (n, f"{arm_obj.name}_{n}", f"Armature_{n}"):
            if k in available:
                return available[k]
        if n.lower() in lc:
            return available[lc[n.lower()]]
        for k, m in available.items():
            if k.endswith("_" + n) or k.endswith(n):
                return m
        return None

    # ---- Detect Blender's convention from the FIRST matching bone ----------
    # In the no-heuristic case (most bones), one of two relations holds
    # exactly:  A_blend == Y_UP_TO_Z_UP @ A_orig   (5.x style, Z-up bones)
    #      or:  A_blend == A_orig                  (4.1 style, Y-up bones).
    # If a heuristic *was* applied for this particular bone, both diffs will
    # be large; we still need a vote, so we keep scanning until we find a
    # bone with a clearly-better match in one direction.
    convention_z_up = None
    for bone in arm_obj.data.bones:
        orig = _lookup(bone.name)
        if orig is None:
            continue
        A_blend = bone.matrix_local
        d_yup = sum(abs(A_blend[i][j] - orig[i][j])
                    for i in range(4) for j in range(4))
        d_zup = sum(abs(A_blend[i][j] - (Y_UP_TO_Z_UP @ orig)[i][j])
                    for i in range(4) for j in range(4))
        # Need a clear winner (>10x difference) to settle on a convention.
        if d_zup < d_yup * 0.1:
            convention_z_up = True; break
        if d_yup < d_zup * 0.1:
            convention_z_up = False; break
    if convention_z_up is None:
        # No clear winner from any bone -> heuristic must be in play for all.
        # Default to Z-up (Blender 5.x style) — matches most modern installs.
        convention_z_up = True

    # ---- Stash each bone's rest in the matching convention ----------------
    for bone in arm_obj.data.bones:
        orig = _lookup(bone.name)
        if orig is None:
            continue
        orig_in_blender_space = (Y_UP_TO_Z_UP @ orig) if convention_z_up else orig
        bone["_bfska_orig_arm_rest"] = [
            orig_in_blender_space[r][c] for r in range(4) for c in range(4)
        ]
        stashed += 1
    return stashed


# ============================================================================
#  DAE -> GLB -> Blender import
# ============================================================================
def import_dae(dae_path):
    helper = DAE_TO_GLB
    py = _system_python()
    if not py:
        return False, "system Python with pythonnet not found (need py -3.11 on PATH)"
    base = os.path.splitext(dae_path)[0]
    glb  = base + ".glb"
    ok, msg = _run(py + [helper, dae_path, glb], "dae_to_glb")
    if not ok:
        return False, msg
    # helper may produce .gltf (text) if the Assimp build has no binary glb.
    produced = glb if os.path.isfile(glb) else (base + ".gltf"
               if os.path.isfile(base + ".gltf") else None)
    if produced is None:
        return False, "dae_to_glb produced no .glb/.gltf"

    # Read ORIGINAL bone matrices from the .gltf/.glb BEFORE Blender imports
    # (so we capture the source-file matrices, untouched by Blender's bone
    # heuristic).  See _gltf_read_arm_rests() docstring for the why.
    orig_arm_rests = _gltf_read_arm_rests(produced)

    pre = set(o.name for o in bpy.data.objects)
    try:
        bpy.ops.import_scene.gltf(filepath=produced)
    except Exception as e:
        return False, f"glTF import failed ({os.path.basename(produced)}): {e}"
    new_objs = [o for o in bpy.data.objects if o.name not in pre]

    # Strip Assimp's joint-visualizer artifacts (default-named primitive per
    # skin with no material and no parent — never part of a Splatoon model).
    removed = []
    HELPER_NAMES = ("Icosphere", "Sphere", "Cube", "Cone", "Cylinder")
    for o in list(new_objs):
        if o.type != 'MESH':
            continue
        is_helper = (
            (len(o.material_slots) == 0 and o.parent is None) or
            (o.name in HELPER_NAMES and len(o.material_slots) == 0)
        )
        if is_helper:
            nm = o.name
            new_objs.remove(o)
            bpy.data.objects.remove(o, do_unlink=True)
            removed.append(nm)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            try: bpy.data.meshes.remove(me)
            except Exception: pass

    # Restore bone names: Assimp's DAE->glTF stage prefixes them with the
    # armature name ("Armature_<bone>") which breaks .bfska binding.
    renamed = []
    for o in new_objs:
        if o.type != 'ARMATURE':
            continue
        for b in o.data.bones:
            for pref in (f"{o.name}_", "Armature_"):
                if b.name.startswith(pref) and len(b.name) > len(pref):
                    new_name = b.name[len(pref):]
                    if new_name not in o.data.bones:
                        renamed.append((b.name, new_name))
                        b.name = new_name
                    break

    # Stash ORIGINAL armature-space rest matrices on each bone so the BFSKA
    # export can undo Blender's bone-heuristic reorientation.
    stashed_total = 0
    if orig_arm_rests:
        for o in new_objs:
            if o.type == 'ARMATURE':
                stashed_total += _stash_orig_arm_rests(o, orig_arm_rests)

    # Splatoon bones use Local +X as the bone direction, but Blender draws its
    # default octahedron cone along Local +Y -> bones LOOK rotated sideways
    # even though their matrices are correct (verified by roundtrip diff:
    # RotateX diff = 0.0000 at every frame).  Switch the display to STICK
    # (thin line, no cone direction) and enable per-bone axis widget so the
    # user can still see the orientation.  Purely visual, doesn't touch data.
    for o in new_objs:
        if o.type == 'ARMATURE':
            o.data.display_type = 'STICK'
            o.data.show_axes = True

    n_arm  = sum(1 for o in new_objs if o.type == 'ARMATURE')
    n_mesh = sum(1 for o in new_objs if o.type == 'MESH')
    extra  = ""
    if removed: extra += f" (stripped helpers: {removed})"
    if renamed: extra += f" (renamed bones: {renamed})"
    if stashed_total: extra += f" (stashed orig rest on {stashed_total} bones)"
    return True, f"{os.path.basename(dae_path)}: +{n_mesh} mesh, +{n_arm} armature ({len(new_objs)} new objects){extra}"


# ============================================================================
#  BFSKA JSON -> Blender Action (pure Python, runs in Blender)
# ============================================================================
def _vec_from(s, n, default):
    if s is None: return list(default)
    if isinstance(s, str):
        parts = [float(x) for x in s.replace(",", ".").split(";") if x != ""]
    elif isinstance(s, (list, tuple)):
        parts = [float(x) for x in s]
    elif isinstance(s, dict):
        keys = ["X", "Y", "Z", "W"][:n]
        parts = [float(s.get(k, 0.0)) for k in keys]
    else:
        parts = [float(s)]
    while len(parts) < n:
        parts.append(default[len(parts)] if len(parts) < len(default) else 0.0)
    return parts[:n]


def _rot_mat(comps, is_quat, use_deg):
    if is_quat:
        x, y, z, w = comps
        return Quaternion((w, x, y, z)).to_matrix().to_4x4()
    rx, ry, rz = comps[0], comps[1], comps[2]
    if use_deg:
        rx, ry, rz = math.radians(rx), math.radians(ry), math.radians(rz)
    return Euler((rx, ry, rz), "XYZ").to_matrix().to_4x4()


def _trs(T, R, S, is_quat, use_deg):
    M  = Matrix.Translation(Vector(T))
    M  = M @ _rot_mat(R, is_quat, use_deg)
    M  = M @ Matrix.Diagonal(Vector((S[0], S[1], S[2], 1.0)))
    return M


def _make_curve_eval(keyframes, interp):
    """KeyFrames dict {frameStr:{Value, In|Out|Delta}} -> f(frame)->value.
    Cubic -> Hermite (In/Out per-frame slopes).  Linear -> {Value, Delta=segment delta}.
    Step  -> hold."""
    ks = sorted(((float(k), v) for k, v in keyframes.items()), key=lambda p: p[0])
    fr = [p[0] for p in ks]
    val = [float(p[1].get("Value", 0.0)) for p in ks]
    delta = [float(p[1].get("Delta", 0.0)) for p in ks]
    tin  = [float(p[1].get("In", 0.0)) for p in ks]
    tout = [float(p[1].get("Out", 0.0)) for p in ks]
    n = len(ks)
    ip = str(interp).lower()
    cub = ip.startswith("cubic"); step = "step" in ip; lin = ip.startswith("linear")
    def ev(f):
        if n == 0: return 0.0
        if f <= fr[0]: return val[0]
        if f >= fr[-1]: return val[-1]
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if fr[mid] <= f: lo = mid
            else: hi = mid
        f0, f1 = fr[lo], fr[hi]
        p0, p1 = val[lo], val[hi]
        df = f1 - f0
        if df <= 0: return p1
        if step: return p0
        if lin:  return p0 + delta[lo] * ((f - f0) / df)
        if cub:
            t = (f - f0) / df; m0 = tout[lo] * df; m1 = tin[hi] * df
            t2, t3 = t * t, t * t * t
            return ((2*t3 - 3*t2 + 1)*p0 + (t3 - 2*t2 + t)*m0
                    + (-2*t3 + 3*t2)*p1 + (t3 - t2)*m1)
        return p0 + (p1 - p0) * ((f - f0) / df)
    return ev


TARGETS = {
    "PositionX": ("T",0), "PositionY": ("T",1), "PositionZ": ("T",2),
    "TranslateX":("T",0), "TranslateY":("T",1), "TranslateZ":("T",2),
    "ScaleX":   ("S",0), "ScaleY":   ("S",1), "ScaleZ":   ("S",2),
    "RotateX":  ("R",0), "RotateY":  ("R",1), "RotateZ":  ("R",2),
    "RotateW":  ("R",3),
}


def apply_fska_json(json_path, armature, step=1, clear=True, action_name=None):
    """Apply a SkeletalAnimHelper JSON to armature pose bones (delta-from-BaseData)."""
    with open(json_path, "r", encoding="utf-8") as fp:
        d = json.load(fp)
    use_deg = bool(d.get("UseDegrees", True))
    is_quat = str(d.get("FlagsRotate", "EulerXYZ")).lower().startswith("quat")
    fc = int(d.get("FrameCount", 0))
    bone_anims = d.get("BoneAnims") or []
    # Stash anim-level flags on the armature data so re-export can match the
    # source's conventions (FlagsScale=Maya, FlagsRotate=EulerXYZ, ...).
    armature.data["_bfska_flags_scale"]  = str(d.get("FlagsScale", "Maya"))
    armature.data["_bfska_flags_rotate"] = str(d.get("FlagsRotate", "EulerXYZ"))
    armature.data["_bfska_loop"]         = bool(d.get("Loop", False))
    if armature.animation_data is None:
        armature.animation_data_create()
    name = action_name or str(d.get("Name") or "ImportedFSKA")
    if clear:
        act = bpy.data.actions.new(name)
    else:
        act = bpy.data.actions.get(name) or bpy.data.actions.new(name)
    armature.animation_data.action = act
    # Tolerate prefixes the glTF importer adds ("Armature_<name>" /
    # "<armatureObj>_<name>") and case-insensitive lookups.
    bones_by_name = {b.name: b for b in armature.pose.bones}
    lc_to_bone    = {b.name.lower(): b for b in armature.pose.bones}
    def find_pb(nm):
        if nm in bones_by_name:                    return bones_by_name[nm]
        for pref in (f"{armature.name}_", "Armature_"):
            cand = pref + nm
            if cand in bones_by_name:              return bones_by_name[cand]
        if nm.lower() in lc_to_bone:               return lc_to_bone[nm.lower()]
        for bn, pb in bones_by_name.items():
            if bn.endswith("_" + nm) or bn.endswith(nm):
                return pb
        return None

    matched = animated = 0
    nframes = list(range(0, fc + 1, max(1, int(step))))
    if fc not in nframes: nframes.append(fc)
    total_keys = 0
    for ba in bone_anims:
        nm = ba.get("Name")
        pb = find_pb(nm)
        if pb is None:
            continue
        matched += 1
        # Stash BFSKA per-bone metadata even if there are no curves (the rest
        # bone-anims like LOD_Root still influence the file we write out on
        # export).  Use the pose bone's underlying Bone (in armature.data) so
        # the custom property survives save/load.
        bd_in = ba.get("BaseData", {}) or {}
        pb.bone["_bfska_basedata_T"] = _vec_from(bd_in.get("Translate"), 3, (0.0, 0.0, 0.0))
        pb.bone["_bfska_basedata_R"] = _vec_from(bd_in.get("Rotate"),
                                                  4 if is_quat else 3,
                                                  (0.0, 0.0, 0.0, 1.0) if is_quat else (0.0, 0.0, 0.0))
        pb.bone["_bfska_basedata_S"] = _vec_from(bd_in.get("Scale"), 3, (1.0, 1.0, 1.0))
        pb.bone["_bfska_ssc"] = bool(ba.get("SegmentScaleCompensate", False))
        pb.bone["_bfska_use_base_T"] = bool(ba.get("UseBaseTranslation", True))
        pb.bone["_bfska_use_base_R"] = bool(ba.get("UseBaseRotation",    True))
        pb.bone["_bfska_use_base_S"] = bool(ba.get("UseBaseScale",       True))
        # Also map Blender's scale-inheritance setting to match SSC.
        # Blender 4.x: bool `use_inherit_scale` (True = inherit).
        # Blender 5.x: enum  `inherit_scale`     ('FULL' = inherit, 'NONE' = not).
        _ssc = bool(pb.bone["_bfska_ssc"])
        if hasattr(pb.bone, "inherit_scale"):           # Blender 5.x
            pb.bone.inherit_scale = 'NONE' if _ssc else 'FULL'
        elif hasattr(pb.bone, "use_inherit_scale"):     # Blender <=4.x
            pb.bone.use_inherit_scale = not _ssc
        if not ba.get("Curves"):
            continue
        animated += 1
        pb.rotation_mode = "QUATERNION"
        S0 = pb.bone["_bfska_basedata_S"]
        T0 = pb.bone["_bfska_basedata_T"]
        R0 = pb.bone["_bfska_basedata_R"]
        B = _trs(T0, R0, S0, is_quat, use_deg)
        Binv = B.inverted_safe()
        chans = {}
        for cv in ba["Curves"]:
            t = TARGETS.get(str(cv.get("Target")))
            if t is None: continue
            chans[t] = _make_curve_eval(cv.get("KeyFrames") or {},
                                        cv.get("Interpolation", "Linear"))
        for f in nframes:
            T = list(T0); S = list(S0); R = list(R0)
            for (grp, idx), ev in chans.items():
                v = ev(float(f))
                if   grp == "T": T[idx] = v
                elif grp == "S": S[idx] = v
                else:            R[idx] = v
            TRS = _trs(T, R, S, is_quat, use_deg)
            pb.matrix_basis = Binv @ TRS
            pb.keyframe_insert("location",            frame=f, group=pb.name)
            pb.keyframe_insert("rotation_quaternion", frame=f, group=pb.name)
            pb.keyframe_insert("scale",               frame=f, group=pb.name)
            total_keys += 3
    sc = bpy.context.scene
    sc.frame_start = 0
    sc.frame_end   = fc if fc > 0 else sc.frame_end
    sc.frame_set(0)
    return matched, animated, total_keys


def import_bfska(bfska_path, armature):
    helper = BFSKA_TO_JSON
    py = _system_python()
    if not py:
        return False, "system Python with pythonnet not found (need py -3.11 on PATH)"
    tmp = tempfile.NamedTemporaryFile(suffix=".fska.json", delete=False); tmp.close()
    try:
        ok, msg = _run(py + [helper, bfska_path, tmp.name], "bfska_to_json")
        if not ok: return False, msg
        m, a, k = apply_fska_json(tmp.name, armature)
        return True, f"{os.path.basename(bfska_path)}: matched={m}, animated={a}, keys={k}"
    finally:
        try: os.remove(tmp.name)
        except Exception: pass


# ============================================================================
#  Bake armature -> FSKA JSON -> .bfska (via helper)
# ============================================================================
def bake_armature_to_fska(armature, start, end, name="ExportedBFSKA", loop=False):
    scene = bpy.context.scene
    pbones = list(armature.pose.bones)
    n = len(pbones)
    samples = [{"T": [], "R": [], "S": []} for _ in range(n)]
    frames = list(range(int(start), int(end) + 1))

    # AXIS CONSISTENCY (critical):
    # The game wants bone matrices in Y-up.  Blender stores them in some
    # Z-up convention (the exact split between "arm.matrix_world" and
    # "pb.matrix" depends on the Blender version's glTF importer: 5.x puts
    # the Y-up->Z-up rotation in bone matrices, 4.1 puts it in the armature
    # object's world transform).  Both yield the same WORLD position for
    # the bone — `armature.matrix_world @ pb.matrix` is always Blender Z-up.
    # To get game Y-up: `G @ armature.matrix_world @ pb.matrix`.
    #
    # For CHILD bones, both `armature.matrix_world` and G cancel inside the
    # local-to-parent formula, so children's local matrices are independent
    # of either.  Only ROOT bones need the conversion, because their
    # local-to-parent IS their world matrix.
    #
    # Earlier versions hard-coded `G @ pb.matrix` and assumed arm.matrix_world
    # was identity — that broke on Blender 4.1 where the armature carries the
    # axis conversion, producing a spurious -90 deg X rotation in every
    # exported root bone.  Including armature.matrix_world fixes that with
    # zero effect on the Blender 5.x case (where it's identity).
    from bpy_extras.io_utils import axis_conversion
    G = axis_conversion(to_forward='-Z', to_up='Y').to_4x4()
    arm_world = armature.matrix_world.copy()

    # BONE-HEURISTIC CORRECTION:
    # If import_dae() stashed the ORIGINAL armature-space rest matrices on
    # each bone (custom prop "_bfska_orig_arm_rest"), use them to undo
    # Blender's bone reorientation on export.  Math:
    #   Blender stores A' (= reoriented arm-rest); original was A.
    #   For deformation to match in Blender: pb.matrix @ A'^-1 == W_orig @ A^-1
    #   so W_orig = pb.matrix @ A'^-1 @ A   (the bone's world matrix as the
    #   game expects to see it).  Then BFSKA local-to-parent in original
    #   convention = (W_orig_parent)^-1 @ W_orig_self.  Children cancel their
    #   parents' Blender-vs-orig delta, so the correction effectively only
    #   matters at the root + wherever the bone hierarchy is non-trivial.
    orig_rest = {}
    for pb in pbones:
        raw = pb.bone.get("_bfska_orig_arm_rest")
        if raw is None or len(raw) != 16:
            continue
        try:
            orig_rest[pb.name] = Matrix(
                ((raw[0],  raw[1],  raw[2],  raw[3]),
                 (raw[4],  raw[5],  raw[6],  raw[7]),
                 (raw[8],  raw[9],  raw[10], raw[11]),
                 (raw[12], raw[13], raw[14], raw[15])))
        except Exception:
            pass
    correct = len(orig_rest) > 0

    def _world_orig(pb):
        """Bone's WORLD matrix in the ORIGINAL convention (i.e. as the
        source file / game expects), accounting for Blender's heuristic."""
        A_blend = pb.bone.matrix_local
        A_orig  = orig_rest.get(pb.name)
        if A_orig is None:
            return pb.matrix     # no correction available for this bone
        return pb.matrix @ A_blend.inverted_safe() @ A_orig

    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for i, pb in enumerate(pbones):
            if correct:
                Wo = _world_orig(pb)
                if pb.parent:
                    Wp = _world_orig(pb.parent)
                    M = Wp.inverted_safe() @ Wo
                else:
                    # Root: convert Blender world (arm_world @ Wo) to game Y-up.
                    M = G @ arm_world @ Wo
            else:
                # Original (uncorrected) path — kept as fallback for armatures
                # not imported via this addon (no stashed rest matrices).
                if pb.parent:
                    M = pb.parent.matrix.inverted_safe() @ pb.matrix
                else:
                    M = G @ arm_world @ pb.matrix
            t, q, s = M.decompose()
            e = q.to_euler("XYZ")
            samples[i]["T"].append((t.x, t.y, t.z))
            samples[i]["R"].append((math.degrees(e.x),
                                    math.degrees(e.y),
                                    math.degrees(e.z)))
            samples[i]["S"].append((s.x, s.y, s.z))

    # Unwrap euler so consecutive frames don't have +/-180 jumps.
    for s in samples:
        for ai in range(3):
            for k in range(1, len(s["R"])):
                while s["R"][k][ai] - s["R"][k-1][ai] >  180.0:
                    s["R"][k] = tuple(s["R"][k][j] - (360.0 if j == ai else 0.0)
                                      for j in range(3))
                while s["R"][k][ai] - s["R"][k-1][ai] < -180.0:
                    s["R"][k] = tuple(s["R"][k][j] + (360.0 if j == ai else 0.0)
                                      for j in range(3))

    # BaseData should be the bone's REST pose in the BFSKA local-to-parent
    # convention, not the sampled pose at frame 0.  If we imported a .bfska
    # earlier, use the EXACT same BaseData (preserves Nintendo's R=(0,0,0) for
    # bones that just spin around X — without this, our BaseData captured
    # frame-0's spin offset, doubling the rotation in-game).  Fallback for
    # armatures NOT imported via this addon: compute REST local-to-parent
    # from `bone.matrix_local` (Blender's rest), not from frame-0 sample.
    def _rest_local_to_parent(pb):
        if correct:
            Wo = _world_orig(pb)
            if pb.parent:
                Wp = _world_orig(pb.parent)
                return Wp.inverted_safe() @ Wo
            return G @ arm_world @ Wo
        else:
            # No heuristic-correction stash; use bone.matrix_local for the
            # REST instead of pb.matrix at frame 0.
            A = pb.bone.matrix_local
            if pb.parent:
                return pb.parent.bone.matrix_local.inverted_safe() @ A
            return G @ arm_world @ A

    # EPS: tighten the "is this axis animated" check.  1e-6 lets float-decomp
    # noise create spurious RotateY/RotateZ curves; 1e-3 (~0.001° / 1mm)
    # suppresses those without losing real micro-animations.
    EPS = 1e-3

    def linear_curve(target, vals, key_type="Single"):
        kf = {}
        for i, fr in enumerate(frames):
            v = vals[i]
            d = (vals[i + 1] - v) if i + 1 < len(vals) else 0.0
            kf[str(fr - frames[0])] = {"Value": float(v), "Delta": float(d)}
        # FrameType=Byte (8-bit frame indices) matches Nintendo's encoding for
        # animations <=255 frames; some runtimes only decode the Byte variant
        # and silently drop curves stored as Single.
        ftype = "Byte" if len(frames) <= 256 else "Single"
        return {
            "Target": target, "Interpolation": "Linear",
            "FrameType": ftype, "KeyType": key_type,
            "WrapMode": "Clamp, Clamp",
            "Scale": 1.0, "Offset": 0.0,
            "KeyFrames": kf,
        }

    bone_anims = []
    for i, pb in enumerate(pbones):
        d = samples[i]
        # BaseData = REST in BFSKA local-to-parent convention.  Use stash from
        # apply_fska_json() if the user imported a .bfska first; else compute
        # from `bone.matrix_local`.  NEVER use frame-0 samples (that was the
        # rotation-doubles-in-game bug).
        stash_T = pb.bone.get("_bfska_basedata_T")
        stash_R = pb.bone.get("_bfska_basedata_R")
        stash_S = pb.bone.get("_bfska_basedata_S")
        if stash_T is not None and stash_R is not None and stash_S is not None:
            T0 = (float(stash_T[0]), float(stash_T[1]), float(stash_T[2]))
            R0 = (float(stash_R[0]), float(stash_R[1]), float(stash_R[2]))
            S0 = (float(stash_S[0]), float(stash_S[1]), float(stash_S[2]))
        else:
            M_rest = _rest_local_to_parent(pb)
            tr, qr, sr = M_rest.decompose()
            er = qr.to_euler("XYZ")
            T0 = (tr.x, tr.y, tr.z)
            R0 = (math.degrees(er.x), math.degrees(er.y), math.degrees(er.z))
            S0 = (sr.x, sr.y, sr.z)
        # Curves still come from the per-frame samples (absolute values).
        # Compare to FRAME-0 sample for "is it animated", not to BaseData —
        # otherwise a bone whose rest != frame-0 would always have curves.
        sample0 = (d["T"][0], d["R"][0], d["S"][0])
        curves = []
        for ai, ax in enumerate(("X", "Y", "Z")):
            t_vals = [t[ai] for t in d["T"]]
            r_vals = [r[ai] for r in d["R"]]
            s_vals = [s[ai] for s in d["S"]]
            if any(abs(v - sample0[0][ai]) > EPS for v in t_vals):
                curves.append(linear_curve(f"Position{ax}", t_vals))
            if any(abs(v - sample0[1][ai]) > EPS for v in r_vals):
                curves.append(linear_curve(f"Rotate{ax}",   r_vals))
            if any(abs(v - sample0[2][ai]) > EPS for v in s_vals):
                curves.append(linear_curve(f"Scale{ax}",    s_vals))
        # SegmentScaleCompensate: prefer stashed BFSKA value, else derive from
        # Blender's `use_inherit_scale` (inverted: don't-inherit == SSC).
        if "_bfska_ssc" in pb.bone:
            ssc = bool(pb.bone["_bfska_ssc"])
        else:
            # Same API split as above (Blender 5.x enum vs <=4.x bool).
            if hasattr(pb.bone, "inherit_scale"):
                ssc = (pb.bone.inherit_scale == 'NONE')
            elif hasattr(pb.bone, "use_inherit_scale"):
                ssc = not bool(pb.bone.use_inherit_scale)
            else:
                ssc = False
        use_T = bool(pb.bone.get("_bfska_use_base_T", True))
        use_R = bool(pb.bone.get("_bfska_use_base_R", True))
        use_S = bool(pb.bone.get("_bfska_use_base_S", True))
        bone_anims.append({
            "Name": pb.name,
            "SegmentScaleCompensate": ssc,
            "UseBaseTranslation": use_T,
            "UseBaseRotation":    use_R,
            "UseBaseScale":       use_S,
            "BaseData": {
                "Flags": 0,
                "Scale":     "{};{};{}".format(*S0),
                "Translate": "{};{};{}".format(*T0),
                "Rotate":    "{};{};{}".format(*R0),
            },
            "Curves": curves,
        })

    # Top-level flags: prefer stashed values (from a previously imported .bfska
    # on this armature), else default to Nintendo's typical "Maya" scale flag
    # so Toolbox/runtime applies the same transform composition as the source.
    flags_scale  = str(armature.data.get("_bfska_flags_scale",  "Maya"))
    flags_rotate = str(armature.data.get("_bfska_flags_rotate", "EulerXYZ"))
    return {
        "Name": name,
        "Path": "",
        "FrameCount": int(end - start),
        "Loop":   bool(loop),
        "Baked":  False,
        "UseDegrees": True,
        "FlagsScale":  flags_scale,
        "FlagsRotate": flags_rotate,
        "BoneAnims":   bone_anims,
        "UserData":    {},
    }


def export_bfska(out_path, armature, start, end, name=None, loop=False):
    helper = MAKE_BFSKA
    py = _system_python()
    if not py:
        return False, "system Python with pythonnet not found"
    if name is None:
        name = os.path.splitext(os.path.basename(out_path))[0]
    j = bake_armature_to_fska(armature, start, end, name=name, loop=loop)
    tmp = tempfile.NamedTemporaryFile(suffix=".fska.json", mode="w",
                                       delete=False, encoding="utf-8")
    json.dump(j, tmp); tmp.close()
    try:
        ok, msg = _run(py + [helper, tmp.name, out_path], "make_bfska")
        return ok, msg if not ok else f"{os.path.basename(out_path)} ({os.path.getsize(out_path)} bytes)"
    finally:
        try: os.remove(tmp.name)
        except Exception: pass


def find_armature():
    """Active object if armature; else first armature in scene."""
    a = bpy.context.active_object
    if a and a.type == "ARMATURE":
        return a
    for o in bpy.data.objects:
        if o.type == "ARMATURE":
            return o
    return None


# ============================================================================
#  Model export to FBX (pure Blender, size-preserving)
# ============================================================================
def export_fbx(out_fbx_path, include_armature=True, selection_only=False):
    # AXIS: bake -90 X into the DATA, then export literally (axis_up='Z', no
    # conversion).  Blender's FBX exporter applies axis conversion to object
    # transforms but NOT to bone-local rest, so an axis_up='Y' export leaves
    # bones 90 deg off ("Rotate Bones -90" Toolbox workaround).  Done on
    # TEMPORARY DUPLICATES so the real scene isn't mutated.
    R = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))

    src = (list(bpy.context.selected_objects) if selection_only
           else [o for o in bpy.data.objects if o.visible_get()])
    src_arms   = [o for o in src if o.type == 'ARMATURE'] if include_armature else []
    src_meshes = [o for o in src if o.type == 'MESH']
    if not src_meshes and not src_arms:
        return False, "nothing to export (no visible mesh/armature)"

    scene_coll = bpy.context.scene.collection
    copies, arm_map = [], {}
    try:
        for a in src_arms:
            ac = a.copy(); ac.data = a.data.copy()
            scene_coll.objects.link(ac)
            ac.data.transform(R)
            ac.matrix_world = a.matrix_world
            arm_map[a] = ac
            copies.append(ac)
        for me in src_meshes:
            mc = me.copy(); mc.data = me.data.copy()
            scene_coll.objects.link(mc)
            mc.data.transform(R)
            mc.matrix_world = me.matrix_world
            for mod in mc.modifiers:
                if mod.type == 'ARMATURE' and mod.object in arm_map:
                    mod.object = arm_map[mod.object]
            if mc.parent in arm_map:
                mc.parent = arm_map[mc.parent]
            copies.append(mc)

        for o in bpy.data.objects:
            o.select_set(False)
        for o in copies:
            o.select_set(True)
        if copies:
            bpy.context.view_layer.objects.active = copies[0]

        bpy.ops.export_scene.fbx(
            filepath=out_fbx_path,
            use_selection=True,
            object_types={'MESH', 'ARMATURE'},
            apply_unit_scale=True,
            apply_scale_options='FBX_SCALE_UNITS',
            global_scale=1.0,
            bake_space_transform=False,
            use_space_transform=False,
            axis_forward='Y', axis_up='Z',
            primary_bone_axis='Y', secondary_bone_axis='X',
            add_leaf_bones=False,
            use_armature_deform_only=False,
            bake_anim=False,
            path_mode='COPY',
            embed_textures=False,
        )
    except Exception as e:
        return False, f"Blender FBX export failed: {e}"
    finally:
        for o in list(copies):
            d = o.data
            try: bpy.data.objects.remove(o, do_unlink=True)
            except Exception: pass
            try:
                if d and d.users == 0:
                    if   isinstance(d, bpy.types.Mesh):     bpy.data.meshes.remove(d)
                    elif isinstance(d, bpy.types.Armature): bpy.data.armatures.remove(d)
            except Exception: pass

    if not os.path.isfile(out_fbx_path):
        return False, "FBX export produced no file"
    return True, f"{os.path.basename(out_fbx_path)} ({os.path.getsize(out_fbx_path)} bytes)"


# ============================================================================
#  Operators
# ============================================================================
class BFSKA_OT_ImportModel(Operator, ImportHelper):
    bl_idname = "bfska.import_model"
    bl_label  = "Splatoon Model (.dae / .glb)"
    bl_description = "Import Splatoon-3 model(s).  .dae goes through Assimp (dae_to_glb helper) since Blender 5.x dropped Collada."
    filename_ext = ".dae"
    filter_glob: StringProperty(default="*.dae;*.glb;*.gltf", options={'HIDDEN'})
    files: CollectionProperty(name="Files", type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        ok = 0; bad = []
        items = list(self.files) if self.files else [None]
        for f in items:
            p = os.path.join(self.directory, f.name) if f else self.filepath
            ext = os.path.splitext(p)[1].lower()
            try:
                if ext == ".dae":
                    success, msg = import_dae(p)
                elif ext in (".glb", ".gltf"):
                    pre = set(o.name for o in bpy.data.objects)
                    bpy.ops.import_scene.gltf(filepath=p)
                    new = [o for o in bpy.data.objects if o.name not in pre]
                    success, msg = True, f"{os.path.basename(p)}: +{len(new)} new"
                else:
                    success, msg = False, f"unsupported extension {ext}"
            except Exception as e:
                success, msg = False, str(e)
            if success:
                ok += 1
                self.report({'INFO'}, msg)
            else:
                bad.append((os.path.basename(p), msg))
        if bad:
            self.report({'ERROR'}, f"{ok} ok, {len(bad)} failed; first: {bad[0][0]}: {bad[0][1][:200]}")
        else:
            self.report({'INFO'}, f"imported {ok} model(s)")
        return {'FINISHED'} if ok else {'CANCELLED'}


class BFSKA_OT_ImportAnim(Operator, ImportHelper):
    bl_idname = "bfska.import_anim"
    bl_label  = "Splatoon Animation (.bfska)"
    bl_description = "Import Splatoon-3 .bfska skeletal animation(s) onto the active (or only) armature."
    filename_ext = ".bfska"
    filter_glob: StringProperty(default="*.bfska", options={'HIDDEN'})
    files: CollectionProperty(name="Files", type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        arm = find_armature()
        if arm is None:
            self.report({'ERROR'}, "no armature in scene — import a model first.")
            return {'CANCELLED'}
        ok = 0; bad = []
        items = list(self.files) if self.files else [None]
        for f in items:
            p = os.path.join(self.directory, f.name) if f else self.filepath
            try:
                success, msg = import_bfska(p, arm)
            except Exception as e:
                success, msg = False, str(e)
            if success:
                ok += 1; self.report({'INFO'}, msg)
            else:
                bad.append((os.path.basename(p), msg))
        if bad:
            self.report({'ERROR'}, f"{ok} ok, {len(bad)} failed; first: {bad[0][0]}: {bad[0][1][:200]}")
        else:
            self.report({'INFO'}, f"applied {ok} animation(s) onto '{arm.name}'")
        return {'FINISHED'} if ok else {'CANCELLED'}


class BFSKA_OT_ExportModel(Operator, ExportHelper):
    bl_idname = "bfska.export_model"
    bl_label  = "Splatoon Model (.fbx)"
    bl_description = ("Export the active armature + meshes to FBX with "
                      "size-preserving settings (raw Blender coords -> FBX, "
                      "no unit/scale conversion).  Splatoon bone-axis defaults.")
    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})
    selection_only: BoolProperty(name="Selection only", default=False)
    include_armature: BoolProperty(name="Include armature", default=True)

    def execute(self, context):
        ok, msg = export_fbx(self.filepath,
                             include_armature=self.include_armature,
                             selection_only=self.selection_only)
        if ok:
            self.report({'INFO'}, msg); return {'FINISHED'}
        self.report({'ERROR'}, msg[:400]); return {'CANCELLED'}


class BFSKA_OT_ExportAnim(Operator, ExportHelper):
    bl_idname = "bfska.export_anim"
    bl_label  = "Splatoon Animation (.bfska)"
    bl_description = "Bake the active armature's current Action across the scene frame range to a .bfska file."
    filename_ext = ".bfska"
    filter_glob: StringProperty(default="*.bfska", options={'HIDDEN'})
    loop: BoolProperty(name="Looping", default=False)

    def execute(self, context):
        arm = find_armature()
        if arm is None:
            self.report({'ERROR'}, "no armature in scene to export.")
            return {'CANCELLED'}
        sc = context.scene
        ok, msg = export_bfska(self.filepath, arm,
                               sc.frame_start, sc.frame_end,
                               name=os.path.splitext(os.path.basename(self.filepath))[0],
                               loop=self.loop)
        if ok:
            self.report({'INFO'}, msg or "exported")
            return {'FINISHED'}
        self.report({'ERROR'}, msg[:400])
        return {'CANCELLED'}


# ============================================================================
#  File menu integration  (the "richtigen Import & Export menüs")
# ============================================================================
def menu_func_import(self, context):
    self.layout.operator(BFSKA_OT_ImportModel.bl_idname,
                         text="Splatoon Model (.dae / .glb)")
    self.layout.operator(BFSKA_OT_ImportAnim.bl_idname,
                         text="Splatoon Animation (.bfska)")


def menu_func_export(self, context):
    self.layout.operator(BFSKA_OT_ExportModel.bl_idname,
                         text="Splatoon Model (.fbx)")
    self.layout.operator(BFSKA_OT_ExportAnim.bl_idname,
                         text="Splatoon Animation (.bfska)")


# ============================================================================
#  Registration
# ============================================================================
CLASSES = (
    BFSKA_OT_ImportModel,
    BFSKA_OT_ImportAnim,
    BFSKA_OT_ExportModel,
    BFSKA_OT_ExportAnim,
)


def register():
    for c in CLASSES:
        try: bpy.utils.register_class(c)
        except ValueError: pass   # already registered
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    try: bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    except Exception: pass
    try: bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    except Exception: pass
    for c in reversed(CLASSES):
        try: bpy.utils.unregister_class(c)
        except (RuntimeError, ValueError): pass


if __name__ == "__main__":
    register()
