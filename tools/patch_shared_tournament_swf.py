#!/usr/bin/env python3
"""Patch the Tournament Arena lobby for shared five-player admission rooms.

The stock client assumes four players and displays a 60-second server-poll
counter as if it were the tournament wait time.  The server now has a real
24-hour admission phase, five participant slots and a fourth fallback bot.
This patch keeps the minute polling interval but displays the authoritative
admission countdown, lays out five cards, and makes the irreversible leave
rule explicit in the confirmation dialog.
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
    "popups.tournament.PopupTournamentRoom",
))
SCRIPTS = (
    Path("managers/TournamentManager.as"),
    Path("popups/tournament/PopupTournamentRoom.as"),
)
MARKER = b"socialemperors-shared-tournament-v1"
TIMER_FIT_MARKER = b"socialemperors-tournament-timer-fit-v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return source.replace(old, new, 1)


def patch_sources(root: Path) -> None:
    manager_path = root / SCRIPTS[0]
    manager = manager_path.read_text()
    marker = MARKER.decode()
    if marker not in manager:
        manager = _replace_once(
            manager,
            "   public class TournamentManager\n   {\n",
            "   public class TournamentManager\n"
            "   {\n"
            f'      \n      private static const SHARED_TOURNAMENT_PATCH:String = "{marker}";\n',
            "TournamentManager marker",
        )
        manager = _replace_once(
            manager,
            'private static var botPlayers:Array = ["10000001","10000002","10000003"];',
            'private static var botPlayers:Array = ["10000001","10000002","10000003","10000004"];',
            "fourth bot",
        )
        manager = _replace_once(
            manager,
            "var _loc1_:String = Language.getLiteral(Language.TOURNAMENT_LEAVE_CONFIRM_TEXT);",
            'var _loc1_:String = Language.getLiteral(Language.TOURNAMENT_LEAVE_CONFIRM_TEXT) + "\\n\\nYou will not be able to enter this tournament again.";',
            "irreversible leave warning",
        )
        manager = _replace_once(
            manager,
            """      public static function getDayDailyTournament(param1:String) : String
      {
         return String(TournamentManager.data["tournament_daily"][param1]["day"]);
      }
""",
            """      public static function getDayDailyTournament(param1:String) : String
      {
         return String(TournamentManager.data["tournament_daily"][param1]["day"]);
      }
      
      public static function get admissionTimeLeft() : int
      {
         try
         {
            return Math.max(0,int(TournamentManager.data["tournament"]["admission_time_left"]));
         }
         catch(error:Error)
         {
         }
         return 0;
      }
""",
            "admission countdown accessor",
        )
        manager_path.write_text(manager)

    room_path = root / SCRIPTS[1]
    room = room_path.read_text()
    if marker not in room:
        room = _replace_once(
            room,
            "   public class PopupTournamentRoom extends PopupTournamentRoomMC\n   {\n",
            "   public class PopupTournamentRoom extends PopupTournamentRoomMC\n"
            "   {\n"
            f'      \n      private static const SHARED_TOURNAMENT_PATCH:String = "{marker}";\n',
            "PopupTournamentRoom marker",
        )
        room = _replace_once(
            room,
            "private var _numPlayersNeeded:int = 4;",
            "private var _numPlayersNeeded:int = 5;",
            "five lobby slots",
        )
        room = _replace_once(
            room,
            "private var _seconds:int = 60;",
            """private var _seconds:int = 60;
      
      private var _admissionSeconds:int = 0;""",
            "admission seconds field",
        )
        room = _replace_once(
            room,
            """         this.setPlayers();
         this.refreshPlayers();""",
            """         this.setPlayers();
         this.evalStartTournament();""",
            "initial lobby evaluation",
        )
        room = _replace_once(
            room,
            """         var _loc1_:int = -330;
         var _loc2_:int = -128;
         var _loc3_:int = 17;""",
            """         var _loc1_:int = -330;
         var _loc2_:int = -128;
         var _loc3_:int = 8;""",
            "five-card spacing",
        )
        room = _replace_once(
            room,
            """            TextFieldUtil.setHTML(_loc5_.name_txt,Language.getLiteral(Language.TOURNAMENT_PLAYER,[(_loc4_ + 1).toString()]));
            _loc5_.x = _loc1_ + _loc4_ * (_loc5_.width + _loc3_);""",
            """            TextFieldUtil.setHTML(_loc5_.name_txt,Language.getLiteral(Language.TOURNAMENT_PLAYER,[(_loc4_ + 1).toString()]));
            _loc5_.scaleX = _loc5_.scaleY = 0.78;
            _loc5_.x = _loc1_ + _loc4_ * (_loc5_.width + _loc3_);""",
            "scaled player cards",
        )
        room = _replace_once(
            room,
            "while(i < 4)",
            "while(i < this._numPlayersNeeded)",
            "refresh five cards",
        )
        room = _replace_once(
            room,
            """      private function onSecond(param1:TimerEvent) : void
      {
         var _temp_1:* = this;
         --this._seconds;
         countdown_mc.time_txt.text = this._seconds.toString();
         if(this._seconds == 0)
         {
            this._countdown.stop();
            this.showSearchingWindow();
         }
      }
