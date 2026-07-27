#!/usr/bin/env python3
"""Guard the mission-complete animation so quest/PvP results stop hanging.

``popups.MissionPopup.misionCompletada`` dereferences a local ``mayor`` that
the compiled bytecode initialises to ``null`` and never assigns before calling
``mayor.getChildAt(0)`` (outside the method's own try/catch), so it throws
``Error #1009`` every time it runs. Completing a goal during quest/PvP end
routes ``Quest.end -> MissionsManager.checkAllMissions -> ... ->
MissionsManager.misionCompletada -> MissionPopup.misionCompletada``; the crash
aborts ``Quest.sendResults`` before it queues END_QUEST/END_ATTACK, so the
"Mission result" popup is stuck forever on "Saving Results ...".

The popup call is a purely cosmetic completion animation (the goal is already
recorded server-side via CMD_COMPLETE_MISSION and client-side beforehand), so
wrap the single call site in ``MissionsManager.misionCompletada`` in a
try/catch. The result flow then finishes and reveals the OK button.

This is a single-class import, matching how MissionsManager was already
class-replaced, so it does not disturb the whole-SWF v26 behaviour patch.

Requires JPEXS FFDec. Example:

    python tools/patch_mission_popup_crash_swf.py --ffdec /path/to/ffdec.jar
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWF = ROOT / "assets" / "flash" / "SocialEmpires0926bsec.swf"
TARGET_CLASS = "managers.MissionsManager"
SCRIPT_FILE = Path("managers/MissionsManager.as")

MARKER = "socialemperors-mission-popup-crash-guard-v1"

_CLASS_DECL = """   public class MissionsManager
   {
"""

_CLASS_DECL_MARKED = """   public class MissionsManager
   {

      private static const MISSION_POPUP_CRASH_GUARD:String = "socialemperors-mission-popup-crash-guard-v1";
"""

_ORIGINAL = """      private function misionCompletada(param1:int) : void
      {
         if(Base.Gui.missionBox != null)
         {
            Base.Gui.missionBox.misionCompletada("" + param1);
         }
      }"""

_GUARDED = """      private function misionCompletada(param1:int) : void
      {
         if(Base.Gui.missionBox != null)
         {
            try
            {
               Base.Gui.missionBox.misionCompletada("" + param1);
            }
            catch(e:Error)
            {
            }
         }
      }"""


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-mission-popup-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", TARGET_CLASS,
            "-export", "script", str(export_dir), str(work_swf),
        ], check=True)
        source = export_dir / "scripts" / SCRIPT_FILE
        text = source.read_text()
        if _GUARDED in text and MARKER in text:
            print("MissionsManager.misionCompletada already guarded; nothing to do.")
            return
        if text.count(_ORIGINAL) != 1:
            raise RuntimeError(
                "MissionsManager.misionCompletada signature not found (once)"
            )
        if text.count(_CLASS_DECL) != 1:
            raise RuntimeError("MissionsManager class declaration not found (once)")
        text = text.replace(_ORIGINAL, _GUARDED, 1)
        text = text.replace(_CLASS_DECL, _CLASS_DECL_MARKED, 1)
        source.write_text(text)
        one = tmp / "one-script"
        target = one / SCRIPT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-importScript", str(work_swf), str(work_swf), str(one),
        ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
