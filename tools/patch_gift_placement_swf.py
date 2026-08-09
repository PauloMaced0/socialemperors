#!/usr/bin/env python3
"""Flush gift placement immediately so refresh cannot restore the gift.

The stock client removes a placed gift from its local panel immediately but
only queues ``place_gift`` for CommandManager's periodic timer.  Closing or
reloading the page in that gap leaves the server-side count untouched, so the
unit appears in Gifts again on the next load.  Other gift awards already send
their own commands immediately; placement needs the same durability boundary.

This patch keeps the normal command queue and asks it to flush as soon as a
``place_gift`` command is added.  CommandManager's existing in-flight-packet
guard remains intact, so it does not create concurrent requests.

Requires JPEXS FFDec. Example:

    python tools/patch_gift_placement_swf.py --ffdec /path/to/ffdec.jar
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
CLASS = "managers.CommandManager"
SCRIPT = Path("managers/CommandManager.as")
MARKER = b"socialemperors-gift-placement-flush-v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source match, found {count}")
    return source.replace(old, new, 1)


def patch_source(path: Path) -> None:
    source = path.read_text()
    source = _replace_once(
        source,
        "   public class CommandManager\n   {\n",
        """   public class CommandManager
   {
      
      private static const GIFT_PLACEMENT_FLUSH_FIX:String = "socialemperors-gift-placement-flush-v1";
""",
        "CommandManager marker",
    )
    source = _replace_once(
        source,
        """            this.commands.push(param1);
         }
      }
      
      public function add(param1:String, ... rest) : void
""",
        """            this.commands.push(param1);
            if(param1.cmd == Constants.CMD_PLACE_GIFT)
            {
               this.sendCommands();
            }
         }
      }
      
      public function add(param1:String, ... rest) : void
""",
        "gift placement flush",
    )
    path.write_text(source)


def patched(data: bytes) -> bool:
    if data[:3] == b"CWS":
        data = b"FWS" + data[3:8] + zlib.decompress(data[8:])
    return data.count(MARKER) == 1


def run(args: argparse.Namespace) -> None:
    swf = args.swf.resolve()
    output = args.output.resolve() if args.output else swf
    ffdec = args.ffdec.resolve()
    if patched(swf.read_bytes()):
        if output != swf:
            shutil.copy2(swf, output)
        print(f"Gift placement already patched: {output}")
        return

    with tempfile.TemporaryDirectory(prefix="se-gift-placement-swf-") as raw_tmp:
        tmp = Path(raw_tmp)
        export_dir = tmp / "export"
        work_swf = tmp / swf.name
        shutil.copy2(swf, work_swf)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-selectclass", CLASS,
            "-export", "script", str(export_dir), str(work_swf),
        ], check=True)
        source = export_dir / "scripts" / SCRIPT
        patch_source(source)
        one = tmp / "one-script" / SCRIPT
        one.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, one)
        subprocess.run([
            "java", f"-Duser.home={tmp / 'ffdec-home'}", "-jar", str(ffdec),
            "-importScript", str(work_swf), str(work_swf),
            str(tmp / "one-script"),
        ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_swf, output)

    if not patched(output.read_bytes()):
        raise RuntimeError("gift-placement SWF marker missing or duplicated")
    print(f"Patched {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swf", type=Path, default=DEFAULT_SWF)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffdec", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
