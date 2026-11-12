"""Local English tokenization/sentencizing; no trained pipeline or model downloads."""

import re
from bisect import bisect_left, bisect_right
from functools import lru_cache
from importlib.metadata import version
from threading import Lock

SENTENCIZER_VERSION = "spacy-" + version("spacy") + "-blank-en-sentencizer-v1"
WORD_LIMIT = 300
WORD_OVERLAP = 30
MAX_CHARS = 6000
NLP_WINDOW_CHARS = 65536
_LOCK = Lock()


def is_english_text(text):
    # Conservative routing, not a learned language detector. Keep Han/mixed material on its character budget.
    return not re.search(r"[\u3400-\u9fff\U00020000-\U0003134f]", text) and bool(re.search(r"[A-Za-z]", text))


@lru_cache(maxsize=1)
def pipeline():
    import spacy

    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    nlp.max_length = NLP_WINDOW_CHARS + 1
    return nlp


def windows(text):
    start = 0
    while start < len(text):
        stop = min(start + NLP_WINDOW_CHARS, len(text))
        if stop < len(text):
            # Keep tokenizer inputs bounded without introducing a sentence break at the window edge.
            boundary = text.rfind("\n\n", start + NLP_WINDOW_CHARS // 2, stop)
            if boundary >= 0:
                stop = boundary + 2
            else:
                boundary = max(text.rfind(" ", stop - 1024, stop), text.rfind("\n", stop - 1024, stop))
                if boundary >= 0:
                    stop = boundary + 1
        yield start, stop
        start = stop


class EnglishText:
    def __init__(self, text, word_limit=WORD_LIMIT, overlap=WORD_OVERLAP):
        self.length = len(text)
        self.limit, self.overlap = word_limit, overlap
        self.starts, self.ends, self.sentences, self.boundaries = [], [], [], []
        self.sentence_starts = []
        self.window_count = 0
        nlp = pipeline()
        with _LOCK, nlp.memory_zone():
            for left, right in windows(text):
                self.window_count += 1
                doc = nlp(text[left:right])
                for token in doc:
                    start, end = left + token.idx, left + token.idx + len(token)
                    # A "word" is a spaCy token with a letter/number, excluding whitespace/pure punctuation.
                    if not token.is_space and any(char.isalnum() for char in token.text):
                        self.starts.append(start)
                        self.ends.append(end)
                for sentence in doc.sents:
                    if sentence.start > 0 or left == 0:
                        self.sentence_starts.append(left + sentence.start_char)
                    end = left + sentence.end_char
                    if sentence.end < len(doc) or right == len(text):
                        self.sentences.append(end)
        # spaCy splits contractions into multiple tokens; chunk boundaries must not split "can't".
        self.boundaries = [match.end() for match in re.finditer(r"\s+", text)]

    def count(self, left, right):
        return bisect_left(self.starts, right) - bisect_right(self.ends, left)

    def fits(self, left, right):
        return right - left <= MAX_CHARS and self.count(left, right) <= self.limit

    def cap(self, start, count=None):
        first = bisect_right(self.ends, start)
        after = first + (self.limit if count is None else count)
        word_cap = self.starts[after] if after < len(self.starts) else self.length
        return min(word_cap, start + MAX_CHARS, self.length)

    def restart(self, start, end):
        if not self.overlap:
            return end
        first = bisect_right(self.ends, start)
        last = bisect_left(self.starts, end)
        if last <= first:
            return end
        index = max(first + 1, last - self.overlap)
        if index >= last:
            return end
        return max(start + 1, self.starts[index])
