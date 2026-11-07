"""Deterministic, provenance-preserving German text preparation for the prewizard."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


MAX_TEXT_LENGTH = 4000
MAX_INTEGER = 999_999_999


def default_rules() -> dict[str, Any]:
    return {
        "uppercase_enabled": True,
        "integer_enabled": True,
        "uppercase_exceptions": ["SCUMM"],
        "quantity_units": ["Flasche", "Flaschen", "Goldstück", "Goldstücke", "Haus", "Häuser", "Pirat", "Piraten", "Stück", "Stücke", "Taler"],
        "replacements": [],
    }


def create_rule(template: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create one of the fixed, validated rule templates used by the UI/API."""
    if not isinstance(fields, dict):
        raise ValueError("Regelfelder müssen ein Objekt sein")
    if template == "uppercase":
        allowed = {"enabled", "exceptions"}
        if set(fields) - allowed or not isinstance(fields.get("enabled", True), bool):
            raise ValueError("Ungültige ALL-CAPS-Regelfelder")
        exceptions = fields.get("exceptions", [])
        if not isinstance(exceptions, list) or any(not isinstance(x, str) or not x.strip() for x in exceptions):
            raise ValueError("Ausnahmen müssen nichtleere Wörter sein")
        return {"template": template, "enabled": fields.get("enabled", True), "exceptions": list(exceptions)}
    if template == "integer":
        allowed = {"enabled", "quantity_units"}
        if set(fields) - allowed or not isinstance(fields.get("enabled", True), bool):
            raise ValueError("Ungültige Zahlenregelfelder")
        units = fields.get("quantity_units", [])
        if not isinstance(units, list) or any(not isinstance(x, str) or not x.strip() for x in units):
            raise ValueError("Mengeneinheiten müssen nichtleere Wörter sein")
        return {"template": template, "enabled": fields.get("enabled", True), "quantity_units": list(units)}
    if template == "replace_phrase":
        allowed = {"id", "source", "replacement", "priority", "case_sensitive"}
        if set(fields) != allowed:
            raise ValueError("Ersetzungsregel braucht genau id, source, replacement, priority und case_sensitive")
        if any(not isinstance(fields[x], str) or not fields[x].strip() for x in ("id", "source")):
            raise ValueError("Ersetzungs-ID und Quelltext dürfen nicht leer sein")
        if not isinstance(fields["replacement"], str) or not isinstance(fields["case_sensitive"], bool):
            raise ValueError("Ungültige Ersetzungsfelder")
        if isinstance(fields["priority"], bool) or not isinstance(fields["priority"], int) or not 0 <= fields["priority"] <= 100:
            raise ValueError("Priorität muss eine ganze Zahl von 0 bis 100 sein")
        return {"template": template, **fields}
    if template == "case_exception":
        if set(fields) != {"word"} or not isinstance(fields["word"], str) or not fields["word"].strip():
            raise ValueError("Case-Ausnahme braucht genau ein nichtleeres word-Feld")
        return {"template": template, "word": fields["word"]}
    raise ValueError(f"Unbekannte Regelvorlage: {template}")


def _validate_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    value = default_rules()
    if rules is None:
        return value
    if not isinstance(rules, dict):
        raise ValueError("rules muss ein Objekt sein")
    allowed = set(value)
    if set(rules) - allowed:
        raise ValueError("Unbekannte Regelfelder")
    value.update(rules)
    if not isinstance(value["uppercase_enabled"], bool) or not isinstance(value["integer_enabled"], bool):
        raise ValueError("Regelschalter müssen boolesch sein")
    for key in ("uppercase_exceptions", "quantity_units"):
        if not isinstance(value[key], list) or any(not isinstance(x, str) or not x.strip() for x in value[key]):
            raise ValueError(f"{key} muss eine Liste nichtleerer Wörter sein")
    replacements = value["replacements"]
    if not isinstance(replacements, list):
        raise ValueError("replacements muss eine Liste sein")
    checked = []
    ids = set()
    for item in replacements:
        if not isinstance(item, dict):
            raise ValueError("Ersetzungsregel muss ein Objekt sein")
        raw_item = dict(item)
        if raw_item.pop("template", "replace_phrase") != "replace_phrase":
            raise ValueError("Ungültige Ersetzungsvorlage")
        entry = create_rule("replace_phrase", raw_item)
        if entry["id"] in ids:
            raise ValueError("Ersetzungs-IDs müssen eindeutig sein")
        ids.add(entry["id"])
        checked.append(entry)
    value["replacements"] = checked
    return value


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str
    rule_id: str
    kind: str
    precedence: int
    specificity: int


