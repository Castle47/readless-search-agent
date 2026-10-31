"""Map HTML headings only when they actually occur in the frozen extracted text."""

from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit


def retained_bibliography(html, body, max_chars):
    """Preserve observed bibliographic author fields dropped by body extraction."""
    from lxml import html as parser

    try:
        tree = parser.fromstring(html, parser=parser.HTMLParser(no_network=True))
    except (ValueError, parser.etree.ParserError):
        return [], []
    values = [
        m.get("content", "").strip()
        for m in tree.xpath("//meta")
        if (m.get("name") or m.get("property") or "").lower() == "citation_author"
    ]
    if not any(values):
        for node in tree.xpath("//*[@class]"):
            if set(node.get("class", "").split()) & {"authors", "ltx_authors"}:
                values.append(" ".join(node.text_content().split()))
    fields, kept, size = [], [], 0
    for value in dict.fromkeys(v for v in values if v):
        if size + len(value) + 2 > max_chars:
            continue
        fields.append({"field": "author", "value": value, "provenance": "observed_html_bibliography"})
        if value not in body:
            kept.append(value)
        size += len(value) + 2
    return kept, fields


def retained_identity_text(html, body, max_chars):
    """Preserve actual footer ownership/disclosure text removed by extraction.

    No inferred authorship or positive authority flag is added. These are source
    words, included in the same immutable text snapshot and normal span checks.
    """
    from lxml import html as parser

    try:
        tree = parser.fromstring(html, parser=parser.HTMLParser(no_network=True))
    except (ValueError, parser.etree.ParserError):
        return []
    for el in tree.xpath("//script|//style|//template"):
        el.drop_tree()
    regions = tree.xpath("//footer|//*[@role='contentinfo']|//*[contains(@class,'footer')]")
    region_set = set(regions)
    regions = [el for el in regions if not any(parent in region_set for parent in el.iterancestors())]
    pattern = re.compile(
        r"©|copyright|not\s+affiliated|not\s+(?:endorsed|operated)\s+by|unofficial|independent\s+(?:notes|guide|website)|非官方|无隶属关系",
        re.I,
    )
    candidates = []
    for region in regions:
        for el in region.iter():
            if el.tag not in {"p", "small", "div", "footer"}:
                continue
            if el.tag in {"div", "footer"} and any(
                child.tag in {"p", "small", "div", "footer"} for child in el.iterdescendants()
            ):
                continue
            text = " ".join(el.text_content().split())
            if len(text) <= max_chars and pattern.search(text) and text not in candidates:
                candidates.append(text)
    # Keep the smallest complete matching blocks, not their surrounding menus.
    leaves = [t for t in candidates if not any(t != q and q in t for q in candidates)]
    normalized_body = " ".join(body.split())
    kept, size = [], 0
    for text in leaves:
        if text not in normalized_body and size + len(text) + 2 <= max_chars:
            kept.append(text)
            size += len(text) + 2
    return kept


def section_locations(html, text):
    class Headings(HTMLParser):
        def __init__(self):
            super().__init__()
            self.level = None
            self.parts = []
            self.items = []

        def handle_starttag(self, tag, attrs):
            if re.fullmatch(r"h[1-6]", tag):
                self.level = int(tag[1])
                self.parts = []

        def handle_data(self, data):
            if self.level is not None:
                self.parts.append(data)

        def handle_endtag(self, tag):
            if self.level is not None and tag == f"h{self.level}":
                self.items.append((self.level, " ".join("".join(self.parts).split())))
                self.level = None

    parser = Headings()
    parser.feed(html)
    sections = []
    cursor = 0
    for level, title in parser.items:
        start = text.find(title, cursor) if title else -1
        if start < 0:
            continue
        sections.append({"title": title, "level": level, "start_char": start, "end_char": len(text)})
        cursor = start + len(title)
    for i, section in enumerate(sections):
        section["end_char"] = next(
            (s["start_char"] for s in sections[i + 1 :] if s["level"] <= section["level"]), len(text)
        )
    return sections


def explicit_publisher_link(label):
    # A publisher may say "our workflow evals site", not just "official site".
    # This is a provenance candidate, never automatic first-hand qualification.
    return bool(
        re.search(
            r"official (?:web)?site|官方网站|官网|^website\b|^网站|"
            r"\bour\s+(?:(?:workflow|evaluation|evals|benchmark|benchmarks|documentation|docs)\s+)*(?:site|website|documentation|docs|repository)\b|"
            r"我们的(?:评测|文档|项目|官方)?(?:网站|站点|文档|仓库)",
            label,
            re.I,
        )
    )


def reference_links(html, url, query):
    """Observed publisher/documentation leads, not a claim of authority."""
    from ..adapters.fetch import canonical_url
    from ..errors import ToolFailure

    terms = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", query)} - {
        "model",
        "the",
        "what",
        "how",
        "is",
        "and",
        "for",
        "explain",
        "introduce",
        "research",
        "compare",
    }

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.href = None
            self.parts = []
            self.items = []
            self.last_text = ""
            self.label_prefix = ""

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.href = dict(attrs).get("href")
                self.parts = []
                self.label_prefix = (
                    self.last_text
                    if re.fullmatch(
                        r"(?:official\s+)?(?:website|site)|官网|官方网站|网站",
                        self.last_text.strip(" :："),
                        re.I,
                    )
                    else ""
                )

        def handle_data(self, data):
            if self.href is not None:
                self.parts.append(data)
            elif data.strip():
                self.last_text = data.strip()

        def handle_endtag(self, tag):
            if tag == "a" and self.href is not None:
                self.items.append(
                    (self.href, " ".join((self.label_prefix + " " + "".join(self.parts)).split()))
                )
                self.href = None

    parser = Links()
    parser.feed(html)
    result = []
    seen = set()
    for href, label in parser.items:
        target = urljoin(url, href)
        if urlsplit(target).scheme not in {"http", "https"}:
            continue
        try:
            target = canonical_url(target)
        except (ToolFailure, ValueError, UnicodeError):
            continue
        official = explicit_publisher_link(label)
        full_text = bool(
            re.search(r"\bfull[ -]?text\b|全文", label, re.I)
            or re.fullmatch(
                r"(?:(?:view|read|download)\s+)?(?:html|pdf)(?:\s*\([^)]*\))?", label.strip(), re.I
            )
        )
        documentation = (
            bool(re.search(r"documentation|\bdocs?\b|\bAPI\b|文档|原文", label, re.I)) or full_text
        )
        relevant = any(t in (label + " " + target).lower() for t in terms)
        external = urlsplit(target).hostname != urlsplit(url).hostname
        # The publisher's name/domain need not contain the product's name.
        # Retain labelled external links as leads, never as authority proof.
        if target == canonical_url(url):
            continue
        if target not in seen and (official or documentation or relevant or (external and label)):
            result.append(
                {
                    "url": target,
                    "label": label,
                    "official_link": official,
                    "documentation": documentation,
                    "full_text": full_text,
                }
            )
            seen.add(target)
    return sorted(
        result,
        key=lambda x: (
            not x["official_link"],
            not x["documentation"],
            bool(
                re.fullmatch(
                    r"sign in|log ?in|sign up|open roles|careers|linkedin|x|twitter|登录|注册",
                    x["label"],
                    re.I,
                )
            ),
            urlsplit(x["url"]).path not in {"", "/"},
            x["url"],
        ),
    )
