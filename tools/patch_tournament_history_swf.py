#!/usr/bin/env python3
"""Add recent-winner history and centered availability labels to the arena."""
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
))
SCRIPTS = (
    Path("managers/TournamentManager.as"),
    Path("popups/tournament/PopupTournament.as"),
    Path("popups/tournament/ThumbTournament.as"),
)
MARKER = b"socialemperors-tournament-history-v1"
LAYOUT_MARKER = b"socialemperors-tournament-history-layout-v2"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return source.replace(old, new, 1)


def patch_sources(root: Path) -> None:
    marker = MARKER.decode()
    manager_path = root / SCRIPTS[0]
    manager = manager_path.read_text()
    if marker not in manager:
        manager = _replace_once(
            manager,
            'private static const SHARED_TOURNAMENT_PATCH:String = "socialemperors-shared-tournament-v1";',
            'private static const SHARED_TOURNAMENT_PATCH:String = "socialemperors-shared-tournament-v1";\n'
            f'      \n      private static const TOURNAMENT_HISTORY_PATCH:String = "{marker}";',
            "manager marker",
        )
        manager = _replace_once(
            manager,
            """      public static function getDayDailyTournament(param1:String) : String
      {
         return String(TournamentManager.data[\"tournament_daily\"][param1][\"day\"]);
      }
""",
            """      public static function getDayDailyTournament(param1:String) : String
      {
         return String(TournamentManager.data[\"tournament_daily\"][param1][\"day\"]);
      }
      
      public static function get winnerHistory() : Array
      {
         if(TournamentManager.data != null && TournamentManager.data[\"tournament_winner_history\"] is Array)
         {
            return TournamentManager.data[\"tournament_winner_history\"] as Array;
         }
         return [];
      }
""",
            "winner history accessor",
        )
        manager_path.write_text(manager)

    popup_path = root / SCRIPTS[1]
    popup = popup_path.read_text()
    if "_winnerHistoryText" not in popup:
        popup = _replace_once(
            popup,
            "      private var _currentPage:int = 1;",
            """      private var _currentPage:int = 1;
      
      private var _winnerHistoryText:TextField;""",
            "history text field",
        )
        popup = _replace_once(
            popup,
            """         this.drawThumbsTournament();
         this.setNavigation();
         addEventListener(Event.ADDED_TO_STAGE,this.onStage);""",
            """         this.drawThumbsTournament();
         this.setNavigation();
         this.drawWinnerHistory();
         addEventListener(Event.ADDED_TO_STAGE,this.onStage);""",
            "draw history",
        )
        popup = _replace_once(
            popup,
            """      private function onStage(param1:Event) : void
""",
            """      private function drawWinnerHistory() : void
      {
         var _loc1_:Array = TournamentManager.winnerHistory;
         var _loc2_:Array = [];
         var _loc3_:int = 0;
         var _loc4_:Object = null;
         while(_loc3_ < _loc1_.length && _loc3_ < 3)
         {
            _loc4_ = _loc1_[_loc3_];
            _loc2_.push(String(_loc4_[\"winner_name\"]) + \" - \" + String(_loc4_[\"tournament_name\"]));
            _loc3_++;
         }
         this._winnerHistoryText = new TextField();
         this._winnerHistoryText.x = -292;
         this._winnerHistoryText.y = 88;
         this._winnerHistoryText.width = 584;
         this._winnerHistoryText.height = 32;
         this._winnerHistoryText.selectable = false;
         this._winnerHistoryText.mouseEnabled = false;
         this._winnerHistoryText.multiline = true;
         this._winnerHistoryText.wordWrap = true;
         this._winnerHistoryText.defaultTextFormat = new TextFormat(\"_sans\",11,4925969,true,null,null,null,null,TextFormatAlign.CENTER);
         this._winnerHistoryText.text = \"Recent winners: \" + (_loc2_.length > 0 ? _loc2_.join(\"  |  \") : \"No completed tournaments yet\");
         addChild(this._winnerHistoryText);
      }
      
      private function onStage(param1:Event) : void
""",
            "history renderer",
        )
        popup_path.write_text(popup)

    popup = popup_path.read_text()
    layout_marker = LAYOUT_MARKER.decode()
    if layout_marker not in popup:
        popup = _replace_once(
            popup,
            "   public class PopupTournament extends PopupTournamentMC\n   {\n",
            "   public class PopupTournament extends PopupTournamentMC\n"
            "   {\n"
            f'      \n      private static const TOURNAMENT_HISTORY_UI:String = "{marker}";\n'
            f'      \n      private static const TOURNAMENT_HISTORY_LAYOUT:String = "{layout_marker}";\n',
            "popup layout marker",
        )
        popup = _replace_once(
            popup,
            "this._winnerHistoryText.y = 88;",
            "this._winnerHistoryText.y = 63;",
            "history vertical position",
        )
        popup = _replace_once(
            popup,
            "this._winnerHistoryText.height = 32;",
            "this._winnerHistoryText.height = 22;",
            "history field height",
        )
        popup = _replace_once(
            popup,
            "this._winnerHistoryText.multiline = true;",
            "this._winnerHistoryText.multiline = false;",
            "history single line",
        )
        popup = _replace_once(
            popup,
            "this._winnerHistoryText.wordWrap = true;",
            "this._winnerHistoryText.wordWrap = false;",
            "history no wrapping",
        )
        popup_path.write_text(popup)

    thumb_path = root / SCRIPTS[2]
    thumb = thumb_path.read_text()
    if "cost_txt.width = 180;" not in thumb:
        thumb = _replace_once(
            thumb,
            "   import flash.text.TextFieldAutoSize;",
            "   import flash.text.TextFieldAutoSize;\n   import flash.text.TextFormat;\n   import flash.text.TextFormatAlign;",
            "text alignment imports",
        )
        thumb = _replace_once(
            thumb,
            """      public function configureDailyAvailability() : void
      {
         if(this.isPrivate || TournamentManager.isDailyTournamentOpen(this.tournament_type_id))""",
            """      public function configureDailyAvailability() : void
      {
         var _loc1_:TextFormat = null;
         if(this.isPrivate || TournamentManager.isDailyTournamentOpen(this.tournament_type_id))""",
            "availability format local",
        )
        thumb = _replace_once(
            thumb,
            """         TextFieldUtil.setHTML(cost_txt,Language.getLiteral(Language.TOURNAMENT_WEEKLY_AVAILABLE) + \" \" + TournamentManager.getDayDailyTournament(this.tournament_type_id),TextFieldAutoSize.CENTER);
         this._dailyCountDown = new CountDown(0.5,4,170);""",
            """         TextFieldUtil.setHTML(cost_txt,Language.getLiteral(Language.TOURNAMENT_WEEKLY_AVAILABLE) + \" \" + TournamentManager.getDayDailyTournament(this.tournament_type_id),TextFieldAutoSize.NONE);
         cost_txt.autoSize = TextFieldAutoSize.NONE;
         cost_txt.x = 0;
         cost_txt.width = 180;
         _loc1_ = cost_txt.getTextFormat();
         _loc1_.align = TextFormatAlign.CENTER;
         cost_txt.defaultTextFormat = _loc1_;
         cost_txt.setTextFormat(_loc1_);
         this._dailyCountDown = new CountDown(0.5,4,170);""",
            "center availability label",
        )
        thumb_path.write_text(thumb)


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    # All imported classes share this ABC string in the constant pool.
    plain = _plain(data)
    return (
        plain.count(MARKER) == 1
        and plain.count(LAYOUT_MARKER) == 1
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return
    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-tournament-history-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export = tmp / "export"
        work = tmp / swf.name
        shutil.copy2(swf, work)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", CLASSES, "-export", "script", str(export),
            str(work),
        ], check=True)
        patch_sources(export / "scripts")
        for index, relative in enumerate(SCRIPTS):
            one = tmp / f"one-script-{index}"
            target = one / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(export / "scripts" / relative, target)
            subprocess.run([
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
                "-importScript", str(work), str(work), str(one),
            ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work, output)
    if not patched(output.read_bytes()):
        raise RuntimeError("tournament history patch marker is missing")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
