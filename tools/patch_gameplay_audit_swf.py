#!/usr/bin/env python3
"""Patch the remaining store-limit and mission-progress gameplay bugs.

The server treats ``units_limit`` as a limit for a whole upgrade family. The
stock Flash client counted only the exact tier, so Mine II did not visually
block placing Mine I even though the authoritative server rejected it.

The Open Market goal also depended only on a transient "building opened"
event. If an already-open market was restored during browser reload, the event
had already been missed and the goal stayed at 0/1.
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
CLASSES = "core.Base,managers.MissionsManager"
SCRIPT_FILES = (
    Path("core/Base.as"),
    Path("managers/MissionsManager.as"),
)


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path.name}: expected one source signature, found {count}"
        )
    path.write_text(source.replace(old, new, 1))


def patch_sources(scripts: Path) -> None:
    base = scripts / "core" / "Base.as"
    missions = scripts / "managers" / "MissionsManager.as"

    replace_once(
        base,
        """   public class Base extends MovieClip
   {
""",
        """   public class Base extends MovieClip
   {
      
      private static const BUILDING_FAMILY_LIMIT_FIX:String = "socialemperors-building-family-limit-v1";
""",
    )
    replace_once(
        base,
        """      public function checkBuildingConstructionLimit(param1:Object, param2:Boolean = true) : Boolean
      {""",
        """      private function sameUpgradeFamily(param1:int, param2:int) : Boolean
      {
         var _loc3_:StaticData = null;
         var _loc4_:int = param1;
         var _loc5_:int = 0;
         while(_loc4_ > 0 && _loc5_ < 16)
         {
            if(_loc4_ == param2)
            {
               return true;
            }
            _loc3_ = StaticDataLibrary.api.getItem(_loc4_);
            if(_loc3_ == null || _loc3_.upgrades_to <= 0)
            {
               break;
            }
            _loc4_ = _loc3_.upgrades_to;
            _loc5_++;
         }
         _loc4_ = param2;
         _loc5_ = 0;
         while(_loc4_ > 0 && _loc5_ < 16)
         {
            if(_loc4_ == param1)
            {
               return true;
            }
            _loc3_ = StaticDataLibrary.api.getItem(_loc4_);
            if(_loc3_ == null || _loc3_.upgrades_to <= 0)
            {
               break;
            }
            _loc4_ = _loc3_.upgrades_to;
            _loc5_++;
         }
         return false;
      }
      
      public function checkBuildingConstructionLimit(param1:Object, param2:Boolean = true) : Boolean
      {""",
    )
    replace_once(
        base,
        """            if(this.buildingArray[_loc3_].building.id == param1.id)
            {
               _loc5_++;
            }""",
        """            if(this.sameUpgradeFamily(this.buildingArray[_loc3_].building.id,param1.id))
            {
               _loc5_++;
            }""",
    )
    replace_once(
        base,
        """         if(param1.subcat_functional == Constants.SUBCATFUNC_BUILDING_MARKET && _loc6_ >= 1)
         {""",
        """         if(param1.units_limit > 0 && _loc5_ >= param1.units_limit)
         {
            if(param2)
            {
               Base.PopUp.showPoupMaxConstructions(param1,false);
            }
            return true;
         }
         if(param1.subcat_functional == Constants.SUBCATFUNC_BUILDING_MARKET && _loc6_ >= 1)
         {""",
    )

    replace_once(
        missions,
        """      private static const GAMEPLAY_MISSION_PROGRESS_FIX:String = "socialemperors-mission-progress-v1";
""",
        """      private static const GAMEPLAY_MISSION_PROGRESS_FIX:String = "socialemperors-mission-progress-v1";
      
      private static const RELOADED_OPEN_MARKET_GOAL_FIX:String = "socialemperors-open-market-goal-v2";
""",
    )
    replace_once(
        missions,
        """               case Constants.MISSION_COMPLETE_SOCIAL_SUBCAT:
                  _loc5_ = this.socialSubcatFuncCompleted[_loc4_[2]] == null ? 0 : int(this.socialSubcatFuncCompleted[_loc4_[2]]);
                  _loc3_ = _loc5_ >= _loc4_[3];
                  this.arPercentatge[_loc2_] = [_loc5_,_loc4_[3]];
                  break;""",
        """               case Constants.MISSION_COMPLETE_SOCIAL_SUBCAT:
                  _loc5_ = this.socialSubcatFuncCompleted[_loc4_[2]] == null ? 0 : int(this.socialSubcatFuncCompleted[_loc4_[2]]);
                  _loc3_ = _loc5_ >= _loc4_[3];
                  if(_loc4_[2] == Constants.SUBCATFUNC_BUILDING_MARKET && Base.Iso.eMarket != null && !Base.Main.IsBeingBuilt(Base.Iso.eMarket))
                  {
                     _loc3_ = true;
                     _loc5_ = _loc4_[3];
                  }
                  this.arPercentatge[_loc2_] = [_loc5_,_loc4_[3]];
                  break;""",
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-audit-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", CLASSES,
            "-export", "script", str(export_dir), str(work_swf),
        ], check=True)
        scripts = export_dir / "scripts"
        patch_sources(scripts)
        for relative in SCRIPT_FILES:
            one = tmp / "one-script"
            if one.exists():
                shutil.rmtree(one)
            target = one / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scripts / relative, target)
            subprocess.run([
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
                "-importScript", str(work_swf), str(work_swf), str(one),
            ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    raw = output.read_bytes()
    if raw[:3] == b"CWS":
        raw = b"FWS" + raw[3:8] + zlib.decompress(raw[8:])
    for marker in (
        b"socialemperors-building-family-limit-v1",
        b"socialemperors-open-market-goal-v2",
    ):
        if raw.count(marker) != 1:
            raise RuntimeError(
                f"patched SWF marker missing or duplicated: {marker!r}"
            )
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
