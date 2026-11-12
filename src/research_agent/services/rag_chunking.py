"""Boundary-aware chunks that are exact slices of the original Unicode text."""

import re
from bisect import bisect_left, bisect_right

from .rag_english import (
    MAX_CHARS,
    SENTENCIZER_VERSION,
    WORD_LIMIT,
    WORD_OVERLAP,
    EnglishText,
    is_english_text,
)

VERSION = "boundary-v2-spacy"
TEXT_INDEX_VERSION = "4"
DEFAULT_SIZE = 1800
DEFAULT_OVERLAP = 200

_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SENTENCE = re.compile(r"""[。！？；][”’"）\]]*|[.!?;][”’"')\]]*(?=\s|$)""")
_PUNCTUATION = re.compile(r"[,，、:：](?:[ \t]*)")


def _structures(text):
    """Recognize fenced code and pipe tables; never rewrite Markdown to balance fragments."""
    lines = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[^\n]+(?:\n|$)|\n", text)]
    blocks = []
    i = 0
    while i < len(lines):
        begin, _, line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            marker = fence[1]
            close = re.compile(
                r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\r?\n?$"
            )
            j = i + 1
            while j < len(lines) and not close.fullmatch(lines[j][2]):
                j += 1
            j = min(j, len(lines) - 1)
            blocks.append((begin, lines[j][1], "code"))
            i = j + 1
        elif i + 1 < len(lines) and "|" in line and _TABLE_SEPARATOR.fullmatch(lines[i + 1][2]):
            j = i + 2
            while j < len(lines) and lines[j][2].strip() and "|" in lines[j][2]:
                j += 1
            blocks.append((begin, lines[j - 1][1], "table"))
            i = j
        else:
            i += 1
    return blocks