""",
            """      private function formatAdmissionTime(param1:int) : String
      {
         var _loc2_:int = Math.max(0,param1);
         var _loc3_:int = Math.floor(_loc2_ / 3600);
         var _loc4_:int = Math.floor(_loc2_ % 3600 / 60);
         var _loc5_:int = _loc2_ % 60;
         return (_loc3_ < 10 ? "0" : "") + _loc3_.toString() + ":" + (_loc4_ < 10 ? "0" : "") + _loc4_.toString() + ":" + (_loc5_ < 10 ? "0" : "") + _loc5_.toString();
      }
      
      private function onSecond(param1:TimerEvent) : void
      {
         var _temp_1:* = this;
         --this._seconds;
         if(this._admissionSeconds > 0)
         {
            --this._admissionSeconds;
         }
         countdown_mc.time_txt.text = this.formatAdmissionTime(this._admissionSeconds);
         if(this._seconds == 0)
         {
            this._countdown.stop();
            this.showSearchingWindow();
         }
      }
""",
            "real admission countdown",
        )
        room = _replace_once(
            room,
            """            this._seconds = this.TIME_BETWEEN_SERVER_CALLS;
            countdown_mc.time_txt.text = this._seconds.toString();
            this._countdown.reset();""",
            """            this._seconds = this.TIME_BETWEEN_SERVER_CALLS;
            this._admissionSeconds = TournamentManager.admissionTimeLeft;
            countdown_mc.time_txt.text = this.formatAdmissionTime(this._admissionSeconds);
            this._countdown.reset();""",
            "load admission deadline",
        )
        room_path.write_text(room)

    # The stock countdown field was sized for a two-digit minute poll. Keep a
    # compact hours/minutes admission clock and let the field expand so the
    # authoritative day-long timer is not visually clipped.
    room = room_path.read_text()
    timer_marker = TIMER_FIT_MARKER.decode()
    if timer_marker not in room:
        room = _replace_once(
            room,
            f'private static const SHARED_TOURNAMENT_PATCH:String = "{marker}";',
            f'private static const SHARED_TOURNAMENT_PATCH:String = "{marker}";\n'
            f'      \n      private static const TOURNAMENT_TIMER_FIT:String = "{timer_marker}";',
            "timer-fit marker",
        )
        room = _replace_once(
            room,
            """      private function formatAdmissionTime(param1:int) : String
      {
         var _loc2_:int = Math.max(0,param1);
         var _loc3_:int = Math.floor(_loc2_ / 3600);
         var _loc4_:int = Math.floor(_loc2_ % 3600 / 60);
         var _loc5_:int = _loc2_ % 60;
         return (_loc3_ < 10 ? "0" : "") + _loc3_.toString() + ":" + (_loc4_ < 10 ? "0" : "") + _loc4_.toString() + ":" + (_loc5_ < 10 ? "0" : "") + _loc5_.toString();
      }
""",
            """      private function formatAdmissionTime(param1:int) : String
      {
         var _loc2_:int = Math.max(0,param1);
         var _loc3_:int = Math.floor(_loc2_ / 3600);
         var _loc4_:int = Math.floor(_loc2_ % 3600 / 60);
         var _loc5_:int = _loc2_ % 60;
         if(_loc3_ > 0)
         {
            return _loc3_.toString() + "h " + (_loc4_ < 10 ? "0" : "") + _loc4_.toString() + "m";
         }
         return _loc4_.toString() + "m " + (_loc5_ < 10 ? "0" : "") + _loc5_.toString() + "s";
      }
""",
            "compact admission countdown",
        )
        room = _replace_once(
            room,
            """            this._admissionSeconds = TournamentManager.admissionTimeLeft;
            countdown_mc.time_txt.text = this.formatAdmissionTime(this._admissionSeconds);""",
            """            this._admissionSeconds = TournamentManager.admissionTimeLeft;
            countdown_mc.time_txt.autoSize = TextFieldAutoSize.LEFT;
            countdown_mc.time_txt.text = this.formatAdmissionTime(this._admissionSeconds);""",
            "expand admission countdown field",
        )
        room_path.write_text(room)


def uncompress(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    # Both imported classes share the marker string in the ABC constant pool.
    plain = uncompress(data)
    return plain.count(MARKER) == 1 and plain.count(TIMER_FIT_MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return
    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-shared-tournament-swf-") as raw_tmp:
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
        # FFDec is most reliable when importing one modified AS3 class at a time.
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
        raise RuntimeError("shared-tournament marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
