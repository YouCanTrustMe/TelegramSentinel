"""Checks a model-written summary against the text it was written from, without a model.

A summary may only carry numbers and names its source carries. On 60 real posts
(2026-10-18) ministral-14b wrote 7 summaries with an invented or swapped fact, and over
10 days of prod summaries this check found more: "37 млрд доларів" and "199 стовпчиків"
no source gave, "Віктором Орбаном" for a post naming "Мадяр", "Томас Ахмет Гутцайт",
"ТЕЦ-1, ТЕЦ-2, ТЕЦ-5" for "усі три ТЕЦ", "у 2024 році" for "this year". It cannot see a
fact moved inside the source's own words (a fire put in the wrong town), so it is a
second line, not a judge."""
import re
from datetime import datetime, timezone

_CYRILLIC = re.compile(r"[а-яіїєґё]", re.IGNORECASE)
_SPACED_THOUSANDS = re.compile(r"(?<=\d)[    ](?=\d{3}(?!\d))")
_COMMA_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DOT_THOUSANDS = re.compile(r"(?<=\d)\.(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)\s*(тис|thousand|tisuć|млн|million|milijun|млрд|billion|milijard|bn|[kmb](?![a-z]))?", re.IGNORECASE)
_INTEGER = re.compile(r"\d+")
_SCALE = {"тис": 1e3, "thousand": 1e3, "tisuć": 1e3, "k": 1e3,
          "млн": 1e6, "million": 1e6, "milijun": 1e6, "m": 1e6,
          "млрд": 1e9, "billion": 1e9, "milijard": 1e9, "bn": 1e9, "b": 1e9}
_WORD = re.compile(r"[a-zа-яіїєґёčćžšđ'’ʼ]+", re.IGNORECASE)
# A capitalised Cyrillic word that does not open a sentence: a name, a place, an organisation.
_NAME = re.compile(r"(?<![.!?…:;«\"„“(\n—–-])(?<![.!?…:;«\"„“(\n—–-] )\b([А-ЯІЇЄҐЁ][а-яіїєґё'’ʼ]{3,})")
_UKRAINE = re.compile(r"україн|украин|ukrain|ukrajin", re.IGNORECASE)
# Names a model rightly reads out of "рф", "ворог", "столиця", "ВР", "МО".
_IMPLIED = re.compile(r"^(?:росі|київ|києв|верховн|рад[аиіу]|міноборон)", re.IGNORECASE)
# Consonant classes shared by Cyrillic and Latin spelling, merging the letters Ukrainian
# stems alternate (Ліга → Лізі, рука → руці, Київ → Києві): «Загребі» matches Zagreb,
# «Рієки» Rijeka. Vowels, й/j/y and apostrophes carry no weight.
_CLASSES = {
    **dict.fromkeys("бb", "b"), **dict.fromkeys("вwv", "v"), **dict.fromkeys("гґзжhgzž", "g"),
    **dict.fromkeys("дđd", "d"), **dict.fromkeys("кцчхcčćkq", "k"), **dict.fromkeys("лl", "l"),
    **dict.fromkeys("мm", "m"), **dict.fromkeys("нn", "n"), **dict.fromkeys("пp", "p"),
    **dict.fromkeys("рr", "r"), **dict.fromkeys("сшщsšx", "s"), **dict.fromkeys("тt", "t"),
    **dict.fromkeys("фf", "f"),
}
_SKELETON_LEN = 3