class _Boundaries:
    def __init__(self, text, size, *, english=None, word_budget=False):
        self.words = english if word_budget else None
        self.blocks = _structures(text)
        self.block_starts = [block[0] for block in self.blocks]
        self.atomic = [block for block in self.blocks if self.fits(block[0], block[1], size)]
        self.atomic_starts = [block[0] for block in self.atomic]
        paragraphs = [m.end() for m in re.finditer(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*", text)]
        paragraphs += [m.start() for m in re.finditer(r"(?m)^ {0,3}#{1,6}[ \t]+", text)]
        paragraphs += [point for block in self.blocks for point in block[:2]]
        self.groups = {
            "paragraph": self.safe(paragraphs),
            "sentence": self.safe(
                english.sentences if english else (m.end() for m in _SENTENCE.finditer(text))
            ),
            "line": self.safe(m.end() for m in re.finditer(r"\n", text)),
            "word": self.safe(
                english.boundaries if english else (m.end() for m in re.finditer(r"\s+", text))
            ),
            "punctuation": self.safe(m.end() for m in _PUNCTUATION.finditer(text)),
        }
        self.restart_sentences = (
            self.safe(english.sentence_starts + english.sentences) if english else self.groups["sentence"]
        )

    def fits(self, begin, end, size):
        return self.words.fits(begin, end) if self.words else end - begin <= size

    @staticmethod
    def inside(position, blocks, starts):
        i = bisect_right(starts, position) - 1
        if i >= 0 and blocks[i][0] < position < blocks[i][1]:
            return blocks[i]
        return None

    def safe(self, points):
        return sorted({p for p in points if not self.inside(p, self.atomic, self.atomic_starts)})

    def last(self, kind, begin, end):
        points = self.groups[kind]
        index = bisect_right(points, end) - 1
        return points[index] if index >= 0 and points[index] >= begin else None

    def end(self, start, size, length):
        cap = self.words.cap(start) if self.words else min(start + size, length)
        if cap == length:
            return cap, "end"
        block = self.inside(cap, self.blocks, self.block_starts)
        if block:
            if block[0] > start:
                # Put a structure in the next chunk if it cannot fit in the remaining space.
                return block[0], "structure"
            # Oversized structure: prefer complete code lines / table rows.
            line = self.last("line", start + 1, cap)
            return (line, "structure_line") if line else (cap, "hard")
        # Avoid very short fragments just because an early paragraph boundary exists.
        minimum = (
            self.words.cap(start, max(1, self.words.limit // 2)) if self.words else start + max(1, size // 2)
        )
        if self.words:
            # Prefer a short complete sentence over filling the budget with half of the next sentence.
            for kind, begin in (("paragraph", minimum), ("sentence", start + 1), ("paragraph", start + 1)):
                point = self.last(kind, begin, cap)
                if point is not None:
                    return point, kind
        for kind in ("paragraph", "sentence", "line", "word", "punctuation"):
            point = self.last(kind, minimum, cap)
            if point is not None:
                return point, kind
        # A short complete sentence is preferable to cutting a subsequent long word.
        for kind in ("paragraph", "sentence", "line", "word", "punctuation"):
            point = self.last(kind, start + 1, cap)
            if point is not None:
                return point, kind
        return cap, "hard"

    def restart(self, start, end, overlap):
        target = self.words.restart(start, end) if self.words else max(start + 1, end - overlap)
        if target >= end:
            return end
        if self.inside(target, self.blocks, self.block_starts):
            # Code/table continuation starts on a row boundary, never in a row's words.
            points = self.groups["line"]
            index = bisect_left(points, target)
            if index < len(points) and points[index] < end:
                return points[index]
            return end
        for kind in ("paragraph", "sentence", "line", "word", "punctuation"):
            points = self.restart_sentences if kind == "sentence" else self.groups[kind]
            index = bisect_left(points, target)
            if index < len(points) and points[index] < end:
                return points[index]
        # No natural boundary, e.g. a single overlong identifier or unpunctuated text.
        return target


def chunks(
    text, size=None, overlap=None, *, english_words=WORD_LIMIT, english_overlap=WORD_OVERLAP, diagnostics=None
):
    # Explicit size/overlap arguments retain the existing character-budget API.
    character_override = size is not None or overlap is not None
    size = DEFAULT_SIZE if size is None else size
    overlap = DEFAULT_OVERLAP if overlap is None else overlap
    if not isinstance(size, int) or not isinstance(overlap, int) or size <= 0 or not 0 <= overlap < size:
        raise ValueError("Chunk size must be positive and overlap must be between 0 and size-1")
    if (
        not isinstance(english_words, int)
        or not isinstance(english_overlap, int)
        or english_words <= 0
        or not 0 <= english_overlap < english_words
    ):
        raise ValueError("English word limit must be positive and overlap must be between 0 and limit-1")
    stats = diagnostics if diagnostics is not None else {}
    stats.setdefault("version", VERSION)
    stats.setdefault("sentencizer_version", SENTENCIZER_VERSION)
    stats.setdefault("english_word_limit", english_words)
    stats.setdefault("english_overlap", english_overlap)
    stats.setdefault("english_max_chars", MAX_CHARS)
    for key in (
        "chunks",
        "hard_splits",
        "oversized_code_blocks",
        "oversized_tables",
        "excluded_image_payloads",
        "english_word_chunks",
        "character_chunks",
        "sentencizer_windows",
    ):
        stats.setdefault(key, 0)
    spans, cursor = [], 0
    for image in _IMAGE.finditer(text):
        spans.append((cursor, image.start()))
        cursor = image.end()
        stats["excluded_image_payloads"] += 1
    spans.append((cursor, len(text)))
    for begin, end in spans:
        source = text[begin:end]
        if not source.strip():
            continue
        english = EnglishText(source, english_words, english_overlap) if is_english_text(source) else None
        word_budget = english is not None and not character_override
        if english:
            stats["sentencizer_windows"] += english.window_count
        boundaries = _Boundaries(source, size, english=english, word_budget=word_budget)
        for left, right, kind in boundaries.blocks:
            if not boundaries.fits(left, right, size):
                stats["oversized_code_blocks" if kind == "code" else "oversized_tables"] += 1
        start, previous_end = 0, 0
        while start < len(source):
            stop, reason = boundaries.end(start, size, len(source))
            if stop <= previous_end:
                # Overlap must not consume space needed by the next protected block.
                start = previous_end
                continue
            value = source[start:stop]
            if value.strip():
                stats["chunks"] += 1
                stats["english_word_chunks" if word_budget else "character_chunks"] += 1
                stats["hard_splits"] += reason == "hard"
                yield begin + start, value
            previous_end = stop
            if stop == len(source):
                break
            start = boundaries.restart(start, stop, overlap)
