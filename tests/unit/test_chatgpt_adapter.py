from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from playwright._impl._driver import compute_driver_executable

import ebook_pipeline.browser.chatgpt as chatgpt_module
from ebook_pipeline.browser.chatgpt import (
    COMPOSER_TEXT_DIAGNOSTIC_SCRIPT,
    PASTED_TEXT_MODAL_CLOSE_PATTERN,
    ChatGPTWebAdapter,
    StructuralSendProofState,
)
from ebook_pipeline.browser.fingerprints import transport_fingerprint
from ebook_pipeline.browser.models import SessionState, TurnInspection, TurnState
from ebook_pipeline.browser.profile_lock import BrowserProfileLock
from ebook_pipeline.browser.selectors import (
    ASSISTANT_GENERATION_INDICATOR_SELECTOR,
    ASSISTANT_MESSAGE_SELECTOR,
    ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR,
    ASSISTANT_RENDERED_CONTENT_SELECTOR,
    COMPOSER_PASTED_TEXT_ATTACHMENT_REMOVE_SELECTOR,
    COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR,
    CONVERSATION_LINK_SELECTOR,
    CONVERSATION_ROOT_SELECTOR,
    TURN_SELECTOR,
    USER_MESSAGE_SELECTOR,
    USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR,
    USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_CONTAINER_XPATH,
    USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_TITLE_SELECTOR,
)
from ebook_pipeline.core.errors import ConfigurationError, ConflictError, IntegrityError

CONVERSATION_A = "/c/9491397d-073e-4cc8-a1b9-a38a8968cfaf"
CONVERSATION_B = "/c/3a615ed1-ab7d-449e-aeeb-80eb29fc615f"
CONVERSATION_C = "/c/595c9f06-2989-4b10-a517-db295e0872cc"


def _focused_composer_metadata() -> dict[str, object]:
    return {
        "tag_name": "textarea",
        "contenteditable": None,
        "role": "textbox",
        "id": "prompt-textarea",
        "data_testid": None,
        "is_content_editable": False,
        "focused": True,
        "editable_descendant_count": 0,
    }


def _bypass_composer_verification(
    adapter: ChatGPTWebAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        adapter, "_ensure_empty_composer_before_insert", lambda: MagicMock()
    )
    monkeypatch.setattr(adapter, "_focus_composer", lambda: adapter._composer())
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _composer: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter, "_wait_for_normal_composer_insertion", lambda *_args, **_kwargs: None
    )
    def capture_empty_tail() -> int:
        adapter._pending_pre_send_turn_anchor = {
            "version": 1,
            "conversation_path": None,
            "pre_send_user_turn_count": 0,
            "pre_send_assistant_turn_count": 0,
            "tail": [],
        }
        return 0

    monkeypatch.setattr(adapter, "_capture_pre_send_user_turn_count", capture_empty_tail)


def _adapter(
    tmp_path: Path,
    method: str = "rendered_text_v1",
    channel: str = "chromium",
    *,
    headless: bool = True,
) -> ChatGPTWebAdapter:
    return ChatGPTWebAdapter(
        profile_dir=tmp_path / "profile",
        browser_channel=channel,
        base_url="https://chatgpt.com",
        headless=headless,
        timeout_seconds=10,
        capture_method_version=method,
    )


@pytest.mark.parametrize(
    ("channel", "expected_channel"),
    [("chrome", "chrome"), ("chromium", None)],
)
def test_persistent_context_uses_selected_browser_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    expected_channel: str | None,
) -> None:
    adapter = _adapter(tmp_path, channel=channel)
    module = MagicMock()
    playwright = MagicMock()
    context = MagicMock()
    page = MagicMock()
    context.pages = [page]
    module.sync_playwright.return_value.start.return_value = playwright
    playwright.chromium.launch_persistent_context.return_value = context
    monkeypatch.setattr(adapter, "_module", lambda: module)

    assert adapter._start() is page
    _, kwargs = playwright.chromium.launch_persistent_context.call_args
    assert kwargs["headless"] is True
    if expected_channel is None:
        assert "channel" not in kwargs
    else:
        assert kwargs["channel"] == expected_channel
    adapter.close()


def test_profile_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    first = BrowserProfileLock(tmp_path / "profile")
    second = BrowserProfileLock(tmp_path / "profile")
    first.acquire()
    try:
        with pytest.raises(ConflictError) as captured:
            second.acquire()
        assert captured.value.code == "BROWSER_PROFILE_IN_USE"
    finally:
        first.release()
    second.acquire()
    second.release()


def test_cleanup_suppresses_target_closed_cascade_and_releases_profile(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    context = MagicMock()
    context.close.side_effect = RuntimeError("TargetClosedError")
    playwright = MagicMock()
    playwright.stop.side_effect = RuntimeError("TargetClosedError")
    adapter._context = context
    adapter._playwright = playwright
    adapter._profile_lock.acquire()

    adapter.close()

    assert adapter._context is None
    assert adapter._playwright is None
    replacement = BrowserProfileLock(tmp_path / "profile")
    replacement.acquire()
    replacement.release()


def test_session_detection_is_observable_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    body = MagicMock()
    page.locator.return_value = body
    login = MagicMock()
    login.count.return_value = 0
    page.get_by_role.return_value = login
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)

    body.inner_text.return_value = "Workspace normal"
    assert adapter.ensure_ready() is SessionState.READY
    body.inner_text.return_value = "Verify you are human - CAPTCHA"
    assert adapter.ensure_ready() is SessionState.CHALLENGE
    body.inner_text.return_value = "Workspace normal"
    login.count.return_value = 1
    assert adapter.ensure_ready() is SessionState.LOGIN_REQUIRED
    login.count.return_value = 0
    composer.count.return_value = 0
    assert adapter.ensure_ready() is SessionState.LOGIN_REQUIRED


def test_navigation_paths_and_send_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter(tmp_path)
    _bypass_composer_verification(adapter, monkeypatch)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    monkeypatch.setattr(adapter, "_start", lambda: page)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    observed_editor_values = iter(("", "mensagem", "mensagem"))
    monkeypatch.setattr(
        adapter, "_composer_text", lambda _composer: next(observed_editor_values)
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    send_button = MagicMock()
    send_button.count.return_value = 1
    send_button.is_visible.return_value = True
    send_button.is_enabled.return_value = True
    monkeypatch.setattr(adapter, "_send_button", lambda: send_button)
    composer_states = iter((True, False))
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: next(composer_states))
    adapter.create_conversation()
    page.goto.assert_called()
    adapter.open_conversation(CONVERSATION_B)
    with pytest.raises(IntegrityError):
        adapter.open_conversation("https://evil.example/c/id")
    boundaries: list[str] = []
    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        assert (
            adapter.send_message(
                "mensagem",
                on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
            )
            is None
        )
    assert boundaries == ["send_attempt_started"]
    composer.fill.assert_called_once_with(
        "mensagem", timeout=min(30_000, adapter.timeout_ms), force=True
    )
    send_button.click.assert_called_once_with(timeout=adapter.timeout_ms)
    checkpoints = [
        getattr(record, "status", None)
        for record in caplog.records
        if getattr(record, "operation", None) == "browser.send_message"
    ]
    assert checkpoints == [
        "composer_found",
        "composer_filled",
        "send_button_found",
        "send_button_enabled",
        "send_trigger_started",
        "send_trigger_completed",
        "composer_cleared",
    ]
    with pytest.raises(IntegrityError):
        adapter.send_message("")
    composer.count.return_value = 2
    with pytest.raises(ConflictError):
        adapter.send_message("ambígua")

    links = MagicMock()
    links.count.return_value = 3
    links.nth.side_effect = [
        SimpleNamespace(get_attribute=lambda _name: CONVERSATION_A),
        SimpleNamespace(
            get_attribute=lambda _name: f"https://chatgpt.com{CONVERSATION_B}?x=1"
        ),
        SimpleNamespace(get_attribute=lambda _name: "/g/not-a-conversation"),
    ]
    page.locator.side_effect = lambda selector: (
        links if selector == CONVERSATION_LINK_SELECTOR else MagicMock()
    )
    assert adapter.list_conversation_paths() == tuple(sorted((CONVERSATION_A, CONVERSATION_B)))


def test_reload_conversation_uses_same_page_reload_not_goto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    start = MagicMock(return_value=page)
    monkeypatch.setattr(adapter, "_start", start)

    adapter.reload_conversation(CONVERSATION_A)

    start.assert_called_once_with()
    page.reload.assert_called_once_with(
        wait_until="domcontentloaded", timeout=adapter.timeout_ms
    )
    page.goto.assert_not_called()


def test_reload_conversation_rejects_wrong_current_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_B}"
    monkeypatch.setattr(adapter, "_start", lambda: page)

    with pytest.raises(ConflictError) as captured:
        adapter.reload_conversation(CONVERSATION_A)

    assert captured.value.code == "BROWSER_CONVERSATION_RELOAD_WRONG_PATH"
    page.reload.assert_not_called()


def test_user_turn_structural_probe_clicks_only_attachment_and_audits_modal_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    roots = MagicMock()
    roots.count.return_value = 1
    root = MagicMock()
    roots.first = root
    first_user_turns = MagicMock()
    first_user_turns.count.return_value = 0
    second_user_turns = MagicMock()
    second_user_turns.count.return_value = 1
    third_user_turns = MagicMock()
    third_user_turns.count.return_value = 1
    user_turn = MagicMock()
    attachments = MagicMock()
    attachments.count.return_value = 1
    group = MagicMock()
    group.get_attribute.return_value = "Texto colado (request).txt"
    attachments.first = group
    openers = MagicMock()
    openers.count.return_value = 1
    opener = MagicMock()
    opener.get_attribute.return_value = "Texto colado (request).txt"
    openers.first = opener
    group.locator.return_value = openers
    user_turn.locator.return_value = attachments
    close_control = MagicMock()
    close_control.evaluate.return_value = [
        {
            "elements": [
                {
                    "tag_name": "div",
                    "role": None,
                    "aria-label": None,
                    "data-testid": None,
                    "text_length": 67_700,
                    "child_count": 2,
                    "button_count": 1,
                    "scrollable_descendant_count": 1,
                }
            ]
        }
    ]
    third_user_turns.first = user_turn
    root.locator.side_effect = (first_user_turns, second_user_turns, third_user_turns)
    page.locator.return_value = roots
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        adapter, "_wait_for_modal_audit_close_control", lambda: close_control
    )

    result = adapter.user_turn_structural_probe(CONVERSATION_A)

    page.goto.assert_called_once_with(
        f"https://chatgpt.com{CONVERSATION_A}", wait_until="domcontentloaded"
    )
    assert page.locator.call_count == 3
    assert root.locator.call_count == 3
    assert all(
        call.args == (USER_MESSAGE_SELECTOR,) for call in root.locator.call_args_list
    )
    user_turn.locator.assert_called_once_with(
        USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
    )
    group.locator.assert_called_once_with(
        USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR
    )
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    close_control.evaluate.assert_called_once()
    assert result["conversation_path"] == CONVERSATION_A
    assert result["user_turn_count"] == 1
    assert result["attachment_aria_label"] == "Texto colado (request).txt"
    assert result["modal_candidate_count"] == 1
    assert result["modal_candidates"] == close_control.evaluate.return_value


def test_modal_audit_wait_reacquires_candidates_until_two_stable_visible_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    absent = MagicMock()
    absent.count.return_value = 0
    first = MagicMock()
    first.count.return_value = 1
    first.nth.return_value.is_visible.return_value = True
    first.first.evaluate.return_value = {
        "progressbar_count": 0,
        "text_length": 67_700,
    }
    second = MagicMock()
    second.count.return_value = 1
    second.nth.return_value.is_visible.return_value = True
    second.first.evaluate.return_value = {
        "progressbar_count": 0,
        "text_length": 67_700,
    }
    page.get_by_role.side_effect = (absent, first, second)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    observed = adapter._wait_for_modal_audit_close_control()

    assert observed is second.first
    assert page.get_by_role.call_count == 3
    assert all(
        call.args == ("button",)
        and call.kwargs == {"name": PASTED_TEXT_MODAL_CLOSE_PATTERN}
        for call in page.get_by_role.call_args_list
    )
    assert page.wait_for_timeout.call_count == 2


def test_modal_audit_wait_times_out_without_semantic_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    absent = MagicMock()
    absent.count.return_value = 0
    page.get_by_role.return_value = absent
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        "ebook_pipeline.browser.chatgpt.USER_TURN_SPIKE_MODAL_TIMEOUT_MS", 0
    )

    with pytest.raises(IntegrityError) as captured:
        adapter._wait_for_modal_audit_close_control()

    assert captured.value.code == "BROWSER_USER_TURN_SPIKE_MODAL_HYDRATION_TIMEOUT"
    assert captured.value.context.evidence == {
        "modal_poll_count": 1,
        "modal_candidate_count": 0,
        "progressbar_count": 0,
        "modal_text_length": 0,
        "stable_reads": 0,
    }
    page.wait_for_timeout.assert_not_called()


def test_user_turn_spike_reacquires_root_and_candidates_until_two_stable_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    missing_root = MagicMock()
    missing_root.count.return_value = 0
    first_root = MagicMock()
    first_root.count.return_value = 1
    first_candidates = MagicMock()
    first_candidates.count.return_value = 1
    first_root.first.locator.return_value = first_candidates
    second_root = MagicMock()
    second_root.count.return_value = 1
    second_candidates = MagicMock()
    second_candidates.count.return_value = 1
    expected_turn = MagicMock()
    second_candidates.first = expected_turn
    second_root.first.locator.return_value = second_candidates
    page.locator.side_effect = (missing_root, first_root, second_root)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    observed = adapter._wait_for_hydrated_user_turn(CONVERSATION_A)

    assert observed is expected_turn
    assert page.locator.call_count == 3
    assert first_root.first.locator.call_args.args == (USER_MESSAGE_SELECTOR,)
    assert second_root.first.locator.call_args.args == (USER_MESSAGE_SELECTOR,)
    assert page.wait_for_timeout.call_count == 2


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_user_turn_spike_hydration_timeout_is_fail_closed_for_zero_or_ambiguous_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_count: int,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    root = MagicMock()
    root.count.return_value = 1
    candidates = MagicMock()
    candidates.count.return_value = candidate_count
    root.first.locator.return_value = candidates
    page.locator.return_value = root
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        "ebook_pipeline.browser.chatgpt.USER_TURN_SPIKE_HYDRATION_TIMEOUT_MS", 0
    )

    with pytest.raises(IntegrityError) as captured:
        adapter._wait_for_hydrated_user_turn(CONVERSATION_A)

    assert captured.value.code == "BROWSER_USER_TURN_SPIKE_HYDRATION_TIMEOUT"
    assert captured.value.context.evidence == {
        "hydration_poll_count": 1,
        "conversation_path": CONVERSATION_A,
        "conversation_root_found": True,
        "user_turn_candidate_count": candidate_count,
        "stable_reads": 1,
    }
    candidates.first.evaluate.assert_not_called()
    page.wait_for_timeout.assert_not_called()


def test_user_turn_spike_requires_canonical_url_and_root_before_counting_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    root = MagicMock()
    root.count.return_value = 1
    page.locator.return_value = root
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        "ebook_pipeline.browser.chatgpt.USER_TURN_SPIKE_HYDRATION_TIMEOUT_MS", 0
    )

    with pytest.raises(IntegrityError) as captured:
        adapter._wait_for_hydrated_user_turn(CONVERSATION_A)

    assert captured.value.code == "BROWSER_USER_TURN_SPIKE_HYDRATION_TIMEOUT"
    assert captured.value.context.evidence == {
        "hydration_poll_count": 1,
        "conversation_path": None,
        "conversation_root_found": False,
        "user_turn_candidate_count": 0,
        "stable_reads": 0,
    }
    root.first.locator.assert_not_called()


@pytest.mark.parametrize(
    (
        "visible",
        "enabled",
        "click_error",
        "composer_cleared",
        "code",
        "attempt_started",
    ),
    [
        (
            True,
            True,
            RuntimeError("obscured"),
            True,
            "BROWSER_SEND_BUTTON_NOT_ACTIONABLE",
            True,
        ),
        (True, True, None, False, "BROWSER_SEND_COMPOSER_NOT_CLEARED", True),
    ],
)
def test_send_message_classifies_non_actionable_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    visible: bool,
    enabled: bool,
    click_error: Exception | None,
    composer_cleared: bool,
    code: str,
    attempt_started: bool,
) -> None:
    adapter = _adapter(tmp_path)
    adapter.timeout_ms = 0
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    monkeypatch.setattr(adapter, "_start", lambda: page)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    _bypass_composer_verification(adapter, monkeypatch)
    button = MagicMock()
    button.count.return_value = 1
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled
    button.click.side_effect = click_error
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    states = iter((True, not composer_cleared))
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: next(states))

    boundaries: list[str] = []
    with pytest.raises(ConflictError) as captured:
        adapter.send_message(
            "mensagem",
            on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
        )
    assert captured.value.code == code
    assert bool(boundaries) is attempt_started


def test_send_button_waits_for_two_consecutive_enabled_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    button = MagicMock()
    button.count.return_value = 1
    button.is_visible.return_value = True
    button.is_enabled.side_effect = (False, True, True)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_send_button", lambda: button)

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        result = adapter._wait_for_send_button_enabled(timeout_ms=1000)

    assert result is button
    assert button.is_enabled.call_count == 3
    button.click.assert_not_called()
    assert page.wait_for_timeout.call_count == 2
    messages = [
        record.message
        for record in caplog.records
        if "browser_send_button_wait" in record.message
    ]
    assert "send_button_poll_count=3" in messages[-1]
    assert "send_button_visible=True" in messages[-1]
    assert "send_button_enabled=True" in messages[-1]
    assert "send_button_stable_enabled_reads=2" in messages[-1]


