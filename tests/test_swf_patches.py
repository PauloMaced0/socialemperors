"""Regression checks for bytecode fixes applied to the bundled game client.

Run from the repository root:

    /path/to/.venv/bin/python tests/test_swf_patches.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.patch_attack_click_swf import DEFAULT_SWF, is_patched, patch_swf_bytes


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


def test_natural_stone_and_gold_do_not_install_regrowth_timers():
    data = DEFAULT_SWF.read_bytes()
    assert is_patched(data), (
        "stone/gold harvest still creates an ID 80/81 regeneration object; "
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


def test_attack_click_patch_is_idempotent():
    data = DEFAULT_SWF.read_bytes()
    result, changed = patch_swf_bytes(data)
    assert not changed, "an already-patched client was changed again"
    assert result == data, "idempotent patch altered compressed SWF bytes"


TESTS = [
    test_enemy_click_dispatches_an_attack_instead_of_a_ground_move,
    test_action_clicks_are_consumed_before_the_map_click_handler,
    test_natural_stone_and_gold_do_not_install_regrowth_timers,
    test_established_town_does_not_repopulate_resources_on_reload,
    test_remove_tool_accepts_deployed_units_with_confirmation,
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