_TOKEN = re.compile(r"(?<!\w)[^\W\d_]+(?:['’\-][^\W\d_]+)*(?!\w)", re.UNICODE)
_NUMBER = re.compile(r"(?<![\w.,:/])[+\-−]?\d+(?![\w.,:/])")
_DATE = re.compile(r"(?<!\d)(?:\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{1,2}-\d{1,2})(?!\d)")
_TIME = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?: Uhr)?(?!\w)", re.IGNORECASE)
_DECIMAL = re.compile(r"(?<!\d)[+-]?\d+[.,]\d+(?!\d)")
_RANGE = re.compile(r"(?<!\d)[+-]?\d+\s*(?:-|–|—|bis)\s*[+-]?\d+(?!\d)", re.IGNORECASE)
_GROUPED_NUMBER = re.compile(r"(?<!\w)[+\-−]?\d{1,3}(?:[.'’]\d{3})+(?!\w)")
_ORDINAL = re.compile(r"(?<!\d)\d+\.(?=\s|$)")
_NAMED_DATE = re.compile(
    r"(?<![\w.,:/+\-−])(?P<day>[1-9]|[12]\d|3[01])\.\s+"
    r"(?P<month>Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)\b", re.IGNORECASE | re.UNICODE
)


def _day_ordinal(n: int) -> str:
    irregular = {
        1: "erster", 2: "zweiter", 3: "dritter", 4: "vierter", 5: "fünfter",
        6: "sechster", 7: "siebter", 8: "achter", 9: "neunter", 10: "zehnter",
        11: "elfter", 12: "zwölfter", 13: "dreizehnter", 14: "vierzehnter",
        15: "fünfzehnter", 16: "sechzehnter", 17: "siebzehnter",
        18: "achtzehnter", 19: "neunzehnter",
    }
    return irregular.get(n, integer_to_german(n) + "ster")


def _date_day_form(text: str, start: int, day: int) -> str:
    ordinal = _day_ordinal(day)
    # These explicit constructions take the weak masculine ordinal ending.
    prefix = text[:start]
    if re.search(r"\bder\s+$", prefix, re.IGNORECASE):
        return ordinal[:-2] + "e"
    if re.search(r"\b(?:am|vom|den|bis\s+zum)\s+$", prefix, re.IGNORECASE):
        return ordinal[:-2] + "en"
    return ordinal


def _number_under_100(n: int) -> str:
    units = ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"]
    teens = ["zehn", "elf", "zwölf", "dreizehn", "vierzehn", "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn"]
    tens = ["", "", "zwanzig", "dreißig", "vierzig", "fünfzig", "sechzig", "siebzig", "achtzig", "neunzig"]
    if n < 10:
        return units[n]
    if n < 20:
        return teens[n - 10]
    one, ten = n % 10, n // 10
    return ("" if one == 0 else "ein" if one == 1 else units[one]) + ("und" if one else "") + tens[ten]


def _under_1000(n: int) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(("ein" if hundreds == 1 else _number_under_100(hundreds)) + "hundert")
    if rest:
        parts.append(("ein" if rest == 1 else _number_under_100(rest)) + ("s" if rest == 1 and not hundreds else ""))
    return "".join(parts) or "null"


def integer_to_german(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int) or abs(n) > MAX_INTEGER:
        raise ValueError("Integer außerhalb des unterstützten Bereichs")
    if n < 0:
        return "minus " + integer_to_german(-n)
    if n < 1000:
        return _under_1000(n)
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = "eintausend" if thousands == 1 else _under_1000(thousands) + "tausend"
        return head + (_under_1000(rest) if rest else "")
    millions, rest = divmod(n, 1_000_000)
    head = ("eine Million" if millions == 1 else _under_1000(millions) + " Millionen")
    if rest:
        head += " " + integer_to_german(rest)
    return head


def _integer_number_edits(text: str, allow_sentence_final_period: bool = False):
    final_period = bool(allow_sentence_final_period and re.search(r"\s[+\-−]?\d+\.$", text))
    if final_period:
        text = text[:-1]
    special_spans = _special_number_spans(text)
    units = {unit.casefold() for unit in default_rules()["quantity_units"]}
    edits = []
    for match in _NUMBER.finditer(text):
        start, end = match.span()
        raw = match.group()
        if any(start < stop and begin < end for begin, stop, _ in special_spans):
            continue
        if end < len(text) and text[end] == ".":
            continue
        number = int(raw.replace("−", "-"))
        if abs(number) > MAX_INTEGER:
            continue
        unit_match = re.match(r"\s+([^\W\d_]+)", text[end:], re.UNICODE)
        explicit_unit = bool(unit_match and unit_match.group(1).casefold() in units)
        if 1000 <= abs(number) <= 2999 and not explicit_unit:
            continue
        edits.append((start, end, integer_to_german(number), number))
    return edits, final_period


