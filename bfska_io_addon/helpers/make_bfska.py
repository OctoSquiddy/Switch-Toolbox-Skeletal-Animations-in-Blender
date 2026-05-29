#!/usr/bin/env python3
# =============================================================================
#  make_bfska.py  --  JSON (SkeletalAnimHelper schema)  ->  standalone .bfska
#  Drives BfresLibrary via pythonnet. DLLs are loaded from `../dlls/`
#  (relative to this script), so the addon is fully portable.
#
#    py -3.11 make_bfska.py  [in.fska.json]  [out.bfska]
# =============================================================================
import os, sys

HERE   = os.path.dirname(os.path.abspath(__file__))
DLLDIR = os.path.normpath(os.path.join(HERE, "..", "dlls"))

IN_JSON  = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "EnergyOrb.fska.json")
OUT_BFSKA = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "EnergyOrb.bfska")
VERIFY_JSON = os.path.splitext(OUT_BFSKA)[0] + ".verify.fska.json"

if not os.path.isdir(DLLDIR):
    sys.exit(f"bundled DLL dir not found: {DLLDIR}\n"
             f"(expected ../dlls/ next to this script)")

from pythonnet import load
load("netfx")
import clr

sys.path.append(DLLDIR)
os.environ["PATH"] = DLLDIR + os.pathsep + os.environ.get("PATH", "")
for asm in ("Newtonsoft.Json", "Syroot.BinaryData", "Syroot.Maths",
            "Syroot.NintenTools.NSW.Bntx", "BfresLibrary"):
    clr.AddReference(asm)

from BfresLibrary import ResFile, SkeletalAnim
from System import Exception as NetException, UInt32
from System.Reflection import BindingFlags
from System.Globalization import CultureInfo
from System.Threading import Thread

# CRITICAL: force InvariantCulture so Syroot Vector "x;y;z" string parsing
# uses '.'-decimals.  German (',' decimal) locale would misparse '0.0215' as
# 2.15e16 -> bones to infinity -> model despawns in Toolbox.
_inv = CultureInfo.InvariantCulture
Thread.CurrentThread.CurrentCulture = _inv
Thread.CurrentThread.CurrentUICulture = _inv
try:
    CultureInfo.DefaultThreadCurrentCulture = _inv
    CultureInfo.DefaultThreadCurrentUICulture = _inv
except Exception:
    pass

VERSION = 0x000A0000


def make_resfile():
    rf = ResFile()
    rf.IsPlatformSwitch = True
    rf.VersionMajor   = 0
    rf.VersionMajor2  = 10
    rf.VersionMinor   = 0
    rf.VersionMinor2  = 0
    pi = rf.GetType().GetProperty("Version",
            BindingFlags.NonPublic | BindingFlags.Instance)
    pi.SetValue(rf, UInt32(VERSION))
    return rf


def main():
    if not os.path.isfile(IN_JSON):
        sys.exit(f"input json not found: {IN_JSON}")
    print(f"[1] BfresLibrary loaded from {DLLDIR}")

    rf = make_resfile()
    print(f"[2] ResFile: Switch={rf.IsPlatformSwitch} "
          f"ver={rf.VersionMajor}.{rf.VersionMajor2}.{rf.VersionMinor}.{rf.VersionMinor2}")

    ska = SkeletalAnim()
    try:
        ska.Import(IN_JSON, rf)
    except NetException as e:
        sys.exit(f".NET error in Import(json): {e}")

    # CRITICAL: BindIndices = 0xFFFF (resolve by bone NAME at apply time)
    # so the BoneAnims aren't all bound to skeleton bone 0.
    from System import Array, UInt16
    n = ska.BoneAnims.Count
    bi = Array.CreateInstance(UInt16, n)
    for i in range(n):
        bi[i] = UInt16(0xFFFF)
    ska.BindIndices = bi
    print(f"[3] Imported JSON: Name={ska.Name} Frames={ska.FrameCount} "
          f"Bones={ska.BoneAnims.Count} Loop={ska.Loop} "
          f"Rot={ska.FlagsRotate} Scale={ska.FlagsScale} "
          f"curves={sum(b.Curves.Count for b in ska.BoneAnims)} "
          f"BindIndices=0xFFFF(by-name)")

    try:
        ska.Export(OUT_BFSKA, rf)
    except NetException as e:
        sys.exit(f".NET error in Export(bfska): {e}")
    sz = os.path.getsize(OUT_BFSKA)
    with open(OUT_BFSKA, "rb") as fp:
        head = fp.read(0x34)
    magic_ok = head[:8] == b"\x7ffresSUB" and head[0x0C:0x10] == b"FSKA" and head[0x30:0x34] == b"FSKA"
    print(f"[4] Wrote {OUT_BFSKA}  ({sz} bytes)  header_ok={magic_ok}")

    rf2 = make_resfile()
    chk = SkeletalAnim()
    try:
        chk.Import(OUT_BFSKA, rf2)
    except NetException as e:
        sys.exit(f".NET error re-importing our .bfska: {e}")
    chk.Export(VERIFY_JSON, rf2)
    ok = (chk.FrameCount == ska.FrameCount
          and chk.BoneAnims.Count == ska.BoneAnims.Count
          and str(chk.FlagsRotate) == str(ska.FlagsRotate)
          and str(chk.FlagsScale) == str(ska.FlagsScale)
          and chk.Loop == ska.Loop)
    LIMIT = 1.0e6
    bad = []
    for b in chk.BoneAnims:
        bd = b.BaseData
        for nm, v in (("S", bd.Scale), ("T", bd.Translate)):
            for comp in ("X", "Y", "Z"):
                val = getattr(v, comp)
                if abs(val) > LIMIT or val != val:
                    bad.append(f"{b.Name}.BaseData.{nm}.{comp}={val:g}")
        for c in b.Curves:
            if c.Keys is not None:
                d0, d1 = c.Keys.GetLength(0), c.Keys.GetLength(1)
                for ii in range(d0):
                    for jj in range(d1):
                        kv = c.Keys[ii, jj]
                        if abs(kv) > LIMIT or kv != kv:
                            bad.append(f"{b.Name} curve@{hex(c.AnimDataOffset)} key[{ii},{jj}]={kv:g}")
                            break
                    else:
                        continue
                    break
    sane = len(bad) == 0
    print(f"[5] Round-trip re-import: Frames={chk.FrameCount} "
          f"Bones={chk.BoneAnims.Count} Loop={chk.Loop} "
          f"Rot={chk.FlagsRotate} Scale={chk.FlagsScale} "
          f"curves={sum(b.Curves.Count for b in chk.BoneAnims)}")
    print(f"[6] Value sanity (|v|<={LIMIT:g}): {'OK' if sane else 'FAIL'}"
          + ("" if sane else f"  -> {bad[:6]}{' ...' if len(bad)>6 else ''}"))
    passed = magic_ok and ok and sane
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 2)


if __name__ == "__main__":
    main()
