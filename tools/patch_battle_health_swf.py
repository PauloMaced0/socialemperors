#!/usr/bin/env python3
"""Persist surviving PvP unit health when returning to the home village.

The stock Assault result contains casualty counts but no survivor HP. Battles
run on temporary clones, so returning home implicitly restores every survivor
to full health. This patch adds compact ``[unit_id, hp]`` rows for both armies;
the server maps those rows back to the persisted living units after applying
casualties.
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
CLASS = "battle.Assault"
SCRIPT = Path("battle/Assault.as")
MARKER = b"socialemperors-pvp-survivor-health-v1"


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
        "   import core.isoengine.IsoFightingElement;",
        "   import core.isoengine.IsoFightingElement;\n   import core.isoengine.IsoUnit;",
    )
    replace_once(
        path,
        "   public class Assault\n   {\n",
        """   public class Assault
   {
      
      private static const PVP_SURVIVOR_HEALTH_FIX:String = "socialemperors-pvp-survivor-health-v1";
""",
    )
    replace_once(
        path,
        "      public static function sendAssaultResults(param1:int, param2:Boolean = false) : void\n",
        """      public static function survivorHealth(param1:Array) : Array
      {
         var _loc2_:Array = new Array();
         var _loc3_:IsoFightingElement = null;
         if(param1 != null)
         {
            for each(_loc3_ in param1)
            {
               if(_loc3_ is IsoUnit && !_loc3_.bDead && _loc3_.buildingReference != null && _loc3_.iHealth > 0)
               {
                  _loc2_.push([_loc3_.buildingReference.building.id,_loc3_.iHealth]);
               }
            }
         }
         return _loc2_;
      }
      
      public static function sendAssaultResults(param1:int, param2:Boolean = false) : void
""",
    )
    replace_once(
        path,
        """               "attacker_units":arPlayerUnits,
               "victim_units":arEnemyUnits,
""",
        """               "attacker_units":arPlayerUnits,
               "victim_units":arEnemyUnits,
               "attacker_health":survivorHealth(Base.Iso.arPlayerIsoFightingElements),
               "victim_health":survivorHealth(Base.Iso.arEnemyIsoFightingElements),
""",
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
    with tempfile.TemporaryDirectory(prefix="se-battle-health-swf-") as raw_tmp:
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
        raise RuntimeError("PvP survivor-health marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
