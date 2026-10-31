import hashlib
from types import SimpleNamespace

from research_agent.contracts import SourceIdentity
from research_agent.services.provenance import linked_target_matches, resolve_identity, valid_stored_identity


def test_identity_must_locate_the_offered_original_quote_and_source_hash():
    text = "Jev product guide.\nDiogo Almeida, founder, TypeSafe\nWe are releasing our first model."
    page = SimpleNamespace(text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest())
    context = {"contexts": [{"context_id": "I0", "start_char": 0, "text": text}], "official_links": []}
    identity = SourceIdentity(
        basis="original_publication",
        publisher_name="TypeSafe",
        context_id="I0",
        quote="Diogo Almeida, founder, TypeSafe",
        link_id=None,
        explanation="Named founder of the publishing organization.",
    )
    proof = resolve_identity(identity, context, "s1", page)
    source = {"id": "s1", "content_sha256": page.content_sha256, "content_text": text}
    assert valid_stored_identity(proof, source, source)
    assert text[proof["start_char"] : proof["end_char"]] == identity.quote
    assert not valid_stored_identity({**proof, "source_sha256": "wrong"}, source, source)
    identity.quote = "Invented founder declaration"
    assert resolve_identity(identity, context, "s1", page) is None
    assert resolve_identity(None, context, "s1", page) is None


def test_official_link_cannot_promote_an_unobserved_url_or_other_tenant():
    assert linked_target_matches("https://docs.example.org/", "https://docs.example.org/guide")
    assert linked_target_matches(
        "https://github.com/team/repo", "https://github.com/team/repo/blob/main/README.md"
    )
    assert not linked_target_matches("https://github.com/team/repo", "https://github.com/other/repo")
    assert not linked_target_matches("https://github.com/", "https://github.com/other/repo")
    assert not linked_target_matches("https://example.org/", "https://example.org.attacker.test/")
    page = SimpleNamespace(canonical_url="https://docs.example.org/guide", content_sha256="target-hash")
    identity = SourceIdentity(
        basis="observed_official_link",
        publisher_name="Example",
        context_id=None,
        quote="",
        link_id="L1",
        explanation="Observed link.",
    )
    assert resolve_identity(identity, {"contexts": [], "official_links": []}, "target", page) is None


def test_stored_identity_requires_the_same_run_support_record():
    target = {"id": "t", "canonical_url": "https://docs.example.org/guide", "content_sha256": "target"}
    support = {
        "id": "s",
        "content_sha256": "support",
        "metadata": {
            "reference_links": [
                {"url": "https://docs.example.org/", "label": "Official website", "official_link": True}
            ]
        },
    }
    proof = {
        "version": 1,
        "basis": "observed_official_link",
        "publisher_name": "Example",
        "source_id": "t",
        "source_sha256": "target",
        "support_source_id": "s",
        "support_sha256": "support",
        "link_url": "https://docs.example.org/",
        "link_label": "Official website",
    }
    assert valid_stored_identity(proof, target, support)
    assert not valid_stored_identity(proof, target, None)
    assert not valid_stored_identity({**proof, "support_source_id": "another"}, target, support)