def test_send_button_wait_reacquires_replaced_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    first = MagicMock()
    replacement = MagicMock()
    for button in (first, replacement):
        button.count.return_value = 1
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
    buttons = iter((first, replacement))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_send_button", lambda: next(buttons))

    result = adapter._wait_for_send_button_enabled(timeout_ms=1000)

    assert result is replacement
    first.click.assert_not_called()
    replacement.click.assert_not_called()


def test_send_button_wait_allows_temporary_disappearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    missing = MagicMock()
    missing.count.return_value = 0
    first_enabled = MagicMock()
    stable_enabled = MagicMock()
    for button in (first_enabled, stable_enabled):
        button.count.return_value = 1
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
    buttons = iter((missing, first_enabled, stable_enabled))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_send_button", lambda: next(buttons))

    result = adapter._wait_for_send_button_enabled(timeout_ms=1000)

    assert result is stable_enabled
    assert page.wait_for_timeout.call_count == 2
    missing.click.assert_not_called()
    first_enabled.click.assert_not_called()
    stable_enabled.click.assert_not_called()


def test_send_button_enable_timeout_remains_strictly_pre_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    button = MagicMock()
    button.count.return_value = 1
    button.is_visible.return_value = True
    button.is_enabled.return_value = False
    times = iter((0.0, 31.0))
    checkpoints: list[str] = []
    boundaries: list[str] = []
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)
    _bypass_composer_verification(adapter, monkeypatch)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message(
            "mensagem",
            on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
        )

    assert captured.value.code == "BROWSER_SEND_BUTTON_ENABLE_TIMEOUT"
    assert captured.value.context.evidence == {
        "send_button_poll_count": 1,
        "send_button_visible": True,
        "send_button_enabled": False,
        "send_button_stable_enabled_reads": 0,
    }
    assert checkpoints == ["composer_found", "composer_filled", "send_button_found"]
    assert boundaries == []
    button.click.assert_not_called()


def test_send_click_occurs_only_after_stable_enabled_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    button = MagicMock()
    button.count.return_value = 1
    button.is_visible.return_value = True
    button.is_enabled.side_effect = (False, True, True)
    checkpoints: list[str] = []
    events: list[str] = []

    def record_checkpoint(checkpoint: str) -> None:
        checkpoints.append(checkpoint)
        events.append(checkpoint)

    button.click.side_effect = lambda **_kwargs: events.append("click")
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: False)
    monkeypatch.setattr(
        adapter,
        "_send_checkpoint",
        record_checkpoint,
    )
    _bypass_composer_verification(adapter, monkeypatch)

    adapter.send_message("mensagem")

    assert events.index("send_button_enabled") < events.index("send_trigger_started")
    assert events.index("send_trigger_started") < events.index("click")
    button.click.assert_called_once_with(timeout=adapter.timeout_ms)


@pytest.mark.parametrize(
    ("conversation_url", "pre_send_user_turn_count"),
    [
        ("https://chatgpt.com/", 0),
        (f"https://chatgpt.com{CONVERSATION_A}", 3),
    ],
    ids=("new_conversation", "reused_conversation"),
)
def test_send_captures_hydrated_user_turn_baseline_before_effect_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conversation_url: str,
    pre_send_user_turn_count: int,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = conversation_url
    roots = MagicMock()
    roots.count.return_value = 1
    root = roots.first
    user_turns = MagicMock()
    user_turns.count.return_value = pre_send_user_turn_count
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = pre_send_user_turn_count
    messages = [MagicMock() for _index in range(pre_send_user_turn_count * 2)]
    for index, message in enumerate(messages):
        message.get_attribute.side_effect = lambda name, index=index: {
            "data-message-author-role": "user" if index % 2 == 0 else "assistant",
            "data-message-id": f"message-{index}",
            "id": None,
        }.get(name)
    turns = MagicMock()
    turns.count.return_value = len(messages)
    turns.nth.side_effect = lambda index: messages[index]
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    page.locator.return_value = roots
    composer = MagicMock()
    composer.count.return_value = 1
    button = MagicMock()
    button.count.return_value = 1
    checkpoints: list[str] = []
    events: list[str] = []

    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_focus_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter, "_ensure_empty_composer_before_insert", lambda: composer
    )
    monkeypatch.setattr(adapter, "_insert_composer_text", lambda *_args: composer)
    monkeypatch.setattr(
        adapter, "_wait_for_normal_composer_insertion", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    monkeypatch.setattr(adapter, "_wait_for_send_button_enabled", lambda: button)
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: False)
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)
    button.click.side_effect = lambda **_kwargs: events.append("click")

    def effect_boundary() -> None:
        state = adapter._structural_send_proof
        assert state is not None
        assert state.pre_send_user_turn_count == pre_send_user_turn_count
        events.append("send_attempt_started")

    adapter.send_message("request", on_send_attempt_started=effect_boundary)

    state = adapter._structural_send_proof
    assert state is not None
    assert state.pre_send_user_turn_count == pre_send_user_turn_count
    assert state.turn_anchor is not None
    assert checkpoints.index("send_button_enabled") < checkpoints.index(
        "send_trigger_started"
    )
    assert events == ["send_attempt_started", "click"]
    assert user_turns.count.call_count == 2


def test_send_fails_before_effect_when_hydrated_user_turn_baseline_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    adapter.timeout_ms = 0
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    roots = MagicMock()
    roots.count.return_value = 0
    page.locator.return_value = roots
    composer = MagicMock()
    composer.count.return_value = 1
    button = MagicMock()
    button.count.return_value = 1
    boundary = MagicMock()
    checkpoints: list[str] = []

    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_focus_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter, "_ensure_empty_composer_before_insert", lambda: composer
    )
    monkeypatch.setattr(adapter, "_insert_composer_text", lambda *_args: composer)
    monkeypatch.setattr(
        adapter, "_wait_for_normal_composer_insertion", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    monkeypatch.setattr(adapter, "_wait_for_send_button_enabled", lambda: button)
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message("request", on_send_attempt_started=boundary)

    assert captured.value.code == "BROWSER_PRE_SEND_USER_TURN_BASELINE_UNAVAILABLE"
    assert checkpoints[-1] == "send_button_enabled"
    assert "send_trigger_started" not in checkpoints
    boundary.assert_not_called()
    button.click.assert_not_called()


def test_composer_fill_failure_occurs_before_send_attempt_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    composer.fill.side_effect = RuntimeError("fill failed")
    page = MagicMock()
    page.keyboard.insert_text.side_effect = RuntimeError("fallback failed")
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    _bypass_composer_verification(adapter, monkeypatch)
    boundaries: list[str] = []

    with pytest.raises(ConflictError) as captured:
        adapter.send_message(
            "mensagem",
            on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
        )
    assert captured.value.code == "BROWSER_COMPOSER_FILL_FAILED"
    assert boundaries == []


def test_preinsert_composer_baseline_already_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    clear_composer = MagicMock()
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: "")
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_clear_composer", clear_composer)

    result = adapter._ensure_empty_composer_before_insert()

    assert result is composer
    clear_composer.assert_not_called()


def test_preinsert_composer_removes_stale_pasted_text_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    attachment_counts = iter((1, 0))
    clear_composer = MagicMock()
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: "")
    monkeypatch.setattr(
        adapter,
        "_composer_pasted_text_attachment_count",
        lambda _composer: next(attachment_counts),
    )
    monkeypatch.setattr(adapter, "_clear_composer", clear_composer)

    result = adapter._ensure_empty_composer_before_insert()

    assert result is composer
    clear_composer.assert_called_once_with(pasted_text_attachment_baseline=0)


def test_preinsert_composer_removes_stale_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    composer_texts = iter(("stale draft", ""))
    clear_composer = MagicMock()
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter, "_composer_text", lambda _composer: next(composer_texts)
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_clear_composer", clear_composer)

    result = adapter._ensure_empty_composer_before_insert()

    assert result is composer
    clear_composer.assert_called_once_with(pasted_text_attachment_baseline=0)


def test_stale_draft_clear_failure_prevents_insert_and_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    insert_composer_text = MagicMock()
    send_button = MagicMock()
    checkpoints: list[str] = []
    boundaries: list[str] = []
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_focus_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: "stale draft")
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(
        adapter,
        "_clear_composer",
        MagicMock(side_effect=RuntimeError("clear failed")),
    )
    monkeypatch.setattr(adapter, "_insert_composer_text", insert_composer_text)
    monkeypatch.setattr(adapter, "_send_button", send_button)
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message(
            "request",
            on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
        )

    assert captured.value.code == "BROWSER_COMPOSER_STALE_DRAFT_CLEAR_FAILED"
    assert captured.value.context.evidence == {
        "composer_text_length": len("stale draft"),
        "pasted_text_attachment_count": 0,
    }
    assert checkpoints == ["composer_found"]
    assert boundaries == []
    insert_composer_text.assert_not_called()
    send_button.assert_not_called()


def test_composer_must_retain_focus_before_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_focus_composer",
        MagicMock(
            side_effect=ConflictError(
                "BROWSER_COMPOSER_FOCUS_TIMEOUT",
                "No stable focusable composer appeared",
            )
        ),
    )
    insert = MagicMock()
    paste = MagicMock()
    send_button = MagicMock()
    monkeypatch.setattr(adapter, "_insert_composer_text", insert)
    monkeypatch.setattr(adapter, "_paste_composer_text", paste)
    monkeypatch.setattr(adapter, "_send_button", send_button)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message("mensagem")

    assert captured.value.code == "BROWSER_COMPOSER_FOCUS_TIMEOUT"
    composer.fill.assert_not_called()
    insert.assert_not_called()
    paste.assert_not_called()
    send_button.assert_not_called()


def _focus_metadata(tag_name: str) -> dict[str, object]:
    return {
        "tag_name": tag_name,
        "contenteditable": "true" if tag_name == "div" else None,
        "role": "textbox",
        "id": "prompt-textarea",
        "data_testid": None,
        "is_content_editable": tag_name == "div",
        "focused": False,
        "editable_descendant_count": 0,
    }


def _configure_focus_poll(
    adapter: ChatGPTWebAdapter,
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[MagicMock],
    metadata: dict[int, dict[str, object]],
) -> MagicMock:
    page = MagicMock()
    pending = iter(candidates)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: next(pending))
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _candidate: True)
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda candidate, **_kwargs: metadata[id(candidate)],
    )
    monkeypatch.setattr(
        adapter,
        "_composer_focus_state",
        lambda _candidate, **_kwargs: (True, True),
    )

    def audit(
        candidate: MagicMock, *, phase: str, timeout_ms: int | None = None
    ) -> dict[str, object]:
        assert timeout_ms == 500
        assert phase == "focused"
        return {**metadata[id(candidate)], "focused": True}

    monkeypatch.setattr(adapter, "_audit_composer", audit)
    for candidate in candidates:
        candidate.count.return_value = 1
    return page


def test_focus_composer_reacquires_contenteditable_after_transient_textarea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    textarea = MagicMock()
    contenteditable = MagicMock()
    page = _configure_focus_poll(
        adapter,
        monkeypatch,
        [textarea, contenteditable, contenteditable],
        {
            id(textarea): _focus_metadata("textarea"),
            id(contenteditable): _focus_metadata("div"),
        },
    )

    focused = adapter._focus_composer()

    assert focused is contenteditable
    textarea.click.assert_not_called()
    contenteditable.click.assert_called_once_with(timeout=500)
    contenteditable.focus.assert_called_once_with(timeout=500)
    assert page.wait_for_timeout.call_count == 2


def test_focus_composer_reacquires_after_initial_locator_detaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    detached = MagicMock()
    detached.click.side_effect = RuntimeError("detached")
    contenteditable = MagicMock()
    _configure_focus_poll(
        adapter,
        monkeypatch,
        [detached, contenteditable, contenteditable],
        {
            id(detached): _focus_metadata("div"),
            id(contenteditable): _focus_metadata("div"),
        },
    )

    focused = adapter._focus_composer()

    assert focused is contenteditable
    detached.click.assert_called_once_with(timeout=500)
    contenteditable.click.assert_called_once_with(timeout=500)


def test_focus_composer_uses_existing_contenteditable_after_two_stable_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    contenteditable = MagicMock()
    _configure_focus_poll(
        adapter,
        monkeypatch,
        [contenteditable, contenteditable],
        {id(contenteditable): _focus_metadata("div")},
    )

    assert adapter._focus_composer() is contenteditable
    contenteditable.click.assert_called_once_with(timeout=500)


def test_focus_composer_times_out_when_no_focusable_editor_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    missing = MagicMock()
    missing.count.return_value = 0
    times = iter((0.0, 30.1))
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: missing)

    with pytest.raises(ConflictError) as captured:
        adapter._focus_composer()

    assert captured.value.code == "BROWSER_COMPOSER_FOCUS_TIMEOUT"
    missing.click.assert_not_called()


def test_focus_composer_treats_persistent_textarea_as_hydration_until_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    textarea = MagicMock()
    textarea.count.return_value = 1
    times = iter((0.0, 10.0, 30.1))
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: textarea)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _candidate: True)
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _candidate, **_kwargs: _focus_metadata("textarea"),
    )

    with pytest.raises(ConflictError) as captured:
        adapter._focus_composer()

    assert captured.value.code == "BROWSER_COMPOSER_FOCUS_TIMEOUT"
    assert page.wait_for_timeout.call_count == 1
    textarea.click.assert_not_called()
    textarea.focus.assert_not_called()


def test_focus_composer_reacquires_replaced_contenteditable_until_focus_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    first = MagicMock()
    replacement = MagicMock()
    page = _configure_focus_poll(
        adapter,
        monkeypatch,
        [first, replacement, replacement, replacement],
        {
            id(first): _focus_metadata("div"),
            id(replacement): _focus_metadata("div"),
        },
    )
    focus_states = iter(((True, True), (True, False), (True, True), (True, True)))
    monkeypatch.setattr(
        adapter,
        "_composer_focus_state",
        lambda _candidate, **_kwargs: next(focus_states),
    )

    focused = adapter._focus_composer()

    assert focused is replacement
    first.click.assert_called_once_with(timeout=500)
    replacement.click.assert_called_once_with(timeout=500)
    replacement.focus.assert_called_once_with(timeout=500)
    assert page.wait_for_timeout.call_count == 3


def test_focusable_composer_prefers_visible_contenteditable_over_textarea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    preferred = MagicMock()
    preferred.count.return_value = 1
    contenteditable = MagicMock()
    preferred.nth.return_value = contenteditable
    page.locator.return_value = preferred
    fallback = MagicMock(side_effect=AssertionError("textarea fallback must not be used"))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", fallback)
    monkeypatch.setattr(
        adapter,
        "_is_focusable_editor",
        lambda candidate: candidate is contenteditable,
    )

    assert adapter._focusable_composer() is contenteditable
    fallback.assert_not_called()


def test_focusable_editor_uses_short_actionability_timeouts(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    candidate = MagicMock()
    candidate.is_visible.return_value = True
    candidate.is_enabled.return_value = True
    candidate.is_editable.return_value = True

    assert adapter._is_focusable_editor(candidate) is True
    candidate.is_visible.assert_called_once_with(timeout=500)
    candidate.is_enabled.assert_called_once_with(timeout=500)
    candidate.is_editable.assert_called_once_with(timeout=500)


def test_large_composer_payload_uses_windows_sendinput_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    page = MagicMock()
    payload = "request integral\n" * 2500
    clipboard = {"value": "existing clipboard"}
    editor = {"value": ""}

    def write_clipboard(value: str) -> None:
        clipboard["value"] = value

    emitted: list[tuple[int, bool]] = []

    def send_virtual_key(key: int, *, key_up: bool) -> None:
        emitted.append((key, key_up))
        if key == 0x56 and not key_up:
            editor["value"] = payload

    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: editor["value"])
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _composer: _focused_composer_metadata(),
    )
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(adapter, "_windows_clipboard_write", write_clipboard)
    monkeypatch.setattr(adapter, "_windows_send_virtual_key", send_virtual_key)
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", lambda: 202)
    monkeypatch.setattr(adapter, "_focused_composer_for_native_paste", lambda _hwnd: composer)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_native_paste_editor_focus_state", lambda _composer: (True, True))
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    send_message = MagicMock(side_effect=AssertionError("send must not be used"))
    send_button = MagicMock(side_effect=AssertionError("send button must not be resolved"))
    monkeypatch.setattr(adapter, "send_message", send_message)
    monkeypatch.setattr(adapter, "_send_button", send_button)

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        assert adapter._insert_composer_text(composer, payload) is composer

    composer.fill.assert_not_called()
    assert emitted == [(0x11, False), (0x56, False), (0x56, True), (0x11, True)]
    composer.press.assert_not_called()
    page.keyboard.insert_text.assert_not_called()
    assert clipboard["value"] == "existing clipboard"
    messages = [record.message for record in caplog.records]
    assert any(
        "clipboard_expected_length=" in message and "clipboard_observed_sha=" in message
        for message in messages
    )
    assert any("paste_key_completed=True" in message for message in messages)
    assert any("editor_focused_after_paste=True" in message for message in messages)
    send_message.assert_not_called()
    send_button.assert_not_called()


def test_large_paste_with_matching_clipboard_sha_reaches_send_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    _bypass_composer_verification(adapter, monkeypatch)
    adapter.timeout_ms = 0
    payload = "x" * 40_000
    composer = MagicMock()
    composer.count.return_value = 1
    page = MagicMock()
    editor = {"value": ""}
    clipboard = {"value": "previous"}

    def write_clipboard(value: str) -> None:
        clipboard["value"] = value

    def send_virtual_key(key: int, *, key_up: bool) -> None:
        if key == 0x56 and not key_up:
            editor["value"] = payload

    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: editor["value"])
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _composer: _focused_composer_metadata(),
    )
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(adapter, "_windows_clipboard_write", write_clipboard)
    monkeypatch.setattr(adapter, "_windows_send_virtual_key", send_virtual_key)
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", lambda: 202)
    monkeypatch.setattr(adapter, "_focused_composer_for_native_paste", lambda _hwnd: composer)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_native_paste_editor_focus_state", lambda _composer: (True, True))
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    button = MagicMock()
    button.count.return_value = 1
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    button.click.side_effect = lambda **_kwargs: editor.__setitem__("value", "")
    monkeypatch.setattr(adapter, "_send_button", lambda: button)
    checkpoints: list[str] = []
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)
    boundaries: list[str] = []

    adapter.send_message(
        payload,
        on_send_attempt_started=lambda: boundaries.append("send_attempt_started"),
    )

    assert checkpoints == [
        "composer_found",
        "composer_filled",
        "send_button_found",
        "send_button_enabled",
        "send_trigger_started",
        "send_trigger_completed",
        "composer_cleared",
    ]
    assert boundaries == ["send_attempt_started"]
    composer.press.assert_not_called()
    page.keyboard.insert_text.assert_not_called()


