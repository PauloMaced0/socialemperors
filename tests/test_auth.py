"""Per-village password auth tests. Run from repo root:

    /path/to/.venv/bin/python tests/test_auth.py

No pytest dependency: plain asserts, prints PASS/FAIL, non-zero exit on
failure. Uses an isolated temporary saves dir; never touches ./saves.
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions
import auth

UID = "test-auth-0001"


def _template_save():
    save = json.load(open(os.path.join("villages", "initial.json")))
    save["playerInfo"]["pid"] = UID
    save["playerInfo"].pop("password_hash", None)  # start with no password
    return save


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="se_auth_test_")
    sessions.SAVES_DIR = tmp
    json.dump(_template_save(), open(os.path.join(tmp, f"{UID}.save.json"), "w"), indent=4)
    sessions.load_saved_villages()
    return tmp


# --- Tests ---------------------------------------------------------------

def test_legacy_village_has_no_password(tmp):
    assert auth.has_password(UID) is False, "fresh village should have no password"
    # and no password means login check fails (can't slip in with empty pw)
    assert auth.check_password(UID, "") is False
    assert auth.check_password(UID, "anything") is False


def test_set_password_then_check(tmp):
    assert auth.set_password(UID, "hunter2") is True
    assert auth.has_password(UID) is True
    assert auth.check_password(UID, "hunter2") is True
    assert auth.check_password(UID, "wrong") is False


def test_password_stored_hashed_not_plaintext(tmp):
    auth.set_password(UID, "s3cr3t")
    disk = json.load(open(os.path.join(tmp, f"{UID}.save.json")))
    stored = disk["playerInfo"]["password_hash"]
    assert stored, "hash not persisted to disk"
    assert "s3cr3t" not in stored, "password stored in plaintext!"
    assert stored != "s3cr3t"


def test_empty_password_rejected(tmp):
    assert auth.set_password(UID, "") is False
    assert auth.has_password(UID) is False


def test_unknown_village_rejected(tmp):
    assert auth.set_password("no-such-uid", "x") is False
    assert auth.check_password("no-such-uid", "x") is False
    assert auth.has_password("no-such-uid") is False


def test_change_password_requires_correct_current(tmp):
    auth.set_password(UID, "old")
    assert auth.change_password(UID, "wrong", "new") is False, "changed with wrong current pw"
    assert auth.check_password(UID, "old") is True, "old password should still work"
    assert auth.change_password(UID, "old", "new") is True
    assert auth.check_password(UID, "new") is True
    assert auth.check_password(UID, "old") is False


def test_change_to_empty_rejected(tmp):
    auth.set_password(UID, "old")
    assert auth.change_password(UID, "old", "") is False
    assert auth.check_password(UID, "old") is True


TESTS = [
    test_legacy_village_has_no_password,
    test_set_password_then_check,
    test_password_stored_hashed_not_plaintext,
    test_empty_password_rejected,
    test_unknown_village_rejected,
    test_change_password_requires_correct_current,
    test_change_to_empty_rejected,
]


def main():
    passed = failed = 0
    for t in TESTS:
        tmp = _fresh_env()
        try:
            t(tmp)
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
