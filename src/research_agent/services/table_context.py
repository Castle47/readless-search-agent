"""Keep original Markdown comparison headers attached to their frozen rows."""

import re


def table_header(text, start, end):
    lines = text.splitlines(keepends=True)
    offset, header, region_start = 0, "", -1
    previous, previous_offset = "", 0
    for line in lines:
        if line.lstrip().startswith("|"):
            if (
                re.fullmatch(r"\s*\|[\s:|\-]+\|\s*", line)
                and "---" in line
                and previous.lstrip().startswith("|")
            ):
                header, region_start = previous + line, previous_offset
        else:
            if header and region_start <= start < end <= offset:
                return header
            header, region_start = "", -1
        previous, previous_offset = line, offset
        offset += len(line)
    return header if header and region_start <= start < end <= offset else ""
