from ebook_pipeline.writing.citations import citation_ledger_bytes, extract_citations


def test_high_confidence_author_date_occurrences_preserve_offsets_and_raw() -> None:
    raw = "Porter (1980) fundamenta o argumento; a aplicação foi revista (Silva, 2020).".encode()
    occurrences = extract_citations(
        project_id="project", submission_id="submission", unit_id="UNIT", raw=raw
    )
    assert [item.raw_citation_text for item in occurrences] == [
        "Porter (1980)",
        "(Silva, 2020)",
    ]
    text = raw.decode()
    assert all(
        text[item.start_offset : item.end_offset] == item.raw_citation_text for item in occurrences
    )
    assert occurrences[0].observed_author == "Porter"
    assert occurrences[0].parsed_year == 1980
    ledger = citation_ledger_bytes(occurrences)
    assert b"Citation Ledger != Reference Reconciliation" in ledger
    assert b"DOI" not in ledger


def test_ambiguous_year_suffix_is_preserved_without_forced_integer() -> None:
    occurrence = extract_citations(
        project_id="p",
        submission_id="s",
        unit_id="U",
        raw="Silva (2020a) apresenta a análise.".encode(),
    )[0]
    assert occurrence.year_text == "2020a"
    assert occurrence.parsed_year is None
