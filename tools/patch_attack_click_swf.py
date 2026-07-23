#!/usr/bin/env python3
"""Patch contextual clicks so their action is not followed by a ground move.

The original ``IsoInteractiveElement.clickItem`` routes an enemy click through
``Base.startMovements``. That method is also the ordinary ground-move handler,
so one click mixes two intents: it records an attack target *and* a forced move
to the clicked/formation tile. An earlier patch skipped this block completely;
that stopped the extra move, but also explains why the sword cursor sometimes
did nothing. The final patch keeps the enemy-click condition and replaces only
its body with the explicit contextual route::

    Base.Main.removeCursor()
    Base.Main.startMovementsObjective(Base.Main.hitArray, enemy)

This makes the click an attack order. ``IA_Attack`` can still approach an
out-of-range enemy, but it is no longer an ordinary ground-move order.

There is a second, less visible bug in ``Base.startMovementsObjective``. It
always sets ``STATE_FORCE_MOVE`` and calls
``IA_MoveToDoAction(formationX, formationY, target)``. That is correct for
construction, repair and harvesting, but an enemy click queues a path to the
enemy's formation tile. Calling ``IA_Attack`` without removing
``STATE_FORCE_MOVE`` is also wrong: the AI can suppress the attack or resume
the forced route after attacking. The patched code dispatches by target owner.
Enemy targets cancel the stale route, explicitly return their AI from
``STATE_FORCE_MOVE`` to ``STATE_IDLE``, and receive ``IA_Attack`` without
entering another forced move. This reset matters when the player first issued
a ground order: ``cancelMovement`` clears the path but does not clear iState,
so the sword cursor could otherwise produce no attack. Non-enemy contextual
targets retain the original force-move plus ``IA_MoveToDoAction`` behavior.

Finally, ``IsoUnit.updateAttack`` notices when a ranged unit enters attack range
while moving, but it only clears ``aNodes``. The current straight ``myMove``
segment remains active and can end beside the target. The patch neutralizes that
active segment by setting ``myMove`` to zero coordinates.
It deliberately does not call ``cancelMovement()`` here: this branch runs every
update while the target is in range, and cancelMovement would repeatedly reset
the attack animation before its damage/projectile frame.

The client also listens to two phases of the same mouse gesture. Interactive
objects (enemies, ready producers, resources, and so on) perform their action on
``MOUSE_DOWN``; the map receives the later bubbling ``CLICK`` and can interpret
it as a second, ordinary ground order. Floating gold/XP/resource tokens have the
same problem. The map already owns a ``panDoned`` flag whose purpose is to
consume a click without moving selected units. The final two patches set that
flag for real mouse interactions before the contextual handler runs. Calls made
programmatically with a null MouseEvent remain untouched.

The stock harvest code also replaces every depleted stone or gold deposit with
an invisible ``ID_BUILDING_REGEN_*`` object. That object blocks the tile and
grows the deposit back after three hours. Both the manual-collection and
auto-collection paths are patched to compare against the unreachable
``SUBCATFUNC_RESOURCE_REGEN`` value instead. Trees already disappear correctly;
explicitly renewable production buildings retain their normal cooldowns.

The Remove Tool has a separate gameplay inconsistency: the client rejects every
``IsoUnit`` before it reaches the existing sale code, even though that code and
the server both support selling a deployed unit. The patch makes only that type
guard false, leaving the following ownership, Town Hall, construction and enemy
checks intact. A second rewrite keeps units in the normal confirmation/refund
branch instead of silently deleting them. Non-cash units receive the configured
5% resale value; cash units keep the original zero-cash resale rule.

There is a related reload bug in ``MapInitializer``: its initial natural-
resource population methods run on every map load. The server exposes a
per-town initialized marker through ``privateState.arrayAnimals[128]`` (128 is
RESOURCE_REGEN, not an animal). Small guards skip the tree/stone/gold population
for an established town. A fresh town still receives its resources once, and
the remainder of ``spawnRemainingResources`` still replenishes animals.

The signature includes the surrounding PlayerID/PLAYER_SELF comparison and is
required to occur exactly once. This makes the patch fail safely if a different
SWF is supplied instead of changing an unrelated byte sequence.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import zlib


DEFAULT_SWF = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "flash"
    / "SocialEmpires0926bsec.swf"
)

# The selected-unit ownership check immediately before the enemy action body.
GROUND_ATTACK_PREFIX = bytes.fromhex(
    "66 9e 70 60 02 66 87 01 ab 12 30 00 00"
)

# Base.Main.removeCursor();
# Base.Main.startMovements(enemyTile, Base.Main.hitArray, PLAYER_SELF);
# jump after_enemy_action;
GROUND_ATTACK_BODY_ORIGINAL = bytes.fromhex(
    "60 01 66 0b 4f c6 08 00 "
    "60 01 66 0b d0 66 09 66 1e d0 66 09 66 1f "
    "60 1c 66 43 a2 a0 "
    "60 01 66 0b 66 9f 04 "
    "60 02 66 87 01 4f c3 3c 03 "
    "10 1d 00 00"
)

# v1-v7 skipped the enemy action completely. It fixed the extra ground move,
# but also made a visible sword cursor capable of producing no attack order.
GROUND_ATTACK_BODY_V7 = (
    bytes.fromhex("10 2c 00 00") + GROUND_ATTACK_BODY_ORIGINAL[4:]
)

# Base.Main.removeCursor();
# Base.Main.startMovementsObjective(Base.Main.hitArray, this);
# jump after_enemy_action;
#
# NOP padding keeps the original 48-byte body and all existing offsets.
GROUND_ATTACK_BODY_PATCHED = (
    bytes.fromhex(
        "60 01 66 0b 4f c6 08 00 "
        "60 01 66 0b 60 01 66 0b 66 9f 04 d0 4f cd 18 02"
    )
    + bytes([0x02]) * 20
    + bytes.fromhex("10 1d 00 00")
)

GROUND_ATTACK_ORIGINAL = GROUND_ATTACK_PREFIX + GROUND_ATTACK_BODY_ORIGINAL
GROUND_ATTACK_V7 = GROUND_ATTACK_PREFIX + GROUND_ATTACK_BODY_V7
GROUND_ATTACK_PATCHED = GROUND_ATTACK_PREFIX + GROUND_ATTACK_BODY_PATCHED

# ``IsoInteractiveElement.clickItem`` calculates local mouse coordinates into
# two locals which are never subsequently read. The calculation only runs when
# the MouseEvent is non-null, making it a safe, length-preserving place to mark
# the real user gesture as consumed:
#
#     Base.Main.panDoned = true
#
# Context includes both localX/localY calculations, so this 35-byte signature
# is unique. NOP padding preserves every existing branch offset.
CONTEXT_CLICK_CONSUME_ORIGINAL = bytes.fromhex(
    "d1 66 af 84 01 d0 66 80 01 66 cb 6b 24 02 a3 a0 73 63 04 "
    "d1 66 d9 87 01 d0 66 80 01 66 bf 1c a0 73 63 05"
)
CONTEXT_CLICK_CONSUME_PATCHED = (
    bytes.fromhex("60 01 66 0b 26 61 a5 57")
    + bytes([0x02]) * 27
)

# ``Token.onClick`` can also be invoked automatically with a null MouseEvent,
# so unlike contextual objects it needs an explicit null guard. The replaced
# local initializers are redundant: token index is assigned immediately, and
# the other locals are assigned inside their only consuming branch.
#
#     if (event != null) Base.Main.panDoned = true
#
# The branch skips the eight-byte assignment and two NOPs. The leading
# setlocal2 and following arTokens access make the signature fail safely.
TOKEN_CLICK_CONSUME_ORIGINAL = bytes.fromhex(
    "d6 20 80 06 d7 24 00 63 04 24 00 63 05 20 85 63 08 "
    "60 01 66 0b 66 d9 0a"
)
TOKEN_CLICK_CONSUME_PATCHED = bytes.fromhex(
    "d6 d1 20 13 0a 00 00 60 01 66 0b 26 61 a5 57 02 02 "
    "60 01 66 0b 66 d9 0a"
)

# In Base.startMovementsObjective's selected-unit loop, this is the original
# generic contextual action. It is correct for villagers, construction,
# repairs, resources and other non-enemy targets.
OBJECTIVE_MOVE_ORIGINAL = bytes.fromhex(
    "60 05 64 62 06 41 01 62 07 62 05 66 4a 66 cd 08 "
    "62 07 62 05 66 4a 66 b8 08 d2 4f a7 9c 01 03"
)
# The state assignment immediately before the action. Keeping it before a
# direct IA_Attack was the v3-v5 regression: STATE_FORCE_MOVE could suppress
# the attack or resume the target-tile route afterwards.
OBJECTIVE_FORCE_MOVE_ORIGINAL = bytes.fromhex(
    "60 05 64 62 06 41 01 66 ef 5d 60 c4 01 66 c5 07 "
    "2a 10 0d 00 00 08 06 ac 30 ae 08 02 c2 06 a9 75 "
    "c2 09 63 0a 61 a1 5e 62 0a 08 0a 29"
)

# Previous v2 action: direct IA_Attack without cancelling a stale route.
OBJECTIVE_MOVE_V2 = (
    bytes.fromhex("60 05 64 62 06 41 01 d2 4f 8a 08 01")
    + bytes([0x02]) * 19
)

# ``unit.cancelMovement(); unit.IA_Attack(target)``. ``dup`` keeps the coerced
# IsoUnit instance on the stack for IA_Attack after cancelMovement consumes its
# receiver. NOPs retain the original method length and branch offsets.
OBJECTIVE_MOVE_PATCHED = (
    bytes.fromhex(
        "60 05 64 62 06 41 01 2a 4f c2 01 00 d2 4f 8a 08 01"
    )
    + bytes([0x02]) * 14
)

OBJECTIVE_DISPATCH_ORIGINAL = OBJECTIVE_FORCE_MOVE_ORIGINAL + OBJECTIVE_MOVE_ORIGINAL
OBJECTIVE_DISPATCH_V2 = OBJECTIVE_FORCE_MOVE_ORIGINAL + OBJECTIVE_MOVE_V2
OBJECTIVE_DISPATCH_V3_TO_V5 = OBJECTIVE_FORCE_MOVE_ORIGINAL + OBJECTIVE_MOVE_PATCHED

# if (target.PlayerID == PLAYER_ENEMY) {
#     IsoUnit(unit).cancelMovement();
#     IsoUnit(unit).IA_Attack(target);
# } else {
#     unit.iaIA.iState = IA.STATE_FORCE_MOVE;
#     IsoUnit(unit).IA_MoveToDoAction(tile.x, tile.y, target);
# }
#
# This is exactly 75 bytes, the same size as the original obfuscated state
# assignment plus contextual call, so every existing branch offset remains
# valid. The first iffalse skips 16 bytes to the generic branch; the enemy
# branch jumps 47 bytes to the instruction following this replacement.
OBJECTIVE_DISPATCH_V6_TO_V10 = bytes.fromhex(
    "d2 66 32 60 02 66 45 ab 12 10 00 00 "
    "62 06 2a 4f c2 01 00 d2 4f 8a 08 01 10 2f 00 00 "
    "62 06 66 ef 5d 60 c4 01 66 c5 07 61 a1 5e "
    "60 05 64 62 06 41 01 62 07 62 05 66 4a 66 cd 08 "
    "62 07 62 05 66 4a 66 b8 08 d2 4f a7 9c 01 03 "
    "02 02"
)

# Current dispatch. The enemy branch preserves the unit on the stack while it
# cancels movement, writes IA.STATE_IDLE (0), then calls IA_Attack. To make
# room without changing the method length, the generic branch uses literal
# STATE_FORCE_MOVE (3) and calls its already-verified IsoUnit receiver directly.
OBJECTIVE_DISPATCH_PATCHED = bytes.fromhex(
    "d2 66 32 60 02 66 45 ab 12 19 00 00 "
    "62 06 2a 4f c2 01 00 2a 66 ef 5d 24 00 61 a1 5e "
    "d2 4f 8a 08 01 10 26 00 00 "
    "62 06 66 ef 5d 24 03 61 a1 5e "
    "62 06 62 07 62 05 66 4a 66 cd 08 "
    "62 07 62 05 66 4a 66 b8 08 d2 4f a7 9c 01 03 "
    "02 02"
)

# In IsoUnit.updateAttack's ``distance <= iAttackRange`` branch for ranged
# units, replace ``this.aNodes = new Array()`` with ``this.myMove = [false,
# false]``. AVM2 coerces those values to zero in the existing movement checks,
# stopping the active segment without resetting the attack animation. The
# context includes the attack-range comparison and following Base.Main access
# so this otherwise-common array-clear sequence is unique in the SWF.
RANGED_STOP_ORIGINAL = bytes.fromhex(
    "d0 66 98 01 24 02 b0 12 e8 05 00 "
    "d0 5d 08 4a 08 00 68 eb 03 "
    "60 01 66 0b"
)
RANGED_STOP_V4 = bytes.fromhex(
    "d0 66 98 01 24 02 b0 12 e8 05 00 "
    "d0 4f c2 01 00 02 02 02 02 "
    "60 01 66 0b"
)
RANGED_STOP_PATCHED = bytes.fromhex(
    "d0 66 98 01 24 02 b0 12 e8 05 00 "
    "d0 27 27 56 02 68 91 05 02 "
    "60 01 66 0b"
)

# IsoElement has two natural-resource completion paths: normal ``collect()``
# and the villager/auto-collect update. Each path contains a stone branch and a
# gold branch that installs an ID 81/80 regrowth timer. Change only the compared
# constant from the actual resource subcategory to RESOURCE_REGEN. The outer
# branch accepts only TREE/STONE/GOLD, so the replacement is unreachable and
# safely skips timer creation while preserving bytecode length and offsets.
AUTO_REGEN_STONE_ORIGINAL = bytes.fromhex(
    "62 0e 60 02 66 88 06 14 8d 00 00 60 61 66 53 60 02 66 eb 13"
)
AUTO_REGEN_STONE_PATCHED = bytes.fromhex(
    "62 0e 60 02 66 c7 19 14 8d 00 00 60 61 66 53 60 02 66 eb 13"
)
AUTO_REGEN_GOLD_ORIGINAL = bytes.fromhex(
    "62 0e 60 02 66 d0 05 14 89 00 00 60 61 66 53 60 02 66 af 13"
)
AUTO_REGEN_GOLD_PATCHED = bytes.fromhex(
    "62 0e 60 02 66 c7 19 14 89 00 00 60 61 66 53 60 02 66 af 13"
)
MANUAL_REGEN_STONE_ORIGINAL = bytes.fromhex(
    "62 18 60 02 66 88 06 14 89 00 00 60 61 66 53 60 02 66 eb 13"
)
MANUAL_REGEN_STONE_PATCHED = bytes.fromhex(
    "62 18 60 02 66 c7 19 14 89 00 00 60 61 66 53 60 02 66 eb 13"
)
MANUAL_REGEN_GOLD_ORIGINAL = bytes.fromhex(
    "62 18 60 02 66 d0 05 14 80 00 00 60 61 66 53 60 02 66 af 13"
)
MANUAL_REGEN_GOLD_PATCHED = bytes.fromhex(
    "62 18 60 02 66 c7 19 14 80 00 00 60 61 66 53 60 02 66 af 13"
)

# ``Base.handleMouseClick`` stores the clicked IsoElement in local 23, then
# immediately rejects it when it is an IsoUnit. Preserve the stored object on
# local 23 but replace ``getlex IsoUnit; istypelate`` with
# ``pop; pushfalse; nop``. The rest of the protected-object expression remains
# unchanged, so enemy units and other forbidden objects are still rejected.
UNIT_SELL_GUARD_ORIGINAL = bytes.fromhex(
    "80 2d 2a 63 17 60 05 b3 2a 11 14 00 00 29 d0 66 b0 01"
)
UNIT_SELL_GUARD_PATCHED = bytes.fromhex(
    "80 2d 2a 63 17 29 27 02 2a 11 14 00 00 29 d0 66 b0 01"
)

# The original confirmation condition excludes CAT_UNIT, which would make an
# accepted unit skip the confirmation dialog and be removed immediately. Keep
# the preceding CAT_WONDER/CAT_TERRAIN checks, but make only the final
# ``category != CAT_UNIT`` term true. NOP padding preserves branch offsets.
UNIT_SELL_CONFIRM_ORIGINAL = bytes.fromhex(
    "12 15 00 00 29 "
    "d0 66 b0 01 d1 66 4a 66 88 03 66 ec 0e 60 02 66 da 2d ab 96 "
    "12 a6 01 00"
)
UNIT_SELL_CONFIRM_PATCHED = (
    bytes.fromhex("12 15 00 00 29 26")
    + bytes([0x02]) * 19
    + bytes.fromhex("12 a6 01 00")
)

PATCHES = (
    ("enemy click attack dispatch", GROUND_ATTACK_ORIGINAL, GROUND_ATTACK_PATCHED),
    ("contextual enemy dispatch", OBJECTIVE_DISPATCH_ORIGINAL, OBJECTIVE_DISPATCH_PATCHED),
    ("ranged attack active segment", RANGED_STOP_ORIGINAL, RANGED_STOP_PATCHED),
    ("contextual click consumption", CONTEXT_CLICK_CONSUME_ORIGINAL,
     CONTEXT_CLICK_CONSUME_PATCHED),
    ("reward token click consumption", TOKEN_CLICK_CONSUME_ORIGINAL,
     TOKEN_CLICK_CONSUME_PATCHED),
    ("auto-harvest stone regeneration", AUTO_REGEN_STONE_ORIGINAL,
     AUTO_REGEN_STONE_PATCHED),
    ("auto-harvest gold regeneration", AUTO_REGEN_GOLD_ORIGINAL,
     AUTO_REGEN_GOLD_PATCHED),
    ("manual-harvest stone regeneration", MANUAL_REGEN_STONE_ORIGINAL,
     MANUAL_REGEN_STONE_PATCHED),
    ("manual-harvest gold regeneration", MANUAL_REGEN_GOLD_ORIGINAL,
     MANUAL_REGEN_GOLD_PATCHED),
    ("deployed-unit remove-tool guard", UNIT_SELL_GUARD_ORIGINAL,
     UNIT_SELL_GUARD_PATCHED),
    ("deployed-unit sale confirmation", UNIT_SELL_CONFIRM_ORIGINAL,
     UNIT_SELL_CONFIRM_PATCHED),
)


# These two patches insert a guard, so unlike PATCHES they deliberately grow
# their AVM2 method bodies by 21 bytes. The signatures include method-body
# metadata and enough of the obfuscation prologue to remain unique to this SWF.
#
#     if (Base.Player.privateState.arrayAnimals[128]) ...
#
# spawnInitResources returns immediately. spawnRemainingResources first builds
# the shared available-tile array (animals need it), then its guard jumps over
# only the tree population block to the first animal-spawn instruction.
RESOURCE_RELOAD_GUARD_PREFIX = bytes.fromhex(
    "60 01 66 21 66 c1 01 66 81 ba 01 25 80 01 66 9f 02 11"
)

INIT_RESOURCE_METHOD_ORIGINAL = bytes.fromhex(
    "ce 12 08 09 03 04 9c 05 "
    "d0 30 24 00 10 0d 00 00 c2 03 d2 96 92 02 08 02 c2 05 ae 57 d6 d5"
)
INIT_RESOURCE_METHOD_PATCHED = (
    bytes.fromhex("ce 12 08 09 03 04 b1 05 d0 30")
    + RESOURCE_RELOAD_GUARD_PREFIX
    # Old length is 668; returnvoid is at 667. After insertion it is at 688,
    # and iftrue ends at new offset 23: 688 - 23 = 665 (0x299).
    + bytes.fromhex("99 02 00")
    + INIT_RESOURCE_METHOD_ORIGINAL[10:]
)

REMAINING_RESOURCE_METHOD_ORIGINAL = bytes.fromhex(
    "cf 12 08 09 03 04 b5 06 "
    "d0 30 24 00 10 0e 00 00 c2 06 a7 c3 06 c2 06 08 07 c6 d6 d0 92 04 d6"
)
REMAINING_RESOURCE_METHOD_PATCHED = (
    bytes.fromhex("cf 12 08 09 03 04 ca 06")
    + REMAINING_RESOURCE_METHOD_ORIGINAL[8:]
)

# v10 put the same guard at method entry. That skipped creation of
# arAvailableAndBorderTiles, which the following animal spawning also uses.
# Keep an explicit upgrade signature so existing patched clients are repaired.
REMAINING_RESOURCE_METHOD_V10 = (
    bytes.fromhex("cf 12 08 09 03 04 ca 06 d0 30")
    + RESOURCE_RELOAD_GUARD_PREFIX
    + bytes.fromhex("a2 01 00")
    + REMAINING_RESOURCE_METHOD_ORIGINAL[10:]
)

REMAINING_TREE_BOUNDARY_ORIGINAL = bytes.fromhex(
    "32 07 06 11 e0 ff ff 08 07 08 06 "
    "60 d8 05 60 02 66 81 1b 66 9f 02 20"
)
REMAINING_TREE_BOUNDARY_PATCHED = (
    REMAINING_TREE_BOUNDARY_ORIGINAL[:11]
    + RESOURCE_RELOAD_GUARD_PREFIX
    # Tree code starts at old offset 157 and animals at 420. With the guard,
    # iftrue ends at 178 and the animal block starts at 441: 441 - 178 = 263.
    + bytes.fromhex("07 01 00")
    + REMAINING_TREE_BOUNDARY_ORIGINAL[11:]
)


def _uncompress(data: bytes) -> tuple[bytes, bytes]:
    signature = data[:3]
    if signature == b"CWS":
        return b"FWS" + data[3:8] + zlib.decompress(data[8:]), signature
    if signature == b"FWS":
        return data, signature
    raise ValueError(f"unsupported SWF signature: {signature!r}")


def _recompress(raw: bytes, signature: bytes) -> bytes:
    if signature == b"FWS":
        return raw
    return b"CWS" + raw[3:8] + zlib.compress(raw[8:], level=9)


def _swf_tag_containing(raw: bytes, offset: int) -> tuple[int, int, int]:
    """Return ``(header_offset, header_size, content_length)`` for offset."""
    nbits = raw[8] >> 3
    rect_size = (5 + 4 * nbits + 7) // 8
    pos = 8 + rect_size + 4  # RECT + frame rate + frame count
    while pos < len(raw):
        tag_header = struct.unpack_from("<H", raw, pos)[0]
        length = tag_header & 0x3F
        header_size = 2
        if length == 0x3F:
            length = struct.unpack_from("<I", raw, pos + 2)[0]
            header_size = 6
        if pos + header_size <= offset < pos + header_size + length:
            return pos, header_size, length
        pos += header_size + length
    raise ValueError(f"no SWF tag contains byte offset {offset}")


def _patch_resource_reload_guards(raw: bytes) -> tuple[bytes, bool]:
    """Insert the two per-town resource population guards."""
    patched_raw = raw
    changed = False
    delta = 0
    growth_offsets = []

    init_original = raw.count(INIT_RESOURCE_METHOD_ORIGINAL)
    init_patched = raw.count(INIT_RESOURCE_METHOD_PATCHED)
    if init_original == 1 and init_patched == 0:
        growth_offsets.append(raw.find(INIT_RESOURCE_METHOD_ORIGINAL))
        patched_raw = patched_raw.replace(
            INIT_RESOURCE_METHOD_ORIGINAL, INIT_RESOURCE_METHOD_PATCHED, 1
        )
        delta += 21
        changed = True
    elif not (init_original == 0 and init_patched == 1):
        raise ValueError(
            "initial resource population guard signature mismatch: "
            f"original={init_original}, patched={init_patched}"
        )

    remaining_original = raw.count(REMAINING_RESOURCE_METHOD_ORIGINAL)
    remaining_patched = raw.count(REMAINING_RESOURCE_METHOD_PATCHED)
    remaining_v10 = raw.count(REMAINING_RESOURCE_METHOD_V10)
    boundary_original = raw.count(REMAINING_TREE_BOUNDARY_ORIGINAL)
    boundary_patched = raw.count(REMAINING_TREE_BOUNDARY_PATCHED)

    if (remaining_original == 1 and remaining_patched == 0
            and remaining_v10 == 0 and boundary_original == 1
            and boundary_patched == 0):
        growth_offsets.append(raw.find(REMAINING_TREE_BOUNDARY_ORIGINAL))
        patched_raw = patched_raw.replace(
            REMAINING_RESOURCE_METHOD_ORIGINAL,
            REMAINING_RESOURCE_METHOD_PATCHED, 1
        )
        patched_raw = patched_raw.replace(
            REMAINING_TREE_BOUNDARY_ORIGINAL,
            REMAINING_TREE_BOUNDARY_PATCHED, 1
        )
        delta += 21
        changed = True
    elif (remaining_original == 0 and remaining_patched == 1
          and remaining_v10 == 0 and boundary_original == 0
          and boundary_patched == 1):
        pass
    elif (remaining_original == 0 and remaining_patched == 0
          and remaining_v10 == 1 and boundary_original == 1
          and boundary_patched == 0):
        # Relocate the v10 guard without changing the method/tag length.
        patched_raw = patched_raw.replace(
            REMAINING_RESOURCE_METHOD_V10,
            REMAINING_RESOURCE_METHOD_PATCHED, 1
        )
        patched_raw = patched_raw.replace(
            REMAINING_TREE_BOUNDARY_ORIGINAL,
            REMAINING_TREE_BOUNDARY_PATCHED, 1
        )
        changed = True
    else:
        raise ValueError(
            "remaining resource population guard signature mismatch: "
            f"original={remaining_original}, patched={remaining_patched}, "
            f"v10={remaining_v10}, boundary_original={boundary_original}, "
            f"boundary_patched={boundary_patched}"
        )

    if delta:
        tags = {_swf_tag_containing(raw, offset) for offset in growth_offsets}
        if len(tags) != 1:
            raise ValueError(
                "resource methods are unexpectedly in different SWF tags"
            )
        tag_offset, tag_header_size, tag_length = tags.pop()
        if tag_header_size != 6:
            raise ValueError(
                "resource method DoABC tag does not use a long header"
            )
        patched_raw = bytearray(patched_raw)
        struct.pack_into("<I", patched_raw, tag_offset + 2, tag_length + delta)
        struct.pack_into("<I", patched_raw, 4, len(patched_raw))
        patched_raw = bytes(patched_raw)

    return patched_raw, changed


def patch_swf_bytes(data: bytes) -> tuple[bytes, bool]:
    """Return ``(patched_data, changed)``; accept an already-patched SWF."""
    raw, signature = _uncompress(data)
    patched_raw = raw
    changed = False

    patched_raw, resource_guards_changed = _patch_resource_reload_guards(
        patched_raw
    )
    changed |= resource_guards_changed

    # Upgrade v1-v7, which jumped over the original ground-move block but did
    # not replace it with an attack call. Match the complete condition and
    # body, not the short jump sequence, so unrelated bytecode cannot match.
    ground_v7_count = patched_raw.count(GROUND_ATTACK_V7)
    ground_current_count = patched_raw.count(GROUND_ATTACK_PATCHED)
    if ground_v7_count == 1 and ground_current_count == 0:
        patched_raw = patched_raw.replace(
            GROUND_ATTACK_V7, GROUND_ATTACK_PATCHED, 1
        )
        changed = True
    elif ground_v7_count not in (0, 1) or ground_current_count not in (0, 1):
        raise ValueError(
            "enemy click attack-dispatch upgrade signature mismatch: "
            f"v7={ground_v7_count}, current={ground_current_count}"
        )

    # Upgrade clients produced by the earlier direct-attack patches. Those
    # versions left STATE_FORCE_MOVE active and also replaced contextual
    # villager actions. Match the full state+action region so a short byte
    # sequence elsewhere in the SWF can never be changed accidentally.
    dispatch_current_count = patched_raw.count(OBJECTIVE_DISPATCH_PATCHED)
    legacy_dispatches = (
        ("v2", OBJECTIVE_DISPATCH_V2),
        ("v3-v5", OBJECTIVE_DISPATCH_V3_TO_V5),
        ("v6-v10", OBJECTIVE_DISPATCH_V6_TO_V10),
    )
    legacy_counts = {name: patched_raw.count(signature)
                     for name, signature in legacy_dispatches}
    if dispatch_current_count == 0 and sum(legacy_counts.values()) == 1:
        legacy_name = next(name for name, count in legacy_counts.items() if count == 1)
        legacy_signature = dict(legacy_dispatches)[legacy_name]
        patched_raw = patched_raw.replace(
            legacy_signature, OBJECTIVE_DISPATCH_PATCHED, 1
        )
        changed = True
    elif (dispatch_current_count not in (0, 1)
          or any(count not in (0, 1) for count in legacy_counts.values())
          or sum(legacy_counts.values()) > 1):
        raise ValueError(
            "contextual enemy dispatch upgrade signature mismatch: "
            f"legacy={legacy_counts}, current={dispatch_current_count}"
        )

    # Upgrade the v4 ranged-stop patch, which called cancelMovement on every
    # in-range update and therefore prevented attack animations from completing.
    v4_count = patched_raw.count(RANGED_STOP_V4)
    ranged_current_count = patched_raw.count(RANGED_STOP_PATCHED)
    if v4_count == 1 and ranged_current_count == 0:
        patched_raw = patched_raw.replace(
            RANGED_STOP_V4, RANGED_STOP_PATCHED, 1
        )
        changed = True
    elif v4_count not in (0, 1) or ranged_current_count not in (0, 1):
        raise ValueError(
            "ranged attack active segment upgrade signature mismatch: "
            f"v4={v4_count}, current={ranged_current_count}"
        )

    for name, original, patched in PATCHES:
        original_count = patched_raw.count(original)
        patched_count = patched_raw.count(patched)
        if original_count == 0 and patched_count == 1:
            continue
        if original_count != 1 or patched_count != 0:
            raise ValueError(
                f"{name} signature mismatch: "
                f"original={original_count}, patched={patched_count}"
            )
        patched_raw = patched_raw.replace(original, patched, 1)
        changed = True

    if not changed:
        return data, False
    # Equal-length gameplay rewrites preserve size; the resource reload guards
    # update both their DoABC tag and the uncompressed SWF size above.
    declared_size = struct.unpack_from("<I", patched_raw, 4)[0]
    if declared_size != len(patched_raw):
        raise ValueError(
            f"invalid SWF length after patch: header={declared_size}, actual={len(patched_raw)}"
        )
    return _recompress(patched_raw, signature), changed


def is_patched(data: bytes) -> bool:
    raw, _ = _uncompress(data)
    equal_length_patches = all(
        raw.count(patched) == 1 and raw.count(original) == 0
        for _, original, patched in PATCHES
    )
    reload_guards = all(
        condition for condition in (
            raw.count(INIT_RESOURCE_METHOD_PATCHED) == 1,
            raw.count(INIT_RESOURCE_METHOD_ORIGINAL) == 0,
            raw.count(REMAINING_RESOURCE_METHOD_PATCHED) == 1,
            raw.count(REMAINING_RESOURCE_METHOD_ORIGINAL) == 0,
            raw.count(REMAINING_RESOURCE_METHOD_V10) == 0,
            raw.count(REMAINING_TREE_BOUNDARY_PATCHED) == 1,
            raw.count(REMAINING_TREE_BOUNDARY_ORIGINAL) == 0,
        )
    )
    return equal_length_patches and reload_guards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("swf", nargs="?", type=Path, default=DEFAULT_SWF)
    parser.add_argument(
        "--check", action="store_true", help="verify the patch without changing the file"
    )
    args = parser.parse_args()

    data = args.swf.read_bytes()
    if args.check:
        if not is_patched(data):
            raise SystemExit(f"not patched: {args.swf}")
        print(f"Gameplay SWF patches present: {args.swf}")
        return

    patched, changed = patch_swf_bytes(data)
    if changed:
        args.swf.write_bytes(patched)
        print(f"Patched gameplay client: {args.swf}")
    else:
        print(f"Already patched: {args.swf}")


if __name__ == "__main__":
    main()
