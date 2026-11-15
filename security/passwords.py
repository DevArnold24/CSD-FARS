"""Shared password policy for bootstrap, provisioning, and forced reset."""


def validate_password_complexity(password):
    if not password:
        return False, "Password cannot be empty."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not any(character.isupper() for character in password):
        return False, "Password must contain at least one uppercase letter."
    if not any(character.islower() for character in password):
        return False, "Password must contain at least one lowercase letter."
    if not any(character.isdigit() for character in password):
        return False, "Password must contain at least one digit."
    if not any(not character.isalnum() for character in password):
        return False, "Password must contain at least one special character."
    return True, ""


from functools import lru_cache
import sys
import unicodedata


@lru_cache(maxsize=1)
def browser_password_policy():
    """Public character ranges from this server's exact Python Unicode semantics.

    No password enters this function. Cache once per process, and render only on
    new-password forms: JS Unicode categories alone differ from str.isdigit,
    str.isalnum and the server's Unicode version.
    """
    rules = {}
    for name, predicate in (("upper", str.isupper), ("lower", str.islower),
                            ("digit", str.isdigit), ("alnum", str.isalnum)):
        ranges = []
        for value in range(sys.maxunicode + 1):
            if predicate(chr(value)):
                if ranges and ranges[-1][1] == value - 1:
                    ranges[-1][1] = value
                else:
                    ranges.append([value, value])
        rules[name] = ranges
    return {"minimum": 8, "unicode_version": unicodedata.unidata_version, **rules}
