"""Remove personal data (emails, phone numbers) from text that leaves our systems,
e.g. error messages sent to GitHub issues."""

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 10+ digits, optionally with +, spaces, dots, dashes or parentheses between them.
_PHONE = re.compile(r"\+?\d(?:[\s().-]*\d){9,}")


def redact(text: str) -> str:
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))
