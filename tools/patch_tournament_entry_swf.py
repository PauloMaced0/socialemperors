#!/usr/bin/env python3
"""Fix daily-tournament bot portraits that wedge the results window.

The local daily brackets reuse the three bot profiles configured for Weekly
Gold.  The stock client nevertheless looks up each bot picture in the current
daily definition, whose ``weekly_opponent`` value is null.  That throws while
the results popup is being constructed and leaves the global Loading overlay
on screen forever.

This patch keeps the normal current-definition lookup, then safely falls back
to tournament type 8 (Weekly Gold), which owns those bot profiles.
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
CLASS = "managers.TournamentManager"
SCRIPT = Path("managers/TournamentManager.as")
MARKER = b"socialemperors-tournament-entry-v1"


ORIGINAL_LOOKUP = """      public static function getPlayerBotPicture(param1:String) : String
      {
         var _loc2_:String = "";
         if(TournamentManager.isPlayerBot(param1))
         {
            _loc2_ = TournamentManager.getTournamentDefinition(TournamentManager.currentTournamentType)["weekly_opponent"][param1]["picture"];
         }
         return _loc2_;
      }"""


PATCHED_LOOKUP = """      public static function getPlayerBotPicture(param1:String) : String
      {
         var _loc2_:String = "";
         var _loc3_:Object = null;
         if(TournamentManager.isPlayerBot(param1))
         {
            try
            {
               _loc3_ = TournamentManager.getTournamentDefinition(TournamentManager.currentTournamentType)["weekly_opponent"];
               if(_loc3_ != null && _loc3_[param1] != null)
               {
                  return String(_loc3_[param1]["picture"]);
               }
            }
            catch(error:Error)
            {
            }
            try
            {
               _loc3_ = TournamentManager.getTournamentDefinition("8")["weekly_opponent"];
               if(_loc3_ != null && _loc3_[param1] != null)
               {
                  _loc2_ = String(_loc3_[param1]["picture"]);
               }
            }
            catch(fallbackError:Error)
            {
               _loc2_ = "";
            }
         }
         return _loc2_;
      }"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker in source:
        return
    count = source.count(ORIGINAL_LOOKUP)
    if count != 1:
        raise RuntimeError(
            f"tournament bot-picture signature: expected once, found {count}"
        )
    source = source.replace(
        "   public class TournamentManager\n   {\n",
        "   public class TournamentManager\n"
        "   {\n"
        f'      \n      private static const TOURNAMENT_ENTRY_FIX:String = "{marker}";\n',
        1,
    )
    source = source.replace(ORIGINAL_LOOKUP, PATCHED_LOOKUP, 1)
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
    with tempfile.TemporaryDirectory(prefix="se-tournament-entry-swf-") as raw_tmp:
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
        raise RuntimeError("tournament-entry marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