def integer_number_words(text: str, *, allow_sentence_final_period: bool = False) -> set[str]:
    """Return number tokens implicated by TTS-eligible numerals in this text."""
    edits, _ = _integer_number_edits(text, allow_sentence_final_period)
    protected = set()
    for _, _, replacement, number in edits:
        for candidate in (number - 1, number, number + 1):
            if abs(candidate) <= MAX_INTEGER:
                phrase = integer_to_german(candidate)
                protected.update(token.casefold().replace("ß", "ss") for token in re.findall(r"[^\W_]+", phrase, re.UNICODE))
    return protected


def canonicalize_integer_numbers(text: str, *, allow_sentence_final_period: bool = False) -> str:
    """Spell unambiguous integers using the same rules as text preparation."""
    edits, final_period = _integer_number_edits(text, allow_sentence_final_period)
    if final_period:
        text = text[:-1]
    for start, end, replacement, _ in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text + ("." if final_period else "")


def _special_number_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for kind, pattern in (("date", _DATE), ("time", _TIME), ("decimal", _DECIMAL), ("range", _RANGE), ("grouped_number_choice", _GROUPED_NUMBER), ("ordinal_choice", _ORDINAL)):
        spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(text))
    spans.extend((m.start(), m.end(), "named_date") for m in _NAMED_DATE.finditer(text))
    return spans


def _collect_edits(text: str, rules: dict[str, Any]) -> tuple[list[_Edit], list[dict[str, Any]]]:
    candidates: list[_Edit] = []
    special_spans = _special_number_spans(text)
    named_dates = list(_NAMED_DATE.finditer(text))
    reviews: list[dict[str, Any]] = [
        {"kind": kind, "source_span": [start, end], "text": text[start:end]}
        for start, end, kind in special_spans
        if kind != "named_date" and not (rules["integer_enabled"] and kind == "ordinal_choice" and any(m.start() <= start and end <= m.end() for m in named_dates))
    ]
    exceptions = {x.casefold() for x in rules["uppercase_exceptions"]}
    # Specific, user-authored rules have higher precedence than the built-ins.
    for rule in rules["replacements"]:
        flags = 0 if rule["case_sensitive"] else re.IGNORECASE
        pattern = re.compile(re.escape(rule["source"]), flags)
        for match in pattern.finditer(text):
            candidates.append(_Edit(match.start(), match.end(), rule["replacement"], rule["id"], "replacement", 100, len(match.group())))
    if rules["uppercase_enabled"]:
        for match in _TOKEN.finditer(text):
            token = match.group()
            cased = [c for c in token if c.lower() != c.upper()]
            if len(cased) >= 2 and token.isupper() and token.casefold() not in exceptions:
                candidates.append(_Edit(match.start(), match.end(), token.lower(), "uppercase", "uppercase", 10, len(token)))
    if rules["integer_enabled"]:
        for match in named_dates:
            day = int(match.group("day"))
            candidates.append(_Edit(match.start("day"), match.end("day") + 1, _date_day_form(text, match.start(), day), "integer", "integer", 10, match.end("day") + 1 - match.start("day")))
        units = {unit.casefold() for unit in rules["quantity_units"]}
        for match in _NUMBER.finditer(text):
            start, end = match.span()
            raw = match.group()
            if any(start < e and s < end for s, e, _ in special_spans):
                continue
            # A period immediately after digits is a choice case, never an inferred ordinal.
            if end < len(text) and text[end] == ".":
                continue
            try:
                number = int(raw.replace("−", "-"))
            except ValueError:
                continue
            if abs(number) > MAX_INTEGER:
                reviews.append({"kind": "large_integer", "source_span": [start, end], "text": raw})
                continue
            after = text[end:]
            unit_match = re.match(r"\s+([^\W\d_]+)", after, re.UNICODE)
            explicit_unit = bool(unit_match and unit_match.group(1).casefold() in units)
            if 1000 <= abs(number) <= 2999 and not explicit_unit:
                reviews.append({"kind": "possible_year", "source_span": [start, end], "text": raw})
                continue
            candidates.append(_Edit(start, end, integer_to_german(number), "integer", "integer", 10, end - start))

    # Apply higher precedence first. Equal-ranked overlapping changes are left intact and reported.
    conflicts: list[dict[str, Any]] = []
    rejected: set[int] = set()
    for i, left in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            right = candidates[j]
            if left.start >= right.end or right.start >= left.end:
                continue
            if (left.precedence, left.specificity) == (right.precedence, right.specificity) and left.replacement != right.replacement:
                rejected.update((i, j))
                conflicts.append({"source_span": [min(left.start, right.start), max(left.end, right.end)], "rules": sorted({left.rule_id, right.rule_id}), "text": text[min(left.start, right.start):max(left.end, right.end)]})
    viable = [(i, e) for i, e in enumerate(candidates) if i not in rejected]
    chosen: list[_Edit] = []
    for _, edit in sorted(viable, key=lambda pair: (-pair[1].precedence, -pair[1].specificity, pair[1].start, pair[1].end, pair[1].rule_id)):
        if any(edit.start < old.end and old.start < edit.end for old in chosen):
            continue
        chosen.append(edit)
    return sorted(chosen, key=lambda e: e.start), reviews + conflicts


