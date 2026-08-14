#!/usr/bin/env python3
"""Keep the construction lessons recoverable after the store or cursor is lost.

The starter tutorial drives building placement through three consecutive
steps -- open Build (8/17), pick the item (9/18), click the tile (10/19) --
and each control only answers on its own step.  ``storeButtMouse1`` ignores
the Build button unless the step is exactly 8 or 17, and
``ItemButtonLarge.mouseUsed`` ignores every store item unless the step is
exactly 9 or 18.  Nothing re-arms those controls, so closing the store on
step 9/18, or dropping the placement cursor on step 10/19, leaves the player
with a lesson they can no longer perform: the Build button is visibly
highlighted but dead, and the tutorial cannot advance.

Make the two controls idempotent for the steps they already belong to.  Build
reopens the store on 9/10/18/19 without advancing the lesson, and the required
item (House I on 10, Farm Land on 19) can be picked again to re-arm the
placement cursor.  Every other item stays rejected, and no step is skipped, so
the lesson order is unchanged for a player who never loses the window.
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

GUI_CLASS = "GUI.GuiManager"
GUI_SCRIPT = Path("GUI/GuiManager.as")
GUI_MARKER = b"socialemperors-tutorial-store-reopen-v1"

ITEM_CLASS = "GUI.ItemButtonLarge"
ITEM_SCRIPT = Path("GUI/ItemButtonLarge.as")
ITEM_MARKER = b"socialemperors-tutorial-store-repick-v1"


ORIGINAL_STORE_BUTTON = """         Base.Main.placingStoreObject = null;
         if(Base.Main.tutorialMode && Base.Main.tutorial.getStep() != 8 && Base.Main.tutorial.getStep() != 17)
         {
            return;
         }"""

PATCHED_STORE_BUTTON = """         Base.Main.placingStoreObject = null;
         if(Base.Main.tutorialMode && (Base.Main.tutorial.getStep() == 9 || Base.Main.tutorial.getStep() == 10 || Base.Main.tutorial.getStep() == 18 || Base.Main.tutorial.getStep() == 19))
         {
            this.addGuiCover(this);
            this.storeWindow.visible = true;
            this.updateFilterState();
            return;
         }
         if(Base.Main.tutorialMode && Base.Main.tutorial.getStep() != 8 && Base.Main.tutorial.getStep() != 17)
         {
            return;
         }"""

ORIGINAL_ITEM_CLICK = """         if(Base.Main.tutorialMode)
         {
            if(Base.Main.tutorial.getStep() == 9)
            {"""

PATCHED_ITEM_CLICK = """         if(Base.Main.tutorialMode)
         {
            if(Base.Main.tutorial.getStep() == 10 && this.item.id == 1 || Base.Main.tutorial.getStep() == 19 && this.item.id == 10)
            {
               Base.Sound.playSfx(SoundManager.SFX_BUTTON_CLICK);
               Base.Main.setBuilding(this.item);
               return;
            }
            if(Base.Main.tutorial.getStep() == 9)
            {"""


def _declare_marker(source: str, signature: str, name: str, marker: bytes) -> str:
    """Insert a private marker constant right after a class declaration."""
    text = marker.decode()
    if text in source:
        return source
    if source.count(signature) != 1:
        raise RuntimeError(f"{signature.strip()} not found exactly once")
    return source.replace(
        signature,
        signature + f'      \n      private static const {name}:String = "{text}";\n',
        1,
    )


def _replace_once(source: str, label: str, original: str, patched: str) -> str:
    if source.count(patched) == 1:
        return source
    count = source.count(original)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one source match, found {count}"
        )
    return source.replace(original, patched, 1)


def patch_gui_manager(path: Path) -> None:
    source = path.read_text().replace("\r\n", "\n")
    source = _declare_marker(
        source,
        "   public class GuiManager extends GuiMC\n   {\n",
        "TUTORIAL_STORE_REOPEN_FIX",
        GUI_MARKER,
    )
    source = _replace_once(
        source,
        "tutorial Build button reopen",
        ORIGINAL_STORE_BUTTON,
        PATCHED_STORE_BUTTON,
    )
    path.write_text(source)


def patch_item_button(path: Path) -> None:
    source = path.read_text().replace("\r\n", "\n")
    source = _declare_marker(
        source,
        "   public class ItemButtonLarge extends BuildButtonLargeMC\n   {\n",
        "TUTORIAL_STORE_REPICK_FIX",
        ITEM_MARKER,
    )
    source = _replace_once(
        source,
        "tutorial placement item re-pick",
        ORIGINAL_ITEM_CLICK,
        PATCHED_ITEM_CLICK,
    )
    path.write_text(source)


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    plain = _plain(data)
    return plain.count(GUI_MARKER) == 1 and plain.count(ITEM_MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = (args.output or swf).resolve()
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-tutorial-store-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run(
            [
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                str(ffdec), "-selectclass", f"{GUI_CLASS},{ITEM_CLASS}",
                "-export", "script", str(export_dir), str(work_swf),
            ],
            check=True,
        )
        patch_gui_manager(export_dir / "scripts" / GUI_SCRIPT)
        patch_item_button(export_dir / "scripts" / ITEM_SCRIPT)
        staged = tmp / "scripts-in"
        for script in (GUI_SCRIPT, ITEM_SCRIPT):
            target = staged / script
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(export_dir / "scripts" / script, target)
        subprocess.run(
            [
                "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar",
                str(ffdec), "-importScript", str(work_swf), str(work_swf),
                str(staged),
            ],
            check=True,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    if not patched(output.read_bytes()):
        raise RuntimeError("tutorial store-reopen markers missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
