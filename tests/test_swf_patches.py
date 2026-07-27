"""Regression checks for bytecode fixes applied to the bundled game client.

Run from the repository root:

    /path/to/.venv/bin/python tests/test_swf_patches.py
"""

import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.patch_attack_click_swf import (
    DEFAULT_SWF,
    gameplay_ui_fixes_present,
    is_patched,
    merged_ui_fixes_present,
    patch_swf_bytes,
)


def test_enemy_click_dispatches_an_attack_instead_of_a_ground_move():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "enemy click does not use the explicit attack dispatch, or the attack "
        "still retains an unwanted movement segment; "
        "run tools/patch_attack_click_swf.py"
    )


def test_action_clicks_are_consumed_before_the_map_click_handler():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "an object or reward-token action can still fall through to a second "
        "ground-move order; run tools/patch_attack_click_swf.py"
    )


def test_natural_stone_and_gold_defer_to_server_random_respawns():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "stone/gold harvest still creates a same-tile ID 80/81 placeholder; "
        "run tools/patch_attack_click_swf.py"
    )


def test_established_town_does_not_repopulate_resources_on_reload():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "MapInitializer still repopulates trees/stone/gold on every reload; "
        "run tools/patch_attack_click_swf.py"
    )


def test_remove_tool_accepts_deployed_units_with_confirmation():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "the Remove Tool still rejects deployed units or bypasses the normal "
        "sale confirmation; run tools/patch_attack_click_swf.py"
    )


def test_remote_ui_fixes_survive_the_binary_merge():
    data = DEFAULT_SWF.read_bytes()
    assert merged_ui_fixes_present(data), (
        "the merged SWF lost the formatted cash HUD, non-negative enemy "
        "count, or special-attack tooltip methods"
    )


def test_gameplay_ui_fixes_are_present():
    data = DEFAULT_SWF.read_bytes()
    assert gameplay_ui_fixes_present(data), (
        "the rebuilt client lost Escape deselection, stable friend-card hover, "
        "live recurring events, market trade repair, social goal progress, "
        "or safe friend paging"
    )


def test_unit_health_and_training_stable_client_fixes_are_present():
    data = DEFAULT_SWF.read_bytes()
    if data[:3] == b"CWS":
        data = b"FWS" + data[3:8] + zlib.decompress(data[8:])
    assert data.count(b"socialemperors-gameplay-behaviors-v26") == 1, \
        "the current gameplay persistence patch was not rebuilt into the SWF"
    assert data.count(b"socialemperors-training-stable-ui-v1") == 1, \
        "Training Stables still exposes the generic 0% / 0 gold producer UI"
    assert data.count(b"socialemperors-staffed-building-actions-v1") == 1, \
        "unstaffed training/actions are not locked in the game client"
    assert data.count(b"socialemperors-harbour-staffing-gate-v1") == 1, \
        "Ship Land is still available while the Harbor is unstaffed"
    assert data.count(
        b"socialemperors-cathedral-unstaffed-description-v2"
    ) == 1, "the unstaffed Cathedral still exposes Monk price/training UI"
    assert data.count(
        b"socialemperors-harbour-reload-staff-v1"
    ) == 1, "the Harbor still auto-completes staffing during map reload"
    assert data.count(
        b"socialemperors-mission-popup-crash-guard-v1"
    ) == 1, (
        "MissionPopup.misionCompletada can still throw #1009 and hang "
        "the quest/PvP result popup on 'Saving Results'"
    )


def test_attack_click_patch_is_idempotent():
    data = DEFAULT_SWF.read_bytes()
    result, changed = patch_swf_bytes(data)
    assert not changed, "an already-patched client was changed again"
    assert result == data, "idempotent patch altered compressed SWF bytes"


TESTS = [
    test_enemy_click_dispatches_an_attack_instead_of_a_ground_move,
    test_action_clicks_are_consumed_before_the_map_click_handler,
    test_natural_stone_and_gold_defer_to_server_random_respawns,
    test_established_town_does_not_repopulate_resources_on_reload,
    test_remove_tool_accepts_deployed_units_with_confirmation,
    test_remote_ui_fixes_survive_the_binary_merge,
    test_gameplay_ui_fixes_are_present,
    test_unit_health_and_training_stable_client_fixes_are_present,
    test_attack_click_patch_is_idempotent,
]


def main():
    passed = failed = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
