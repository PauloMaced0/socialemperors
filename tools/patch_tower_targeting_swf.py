#!/usr/bin/env python3
"""Fix defender towers ignoring an in-range attacking army.

The stock client uses an army bounding-box as an optimization before a PvP
defender tower scans for targets. A one-unit army (or units aligned on one
axis) produces a zero-width/height Flash Rectangle, whose ``intersects`` call
is always false. The box is also refreshed only once every 85 frames, so it
can remain behind a moving siege weapon.

Version 1 removed that lossy precondition and kept an acquired target for the
tower's configured full range. The original used half range for retention and
full range for reacquisition, causing needless target churn.

Version 2 fixes a second range mismatch. ``getNearestEnemy`` uses Euclidean
anchor-to-anchor distance to acquire a target, while both unit firing and the
tower's retention check use ``Base.Iso.distance`` (footprint-aware Chebyshev
tile distance). A cannon could consequently fire diagonally from its range 11
while a Tower V with range 12 never acquired it. Towers now scan the fighting-
element list with the same distance function they use to retain/fire.

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
OLD_MARKER = b"socialemperors-tower-targeting-v1"
MARKER = b"socialemperors-tower-targeting-v2"

TARGET_HELPER = """      private function acquireTowerTarget(param1:int, param2:Boolean = false) : void
      {
         var _loc3_:Array = null;
         var _loc4_:IsoFightingElement = null;
         var _loc5_:IsoFightingElement = null;
         var _loc6_:int = 0;
         var _loc7_:int = 0;
         var _loc8_:int = 2147483647;
         if(this.PlayerID == Constants.PLAYER_SELF)
         {
            _loc3_ = Base.Iso.arEnemyIsoFightingElements;
            _loc6_ = int(Constants.PLAYER_ENEMY);
         }
         else
         {
            _loc3_ = Base.Iso.arPlayerIsoFightingElements;
            _loc6_ = int(Constants.PLAYER_SELF);
         }
         for each(_loc4_ in _loc3_)
         {
            if(_loc4_ != null && _loc4_.buildingReference != null && Base.Iso.fCheckNearestTarget(_loc4_,_loc6_) && (!param2 || !(_loc4_ is IsoUnit) || !IsoUnit(_loc4_).Incapacitated))
            {
               _loc7_ = int(Base.Iso.distance(this,_loc4_)[0]);
               if(_loc7_ <= param1 && _loc7_ < _loc8_)
               {
                  _loc5_ = _loc4_;
                  _loc8_ = _loc7_;
               }
            }
         }
         this.TargetElement = _loc5_;
      }

"""

V1_ACQUISITION = """               if((this.TargetElement == null || TargetElement.bDead) && !this.isSorroundedByTowers())
               {
                  if(this.buildingReference.building.id == Constants.ID_BUILDING_TOWER_ICE)
                  {
                     acquireNewTargetNotIncapacitated(_loc3_,_loc4_,iAttackRange);
                  }
                  else
                  {
                     acquireNewTarget(_loc3_,_loc4_,this.iAttackRange);
                  }
               }
"""

STOCK_ACQUISITION = """               if((this.TargetElement == null || TargetElement.bDead) && !this.isSorroundedByTowers())
               {
                  if(PlayerID != Constants.PLAYER_ENEMY || Base.Iso.bboxPlayers.intersectsRange(this))
                  {
                     if(this.buildingReference.building.id == Constants.ID_BUILDING_TOWER_ICE)
                     {
                        acquireNewTargetNotIncapacitated(_loc3_,_loc4_,iAttackRange);
                     }
                     else
                     {
                        acquireNewTarget(_loc3_,_loc4_,this.iAttackRange);
                     }
                  }
               }
"""

V2_ACQUISITION = """               if((this.TargetElement == null || TargetElement.bDead) && !this.isSorroundedByTowers())
               {
                  this.acquireTowerTarget(this.iAttackRange,this.buildingReference.building.id == Constants.ID_BUILDING_TOWER_ICE);
               }
"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    if MARKER.decode() in source:
        return

    if OLD_MARKER.decode() in source:
        source = source.replace(OLD_MARKER.decode(), MARKER.decode(), 1)
    else:
        class_signature = (
            "   public class IsoBuilding extends IsoFightingElement\n   {\n"
        )
        if source.count(class_signature) != 1:
            raise RuntimeError("IsoBuilding.as: class signature not found")
        source = source.replace(
            class_signature,
            """   public class IsoBuilding extends IsoFightingElement
   {
      
      private static const TOWER_TARGETING_FIX:String = "socialemperors-tower-targeting-v2";
""",
            1,
        )
        old_retention = (
            "Base.Iso.distance(this,this.TargetElement)[0] > "
            "this.iAttackRange / 2"
        )
        if source.count(old_retention) != 1:
            raise RuntimeError("IsoBuilding.as: half-range signature not found")
        source = source.replace(
            old_retention,
            "Base.Iso.distance(this,this.TargetElement)[0] > this.iAttackRange",
            1,
        )

    update_signature = "      override public function Update(param1:uint) : void\n"
    if source.count(update_signature) != 1:
        raise RuntimeError("IsoBuilding.as: Update signature not found")
    source = source.replace(update_signature, TARGET_HELPER + update_signature, 1)

    if source.count(V1_ACQUISITION) == 1:
        source = source.replace(V1_ACQUISITION, V2_ACQUISITION, 1)
    elif source.count(STOCK_ACQUISITION) == 1:
        source = source.replace(STOCK_ACQUISITION, V2_ACQUISITION, 1)
    else:
        raise RuntimeError("IsoBuilding.as: tower acquisition signature not found")

    path.write_text(source)


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
