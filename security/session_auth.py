"""Bind signed login cookies to the exact credential that was verified.

The keyed digest is safe to include in Flask's readable signed cookie. Never
store the password or its stored verifier in that cookie. Changing the stored
hash (including an administrator reset) revokes every prior binding without a
schema migration. Cookies issued before this binding existed must log in again.
"""

import hashlib
import hmac

from flask import current_app


def credential_binding(user_id, password_hash, *, secret_key=None):
    key = current_app.secret_key if secret_key is None else secret_key
    if not key or not password_hash:
        raise ValueError("A configured signing key and credential are required.")
    if isinstance(key, str):
        key = key.encode("utf-8")
    payload = f"csd-login-credential-v1\0{user_id}\0{password_hash}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def credential_matches(user_id, password_hash, binding, *, secret_key=None):
    # Missing, malformed and legacy bindings fail closed, including in tests.
    if not isinstance(binding, str) or len(binding) != 64 or not binding.isascii():
        return False
    if not password_hash:
        return False
    return hmac.compare_digest(
        binding, credential_binding(user_id, password_hash, secret_key=secret_key)
    )


def authenticated_session_user(conn, browser_session):
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (browser_session.get("user_id"),)
    ).fetchone()
    if not user or not user["is_active"] or not credential_matches(
        user["id"], user["password_hash"], browser_session.get("credential_binding")
    ):
        return None
    return user


def establish_authenticated_session(browser_session, user):
    """Only call after password verification or a successful conditional change."""
    browser_session.clear()
    browser_session.update(
        user_id=user["id"], username=user["username"], role=user["role"],
        requires_password_change=user["requires_password_change"],
        credential_binding=credential_binding(user["id"], user["password_hash"]),
    )
