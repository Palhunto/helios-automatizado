from ebook_pipeline.browser.capture_comparison import compare_capture_text


def test_capture_comparison_classifies_whitespace_and_lengths() -> None:
    comparison = compare_capture_text("Texto\ncom espaço", "Texto  com espaço")
    assert comparison["classification"] == "whitespace"
    assert comparison["rendered"] == {"byte_length": 17, "character_length": 16}
    assert comparison["copied"] == {"byte_length": 18, "character_length": 17}
    assert comparison["first_divergence"] == {
        "copied_character": {"codepoint": "U+0020", "value": " "},
        "copied_context": "Texto  com espaço",
        "index": 5,
        "rendered_character": {"codepoint": "U+000A", "value": "\n"},
        "rendered_context": "Texto\ncom espaço",
    }
    substantive = comparison["substantive_difference"]
    assert isinstance(substantive, dict)
    assert substantive["present"] is False


def test_capture_comparison_classifies_markdown_without_substantive_loss() -> None:
    comparison = compare_capture_text("Título\nTexto importante", "# Título\n**Texto importante**")
    assert comparison["classification"] == "markdown_formatting"
    substantive = comparison["substantive_difference"]
    assert isinstance(substantive, dict)
    assert substantive["present"] is False


def test_capture_comparison_detects_substantive_text_on_each_side() -> None:
    comparison = compare_capture_text(
        "Conteúdo comum e exclusivo rendered",
        "Conteúdo comum e exclusivo copy",
    )
    assert comparison["classification"] == "content"
    substantive = comparison["substantive_difference"]
    assert isinstance(substantive, dict)
    assert substantive["present"] is True
    assert substantive["rendered_has_text_absent_from_copied"] is True
    assert substantive["copied_has_text_absent_from_rendered"] is True
