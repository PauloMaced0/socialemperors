#!/usr/bin/env python3
"""Prevent one malformed legacy map object from wedging map loading forever.

The Flash client progressively creates saved map objects from an ENTER_FRAME
handler.  The original loop increments its cursor only after an object is
created successfully.  If a legacy neighbour object throws in
``CreateIsoElement`` (Arthur's large, old-format village exposes this), the
same object is retried on every frame and ``StageBlocker`` is never unlocked.

This patch removes only the object that failed to initialise, records a bounded
diagnostic through ``Tracing``, and continues with the rest of the village.
Valid objects follow the original path unchanged.  It does not alter graphics,
combat, target selection, persistence, or building behavior.
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
CLASS = "core.Base"
SCRIPT = Path("core/Base.as")
MARKER = b"socialemperors-neighbor-loading-v1"


ORIGINAL_LOOP = """         while(_loc4_ < this.buildingsAdded + _loc3_ && _loc4_ < this.buildingArray.length)
         {
            _loc5_ = Base.Iso.CreateIsoElement(this.buildingArray[_loc4_],Constants.PLAYER_UNDECIDED);
            Base.Iso.addElement(_loc5_);
            this.buildingArray[_loc4_].mc = this.buildingLayer.addChild(_loc5_) as IsoElement;
            this.buildingArray[_loc4_].mc.currentDepth = _loc4_;
            this.buildingLayer.setChildIndex(this.buildingArray[_loc4_].mc,_loc4_);
            this.setOccupiedGrid(this.buildingArray[_loc4_]);
            _loc4_++;
         }"""


PATCHED_LOOP = """         while(_loc4_ < this.buildingsAdded + _loc3_ && _loc4_ < this.buildingArray.length)
         {
            try
            {
               _loc5_ = Base.Iso.CreateIsoElement(this.buildingArray[_loc4_],Constants.PLAYER_UNDECIDED);
               Base.Iso.addElement(_loc5_);
               this.buildingArray[_loc4_].mc = this.buildingLayer.addChild(_loc5_) as IsoElement;
               this.buildingArray[_loc4_].mc.currentDepth = _loc4_;
               this.buildingLayer.setChildIndex(this.buildingArray[_loc4_].mc,_loc4_);
               this.setOccupiedGrid(this.buildingArray[_loc4_]);
               _loc4_++;
            }
            catch(error:Error)
            {
               Tracing.Trace("Skipping invalid map object while loading: " + error.message);
               this.buildingArray.splice(_loc4_,1);
            }
         }"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker in source:
        return
    count = source.count(ORIGINAL_LOOP)
    if count != 1:
        raise RuntimeError(
            f"progressive map-loader signature: expected once, found {count}"
        )
    source = source.replace(
        "   public class Base extends MovieClip\n   {\n",
        "   public class Base extends MovieClip\n"
        "   {\n"
        f'      \n      private static const NEIGHBOR_LOADING_FIX:String = "{marker}";\n',
        1,
    )
    source = source.replace(ORIGINAL_LOOP, PATCHED_LOOP, 1)
    path.write_text(source)


def uncompress(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    return uncompress(data).count(MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-neighbor-loading-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run(
            [
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                str(ffdec), "-selectclass", CLASS, "-export", "script",
                str(export_dir), str(work_swf),
            ],
            check=True,
        )
        source = export_dir / "scripts" / SCRIPT
        patch_source(source)
        one = tmp / "one-script" / SCRIPT
        one.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, one)
        subprocess.run(
            [
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                str(ffdec), "-importScript", str(work_swf), str(work_swf),
                str(tmp / "one-script"),
            ],
            check=True,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    if not patched(output.read_bytes()):
        raise RuntimeError("neighbor-loading marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