_NUMBER_WORDS = {
    1: ("один", "одна", "одне", "перш", "one", "first", "jedan", "jedna", "prv"),
    2: ("два", "дві", "двох", "двоє", "друг", "two", "second", "both", "dva", "dvije", "drug"),
    3: ("три", "трьох", "троє", "трет", "three", "third", "tri", "treć"),
    4: ("чотир", "четвер", "four", "četir"),
    5: ("п'ят", "п’ят", "five", "pet"),
    6: ("шість", "шести", "шост", "six", "šest"),
    7: ("сім", "семи", "сьом", "seven", "sedam"),
    8: ("вісім", "восьм", "eight", "osam"),
    9: ("дев'ят", "дев’ят", "nine", "devet"),
    10: ("десят", "ten", "deset"),
    11: ("одинадцят", "eleven", "jedanaest"),
    12: ("дванадцят", "twelve", "dvanaest"),
    13: ("тринадцят", "thirteen", "trinaest"),
    14: ("чотирнадцят", "fourteen", "četrnaest"),
    15: ("п'ятнадцят", "п’ятнадцят", "fifteen", "petnaest"),
    20: ("двадцят", "twenty", "dvadeset"),
    100: ("сотн", "hundred", "stotin"),
    1000: ("тисяч", "thousand", "tisuć"),
}


