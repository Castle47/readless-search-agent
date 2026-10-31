import itertools
import json
import pytest
from pydantic import ValidationError
from research_agent.contracts import ResearchRequest, stable_id, normalize
from research_agent.graph.state import merge_immutable_map
from research_agent.errors import InvariantViolation, ToolFailure
from research_agent.adapters.fetch import canonical_url, domain_allowed, public_ip
from research_agent.storage.evidence import independent_origins
from research_agent.config import effective_limits


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "x", "owner_id": "someone"},
        {"query": " "},
        {"query": "x", "limits": {"search_calls": 17}},
        {"query": "x", "limits": {"search_calls": True}},
        {"query": "x", "as_of": "2099-01-01T00:00:00Z"},
        {"query": "x", "time_range": {"start": "2020-02-01T00:00:00Z", "end": "2020-01-01T00:00:00Z"}},
        {"query": "x", "allowed_domains": ["evil.example/path"]},
    ],
)
def test_request_rejects_invalid(payload):
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate_json(json.dumps(payload))


def test_unicode_ids_and_reducer_algebra():
    assert normalize("  Ａ\n中 文 ") == "A 中 文"
    assert stable_id("x", "中文🐿") == stable_id("x", "中文🐿")
    maps = [{"a": {"id": 1}}, {"b": {"id": 2}}, {"a": {"id": 1}}]
    results = []
    for order in itertools.permutations(maps):
        result = {}
        for item in order:
            result = merge_immutable_map(result, item)
        results.append(result)
    assert all(r == results[0] for r in results)
    with pytest.raises(InvariantViolation):
        merge_immutable_map({"a": 1}, {"a": 2})


def test_url_filters_preserve_semantic_parameters():
    assert (
        canonical_url("HTTPS://EXAMPLE.COM:443/Path?a=2&utm_source=x&a=1#f")
        == "https://example.com/Path?a=2&a=1"
    )
    assert domain_allowed("https://docs.example.com/a", ["example.com"])
    assert not domain_allowed("https://notexample.com/a", ["example.com"])
    assert not domain_allowed("https://docs.example.com/a", ["example.com"], ["docs.example.com"])
    for url in [
        "file:///etc/passwd",
        "https://user:pw@example.com",
        "http://example.com:8888",
        "http://example.com\\@127.0.0.1",
    ]:
        with pytest.raises(ToolFailure):
            canonical_url(url)
    for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"]:
        assert not public_ip(ip)
    assert public_ip("8.8.8.8")


def test_publisher_or_same_content_does_not_count_twice():
    values = [
        {"publisher_key": "a.com", "content_sha256": "x"},
        {"publisher_key": "a.com", "content_sha256": "y"},
        {"publisher_key": "b.com", "content_sha256": "y"},
    ]
    assert independent_origins(values) == 1
    values.append({"publisher_key": "c.com", "content_sha256": "z"})
    assert independent_origins(values) == 2


def test_partitions_do_not_borrow(settings):
    r = ResearchRequest(query="x")
    limits = effective_limits(r, settings.snapshot())
    assert (
        sum(limits["tokens_" + k] for k in ["research", "finalization", "memory"])
        == limits["total_tokens"]
        == 300000
    )


def test_corroboration_requires_same_claim_not_just_two_pages():
    from research_agent.contracts import Criterion
    from research_agent.storage.evidence import assess_criteria

    bundle = {
        "claims": [
            {"id": "a", "merged_into": None, "status": "unverified"},
            {"id": "b", "merged_into": None, "status": "unverified"},
        ],
        "evidence": [
            {"id": "e1", "source_id": "s1", "verified": True, "directness": "direct"},
            {"id": "e2", "source_id": "s2", "verified": True, "directness": "direct"},
        ],
        "sources": [
            {"id": "s1", "publisher_key": "one.test", "content_sha256": "one"},
            {"id": "s2", "publisher_key": "two.test", "content_sha256": "two"},
        ],
        "relations": [
            {
                "claim_id": "a",
                "evidence_id": "e1",
                "relation": "supports",
                "verification": {"direct": True, "primary_for_claim": True, "criterion_ids": ["criterion"]},
            },
            {
                "claim_id": "b",
                "evidence_id": "e2",
                "relation": "supports",
                "verification": {"direct": True, "primary_for_claim": True, "criterion_ids": ["criterion"]},
            },
        ],
        "mappings": [
            {"claim_id": "a", "criterion_id": "criterion"},
            {"claim_id": "b", "criterion_id": "criterion"},
        ],
        "conflicts": [],
    }
    c = Criterion(criterion_id="criterion", description="The measured value", kind="corroborated")
    assert assess_criteria([c], bundle, "primary_required")[0].status == "missing"
    bundle["relations"][1]["claim_id"] = "a"
    assert assess_criteria([c], bundle, "primary_required")[0].status == "covered"
