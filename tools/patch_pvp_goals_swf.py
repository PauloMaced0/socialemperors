#!/usr/bin/env python3
"""Patch two reload-state defects in the current Social Empires SWF.

* An empty PvP continent dereferenced ``continent[0]`` and left the world
  animation/loading overlay stuck forever.
* The reload fallback for Open Market considered any built Market sufficient,
  including an unstaffed social Market.

Run after ``patch_gameplay_audit_swf.py`` in the SWF patch sequence.
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
CLASSES = "managers.MissionsManager,newTowns.ExplorationsManager"
SCRIPT_FILES = (
    Path("managers/MissionsManager.as"),
    Path("newTowns/ExplorationsManager.as"),
)
MARKERS = (
    b"socialemperors-open-market-staff-goal-v3",
    b"socialemperors-empty-pvp-continent-v1",
)


def uncompressed(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:3] == b"CWS":
        return b"FWS" + raw[3:8] + zlib.decompress(raw[8:])
    return raw


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path.name}: expected one source signature, found {count}"
        )
    path.write_text(source.replace(old, new, 1))


def patch_sources(scripts: Path) -> None:
    missions = scripts / "managers" / "MissionsManager.as"
    world = scripts / "newTowns" / "ExplorationsManager.as"

    # FFDec emits an invalid pseudo-stack sequence for this harmless
    # catch-and-ignore block. Normalize it before re-importing the class.
    replace_once(
        world,
        """            catch(e:Error)
            {
               §§push(e);
               §§push(e);
               var _temp_1:* = §§pop();
               _temp_1.§§slot[1] = §§pop();
            }""",
        """            catch(e:Error)
            {
            }""",
    )

    replace_once(
        missions,
        """      private static const RELOADED_OPEN_MARKET_GOAL_FIX:String = "socialemperors-open-market-goal-v2";
""",
        """      private static const RELOADED_OPEN_MARKET_GOAL_FIX:String = "socialemperors-open-market-goal-v2";
      
      private static const OPEN_MARKET_STAFF_GOAL_FIX:String = "socialemperors-open-market-staff-goal-v3";
""",
    )
    replace_once(
        missions,
        """                  if(_loc4_[2] == Constants.SUBCATFUNC_BUILDING_MARKET && Base.Iso.eMarket != null && !Base.Main.IsBeingBuilt(Base.Iso.eMarket))
                  {
                     _loc3_ = true;
                     _loc5_ = int(_loc4_[3]);
                  }""",
        """                  if(_loc4_[2] == Constants.SUBCATFUNC_BUILDING_MARKET && Base.Iso.eMarket != null && !Base.Main.IsBeingBuilt(Base.Iso.eMarket) && Base.Iso.eMarket.buildingReference != null && Base.Iso.eMarket.buildingReference.loaded != null && Base.Iso.eMarket.buildingReference.loaded.attrs.si == null)
                  {
                     _loc3_ = true;
                     _loc5_ = int(_loc4_[3]);
                  }""",
    )

    replace_once(
        world,
        """   public class ExplorationsManager extends ExplorationWorldMC
   {
""",
        """   public class ExplorationsManager extends ExplorationWorldMC
   {
      
      private static const EMPTY_PVP_CONTINENT_FIX:String = "socialemperors-empty-pvp-continent-v1";
""",
    )
    replace_once(
        world,
        """         this.continentId = this.currentLevel = parseInt(this.continentData.continent[0].nivel);
""",
        """         if(this.continentData.continent is Array && this.continentData.continent.length > 0)
         {
            this.continentId = this.currentLevel = parseInt(this.continentData.continent[0].nivel);
         }
         else
         {
            this.continentId = this.currentLevel = parseInt(this.continentData.level_id);
            if(this.currentLevel <= 0)
            {
               this.continentId = this.currentLevel = this.myLevel > 0 ? this.myLevel : Base.Player.iLevel;
            }
         }
""",
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    current = uncompressed(swf)
    present = [current.count(marker) for marker in MARKERS]
    if present == [1, 1]:
        if output != swf:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(swf, output)
        print(f"Already patched {output}")
        return
    if any(present):
        raise RuntimeError(f"partial/duplicated patch markers: {present}")

    with tempfile.TemporaryDirectory(prefix="se-pvp-goals-swf-") as raw_tmp:
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

    raw = uncompressed(output)
    for marker in MARKERS:
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
