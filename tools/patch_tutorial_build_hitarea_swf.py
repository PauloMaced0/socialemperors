#!/usr/bin/env python3
"""Let construction-tutorial clicks reach the highlighted Build controls.

The dark tutorial mask has a visual opening over the Build button, House I,
and the required placement tile, but its Flash hit area is larger than that
opening.  Under Ruffle it can therefore consume a click which is visibly on
the highlighted control.  The construction flow already validates every
action (step 8 only opens Build, step 9 only accepts House I, and step 10 only
accepts tile 55,55), so make just those three mask frames non-interactive.
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
CLASS = "managers.PopupManager"
SCRIPT = Path("managers/PopupManager.as")
MARKER = b"socialemperors-tutorial-build-hitarea-v1"


ORIGINAL_MASK = """         this.fondoNegroTutorial = this.popupLayer.addChild(new FondoNegroTutorialMC());
         this.fondoNegroTutorial.gotoAndStop(param1 + 1);"""

PATCHED_MASK = """         this.fondoNegroTutorial = this.popupLayer.addChild(new FondoNegroTutorialMC());
         this.fondoNegroTutorial.gotoAndStop(param1 + 1);
         if(param1 >= 8 && param1 <= 10)
         {
            this.fondoNegroTutorial.mouseEnabled = false;
            this.fondoNegroTutorial.mouseChildren = false;
         }"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker not in source:
        signature = "   public class PopupManager\n   {\n"
        if source.count(signature) != 1:
            raise RuntimeError("PopupManager class declaration not found exactly once")
        source = source.replace(
            signature,
            signature
            + f'      \n      private static const TUTORIAL_BUILD_HITAREA_FIX:String = "{marker}";\n',
            1,
        )
    if source.count(PATCHED_MASK) == 1:
        path.write_text(source)
        return
    count = source.count(ORIGINAL_MASK)
    if count != 1:
        raise RuntimeError(
            "tutorial construction mask: expected one source match, "
            f"found {count}"
        )
    path.write_text(source.replace(ORIGINAL_MASK, PATCHED_MASK, 1))


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    plain = _plain(data)
    return plain.count(MARKER) == 1 and b"mouseChildren" in plain


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-tutorial-build-swf-") as raw_tmp:
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
        raise RuntimeError("tutorial Build hit-area marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