def test_large_paste_clipboard_sha_mismatch_remains_pre_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "x" * 40_000
    composer = MagicMock()
    clipboard_reads = iter(("previous", "corrupted clipboard"))
    clipboard_write = MagicMock()
    ctrl_v = MagicMock()
    foreground = MagicMock()
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: next(clipboard_reads))
    monkeypatch.setattr(adapter, "_windows_clipboard_write", clipboard_write)
    monkeypatch.setattr(adapter, "_windows_ctrl_v", ctrl_v)
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", foreground)

    with pytest.raises(ConflictError) as captured:
        adapter._paste_composer_text(composer, payload)

    assert captured.value.code == "BROWSER_COMPOSER_CLIPBOARD_MISMATCH"
    foreground.assert_not_called()
    ctrl_v.assert_not_called()
    assert clipboard_write.call_args_list == [call(payload), call("previous")]


def test_large_paste_failure_does_not_start_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    _bypass_composer_verification(adapter, monkeypatch)
    payload = "x" * 40_000
    composer = MagicMock()
    composer.count.return_value = 1
    page = MagicMock()
    clipboard = {"value": "previous"}
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(
        adapter,
        "_windows_send_virtual_key",
        lambda _key, *, key_up: (_ for _ in ()).throw(OSError("SendInput failed")),
    )
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", lambda: 202)
    monkeypatch.setattr(adapter, "_focused_composer_for_native_paste", lambda _hwnd: composer)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_native_paste_editor_focus_state", lambda _composer: (True, True))
    monkeypatch.setattr(
        adapter, "_windows_clipboard_write", lambda value: clipboard.__setitem__("value", value)
    )
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    send_button = MagicMock(side_effect=AssertionError("send must not be resolved"))
    monkeypatch.setattr(adapter, "_send_button", send_button)
    checkpoints: list[str] = []
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message(payload)

    assert captured.value.code == "BROWSER_COMPOSER_NATIVE_PASTE_FAILED"
    assert checkpoints == ["composer_found"]
    send_button.assert_not_called()
    assert "Enter" not in [call.args[0] for call in composer.press.call_args_list]
    page.keyboard.press.assert_not_called()


def test_windows_ctrl_v_releases_control_after_intermediate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    emitted: list[tuple[int, bool]] = []

    def send_virtual_key(key: int, *, key_up: bool) -> None:
        emitted.append((key, key_up))
        if key == 0x56 and not key_up:
            raise OSError("SendInput failed")

    monkeypatch.setattr(adapter, "_windows_send_virtual_key", send_virtual_key)

    with pytest.raises(OSError):
        adapter._windows_ctrl_v()

    assert emitted == [(0x11, False), (0x56, False), (0x11, True)]


def test_composer_center_is_converted_from_renderer_to_dedicated_window_screen_coordinates(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)

    assert adapter._composer_center_screen_point(
        box={"x": 390, "y": 600, "width": 600, "height": 100},
        window_rect=(100, 50, 1850, 1175),
        viewport_metrics={
            "outer_width": 1400,
            "outer_height": 900,
            "inner_width": 1380,
            "inner_height": 800,
        },
    ) == (975, 975)


def test_windows_native_click_emits_mouse_down_then_mouse_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    emitted: list[tuple[str, int | bool, int | None]] = []
    monkeypatch.setattr(
        adapter,
        "_windows_set_cursor_position",
        lambda x, y: emitted.append(("position", x, y)),
    )
    monkeypatch.setattr(
        adapter,
        "_windows_send_mouse_button",
        lambda *, key_up: emitted.append(("button", key_up, None)),
    )

    adapter._windows_click_left(975, 975)

    assert emitted == [
        ("position", 975, 975),
        ("button", False, None),
        ("button", True, None),
    ]


def test_native_paste_clicks_editor_before_permitting_sendinput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    page = MagicMock()
    observed_states = iter(((False, False), (True, True)))
    emitted: list[tuple[int, bool]] = []
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        adapter,
        "_native_paste_composer_screen_point",
        lambda _composer, _hwnd: (975, 975),
    )
    native_click = MagicMock()
    monkeypatch.setattr(adapter, "_windows_click_left", native_click)
    monkeypatch.setattr(
        adapter,
        "_native_paste_editor_focus_state",
        lambda _composer: next(observed_states),
    )
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_windows_send_virtual_key",
        lambda key, *, key_up: emitted.append((key, key_up)),
    )

    adapter._focused_composer_for_native_paste(202)
    adapter._windows_ctrl_v()

    native_click.assert_called_once_with(975, 975)
    composer.click.assert_not_called()
    composer.focus.assert_not_called()
    page.wait_for_timeout.assert_called_once_with(100)
    assert emitted == [(0x11, False), (0x56, False), (0x56, True), (0x11, True)]
    composer.press.assert_not_called()
    page.keyboard.press.assert_not_called()


def test_native_paste_without_renderer_focus_prohibits_sendinput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    page = MagicMock()
    emitted: list[tuple[int, bool]] = []
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        adapter,
        "_native_paste_composer_screen_point",
        lambda _composer, _hwnd: (975, 975),
    )
    native_click = MagicMock()
    monkeypatch.setattr(adapter, "_windows_click_left", native_click)
    monkeypatch.setattr(
        adapter,
        "_native_paste_editor_focus_state",
        lambda _composer: (False, False),
    )
    monkeypatch.setattr(
        adapter,
        "_windows_send_virtual_key",
        lambda key, *, key_up: emitted.append((key, key_up)),
    )

    with pytest.raises(ConflictError) as captured:
        adapter._focused_composer_for_native_paste(202)

    assert captured.value.code == "BROWSER_NATIVE_PASTE_EDITOR_FOCUS_FAILED"
    native_click.assert_called_once_with(975, 975)
    composer.click.assert_not_called()
    composer.focus.assert_not_called()
    page.wait_for_timeout.assert_called_once_with(100)
    assert emitted == []
    composer.press.assert_not_called()
    page.keyboard.press.assert_not_called()


def test_native_click_failure_prohibits_ctrl_v(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "x" * 40_000
    composer = MagicMock()
    composer.count.return_value = 1
    clipboard = {"value": "previous"}
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(
        adapter,
        "_windows_clipboard_write",
        lambda value: clipboard.__setitem__("value", value),
    )
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", lambda: 202)
    monkeypatch.setattr(adapter, "_native_paste_editor_focus_state", lambda _composer: (True, True))
    monkeypatch.setattr(
        adapter,
        "_native_paste_composer_screen_point",
        lambda _composer, _hwnd: (975, 975),
    )
    monkeypatch.setattr(
        adapter,
        "_windows_click_left",
        MagicMock(side_effect=OSError("native click failed")),
    )
    ctrl_v = MagicMock()
    monkeypatch.setattr(adapter, "_windows_ctrl_v", ctrl_v)

    with pytest.raises(ConflictError) as captured:
        adapter._paste_composer_text(composer, payload)

    assert captured.value.code == "BROWSER_NATIVE_PASTE_EDITOR_CLICK_FAILED"
    ctrl_v.assert_not_called()
    assert clipboard["value"] == "previous"


def test_multiple_chrome_windows_with_one_dedicated_profile_permits_sendinput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    emitted: list[tuple[int, bool]] = []
    command_lines = {
        10: 'chrome.exe --user-data-dir="C:\\other-profile"',
        20: f'chrome.exe --user-data-dir="{adapter.profile_dir}"',
        30: "chrome.exe",
    }

    monkeypatch.setattr(
        adapter,
        "_windows_visible_chrome_windows",
        lambda: ((101, 10), (202, 20), (303, 30)),
    )
    monkeypatch.setattr(adapter, "_windows_process_command_line", command_lines.__getitem__)
    monkeypatch.setattr(adapter, "_windows_foreground_window", lambda: 202)
    monkeypatch.setattr(
        adapter,
        "_windows_send_virtual_key",
        lambda key, *, key_up: emitted.append((key, key_up)),
    )

    adapter._ensure_windows_native_paste_foreground()
    adapter._windows_ctrl_v()

    assert emitted == [(0x11, False), (0x56, False), (0x56, True), (0x11, True)]


def test_wrong_foreground_prohibits_sendinput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    emitted: list[tuple[int, bool]] = []
    activations: list[int] = []
    foreground = iter((55, 55))
    monkeypatch.setattr(
        adapter,
        "_windows_visible_chrome_windows",
        lambda: ((101, 10),),
    )
    monkeypatch.setattr(
        adapter,
        "_windows_process_command_line",
        lambda _pid: f'chrome.exe --user-data-dir="{adapter.profile_dir}"',
    )
    monkeypatch.setattr(adapter, "_windows_foreground_window", lambda: next(foreground))
    monkeypatch.setattr(adapter, "_windows_set_foreground_window", activations.append)
    monkeypatch.setattr(
        adapter,
        "_windows_send_virtual_key",
        lambda key, *, key_up: emitted.append((key, key_up)),
    )

    with pytest.raises(ConflictError) as captured:
        adapter._ensure_windows_native_paste_foreground()

    assert captured.value.code == "BROWSER_NATIVE_PASTE_WINDOW_FOCUS_FAILED"
    assert activations == [101]
    assert emitted == []


@pytest.mark.parametrize(
    ("windows", "profile_process_ids", "expected_code"),
    [
        (((101, 10), (202, 20)), set(), "BROWSER_NATIVE_PASTE_WINDOW_NOT_FOUND"),
        (
            ((101, 10), (202, 20)),
            {10, 20},
            "BROWSER_NATIVE_PASTE_WINDOW_AMBIGUOUS",
        ),
    ],
)
def test_chrome_window_profile_filter_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows: tuple[tuple[int, int], ...],
    profile_process_ids: set[int],
    expected_code: str,
) -> None:
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(
        adapter,
        "_windows_visible_chrome_windows",
        lambda: windows,
    )
    monkeypatch.setattr(
        adapter,
        "_windows_process_command_line",
        lambda process_id: (
            f'chrome.exe --user-data-dir="{adapter.profile_dir}"'
            if process_id in profile_process_ids
            else "chrome.exe --user-data-dir=C:\\other-profile"
        ),
    )
    send_input = MagicMock()
    monkeypatch.setattr(adapter, "_windows_send_virtual_key", send_input)

    with pytest.raises(ConflictError) as captured:
        adapter._ensure_windows_native_paste_foreground()

    assert captured.value.code == expected_code
    send_input.assert_not_called()


def test_composer_locator_resolves_visible_editable_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    wrapper_collection = MagicMock()
    wrapper_collection.count.return_value = 1
    wrapper = MagicMock()
    wrapper_collection.nth.return_value = wrapper
    wrapper.is_visible.return_value = True
    wrapper.evaluate.return_value = {
        "tag_name": "div",
        "contenteditable": None,
        "role": None,
        "id": "prompt-textarea",
        "data_testid": None,
        "is_content_editable": False,
        "focused": False,
        "editable_descendant_count": 1,
    }
    descendants = MagicMock()
    descendants.count.return_value = 1
    editor = MagicMock()
    descendants.nth.return_value = editor
    wrapper.locator.return_value = descendants
    editor.is_visible.return_value = True
    editor.evaluate.return_value = {
        "tag_name": "textarea",
        "contenteditable": None,
        "role": "textbox",
        "id": None,
        "data_testid": None,
        "is_content_editable": False,
        "focused": False,
        "editable_descendant_count": 0,
    }
    page.locator.side_effect = lambda selector: (
        wrapper_collection if selector == "#prompt-textarea" else MagicMock()
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    assert adapter._composer() is editor


def test_composer_text_reads_value_for_textarea_and_inner_text_for_contenteditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    editor = MagicMock()
    metadata = {
        "tag_name": "textarea",
        "contenteditable": None,
        "role": "textbox",
        "id": "prompt-textarea",
        "data_testid": None,
        "is_content_editable": False,
        "focused": True,
        "editable_descendant_count": 0,
    }
    monkeypatch.setattr(adapter, "_composer_metadata", lambda _editor: metadata)
    editor.input_value.return_value = "request integral"
    assert adapter._composer_text(editor) == "request integral"
    editor.input_value.assert_called_once_with(timeout=adapter.timeout_ms)
    editor.inner_text.assert_not_called()

    metadata["tag_name"] = "div"
    metadata["contenteditable"] = "true"
    metadata["is_content_editable"] = True
    editor.evaluate.return_value = "request integral"
    assert adapter._composer_text(editor) == "request integral"
    editor.evaluate.assert_called_once()
    editor.inner_text.assert_not_called()


def test_composer_expected_text_accepts_exact_text_content_after_keyboard_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    editor = MagicMock()
    metadata = _focused_composer_metadata()
    metadata.update(
        tag_name="div",
        contenteditable="true",
        is_content_editable=True,
    )
    monkeypatch.setattr(adapter, "_composer_metadata", lambda _editor: metadata)
    editor.evaluate.return_value = "linha 1\n\nlinha 2"
    editor.text_content.return_value = "linha 1\nlinha 2"
    expected = transport_fingerprint("linha 1\nlinha 2")

    assert adapter._composer_text_for_expected(editor, expected) == "linha 1\nlinha 2"
    editor.text_content.assert_called_once_with(timeout=adapter.timeout_ms)


def test_paste_verification_starts_empty_then_accepts_two_nonempty_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    editors = [MagicMock(), MagicMock(), MagicMock()]
    for editor in editors:
        editor.count.return_value = 1
    observations = iter(("", "request integral", "request integral"))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: editors.pop(0))
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: next(observations),
    )
    expected = transport_fingerprint("request integral")

    result = adapter._wait_for_pasted_composer(
        "request integral", expected, timeout_ms=1000
    )

    assert result.count() == 1
    assert page.wait_for_timeout.call_count == 3


def test_paste_verification_accepts_stable_nonempty_dom_without_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    observations = iter(("representação parcial", "representação diferente"))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: next(observations),
    )
    expected = transport_fingerprint("request integral")

    adapter._wait_for_pasted_composer("request integral", expected, timeout_ms=1000)

    assert page.wait_for_timeout.call_count == 2


def test_paste_verification_reacquires_replaced_editor_on_every_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    stale_editor = MagicMock()
    replacement_editor = MagicMock()
    stable_editor = MagicMock()
    editors = iter((stale_editor, replacement_editor, stable_editor))
    for editor in (stale_editor, replacement_editor, stable_editor):
        editor.count.return_value = 1
    reads: list[object] = []

    def read(editor: object) -> str:
        reads.append(editor)
        return "" if editor is stale_editor else "request integral"

    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: next(editors))
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(adapter, "_composer_text", read)
    expected = transport_fingerprint("request integral")

    result = adapter._wait_for_pasted_composer(
        "request integral", expected, timeout_ms=1000
    )

    assert result is stable_editor
    assert reads == [stale_editor, replacement_editor, stable_editor]


def test_paste_verification_timeout_remains_pre_send_and_does_not_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter(tmp_path)
    _bypass_composer_verification(adapter, monkeypatch)
    payload = "x" * 40_000
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    clipboard = {"value": "previous"}
    checkpoints: list[str] = []
    ctrl_v = MagicMock()
    send_button = MagicMock(side_effect=AssertionError("send must remain pre-send"))
    clear_composer = MagicMock()
    times = iter((0.0, 16.0))
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: "",
    )
    monkeypatch.setattr(adapter, "_windows_clipboard_read", lambda: clipboard["value"])
    monkeypatch.setattr(
        adapter,
        "_windows_clipboard_write",
        lambda value: clipboard.__setitem__("value", value),
    )
    monkeypatch.setattr(adapter, "_ensure_windows_native_paste_foreground", lambda: 202)
    monkeypatch.setattr(adapter, "_focused_composer_for_native_paste", lambda _hwnd: composer)
    monkeypatch.setattr(adapter, "_windows_ctrl_v", ctrl_v)
    monkeypatch.setattr(adapter, "_send_button", send_button)
    monkeypatch.setattr(adapter, "_clear_composer", clear_composer)
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)

    with caplog.at_level(
        "DEBUG", logger="ebook_pipeline.browser.chatgpt"
    ), pytest.raises(ConflictError) as captured:
        adapter.send_message(payload)

    assert captured.value.code == "BROWSER_COMPOSER_PASTE_VERIFICATION_TIMEOUT"
    assert captured.value.context.evidence == {
        "expected_length": len(payload),
        "last_observed_length": 0,
        "stable_nonempty_reads": 0,
        "pasted_text_attachment_count_before": 0,
        "pasted_text_attachment_count_after": 0,
        "poll_count": 1,
    }
    assert checkpoints == ["composer_found"]
    ctrl_v.assert_called_once_with()
    send_button.assert_not_called()
    clear_composer.assert_not_called()
    assert clipboard["value"] == "previous"


def test_big_paste_accepts_exactly_one_new_attachment_without_reading_its_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: "",
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 3
    )
    expected = transport_fingerprint("request integral")

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        result = adapter._wait_for_pasted_composer(
            "request integral",
            expected,
            pasted_text_attachment_count_before=2,
            timeout_ms=1000,
        )

    assert result is composer
    message = next(
        record.message
        for record in caplog.records
        if "browser_composer_big_paste" in record.message
    )
    assert "big_paste_detected=True" in message
    assert "pasted_text_attachment_count_before=2" in message
    assert "pasted_text_attachment_count_after=3" in message
    assert "attachment_added=True" in message
    assert "composer_text_length=0" in message
    assert "request integral" not in message


