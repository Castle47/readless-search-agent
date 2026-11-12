"""Deterministic, local query/document tokenization for SQLite FTS5."""

import logging
import re
import unicodedata

import jieba

TOKENIZER_VERSION = "jieba-0.42.1-search-hmm-off-v1"
_tokenizer = jieba.Tokenizer()
jieba.setLogLevel(logging.WARNING)
# Query boilerplate only; do not discard domain words such as model or cache.
STOP_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "it",
        "this",
        "that",
        "with",
        "from",
        "what",
        "how",
        "why",
        "please",
        "explain",
        "introduce",
        "describe",
        "tell",
        "me",
        "my",
        "about",
        "does",
        "do",
        "的",
        "了",
        "和",
        "与",
        "或",
        "是",
        "在",
        "我",
        "我的",
        "你",
        "请",
        "请问",
        "什么",
        "怎么",
        "如何",
        "为什么",
        "介绍",
        "解释",
        "一下",
        "关于",
        "笔记",
        "资料",
        "里面",
        "中",
        "里",
    ]
)
MAX_QUERY_TERMS = 64


def tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    result = []
    for part in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text):
        if part.isascii():
            result.append(part)
        else:
            result.extend(word for word in _tokenizer.cut_for_search(part, HMM=False) if word.strip())
    return result


def query_terms(text):
    return list(dict.fromkeys(term for term in tokens(text) if term not in STOP_WORDS))[:MAX_QUERY_TERMS]


def fields(path, title, text):
    return tuple(" ".join(tokens(value)) for value in (path, title, text))
