"""Exact comparison equivalence for numeric and spoken German named dates."""
from __future__ import annotations

import re
import unicodedata

MONTH = ("januar", "februar", "märz", "april", "mai", "juni", "juli",
         "august", "september", "oktober", "november", "dezember")


def _ordinal(day: int) -> str:
    names = {
        1: "erster", 2: "zweiter", 3: "dritter", 4: "vierter", 5: "fünfter",
        6: "sechster", 7: "siebter", 8: "achter", 9: "neunter", 10: "zehnter",
        11: "elfter", 12: "zwölfter", 13: "dreizehnter", 14: "vierzehnter",
        15: "fünfzehnter", 16: "sechzehnter", 17: "siebzehnter", 18: "achtzehnter",
        19: "neunzehnter",
    }
    if day in names:
        return names[day]
    units = ("", "ein", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun")
    tens = {2: "zwanzig", 3: "dreißig"}[day // 10]
    return (units[day % 10] + "und" if day % 10 else "") + tens + "ster"


_FORMS: dict[str, tuple[int, str]] = {}
for _day in range(1, 32):
    _base = _ordinal(_day)
    for _form, _ending in ((_base, "base"), (_base[:-2] + "e", "der"),
                           (_base[:-2] + "en", "weak")):
        _FORMS[_form.casefold().replace("ß", "ss")] = (_day, _ending)

_MONTH_PATTERN = "|".join(MONTH)
_DATE = re.compile(
    rf"(?<![\w.,:/+−-])(?P<day>[1-9]|[12]\d|3[01])\.(?:\s*)"
    rf"(?P<month>{_MONTH_PATTERN})\b|"
    rf"(?<![\w.,:/+−-])(?P<ordinal>[\wäöüß]+)\s+"
    rf"(?P<spoken_month>{_MONTH_PATTERN})\b",
    re.IGNORECASE | re.UNICODE,
)


def canonical_named_dates(value: str) -> str:
    """Normalize only exact day/month pairs (numeric ordinal or spelled ordinal)."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ß", "ss")

    def replace(match: re.Match[str]) -> str:
        if match.group("day") is not None:
            day, month = int(match.group("day")), match.group("month")
        else:
            ordinal = match.group("ordinal")
            found = _FORMS.get(ordinal)
            if found is None:
                return match.group(0)
            day, form = found
            prefix = value[:match.start()]
            if form == "der" and not re.search(r"\bder\s+$", prefix):
                return match.group(0)
            if form == "weak" and not re.search(r"\b(?:am|vom|den|bis\s+zum)\s+$", prefix):
                return match.group(0)
            month = match.group("spoken_month")
        return f"{day} {month.casefold()}"

    return _DATE.sub(replace, value)


def dates_equivalent(left: str, right: str) -> bool:
    """Return true only when full texts become equal after named-date conversion."""
    def plain(value: str) -> str:
        value = canonical_named_dates(value)
        value = "".join(c if c.isalnum() or c.isspace() else " " for c in value)
        return " ".join(value.split())

    return plain(left) == plain(right)


def has_named_date_conversion(left: str, right: str) -> bool:
    """Require equality that actually depends on converting a named date."""
    def plain(value: str, convert: bool) -> str:
        if convert:
            value = canonical_named_dates(value)
        value = unicodedata.normalize("NFKC", value).casefold().replace("ß", "ss")
        value = "".join(c if c.isalnum() or c.isspace() else " " for c in value)
        return " ".join(value.split())

    return plain(left, False) != plain(right, False) and plain(left, True) == plain(right, True)
