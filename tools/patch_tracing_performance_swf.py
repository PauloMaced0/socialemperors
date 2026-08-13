#!/usr/bin/env python3
"""Bound the legacy in-client debug log so long sessions do not leak memory.

``Tracing.Init(null, true)`` enables the old debug collector at startup even
though its text panel is normally absent. Every asset load, command and
diagnostic then appends another timestamped string to an unbounded Vector.
Ruffle sessions that stay open for hours therefore retain an ever-growing
debug history which has no gameplay value.

This patch changes only ``com.socialpoint.debug.Tracing``. It keeps the most
recent messages for the optional admin panel, caps that history, and removes
the duplicate native ``trace()`` emission. No game, map, combat or persistence
class is rebuilt.
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
CLASS = "com.socialpoint.debug.Tracing"
SCRIPT = Path("com/socialpoint/debug/Tracing.as")
MARKER = b"socialemperors-bounded-tracing-v1"


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
        "   public class Tracing\n   {\n",
        "   public class Tracing\n"
        "   {\n"
        f'      \n      private static const PERFORMANCE_FIX:String = "{marker}";\n'
        "      \n      private static const MAX_HISTORY:int = 200;\n",
        "Tracing marker and history cap",
    )
    source = _replace_once(
        source,
        "            oTracing.vText.push(param1);",
        "            oTracing.remember(param1);",
        "plain debug message retention",
    )
    source = _replace_once(
        source,
        '            oTracing.vText.push("[" + _loc2_.toTimeString() + "] " + param1);',
        '            oTracing.remember("[" + _loc2_.toTimeString() + "] " + param1);',
        "timestamped debug message retention",
    )
    source = _replace_once(
        source,
        '      public static function Trace(param1:String) : void\n'
        "      {\n"
        '         trace("TRACE: " + param1);\n'
        "         PrintWithTimeStamp(param1);\n"
        "      }",
        '      public static function Trace(param1:String) : void\n'
        "      {\n"
        "         PrintWithTimeStamp(param1);\n"
        "      }",
        "native trace suppression",
    )
    source = _replace_once(
        source,
        "      public static function Refresh() : void\n"
        "      {",
        "      private function remember(param1:String) : void\n"
        "      {\n"
        "         this.vText.push(param1);\n"
        "         if(this.vText.length > MAX_HISTORY)\n"
        "         {\n"
        "            this.vText.splice(0,this.vText.length - MAX_HISTORY);\n"
        "         }\n"
        "      }\n"
        "      \n"
        "      public static function Refresh() : void\n"
        "      {",
        "bounded debug history helper",
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
    with tempfile.TemporaryDirectory(prefix="se-tracing-swf-") as raw_tmp:
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
        raise RuntimeError("bounded Tracing marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
