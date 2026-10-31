from research_agent.services.request_urls import request_urls


def test_chinese_url_wrapper_does_not_become_part_of_the_request_address():
    assert request_urls(
        "根据官方文档（https://docs.python.org/3/tutorial/datastructures.html），list 是否可变？"
    ) == ["https://docs.python.org/3/tutorial/datastructures.html"]


def test_balanced_url_parentheses_and_queries_survive_markdown_and_prose_wrappers():
    url = "https://example.org/Foo_(bar)?x=a,b&n=2#section"
    assert request_urls("[source](" + url + "), see `https://example.org/doc`.") == [
        url,
        "https://example.org/doc",
    ]
    assert request_urls("https://example.org/中文页面，继续解释。") == ["https://example.org/中文页面"]


def test_parentheses_inside_a_unicode_url_are_preserved():
    assert request_urls("来源（https://example.org/条目（说明）），继续。") == [
        "https://example.org/条目（说明）"
    ]