def test_composer_probe_accepts_new_pasted_text_attachment_without_text_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    composer = MagicMock()
    composer.count.return_value = 1
    attachment_counts = iter((0, 1))
    fingerprint_gate = MagicMock()
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_composer_pasted_text_attachment_count",
        lambda _composer: next(attachment_counts),
    )
    monkeypatch.setattr(adapter, "_insert_composer_text", lambda _composer, _text: composer)
    monkeypatch.setattr(adapter, "_composer_text_for_expected", lambda *_args: "")
    monkeypatch.setattr(adapter, "_stable_composer_reads", fingerprint_gate)

    result = adapter._probe_composer_payload("x" * 40_000)

    assert result.observed_length == 0
    assert result.editor_metadata["big_paste_detected"] is True
    assert result.editor_metadata["pasted_text_attachment_count_before"] == 0
    assert result.editor_metadata["pasted_text_attachment_count_after"] == 1
    assert result.editor_metadata["attachment_added"] is True
    assert result.editor_metadata["composer_text_length"] == 0
    fingerprint_gate.assert_not_called()


@pytest.mark.parametrize(
    ("pasted_text_attachment_count_after", "expected_error"),
    [
        (2, "BROWSER_COMPOSER_PASTE_VERIFICATION_TIMEOUT"),
        (4, "BROWSER_COMPOSER_PASTE_ATTACHMENT_DELTA_INVALID"),
    ],
)
def test_big_paste_rejects_preexisting_or_multiple_new_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pasted_text_attachment_count_after: int,
    expected_error: str,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    times = iter((0.0, 16.0))
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: "",
    )
    monkeypatch.setattr(
        adapter,
        "_composer_pasted_text_attachment_count",
        lambda _composer: pasted_text_attachment_count_after,
    )

    with pytest.raises(ConflictError) as captured:
        adapter._wait_for_pasted_composer(
            "request integral",
            transport_fingerprint("request integral"),
            pasted_text_attachment_count_before=2,
        )

    assert captured.value.code == expected_error


def test_pasted_text_attachment_count_uses_semantic_opener_in_composer_form() -> None:
    composer = MagicMock()
    form = composer.locator.return_value
    form.count.return_value = 1
    attachments = MagicMock()
    attachments.count.return_value = 2
    form.locator.return_value = attachments

    result = ChatGPTWebAdapter._composer_pasted_text_attachment_count(composer)

    assert result == 2
    composer.locator.assert_called_once_with("xpath=ancestor::form[1]")
    form.locator.assert_called_once_with(COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR)
    attachments.text_content.assert_not_called()


def test_pasted_text_attachment_removal_is_scoped_to_its_semantic_group() -> None:
    composer = MagicMock()
    form = composer.locator.return_value
    attachments = MagicMock()
    attachment = MagicMock()
    attachments.first = attachment
    form.locator.return_value = attachments
    group = attachment.locator.return_value
    group.count.return_value = 1
    remove_control = group.locator.return_value
    remove_control.count.return_value = 1

    ChatGPTWebAdapter._remove_one_pasted_text_attachment(composer)

    form.locator.assert_called_once_with(COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR)
    attachment.locator.assert_called_once_with("xpath=ancestor::*[@role='group'][1]")
    group.locator.assert_called_once_with(COMPOSER_PASTED_TEXT_ATTACHMENT_REMOVE_SELECTOR)
    remove_control.click.assert_called_once_with()


def test_reconciliation_reads_pasted_text_attachment_without_send() -> None:
    adapter = _adapter(Path("."))
    page = MagicMock()
    opener = MagicMock()
    dialog = page.get_by_role.return_value
    dialog.count.return_value = 1
    payload = "request integral\n" * 100
    dialog.evaluate.return_value = [
        "Abrir anexo de texto colado",
        payload,
        payload,
    ]
    adapter._page = page

    observed = adapter._read_pasted_text_attachment(
        opener, transport_fingerprint(payload)
    )

    assert observed == payload
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    dialog.wait_for.assert_called_once_with(state="visible", timeout=adapter.timeout_ms)
    page.keyboard.press.assert_called_once_with("Escape")
    assert "Enter" not in [call.args[0] for call in page.keyboard.press.call_args_list]
    page.get_by_role.assert_called_once_with("dialog")


def test_reconciliation_fails_closed_when_attachment_content_is_inaccessible() -> None:
    adapter = _adapter(Path("."))
    page = MagicMock()
    opener = MagicMock()
    dialog = page.get_by_role.return_value
    dialog.count.return_value = 1
    dialog.evaluate.return_value = ["metadata only"]
    adapter._page = page

    with pytest.raises(ConflictError) as captured:
        adapter._read_pasted_text_attachment(
            opener, transport_fingerprint("request integral")
        )

    assert captured.value.code == "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE"
    assert captured.value.context.evidence == {
        "matching_structural_representation_count": 0
    }
    page.keyboard.press.assert_called_once_with("Escape")


def test_probe_cleanup_removes_new_pasted_text_attachment_and_restores_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    attachment_count = {"value": 2}
    remove_attachment = MagicMock(
        side_effect=lambda _composer: attachment_count.__setitem__("value", 1)
    )
    stable_reads = MagicMock(
        return_value=(
            transport_fingerprint(""),
            transport_fingerprint(""),
            transport_fingerprint(""),
            0,
        )
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_composer_pasted_text_attachment_count",
        lambda _composer: attachment_count["value"],
    )
    monkeypatch.setattr(adapter, "_remove_one_pasted_text_attachment", remove_attachment)
    monkeypatch.setattr(adapter, "_stable_composer_reads", stable_reads)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )

    adapter._clear_composer(pasted_text_attachment_baseline=1)

    assert attachment_count["value"] == 1
    remove_attachment.assert_called_once_with(composer)
    composer.press.assert_any_call("Control+A", timeout=adapter.timeout_ms)
    composer.press.assert_any_call("Backspace", timeout=adapter.timeout_ms)
    stable_reads.assert_called_once_with(
        composer, transport_fingerprint(""), reacquire=True
    )


def test_large_attachment_proof_reaches_composer_filled_without_editor_fingerprint_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    _bypass_composer_verification(adapter, monkeypatch)
    payload = "x" * 40_000
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    checkpoints: list[str] = []
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda _composer: "",
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 1
    )

    def insert(_composer: object, text: str) -> object:
        return adapter._wait_for_pasted_composer(
            text,
            transport_fingerprint(text),
            pasted_text_attachment_count_before=0,
            timeout_ms=1000,
        )

    monkeypatch.setattr(adapter, "_insert_composer_text", insert)
    normal_text_gate = MagicMock()
    monkeypatch.setattr(
        adapter, "_wait_for_normal_composer_insertion", normal_text_gate
    )
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)
    monkeypatch.setattr(
        adapter,
        "_send_button",
        MagicMock(side_effect=ConflictError("TEST_STOP", "stop before Send")),
    )

    with pytest.raises(ConflictError) as captured:
        adapter.send_message(payload)

    assert captured.value.code == "TEST_STOP"
    assert checkpoints == ["composer_found", "composer_filled"]
    normal_text_gate.assert_not_called()


def test_normal_insert_accepts_two_stable_nonempty_observations_without_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    monkeypatch.setattr(adapter, "_start", lambda: page)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _editor: True)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _editor: 0
    )
    observations = iter(("representação DOM A", "representação DOM B"))
    monkeypatch.setattr(
        adapter,
        "_composer_text",
        lambda *_args, **_kwargs: next(observations),
    )

    adapter._wait_for_normal_composer_insertion(
        composer,
        pasted_text_attachment_count_before=0,
    )

    page.wait_for_timeout.assert_called_once_with(100)


def test_composer_verification_reacquires_editor_after_dom_rehydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    adapter.timeout_ms = 0
    page = MagicMock()
    monkeypatch.setattr(adapter, "_start", lambda: page)
    stale_editor = MagicMock()
    live_editor = MagicMock()
    live_editor.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: live_editor)
    reads: list[object] = []

    def read(editor: object) -> str:
        reads.append(editor)
        if editor is stale_editor:
            raise AssertionError("stale editor must not be read")
        return "request integral"

    monkeypatch.setattr(adapter, "_composer_text", read)
    expected = transport_fingerprint("request integral")

    result = adapter._stable_composer_reads(stale_editor, expected, reacquire=True)

    assert result == (expected, expected, expected, len("request integral"))
    assert reads == [live_editor, live_editor]


def test_normal_insert_verification_reacquires_after_textarea_becomes_contenteditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    textarea = MagicMock()
    textarea.count.return_value = 1
    contenteditable = MagicMock()
    contenteditable.count.return_value = 1
    editors = iter((textarea, contenteditable))
    reads: list[object] = []
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: next(editors))
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _editor: True)
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _editor: 0
    )

    def read(editor: object, *_args: object, **_kwargs: object) -> str:
        reads.append(editor)
        return "request integral"

    monkeypatch.setattr(adapter, "_composer_text", read)

    adapter._wait_for_normal_composer_insertion(
        textarea,
        pasted_text_attachment_count_before=0,
    )

    assert reads == [textarea, contenteditable]
    page.wait_for_timeout.assert_called_once_with(100)


def test_normal_insert_verification_rejects_unexpected_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    editor = MagicMock()
    editor.count.return_value = 1
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: editor)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _editor: True)
    monkeypatch.setattr(
        adapter, "_composer_text", lambda _editor, **_kwargs: "request"
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _editor: 1
    )

    with pytest.raises(ConflictError) as captured:
        adapter._wait_for_normal_composer_insertion(
            editor,
            pasted_text_attachment_count_before=0,
        )

    assert captured.value.code == "BROWSER_COMPOSER_INSERT_UNEXPECTED_ATTACHMENT"
    page.wait_for_timeout.assert_not_called()


def test_normal_insert_verification_empty_text_times_out_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    editor = MagicMock()
    editor.count.return_value = 1
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: editor)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _editor: True)
    monkeypatch.setattr(adapter, "_composer_text", lambda _editor, **_kwargs: "")
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _editor: 0
    )

    with caplog.at_level(
        "DEBUG", logger="ebook_pipeline.browser.chatgpt"
    ), pytest.raises(ConflictError) as captured:
        adapter._wait_for_normal_composer_insertion(
            editor,
            pasted_text_attachment_count_before=0,
            timeout_ms=0,
        )

    assert captured.value.code == "BROWSER_COMPOSER_INSERT_VERIFY_TIMEOUT"
    assert captured.value.context.evidence == {
        "observed_length": 0,
        "stable_reads": 0,
        "composer_nonempty": False,
        "pasted_text_attachment_count_before": 0,
        "pasted_text_attachment_count_after": 0,
    }
    assert not any("request esperado" in record.message for record in caplog.records)
    page.wait_for_timeout.assert_not_called()


def test_composer_text_diagnostic_compares_representations_without_content_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter(tmp_path)
    editor = MagicMock()
    editor.count.return_value = 1
    expected = "segredo-metodológico-linha-1\nlinha-2"
    editor.evaluate.return_value = {
        "textarea_value": None,
        "text_content": "segredo-metodológico-linha-1linha-2",
        "inner_text": "segredo-metodológico-linha-1\r\nlinha-2",
        "reconstructed": "segredo-metodológico-linha-1\nlinha-2\n",
    }
    monkeypatch.setattr(
        adapter,
        "_wait_for_hydrated_composer_read_only",
        lambda _conversation_path: editor,
    )

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        result = adapter.composer_text_diagnostic(expected, CONVERSATION_A)

    representations = result["representations"]
    assert isinstance(representations, dict)
    assert set(representations) == {
        "text_content",
        "inner_text",
        "reconstructed",
    }
    assert representations["text_content"] == {
        "raw_length": len("segredo-metodológico-linha-1linha-2"),
        "normalized_length": len("segredo-metodológico-linha-1linha-2"),
        "normalized_sha256": transport_fingerprint(
            "segredo-metodológico-linha-1linha-2"
        ),
        "matches_expected": False,
    }
    assert representations["inner_text"]["matches_expected"] is True
    assert representations["reconstructed"]["matches_expected"] is True
    assert result["comparison_representation"] == "reconstructed"
    assert result["first_divergence_index"] is None
    assert result["common_prefix_length"] == len(expected)
    assert result["common_suffix_length"] == 0
    assert result["length_delta"] == 0
    editor.evaluate.assert_called_once()
    assert editor.evaluate.call_args.args == (COMPOSER_TEXT_DIAGNOSTIC_SCRIPT,)
    assert editor.evaluate.call_args.kwargs == {"timeout": 500}
    assert "segredo-metodológico" not in json.dumps(result, ensure_ascii=False)
    assert not any("segredo-metodológico" in record.message for record in caplog.records)


def test_composer_text_diagnostic_script_compiles_and_executes_exact_source() -> None:
    node_executable, _cli_js = compute_driver_executable()
    source = json.dumps(COMPOSER_TEXT_DIAGNOSTIC_SCRIPT)
    harness = (
        f"const source={source};"
        "const diagnostic=(0,eval)('('+source+')');"
        "global.Node={TEXT_NODE:3,ELEMENT_NODE:1};"
        "global.HTMLTextAreaElement=class {};"
        "global.HTMLInputElement=class {};"
        "const text=(value)=>({nodeType:3,nodeValue:value});"
        "const element=(tagName,childNodes)=>({nodeType:1,tagName,childNodes});"
        "const root=element('DIV',["
        "element('P',[text('alpha')]),element('BR',[]),element('P',[text('beta')])"
        "]);"
        "root.textContent='alphabeta';"
        "root.innerText='alpha beta';"
        "process.stdout.write(JSON.stringify(diagnostic(root)));"
    )

    completed = subprocess.run(
        [node_executable],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "textarea_value": None,
        "text_content": "alphabeta",
        "inner_text": "alpha beta",
        "reconstructed": "alpha\n\nbeta\n",
    }


def test_composer_text_diagnostic_reports_normalized_divergence_boundaries() -> None:
    metadata = ChatGPTWebAdapter._text_divergence_metadata(
        "prefix-ABC-suffix",
        "prefix-XY-suffix",
    )

    assert metadata == {
        "first_divergence_index": len("prefix-"),
        "common_prefix_length": len("prefix-"),
        "common_suffix_length": len("-suffix"),
        "length_delta": -1,
    }


def test_composer_text_spike_navigates_and_waits_for_contenteditable_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    page.goto.side_effect = lambda url, **_kwargs: setattr(page, "url", url)
    textarea = MagicMock()
    textarea.count.return_value = 1
    contenteditable = MagicMock()
    contenteditable.count.return_value = 1
    candidates = iter((textarea, contenteditable, contenteditable))
    metadata = {
        id(textarea): _focus_metadata("textarea"),
        id(contenteditable): _focus_metadata("div"),
    }
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: next(candidates))
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _candidate: True)
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda candidate, **_kwargs: metadata[id(candidate)],
    )

    editor = adapter._wait_for_hydrated_composer_read_only(CONVERSATION_A)

    assert editor is contenteditable
    page.goto.assert_called_once_with(
        f"https://chatgpt.com{CONVERSATION_A}",
        wait_until="domcontentloaded",
        timeout=10_000,
    )
    assert page.wait_for_timeout.call_count == 2
    textarea.click.assert_not_called()
    textarea.focus.assert_not_called()
    contenteditable.click.assert_not_called()
    contenteditable.focus.assert_not_called()


def test_composer_text_spike_does_not_navigate_when_already_on_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    editor = MagicMock()
    editor.count.return_value = 1
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: editor)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _candidate: True)
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _candidate, **_kwargs: _focus_metadata("div"),
    )

    assert adapter._wait_for_hydrated_composer_read_only(CONVERSATION_A) is editor
    page.goto.assert_not_called()
    page.wait_for_timeout.assert_called_once_with(100)
    editor.click.assert_not_called()
    editor.focus.assert_not_called()


def test_composer_text_spike_hydration_timeout_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    missing = MagicMock()
    missing.count.return_value = 0
    times = iter((0.0, 31.0))
    monkeypatch.setattr("ebook_pipeline.browser.chatgpt.monotonic", lambda: next(times))
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: missing)

    with pytest.raises(ConflictError) as captured:
        adapter._wait_for_hydrated_composer_read_only(CONVERSATION_A)

    assert captured.value.code == "BROWSER_COMPOSER_TEXT_SPIKE_EDITOR_NOT_FOUND"
    page.goto.assert_not_called()
    missing.click.assert_not_called()
    missing.focus.assert_not_called()


def test_normal_composer_noncanonical_dom_reaches_composer_filled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    monkeypatch.setattr(adapter, "_start", lambda: page)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_focus_composer", lambda: composer)
    monkeypatch.setattr(
        adapter, "_ensure_empty_composer_before_insert", lambda: composer
    )
    monkeypatch.setattr(adapter, "_focusable_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_is_focusable_editor", lambda _editor: True)
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter,
        "_composer_metadata",
        lambda _composer: _focused_composer_metadata(),
    )
    monkeypatch.setattr(
        adapter, "_composer_text", lambda _composer, **_kwargs: "texto não canônico"
    )
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    checkpoints: list[str] = []
    monkeypatch.setattr(adapter, "_send_checkpoint", checkpoints.append)
    send_button = MagicMock(side_effect=ConflictError("TEST_STOP", "stop before Send"))
    monkeypatch.setattr(adapter, "_send_button", send_button)

    with pytest.raises(ConflictError) as captured:
        adapter.send_message("pedido integral")

    assert captured.value.code == "TEST_STOP"
    assert checkpoints == ["composer_found", "composer_filled"]


