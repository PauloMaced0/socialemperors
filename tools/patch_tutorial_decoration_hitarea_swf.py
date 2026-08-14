#!/usr/bin/env python3
"""Prevent tutorial decorations from swallowing highlighted-control clicks.

TutorialMain draws Arthur and the large yellow arrow above the game UI.  They
are visual instructions only, but their MovieClip hit areas remain active and
can cover the Build control at larger client sizes.  Disable mouse handling on
the TutorialMain display list; tutorial buttons live in PopupManager and map,
store, unit, and resource actions continue to receive their own events.
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
CLASS = "core.TutorialMain"
SCRIPT = Path("core/TutorialMain.as")
MARKER = b"socialemperors-tutorial-decoration-hitarea-v1"


ORIGINAL_CONSTRUCTOR = """      public function TutorialMain()
      {
         super();
         this.init();
      }"""

PATCHED_CONSTRUCTOR = """      public function TutorialMain()
      {
         super();
         mouseEnabled = false;
         mouseChildren = false;
         this.init();
      }"""


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker not in source:
        signature = "   public class TutorialMain extends MovieClip\n   {\n"
        if source.count(signature) != 1:
            raise RuntimeError("TutorialMain class declaration not found exactly once")
        source = source.replace(
            signature,
            signature
            + f'      \n      private static const TUTORIAL_DECORATION_HITAREA_FIX:String = "{marker}";\n',
            1,
        )
    if source.count(PATCHED_CONSTRUCTOR) == 1:
        path.write_text(source)
        return
    count = source.count(ORIGINAL_CONSTRUCTOR)
    if count != 1:
        raise RuntimeError(
            "TutorialMain constructor: expected one source match, "
            f"found {count}"
        )
    path.write_text(source.replace(ORIGINAL_CONSTRUCTOR, PATCHED_CONSTRUCTOR, 1))


def _plain(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    plain = _plain(data)
    return plain.count(MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-tutorial-decoration-swf-") as raw_tmp:
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
        raise RuntimeError("tutorial-decoration hit-area marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
