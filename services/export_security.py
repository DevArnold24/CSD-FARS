"""Safety helpers for files opened by spreadsheet applications."""


def spreadsheet_safe_text(value):
    """Return text that spreadsheet software cannot interpret as a formula."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
