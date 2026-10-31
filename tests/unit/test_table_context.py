from research_agent.services.table_context import table_header


def test_comparison_header_is_kept_with_its_rows_not_with_unrelated_prose():
    text = "Intro\n| Feature | A | B |\n|---|---|---|\n| Sampling | Sequential | Parallel |\nEnd\n| X | Y |\n|---|---|\n| One | Two |\n"
    row = "| Sampling | Sequential | Parallel |"
    start = text.index(row)
    assert table_header(text, start, start + len(row)) == "| Feature | A | B |\n|---|---|---|\n"
    assert table_header(text, 0, 5) == ""
    assert table_header(text, text.index("End"), text.index("End") + 3) == ""
    second = text.index("| One")
    assert table_header(text, second, len(text)) == "| X | Y |\n|---|---|\n"
