from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedOptionSymbol:
    root: str
    expiry: str
    side_code: str
    strike_millis: int

    @property
    def occ_symbol(self) -> str:
        return f"{self.root}{self.expiry}{self.side_code}{self.strike_millis:08d}"

    @property
    def longbridge_symbol(self) -> str:
        return f"{self.root}{self.expiry}{self.side_code}{self.strike_millis}.US"


def parse_option_symbol(value: Any) -> ParsedOptionSymbol | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text.endswith(".US"):
        text = text[:-3]
    compact = "".join(text.split())
    marker_index = -1
    for index in range(len(compact) - 9):
        if compact[index : index + 6].isdigit() and compact[index + 6] in {"C", "P"}:
            marker_index = index
            break
    if marker_index < 1:
        return None
    root = compact[:marker_index]
    expiry = compact[marker_index : marker_index + 6]
    side_code = compact[marker_index + 6]
    strike_code = compact[marker_index + 7 :]
    if not root or not strike_code.isdigit():
        return None
    return ParsedOptionSymbol(root=root, expiry=expiry, side_code=side_code, strike_millis=int(strike_code))


def option_symbol_for_longbridge(value: Any) -> str:
    parsed = parse_option_symbol(value)
    if parsed is not None:
        return parsed.longbridge_symbol
    return str(value or "").strip()


def option_symbol_for_occ(value: Any) -> str:
    parsed = parse_option_symbol(value)
    if parsed is not None:
        return parsed.occ_symbol
    text = str(value or "").strip()
    return text[:-3] if text.upper().endswith(".US") else text


def option_symbol_for_usmart(value: Any) -> str:
    """uSMART uses an OCC-style option code (e.g. AAPL250815C00090000) with no
    market suffix — identical to the OCC form, so we reuse it. Equities pass
    through unchanged (the .US/.HK suffix is handled at the exchangeType layer)."""
    parsed = parse_option_symbol(value)
    if parsed is not None:
        return parsed.occ_symbol
    return str(value or "").strip()


def option_symbol_aliases(value: Any) -> set[str]:
    text = str(value or "").strip().upper()
    aliases = {text, text.removesuffix(".US")}
    parsed = parse_option_symbol(text)
    if parsed is not None:
        aliases.add(parsed.occ_symbol)
        aliases.add(parsed.longbridge_symbol)
        aliases.add(parsed.longbridge_symbol.removesuffix(".US"))
    return {item for item in aliases if item}
