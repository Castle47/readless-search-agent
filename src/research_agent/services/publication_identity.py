"""Identity of an observed scholarly work, separate from any particular claim.

Repository records establish an original publication, not truth of its claims,
independent replication, or author names absent from the captured source.
"""

import re
from .documents import work_identity, same_work


def publication_record(source):
    url = source.get("canonical_url", "")
    doc = work_identity(url)
    text = source.get("content_text", "")
    if not doc["work_id"] or not doc["work_id"].startswith("arxiv:"):
        return None
    if doc["representation"] == "abstract":
        title = re.search(r"(?m)^\s*Title:\s*([^\n]+)", text)
        if not title or not re.search(r"Submitted on|Submission history", text) or "Abstract:" not in text:
            return None
        edition = re.search(r"this version, (v\d+)", text)
        return {
            "title": title[1].strip(),
            "quote": title[0].strip(),
            "start_char": title.start() + len(title[0]) - len(title[0].lstrip()),
            "edition": doc["edition"] or (edition[1] if edition else None),
            "document": doc,
        }
    # Fresh full text can retain literal bibliographic author metadata; a
    # copyright permission or a title alone never meets this route.
    fields = source.get("metadata", {}).get("bibliographic_fields", [])
    authors = [f["value"] for f in fields if f.get("field") == "author" and f.get("value")]
    title = source.get("title", "")
    if doc["representation"] == "full_text" and authors and title in text:
        value = next((a for a in authors if a in text), None)
        if value:
            return {
                "title": title,
                "quote": value,
                "start_char": text.index(value),
                "edition": doc["edition"],
                "document": doc,
            }
    return None


def publication_proof(target, support):
    record = publication_record(support)
    if not record:
        return None
    same = str(target["id"]) == str(support["id"])
    observed = None
    if not same:
        if record["title"] not in target.get("content_text", ""):
            return None
        document = work_identity(target["canonical_url"])
        if not same_work(record["document"], document):
            return None
        if record["edition"] and document["edition"] and record["edition"] != document["edition"]:
            return None
        observed = next(
            (
                l
                for l in support.get("metadata", {}).get("reference_links", [])
                if l.get("full_text") and l["url"] == target["canonical_url"]
            ),
            None,
        )
        if not observed:
            return None
    return {
        "version": 1,
        "basis": "publication_record" if same else "linked_publication_record",
        "publisher_name": record["title"],
        "explanation": "Observed record of this original work. Named authorship requires separately captured author evidence; copyright alone is not authorship.",
        "identity_scope": "original work content, not independent replication or uncaptured author names",
        "source_id": str(target["id"]),
        "source_sha256": target["content_sha256"],
        "support_source_id": str(support["id"]),
        "support_sha256": support["content_sha256"],
        "work_id": record["document"]["work_id"],
        "quote": record["quote"],
        "start_char": record["start_char"],
        "end_char": record["start_char"] + len(record["quote"]),
        **({"link_url": observed["url"], "link_label": observed.get("label", "")} if observed else {}),
    }


async def established_identity(services, source_id):
    from .provenance import valid_stored_identity

    key = "source-identity:" + source_id
    saved = await services.db.saved_value(services.run_id, key)
    target = await services.evidence.get_source(source_id)
    if saved:
        support = await services.evidence.get_source(saved["proof"]["support_source_id"])
        if not valid_stored_identity(saved["proof"], target, support):
            raise ValueError("Stored publication identity differs from immutable sources")
        return saved["proof"]
    candidates = [target] + [s for s in await services.evidence.source_index() if s["id"] != source_id]
    for index, row in enumerate(candidates):
        if index and not same_work(
            work_identity(target["canonical_url"]), work_identity(row["canonical_url"])
        ):
            continue
        support = row if index == 0 else await services.evidence.get_source(row["id"])
        proof = publication_proof(target, support)
        if proof:
            await services.db.commit_value(
                services.run_id, services.fence, key, "source_identity", {"proof": proof}
            )
            return proof
    return None


def identity_summary(proof):
    return {
        k: proof[k]
        for k in ["basis", "publisher_name", "source_id", "support_source_id", "quote", "explanation"]
        if k in proof
    }