def test_composer_audit_logs_metadata_without_editorial_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter(tmp_path)
    editor = MagicMock()
    editor.evaluate.return_value = {
        "tag_name": "div",
        "contenteditable": "true",
        "role": "textbox",
        "id": "prompt-textarea",
        "data_testid": "composer",
        "is_content_editable": True,
        "focused": True,
        "editable_descendant_count": 0,
        "text": "CONTEUDO_EDITORIAL_PROIBIDO",
    }

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        adapter._audit_composer(editor, phase="focused")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "tag_name=div" in messages
    assert "contenteditable=true" in messages
    assert "focused=True" in messages
    assert "CONTEUDO_EDITORIAL_PROIBIDO" not in messages


def test_headed_chrome_composer_probe_clears_draft_and_never_reaches_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path, channel="chrome", headless=False)
    page = MagicMock()
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "ensure_ready", lambda: SessionState.READY)
    create_conversation = MagicMock()
    monkeypatch.setattr(adapter, "create_conversation", create_conversation)
    state = {"text": "restored draft"}
    pressed: list[str] = []
    composer = MagicMock()
    composer.count.return_value = 1
    composer.fill.side_effect = lambda value, **_kwargs: state.update(text=value)

    def press(key: str, *, timeout: int) -> None:
        del timeout
        assert key != "Enter"
        pressed.append(key)
        if key == "Backspace":
            state["text"] = ""

    composer.press.side_effect = press
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "_composer_text", lambda _composer: state["text"])
    monkeypatch.setattr(
        adapter, "_composer_pasted_text_attachment_count", lambda _composer: 0
    )
    monkeypatch.setattr(
        adapter,
        "_audit_composer",
        lambda *_args, **_kwargs: _focused_composer_metadata(),
    )
    send_message = MagicMock(side_effect=AssertionError("send_message must not be called"))
    send_button = MagicMock(side_effect=AssertionError("send button must not be resolved"))
    monkeypatch.setattr(adapter, "send_message", send_message)
    monkeypatch.setattr(adapter, "_send_button", send_button)
    payloads = {
        "short": "curto",
        "multiline_unicode": "ação\nΩ\n漢字",
        "large": "grande\n" * 512,
        "writing_context_request": "request artifact integral",
    }

    result = adapter.composer_probe(payloads)

    assert list(result) == list(payloads)
    for name, payload in payloads.items():
        case = result[name]
        expected = transport_fingerprint(payload)
        assert set(asdict(case)) == {
            "expected_length",
            "observed_length",
            "expected_fingerprint",
            "observed_fingerprint",
            "stable_read_1",
            "stable_read_2",
            "editor_metadata",
        }
        assert case.expected_length == case.observed_length == len(payload)
        assert case.expected_fingerprint == case.observed_fingerprint == expected
        assert case.stable_read_1 == case.stable_read_2 == expected
    assert state["text"] == ""
    create_conversation.assert_called_once_with()
    send_message.assert_not_called()
    send_button.assert_not_called()
    assert [call.args[0] for call in composer.fill.call_args_list] == list(payloads.values())
    assert pressed == ["Control+A", "Backspace"] * 5


def test_composer_probe_rejects_headless_or_non_chrome_modes(tmp_path: Path) -> None:
    for adapter in (
        _adapter(tmp_path / "headless", channel="chrome", headless=True),
        _adapter(tmp_path / "chromium", channel="chromium", headless=False),
    ):
        with pytest.raises(ConfigurationError) as captured:
            adapter.composer_probe({"short": "payload"})
        assert captured.value.code == "BROWSER_COMPOSER_SPIKE_REQUIRES_HEADED_CHROME"


def _turn_page(request: str, response: str) -> tuple[MagicMock, MagicMock]:
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    turns = MagicMock()
    turns.count.return_value = 2
    user_turn = MagicMock()
    assistant_turn = MagicMock()
    user_turn.get_attribute.return_value = "user"
    user_turn.inner_text.return_value = request
    no_attachment = MagicMock()
    no_attachment.count.return_value = 0
    user_turn.locator.return_value = no_attachment
    assistant_turn.get_attribute.return_value = "assistant"
    assistant_turn.inner_text.return_value = response
    rendered_candidates = MagicMock()
    rendered_candidates.count.return_value = 1
    rendered_content = MagicMock()
    rendered_content.is_visible.return_value = True
    rendered_content.inner_text.return_value = response
    rendered_candidates.first = rendered_content
    rendered_candidates.nth.return_value = rendered_content
    assistant_turn.locator.return_value = rendered_candidates
    turns.nth.side_effect = lambda index: (user_turn, assistant_turn)[index]
    page.locator.return_value = turns
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    return page, turns


def _structural_post_send_page(
    *,
    attachment_count: int,
    user_turn_count: int = 1,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    user_turn = MagicMock()
    attachment_groups = MagicMock()
    attachment_groups.count.return_value = attachment_count
    user_turn.locator.return_value = attachment_groups
    user_turns = MagicMock()
    user_turns.count.return_value = user_turn_count
    user_turns.nth.return_value = user_turn
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 1

    assistant_turn = MagicMock()
    assistant_turn.get_attribute.return_value = "assistant"
    assistant_turn.inner_text.return_value = "resposta integral"
    rendered_candidates = MagicMock()
    rendered_candidates.count.return_value = 1
    rendered_content = MagicMock()
    rendered_content.is_visible.return_value = True
    rendered_content.inner_text.return_value = "resposta integral"
    rendered_candidates.first = rendered_content
    rendered_candidates.nth.return_value = rendered_content
    generation_indicators = MagicMock()
    generation_indicators.count.return_value = 0
    post_response_controls = MagicMock()
    post_response_controls.count.return_value = 1
    post_response_controls.nth.return_value.is_visible.return_value = True
    assistant_turn.locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: rendered_candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: generation_indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: post_response_controls,
    }[selector]
    turns = MagicMock()
    turns.count.return_value = 2
    user_turn.get_attribute.return_value = "user"
    turns.nth.side_effect = lambda index: (user_turn, assistant_turn)[index]
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root

    def locate(selector: str) -> MagicMock:
        return {
            CONVERSATION_ROOT_SELECTOR: roots,
            USER_MESSAGE_SELECTOR: user_turns,
            TURN_SELECTOR: turns,
        }[selector]

    page.locator.side_effect = locate
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    return page, user_turn, attachment_groups


def test_happy_path_big_paste_uses_two_stable_structural_reads_without_opening_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page, user_turn, attachment_groups = _structural_post_send_page(
        attachment_count=1
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        adapter,
        "_scan_user_turn_proof",
        MagicMock(side_effect=AssertionError("integral proof must not run")),
    )
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=0,
        big_paste_used=True,
    )

    first = adapter.inspect_sent_turn_structure(fingerprint)
    second = adapter.inspect_sent_turn_structure(fingerprint)
    third = adapter.inspect_sent_turn_structure(fingerprint)

    assert first.state is TurnState.NOT_SENT
    assert first.evidence is not None and first.evidence["stable_reads"] == 1
    assert second.state is TurnState.STREAMING
    assert third.state is TurnState.COMPLETE
    assert third.observed_user_turn_fingerprint == fingerprint
    assert third.evidence is not None
    assert third.evidence["new_user_turn_attachment_count"] == 1
    assert third.evidence["stable_reads"] == 2
    assert third.evidence["response_stable_reads"] == 2
    assert adapter.capture_structural_response(fingerprint) == "resposta integral"
    user_turn.inner_text.assert_not_called()
    attachment_groups.click.assert_not_called()


@pytest.mark.parametrize(
    ("attachment_count", "expected_state"),
    [(0, TurnState.NOT_SENT), (2, TurnState.AMBIGUOUS)],
)
def test_happy_path_big_paste_fails_closed_without_exactly_one_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attachment_count: int,
    expected_state: TurnState,
) -> None:
    adapter = _adapter(tmp_path)
    page, _user_turn, attachment_groups = _structural_post_send_page(
        attachment_count=attachment_count
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=0,
        big_paste_used=True,
    )

    assert adapter.inspect_sent_turn_structure(fingerprint).state is expected_state
    attachment_groups.click.assert_not_called()


def test_happy_path_structural_proof_requires_exactly_one_new_user_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page, _user_turn, _attachment_groups = _structural_post_send_page(
        attachment_count=0,
        user_turn_count=2,
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=0,
        big_paste_used=False,
    )

    inspection = adapter.inspect_sent_turn_structure(fingerprint)

    assert inspection.state is TurnState.AMBIGUOUS


def test_reused_conversation_structural_proof_selects_only_baseline_plus_one_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    messages = [MagicMock() for _index in range(8)]
    for index, message in enumerate(messages):
        message.get_attribute.return_value = "user" if index % 2 == 0 else "assistant"
    new_user_turn = messages[6]
    new_attachments = MagicMock()
    new_attachments.count.return_value = 0
    new_user_turn.locator.return_value = new_attachments
    assistant = messages[7]
    assistant.inner_text.return_value = "resposta integral"
    rendered = MagicMock()
    rendered.is_visible.return_value = True
    rendered.inner_text.return_value = "resposta integral"
    rendered_candidates = MagicMock()
    rendered_candidates.count.return_value = 1
    rendered_candidates.nth.return_value = rendered
    generation_indicators = MagicMock()
    generation_indicators.count.return_value = 0
    post_response_controls = MagicMock()
    post_response_controls.count.return_value = 1
    post_response_controls.nth.return_value.is_visible.return_value = True
    assistant.locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: rendered_candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: generation_indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: post_response_controls,
    }[selector]
    user_turns = MagicMock()
    user_turns.count.return_value = 4
    user_turns.nth.return_value = new_user_turn
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 4
    turns = MagicMock()
    turns.count.return_value = len(messages)
    turns.nth.side_effect = lambda index: messages[index]
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.side_effect = lambda selector: {
        CONVERSATION_ROOT_SELECTOR: roots,
        USER_MESSAGE_SELECTOR: user_turns,
        TURN_SELECTOR: turns,
    }[selector]
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=3,
        big_paste_used=False,
    )

    first = adapter.inspect_sent_turn_structure(fingerprint)
    second = adapter.inspect_sent_turn_structure(fingerprint)
    third = adapter.inspect_sent_turn_structure(fingerprint)

    assert first.state is TurnState.NOT_SENT
    assert second.state is TurnState.STREAMING
    assert third.state is TurnState.COMPLETE
    assert adapter._structural_send_proof.user_turn_index == 6
    user_turns.nth.assert_not_called()
    turns.nth.assert_any_call(6)
    new_user_turn.locator.assert_called_with(
        USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
    )


def test_post_send_reuses_canonical_hydration_and_ordinal_stabilization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    messages = [MagicMock() for _index in range(8)]
    for index, message in enumerate(messages):
        message.get_attribute.side_effect = lambda name, index=index: {
            "data-message-author-role": "user" if index % 2 == 0 else "assistant",
            "data-message-id": f"message-{index}",
            "id": None,
        }.get(name)
    attachments = MagicMock()
    attachments.count.return_value = 0
    messages[6].locator.return_value = attachments
    messages[7].inner_text.return_value = "resposta integral"
    rendered = MagicMock()
    rendered.is_visible.return_value = True
    rendered.inner_text.return_value = "resposta integral"
    rendered_candidates = MagicMock()
    rendered_candidates.count.return_value = 1
    rendered_candidates.nth.return_value = rendered
    generation_indicators = MagicMock()
    generation_indicators.count.return_value = 0
    post_response_controls = MagicMock()
    post_response_controls.count.return_value = 1
    post_response_controls.nth.return_value.is_visible.return_value = True
    messages[7].locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: rendered_candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: generation_indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: post_response_controls,
    }[selector]
    user_turns = MagicMock()
    user_turns.count.side_effect = [0, 3, 4, 4, 4]
    assistant_turns = MagicMock()
    assistant_turns.count.side_effect = [0, 3, 4, 4, 4]
    turns = MagicMock()
    turns.count.side_effect = [0, 6, *([8] * 20)]
    turns.nth.side_effect = lambda index: messages[index]
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.return_value = roots
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    monkeypatch.setattr(adapter, "_start", lambda: page)
    canonical = MagicMock(wraps=adapter._evaluate_unit_turn_structure)
    monkeypatch.setattr(adapter, "_evaluate_unit_turn_structure", canonical)
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=3,
        big_paste_used=False,
    )

    inspections = [adapter.inspect_sent_turn_structure(fingerprint) for _index in range(5)]

    assert [inspection.state for inspection in inspections] == [
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]
    assert [
        inspection.evidence["failure_reason"]
        if inspection.evidence is not None
        else None
        for inspection in inspections[:3]
    ] == [
        "turn_cardinality_hydrating",
        "turn_cardinality_hydrating",
        "structure_not_stable",
    ]
    assert canonical.call_count == 5
    assert adapter._structural_send_proof.user_turn_index == 6


def test_post_send_uses_canonical_extra_cardinality_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    user_turns = MagicMock()
    user_turns.count.return_value = 4
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 5
    turns = MagicMock()
    turns.count.return_value = 9
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.return_value = roots
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=3,
        big_paste_used=False,
    )

    inspection = adapter.inspect_sent_turn_structure(fingerprint)

    assert inspection.state is TurnState.AMBIGUOUS
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "unexplained_extra_assistant_turn"


def test_unit_reconciliation_uses_stable_ordinal_turn_and_rendered_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    messages = [MagicMock() for _index in range(4)]
    for index, message in enumerate(messages):
        message.get_attribute.return_value = "user" if index % 2 == 0 else "assistant"
    messages[3].inner_text.return_value = "resposta unitária integral"
    rendered_candidates = MagicMock()
    rendered_candidates.count.return_value = 1
    rendered_content = MagicMock()
    rendered_content.is_visible.return_value = True
    rendered_content.inner_text.return_value = "resposta unitária integral"
    rendered_candidates.first = rendered_content
    rendered_candidates.nth.return_value = rendered_content
    generation_indicators = MagicMock()
    generation_indicators.count.return_value = 0
    post_response_controls = MagicMock()
    post_response_controls.count.return_value = 3
    post_response_controls.nth.return_value.is_visible.return_value = True
    messages[3].locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: rendered_candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: generation_indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: post_response_controls,
    }[selector]
    user_turns = MagicMock()
    user_turns.count.return_value = 2
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 2
    turns = MagicMock()
    turns.count.return_value = 4
    turns.nth.side_effect = lambda index: messages[index]
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.side_effect = lambda selector: {
        CONVERSATION_ROOT_SELECTOR: roots,
        TURN_SELECTOR: turns,
    }[selector]
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    monkeypatch.setattr(adapter, "_start", lambda: page)

    first = adapter.inspect_reconciliation_unit_turn(1)
    second = adapter.inspect_reconciliation_unit_turn(1)
    third = adapter.inspect_reconciliation_unit_turn(1)

    assert first.state is TurnState.NOT_SENT
    assert second.state is TurnState.STREAMING
    assert third.state is TurnState.COMPLETE
    assert third.evidence is not None
    assert third.evidence["stable_read_count"] == 2
    assert adapter.capture_reconciled_unit_response(1) == "resposta unitária integral"
    messages[2].inner_text.assert_not_called()
    messages[3].inner_text.assert_not_called()


def test_unit_reconciliation_rejects_unexplained_extra_user_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    user_turns = MagicMock()
    user_turns.count.return_value = 3
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 2
    turns = MagicMock()
    turns.count.return_value = 5
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.return_value = roots
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(1)

    assert inspection.state is TurnState.AMBIGUOUS
    user_turns.nth.assert_not_called()


def test_unit_reconciliation_treats_missing_ordinal_assistant_as_hydrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    messages = [MagicMock() for _index in range(3)]
    for index, message in enumerate(messages):
        message.get_attribute.return_value = "user" if index % 2 == 0 else "assistant"
    user_turns = MagicMock()
    user_turns.count.return_value = 2
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 1
    turns = MagicMock()
    turns.count.return_value = 3
    turns.nth.side_effect = lambda index: messages[index]
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.return_value = roots
    monkeypatch.setattr(adapter, "_start", lambda: page)

    first = adapter.inspect_reconciliation_unit_turn(1)
    inspection = adapter.inspect_reconciliation_unit_turn(1)

    assert first.state is TurnState.NOT_SENT
    assert inspection.state is TurnState.NOT_SENT
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "turn_cardinality_hydrating"


def _unit_response_probe_page(*, wrapper_reads: tuple[str, ...]) -> MagicMock:
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    root = MagicMock()
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    user_turns = MagicMock()
    user_turns.count.return_value = 4
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = 4
    messages = [MagicMock() for _index in range(8)]
    for index, message in enumerate(messages):
        message.get_attribute.side_effect = lambda name, index=index: {
            "data-message-author-role": "user" if index % 2 == 0 else "assistant",
            "data-message-id": f"message-{index}",
            "id": None,
        }.get(name)
    assistant = messages[7]
    assistant.get_attribute.side_effect = lambda name: {
        "data-message-author-role": "assistant",
        "data-message-id": "assistant-message-id",
        "id": None,
    }.get(name)
    assistant.inner_text.side_effect = wrapper_reads
    semantic = MagicMock()
    semantic.is_visible.return_value = True
    semantic.inner_text.return_value = "resposta semântica integral"
    semantic_candidates = MagicMock()
    semantic_candidates.count.return_value = 1
    semantic_candidates.nth.return_value = semantic
    indicators = MagicMock()
    indicators.count.return_value = 0
    controls = MagicMock()
    controls.count.return_value = 3
    controls.nth.return_value.is_visible.return_value = True
    assistant.locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: semantic_candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: controls,
    }[selector]
    turns = MagicMock()
    turns.count.return_value = 8
    turns.nth.side_effect = lambda index: messages[index]
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    page.locator.side_effect = lambda selector: {
        CONVERSATION_ROOT_SELECTOR: roots,
        USER_MESSAGE_SELECTOR: user_turns,
        TURN_SELECTOR: turns,
    }[selector]
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    return page


