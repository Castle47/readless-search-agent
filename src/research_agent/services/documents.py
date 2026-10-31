"""Observed work identifiers and representations, never publisher authority."""

import re
import hashlib
import unicodedata
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

ARXIV = re.compile(
    r"^/(abs|html|pdf)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?(?:\.pdf)?/?$", re.I
)
DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def work_identity(url, html=None):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    match = (
        ARXIV.fullmatch(unquote(parsed.path))
        if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org", "ar5iv.labs.arxiv.org", "ar5iv.org"}
        else None
    )
    ids, basis = [], []
    representation, version = "page", None
    if match:
        ids.append("arxiv:" + match[2].lower())
        basis.append({"kind": "arxiv_url", "value": url})
        representation = {"abs": "abstract", "html": "full_text", "pdf": "pdf"}[match[1].lower()]
        version = match[3]
    if host in {"doi.org", "dx.doi.org"} and DOI.fullmatch(unquote(parsed.path).lstrip("/")):
        ids.append("doi:" + unquote(parsed.path).lstrip("/").lower())
        basis.append({"kind": "doi_url", "value": url})
    if html:

        class Meta(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag == "meta":
                    self.values.setdefault((a.get("name") or a.get("property") or "").lower(), []).append(
                        a.get("content", "")
                    )

        parser = Meta()
        parser.feed(html)
        values = parser.values.get("citation_doi", [])
        if len(set(values)) == 1 and DOI.fullmatch(values[0].strip()):
            ids.append("doi:" + values[0].strip().lower())
            basis.append({"kind": "citation_doi", "value": values[0]})
        # Publication pages often omit DOI/arXiv metadata. Explicit complete
        # bibliographies can bridge their record to an identified paper; a
        # matching title or an ordinary outbound reference cannot do so.
        titles = set(parser.values.get("citation_title", []))
        authors = parser.values.get("citation_author", [])
        dates = [
            v
            for key in ("citation_date", "citation_publication_date", "citation_year")
            for v in parser.values.get(key, [])
        ]
        years = {m[0] for value in dates if (m := re.match(r"\d{4}", value.strip()))}
        if len(titles) == 1 and authors and all(a.strip() for a in authors) and len(years) == 1:

            def tokens(value):
                decomposed = unicodedata.normalize("NFKD", value.casefold())
                return re.findall(r"[^\W_]+", "".join(c for c in decomposed if not unicodedata.combining(c)))

            title = next(iter(titles))
            year = next(iter(years))
            normalized = [" ".join(tokens(title)), *[" ".join(sorted(tokens(a))) for a in authors], year]
            if all(normalized):
                identifier = "bibliography:" + hashlib.sha256("\n".join(normalized).encode()).hexdigest()
                ids.append(identifier)
                basis.append(
                    {"kind": "citation_bibliography", "title": title, "authors": authors, "year": year}
                )
    return {
        "version": 1,
        "work_id": ids[0] if ids else None,
        "identifiers": list(dict.fromkeys(ids)),
        "representation": representation,
        "edition": version,
        "basis": basis,
    }


def same_work(a, b):
    return bool(set(a.get("identifiers", [])).intersection(b.get("identifiers", [])))


def redundant_view(candidate, existing):
    if not same_work(candidate, existing):
        return False
    if candidate.get("edition") and existing.get("edition") and candidate["edition"] != existing["edition"]:
        return False
    return existing.get("representation") == "full_text" and candidate.get("representation") in {
        "abstract",
        "pdf",
        "full_text",
    }


def unreadable_url(url):
    return urlsplit(url).path.lower().endswith(".pdf") or work_identity(url)["representation"] == "pdf"
