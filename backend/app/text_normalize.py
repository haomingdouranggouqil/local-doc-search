from __future__ import annotations

from functools import lru_cache


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).replace("\x00", " ").split())


def search_variants(value: str | None) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []
    variants = [normalized]
    for config in ("s2t", "t2s"):
        converted = convert_chinese(normalized, config)
        if converted:
            variants.append(converted)
    return unique_preserve_order(variants)


def search_index_text(value: str | None) -> str:
    return "\n".join(search_variants(value))


def fts_query_expr(value: str | None) -> str:
    phrases = []
    for variant in search_variants(value):
        escaped = variant.replace('"', '""')
        phrases.append(f'"{escaped}"')
    return " OR ".join(phrases)


def convert_chinese(value: str, config: str) -> str:
    converter = opencc_converter(config)
    if converter is None:
        return value
    try:
        return normalize_text(converter.convert(value))
    except Exception:
        return value


@lru_cache(maxsize=4)
def opencc_converter(config: str):
    try:
        from opencc import OpenCC
    except Exception:
        return None
    try:
        return OpenCC(config)
    except Exception:
        return None


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
