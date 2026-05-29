#!/usr/bin/env python3
# =============================================================================
#  bfska_to_json.py  --  Splatoon-3 .bfska  ->  .fska.json
#  Drives BfresLibrary via pythonnet. DLLs are loaded from `../dlls/`
#  (relative to this script), so the addon is fully portable — no machine-
#  specific paths.
#
#    py -3.11 bfska_to_json.py  <in.bfska>  [out.fska.json]
# =============================================================================
import os, sys

HERE   = os.path.dirname(os.path.abspath(__file__))
DLLDIR = os.path.normpath(os.path.join(HERE, "..", "dlls"))

if len(sys.argv) < 2:
    sys.exit("usage: py -3.11 bfska_to_json.py <in.bfska> [out.fska.json]")
IN_BFSKA = sys.argv[1]
OUT_JSON = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(IN_BFSKA)[0] + ".fska.json"

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

# Force InvariantCulture so Syroot Vector "x;y;z" parses '.'-decimals correctly
# regardless of the host machine's locale (German locale ',' would misparse).
_inv = CultureInfo.InvariantCulture
Thread.CurrentThread.CurrentCulture = _inv
Thread.CurrentThread.CurrentUICulture = _inv
try:
    CultureInfo.DefaultThreadCurrentCulture = _inv
    CultureInfo.DefaultThreadCurrentUICulture = _inv
except Exception:
    pass

VERSION = 0x000A0000   # Splatoon 3 = BFRES v10


def make_resfile():
    rf = ResFile()
    rf.IsPlatformSwitch = True
    rf.VersionMajor = 0
    rf.VersionMajor2 = 10
    rf.VersionMinor = 0
    rf.VersionMinor2 = 0
    pi = rf.GetType().GetProperty("Version",
            BindingFlags.NonPublic | BindingFlags.Instance)
    pi.SetValue(rf, UInt32(VERSION))
    return rf


def main():
    if not os.path.isfile(IN_BFSKA):
        sys.exit(f"input not found: {IN_BFSKA}")
    print(f"[1] BfresLibrary from {DLLDIR}")
    rf = make_resfile()

    ska = SkeletalAnim()
    try:
        ska.Import(IN_BFSKA, rf)
    except NetException as e:
        sys.exit(f".NET error Import(.bfska): {e}")

    tot = sum(b.Curves.Count for b in ska.BoneAnims)
    print(f"[2] Parsed: Name={ska.Name} Frames={ska.FrameCount} Loop={ska.Loop} "
          f"Rot={ska.FlagsRotate} Scale={ska.FlagsScale} "
          f"BoneAnims={ska.BoneAnims.Count} curves={tot}")

    try:
        ska.Export(OUT_JSON, rf)
    except NetException as e:
        sys.exit(f".NET error Export(.json): {e}")

    sz = os.path.getsize(OUT_JSON)
    print(f"[3] wrote {OUT_JSON}  ({sz} bytes)")
    print("RESULT:", "PASS" if sz > 0 else "FAIL")
    sys.exit(0 if sz > 0 else 2)


if __name__ == "__main__":
    main()