def _plain(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _numbers(text: str, separators: tuple[re.Pattern, ...]) -> list[tuple[str, str | None]]:
    """Every number in `text` as a plain decimal, with its scaled value when a unit
    follows it ("$12 тис." is 12 and 12000, "$80K" 80 and 80000)."""
    text = _SPACED_THOUSANDS.sub("", text)
    for pattern in separators:
        text = pattern.sub("", text)
    out = []
    for raw, unit in _NUMBER.findall(text):
        value = float(raw.replace(",", "."))
        out.append((_plain(value), _plain(value * _SCALE[unit.lower()]) if unit else None))
    return out


# Numbers a source writes in words: "п'ять тисяч" is 5000, "два з половиною" 2.5,
# "п'ята частина" 20%.
_WORD_SCALE = {"тисяч": 1e3, "тис": 1e3, "thousand": 1e3, "tisuć": 1e3,
               "мільйон": 1e6, "млн": 1e6, "million": 1e6, "milijun": 1e6,
               "мільярд": 1e9, "млрд": 1e9, "billion": 1e9, "milijard": 1e9}
_SCALE_AFTER = re.compile(r"\s+(" + "|".join(_WORD_SCALE) + r")")
_HALF_AFTER = re.compile(r"\s+(?:з половиною|and a half|i pol)")
_SHARES = {
    "50": ("половин", "half", "polovic"),
    "25": ("чверт", "quarter", "četvrtin"),
    "33": ("третин", "one third", "a third", "trećin"),
    "20": ("п'ята частин", "п’ята частин", "п'яту частин", "п’яту частин", "one fifth", "a fifth", "petin"),
    "10": ("десята частин", "десяту частин", "one tenth", "a tenth", "desetin"),
}


def _has_word(lowered: str, word: str) -> bool:
    """`word` starts a word of the text: "ten" in "often", "три" in "стрибок" or "half" in
    "behalf" would vouch for a number the post never gives."""
    return re.search(r"(?<![\w'’])" + re.escape(word), lowered) is not None


def _worded_numbers(lowered: str) -> set[str]:
    found = set()
    for value, words in _NUMBER_WORDS.items():
        for word in words:
            for match in re.finditer(r"(?<![\w'’])" + re.escape(word) + r"[\w'’]*", lowered):
                rest = lowered[match.end():]
                scale = _SCALE_AFTER.match(rest)
                if scale:
                    found.add(_plain(value * _WORD_SCALE[scale.group(1)]))
                if _HALF_AFTER.match(rest):
                    found.add(_plain(value + 0.5))
    for share, words in _SHARES.items():
        if any(_has_word(lowered, w) for w in words):
            found.add(share)
    return found


def _source_numbers(source: str) -> set[str]:
    # "18.10" is a date as often as a decimal, so its parts count on their own too.
    found = {_plain(float(n)) for n in _INTEGER.findall(source)}
    for separators in ((_COMMA_THOUSANDS,), (_DOT_THOUSANDS,), ()):
        for plain, scaled in _numbers(source, separators):
            found.add(plain)
            if scaled:
                found.add(scaled)
    for raw in re.findall(r"(\d+(?:[.,]\d+)?)" + _HALF_AFTER.pattern, source.lower()):
        found.add(_plain(float(raw.replace(",", ".")) + 0.5))
    return found | _worded_numbers(source.lower())


def _is_year(value: float) -> bool:
    return value.is_integer() and 1900 <= value <= 2100


# A model adds up or subtracts the source's own figures ("300 тис + 500 тис + 1 млн" →
# "1,8 млн", "500 грн, a 9 гривень менше" → "491 грн"). Years never take part: 2026 − 2
# must not vouch for an invented "2024". At most this many source figures are combined.
_SUM_OPERANDS = 12


def _is_sum(value: float, source_numbers: set[str]) -> bool:
    if _is_year(value):
        return False
    operands = sorted({float(n) for n in source_numbers if not _is_year(float(n)) and float(n) > 1})[:_SUM_OPERANDS]
    close = lambda total: abs(total - value) <= max(1e-6, abs(value) * 1e-9)
    for i, a in enumerate(operands):
        for j in range(i + 1, len(operands)):
            b = operands[j]
            if close(a + b) or close(abs(a - b)):
                return True
            if any(close(a + b + c) for c in operands[j + 1:]):
                return True
    return False


# English spelling of the same sounds: «Джонсон» is Johnson, «Шмигаль» Shmyhal, «Харків»
# Kharkiv. A Latin initial j is дж; an initial h before a consonant is the Croatian х
# (Hrvatska, Hvar); an h that only marks the letter before it (sh, kh, a silent oh) is no
# sound of its own; any other h is Ukrainian г. Ph is ф (Philip, Філіп).
_LATIN_J = re.compile(r"^j")
_LATIN_HARD_H = re.compile(r"^h(?=[^aeiouy])")
_LATIN_SILENT_H = re.compile(r"(?<=[^aeiouy])h|(?<=[aeiouy])h(?=[^aeiouy]|$)")


def _skeleton(word: str) -> str:
    word = word.lower()
    if _CYRILLIC.search(word):
        word = word.replace("дж", "ж")
    else:
        word = _LATIN_SILENT_H.sub("", _LATIN_HARD_H.sub("k", _LATIN_J.sub("ž", word.replace("ph", "f"))))
    return "".join(_CLASSES.get(c, "") for c in word)[:_SKELETON_LEN]


def unsupported_facts(summary: str, source: str) -> list[str]:
    """Numbers and names in `summary` that `source` does not carry, plus "Україна" when a
    non-Cyrillic source never mentions Ukraine. Empty when nothing new was brought in."""
    summary = summary.rstrip(" …")
    found: list[str] = []

    source_numbers = _source_numbers(source)
    lowered = source.lower()
    year = datetime.now(timezone.utc).year
    # A year next to this one is how a model writes "last year" / "next year".
    allowed = {"0", "1"} | {str(y) for y in (year - 1, year, year + 1)}
    for plain, scaled in _numbers(summary, ()):
        if plain in allowed or plain in source_numbers or (scaled and scaled in source_numbers):
            continue
        value = float(plain)
        words = _NUMBER_WORDS.get(int(value)) if value.is_integer() else None
        if words and any(_has_word(lowered, w) for w in words):
            continue
        if _is_sum(value, source_numbers) or (scaled and _is_sum(float(scaled), source_numbers)):
            continue
        if plain not in found:
            found.append(plain)

    if len(_CYRILLIC.findall(source)) >= 20:
        skeletons = {_skeleton(w) for w in _WORD.findall(source)}
        for match in _NAME.finditer(summary):
            if match.start() == 0:
                continue
            name = match.group(1)
            skeleton = _skeleton(name)
            if _UKRAINE.search(name) or _IMPLIED.match(name) or not skeleton:
                continue
            if not any(s.startswith(skeleton) for s in skeletons):
                found.append(name)
    elif _UKRAINE.search(summary) and not _UKRAINE.search(source):
        found.append("Україна")
    return found
