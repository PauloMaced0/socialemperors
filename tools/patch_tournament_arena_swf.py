#!/usr/bin/env python3
"""Patch the Tournament Arena for daily rotation and fair weekly rewards.

The stock client only understands two presentation modes: always-open regular
tournaments and the single server-scheduled Weekly Gold tournament.  The local
server now returns ``tournament_daily`` for types 1-7, so this patch teaches
regular tournament cards to hide their Enter button outside their assigned day
and display ``Available on <day>`` plus the server countdown.

Weekly Gold is also first-place-only.  The server enforces that rule and this
patch changes the client-side winner threshold from the original top ten to
one so the result popup never advertises an uncredited prize.

Requires JPEXS FFDec. Example:

    python tools/patch_tournament_arena_swf.py --ffdec /path/to/ffdec.jar
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
CLASSES = ",".join((
    "managers.TournamentManager",
    "popups.tournament.PopupTournament",
    "popups.tournament.ThumbTournament",
    "popups.tournament.TournamentResultTabResults",
))
SCRIPT_FILES = (
    Path("managers/TournamentManager.as"),
    Path("popups/tournament/PopupTournament.as"),
    Path("popups/tournament/ThumbTournament.as"),
    Path("popups/tournament/TournamentResultTabResults.as"),
)
MARKER = "socialemperors-tournament-arena-v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return source.replace(old, new, 1)


def patch_sources(scripts: Path) -> None:
    manager_path = scripts / SCRIPT_FILES[0]
    manager = manager_path.read_text()
    if MARKER not in manager:
        manager = _replace_once(
            manager,
            "   public class TournamentManager\n   {\n",
            """   public class TournamentManager
   {
      
      private static const TOURNAMENT_ARENA_PATCH:String = "socialemperors-tournament-arena-v1";
""",
            "TournamentManager marker",
        )
        manager = _replace_once(
            manager,
            "      private static var MAX_PLAYERS_WEEKLY_WINNERS:int = 10;",
            "      private static var MAX_PLAYERS_WEEKLY_WINNERS:int = 1;",
            "weekly winner limit",
        )
        manager = _replace_once(
            manager,
            """      public static function getTimeWeeklyTournament(param1:String) : Number
      {
         return Number(TournamentManager.data["tournament_weekly"][param1]["timeLeft"]);
      }
""",
            """      public static function getTimeWeeklyTournament(param1:String) : Number
      {
         return Number(TournamentManager.data["tournament_weekly"][param1]["timeLeft"]);
      }
      
      public static function isDailyTournamentOpen(param1:String) : Boolean
      {
         return TournamentManager.data["tournament_daily"] != null && TournamentManager.data["tournament_daily"][param1] != null && TournamentManager.data["tournament_daily"][param1]["open"] == "1";
      }
      
      public static function getTimeDailyTournament(param1:String) : Number
      {
         return Number(TournamentManager.data["tournament_daily"][param1]["timeLeft"]);
      }
      
      public static function getDayDailyTournament(param1:String) : String
      {
         return String(TournamentManager.data["tournament_daily"][param1]["day"]);
      }
""",
            "daily schedule accessors",
        )
        manager_path.write_text(manager)

    thumb_path = scripts / SCRIPT_FILES[2]
    thumb = thumb_path.read_text()
    if MARKER not in thumb:
        thumb = _replace_once(
            thumb,
            "   import utils.TextFieldUtil;",
            "   import utils.CountDown;\n   import utils.TextFieldUtil;",
            "CountDown import",
        )
        thumb = _replace_once(
            thumb,
            "   public class ThumbTournament extends ThumbTournamentMC\n   {\n",
            """   public class ThumbTournament extends ThumbTournamentMC
   {
      
      private static const TOURNAMENT_ARENA_PATCH:String = "socialemperors-tournament-arena-v1";
""",
            "ThumbTournament marker",
        )
        thumb = _replace_once(
            thumb,
            "      private var _selectableUnits:Dictionary = new Dictionary();",
            """      private var _selectableUnits:Dictionary = new Dictionary();
      
      private var _dailyCountDown:CountDown;
""",
            "daily countdown field",
        )
        thumb = _replace_once(
            thumb,
            """      public function disableButton() : void
      {
         enterButton.buttonMode = false;
""",
            """      public function disableButton() : void
      {
         if(this._dailyCountDown != null)
         {
            this._dailyCountDown.destroy();
            this._dailyCountDown = null;
         }
         enterButton.buttonMode = false;
""",
            "countdown cleanup",
        )
        thumb = _replace_once(
            thumb,
            """      public function setImage(param1:String, param2:Boolean = false) : void
""",
            """      public function configureDailyAvailability() : void
      {
         if(this.isPrivate || TournamentManager.isDailyTournamentOpen(this.tournament_type_id))
         {
            return;
         }
         enterButton.visible = false;
         resource1_mc.visible = false;
         prize_txt.visible = false;
         reward_txt.visible = false;
         resource2_mc.visible = false;
         containerThumb.visible = false;
         bufHolder.visible = false;
         TextFieldUtil.setHTML(cost_txt,Language.getLiteral(Language.TOURNAMENT_WEEKLY_AVAILABLE) + " " + TournamentManager.getDayDailyTournament(this.tournament_type_id),TextFieldAutoSize.CENTER);
         this._dailyCountDown = new CountDown(0.5,4,170);
         this._dailyCountDown.setSeconds(TournamentManager.getTimeDailyTournament(this.tournament_type_id),true);
         addChild(this._dailyCountDown);
      }
      
      public function setImage(param1:String, param2:Boolean = false) : void
""",
            "daily card presentation",
        )
        thumb_path.write_text(thumb)

    popup_path = scripts / SCRIPT_FILES[1]
    popup = popup_path.read_text()
    if MARKER not in popup:
        popup = _replace_once(
            popup,
            "   public class PopupTournament extends PopupTournamentMC\n   {\n",
            """   public class PopupTournament extends PopupTournamentMC
   {
      
      private static const TOURNAMENT_ARENA_PATCH:String = "socialemperors-tournament-arena-v1";
""",
            "PopupTournament marker",
        )
        needle = """            _loc4_.setRewards(_loc3_["unit"],_loc3_["gold"],_loc3_["cash"]);
            _loc4_.addEventListener(ThumbTournament.JOIN_TOURNAMENT,this.onJoinTournament);
"""
        replacement = """            _loc4_.setRewards(_loc3_["unit"],_loc3_["gold"],_loc3_["cash"]);
            _loc4_.configureDailyAvailability();
            _loc4_.addEventListener(ThumbTournament.JOIN_TOURNAMENT,this.onJoinTournament);
"""
        if popup.count(needle) != 2:
            raise RuntimeError(
                "PopupTournament daily cards: expected two source matches, "
                f"found {popup.count(needle)}"
            )
        popup = popup.replace(needle, replacement)
        popup_path.write_text(popup)

    results_path = scripts / SCRIPT_FILES[3]
    results = results_path.read_text()
    if MARKER not in results:
        results = _replace_once(
            results,
            "   public class TournamentResultTabResults extends TournamentResultTabResultsMC\n   {\n",
            """   public class TournamentResultTabResults extends TournamentResultTabResultsMC
   {
      
      private static const TOURNAMENT_ARENA_PATCH:String = "socialemperors-tournament-arena-v1";
""",
            "TournamentResultTabResults marker",
        )
        results = results.replace(
            "TournamentManager.getCurrentTournamentRankingPrizePosition(",
            "TournamentManager.getCurrentTournamentRankingPrizeByPosition(",
        )
        results_path.write_text(results)


def patched(data: bytes) -> bool:
    if data[:3] == b"CWS":
        data = b"FWS" + data[3:8] + zlib.decompress(data[8:])
    # ABC string constants are shared across imported classes, so the four
    # marker fields intentionally collapse to one constant-pool entry.
    return data.count(MARKER.encode()) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    if patched(swf.read_bytes()):
        print("Tournament Arena already patched; nothing to do.")
        return
    with tempfile.TemporaryDirectory(prefix="se-tournament-arena-swf-") as raw_tmp:
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
        for index, relative in enumerate(SCRIPT_FILES):
            one = tmp / f"one-script-{index}"
            target = one / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scripts / relative, target)
            subprocess.run([
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
                "-importScript", str(work_swf), str(work_swf), str(one),
            ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)
    if not patched(output.read_bytes()):
        raise RuntimeError("patched Tournament Arena markers are missing")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
