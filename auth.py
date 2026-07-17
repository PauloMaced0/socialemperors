"""Per-village password authentication.

Passwords are stored hashed in each village's save under
``playerInfo["password_hash"]`` and persisted through ``sessions.save_session``.
Villages created before this feature have no hash and are treated as
"password not set", so the player is asked to create one on first login.

The functions here are the only place that reads or writes the hash; the
web routes in ``server.py`` call into them and never touch the field
directly.
"""
from werkzeug.security import generate_password_hash, check_password_hash

from sessions import session, save_session

_HASH_KEY = "password_hash"
# werkzeug defaults to scrypt, which is unavailable on Python builds whose
# hashlib lacks OpenSSL scrypt support; pbkdf2 is always available.
_HASH_METHOD = "pbkdf2:sha256"


def has_password(USERID: str) -> bool:
    """True if the village exists and already has a password set."""
    save = session(USERID)
    return bool(save and save["playerInfo"].get(_HASH_KEY))


def set_password(USERID: str, password: str) -> bool:
    """Hash and persist a password for the village.

    Returns False (and changes nothing) if the village is unknown or the
    password is empty.
    """
    save = session(USERID)
    if save is None or not password:
        return False
    save["playerInfo"][_HASH_KEY] = generate_password_hash(password, method=_HASH_METHOD)
    save_session(USERID)
    return True


def check_password(USERID: str, password: str) -> bool:
    """True if ``password`` matches the village's stored hash."""
    save = session(USERID)
    if save is None:
        return False
    stored = save["playerInfo"].get(_HASH_KEY)
    if not stored:
        return False
    return check_password_hash(stored, password)


def change_password(USERID: str, old: str, new: str) -> bool:
    """Change the password if ``old`` verifies and ``new`` is non-empty."""
    if not new:
        return False
    if not check_password(USERID, old):
        return False
    return set_password(USERID, new)