def test_unit_response_probe_reports_stable_semantic_completion_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(
        wrapper_reads=("wrapper estável",) * 4,
    )
    monkeypatch.setattr(adapter, "open_conversation", lambda _path: None)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    evidence = adapter.unit_response_completion_probe(CONVERSATION_A, 3)

    assert evidence["user_count"] == 4
    assert evidence["assistant_count"] == 4
    assert evidence["selected_user_index"] == 6
    assert evidence["selected_assistant_index"] == 7
    assert evidence["semantic_container_count"] == 1
    assert evidence["semantic_visible_count"] == 1
    assert evidence["semantic_length"] == len("resposta semântica integral")
    assert evidence["semantic_sha_same"] is True
    assert evidence["no_generation_indicator"] is True
    assert evidence["no_stop"] is True
    assert evidence["post_response_control_count"] == 3
    assert evidence["stable_read_count"] == 2
    assert evidence["failure_reason"] == "complete"
    assert "resposta semântica integral" not in repr(evidence)


def test_unit_response_probe_uses_shared_semantic_helper_without_wrapper_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(
        wrapper_reads=("wrapper A", "wrapper B", "wrapper C", "wrapper D"),
    )
    monkeypatch.setattr(adapter, "open_conversation", lambda _path: None)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    evidence = adapter.unit_response_completion_probe(CONVERSATION_A, 3)

    assert evidence["failure_reason"] == "complete"
    assert evidence["semantic_length"] == len("resposta semântica integral")
    assistant = page.locator(TURN_SELECTOR).nth(7)
    assistant.inner_text.assert_not_called()


def test_shared_unit_completion_treats_missing_ordinal_as_hydrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 4)
    root = page.locator(CONVERSATION_ROOT_SELECTOR).first
    root.locator(USER_MESSAGE_SELECTOR).count.return_value = 3
    root.locator(ASSISTANT_MESSAGE_SELECTOR).count.return_value = 3
    root.locator(TURN_SELECTOR).count.return_value = 6
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(3)

    assert inspection.state is TurnState.NOT_SENT
    assert inspection.evidence is not None
    assert inspection.evidence["user_count"] == 3
    assert inspection.evidence["assistant_count"] == 3
    assert inspection.evidence["selected_user_index"] is None
    assert inspection.evidence["selected_assistant_index"] is None
    assert inspection.evidence["failure_reason"] == "turn_cardinality_hydrating"


def test_unit_reconciliation_waits_for_zero_three_then_four_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 5)
    root = page.locator(CONVERSATION_ROOT_SELECTOR).first
    user_turns = root.locator(USER_MESSAGE_SELECTOR)
    assistant_turns = root.locator(ASSISTANT_MESSAGE_SELECTOR)
    user_turns.count.side_effect = [0, 3, 4, 4, 4]
    assistant_turns.count.side_effect = [0, 3, 4, 4, 4]
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspections = [adapter.inspect_reconciliation_unit_turn(3) for _index in range(5)]

    assert [item.state for item in inspections] == [
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]
    assert [
        item.evidence["failure_reason"] if item.evidence is not None else None
        for item in inspections[:3]
    ] == [
        "turn_cardinality_hydrating",
        "turn_cardinality_hydrating",
        "structure_not_stable",
    ]
    assert inspections[-1].evidence is not None
    assert inspections[-1].evidence["turn_message_id_count"] == 8
    assert inspections[-1].evidence["structure_stable_count"] == 2
    assert user_turns.count.call_count == 5
    assert assistant_turns.count.call_count == 5


def test_unit_reconciliation_waits_for_three_then_four_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 4)
    root = page.locator(CONVERSATION_ROOT_SELECTOR).first
    root.locator(USER_MESSAGE_SELECTOR).count.side_effect = [3, 4, 4, 4]
    root.locator(ASSISTANT_MESSAGE_SELECTOR).count.side_effect = [3, 4, 4, 4]
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspections = [adapter.inspect_reconciliation_unit_turn(3) for _index in range(4)]

    assert [item.state for item in inspections] == [
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]


def test_unit_reconciliation_accepts_four_stable_pairs_without_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 3)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspections = [adapter.inspect_reconciliation_unit_turn(3) for _index in range(3)]

    assert [item.state for item in inspections] == [
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]
    assert inspections[-1].evidence is not None
    assert inspections[-1].evidence["structure_stable_count"] == 2


def test_unit_response_probe_times_out_while_three_pairs_remain_hydrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",))
    root = page.locator(CONVERSATION_ROOT_SELECTOR).first
    root.locator(USER_MESSAGE_SELECTOR).count.return_value = 3
    root.locator(ASSISTANT_MESSAGE_SELECTOR).count.return_value = 3
    root.locator(TURN_SELECTOR).count.return_value = 6
    monkeypatch.setattr(adapter, "open_conversation", lambda _path: None)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(chatgpt_module, "UNIT_RESPONSE_SPIKE_HYDRATION_TIMEOUT_MS", 0)

    evidence = adapter.unit_response_completion_probe(CONVERSATION_A, 3)

    assert evidence["probe_timed_out"] is True
    assert evidence["user_count"] == 3
    assert evidence["assistant_count"] == 3
    assert evidence["failure_reason"] == "turn_cardinality_hydrating"
    assert evidence["selected_user_index"] is None
    assert evidence["selected_assistant_index"] is None


def test_unit_reconciliation_rejects_extra_assistant_cardinality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",))
    root = page.locator(CONVERSATION_ROOT_SELECTOR).first
    root.locator(ASSISTANT_MESSAGE_SELECTOR).count.return_value = 5
    root.locator(TURN_SELECTOR).count.return_value = 9
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(3)

    assert inspection.state is TurnState.AMBIGUOUS
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "unexplained_extra_assistant_turn"


def test_unit_reconciliation_reacquires_replaced_root_during_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    pages = [
        _unit_response_probe_page(wrapper_reads=("não usado",))
        for _index in range(6)
    ]
    first_root = pages[0].locator(CONVERSATION_ROOT_SELECTOR).first
    first_root.locator(USER_MESSAGE_SELECTOR).count.return_value = 3
    first_root.locator(ASSISTANT_MESSAGE_SELECTOR).count.return_value = 3
    first_root.locator(TURN_SELECTOR).count.return_value = 6
    start = MagicMock(side_effect=pages)
    monkeypatch.setattr(adapter, "_start", start)

    inspections = [
        adapter.inspect_reconciliation_unit_turn(3, CONVERSATION_A)
        for _index in range(4)
    ]

    assert [item.state for item in inspections] == [
        TurnState.NOT_SENT,
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]
    for page in pages:
        page.locator.assert_any_call(CONVERSATION_ROOT_SELECTOR)


def _conversation_structure_snapshot(
    *,
    user_count: int = 3,
    assistant_count: int = 3,
    lazy_indicator_count: int = 0,
    branch_controls: list[dict[str, object]] | None = None,
    branch_index_indicators: list[dict[str, object]] | None = None,
    branch_state_attributes: list[dict[str, object]] | None = None,
    url_branch_state_present: bool = False,
) -> dict[str, object]:
    return {
        "user_count": user_count,
        "assistant_count": assistant_count,
        "lazy_indicator_count": lazy_indicator_count,
        "branch_controls": branch_controls or [],
        "branch_index_indicators": branch_index_indicators or [],
        "branch_state_attributes": branch_state_attributes or [],
        "url_branch_state_present": url_branch_state_present,
    }


def test_conversation_structure_classifies_scroll_materialization_as_a() -> None:
    before = _conversation_structure_snapshot()
    after_scroll = _conversation_structure_snapshot(user_count=4, assistant_count=4)
    after_reload = _conversation_structure_snapshot(user_count=4, assistant_count=4)

    result = ChatGPTWebAdapter._classify_conversation_structure(
        before, after_scroll, after_reload, False
    )

    assert result == ("A", "scroll_materialized_additional_turns")


def test_conversation_structure_probe_uses_canonical_reload_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    turn_ids = [
        {"turn_index": index, "role": "user" if index % 2 == 0 else "assistant"}
        for index in range(6)
    ]
    snapshot: dict[str, object] = {
        **_conversation_structure_snapshot(),
        "turn_count": 6,
        "turn_id_count": 6,
        "turn_structural_ids": turn_ids,
    }
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "open_conversation", lambda _path: None)
    monkeypatch.setattr(
        adapter,
        "_wait_for_stable_conversation_structure",
        MagicMock(side_effect=(snapshot, snapshot, snapshot)),
    )
    monkeypatch.setattr(
        adapter,
        "_scroll_conversation_to_end",
        lambda: {"scrollable_count": 1},
    )
    reload_conversation = MagicMock()
    monkeypatch.setattr(adapter, "reload_conversation", reload_conversation)

    result = adapter.conversation_structure_probe(CONVERSATION_A)

    reload_conversation.assert_called_once_with(CONVERSATION_A)
    page.reload.assert_not_called()
    assert result["same_turn_ids_after_reload"] is True


def test_conversation_structure_classifies_visible_branch_navigation_as_b() -> None:
    before = _conversation_structure_snapshot(
        branch_controls=[{"visible": True, "disabled": False}]
    )

    result = ChatGPTWebAdapter._classify_conversation_structure(
        before, before, before, True
    )

    assert result == ("B", "branch_or_alternative_controls_present")


def test_conversation_structure_ignores_parent_ids_as_branch_evidence() -> None:
    snapshot = _conversation_structure_snapshot(
        branch_state_attributes=[
            {"name": "data-parent-message-id", "value": "parent-id"},
            {"name": "data-conversation-id", "value": "conversation-id"},
        ]
    )

    result = ChatGPTWebAdapter._classify_conversation_structure(
        snapshot, snapshot, snapshot, True
    )

    assert result == ("C", "same_turn_ids_persist_without_lazy_or_branch_evidence")


def test_conversation_structure_classifies_stable_same_ids_as_c() -> None:
    snapshot = _conversation_structure_snapshot()

    result = ChatGPTWebAdapter._classify_conversation_structure(
        snapshot, snapshot, snapshot, True
    )

    assert result == ("C", "same_turn_ids_persist_without_lazy_or_branch_evidence")


def test_conversation_structure_classifies_stable_fourth_pair_as_a() -> None:
    snapshot = _conversation_structure_snapshot(user_count=4, assistant_count=4)

    result = ChatGPTWebAdapter._classify_conversation_structure(
        snapshot, snapshot, snapshot, True
    )

    assert result == ("A", "fourth_pair_is_persisted_and_stable_after_reload")


def test_conversation_structure_does_not_classify_empty_shell_as_c() -> None:
    snapshot = _conversation_structure_snapshot(user_count=0, assistant_count=0)

    result = ChatGPTWebAdapter._classify_conversation_structure(
        snapshot, snapshot, snapshot, True
    )

    assert result == (
        "INCONCLUSIVE",
        "structural_evidence_does_not_distinguish_a_b_c",
    )


def test_unit_reconciliation_waits_while_positive_generation_signal_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 4)
    assistant = page.locator(TURN_SELECTOR).nth(7)
    indicators = assistant.locator(ASSISTANT_GENERATION_INDICATOR_SELECTOR)
    indicators.count.return_value = 1
    indicators.nth.return_value.is_visible.return_value = True
    monkeypatch.setattr(adapter, "_start", lambda: page)

    states = [adapter.inspect_reconciliation_unit_turn(3).state for _index in range(3)]

    assert states == [TurnState.NOT_SENT, TurnState.STREAMING, TurnState.STREAMING]
    proof = adapter._unit_reconciliation_proofs[(CONVERSATION_A, 3)]
    assert proof.completion_state is not None
    assert proof.completion_state.stable_read_count == 0


def test_unit_reconciliation_requires_stable_post_response_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _unit_response_probe_page(wrapper_reads=("não usado",) * 4)
    assistant = page.locator(TURN_SELECTOR).nth(7)
    controls = assistant.locator(ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR)
    controls.count.return_value = 0
    monkeypatch.setattr(adapter, "_start", lambda: page)

    states = [adapter.inspect_reconciliation_unit_turn(3).state for _index in range(3)]

    assert states == [TurnState.NOT_SENT, TurnState.STREAMING, TurnState.STREAMING]
    proof = adapter._unit_reconciliation_proofs[(CONVERSATION_A, 3)]
    assert proof.completion_state is not None
    assert proof.completion_state.stable_read_count == 0


def test_happy_path_structural_proof_requires_in_process_pre_send_baseline(
    tmp_path: Path
) -> None:
    adapter = _adapter(tmp_path)

    with pytest.raises(ConflictError) as captured:
        adapter.inspect_sent_turn_structure(transport_fingerprint("request"))

    assert captured.value.code == "BROWSER_SEND_REQUIRES_RECONCILE"


def _local_turn_anchor() -> dict[str, object]:
    return {
        "version": 1,
        "conversation_path": CONVERSATION_A,
        "pre_send_user_turn_count": 3,
        "pre_send_assistant_turn_count": 3,
        "tail": [
            {"role": "user", "id": "anchor-user"},
            {"role": "assistant", "id": "anchor-assistant"},
        ],
    }


def _local_successor_page(
    turn_specs: list[tuple[str, str]],
) -> MagicMock:
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    messages = [MagicMock() for _spec in turn_specs]
    for message, (role, identity) in zip(messages, turn_specs, strict=True):
        message.get_attribute.side_effect = lambda name, role=role, identity=identity: {
            "data-message-author-role": role,
            "data-message-id": identity,
            "id": None,
        }.get(name)
    new_user = next(
        message
        for message, (_role, identity) in zip(messages, turn_specs, strict=True)
        if identity == "new-user"
    ) if any(identity == "new-user" for _role, identity in turn_specs) else None
    if new_user is not None:
        attachments = MagicMock()
        attachments.count.return_value = 0
        new_user.locator.return_value = attachments
    new_assistant = next(
        message
        for message, (_role, identity) in zip(messages, turn_specs, strict=True)
        if identity == "new-assistant"
    ) if any(identity == "new-assistant" for _role, identity in turn_specs) else None
    if new_assistant is not None:
        new_assistant.inner_text.return_value = "resposta integral"
        semantic = MagicMock()
        semantic.is_visible.return_value = True
        semantic.inner_text.return_value = "resposta integral"
        semantic_candidates = MagicMock()
        semantic_candidates.count.return_value = 1
        semantic_candidates.nth.return_value = semantic
        indicators = MagicMock()
        indicators.count.return_value = 0
        controls = MagicMock()
        controls.count.return_value = 3
        controls.nth.return_value.is_visible.return_value = True
        new_assistant.locator.side_effect = lambda selector: {
            ASSISTANT_RENDERED_CONTENT_SELECTOR: semantic_candidates,
            ASSISTANT_GENERATION_INDICATOR_SELECTOR: indicators,
            ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: controls,
        }[selector]
    turns = MagicMock()
    turns.count.return_value = len(messages)
    turns.nth.side_effect = lambda index: messages[index]
    user_turns = MagicMock()
    user_turns.count.return_value = sum(role == "user" for role, _identity in turn_specs)
    assistant_turns = MagicMock()
    assistant_turns.count.return_value = sum(
        role == "assistant" for role, _identity in turn_specs
    )
    root = MagicMock()
    root.locator.side_effect = lambda selector: {
        USER_MESSAGE_SELECTOR: user_turns,
        ASSISTANT_MESSAGE_SELECTOR: assistant_turns,
        TURN_SELECTOR: turns,
    }[selector]
    roots = MagicMock()
    roots.count.return_value = 1
    roots.first = root
    page.locator.side_effect = lambda selector: {
        CONVERSATION_ROOT_SELECTOR: roots,
        TURN_SELECTOR: turns,
    }[selector]
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    return page


@pytest.mark.parametrize(
    "turn_specs",
    [
        [
            ("user", "old-user-0"),
            ("assistant", "old-assistant-0"),
            ("user", "old-user-1"),
            ("assistant", "old-assistant-1"),
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ],
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ],
    ],
    ids=("full_history", "old_turns_unmounted"),
)
def test_local_successor_binding_ignores_global_history_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn_specs: list[tuple[str, str]],
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(turn_specs)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    anchor = _local_turn_anchor()

    inspections = [
        adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
        for _index in range(3)
    ]

    assert [inspection.state for inspection in inspections] == [
        TurnState.NOT_SENT,
        TurnState.STREAMING,
        TurnState.COMPLETE,
    ]
    assert inspections[-1].evidence is not None
    assert inspections[-1].evidence["proof_kind"] == (
        "persisted_unit_local_successor_v1"
    )
    assert adapter.capture_reconciled_unit_response(anchor) == "resposta integral"


def test_local_successor_binding_survives_decreasing_global_cardinality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    full = _local_successor_page(
        [
            ("user", "old-user-0"),
            ("assistant", "old-assistant-0"),
            ("user", "old-user-1"),
            ("assistant", "old-assistant-1"),
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    partial = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    calls = 0

    def start() -> MagicMock:
        nonlocal calls
        calls += 1
        return full if calls == 1 else partial

    monkeypatch.setattr(adapter, "_start", start)
    anchor = _local_turn_anchor()

    inspections = [
        adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
        for _index in range(3)
    ]

    assert inspections[-1].state is TurnState.COMPLETE
    assert inspections[-1].evidence is not None
    assert inspections[-1].evidence["user_count"] == 2
    assert inspections[-1].evidence["expected_user_count"] == 4


def test_latched_assistant_survives_temporary_dom_unmount_between_stable_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    full = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    unmounted = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
        ]
    )
    remounted_assistant_only = _local_successor_page(
        [("assistant", "new-assistant")]
    )
    current = {"page": full}
    monkeypatch.setattr(adapter, "_start", lambda: current["page"])
    anchor = _local_turn_anchor()

    assert adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A).state is (
        TurnState.NOT_SENT
    )
    first_complete = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    current["page"] = unmounted
    absent = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    current["page"] = remounted_assistant_only
    stable = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)

    assert first_complete.state is TurnState.STREAMING
    assert first_complete.evidence is not None
    assert first_complete.evidence["selected_assistant_id"] == "new-assistant"
    assert first_complete.evidence["stable_read_count"] == 1
    assert absent.state is TurnState.STREAMING
    assert absent.evidence is not None
    assert absent.evidence["failure_reason"] == (
        "latched_assistant_temporarily_unavailable"
    )
    assert absent.evidence["stable_read_count"] == 1
    assert stable.state is TurnState.COMPLETE
    assert stable.evidence is not None
    assert stable.evidence["selected_assistant_id"] == "new-assistant"
    assert stable.evidence["stable_read_count"] == 2
    assert adapter.capture_reconciled_unit_response(anchor) == "resposta integral"


