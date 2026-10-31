"""Select complete original paragraphs inside the existing retrieval windows."""

from ..contracts import stable_id


def reading_passages(source_id, text, windows, max_chars):
    # Keep tables/code together when present; ordinary extracted paragraphs
    # occupy lines. Never paraphrase, invent an offset, or raise the chunk cap.
    spans = []
    offset = 0
    start = None
    mode = None
    for line in text.splitlines(keepends=True):
        kind = (
            "code"
            if line.lstrip().startswith("```")
            else "table"
            if line.lstrip().startswith("|")
            else "paragraph"
        )
        if mode == "code":
            offset += len(line)
            if kind == "code":
                spans.append((start, offset))
                start = None
                mode = None
            continue
        if mode == "table" and kind != "table":
            spans.append((start, offset))
            start = None
            mode = None
        if kind in {"code", "table"}:
            if start is None:
                start = offset
                mode = kind
        elif line.strip():
            spans.append((offset, offset + len(line)))
        offset += len(line)
    if start is not None:
        spans.append((start, offset))
    selected = []
    for start, end in spans:
        if not any(start < w["start_char"] + len(w["text"]) and end > w["start_char"] for w in windows):
            continue
        while start < end:
            stop = min(end, start + max_chars)
            value = text[start:stop]
            if value.strip():
                selected.append(
                    {
                        "chunk_id": stable_id("reading-passage", source_id, start, stop),
                        "start_char": start,
                        "text": value,
                    }
                )
            start = stop
    return selected


def coalesce_passages(source_id, text, passages, max_chars):
    """Combine adjacent original lines; preserve every byte and source offset."""
    spans = []
    for p in passages:
        start, end = p["start_char"], p["start_char"] + len(p["text"])
        if (
            spans
            and start >= spans[-1][1]
            and not text[spans[-1][1] : start].strip()
            and end - spans[-1][0] <= max_chars
        ):
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    return [
        {
            "chunk_id": stable_id("reading-passage", source_id, start, end),
            "start_char": start,
            "text": text[start:end],
        }
        for start, end in spans
    ]
