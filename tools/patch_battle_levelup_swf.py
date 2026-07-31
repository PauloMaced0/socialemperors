#!/usr/bin/env python3
"""Restore level-up popups after PvP and quest rewards.

Battle rewards are persisted by ``end_attack``/``end_quest``, but the stock
client never applies them to its live ``PlayerStatus`` before loading the home
map. The home response already contains the resulting level, so the normal
old-level -> new-level transition (and its unlock popup) is skipped.

This patch applies the displayed battle reward to the live player exactly once
after the result has been saved. It lets ``checkForLevelUp`` show every crossed
level, then continues the original return-home flow. The server remains
authoritative; loading home replaces these temporary client totals with the
persisted values.
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
CLASSES = (
    "managers.PlayerStatus,popups.PopupAttackFinished,"
    "popups.PopupQuestFinished"
)
SCRIPT_FILES = (
    Path("managers/PlayerStatus.as"),
    Path("popups/PopupAttackFinished.as"),
    Path("popups/PopupQuestFinished.as"),
)
MARKERS = (
    b"socialemperors-battle-levelup-flow-v1",
    b"socialemperors-pvp-levelup-return-v1",
    b"socialemperors-quest-levelup-return-v1",
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
    player = scripts / "managers" / "PlayerStatus.as"
    attack = scripts / "popups" / "PopupAttackFinished.as"
    quest = scripts / "popups" / "PopupQuestFinished.as"

    replace_once(
        player,
        """   public class PlayerStatus implements IEventDispatcher
   {
""",
        """   public class PlayerStatus implements IEventDispatcher
   {
      
      private static const BATTLE_LEVELUP_FLOW_FIX:String = "socialemperors-battle-levelup-flow-v1";
      
      private var battleRewardComplete:Function;
""",
    )
    replace_once(
        player,
        """      public function checkForLevelUp(param1:Boolean = true) : *
      {""",
        """      public function applyBattleRewards(param1:int, param2:int, param3:Function) : void
      {
         this.battleRewardComplete = param3;
         this.adjustStats(param1,0,param2,false);
         this.checkForLevelUp(false);
      }
      
      public function checkForLevelUp(param1:Boolean = true) : *
      {""",
    )
    replace_once(
        player,
        """         else if(Base.PopUp.alertWindow == null && Base.PopUp.confirmWindow == null)
         {
            if(alreadyLeveledUp)
            {
               MapInitializer.checkSiegeHeavyPopup();
            }
         }""",
        """         else if(Base.PopUp.alertWindow == null && Base.PopUp.confirmWindow == null)
         {
            if(alreadyLeveledUp)
            {
               MapInitializer.checkSiegeHeavyPopup();
            }
            if(this.battleRewardComplete != null)
            {
               var _loc3_:Function = this.battleRewardComplete;
               this.battleRewardComplete = null;
               _loc3_();
            }
         }""",
    )

    replace_once(
        attack,
        """   public class PopupAttackFinished extends PopupAttackFinishedMC
   {
""",
        """   public class PopupAttackFinished extends PopupAttackFinishedMC
   {
      
      private static const PVP_LEVELUP_RETURN_FIX:String = "socialemperors-pvp-levelup-return-v1";
""",
    )
    replace_once(
        attack,
        """      public function message1OK(param1:Event) : void
      {
         if(Config.FORCE_RELOAD_ON_ATTACK_END)
         {
            Base.Commands.reloadOnEndAttack();
         }
         else
         {
            this.closeWindow();
            Base.Gui.returnHomeUser();
            Base.Gui.showWorld(null);
         }
      }""",
        """      private function returnHomeAfterBattleReward() : void
      {
         Base.Gui.returnHomeUser();
         Base.Gui.showWorld(null);
      }
      
      public function message1OK(param1:Event) : void
      {
         if(Config.FORCE_RELOAD_ON_ATTACK_END)
         {
            Base.Commands.reloadOnEndAttack();
         }
         else
         {
            this.closeWindow();
            Base.Player.applyBattleRewards(Assault.iGoldGained,Assault.iXPGained,this.returnHomeAfterBattleReward);
         }
      }""",
    )

    replace_once(
        quest,
        """   public class PopupQuestFinished extends PopupQuestFinishedMC
   {
""",
        """   public class PopupQuestFinished extends PopupQuestFinishedMC
   {
      
      private static const QUEST_LEVELUP_RETURN_FIX:String = "socialemperors-quest-levelup-return-v1";
""",
    )
    replace_once(
        quest,
        """      public function message1OK(param1:Event) : void
      {
         if(Config.FORCE_RELOAD_ON_QUEST_END)
         {
            Base.Commands.reloadOnEndQuest();
         }
         else
         {
            this.closeWindow();
            Base.Main.addEventListener(Base.ALL_BUILDINGS_LOADED,this.goToQuestMap);
            Base.Gui.returnHomeUser();
         }
      }""",
        """      private function returnHomeAfterQuestReward() : void
      {
         Base.Main.addEventListener(Base.ALL_BUILDINGS_LOADED,this.goToQuestMap);
         Base.Gui.returnHomeUser();
      }
      
      public function message1OK(param1:Event) : void
      {
         var _loc2_:int = int(Base.Player.questsRank[Quest.idQuest]);
         if(Config.FORCE_RELOAD_ON_QUEST_END)
         {
            Base.Commands.reloadOnEndQuest();
         }
         else
         {
            this.closeWindow();
            if(Quest.bPlayerWinner && Quest.currentQuest != null && Quest.currentQuest.difficulty > _loc2_)
            {
               Base.Player.applyBattleRewards(Quest.iGoldGained,Quest.iXPGained,this.returnHomeAfterQuestReward);
            }
            else
            {
               this.returnHomeAfterQuestReward();
            }
         }
      }""",
    )


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    present = [uncompressed(swf).count(marker) for marker in MARKERS]
    if present == [1, 1, 1]:
        if output != swf:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(swf, output)
        print(f"Already patched {output}")
        return
    if any(present):
        raise RuntimeError(f"partial/duplicated patch markers: {present}")

    with tempfile.TemporaryDirectory(prefix="se-battle-levelup-swf-") as raw_tmp:
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
