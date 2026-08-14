#!/usr/bin/env python3
"""Resume the starter tutorial and advance combat through explicit attacks.

The stock client always set ``startingTutorialStep`` to zero.  The server now
returns a durable ``privateState.tutorialStep`` checkpoint, so restore it and
rebuild the map/tab locks appropriate for that step.  The explicit enemy-click
dispatch introduced by the attack/movement fix bypasses IsoUnit's legacy
``nextStep`` callback; advance step 12 in the shared objective dispatcher.
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
CLASS = "core.Base"
SCRIPT = Path("core/Base.as")
MARKER = b"socialemperors-tutorial-resume-v1"
CAMERA_MARKER = b"socialemperors-tutorial-resume-camera-v2"


ORIGINAL_BOOTSTRAP = """            this.tutorialMode = true;
            this.mapLocked = true;
            this.startingTutorialStep = 0;
            _loc1_ = _loc1_;
            switch(_loc1_)
            {
               case 0:
               case 1:
                  this.tabsLocked = true;
               case 3:
                  this.mapLocked = true;
            }"""

PATCHED_BOOTSTRAP = """            this.tutorialMode = true;
            this.startingTutorialStep = int(Base.Player.privateState[\"tutorialStep\"]);
            if(this.startingTutorialStep < 0 || this.startingTutorialStep > 14)
            {
               this.startingTutorialStep = 0;
            }
            this.tabsLocked = this.startingTutorialStep < 2;
            this.mapLocked = this.startingTutorialStep < 3;"""

ORIGINAL_OBJECTIVE = """         _loc8_ = param2.buildingReference;
         if(_loc8_ != null && _loc8_ != -1)"""

PATCHED_OBJECTIVE = """         if(Base.Main.tutorialMode && Base.Main.tutorial != null && Base.Main.tutorial.getStep() == 12 && param2.PlayerID == Constants.PLAYER_ENEMY)
         {
            Base.Main.tutorial.nextStep();
         }
         _loc8_ = param2.buildingReference;
         if(_loc8_ != null && _loc8_ != -1)"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker not in source:
        signature = "   public class Base extends MovieClip\n   {\n"
        if source.count(signature) != 1:
            raise RuntimeError("Base class declaration not found exactly once")
        source = source.replace(
            signature,
            signature
            + f'      \n      private static const TUTORIAL_RESUME_FIX:String = "{marker}";\n',
            1,
        )
    camera_marker = CAMERA_MARKER.decode()
    if camera_marker not in source:
        marker_declaration = (
            f'      private static const TUTORIAL_RESUME_FIX:String = "{marker}";'
        )
        if source.count(marker_declaration) != 1:
            raise RuntimeError("tutorial resume marker declaration not found")
        source = source.replace(
            marker_declaration,
            marker_declaration
            + f'\n      \n      private static const TUTORIAL_RESUME_CAMERA_FIX:String = "{camera_marker}";',
            1,
        )
    for label, original, replacement in (
        ("tutorial checkpoint bootstrap", ORIGINAL_BOOTSTRAP, PATCHED_BOOTSTRAP),
        ("tutorial attack progression", ORIGINAL_OBJECTIVE, PATCHED_OBJECTIVE),
    ):
        if source.count(replacement) == 1:
            continue
        count = source.count(original)
        if count != 1:
            raise RuntimeError(f"{label}: expected one source match, found {count}")
        source = source.replace(original, replacement, 1)
    camera_original = """            this.tutorial = new TutorialMain();
            this.addChild(this.tutorial);
            TutorialMain(this.tutorial).addEventListener(TutorialMain.TUTORIAL_COMPLETED,this.onTutorialCompleted);"""
    camera_replacement = """            this.tutorial = new TutorialMain();
            this.addChild(this.tutorial);
            if(this.startingTutorialStep >= 11 && this.startingTutorialStep <= 14)
            {
               this.moveMap(int((-250 - this.topX) * this.currentZoom),int((-2300 - this.topY) * this.currentZoom));
            }
            TutorialMain(this.tutorial).addEventListener(TutorialMain.TUTORIAL_COMPLETED,this.onTutorialCompleted);"""
    if source.count(camera_replacement) != 1:
        count = source.count(camera_original)
        if count != 1:
            raise RuntimeError(
                "tutorial resume camera: expected one source match, "
                f"found {count}"
            )
        source = source.replace(camera_original, camera_replacement, 1)
    path.write_text(source)


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    plain = _plain(data)
    return (
        plain.count(MARKER) == 1
        and plain.count(CAMERA_MARKER) == 1
        and b"tutorialStep" in plain
        and b"startingTutorialStep" in plain
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
    with tempfile.TemporaryDirectory(prefix="se-tutorial-resume-swf-") as raw_tmp:
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
        raise RuntimeError("tutorial-resume marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
