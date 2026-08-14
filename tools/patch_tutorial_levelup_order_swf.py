#!/usr/bin/env python3
"""Keep level-up popups behind the starter tutorial's final message.

PlayerStatus normally opens PopupLevel as soon as an XP reward crosses the
next threshold.  PopupManager.levelUp closes the active confirmWindow first,
which destroys TutorialMain's Congratulations window before the player can
press Next.  Defer level evaluation while the starter tutorial is active and
release it from TutorialMain.endTutorial, after TutorialPopup has closed.
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
MARKER = b"socialemperors-tutorial-levelup-order-v1"
CLASSES = (
    ("managers.PlayerStatus", Path("managers/PlayerStatus.as")),
    ("core.TutorialMain", Path("core/TutorialMain.as")),
)


PLAYER_SIGNATURE = "   public class PlayerStatus implements IEventDispatcher\n   {\n"
PLAYER_MARKER_FIELDS = """      
      private static const TUTORIAL_LEVELUP_ORDER_FIX:String = "socialemperors-tutorial-levelup-order-v1";
      
      private var tutorialLevelUpPending:Boolean = false;
      
      private var tutorialLevelUpReleased:Boolean = false;
"""

CHECK_START = """         var alreadyLeveledUp:Boolean = param1;
         if(this.iLevel < Base.Items.arLevelList.length && this.iExperience >= parseInt(Base.Items.arLevelList[this.iLevel].exp_required))"""

CHECK_DEFERRED = """         var alreadyLeveledUp:Boolean = param1;
         if(Base.Main.tutorialMode && !this.tutorialLevelUpReleased)
         {
            if(this.iLevel < Base.Items.arLevelList.length && this.iExperience >= parseInt(Base.Items.arLevelList[this.iLevel].exp_required))
            {
               this.tutorialLevelUpPending = true;
            }
            return;
         }
         if(this.iLevel < Base.Items.arLevelList.length && this.iExperience >= parseInt(Base.Items.arLevelList[this.iLevel].exp_required))"""

CAN_BUY = """      public function canBuy(param1:Object) : Boolean
      {"""

RELEASE_AND_CAN_BUY = """      public function releaseTutorialLevelUp() : void
      {
         this.tutorialLevelUpReleased = true;
         if(this.tutorialLevelUpPending)
         {
            this.tutorialLevelUpPending = false;
            this.checkForLevelUp(false);
         }
      }
      
      public function canBuy(param1:Object) : Boolean
      {"""

TUTORIAL_COMPLETE = """         Base.Main.completarTutorial(15);
         Base.Gui.countdownTimer.setTimer();"""

TUTORIAL_RELEASE = """         Base.Main.completarTutorial(15);
         Base.Player.releaseTutorialLevelUp();
         Base.Gui.countdownTimer.setTimer();"""


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if source.count(new) == 1:
        return source
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return source.replace(old, new, 1)


def patch_player(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker not in source:
        if source.count(PLAYER_SIGNATURE) != 1:
            raise RuntimeError("PlayerStatus declaration not found exactly once")
        source = source.replace(
            PLAYER_SIGNATURE,
            PLAYER_SIGNATURE + PLAYER_MARKER_FIELDS,
            1,
        )
    source = _replace_once(
        source,
        CHECK_START,
        CHECK_DEFERRED,
        "tutorial level-up deferral",
    )
    source = _replace_once(
        source,
        CAN_BUY,
        RELEASE_AND_CAN_BUY,
        "tutorial level-up release method",
    )
    path.write_text(source)


def patch_tutorial(path: Path) -> None:
    source = _replace_once(
        path.read_text(),
        TUTORIAL_COMPLETE,
        TUTORIAL_RELEASE,
        "tutorial completion level-up release",
    )
    path.write_text(source)


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    plain = _plain(data)
    return (
        plain.count(MARKER) == 1
        and b"releaseTutorialLevelUp" in plain
        and b"tutorialLevelUpPending" in plain
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
    with tempfile.TemporaryDirectory(prefix="se-tutorial-levelup-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        for class_name, _ in CLASSES:
            subprocess.run(
                [
                    "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                    str(ffdec), "-selectclass", class_name, "-export", "script",
                    str(export_dir), str(work_swf),
                ],
                check=True,
            )
        patch_player(export_dir / "scripts" / CLASSES[0][1])
        patch_tutorial(export_dir / "scripts" / CLASSES[1][1])
        one = tmp / "one-script"
        for _, script in CLASSES:
            destination = one / script
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(export_dir / "scripts" / script, destination)
        subprocess.run(
            [
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                str(ffdec), "-importScript", str(work_swf), str(work_swf),
                str(one),
            ],
            check=True,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    if not patched(output.read_bytes()):
        raise RuntimeError("tutorial level-up ordering patch is incomplete")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
