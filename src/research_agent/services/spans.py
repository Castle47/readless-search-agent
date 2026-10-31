"""One bounded quote repair: restore source whitespace, never invent source wording."""

import re


def locate_quote(text, quote, start, end):
    if 0 <= start < end <= len(text) and text[start:end] == quote:
        return start, end, quote, False

    def normalized(value):
        chars = []
        positions = []
        for match in re.finditer(r"\s+|\S", value):
            chars.append(" " if match.group().isspace() else match.group())
            positions.append((match.start(), match.end()))
        return "".join(chars), positions

    haystack, positions = normalized(text)
    needle, _ = normalized(quote)
    needle = needle.strip()
    offset = haystack.find(needle)
    if not needle or offset < 0 or haystack.find(needle, offset + 1) >= 0:
        return None
    a = positions[offset][0]
    b = positions[offset + len(needle) - 1][1]
    restored = text[a:b]
    if len(restored) > 1200:
        return None
    return a, b, restored, True
