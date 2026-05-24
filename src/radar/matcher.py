import re
import unicodedata

_CONFUSABLES = str.maketrans(
    {
        "a": "а", "b": "б", "c": "с", "e": "е", "h": "н", "i": "і",
        "k": "к", "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
        "α": "а", "ε": "е", "ι": "і", "κ": "к", "μ": "м", "ν": "н",
        "ο": "о", "π": "п", "ρ": "р", "τ": "т", "χ": "х",
    }
)

_RUN = re.compile(r"(.)\1+")


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().translate(_CONFUSABLES)
    s = "".join(ch for ch in s if ch.isalpha())
    return _RUN.sub(r"\1", s)


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    norm_text = _normalize(text)
    matched = []
    for kw in keywords:
        norm_kw = _normalize(kw)
        if norm_kw and norm_kw in norm_text:
            matched.append(kw)
    return matched