def test_latched_assistant_sha_change_restarts_content_stability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    full = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    unmounted = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
        ]
    )
    changed = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    changed_turns = changed.locator(CONVERSATION_ROOT_SELECTOR).first.locator(
        TURN_SELECTOR
    )
    changed_semantic = changed_turns.nth(3).locator(
        ASSISTANT_RENDERED_CONTENT_SELECTOR
    ).nth(0)
    changed_semantic.inner_text.return_value = "resposta integral alterada"
    current = {"page": full}
    monkeypatch.setattr(adapter, "_start", lambda: current["page"])
    anchor = _local_turn_anchor()

    adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    first_complete = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    current["page"] = unmounted
    adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    current["page"] = changed
    changed_first = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    changed_second = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)

    assert first_complete.evidence is not None
    assert first_complete.evidence["stable_read_count"] == 1
    assert changed_first.state is TurnState.STREAMING
    assert changed_first.evidence is not None
    assert changed_first.evidence["failure_reason"] == "completion_not_stable"
    assert changed_first.evidence["stable_read_count"] == 1
    assert changed_first.evidence["semantic_sha_same"] is False
    assert changed_second.state is TurnState.COMPLETE
    assert changed_second.evidence is not None
    assert changed_second.evidence["stable_read_count"] == 2


def test_latched_assistant_conflicting_real_successor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    assistant_a = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    assistant_b = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "assistant-B"),
        ]
    )
    current = {"page": assistant_a}
    monkeypatch.setattr(adapter, "_start", lambda: current["page"])
    anchor = _local_turn_anchor()

    adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    latched = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    current["page"] = assistant_b
    transient = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
    conflict = adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)

    assert latched.evidence is not None
    assert latched.evidence["selected_assistant_id"] == "new-assistant"
    assert transient.state is TurnState.STREAMING
    assert conflict.state is TurnState.AMBIGUOUS
    assert conflict.evidence is not None
    assert conflict.evidence["failure_reason"] == "turn_successor_identity_conflict"


def test_local_successor_binding_fails_closed_for_multiple_successors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
            ("user", "unexpected-user"),
        ]
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(
        _local_turn_anchor(), CONVERSATION_A
    )

    assert inspection.state is TurnState.AMBIGUOUS
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "turn_successor_ambiguous"


def test_local_successor_binding_fails_closed_when_successor_id_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    turns = page.locator(CONVERSATION_ROOT_SELECTOR).first.locator(TURN_SELECTOR)
    turns.nth(2).get_attribute.side_effect = lambda name: {
        "data-message-author-role": "user",
        "data-message-id": None,
        "id": None,
    }.get(name)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(
        _local_turn_anchor(), CONVERSATION_A
    )

    assert inspection.state is TurnState.AMBIGUOUS
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "turn_successor_identity_missing"


def test_local_successor_binding_fails_closed_when_anchor_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "different-user"),
            ("assistant", "different-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_reconciliation_unit_turn(
        _local_turn_anchor(), CONVERSATION_A
    )

    assert inspection.state is TurnState.NOT_SENT
    assert inspection.evidence is not None
    assert inspection.evidence["failure_reason"] == "turn_anchor_missing"


def test_happy_path_and_reconcile_share_local_successor_binder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    canonical = MagicMock(wraps=adapter._evaluate_local_turn_successor)
    monkeypatch.setattr(adapter, "_evaluate_local_turn_successor", canonical)
    anchor = _local_turn_anchor()
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=3,
        big_paste_used=False,
        turn_anchor=anchor,
    )

    adapter.inspect_sent_turn_structure(fingerprint)
    adapter.inspect_sent_turn_structure(fingerprint)
    happy = adapter.inspect_sent_turn_structure(fingerprint)
    reconciled = [
        adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
        for _index in range(3)
    ]

    assert happy.state is TurnState.COMPLETE
    assert reconciled[-1].state is TurnState.COMPLETE
    assert canonical.call_count == 6


def _attachment_turn_page(
    preview: str,
    attachment_content: str,
    response: str,
    *,
    opener_error: Exception | None = None,
    group_label: str = "Texto colado (request).txt",
    opener_label: str | None = None,
    attachment_count: int = 1,
    opener_count: int = 1,
    modal_title: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    page, turns = _turn_page(preview, response)
    user_turn = turns.nth(0)
    attachments = MagicMock()
    attachments.count.return_value = attachment_count
    group = MagicMock()
    group.get_attribute.return_value = group_label
    attachments.first = group
    openers = MagicMock()
    openers.count.return_value = opener_count
    opener = MagicMock()
    opener.get_attribute.return_value = opener_label or group_label
    openers.first = opener
    group.locator.return_value = openers
    user_turn.locator.return_value = attachments
    if opener_error is not None:
        opener.click.side_effect = opener_error
    close_controls = MagicMock()
    close_controls.count.return_value = 1
    close_control = MagicMock()
    close_control.is_visible.return_value = True
    close_controls.nth.return_value = close_control
    container_locator = MagicMock()
    container_locator.count.return_value = 1
    container = MagicMock()
    container.is_visible.return_value = True
    container_locator.first = container
    close_control.locator.return_value = container_locator
    titles = MagicMock()
    titles.count.return_value = 1
    titles.first.inner_text.return_value = modal_title or group_label
    contents = MagicMock()
    contents.count.return_value = 1
    contents.first.evaluate.return_value = [attachment_content, attachment_content]
    progressbars = MagicMock()
    progressbars.count.return_value = 0

    def container_locator_for(selector: str) -> MagicMock:
        return {
            USER_TURN_PASTED_TEXT_MODAL_TITLE_SELECTOR: titles,
            USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR: contents,
            USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR: progressbars,
        }[selector]

    container.locator.side_effect = container_locator_for
    stop = MagicMock()
    stop.count.return_value = 0

    def by_role(role: str, *, name: object | None = None) -> MagicMock:
        if role == "button" and name == PASTED_TEXT_MODAL_CLOSE_PATTERN:
            return close_controls
        return stop

    page.get_by_role.side_effect = by_role
    close_control.locator.assert_not_called()
    return page, turns, opener


def test_turn_inspection_and_rendered_capture_prove_exact_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page, turns = _turn_page("pedido exato", "resposta integral")
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint("pedido exato")
    inspection = adapter.inspect_turn(fingerprint)
    assert inspection.state is TurnState.COMPLETE
    assert inspection.response_text == "resposta integral"
    assert inspection.observed_user_turn_fingerprint == fingerprint
    assert adapter.capture_response(fingerprint) == "resposta integral"
    turns.nth(0).locator.assert_not_called()

    assert adapter.inspect_turn(transport_fingerprint("outro")).state is TurnState.NOT_SENT
    turns.count.return_value = 1
    assert adapter.inspect_turn(fingerprint).state is TurnState.STREAMING


def test_rendered_capture_uses_only_semantic_model_content_container(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    assistant = MagicMock()
    assistant.get_attribute.return_value = "assistant"
    assistant.inner_text.return_value = "Editar\nEm ambientes empresariais..."
    candidates = MagicMock()
    candidates.count.return_value = 1
    content = MagicMock()
    content.is_visible.return_value = True
    content.inner_text.return_value = (
        "Em ambientes empresariais...\n\n## Título\n\n- item 1\n- item 2"
    )
    candidates.first = content
    candidates.nth.return_value = content
    assistant.locator.return_value = candidates

    captured = adapter._capture_assistant_response(assistant)

    assert captured == "Em ambientes empresariais...\n\n## Título\n\n- item 1\n- item 2"
    assistant.locator.assert_called_once_with(ASSISTANT_RENDERED_CONTENT_SELECTOR)
    assistant.inner_text.assert_not_called()
    content.inner_text.assert_called_once_with(timeout=min(1000, adapter.timeout_ms))


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_rendered_capture_fails_closed_for_nonunique_content_container(
    tmp_path: Path, candidate_count: int
) -> None:
    adapter = _adapter(tmp_path)
    assistant = MagicMock()
    assistant.get_attribute.return_value = "assistant"
    candidates = MagicMock()
    candidates.count.return_value = candidate_count
    candidates.nth.return_value.is_visible.return_value = True
    assistant.locator.return_value = candidates

    with pytest.raises(ConflictError) as captured:
        adapter._capture_assistant_response(assistant)

    assert captured.value.code == "BROWSER_RESPONSE_CONTENT_CONTAINER_AMBIGUOUS"
    assert captured.value.context.evidence is not None
    assert captured.value.context.evidence["assistant_message_id"] == "assistant"
    assert (
        captured.value.context.evidence["content_container_candidate_count"]
        == candidate_count
    )
    assert (
        captured.value.context.evidence[
            "visible_content_container_candidate_count"
        ]
        == candidate_count
    )
    candidates.first.inner_text.assert_not_called()


def test_rendered_capture_ignores_hidden_rehydration_duplicate(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assistant = MagicMock()
    assistant.get_attribute.return_value = "assistant"
    candidates = MagicMock()
    candidates.count.return_value = 2
    hidden = MagicMock()
    hidden.is_visible.return_value = False
    visible = MagicMock()
    visible.is_visible.return_value = True
    visible.inner_text.return_value = "Em ambientes empresariais..."
    candidates.nth.side_effect = (hidden, visible)
    assistant.locator.return_value = candidates

    assert (
        adapter._capture_assistant_response(assistant)
        == "Em ambientes empresariais..."
    )


def test_scoped_capture_treats_nested_markdown_as_one_canonical_root(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    assistant = MagicMock()
    assistant.get_attribute.side_effect = lambda name: {
        "data-message-author-role": "assistant",
        "data-message-id": "bound-assistant",
        "id": None,
    }.get(name)
    semantic_root = MagicMock()
    semantic_root.is_visible.return_value = True
    semantic_root.inner_text.return_value = "resposta integral com regiões aninhadas"
    semantic_root.evaluate.return_value = {
        "parent_candidate_count": 0,
        "child_candidate_count": 2,
    }
    candidates = MagicMock()
    candidates.count.return_value = 1
    candidates.nth.return_value = semantic_root
    assistant.locator.return_value = candidates

    captured = adapter._capture_assistant_response(
        assistant, expected_assistant_id="bound-assistant"
    )

    assert captured == "resposta integral com regiões aninhadas"


def test_scoped_content_diagnostic_reports_metadata_without_response_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    stop = MagicMock()
    stop.count.return_value = 0
    page.get_by_role.return_value = stop
    monkeypatch.setattr(adapter, "_start", lambda: page)
    assistant = MagicMock()
    assistant.get_attribute.side_effect = lambda name: {
        "data-message-author-role": "assistant",
        "data-message-id": "bound-assistant",
        "id": None,
    }.get(name)
    semantic = MagicMock()
    semantic.is_visible.return_value = True
    semantic.inner_text.return_value = "conteúdo que não pode aparecer no diagnóstico"
    semantic.evaluate.return_value = {
        "parent_candidate_count": 0,
        "child_candidate_count": 1,
    }
    candidates = MagicMock()
    candidates.count.return_value = 1
    candidates.nth.return_value = semantic
    indicators = MagicMock()
    indicators.count.return_value = 0
    controls = MagicMock()
    controls.count.return_value = 2
    controls.nth.return_value.is_visible.return_value = True
    assistant.locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: controls,
    }[selector]

    snapshot = adapter._scoped_assistant_content_snapshot(assistant)
    evidence = snapshot.evidence()

    assert evidence["assistant_message_id"] == "bound-assistant"
    assert evidence["semantic_container_count"] == 1
    assert evidence["semantic_visible_count"] == 1
    assert evidence["semantic_length"] == len(
        "conteúdo que não pode aparecer no diagnóstico"
    )
    assert evidence["semantic_candidate_hierarchy"] == [
        {
            "candidate_index": 0,
            "visible": True,
            "parent_candidate_count": 0,
            "child_candidate_count": 1,
            "text_length": len("conteúdo que não pode aparecer no diagnóstico"),
        }
    ]
    assert "conteúdo que não pode aparecer" not in repr(evidence)


def test_happy_and_reconcile_capture_only_bound_assistant_semantic_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "old-user"),
            ("assistant", "old-assistant"),
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    scoped_snapshot = MagicMock(wraps=adapter._scoped_assistant_content_snapshot)
    monkeypatch.setattr(adapter, "_scoped_assistant_content_snapshot", scoped_snapshot)
    turns = page.locator(CONVERSATION_ROOT_SELECTOR).first.locator(TURN_SELECTOR)
    old_assistant = turns.nth(1)
    anchor_assistant = turns.nth(3)
    bound_assistant = turns.nth(5)
    fingerprint = transport_fingerprint("request provenance only")
    anchor = _local_turn_anchor()
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=2,
        big_paste_used=False,
        turn_anchor=anchor,
    )

    happy_inspections = [
        adapter.inspect_sent_turn_structure(fingerprint) for _index in range(3)
    ]
    happy_capture = adapter.capture_structural_response(fingerprint)
    reconcile_inspections = [
        adapter.inspect_reconciliation_unit_turn(anchor, CONVERSATION_A)
        for _index in range(3)
    ]
    reconcile_capture = adapter.capture_reconciled_unit_response(anchor)

    assert happy_inspections[-1].state is TurnState.COMPLETE
    assert reconcile_inspections[-1].state is TurnState.COMPLETE
    assert happy_capture == "resposta integral"
    assert reconcile_capture == "resposta integral"
    old_assistant.locator.assert_not_called()
    anchor_assistant.locator.assert_not_called()
    assert scoped_snapshot.call_count == 6
    assert all(call.args[0] is bound_assistant for call in scoped_snapshot.call_args_list)


def test_bound_assistant_with_independent_semantic_roots_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = _local_successor_page(
        [
            ("user", "anchor-user"),
            ("assistant", "anchor-assistant"),
            ("user", "new-user"),
            ("assistant", "new-assistant"),
        ]
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    turns = page.locator(CONVERSATION_ROOT_SELECTOR).first.locator(TURN_SELECTOR)
    bound_assistant = turns.nth(3)
    candidates = MagicMock()
    candidates.count.return_value = 2
    independent = [MagicMock(), MagicMock()]
    for index, candidate in enumerate(independent):
        candidate.is_visible.return_value = True
        candidate.inner_text.return_value = f"independent-{index}"
        candidate.evaluate.return_value = {
            "parent_candidate_count": 0,
            "child_candidate_count": 0,
        }
    candidates.nth.side_effect = lambda index: independent[index]
    indicators = MagicMock()
    indicators.count.return_value = 0
    controls = MagicMock()
    controls.count.return_value = 2
    controls.nth.return_value.is_visible.return_value = True
    bound_assistant.locator.side_effect = lambda selector: {
        ASSISTANT_RENDERED_CONTENT_SELECTOR: candidates,
        ASSISTANT_GENERATION_INDICATOR_SELECTOR: indicators,
        ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR: controls,
    }[selector]
    fingerprint = transport_fingerprint("request provenance only")
    adapter._structural_send_proof = StructuralSendProofState(
        expected_fingerprint=fingerprint,
        pre_send_user_turn_count=1,
        big_paste_used=False,
        turn_anchor=_local_turn_anchor(),
    )

    first = adapter.inspect_sent_turn_structure(fingerprint)
    second = adapter.inspect_sent_turn_structure(fingerprint)

    assert first.state is TurnState.NOT_SENT
    assert second.state is TurnState.AMBIGUOUS
    assert second.evidence is not None
    assert second.evidence["failure_reason"] == "semantic_container_ambiguous"


def test_assistant_response_spike_reacquires_until_ordinal_cardinality_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    first_root = MagicMock()
    first_root.count.return_value = 1
    first_candidates = MagicMock()
    first_candidates.count.return_value = 2
    first_root.first.locator.return_value = first_candidates
    second_root = MagicMock()
    second_root.count.return_value = 1
    second_candidates = MagicMock()
    second_candidates.count.return_value = 2
    expected_turn = MagicMock()
    second_candidates.nth.return_value = expected_turn
    second_root.first.locator.return_value = second_candidates
    page.locator.side_effect = (first_root, second_root)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    turn, candidate_count, stable_reads = adapter._wait_for_hydrated_assistant_turn(
        CONVERSATION_A, 1
    )

    assert turn is expected_turn
    assert candidate_count == 2
    assert stable_reads == 2
    assert page.wait_for_timeout.call_count == 1


def test_persisted_unit_recapture_reads_stable_ordinal_with_later_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    user_turns = MagicMock()
    user_turns.count.return_value = 3
    turns = MagicMock()
    turns.count.return_value = 6
    assistant = MagicMock()
    assistant.get_attribute.return_value = "assistant"
    turns.nth.return_value = assistant
    page.locator.side_effect = lambda selector: (
        user_turns if selector == USER_MESSAGE_SELECTOR else turns
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(adapter, "_turn_index_for_user_ordinal", lambda ordinal: 2)
    capture = MagicMock(return_value="Em ambientes empresariais...")
    monkeypatch.setattr(adapter, "_capture_rendered_assistant_response", capture)

    result = adapter.recapture_persisted_unit_response(1)

    assert result == "Em ambientes empresariais..."
    assert capture.call_count == 2
    assert all(call.args == (assistant,) for call in capture.call_args_list)
    assert turns.nth.call_args_list == [call(3), call(3)]
    assert page.wait_for_timeout.call_count == 1
    page.get_by_role.assert_not_called()


def test_persisted_unit_recapture_fails_closed_when_ordinal_never_hydrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    adapter.timeout_ms = 0
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    user_turns = MagicMock()
    user_turns.count.return_value = 1
    turns = MagicMock()
    turns.count.return_value = 2
    page.locator.side_effect = lambda selector: (
        user_turns if selector == USER_MESSAGE_SELECTOR else turns
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    with pytest.raises(ConflictError) as captured:
        adapter.recapture_persisted_unit_response(1)

    assert captured.value.code == "BROWSER_RECAPTURE_TURN_HYDRATION_TIMEOUT"
    assert captured.value.context.evidence == {
        "poll_count": 1,
        "conversation_path": CONVERSATION_A,
        "user_turn_ordinal": 1,
        "user_turn_candidate_count": 1,
        "turn_candidate_count": 2,
        "assistant_turn_index": -1,
        "stable_reads": 0,
    }
    turns.nth.assert_not_called()
    page.wait_for_timeout.assert_not_called()


def test_big_paste_user_turn_opens_attachment_and_proves_integral_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral com big paste\n" * 3000
    page, _turns, opener = _attachment_turn_page(
        "Longo demais para mostrar no campo de texto",
        payload,
        "resposta integral",
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint(payload)

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        inspection = adapter.inspect_turn(fingerprint)

    assert inspection.state is TurnState.COMPLETE
    assert inspection.observed_user_turn_fingerprint == fingerprint
    assert inspection.evidence is not None
    assert inspection.evidence["pasted_text_attachment_found"] is True
    assert inspection.evidence["pasted_text_attachment_opened"] is True
    assert inspection.evidence["pasted_text_content_length"] == len(payload)
    assert inspection.evidence["observed_fingerprint"] == fingerprint
    assert adapter.capture_response(fingerprint) == "resposta integral"
    assert opener.click.call_count == 1
    user_turn = _turns.nth(0)
    user_turn.locator.assert_called_with(
        USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
    )
    group = user_turn.locator.return_value.first
    group.locator.assert_called_with(USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR)
    close_control = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    ).nth(0)
    close_control.locator.assert_called_with(
        USER_TURN_PASTED_TEXT_MODAL_CONTAINER_XPATH
    )
    message = next(
        record.message
        for record in caplog.records
        if "browser_user_turn_proof" in record.message
    )
    assert payload not in message
    assert f"pasted_text_content_length={len(payload)}" in message


def test_big_paste_preview_never_proves_divergent_attachment_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    preview = "preview que coincide"
    page, _turns, opener = _attachment_turn_page(
        preview,
        "conteúdo integral divergente",
        "resposta que não pode ser capturada",
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_turn(transport_fingerprint("request integral esperado"))

    assert inspection.state is TurnState.NOT_SENT
    assert inspection.observed_user_turn_fingerprint is None
    assert inspection.evidence is not None
    assert inspection.evidence["pasted_text_attachment_found"] is True
    assert inspection.evidence["pasted_text_attachment_opened"] is False
    opener.click.assert_called_once()


@pytest.mark.parametrize(
    ("attachment_count", "opener_count", "opener_label"),
    [
        (0, 0, None),
        (2, 1, None),
        (1, 0, None),
        (1, 2, None),
        (1, 1, "outro-arquivo.txt"),
    ],
)
def test_big_paste_user_turn_attachment_structure_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attachment_count: int,
    opener_count: int,
    opener_label: str | None,
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta que não pode ser capturada",
        attachment_count=attachment_count,
        opener_count=opener_count,
        opener_label=opener_label,
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_turn(transport_fingerprint(payload))

    assert inspection.state in {TurnState.NOT_SENT, TurnState.AMBIGUOUS}
    assert inspection.response_text is None
    opener.click.assert_not_called()


def test_big_paste_attachment_that_does_not_open_remains_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta que não pode ser capturada",
        opener_error=RuntimeError("attachment still loading"),
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_turn(transport_fingerprint(payload))

    assert inspection.state is TurnState.NOT_SENT
    assert inspection.evidence is not None
    assert inspection.evidence["pasted_text_attachment_found"] is True
    assert inspection.evidence["pasted_text_attachment_opened"] is False
    opener.click.assert_called_once()


def test_big_paste_attachment_click_failure_is_not_retried_by_later_proof_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral depois do carregamento"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta integral",
    )
    opener.click.side_effect = [RuntimeError("still loading"), None]
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint(payload)

    first = adapter.inspect_turn(fingerprint)
    second = adapter.inspect_turn(fingerprint)

    assert first.state is TurnState.NOT_SENT
    assert second.state is TurnState.NOT_SENT
    assert second.observed_user_turn_fingerprint is None
    assert opener.click.call_count == 1


def test_big_paste_modal_waits_for_progressbar_then_two_stable_content_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral após modal hidratado"
    page, _turns, _opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta integral",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_control = close_controls.nth(0)
    container = close_control.locator.return_value.first
    progressbars = container.locator(USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR)
    progressbars.count.side_effect = (1, 0, 0)
    progressbars.nth.return_value.is_visible.return_value = True
    contents = container.locator(USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR)
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_turn(transport_fingerprint(payload))

    assert inspection.state is TurnState.COMPLETE
    assert contents.first.evaluate.call_count == 2
    assert close_control.locator.call_count == 3
    assert page.wait_for_timeout.call_count >= 2


def test_big_paste_modal_title_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta que não pode ser capturada",
        modal_title="outro-arquivo.txt",
    )
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        "ebook_pipeline.browser.chatgpt.USER_TURN_ATTACHMENT_MODAL_TIMEOUT_MS", 0
    )

    inspection = adapter.inspect_turn(transport_fingerprint(payload))

    assert inspection.state is TurnState.NOT_SENT
    assert inspection.response_text is None
    assert inspection.evidence is not None
    assert inspection.evidence["pasted_text_attachment_opened"] is False
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)


@pytest.mark.parametrize("missing_modal_polls", [4, 20])
def test_big_paste_modal_wait_never_reissues_attachment_click(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_modal_polls: int,
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta integral",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_controls.count.side_effect = [0] * missing_modal_polls + [1, 1]
    monkeypatch.setattr(adapter, "_start", lambda: page)

    observed = adapter._read_user_turn_pasted_text_attachment(
        opener, transport_fingerprint(payload)
    )

    assert transport_fingerprint(observed) == transport_fingerprint(payload)
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    assert page.wait_for_timeout.call_count == missing_modal_polls + 1


def test_big_paste_modal_absence_times_out_after_one_click_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta que não pode ser capturada",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_controls.count.return_value = 0
    monkeypatch.setattr(adapter, "_start", lambda: page)
    monkeypatch.setattr(
        "ebook_pipeline.browser.chatgpt.USER_TURN_ATTACHMENT_MODAL_TIMEOUT_MS", 0
    )
    fingerprint = transport_fingerprint(payload)

    with pytest.raises(ConflictError) as first:
        adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)
    with pytest.raises(ConflictError) as second:
        adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)

    assert first.value.code == "BROWSER_USER_TURN_ATTACHMENT_MODAL_TIMEOUT"
    assert second.value.code == first.value.code
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)


