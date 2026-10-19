"""Is a quote really in the text? Loops (engine) and checks (investigation) keep only words that are."""

import re


def alnum(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def appears(needle: str, text: str, phone: bool = False) -> bool:
    """Is `needle` in `text`, comparing letters and digits only? Pages write the same name differently
    ("Sdn. Bhd." vs "Sdn.Bhd", table pipes). Phones compare their last 9 digits (at least 6), so
    "+60 12-000 5678" matches "012-000 5678"."""
    if phone:
        # ponytail: last-9-digits match covers +60 vs 0 prefixes; use a phone library if other formats appear.
        digits = re.sub(r"\D", "", needle)[-9:]
        return len(digits) >= 6 and digits in re.sub(r"\D", "", text)
    return bool(alnum(needle)) and alnum(needle) in alnum(text)
