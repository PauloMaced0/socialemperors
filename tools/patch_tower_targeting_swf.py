#!/usr/bin/env python3
"""Fix defender towers ignoring an in-range attacking army.

The stock client uses an army bounding-box as an optimization before a PvP
defender tower scans for targets. A one-unit army (or units aligned on one
axis) produces a zero-width/height Flash Rectangle, whose ``intersects`` call
is always false. The box is also refreshed only once every 85 frames, so it
can remain behind a moving siege weapon.

Target acquisition is already range-bounded. This patch removes only that
lossy precondition and lets the existing scan decide whether a target is in
range. It also keeps an acquired target for the tower's configured full range;
the original used half range for retention and full range for reacquisition,
causing needless target churn.

Requires JPEXS FFDec. Example:

    python tools/patch_tower_targeting_swf.py --ffdec /path/to/ffdec.jar
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWF = ROOT / "assets" / "flash" / "SocialEmpires0926bsec.swf"
CLASS = "core.isoengine.IsoBuilding"
SCRIPT = Path("core/isoengine/IsoBuilding.as")
MARKER = b"socialemperors-tower-targeting-v1"


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path.name}: expected one source signature, found {count}"
        )
    path.write_text(source.replace(old, new, 1))


def patch_source(path: Path) -> None:
    replace_once(
        path,
        "   public class IsoBuilding extends IsoFightingElement\n   {\n",
        """   public class IsoBuilding extends IsoFightingElement
   {
      
      private static const TOWER_TARGETING_FIX:String = "socialemperors-tower-targeting-v1";
""",
    )
    replace_once(
        path,
        "Base.Iso.distance(this,this.TargetElement)[0] > this.iAttackRange / 2",
        "Base.Iso.distance(this,this.TargetElement)[0] > this.iAttackRange",
    )
    replace_once(
        path,
        "if(PlayerID != Constants.PLAYER_ENEMY || Base.Iso.bboxPlayers.intersectsRange(this))",
        # acquireNewTarget() already scans only iAttackRange tiles. Avoid the
        # stale/empty Rectangle precheck which can incorrectly reject the scan.
        "if(true)",
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    raw = swf.read_bytes()
    if raw[:3] == b"CWS":
        raw = b"FWS" + raw[3:8] + zlib.decompress(raw[8:])
    if raw.count(MARKER) == 1:
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-tower-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", CLASS,
            "-export", "script", str(export_dir), str(work_swf),
        ], check=True)
        source = export_dir / "scripts" / SCRIPT
        patch_source(source)
        one = tmp / "one-script" / SCRIPT
        one.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, one)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-importScript", str(work_swf), str(work_swf),
            str(tmp / "one-script"),
        ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    patched = output.read_bytes()
    if patched[:3] == b"CWS":
        patched = b"FWS" + patched[3:8] + zlib.decompress(patched[8:])
    if patched.count(MARKER) != 1:
        raise RuntimeError("tower-targeting SWF marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
