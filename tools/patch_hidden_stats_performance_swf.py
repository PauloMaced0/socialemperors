#!/usr/bin/env python3
"""Stop the hidden debug monitor accumulating work after map transitions.

The game creates a new ``core.Stats`` instance whenever a map finishes
loading.  The old instance is never removed, and each instance retains an
``ENTER_FRAME`` listener which updates text and a BitmapData graph even while
the widget is invisible.  Visits, quests and PvP therefore add permanent work
to every later frame.

This deliberately patches only the small debug widget.  It does not recompile
Base, IsoEngine, unit, combat or map-lifecycle classes; recompiling those hot
classes caused a severe Ruffle responsiveness regression in an earlier
experiment.
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
CLASS = "core.Stats"
SCRIPT = Path("core/Stats.as")
MARKER = b"socialemperors-hidden-stats-lifecycle-v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source signature, found {count}")
    return source.replace(old, new, 1)


def patch_source(path: Path) -> None:
    source = path.read_text()
    marker = MARKER.decode()
    if marker in source:
        return

    source = _replace_once(
        source,
        "   public class Stats extends Sprite\n   {\n",
        "   public class Stats extends Sprite\n"
        "   {\n"
        f'      \n      private static const LIFECYCLE_FIX:String = "{marker}";\n'
        "      \n      private static var active:Stats = null;\n",
        "Stats marker and singleton",
    )
    source = _replace_once(
        source,
        "      private function update(param1:Event) : void\n"
        "      {\n"
        "         var _loc2_:* = undefined;",
        "      private function update(param1:Event) : void\n"
        "      {\n"
        "         var _loc2_:* = undefined;\n"
        "         if(!visible)\n"
        "         {\n"
        "            return;\n"
        "         }",
        "hidden Stats update guard",
    )
    source = _replace_once(
        source,
        "      private function init(param1:Event) : void\n"
        "      {\n"
        "         var _loc2_:* = undefined;\n"
        "         removeEventListener(Event.ADDED_TO_STAGE,this.init);",
        "      private function init(param1:Event) : void\n"
        "      {\n"
        "         var _loc2_:* = undefined;\n"
        "         removeEventListener(Event.ADDED_TO_STAGE,this.init);\n"
        "         if(active != null && active != this)\n"
        "         {\n"
        "            active.dispose();\n"
        "         }\n"
        "         active = this;",
        "Stats singleton replacement",
    )
    source = _replace_once(
        source,
        "      private function onClick(param1:MouseEvent) : void\n"
        "      {",
        "      private function dispose() : void\n"
        "      {\n"
        "         removeEventListener(Event.ADDED_TO_STAGE,this.init);\n"
        "         removeEventListener(Event.ENTER_FRAME,this.update);\n"
        "         removeEventListener(MouseEvent.MOUSE_DOWN,this.onClick);\n"
        "         if(this._graph != null)\n"
        "         {\n"
        "            this._graph.dispose();\n"
        "            this._graph = null;\n"
        "         }\n"
        "         if(parent != null)\n"
        "         {\n"
        "            parent.removeChild(this);\n"
        "         }\n"
        "      }\n"
        "      \n"
        "      private function onClick(param1:MouseEvent) : void\n"
        "      {",
        "Stats disposal",
    )
    path.write_text(source)


def uncompress(data: bytes) -> bytes:
    if data[:3] == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data


def patched(data: bytes) -> bool:
    return uncompress(data).count(MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Already patched: {output}")
        return

    ffdec = args.ffdec.resolve()
    with tempfile.TemporaryDirectory(prefix="se-hidden-stats-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run(
            [
                "java",
                f"-Duser.home={tmp / 'ffdec-home'}",
                "-jar",
                str(ffdec),
                "-selectclass",
                CLASS,
                "-export",
                "script",
                str(export_dir),
                str(work_swf),
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
                "java",
                f"-Duser.home={tmp / 'ffdec-home'}",
                "-jar",
                str(ffdec),
                "-importScript",
                str(work_swf),
                str(work_swf),
                str(tmp / "one-script"),
            ],
            check=True,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    if not patched(output.read_bytes()):
        raise RuntimeError("hidden Stats lifecycle marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
