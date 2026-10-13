import re
from html import escape

from emoji import replace_emoji

_CUSTOM_EMOJI_TAG = re.compile(r"(<tg-emoji\b[^>]*>.*?</tg-emoji>)", re.DOTALL)


def plain_label(text: str) -> str:
    return re.sub(r"\s+", " ", replace_emoji(text, replace="")).strip()


def html_label(text: str, formatted: str | None = None) -> str:
    if not formatted:
        return escape(plain_label(text))
    parts = _CUSTOM_EMOJI_TAG.split(formatted)
    return "".join(
        part if _CUSTOM_EMOJI_TAG.fullmatch(part) else replace_emoji(part, replace="")
        for part in parts
    ).strip()