def _apply(text: str, edits: Iterable[_Edit]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    pieces = []
    changes = []
    source_map = []
    cursor = 0
    output_offset = 0
    for edit in edits:
        if edit.start > cursor:
            unchanged = text[cursor:edit.start]
            pieces.append(unchanged)
            source_map.append({"output_span": [output_offset, output_offset + len(unchanged)], "source_span": [cursor, edit.start]})
            output_offset += len(unchanged)
        pieces.append(edit.replacement)
        source_map.append({"output_span": [output_offset, output_offset + len(edit.replacement)], "source_span": [edit.start, edit.end]})
        changes.append({"rule_id": edit.rule_id, "kind": edit.kind, "source_span": [edit.start, edit.end], "source_text": text[edit.start:edit.end], "output_text": edit.replacement})
        output_offset += len(edit.replacement)
        cursor = edit.end
    if cursor < len(text):
        unchanged = text[cursor:]
        pieces.append(unchanged)
        source_map.append({"output_span": [output_offset, output_offset + len(unchanged)], "source_span": [cursor, len(text)]})
    return "".join(pieces), changes, source_map


def prepare_text(source: str, rules: dict[str, Any] | None = None, manual_text: str | None = None) -> dict[str, Any]:
    """Return display, TTS and QA text plus changes, review cases, and source provenance."""
    if not isinstance(source, str):
        raise ValueError("source muss Text sein")
    if len(source) > MAX_TEXT_LENGTH:
        raise ValueError(f"source überschreitet {MAX_TEXT_LENGTH} Zeichen")
    if manual_text is not None and (not isinstance(manual_text, str) or len(manual_text) > MAX_TEXT_LENGTH):
        raise ValueError("manual_text muss Text mit höchstens 4000 Zeichen sein")
    checked_rules = _validate_rules(rules)
    if manual_text is not None:
        result_text = manual_text
        changes = ([{"rule_id": "manual", "kind": "manual", "source_span": [0, len(source)], "source_text": source, "output_text": manual_text}] if manual_text != source else [])
        source_map = [{"output_span": [0, len(manual_text)], "source_span": [0, len(source)], "origin": "manual"}] if manual_text else []
        review_cases = []
    else:
        edits, review_cases = _collect_edits(source, checked_rules)
        result_text, changes, source_map = _apply(source, edits)
    return {
        "source_text": source,
        "display_text": source,
        "tts_text": result_text,
        "qa_expected_text": result_text,
        "changes": changes,
        "source_span_map": source_map,
        "review_cases": review_cases,
    }


def preview_rule(source: str, template: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Summarize matches with count, examples, and before/after for a fixed template."""
    rule = create_rule(template, fields)
    baseline = default_rules()
    baseline["uppercase_enabled"] = False
    baseline["integer_enabled"] = False
    if template == "uppercase":
        baseline["uppercase_enabled"] = rule["enabled"]
        baseline["uppercase_exceptions"] = rule["exceptions"]
    elif template == "integer":
        baseline["integer_enabled"] = rule["enabled"]
        baseline["quantity_units"] = rule["quantity_units"]
    elif template == "replace_phrase":
        baseline["replacements"] = [rule]
    elif template == "case_exception":
        baseline["uppercase_enabled"] = True
        baseline["uppercase_exceptions"] = [rule["word"]]
    result = prepare_text(source, baseline)
    changes = [c for c in result["changes"] if template != "case_exception" and c["rule_id"] == ("uppercase" if template == "uppercase" else "integer" if template == "integer" else rule["id"])]
    return {"template": template, "count": len(changes), "examples": changes[:5], "before_after": [{"before": c["source_text"], "after": c["output_text"], "source_span": c["source_span"]} for c in changes]}
