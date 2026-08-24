from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ebook_pipeline.browser.chatgpt import ChatGPTWebAdapter
from ebook_pipeline.browser.models import SessionState, TurnInspection, TurnState


def test_capture_spike_sends_synthetic_prompt_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ChatGPTWebAdapter(
        profile_dir=tmp_path / "profile",
        browser_channel="chromium",
        base_url="https://chatgpt.com",
        headless=True,
        timeout_seconds=10,
        capture_method_version="rendered_text_v1",
    )
    events: list[str] = []
    response = "acknowledgement sintético"
    composer = MagicMock()
    composer.count.return_value = 1
    composer.fill.side_effect = lambda _text: events.append("composer.fill")
    monkeypatch.setattr(adapter, "ensure_ready", lambda: SessionState.READY)
    monkeypatch.setattr(adapter, "create_conversation", lambda: events.append("conversation"))
    monkeypatch.setattr(adapter, "_composer", lambda: composer)

    def send_message(_text: str, *, prefilled: bool = False) -> str:
        assert prefilled is True
        events.append("send_message")
        return "/c/synthetic-spike"

    monkeypatch.setattr(adapter, "send_message", send_message)
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: False)
    monkeypatch.setattr(
        adapter,
        "inspect_turn",
        lambda _fingerprint: TurnInspection(
            TurnState.COMPLETE,
            "/c/synthetic-spike",
            response,
            {"assistant_started": True},
        ),
    )
    turn = MagicMock()
    assistant = MagicMock()
    assistant.count.return_value = 1
    assistant.inner_text.return_value = response
    absent = MagicMock()
    absent.count.return_value = 0
    assistant.locator.return_value = absent
    def locate_assistant(_selector: str) -> MagicMock:
        events.append("rendered.capture")
        return assistant

    turn.locator.side_effect = locate_assistant
    copy_button = MagicMock()
    copy_button.count.return_value = 1
    assistant.get_by_role.return_value = copy_button
    def assistant_for_fingerprint(_fingerprint: str) -> MagicMock:
        events.append("assistant.locate")
        return turn

    monkeypatch.setattr(adapter, "_assistant_for_fingerprint", assistant_for_fingerprint)
    page = MagicMock()

    clipboard_reads = {"count": 0}

    def copied_response(_script: str) -> str:
        clipboard_reads["count"] += 1
        events.append("copy.capture")
        return "stale" if clipboard_reads["count"] == 1 else response

    page.evaluate.side_effect = copied_response
    monkeypatch.setattr(adapter, "_start", lambda: page)

    result = adapter.capture_spike("acknowledgement")

    assert result.equivalent is True
    assert events.index("send_message") < events.index("assistant.locate")
    assert events.index("send_message") < events.index("rendered.capture")
    assert events.index("send_message") < events.index("copy.capture")
