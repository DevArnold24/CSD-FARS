"""Routing constraints shared by database-backed record pages and APIs."""
from werkzeug.routing import IntegerConverter, ValidationError


def parse_record_id(value):
    """Accept a database ID or its form text without boolean/float coercion."""
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > 19 or not text.isascii() or not text.isdecimal():
            return None
        parsed = int(text)
    else:
        return None
    return parsed if 0 < parsed <= 2**63 - 1 else None


class RecordIdConverter(IntegerConverter):
    # Every integer route parameter in this application is a SQLite record ID.
    # Bound the text before int() as well as the number before SQLite binding.
    regex = r"[0-9]{1,19}"

    def to_python(self, value):
        result = super().to_python(value)
        if not 0 < result <= 2**63 - 1:
            raise ValidationError()
        return result
