from ebook_pipeline.browser.urls import (
    conversation_path_from_page_url,
    is_real_conversation_path,
)

REAL_PATH = "/c/9491397d-073e-4cc8-a1b9-a38a8968cfaf"


def test_conversation_path_requires_chatgpt_page_url_and_uuid() -> None:
    assert conversation_path_from_page_url(f"https://chatgpt.com{REAL_PATH}") == REAL_PATH
    assert (
        conversation_path_from_page_url(f"https://chatgpt.com{REAL_PATH}?model=auto")
        == REAL_PATH
    )
    assert conversation_path_from_page_url("https://chatgpt.com/") is None
    assert conversation_path_from_page_url(
        "https://chatgpt.com/c/WEB:9491397d-073e-4cc8-a1b9-a38a8968cfaf"
    ) is None
    assert conversation_path_from_page_url(f"https://evil.example{REAL_PATH}") is None
    assert conversation_path_from_page_url(f"http://chatgpt.com{REAL_PATH}") is None


def test_conversation_path_rejects_dom_and_message_identifiers() -> None:
    assert is_real_conversation_path(REAL_PATH)
    assert not is_real_conversation_path("/c/WEB:9491397d-073e-4cc8-a1b9-a38a8968cfaf")
    assert not is_real_conversation_path("/c/message-123")
    assert not is_real_conversation_path("/c/9491397d-073e-4cc8-a1b9-a38a8968cfaf/extra")
    assert not is_real_conversation_path("/")
