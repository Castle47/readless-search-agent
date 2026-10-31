from research_agent.services.sections import reference_links, section_locations, retained_identity_text


def test_observed_official_link_retains_provenance_without_executing_links():
    html = '<h1>Example</h1><a href="https://publisher.example/">Official website</a><a href="javascript:alert(1)">API docs</a>'
    links = reference_links(html, "https://reference.example/topic", "Example model")
    assert len(links) == 1 and links[0]["official_link"]
    assert links[0]["url"] == "https://publisher.example/"
    assert section_locations(html, "Example\nBody")[0]["start_char"] == 0


def test_reference_infobox_website_label_is_preserved():
    html = (
        '<table><tr><th>Website</th><td><a href="https://publisher.example/">Publisher</a></td></tr></table>'
    )
    assert reference_links(html, "https://reference.example/topic", "A model")[0]["official_link"]


def test_publisher_link_is_not_lost_when_its_name_differs_from_the_product():
    html = '<a href="https://acme.example/">Acme Science ↗</a><a href="https://community.example/project/cedar">Cedar implementation</a>'
    links = reference_links(html, "https://guide.example/cedar", "Explain Cedar model")
    assert links[0]["url"] == "https://acme.example/"
    assert links[0]["official_link"] is False  # A retained lead is NOT authority proof.


def test_relevant_internal_announcement_is_a_lead_but_self_link_is_not():
    html = '<a href="/">Home</a><a href="/blog/introducing-cedar">Introducing Cedar</a>'
    links = reference_links(html, "https://acme.example/", "Explain Cedar")
    assert [x["url"] for x in links] == ["https://acme.example/blog/introducing-cedar"]
    assert not links[0]["official_link"]


def test_affiliation_and_copyright_survive_without_menu_or_script_text():
    html = "<footer><nav>Home API Menu</nav><p>Independent notes. Not affiliated with Acme.</p><div>© 2026 Acme Notes</div><script>unofficial invented text</script></footer>"
    notes = retained_identity_text(html, "Main article text.", 2500)
    assert notes == ["Independent notes. Not affiliated with Acme.", "© 2026 Acme Notes"]
    assert retained_identity_text(html, "\n".join(notes), 2500) == []
    assert retained_identity_text(html, "Main article text.", 10) == []


def test_explicit_publisher_ownership_language_is_an_observed_identity_lead():
    from research_agent.services.sections import reference_links, explicit_publisher_link

    links = reference_links(
        '<a href="https://evals.vendor.example/">our workflow evals site</a>',
        "https://vendor.example/blog/model",
        "model",
    )
    assert links[0]["official_link"] is True
    assert links[0]["label"] == "our workflow evals site"
    assert links[0]["url"] == "https://evals.vendor.example/"
    for label in [
        "someone else’s evals site",
        "independent guide",
        "our review of the vendor",
        "workflow evals",
    ]:
        assert not explicit_publisher_link(label)


def test_observed_full_text_formats_survive_without_topic_name_and_do_not_grant_authority():
    links = reference_links(
        '<a href="/html/1706.03762v7">HTML (experimental)</a>'
        '<a href="/pdf/1706.03762">View PDF</a>'
        '<a href="/help/html">About HTML conversion</a>',
        "https://arxiv.org/abs/1706.03762",
        "介绍一下原生的 transformer 模型",
    )
    assert {link["url"] for link in links} == {
        "https://arxiv.org/html/1706.03762v7",
        "https://arxiv.org/pdf/1706.03762",
    }
    assert all(link["documentation"] and not link["official_link"] for link in links)