def test_big_paste_modal_progressbar_polls_never_reissue_attachment_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta integral",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_control = close_controls.nth(0)
    container = close_control.locator.return_value.first
    progressbars = container.locator(USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR)
    progressbars.count.side_effect = [1] * 6 + [0, 0]
    progressbars.nth.return_value.is_visible.return_value = True
    monkeypatch.setattr(adapter, "_start", lambda: page)

    observed = adapter._read_user_turn_pasted_text_attachment(
        opener, transport_fingerprint(payload)
    )

    assert transport_fingerprint(observed) == transport_fingerprint(payload)
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    assert page.wait_for_timeout.call_count == 7


def test_big_paste_modal_disappearance_is_terminal_without_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta que não pode ser capturada",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_controls.count.side_effect = (1, 0)
    close_control = close_controls.nth(0)
    container = close_control.locator.return_value.first
    progressbars = container.locator(USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR)
    progressbars.count.return_value = 1
    progressbars.nth.return_value.is_visible.return_value = True
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint(payload)

    with pytest.raises(ConflictError) as first:
        adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)
    with pytest.raises(ConflictError) as second:
        adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)

    assert first.value.code == "BROWSER_USER_TURN_ATTACHMENT_MODAL_DISAPPEARED"
    assert second.value.code == first.value.code
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    close_control.click.assert_not_called()


def test_big_paste_modal_exact_fingerprint_closes_once_and_caches_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    payload = "request integral"
    page, _turns, opener = _attachment_turn_page(
        "preview",
        payload,
        "resposta integral",
    )
    close_controls = page.get_by_role(
        "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
    )
    close_control = close_controls.nth(0)
    monkeypatch.setattr(adapter, "_start", lambda: page)
    fingerprint = transport_fingerprint(payload)

    first = adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)
    second = adapter._read_user_turn_pasted_text_attachment(opener, fingerprint)

    assert first == second
    assert transport_fingerprint(first) == fingerprint
    opener.click.assert_called_once_with(timeout=adapter.timeout_ms)
    close_control.click.assert_called_once_with(timeout=adapter.timeout_ms)


def test_multiple_matching_user_turn_candidates_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    page = MagicMock()
    page.url = f"https://chatgpt.com{CONVERSATION_A}"
    turns = MagicMock()
    turns.count.return_value = 4
    messages = [MagicMock() for _ in range(4)]
    for index, message in enumerate(messages):
        message.get_attribute.return_value = "user" if index % 2 == 0 else "assistant"
        message.inner_text.return_value = (
            "pedido duplicado" if index % 2 == 0 else "resposta"
        )
        absent = MagicMock()
        absent.count.return_value = 0
        message.locator.return_value = absent
    turns.nth.side_effect = messages.__getitem__
    page.locator.return_value = turns
    monkeypatch.setattr(adapter, "_start", lambda: page)

    inspection = adapter.inspect_turn(transport_fingerprint("pedido duplicado"))

    assert inspection.state is TurnState.AMBIGUOUS
    assert inspection.evidence is not None
    assert inspection.evidence["user_turn_candidate_count"] == 2


def test_copy_control_is_resolved_from_nearest_assistant_parent(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assistant = MagicMock()
    absent = MagicMock()
    absent.count.return_value = 0
    assistant.get_by_role.return_value = absent
    parent = MagicMock()
    one_assistant = MagicMock()
    one_assistant.count.return_value = 1
    copy_button = MagicMock()
    copy_button.count.return_value = 1
    parent.get_by_role.return_value = copy_button

    def assistant_locator(selector: str) -> MagicMock:
        return parent if selector == "xpath=.." else absent

    def parent_locator(selector: str) -> MagicMock:
        if selector == ASSISTANT_MESSAGE_SELECTOR:
            return one_assistant
        return absent

    assistant.locator.side_effect = assistant_locator
    parent.locator.side_effect = parent_locator

    assert adapter._copy_button_for_assistant(assistant) is copy_button


def test_capture_spike_requires_short_and_long_samples_for_one_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter(tmp_path)
    events: list[str] = []
    page = MagicMock()
    composer = MagicMock()
    composer.count.return_value = 1
    composer.fill.side_effect = lambda _prompt: events.append("fill")
    monkeypatch.setattr(adapter, "ensure_ready", lambda: SessionState.READY)
    monkeypatch.setattr(adapter, "create_conversation", lambda: events.append("conversation"))
    monkeypatch.setattr(adapter, "_composer", lambda: composer)

    def send(_prompt: str, *, prefilled: bool = False) -> str:
        assert prefilled is True
        events.append("send")
        return "/c/spike"

    monkeypatch.setattr(adapter, "send_message", send)
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: False)
    response = {"value": "ack curto"}

    def inspect(_fingerprint: str) -> TurnInspection:
        return TurnInspection(
            TurnState.COMPLETE,
            "/c/spike",
            response["value"],
            {"assistant_started": True},
        )

    monkeypatch.setattr(adapter, "inspect_turn", inspect)
    assistant = MagicMock()
    assistant.count.return_value = 1
    assistant.inner_text.side_effect = lambda: response["value"]
    absent = MagicMock()
    absent.count.return_value = 0
    assistant.locator.return_value = absent
    turn = MagicMock()
    turn.locator.return_value = assistant
    copy = MagicMock()
    copy.count.return_value = 1
    assistant.get_by_role.return_value = copy
    def assistant_for_fingerprint(_fingerprint: str) -> MagicMock:
        events.append("capture")
        return turn

    monkeypatch.setattr(adapter, "_assistant_for_fingerprint", assistant_for_fingerprint)
    clipboard_reads = {"count": 0}

    def clipboard_value(_script: str) -> str:
        clipboard_reads["count"] += 1
        return (
            f"stale-{clipboard_reads['count']}"
            if clipboard_reads["count"] % 2
            else response["value"]
        )

    page.evaluate.side_effect = clipboard_value
    monkeypatch.setattr(adapter, "_start", lambda: page)

    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.chatgpt"):
        short = adapter.capture_spike("acknowledgement")
        assert short.equivalent and not adapter.capture_gate_ready()
        response["value"] = "x" * 4000
        long = adapter.capture_spike("long_unit")
    assert long.equivalent and adapter.capture_gate_ready()
    marker = json.loads(adapter._capture_gate_path().read_text(encoding="utf-8"))
    assert marker["selected_method_version"] == "rendered_text_v1"
    assert marker["samples"]["acknowledgement"]["comparison"]["classification"] == "exact"
    assert marker["samples"]["long_unit"]["comparison"]["classification"] == "exact"
    assert events.index("send") < events.index("capture")
    checkpoints = [getattr(record, "status", None) for record in caplog.records]
    assert checkpoints[-10:] == [
        "provider_ready",
        "conversation_ready",
        "composer_found",
        "composer_filled",
        "send_triggered",
        "user_turn_observed",
        "assistant_response_started",
        "assistant_response_completed",
        "rendered_capture",
        "copy_capture",
    ]
    with pytest.raises(IntegrityError):
        adapter.capture_spike("unknown")


def test_capture_spike_distinguishes_pre_send_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, "ensure_ready", lambda: SessionState.READY)
    monkeypatch.setattr(adapter, "create_conversation", lambda: None)
    composer = MagicMock()
    composer.count.return_value = 0
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    with pytest.raises(ConflictError) as missing:
        adapter.capture_spike("acknowledgement")
    assert missing.value.code == "BROWSER_SPIKE_COMPOSER_NOT_FOUND"

    composer.count.return_value = 1
    monkeypatch.setattr(
        adapter,
        "send_message",
        lambda _text, *, prefilled=False: (_ for _ in ()).throw(RuntimeError("send failed")),
    )
    with pytest.raises(ConflictError) as unsent:
        adapter.capture_spike("acknowledgement")
    assert unsent.value.code == "BROWSER_SPIKE_PROMPT_NOT_SENT"


def test_capture_spike_distinguishes_observation_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    adapter.timeout_ms = 0
    monkeypatch.setattr(
        adapter,
        "inspect_turn",
        lambda _fingerprint: TurnInspection(TurnState.NOT_SENT),
    )
    with pytest.raises(IntegrityError) as user_missing:
        adapter._wait_for_spike_user_turn("a" * 64)
    assert user_missing.value.code == "BROWSER_SPIKE_USER_TURN_NOT_OBSERVED"

    not_started = TurnInspection(
        TurnState.STREAMING, evidence={"assistant_started": False}
    )
    with pytest.raises(IntegrityError) as response_missing:
        adapter._wait_for_spike_response_start("a" * 64, not_started)
    assert response_missing.value.code == "BROWSER_SPIKE_RESPONSE_NOT_STARTED"

    started = TurnInspection(TurnState.STREAMING, evidence={"assistant_started": True})
    with pytest.raises(IntegrityError) as response_timeout:
        adapter._wait_for_spike_completion("a" * 64, started)
    assert response_timeout.value.code == "BROWSER_SPIKE_RESPONSE_TIMEOUT"


def test_capture_spike_distinguishes_missing_or_empty_assistant_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, "ensure_ready", lambda: SessionState.READY)
    monkeypatch.setattr(adapter, "create_conversation", lambda: None)
    composer = MagicMock()
    composer.count.return_value = 1
    monkeypatch.setattr(adapter, "_composer", lambda: composer)
    monkeypatch.setattr(adapter, "send_message", lambda _text, *, prefilled=False: "/c/spike")
    monkeypatch.setattr(adapter, "_composer_has_text", lambda: False)
    response = {"value": "complete"}
    monkeypatch.setattr(
        adapter,
        "inspect_turn",
        lambda _fingerprint: TurnInspection(
            TurnState.COMPLETE,
            "/c/spike",
            response["value"],
            {"assistant_started": True},
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_assistant_for_fingerprint",
        lambda _fingerprint: (_ for _ in ()).throw(
            ConflictError("BROWSER_TURN_AMBIGUOUS", "missing")
        ),
    )
    with pytest.raises(ConflictError) as missing:
        adapter.capture_spike("acknowledgement")
    assert missing.value.code == "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND"

    response["value"] = ""
    with pytest.raises(IntegrityError) as empty:
        adapter.capture_spike("acknowledgement")
    assert empty.value.code == "BROWSER_SPIKE_RESPONSE_EMPTY"


def test_bootstrap_candidate_scan_uses_exact_turn_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    fingerprint = "a" * 64
    opened: list[str] = []
    monkeypatch.setattr(
        adapter, "list_conversation_paths", lambda: (CONVERSATION_A, CONVERSATION_B)
    )
    monkeypatch.setattr(adapter, "_path", lambda: CONVERSATION_C)
    monkeypatch.setattr(adapter, "open_conversation", opened.append)

    def inspect(_fingerprint: str) -> TurnInspection:
        state = TurnState.COMPLETE if opened[-1] == CONVERSATION_A else TurnState.NOT_SENT
        return TurnInspection(
            state,
            opened[-1],
            "ok" if state is TurnState.COMPLETE else None,
            observed_user_turn_fingerprint=(fingerprint if state is TurnState.COMPLETE else None),
        )

    monkeypatch.setattr(adapter, "inspect_turn", inspect)
    candidates = adapter.find_bootstrap_candidates(fingerprint, "2026-01-01T00:00:00+00:00")
    assert [item.conversation_path for item in candidates] == [CONVERSATION_A]
    assert opened[-1] == CONVERSATION_C
