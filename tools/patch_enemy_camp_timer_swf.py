#!/usr/bin/env python3
"""Keep a live enemy camp out of the post-clear cooldown lifecycle.

The legacy client uses one CountdownTimer both to render the enemy-camp HUD
and to call MapInitializer.spawnInit when four hours elapse.  A persisted live
camp therefore either shows a false cooldown (non-zero timer) or is cleaned
and respawned at a new border position (zero timer).  While the persisted
objective exists, show IsoEngine's existing "N enemies alive" text and never
fire the respawn callback.  Once the objective is collected, the unchanged
timer displays and completes the genuine four-hour cooldown.
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
CLASS = "GUI.CountdownTimer"
SCRIPT = Path("GUI/CountdownTimer.as")
MARKER = b"socialemperors-enemy-camp-timer-v1"


ORIGINAL_COMPLETE = """         if(Base.Main.gameMode == Constants.GAME_MODE_NORMAL)
         {
            MapInitializer.spawnInit();
         }"""

PATCHED_COMPLETE = """         if(Base.Main.gameMode == Constants.GAME_MODE_NORMAL && Base.Iso.eTreasureChest == null)
         {
            MapInitializer.spawnInit();
         }"""

ORIGINAL_DISPLAY = """         if(this._currentCount < 1)
         {
            this._textBox.visible = false;
            this.defaultTextBox.visible = true;
         }"""

PATCHED_DISPLAY = """         if(this._currentCount < 1 || Base.Iso.eTreasureChest != null)
         {
            this._textBox.visible = false;
            this.defaultTextBox.visible = true;
         }"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker not in source:
        class_signatures = (
            "   public class CountdownTimer extends Sprite\n   {\n",
            "   public class CountdownTimer extends MovieClip\n   {\n",
        )
        signature = next((value for value in class_signatures
                          if source.count(value) == 1), None)
        if signature is None:
            raise RuntimeError("CountdownTimer class declaration not found")
        source = source.replace(
            signature,
            signature +
            f'      \n      private static const ENEMY_CAMP_TIMER_FIX:String = "{marker}";\n',
            1,
        )
    for label, original, replacement in (
        ("camp timer completion", ORIGINAL_COMPLETE, PATCHED_COMPLETE),
        ("camp timer display", ORIGINAL_DISPLAY, PATCHED_DISPLAY),
    ):
        if source.count(replacement) == 1:
            continue
        count = source.count(original)
        if count != 1:
            raise RuntimeError(
                f"{label} signature: expected once, found {count}"
            )
        source = source.replace(original, replacement, 1)
    path.write_text(source)


def uncompress(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    raw = uncompress(data)
    return (
        raw.count(MARKER) == 1
        and b"eTreasureChest" in raw
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
    with tempfile.TemporaryDirectory(prefix="se-enemy-camp-timer-swf-") as raw_tmp:
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
        raise RuntimeError("enemy-camp timer marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
