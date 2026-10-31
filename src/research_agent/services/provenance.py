"""Traceable source identity; semantic support alone does not establish authority."""

from urllib.parse import urlsplit
import re

from .spans import locate_quote


def linked_target_matches(link, page):
    """A link to one hosted project cannot establish another tenant's identity."""
    a, b = urlsplit(link), urlsplit(page)
    if a.scheme not in {"http", "https"} or b.scheme not in {"http", "https"}:
        return False
    if (a.hostname or "").removeprefix("www.") != (b.hostname or "").removeprefix("www."):
        return False
    path = a.path.rstrip("/")
    if (
        a.hostname in {"github.com", "gitlab.com", "huggingface.co", "bitbucket.org"}
        and len([p for p in path.split("/") if p]) < 2
    ):
        return False
    return not path or b.path.rstrip("/") == path or b.path.startswith(path + "/")


async def identity_context(evidence, source_id, page, context_chars):
    # Reuse the approved fetch chunk size and existing verifier input cap.
    starts = (
        [0] if len(page.text) <= context_chars else [0, max(context_chars, len(page.text) - context_chars)]
    )
    contexts = [
        {"context_id": f"I{i}", "start_char": start, "text": page.text[start : start + context_chars]}
        for i, start in enumerate(starts)
    ]
    links = []
    for source in await evidence.source_index():
        if source["id"] == source_id:
            continue
        for link in source["metadata"].get("reference_links", []):
            if link.get("official_link") and linked_target_matches(link["url"], page.canonical_url):
                links.append(
                    {
                        "link_id": f"L{len(links)}",
                        "url": link["url"],
                        "label": link["label"],
                        "from_source_id": source["id"],
                        "from_url": source["canonical_url"],
                        "from_title": source["title"],
                        "from_sha256": source["content_sha256"],
                    }
                )
    return {"contexts": contexts, "official_links": links}


def resolve_identity(identity, supplied, source_id, page):
    if identity is None:
        return None
    if (
        identity.basis == "original_publication"
        and re.search(r"grants? permission|copyright|版权所有|©", identity.quote or "", re.I)
        and not re.search(
            r"authors?|we (?:propose|introduce|present)|published by|作者", identity.quote or "", re.I
        )
    ):
        return None
    base = {
        "version": 1,
        "basis": identity.basis,
        "publisher_name": identity.publisher_name,
        "explanation": identity.explanation,
        "source_id": source_id,
        "source_sha256": page.content_sha256,
    }
    if identity.basis == "observed_official_link":
        link = next((x for x in supplied["official_links"] if x["link_id"] == identity.link_id), None)
        if not link or not linked_target_matches(link["url"], page.canonical_url):
            return None
        return {
            **base,
            "support_source_id": link["from_source_id"],
            "support_sha256": link["from_sha256"],
            "link_url": link["url"],
            "link_label": link["label"],
        }
    context = next((x for x in supplied["contexts"] if x["context_id"] == identity.context_id), None)
    if not context or not identity.quote:
        return None
    span = locate_quote(context["text"], identity.quote, 0, 0)
    if not span:
        return None
    start, end, quote, _ = span
    return {
        **base,
        "support_source_id": source_id,
        "support_sha256": page.content_sha256,
        "quote": quote,
        "start_char": context["start_char"] + start,
        "end_char": context["start_char"] + end,
    }


def valid_stored_identity(proof, target, support):
    """Check same-run immutable records again at the storage boundary."""
    if not proof or not support or proof.get("version") != 1 or not proof.get("publisher_name"):
        return False
    if (
        str(target["id"]) != proof.get("source_id")
        or str(support["id"]) != proof.get("support_source_id")
        or target["content_sha256"] != proof.get("source_sha256")
        or support["content_sha256"] != proof.get("support_sha256")
    ):
        return False
    if (
        proof.get("basis") == "original_publication"
        and re.search(r"grants? permission|copyright|版权所有|©", proof.get("quote", ""), re.I)
        and not re.search(
            r"authors?|we (?:propose|introduce|present)|published by|作者", proof.get("quote", ""), re.I
        )
    ):
        return False
    if proof.get("basis") in {"publication_record", "linked_publication_record"}:
        from .publication_identity import publication_proof

        expected = publication_proof(target, support)
        return bool(expected) and all(proof.get(k) == v for k, v in expected.items())
    if proof.get("basis") == "observed_official_link":
        return str(target["id"]) != str(support["id"]) and any(
            link.get("official_link")
            and link["url"] == proof.get("link_url")
            and link["label"] == proof.get("link_label")
            and linked_target_matches(link["url"], target["canonical_url"])
            for link in support["metadata"].get("reference_links", [])
        )
    start, end = proof.get("start_char"), proof.get("end_char")
    return (
        proof.get("basis") in {"original_publication", "official_documentation", "firsthand_record"}
        and target["id"] == support["id"]
        and type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(support["content_text"])
        and bool(proof.get("quote"))
        and support["content_text"][start:end] == proof["quote"]
    )
