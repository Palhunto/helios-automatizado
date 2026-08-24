from __future__ import annotations

import ctypes
import importlib
import json
import logging
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import ModuleType
from typing import Any, Never, Protocol, TypedDict
from urllib.parse import urlparse
from uuid import uuid4

from ebook_pipeline.browser.capture_comparison import compare_capture_text
from ebook_pipeline.browser.fingerprints import transport_fingerprint, transport_normalize
from ebook_pipeline.browser.models import (
    BootstrapCandidate,
    CaptureSpikeResult,
    ComposerProbeCaseResult,
    SessionState,
    TurnInspection,
    TurnState,
)
from ebook_pipeline.browser.profile_lock import BrowserProfileLock
from ebook_pipeline.browser.selectors import (
    ASSISTANT_GENERATION_INDICATOR_SELECTOR,
    ASSISTANT_MESSAGE_SELECTOR,
    ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR,
    ASSISTANT_RENDERED_CONTENT_SELECTOR,
    CHALLENGE_PATTERN,
    COMPOSER_EDITOR_SELECTOR,
    COMPOSER_PASTED_TEXT_ATTACHMENT_REMOVE_SELECTOR,
    COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR,
    COMPOSER_TEST_ID,
    CONVERSATION_LINK_SELECTOR,
    CONVERSATION_ROOT_SELECTOR,
    COPY_PATTERN,
    LOGIN_PATTERN,
    OPERATIONAL_ERROR_PATTERN,
    RETRY_PATTERN,
    SEND_BUTTON_TEST_ID,
    SEND_PATTERN,
    STOP_PATTERN,
    TURN_SELECTOR,
    USER_MESSAGE_SELECTOR,
    USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR,
    USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_CONTAINER_XPATH,
    USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR,
    USER_TURN_PASTED_TEXT_MODAL_TITLE_SELECTOR,
)
from ebook_pipeline.browser.urls import (
    conversation_path_from_page_url,
    is_real_conversation_path,
)
from ebook_pipeline.core.errors import (
    ConfigurationError,
    ConflictError,
    HeliosError,
    IntegrityError,
)
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.ids import utc_now

LOGGER = logging.getLogger(__name__)
COMPOSER_FILL_MAX_CHARACTERS = 32_768
COMPOSER_PASTE_VERIFICATION_TIMEOUT_MS = 15_000
COMPOSER_PASTE_POLL_INTERVAL_MS = 100
COMPOSER_FOCUS_TIMEOUT_MS = 30_000
COMPOSER_FOCUS_POLL_INTERVAL_MS = 100
COMPOSER_FOCUS_ACTION_TIMEOUT_MS = 500
COMPOSER_INSERT_VERIFY_TIMEOUT_MS = 5_000
COMPOSER_INSERT_VERIFY_POLL_INTERVAL_MS = 100
COMPOSER_INSERT_VERIFY_READ_TIMEOUT_MS = 500
COMPOSER_TEXT_SPIKE_HYDRATION_TIMEOUT_MS = 30_000
COMPOSER_TEXT_SPIKE_HYDRATION_POLL_INTERVAL_MS = 100
COMPOSER_TEXT_DIAGNOSTIC_SCRIPT = (
    "(element) => {"
    "const lineBreak = String.fromCharCode(10);"
    "const blockTags = new Set(["
    '"ADDRESS","ARTICLE","ASIDE","BLOCKQUOTE","DIV","FOOTER",'
    '"H1","H2","H3","H4","H5","H6","HEADER","LI","MAIN",'
    '"NAV","P","PRE","SECTION"'
    "]);"
    "const reconstruct = (node) => {"
    'if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";'
    'if (node.nodeType !== Node.ELEMENT_NODE) return "";'
    'if (node.tagName === "BR") return lineBreak;'
    'const value = Array.from(node.childNodes).map(reconstruct).join("");'
    "return node !== element && blockTags.has(node.tagName)"
    "? value + lineBreak : value;"
    "};"
    "const isValueEditor = element instanceof HTMLTextAreaElement || "
    "element instanceof HTMLInputElement;"
    "return {"
    "textarea_value: isValueEditor ? element.value : null,"
    'text_content: element.textContent || "",'
    'inner_text: element.innerText || "",'
    "reconstructed: reconstruct(element)"
    "};"
    "}"
)
SEND_BUTTON_ENABLE_TIMEOUT_MS = 30_000
SEND_BUTTON_POLL_INTERVAL_MS = 100
PRE_SEND_USER_TURN_BASELINE_TIMEOUT_MS = 5_000
PRE_SEND_USER_TURN_BASELINE_POLL_INTERVAL_MS = 100
USER_TURN_SPIKE_HYDRATION_TIMEOUT_MS = 30_000
USER_TURN_SPIKE_HYDRATION_POLL_INTERVAL_MS = 250
USER_TURN_SPIKE_MODAL_TIMEOUT_MS = 30_000
USER_TURN_SPIKE_MODAL_POLL_INTERVAL_MS = 250
ASSISTANT_RESPONSE_SPIKE_HYDRATION_TIMEOUT_MS = 30_000
ASSISTANT_RESPONSE_SPIKE_HYDRATION_POLL_INTERVAL_MS = 250
UNIT_RESPONSE_SPIKE_HYDRATION_TIMEOUT_MS = 30_000
UNIT_RESPONSE_SPIKE_POLL_INTERVAL_MS = 250
CONVERSATION_STRUCTURE_SPIKE_TIMEOUT_MS = 30_000
CONVERSATION_STRUCTURE_SPIKE_POLL_INTERVAL_MS = 250
BRANCH_CONTROL_PATTERN = re.compile(
    r"previous\s+(?:response|message)|next\s+(?:response|message)|"
    r"(?:response|message)\s+alternative|branch|alternative|"
    r"resposta\s+anterior|pr[oó]xima\s+resposta|mensagem\s+anterior|"
    r"pr[oó]xima\s+mensagem|alternativ|ramifica",
    re.IGNORECASE,
)
PASTED_TEXT_MODAL_CLOSE_PATTERN = re.compile(r"^(?:fechar|close)$", re.IGNORECASE)
USER_TURN_ATTACHMENT_MODAL_TIMEOUT_MS = 30_000
USER_TURN_ATTACHMENT_MODAL_POLL_INTERVAL_MS = 250
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
USER_DATA_DIR_ARGUMENT = re.compile(
    r"(?:^|\s)--user-data-dir(?:\s*=\s*|\s+)(?:\"([^\"]*)\"|'([^']*)'|(\S+))",
    re.IGNORECASE,
)


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouse_data", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra_info", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("virtual_key", wintypes.WORD),
        ("scan", wintypes.WORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra_info", ctypes.c_size_t),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("message", wintypes.DWORD),
        ("parameter_low", wintypes.WORD),
        ("parameter_high", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [
        ("mouse", _MouseInput),
        ("keyboard", _KeyboardInput),
        ("hardware", _HardwareInput),
    ]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("payload", _InputUnion)]


class ComposerMetadata(TypedDict):
    tag_name: str
    contenteditable: str | None
    role: str | None
    id: str | None
    data_testid: str | None
    is_content_editable: bool
    focused: bool
    editable_descendant_count: int


class UserTurnProofScan(TypedDict):
    match_indices: list[int]
    candidate_count: int
    text_length: int
    attachment_found: bool
    attachment_opened: bool
    attachment_content_length: int
    observed_fingerprint: str | None
    pending: bool
    ambiguous: bool


@dataclass(slots=True)
class UserTurnAttachmentProofState:
    click_issued: bool = False
    modal_seen: bool = False
    modal_poll_count: int = 0
    close_issued: bool = False
    completed_text: str | None = None
    terminal_error_code: str | None = None
    terminal_error_message: str | None = None
    terminal_evidence: dict[str, object] | None = None


@dataclass(slots=True)
class StructuralSendProofState:
    expected_fingerprint: str
    pre_send_user_turn_count: int
    big_paste_used: bool
    turn_anchor: dict[str, object] | None = None
    structure_signature: tuple[object, ...] | None = None
    structure_stable_count: int = 0
    user_turn_index: int | None = None
    user_turn_id: str | None = None
    assistant_turn_id: str | None = None
    completion_state: UnitResponseCompletionState | None = None

    @property
    def stable_reads(self) -> int:
        return self.structure_stable_count


@dataclass(slots=True)
class UnitReconciliationProofState:
    stable_reads: int = 0
    user_turn_index: int | None = None
    user_turn_id: str | None = None
    assistant_turn_id: str | None = None
    completion_state: UnitResponseCompletionState | None = None


@dataclass(slots=True)
class UnitResponseCompletionState:
    structure_signature: tuple[object, ...] | None = None
    structure_stable_count: int = 0
    signature: tuple[object, ...] | None = None
    semantic_sha256: str | None = None
    stable_read_count: int = 0
    user_turn_id: str | None = None
    assistant_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopedAssistantContentObservation:
    assistant_message_id: str | None
    semantic_container_count: int
    semantic_visible_count: int
    semantic_length: int
    semantic_sha256: str | None
    no_generation_indicator: bool
    no_stop: bool
    post_response_control_count: int
    candidate_hierarchy: tuple[dict[str, object], ...]
    failure_reason: str
    response_text: str | None = None

    def stability_signature(self) -> tuple[object, ...]:
        return (
            self.assistant_message_id,
            self.semantic_container_count,
            self.semantic_visible_count,
            self.semantic_length,
            self.semantic_sha256,
            self.no_generation_indicator,
            self.no_stop,
            self.post_response_control_count,
        )

    def evidence(self) -> dict[str, object]:
        return {
            "assistant_message_id": self.assistant_message_id,
            "semantic_container_count": self.semantic_container_count,
            "semantic_visible_count": self.semantic_visible_count,
            "semantic_length": self.semantic_length,
            "semantic_sha256": self.semantic_sha256,
            "semantic_candidate_hierarchy": list(self.candidate_hierarchy),
            "no_generation_indicator": self.no_generation_indicator,
            "no_stop": self.no_stop,
            "post_response_control_count": self.post_response_control_count,
            "failure_reason": self.failure_reason,
        }


class UnitTurnStructureState(Protocol):
    structure_signature: tuple[object, ...] | None
    structure_stable_count: int


@dataclass(frozen=True, slots=True)
class UnitTurnStructureObservation:
    conversation_path: str | None
    ordinal: int
    user_count: int
    assistant_count: int
    expected_user_count: int
    turn_count: int
    turn_message_id_count: int
    selected_user_index: int | None
    selected_assistant_index: int | None
    selected_user_id: str | None
    selected_assistant_id: str | None
    stable_read_count: int
    failure_reason: str


UNIT_TURN_STRUCTURE_AMBIGUOUS_REASONS = frozenset(
    {
        "unexplained_extra_user_turn",
        "unexplained_extra_assistant_turn",
        "turn_cardinality_or_ordinal_ambiguous",
        "turn_anchor_ambiguous",
        "turn_anchor_identity_missing",
        "turn_successor_identity_missing",
        "turn_successor_ambiguous",
        "turn_successor_identity_conflict",
        "latched_assistant_identity_ambiguous",
    }
)


@dataclass(frozen=True, slots=True)
class UnitResponseCompletionObservation:
    conversation_path: str | None
    ordinal: int
    user_count: int
    assistant_count: int
    selected_user_index: int | None
    selected_assistant_index: int | None
    selected_user_id: str | None
    selected_assistant_id: str | None
    expected_user_count: int
    turn_count: int
    turn_message_id_count: int
    structure_stable_count: int
    semantic_container_count: int
    semantic_visible_count: int
    semantic_length: int
    semantic_sha256: str | None
    semantic_sha_same: bool
    no_generation_indicator: bool
    no_stop: bool
    post_response_control_count: int
    stable_read_count: int
    failure_reason: str
    response_text: str | None = None

    def evidence(self) -> dict[str, object]:
        return {
            "conversation_path": self.conversation_path,
            "ordinal": self.ordinal,
            "user_count": self.user_count,
            "assistant_count": self.assistant_count,
            "selected_user_index": self.selected_user_index,
            "selected_assistant_index": self.selected_assistant_index,
            "selected_user_id": self.selected_user_id,
            "selected_assistant_id": self.selected_assistant_id,
            "expected_user_count": self.expected_user_count,
            "turn_count": self.turn_count,
            "turn_message_id_count": self.turn_message_id_count,
            "structure_stable_count": self.structure_stable_count,
            "semantic_container_count": self.semantic_container_count,
            "semantic_visible_count": self.semantic_visible_count,
            "semantic_length": self.semantic_length,
            "semantic_sha_same": self.semantic_sha_same,
            "no_generation_indicator": self.no_generation_indicator,
            "no_stop": self.no_stop,
            "post_response_control_count": self.post_response_control_count,
            "stable_read_count": self.stable_read_count,
            "failure_reason": self.failure_reason,
        }


SPIKE_PROMPTS = {
    "acknowledgement": (
        "Teste técnico de captura Hélios {marker}. Responda em texto simples, sem Markdown, "
        "com uma única frase curta confirmando que recebeu esta mensagem."
    ),
    "long_unit": (
        "Teste técnico de captura Hélios {marker}. Produza entre 3.500 e 4.000 caracteres em "
        "texto simples contínuo, sem Markdown, listas ou títulos, explicando de forma neutra "
        "como persistência, idempotência e recuperação tornam um pipeline local confiável. "
        "Não mencione estas instruções e não inclua dados pessoais."
    ),
}


class ChatGPTWebAdapter:
    """All ChatGPT DOM knowledge lives here; domain services see only the port."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        browser_channel: str,
        base_url: str,
        headless: bool,
        timeout_seconds: int,
        capture_method_version: str,
    ) -> None:
        self.profile_dir = profile_dir
        if browser_channel not in {"chrome", "chromium"}:
            raise IntegrityError(
                "BROWSER_CHANNEL_UNSUPPORTED", f"Unsupported browser channel: {browser_channel}"
            )
        self.browser_channel = browser_channel
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self.capture_method_version = capture_method_version
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._profile_lock = BrowserProfileLock(profile_dir)
        self._user_turn_attachment_proofs: dict[
            tuple[str, str], UserTurnAttachmentProofState
        ] = {}
        self._structural_send_proof: StructuralSendProofState | None = None
        self._pending_pre_send_turn_anchor: dict[str, object] | None = None
        self._unit_reconciliation_proofs: dict[
            tuple[str, object], UnitReconciliationProofState
        ] = {}

    def _module(self) -> ModuleType:
        try:
            return importlib.import_module("playwright.sync_api")
        except ImportError as exc:
            raise IntegrityError(
                "PLAYWRIGHT_NOT_INSTALLED",
                "Install the M3 browser dependency before browser setup",
            ) from exc

    def _start(self) -> Any:
        if self._page is not None:
            return self._page
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_lock.acquire()
        try:
            module = self._module()
            self._playwright = module.sync_playwright().start()
            options: dict[str, object] = {"headless": self.headless}
            if self.browser_channel == "chrome":
                options["channel"] = "chrome"
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                **options,
            )
        except Exception as exc:
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
            self._profile_lock.release()
            if self._is_profile_in_use_error(exc):
                raise ConflictError(
                    "BROWSER_PROFILE_IN_USE",
                    "Close Chrome or Playwright using the dedicated profile before retrying",
                ) from exc
            raise
        self._context.set_default_timeout(self.timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def ensure_ready(self) -> SessionState:
        page = self._start()
        if not page.url.startswith(self.base_url):
            page.goto(self.base_url, wait_until="domcontentloaded")
        body = page.locator("body").inner_text(timeout=self.timeout_ms)
        if CHALLENGE_PATTERN.search(body):
            return SessionState.CHALLENGE
        if page.get_by_role("button", name=LOGIN_PATTERN).count() or page.get_by_role(
            "link", name=LOGIN_PATTERN
        ).count():
            return SessionState.LOGIN_REQUIRED
        if not self._composer().count():
            return SessionState.LOGIN_REQUIRED
        return SessionState.READY

    def create_conversation(self) -> None:
        self._user_turn_attachment_proofs.clear()
        self._structural_send_proof = None
        self._pending_pre_send_turn_anchor = None
        self._unit_reconciliation_proofs.clear()
        self._start().goto(self.base_url, wait_until="domcontentloaded")

    def open_conversation(self, conversation_path: str) -> None:
        if not is_real_conversation_path(conversation_path):
            raise IntegrityError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Conversation path is not a canonical /c/<uuid> path",
            )
        self._user_turn_attachment_proofs.clear()
        self._structural_send_proof = None
        self._pending_pre_send_turn_anchor = None
        self._unit_reconciliation_proofs.clear()
        self._start().goto(f"{self.base_url}{conversation_path}", wait_until="domcontentloaded")

    def reload_conversation(self, conversation_path: str) -> None:
        """Reload the canonical conversation in the current Page without navigation."""
        if not is_real_conversation_path(conversation_path):
            raise IntegrityError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Conversation path is not a canonical /c/<uuid> path",
            )
        page = self._start()
        if conversation_path_from_page_url(str(page.url)) != conversation_path:
            raise ConflictError(
                "BROWSER_CONVERSATION_RELOAD_WRONG_PATH",
                "Conversation reload requires the expected canonical page to be current",
            )
        self._user_turn_attachment_proofs.clear()
        self._structural_send_proof = None
        self._pending_pre_send_turn_anchor = None
        self._unit_reconciliation_proofs.clear()
        page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
        if conversation_path_from_page_url(str(page.url)) != conversation_path:
            raise ConflictError(
                "BROWSER_CONVERSATION_RELOAD_WRONG_PATH",
                "Conversation reload left the expected canonical page",
            )

    def user_turn_structural_probe(self, conversation_path: str) -> dict[str, object]:
        """Inspect one pasted-text modal structurally without reading its content."""
        self.open_conversation(conversation_path)
        user_turn = self._wait_for_hydrated_user_turn(conversation_path)
        attachments = user_turn.locator(
            USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
        )
        if attachments.count() != 1:
            raise IntegrityError(
                "BROWSER_USER_TURN_SPIKE_ATTACHMENT_CARDINALITY",
                "User-turn spike requires exactly one pasted-text attachment group",
            )
        group = attachments.first
        openers = group.locator(USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR)
        if openers.count() != 1:
            raise IntegrityError(
                "BROWSER_USER_TURN_SPIKE_ATTACHMENT_OPENER_CARDINALITY",
                "Pasted-text attachment group requires exactly one opener",
            )
        opener = openers.first
        group_label = group.get_attribute("aria-label")
        opener_label = opener.get_attribute("aria-label")
        if (
            not isinstance(group_label, str)
            or not group_label
            or opener_label != group_label
        ):
            raise IntegrityError(
                "BROWSER_USER_TURN_SPIKE_ATTACHMENT_LABEL_MISMATCH",
                "Pasted-text attachment group and opener labels do not match",
            )
        opener.click(timeout=self.timeout_ms)
        close_control = self._wait_for_modal_audit_close_control()
        candidates = close_control.evaluate(
            """button => {
                const visible = element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0;
                };
                const scrollableCount = element => [...element.querySelectorAll("*")]
                    .filter(descendant => {
                        const style = window.getComputedStyle(descendant);
                        return (style.overflowY === "auto" ||
                            style.overflowY === "scroll") &&
                            descendant.scrollHeight > descendant.clientHeight;
                    }).length;
                const ancestors = [];
                let current = button.parentElement;
                while (current && current !== document.body &&
                    current !== document.documentElement &&
                    current.tagName.toLowerCase() !== "main") {
                    ancestors.push(current);
                    current = current.parentElement;
                }
                const semantic = ancestors.find(element =>
                    element.getAttribute("role") === "dialog" ||
                    element.getAttribute("aria-modal") === "true" ||
                    element.getAttribute("data-slot") === "dialog-content");
                const scrollable = ancestors.find(element => scrollableCount(element) > 0);
                const openState = ancestors.find(element =>
                    element.getAttribute("data-state") === "open");
                const fixed = ancestors.find(element =>
                    window.getComputedStyle(element).position === "fixed");
                const roots = [...new Set(
                    [semantic, scrollable, openState, fixed].filter(Boolean)
                )];
                return roots.map(root => {
                    const nodes = [root, ...root.querySelectorAll("*")];
                    const elements = nodes.filter(visible).map(element => {
                    const isButton = element.tagName.toLowerCase() === "button" ||
                        element.getAttribute("role") === "button";
                    return {
                        tag_name: element.tagName.toLowerCase(),
                        role: element.getAttribute("role"),
                        "aria-label": element.getAttribute("aria-label"),
                        "data-testid": element.getAttribute("data-testid"),
                        text_length: (element.innerText || element.textContent || "").length,
                        child_count: element.children.length,
                        button_count: element.querySelectorAll("button,[role='button']").length +
                            (isButton ? 1 : 0),
                        scrollable_descendant_count: scrollableCount(element),
                    };
                    });
                    return {elements};
                });
            }"""
        )
        if not isinstance(candidates, list) or not candidates:
            raise IntegrityError(
                "BROWSER_USER_TURN_SPIKE_STRUCTURE_INVALID",
                "Modal close control exposed no bounded structural candidate",
            )
        return {
            "conversation_path": conversation_path,
            "user_turn_count": 1,
            "attachment_aria_label": opener_label,
            "modal_candidate_count": len(candidates),
            "modal_candidates": candidates,
        }

    def assistant_response_text_probe(
        self,
        conversation_path: str,
        assistant_turn_ordinal: int,
        expected_prefix: str,
    ) -> dict[str, object]:
        """Capture one existing rendered response without interacting with the page."""
        if assistant_turn_ordinal < 0 or not expected_prefix:
            raise IntegrityError(
                "BROWSER_ASSISTANT_RESPONSE_SPIKE_INPUT_INVALID",
                "Assistant response spike requires a non-negative ordinal and prefix",
            )
        self.open_conversation(conversation_path)
        turn, candidate_count, stable_reads = self._wait_for_hydrated_assistant_turn(
            conversation_path, assistant_turn_ordinal
        )
        text = self._capture_rendered_assistant_response(turn)
        evidence = {
            "conversation_path": conversation_path,
            "assistant_turn_ordinal": assistant_turn_ordinal,
            "assistant_turn_candidate_count": candidate_count,
            "stable_reads": stable_reads,
            "rendered_text_length": len(text),
            "rendered_text_sha256": sha256_bytes(text.encode("utf-8", errors="strict")),
            "starts_with_expected_prefix": text.startswith(expected_prefix),
        }
        if not evidence["starts_with_expected_prefix"]:
            raise IntegrityError(
                "BROWSER_ASSISTANT_RESPONSE_SPIKE_PREFIX_MISMATCH",
                "Rendered assistant content does not start with the expected prefix",
                evidence=evidence,
            )
        return evidence

    def _evaluate_unit_turn_structure(
        self,
        conversation_path: str,
        user_turn_ordinal: int,
        state: UnitTurnStructureState,
        *,
        signature_extension: Callable[[Any], tuple[object, ...]] | None = None,
    ) -> UnitTurnStructureObservation:
        """Reacquire and stabilize one exact ordinal turn structure."""
        page = self._start()
        observed_path = conversation_path_from_page_url(str(page.url))
        expected_count = user_turn_ordinal + 1
        expected_turn_count = expected_count * 2
        user_count = 0
        assistant_count = 0
        turn_count = 0
        turn_message_id_count = 0

        def result(
            reason: str,
            *,
            selected_user_index: int | None = None,
            selected_assistant_index: int | None = None,
            selected_user_id: str | None = None,
            selected_assistant_id: str | None = None,
        ) -> UnitTurnStructureObservation:
            return UnitTurnStructureObservation(
                conversation_path=observed_path,
                ordinal=user_turn_ordinal,
                user_count=user_count,
                assistant_count=assistant_count,
                expected_user_count=expected_count,
                turn_count=turn_count,
                turn_message_id_count=turn_message_id_count,
                selected_user_index=selected_user_index,
                selected_assistant_index=selected_assistant_index,
                selected_user_id=selected_user_id,
                selected_assistant_id=selected_assistant_id,
                stable_read_count=state.structure_stable_count,
                failure_reason=reason,
            )

        def pending(reason: str) -> UnitTurnStructureObservation:
            state.structure_signature = None
            state.structure_stable_count = 0
            return result(reason)

        if observed_path != conversation_path:
            return pending("conversation_path_not_loaded")
        roots = page.locator(CONVERSATION_ROOT_SELECTOR)
        if roots.count() != 1:
            return pending("conversation_root_not_hydrated")
        root = roots.first
        user_turns = root.locator(USER_MESSAGE_SELECTOR)
        assistant_turns = root.locator(ASSISTANT_MESSAGE_SELECTOR)
        turns = root.locator(TURN_SELECTOR)
        user_count = user_turns.count()
        assistant_count = assistant_turns.count()
        turn_count = turns.count()
        if user_count < expected_count or assistant_count < expected_count:
            return pending("turn_cardinality_hydrating")
        if user_count > expected_count:
            return pending("unexplained_extra_user_turn")
        if assistant_count > expected_count:
            return pending("unexplained_extra_assistant_turn")
        if turn_count < expected_turn_count:
            return pending("turn_cardinality_hydrating")
        if turn_count > expected_turn_count:
            return pending("turn_cardinality_or_ordinal_ambiguous")
        turn_roles: list[str | None] = []
        turn_identities: list[str | None] = []
        for index in range(turn_count):
            turn = turns.nth(index)
            role = turn.get_attribute("data-message-author-role")
            turn_roles.append(role)
            identity = turn.get_attribute("data-message-id") or turn.get_attribute("id")
            turn_identities.append(identity if isinstance(identity, str) and identity else None)
        turn_message_id_count = sum(identity is not None for identity in turn_identities)
        expected_roles = tuple(
            role
            for _index in range(expected_count)
            for role in ("user", "assistant")
        )
        if tuple(turn_roles) != expected_roles:
            return pending("turn_cardinality_or_ordinal_ambiguous")
        expected_user_index = user_turn_ordinal * 2
        extension = (
            signature_extension(turns.nth(expected_user_index))
            if signature_extension is not None
            else ()
        )
        structure_signature = (
            observed_path,
            user_count,
            assistant_count,
            turn_count,
            tuple(turn_roles),
            tuple(turn_identities),
            extension,
        )
        if structure_signature == state.structure_signature:
            state.structure_stable_count = min(2, state.structure_stable_count + 1)
        else:
            state.structure_signature = structure_signature
            state.structure_stable_count = 1
        if state.structure_stable_count < 2:
            return result("structure_not_stable")
        return result(
            "structure_stable",
            selected_user_index=expected_user_index,
            selected_assistant_index=expected_user_index + 1,
            selected_user_id=turn_identities[expected_user_index],
            selected_assistant_id=turn_identities[expected_user_index + 1],
        )

    @staticmethod
    def _turn_anchor_tail(
        anchor: dict[str, object],
    ) -> tuple[tuple[str, str], ...] | None:
        raw_tail = anchor.get("tail")
        if not isinstance(raw_tail, list):
            return None
        tail: list[tuple[str, str]] = []
        for entry in raw_tail:
            if not isinstance(entry, dict):
                return None
            role = entry.get("role")
            identity = entry.get("id")
            if role not in {"user", "assistant"} or not isinstance(identity, str):
                return None
            if not identity:
                return None
            tail.append((role, identity))
        return tuple(tail)

    def _evaluate_local_turn_successor(
        self,
        conversation_path: str,
        anchor: dict[str, object],
        state: UnitTurnStructureState,
        *,
        signature_extension: Callable[[Any], tuple[object, ...]] | None = None,
    ) -> UnitTurnStructureObservation:
        """Bind the first local user/assistant successor of a stable pre-Send tail."""
        page = self._start()
        observed_path = conversation_path_from_page_url(str(page.url))
        raw_pre_send_count = anchor.get("pre_send_user_turn_count")
        pre_send_count = (
            raw_pre_send_count
            if isinstance(raw_pre_send_count, int) and raw_pre_send_count >= 0
            else 0
        )
        user_count = 0
        assistant_count = 0
        turn_count = 0
        turn_message_id_count = 0

        def result(
            reason: str,
            *,
            selected_user_index: int | None = None,
            selected_assistant_index: int | None = None,
            selected_user_id: str | None = None,
            selected_assistant_id: str | None = None,
        ) -> UnitTurnStructureObservation:
            return UnitTurnStructureObservation(
                conversation_path=observed_path,
                ordinal=pre_send_count,
                user_count=user_count,
                assistant_count=assistant_count,
                expected_user_count=pre_send_count + 1,
                turn_count=turn_count,
                turn_message_id_count=turn_message_id_count,
                selected_user_index=selected_user_index,
                selected_assistant_index=selected_assistant_index,
                selected_user_id=selected_user_id,
                selected_assistant_id=selected_assistant_id,
                stable_read_count=state.structure_stable_count,
                failure_reason=reason,
            )

        def pending(reason: str) -> UnitTurnStructureObservation:
            state.structure_signature = None
            state.structure_stable_count = 0
            return result(reason)

        if observed_path != conversation_path:
            return pending("conversation_path_not_loaded")
        if anchor.get("version") != 1 or anchor.get("conversation_path") != conversation_path:
            return pending("turn_anchor_identity_missing")
        anchor_tail = self._turn_anchor_tail(anchor)
        if (
            anchor_tail is None
            or len(anchor_tail) != 2
            or anchor_tail[0][0] != "user"
            or anchor_tail[1][0] != "assistant"
            or anchor_tail[0][1] == anchor_tail[1][1]
        ):
            return pending("turn_anchor_identity_missing")
        roots = page.locator(CONVERSATION_ROOT_SELECTOR)
        if roots.count() != 1:
            return pending("conversation_root_not_hydrated")
        root = roots.first
        user_count = root.locator(USER_MESSAGE_SELECTOR).count()
        assistant_count = root.locator(ASSISTANT_MESSAGE_SELECTOR).count()
        turns = root.locator(TURN_SELECTOR)
        turn_count = turns.count()
        roles: list[str | None] = []
        identities: list[str | None] = []
        for index in range(turn_count):
            turn = turns.nth(index)
            roles.append(turn.get_attribute("data-message-author-role"))
            identity = turn.get_attribute("data-message-id") or turn.get_attribute("id")
            identities.append(identity if isinstance(identity, str) and identity else None)
        turn_message_id_count = sum(identity is not None for identity in identities)
        anchor_matches = [
            index
            for index in range(max(0, turn_count - len(anchor_tail) + 1))
            if tuple(
                (roles[index + offset], identities[index + offset])
                for offset in range(len(anchor_tail))
            )
            == anchor_tail
        ]
        if not anchor_matches:
            return pending("turn_anchor_missing")
        if len(anchor_matches) != 1:
            return pending("turn_anchor_ambiguous")
        anchor_end = anchor_matches[0] + len(anchor_tail) - 1
        successor_count = turn_count - anchor_end - 1
        if successor_count == 0:
            return pending("turn_successor_user_missing")
        if successor_count == 1:
            successor_role = roles[anchor_end + 1]
            successor_id = identities[anchor_end + 1]
            if successor_role != "user" or successor_id is None:
                return pending("turn_successor_identity_missing")
            return pending("turn_successor_assistant_missing")
        if successor_count > 2:
            return pending("turn_successor_ambiguous")
        user_index = anchor_end + 1
        assistant_index = anchor_end + 2
        user_id = identities[user_index]
        assistant_id = identities[assistant_index]
        if (
            roles[user_index] != "user"
            or roles[assistant_index] != "assistant"
            or user_id is None
            or assistant_id is None
            or user_id == assistant_id
        ):
            return pending("turn_successor_identity_missing")
        extension = (
            signature_extension(turns.nth(user_index))
            if signature_extension is not None
            else ()
        )
        signature = (
            observed_path,
            anchor_tail,
            user_id,
            assistant_id,
            extension,
        )
        if signature == state.structure_signature:
            state.structure_stable_count = min(2, state.structure_stable_count + 1)
        else:
            state.structure_signature = signature
            state.structure_stable_count = 1
        if state.structure_stable_count < 2:
            return result("structure_not_stable")
        return result(
            "structure_stable",
            selected_user_index=user_index,
            selected_assistant_index=assistant_index,
            selected_user_id=user_id,
            selected_assistant_id=assistant_id,
        )

    def _scoped_assistant_content_snapshot(
        self,
        assistant: Any,
        *,
        evaluate_completion_signals: bool = True,
        page: Any | None = None,
    ) -> ScopedAssistantContentObservation:
        """Inspect model content only inside one already-bound assistant turn."""
        assistant_role = assistant.get_attribute("data-message-author-role")
        assistant_message_id = assistant.get_attribute(
            "data-message-id"
        ) or assistant.get_attribute("id")
        if assistant_role != "assistant":
            return ScopedAssistantContentObservation(
                assistant_message_id=assistant_message_id,
                semantic_container_count=0,
                semantic_visible_count=0,
                semantic_length=0,
                semantic_sha256=None,
                no_generation_indicator=True,
                no_stop=True,
                post_response_control_count=0,
                candidate_hierarchy=(),
                failure_reason="associated_assistant_role_mismatch",
            )

        candidates = assistant.locator(ASSISTANT_RENDERED_CONTENT_SELECTOR)
        candidate_count = candidates.count()
        visible_candidates: list[Any] = []
        hierarchy: list[dict[str, object]] = []
        candidate_texts: dict[int, str] = {}
        for index in range(candidate_count):
            candidate = candidates.nth(index)
            visible = candidate.is_visible() is True
            text = ""
            if visible:
                rendered = candidate.inner_text(timeout=min(1000, self.timeout_ms))
                text = rendered if isinstance(rendered, str) else ""
                visible_candidates.append(candidate)
                candidate_texts[index] = text
            raw_hierarchy = candidate.evaluate(
                """node => {
                    let parentCandidateCount = 0;
                    let parent = node.parentElement;
                    while (parent) {
                        if (parent.matches('.markdown.prose')) parentCandidateCount += 1;
                        parent = parent.parentElement;
                    }
                    return {
                        parent_candidate_count: parentCandidateCount,
                        child_candidate_count: node.querySelectorAll('.markdown.prose').length
                    };
                }"""
            )
            parent_count = 0
            child_count = 0
            if isinstance(raw_hierarchy, dict):
                raw_parent_count = raw_hierarchy.get("parent_candidate_count")
                raw_child_count = raw_hierarchy.get("child_candidate_count")
                parent_count = raw_parent_count if isinstance(raw_parent_count, int) else 0
                child_count = raw_child_count if isinstance(raw_child_count, int) else 0
            hierarchy.append(
                {
                    "candidate_index": index,
                    "visible": visible,
                    "parent_candidate_count": parent_count,
                    "child_candidate_count": child_count,
                    "text_length": len(text),
                }
            )

        no_generation_indicator = True
        no_stop = True
        post_response_control_count = 0
        if evaluate_completion_signals:
            generation_indicators = assistant.locator(
                ASSISTANT_GENERATION_INDICATOR_SELECTOR
            )
            no_generation_indicator = not any(
                generation_indicators.nth(index).is_visible()
                for index in range(generation_indicators.count())
            )
            active_page = page if page is not None else self._start()
            stop_controls = active_page.get_by_role("button", name=STOP_PATTERN)
            no_stop = not any(
                stop_controls.nth(index).is_visible()
                for index in range(stop_controls.count())
            )
            post_response_controls = assistant.locator(
                ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR
            )
            post_response_control_count = sum(
                1
                for index in range(post_response_controls.count())
                if post_response_controls.nth(index).is_visible()
            )

        visible_count = len(visible_candidates)
        semantic_text = ""
        failure_reason = "complete_candidate"
        if visible_count > 1:
            failure_reason = "semantic_container_ambiguous"
        elif visible_count == 0:
            failure_reason = "semantic_container_missing"
        else:
            visible_index = next(
                index for index, item in enumerate(hierarchy) if item["visible"] is True
            )
            semantic_text = candidate_texts[visible_index]
            if not semantic_text.strip():
                failure_reason = "semantic_text_empty"
            elif not no_generation_indicator:
                failure_reason = "generation_indicator_present"
            elif not no_stop:
                failure_reason = "stop_control_present"
            elif evaluate_completion_signals and post_response_control_count == 0:
                failure_reason = "post_response_controls_missing"
        semantic_sha256 = (
            sha256_bytes(semantic_text.encode("utf-8")) if semantic_text else None
        )
        observation = ScopedAssistantContentObservation(
            assistant_message_id=(
                assistant_message_id
                if isinstance(assistant_message_id, str) and assistant_message_id
                else None
            ),
            semantic_container_count=candidate_count,
            semantic_visible_count=visible_count,
            semantic_length=len(semantic_text),
            semantic_sha256=semantic_sha256,
            no_generation_indicator=no_generation_indicator,
            no_stop=no_stop,
            post_response_control_count=post_response_control_count,
            candidate_hierarchy=tuple(hierarchy),
            failure_reason=failure_reason,
            response_text=semantic_text or None,
        )
        LOGGER.debug(
            "browser_assistant_content_scope assistant_message_id=%s "
            "semantic_container_count=%s semantic_visible_count=%s "
            "semantic_candidate_hierarchy=%s semantic_length=%s "
            "no_generation_indicator=%s no_stop=%s "
            "post_response_control_count=%s failure_reason=%s",
            observation.assistant_message_id,
            observation.semantic_container_count,
            observation.semantic_visible_count,
            observation.candidate_hierarchy,
            observation.semantic_length,
            observation.no_generation_indicator,
            observation.no_stop,
            observation.post_response_control_count,
            observation.failure_reason,
            extra={
                "operation": "browser.capture.assistant_content_scope",
                "status": "observed",
            },
        )
        return observation

    @staticmethod
    def _record_scoped_assistant_stability(
        state: UnitResponseCompletionState,
        snapshot: ScopedAssistantContentObservation,
        *,
        signature_prefix: tuple[object, ...] = (),
        semantic_identity_only: bool = False,
    ) -> tuple[bool, int, str]:
        semantic_sha_same = (
            snapshot.semantic_sha256 is not None
            and snapshot.semantic_sha256 == state.semantic_sha256
        )
        if snapshot.failure_reason != "complete_candidate":
            state.signature = None
            state.stable_read_count = 0
            state.semantic_sha256 = snapshot.semantic_sha256
            return semantic_sha_same, 0, snapshot.failure_reason
        signature = (
            *signature_prefix,
            *(
                (snapshot.assistant_message_id, snapshot.semantic_sha256)
                if semantic_identity_only
                else snapshot.stability_signature()
            ),
        )
        if signature == state.signature:
            state.stable_read_count += 1
        else:
            state.signature = signature
            state.stable_read_count = 1
        state.semantic_sha256 = snapshot.semantic_sha256
        return (
            semantic_sha_same,
            state.stable_read_count,
            "complete" if state.stable_read_count >= 2 else "completion_not_stable",
        )

    def _evaluate_unit_response_completion(
        self,
        conversation_path: str,
        user_turn_ordinal: int,
        state: UnitResponseCompletionState,
        *,
        turn_anchor: dict[str, object] | None = None,
    ) -> UnitResponseCompletionObservation:
        """Evaluate one fresh DOM snapshot for probe and reconcile alike."""
        observed_path: str | None = None
        user_count = 0
        assistant_count = 0
        selected_user_index: int | None = None
        selected_assistant_index: int | None = None
        semantic_container_count = 0
        semantic_visible_count = 0
        semantic_text = ""
        semantic_sha256: str | None = None
        no_generation_indicator = True
        no_stop = True
        post_response_control_count = 0
        assistant_identity_sha256: str | None = None
        expected_count = user_turn_ordinal + 1
        turn_count = 0
        turn_message_id_count = 0

        def observation(reason: str) -> UnitResponseCompletionObservation:
            semantic_sha_same = (
                semantic_sha256 is not None
                and semantic_sha256 == state.semantic_sha256
            )
            if reason == "latched_assistant_temporarily_unavailable":
                stable_read_count = state.stable_read_count
                final_reason = reason
            elif reason != "complete_candidate":
                state.signature = None
                state.stable_read_count = 0
                state.semantic_sha256 = semantic_sha256
                stable_read_count = 0
                final_reason = reason
            else:
                signature = (
                    observed_path,
                    user_count,
                    assistant_count,
                    selected_user_index,
                    selected_assistant_index,
                    turn_count,
                    assistant_identity_sha256,
                    semantic_container_count,
                    semantic_visible_count,
                    len(semantic_text),
                    semantic_sha256,
                    no_generation_indicator,
                    no_stop,
                    post_response_control_count,
                )
                if signature == state.signature:
                    state.stable_read_count += 1
                else:
                    state.signature = signature
                    state.stable_read_count = 1
                state.semantic_sha256 = semantic_sha256
                stable_read_count = state.stable_read_count
                final_reason = (
                    "complete" if stable_read_count >= 2 else "completion_not_stable"
                )
            return UnitResponseCompletionObservation(
                conversation_path=observed_path,
                ordinal=user_turn_ordinal,
                user_count=user_count,
                assistant_count=assistant_count,
                selected_user_index=selected_user_index,
                selected_assistant_index=selected_assistant_index,
                selected_user_id=state.user_turn_id,
                selected_assistant_id=state.assistant_turn_id,
                expected_user_count=expected_count,
                turn_count=turn_count,
                turn_message_id_count=turn_message_id_count,
                structure_stable_count=state.structure_stable_count,
                semantic_container_count=semantic_container_count,
                semantic_visible_count=semantic_visible_count,
                semantic_length=len(semantic_text),
                semantic_sha256=semantic_sha256,
                semantic_sha_same=semantic_sha_same,
                no_generation_indicator=no_generation_indicator,
                no_stop=no_stop,
                post_response_control_count=post_response_control_count,
                stable_read_count=stable_read_count,
                failure_reason=final_reason,
                response_text=semantic_text or None,
            )

        structure = (
            self._evaluate_local_turn_successor(
                conversation_path,
                turn_anchor,
                state,
            )
            if turn_anchor is not None
            else self._evaluate_unit_turn_structure(
                conversation_path,
                user_turn_ordinal,
                state,
            )
        )
        observed_path = structure.conversation_path
        user_count = structure.user_count
        assistant_count = structure.assistant_count
        selected_user_index = structure.selected_user_index
        selected_assistant_index = structure.selected_assistant_index
        turn_count = structure.turn_count
        turn_message_id_count = structure.turn_message_id_count
        if turn_anchor is None:
            state.user_turn_id = None
            state.assistant_turn_id = None
            if structure.failure_reason != "structure_stable":
                return observation(structure.failure_reason)
        elif structure.failure_reason == "structure_stable":
            observed_user_id = structure.selected_user_id
            observed_assistant_id = structure.selected_assistant_id
            if observed_user_id is None or observed_assistant_id is None:
                return observation("turn_successor_identity_missing")
            if state.user_turn_id is not None and observed_user_id != state.user_turn_id:
                return observation("turn_successor_identity_conflict")
            if (
                state.assistant_turn_id is not None
                and observed_assistant_id != state.assistant_turn_id
            ):
                return observation("turn_successor_identity_conflict")
            state.user_turn_id = observed_user_id
            state.assistant_turn_id = observed_assistant_id
        elif (
            structure.failure_reason
            in {
                "turn_anchor_identity_missing",
                "turn_anchor_ambiguous",
                "turn_successor_ambiguous",
            }
            or state.user_turn_id is None
            or state.assistant_turn_id is None
        ):
            return observation(structure.failure_reason)

        page = self._start()
        roots = page.locator(CONVERSATION_ROOT_SELECTOR)
        if roots.count() != 1:
            if turn_anchor is not None and state.assistant_turn_id is not None:
                return observation("latched_assistant_temporarily_unavailable")
            state.structure_signature = None
            state.structure_stable_count = 0
            return observation("conversation_root_not_hydrated")
        turns = roots.first.locator(TURN_SELECTOR)
        if turn_anchor is not None and state.assistant_turn_id is not None:
            assistant_matches: list[int] = []
            for index in range(turns.count()):
                turn = turns.nth(index)
                identity = turn.get_attribute("data-message-id") or turn.get_attribute(
                    "id"
                )
                if identity == state.assistant_turn_id:
                    assistant_matches.append(index)
            if not assistant_matches:
                return observation("latched_assistant_temporarily_unavailable")
            if len(assistant_matches) != 1:
                return observation("latched_assistant_identity_ambiguous")
            selected_assistant_index = assistant_matches[0]
        else:
            assert selected_assistant_index is not None
            if selected_assistant_index >= turns.count():
                state.structure_signature = None
                state.structure_stable_count = 0
                return observation("turn_cardinality_hydrating")
        assistant = turns.nth(selected_assistant_index)
        if assistant.get_attribute("data-message-author-role") != "assistant":
            return observation("associated_assistant_role_mismatch")
        identity = assistant.get_attribute("data-message-id") or assistant.get_attribute("id")
        expected_assistant_id = (
            state.assistant_turn_id
            if turn_anchor is not None
            else structure.selected_assistant_id
        )
        if expected_assistant_id is not None and identity != expected_assistant_id:
            state.structure_signature = None
            state.structure_stable_count = 0
            return observation("turn_successor_identity_missing")
        if isinstance(identity, str) and identity:
            assistant_identity_sha256 = sha256_bytes(identity.encode("utf-8"))
        scoped = self._scoped_assistant_content_snapshot(assistant, page=page)
        semantic_container_count = scoped.semantic_container_count
        semantic_visible_count = scoped.semantic_visible_count
        semantic_text = scoped.response_text or ""
        semantic_sha256 = scoped.semantic_sha256
        no_generation_indicator = scoped.no_generation_indicator
        no_stop = scoped.no_stop
        post_response_control_count = scoped.post_response_control_count
        if turn_anchor is not None and state.assistant_turn_id is not None:
            semantic_sha_same, stable_read_count, final_reason = (
                self._record_scoped_assistant_stability(
                    state,
                    scoped,
                    signature_prefix=(conversation_path, state.assistant_turn_id),
                    semantic_identity_only=True,
                )
            )
            return UnitResponseCompletionObservation(
                conversation_path=observed_path,
                ordinal=user_turn_ordinal,
                user_count=user_count,
                assistant_count=assistant_count,
                selected_user_index=selected_user_index,
                selected_assistant_index=selected_assistant_index,
                selected_user_id=state.user_turn_id,
                selected_assistant_id=state.assistant_turn_id,
                expected_user_count=expected_count,
                turn_count=turn_count,
                turn_message_id_count=turn_message_id_count,
                structure_stable_count=state.structure_stable_count,
                semantic_container_count=semantic_container_count,
                semantic_visible_count=semantic_visible_count,
                semantic_length=len(semantic_text),
                semantic_sha256=semantic_sha256,
                semantic_sha_same=semantic_sha_same,
                no_generation_indicator=no_generation_indicator,
                no_stop=no_stop,
                post_response_control_count=post_response_control_count,
                stable_read_count=stable_read_count,
                failure_reason=final_reason,
                response_text=semantic_text or None,
            )
        return observation(scoped.failure_reason)

    def unit_response_completion_probe(
        self, conversation_path: str, user_turn_ordinal: int
    ) -> dict[str, object]:
        """Audit the canonical completion evaluator without interacting with the page."""
        if user_turn_ordinal < 0:
            raise IntegrityError(
                "BROWSER_UNIT_RESPONSE_SPIKE_INPUT_INVALID",
                "Unit response spike requires a non-negative user-turn ordinal",
            )
        self.open_conversation(conversation_path)
        deadline = monotonic() + UNIT_RESPONSE_SPIKE_HYDRATION_TIMEOUT_MS / 1000
        state = UnitResponseCompletionState()
        poll_count = 0
        while True:
            poll_count += 1
            result = self._evaluate_unit_response_completion(
                conversation_path, user_turn_ordinal, state
            )
            evidence = {
                **result.evidence(),
                "hydration_poll_count": poll_count,
                "probe_timed_out": False,
            }
            if result.failure_reason == "complete":
                return evidence
            if monotonic() >= deadline:
                evidence["probe_timed_out"] = True
                return evidence
            self._start().wait_for_timeout(UNIT_RESPONSE_SPIKE_POLL_INTERVAL_MS)

    def conversation_structure_probe(
        self, conversation_path: str
    ) -> dict[str, object]:
        """Audit turn/branch/lazy structure across scroll and reload without clicks."""
        self.open_conversation(conversation_path)
        final_url_after_navigation = str(self._start().url)
        before = self._wait_for_stable_conversation_structure(conversation_path)
        scroll_metadata = self._scroll_conversation_to_end()
        after_scroll = self._wait_for_stable_conversation_structure(conversation_path)
        self.reload_conversation(conversation_path)
        after_reload = self._wait_for_stable_conversation_structure(conversation_path)
        before_ids = before["turn_structural_ids"]
        reload_ids = after_reload["turn_structural_ids"]
        same_ids_after_reload = (
            before_ids == reload_ids
            if before["turn_id_count"] == before["turn_count"]
            and after_reload["turn_id_count"] == after_reload["turn_count"]
            else None
        )
        classification, reason = self._classify_conversation_structure(
            before, after_scroll, after_reload, same_ids_after_reload
        )
        return {
            "final_url_after_navigation": final_url_after_navigation,
            "before_scroll": before,
            "scroll_metadata": scroll_metadata,
            "after_scroll": after_scroll,
            "after_reload": after_reload,
            "same_turn_ids_after_reload": same_ids_after_reload,
            "classification": classification,
            "classification_reason": reason,
        }

    def _wait_for_stable_conversation_structure(
        self, conversation_path: str
    ) -> dict[str, object]:
        deadline = monotonic() + CONVERSATION_STRUCTURE_SPIKE_TIMEOUT_MS / 1000
        previous_signature: tuple[object, ...] | None = None
        stable_reads = 0
        poll_count = 0
        snapshot: dict[str, object] = {}
        while True:
            poll_count += 1
            snapshot = self._conversation_structure_snapshot(conversation_path)
            signature = (
                snapshot["final_url"],
                snapshot["user_count"],
                snapshot["assistant_count"],
                json.dumps(snapshot["turn_structural_ids"], sort_keys=True),
                snapshot["branch_control_count"],
                json.dumps(snapshot["branch_index_indicators"], sort_keys=True),
                snapshot["lazy_indicator_count"],
            )
            if signature == previous_signature:
                stable_reads += 1
            else:
                previous_signature = signature
                stable_reads = 1
            user_count = snapshot.get("user_count")
            assistant_count = snapshot.get("assistant_count")
            has_hydrated_turns = (
                isinstance(user_count, int) and user_count > 0
            ) or (isinstance(assistant_count, int) and assistant_count > 0)
            if stable_reads >= 2 and has_hydrated_turns:
                return {
                    **snapshot,
                    "poll_count": poll_count,
                    "stable_reads": stable_reads,
                    "timed_out": False,
                }
            if monotonic() >= deadline:
                return {
                    **snapshot,
                    "poll_count": poll_count,
                    "stable_reads": stable_reads,
                    "timed_out": True,
                }
            self._start().wait_for_timeout(
                CONVERSATION_STRUCTURE_SPIKE_POLL_INTERVAL_MS
            )

    def _conversation_structure_snapshot(
        self, conversation_path: str
    ) -> dict[str, object]:
        page = self._start()
        final_url = str(page.url)
        parsed_url = urlparse(final_url)
        url_query_parameter_names = sorted(
            {
                item.partition("=")[0]
                for item in parsed_url.query.split("&")
                if item.partition("=")[0]
            }
        )
        url_branch_state_present = any(
            re.search(r"branch|variant|alternative|message|node", name, re.IGNORECASE)
            for name in url_query_parameter_names
        ) or bool(parsed_url.fragment)
        observed_path = conversation_path_from_page_url(final_url)
        roots = page.locator(CONVERSATION_ROOT_SELECTOR)
        if observed_path != conversation_path or roots.count() != 1:
            return {
                "final_url": final_url,
                "conversation_path": observed_path,
                "conversation_root_found": False,
                "user_count": 0,
                "assistant_count": 0,
                "turn_count": 0,
                "turn_id_count": 0,
                "turn_structural_ids": [],
                "branch_control_count": 0,
                "branch_controls": [],
                "branch_index_indicators": [],
                "branch_state_attributes": [],
                "url_query_parameter_names": url_query_parameter_names,
                "url_fragment_present": bool(parsed_url.fragment),
                "url_branch_state_present": url_branch_state_present,
                "lazy_indicator_count": 0,
                "lazy_indicators": {},
                "last_assistant_visible": False,
                "last_assistant_post_response_control_count": 0,
            }
        root = roots.first
        users = root.locator(USER_MESSAGE_SELECTOR)
        assistants = root.locator(ASSISTANT_MESSAGE_SELECTOR)
        turns = root.locator(TURN_SELECTOR)
        turn_ids: list[dict[str, object]] = []
        branch_controls: list[dict[str, object]] = []
        branch_index_indicators: list[dict[str, object]] = []
        branch_state_attributes: list[dict[str, object]] = []
        for index in range(turns.count()):
            turn = turns.nth(index)
            role = turn.get_attribute("data-message-author-role")
            structural = {
                "turn_index": index,
                "role": role,
                "data_message_id": turn.get_attribute("data-message-id"),
                "id": turn.get_attribute("id"),
                "data_turn_id": turn.get_attribute("data-turn-id"),
                "data_parent_message_id": turn.get_attribute("data-parent-message-id"),
                "data_branch_id": turn.get_attribute("data-branch-id"),
                "data_conversation_id": turn.get_attribute("data-conversation-id"),
            }
            turn_ids.append(structural)
            for name in (
                "data-branch-id",
                "data-conversation-id",
                "data-parent-message-id",
                "data-current-branch",
                "data-branch-index",
                "data-branch-count",
            ):
                value = turn.get_attribute(name)
                if isinstance(value, str) and value:
                    branch_state_attributes.append(
                        {"turn_index": index, "name": name, "value": value}
                    )
            controls = turn.locator("button, [role='button']")
            for control_index in range(controls.count()):
                control = controls.nth(control_index)
                aria_label = control.get_attribute("aria-label")
                data_testid = control.get_attribute("data-testid")
                title = control.get_attribute("title")
                searchable = " ".join(
                    value
                    for value in (aria_label, data_testid, title)
                    if isinstance(value, str)
                )
                if BRANCH_CONTROL_PATTERN.search(searchable):
                    branch_controls.append(
                        {
                            "turn_index": index,
                            "control_index": control_index,
                            "aria_label": aria_label,
                            "data_testid": data_testid,
                            "title": title,
                            "disabled": control.is_disabled(),
                            "visible": control.is_visible(),
                        }
                    )
            raw_indicators = turn.evaluate(
                """element => Array.from(element.querySelectorAll("*"))
                    .filter(node => node.children.length === 0)
                    .map(node => (node.textContent || "").trim())
                    .filter(value => /^\\d+\\s*(?:\\/|of|de)\\s*\\d+$/i.test(value))
                    .slice(0, 10)"""
            )
            if isinstance(raw_indicators, list):
                for value in raw_indicators:
                    if isinstance(value, str):
                        branch_index_indicators.append(
                            {"turn_index": index, "indicator": value}
                        )
        lazy_selectors = {
            "progressbar": "[role='progressbar']",
            "aria_busy": "[aria-busy='true']",
            "data_loading": "[data-loading='true']",
            "data_state_loading": "[data-state='loading']",
            "skeleton": "[class*='skeleton' i]",
        }
        lazy_indicators: dict[str, int] = {}
        for name, selector in lazy_selectors.items():
            candidates = root.locator(selector)
            lazy_indicators[name] = sum(
                1
                for index in range(candidates.count())
                if candidates.nth(index).is_visible()
            )
        last_assistant_visible = False
        last_assistant_controls = 0
        if assistants.count():
            last_assistant = assistants.nth(assistants.count() - 1)
            last_assistant_visible = last_assistant.is_visible()
            controls = last_assistant.locator(ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR)
            last_assistant_controls = sum(
                1
                for index in range(controls.count())
                if controls.nth(index).is_visible()
            )
        return {
            "final_url": final_url,
            "conversation_path": observed_path,
            "conversation_root_found": True,
            "user_count": users.count(),
            "assistant_count": assistants.count(),
            "turn_count": turns.count(),
            "turn_id_count": sum(
                1
                for item in turn_ids
                if any(
                    item[name]
                    for name in (
                        "data_message_id",
                        "id",
                        "data_turn_id",
                        "data_parent_message_id",
                        "data_branch_id",
                    )
                )
            ),
            "turn_structural_ids": turn_ids,
            "branch_control_count": len(branch_controls),
            "branch_controls": branch_controls,
            "branch_index_indicators": branch_index_indicators,
            "branch_state_attributes": branch_state_attributes,
            "url_query_parameter_names": url_query_parameter_names,
            "url_fragment_present": bool(parsed_url.fragment),
            "url_branch_state_present": url_branch_state_present,
            "lazy_indicator_count": sum(lazy_indicators.values()),
            "lazy_indicators": lazy_indicators,
            "last_assistant_visible": last_assistant_visible,
            "last_assistant_post_response_control_count": last_assistant_controls,
        }

    def _scroll_conversation_to_end(self) -> dict[str, object]:
        raw = self._start().locator(CONVERSATION_ROOT_SELECTOR).first.evaluate(
            """root => {
                const candidates = [root, ...root.querySelectorAll("*")];
                const scrollables = candidates.filter(element =>
                    element.scrollHeight > element.clientHeight + 1 &&
                    ["auto", "scroll"].includes(getComputedStyle(element).overflowY));
                for (const element of scrollables) element.scrollTop = element.scrollHeight;
                window.scrollTo(0, document.documentElement.scrollHeight);
                return {
                    scrollable_count: scrollables.length,
                    document_scroll_height: document.documentElement.scrollHeight,
                    window_scroll_y: window.scrollY
                };
            }"""
        )
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _classify_conversation_structure(
        before: dict[str, object],
        after_scroll: dict[str, object],
        after_reload: dict[str, object],
        same_ids_after_reload: bool | None,
    ) -> tuple[str, str]:
        def integer(snapshot: dict[str, object], key: str) -> int:
            value = snapshot.get(key)
            return value if isinstance(value, int) else 0

        def has_visible_branch_control(snapshot: dict[str, object]) -> bool:
            controls = snapshot.get("branch_controls")
            return isinstance(controls, list) and any(
                isinstance(control, dict) and control.get("visible") is True
                for control in controls
            )

        def has_multiple_branch_indicator(snapshot: dict[str, object]) -> bool:
            indicators = snapshot.get("branch_index_indicators")
            if not isinstance(indicators, list):
                return False
            for item in indicators:
                if not isinstance(item, dict):
                    continue
                value = item.get("indicator")
                if not isinstance(value, str):
                    continue
                match = re.fullmatch(r"\s*\d+\s*(?:/|of|de)\s*(\d+)\s*", value, re.I)
                if match and int(match.group(1)) > 1:
                    return True
            return False

        def has_explicit_multiple_branch_state(snapshot: dict[str, object]) -> bool:
            attributes = snapshot.get("branch_state_attributes")
            if not isinstance(attributes, list):
                return False
            for item in attributes:
                if not isinstance(item, dict):
                    continue
                if item.get("name") != "data-branch-count":
                    continue
                value = item.get("value")
                if isinstance(value, str) and value.isdigit() and int(value) > 1:
                    return True
            return snapshot.get("url_branch_state_present") is True

        before_counts = (
            integer(before, "user_count"),
            integer(before, "assistant_count"),
        )
        scroll_counts = (
            integer(after_scroll, "user_count"),
            integer(after_scroll, "assistant_count"),
        )
        reload_counts = (
            integer(after_reload, "user_count"),
            integer(after_reload, "assistant_count"),
        )
        if scroll_counts[0] > before_counts[0] or scroll_counts[1] > before_counts[1]:
            return "A", "scroll_materialized_additional_turns"
        branch_evidence = any(
            has_visible_branch_control(snapshot)
            or has_multiple_branch_indicator(snapshot)
            or has_explicit_multiple_branch_state(snapshot)
            for snapshot in (before, after_scroll, after_reload)
        )
        if branch_evidence:
            return "B", "branch_or_alternative_controls_present"
        no_lazy = all(
            integer(snapshot, "lazy_indicator_count") == 0
            for snapshot in (before, after_scroll, after_reload)
        )
        if (
            before_counts[0] >= 4
            and before_counts[1] >= 4
            and before_counts == scroll_counts == reload_counts
            and no_lazy
            and same_ids_after_reload is True
        ):
            return "A", "fourth_pair_is_persisted_and_stable_after_reload"
        if (
            before_counts == scroll_counts == reload_counts
            and before_counts[0] > 0
            and before_counts[1] > 0
            and no_lazy
            and same_ids_after_reload is True
        ):
            return "C", "same_turn_ids_persist_without_lazy_or_branch_evidence"
        return "INCONCLUSIVE", "structural_evidence_does_not_distinguish_a_b_c"

    def _wait_for_hydrated_assistant_turn(
        self, conversation_path: str, assistant_turn_ordinal: int
    ) -> tuple[Any, int, int]:
        deadline = monotonic() + ASSISTANT_RESPONSE_SPIKE_HYDRATION_TIMEOUT_MS / 1000
        previous_count: int | None = None
        stable_reads = 0
        poll_count = 0
        last_path: str | None = None
        last_root_found = False
        last_candidate_count = 0
        while True:
            poll_count += 1
            page = self._start()
            last_path = conversation_path_from_page_url(str(page.url))
            roots = page.locator(CONVERSATION_ROOT_SELECTOR)
            last_root_found = last_path == conversation_path and roots.count() == 1
            candidates = (
                roots.first.locator(ASSISTANT_MESSAGE_SELECTOR)
                if last_root_found
                else None
            )
            last_candidate_count = 0 if candidates is None else candidates.count()
            if last_root_found and last_candidate_count > assistant_turn_ordinal:
                if last_candidate_count == previous_count:
                    stable_reads += 1
                else:
                    previous_count = last_candidate_count
                    stable_reads = 1
                if stable_reads >= 2 and candidates is not None:
                    return (
                        candidates.nth(assistant_turn_ordinal),
                        last_candidate_count,
                        stable_reads,
                    )
            else:
                previous_count = None
                stable_reads = 0
            if monotonic() >= deadline:
                raise IntegrityError(
                    "BROWSER_ASSISTANT_RESPONSE_SPIKE_HYDRATION_TIMEOUT",
                    "Assistant response spike could not prove one hydrated ordinal turn",
                    evidence={
                        "hydration_poll_count": poll_count,
                        "conversation_path": last_path,
                        "conversation_root_found": last_root_found,
                        "assistant_turn_candidate_count": last_candidate_count,
                        "assistant_turn_ordinal": assistant_turn_ordinal,
                        "stable_reads": stable_reads,
                    },
                )
            page.wait_for_timeout(
                ASSISTANT_RESPONSE_SPIKE_HYDRATION_POLL_INTERVAL_MS
            )

    def _wait_for_modal_audit_close_control(self) -> Any:
        deadline = monotonic() + USER_TURN_SPIKE_MODAL_TIMEOUT_MS / 1000
        previous_state: tuple[int, int, int] | None = None
        stable_reads = 0
        poll_count = 0
        last_visible_count = 0
        last_progressbar_count = 0
        last_text_length = 0
        while True:
            poll_count += 1
            page = self._start()
            candidates = page.get_by_role(
                "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
            )
            last_visible_count = sum(
                1 for index in range(candidates.count()) if candidates.nth(index).is_visible()
            )
            last_progressbar_count = 0
            last_text_length = 0
            modal_root_found = False
            if last_visible_count == 1:
                raw_state = candidates.first.evaluate(
                    """button => {
                        const visible = element => {
                            const style = window.getComputedStyle(element);
                            const rect = element.getBoundingClientRect();
                            return style.display !== "none" &&
                                style.visibility !== "hidden" &&
                                rect.width > 0 && rect.height > 0;
                        };
                        const scrollableCount = element =>
                            [...element.querySelectorAll("*")].filter(descendant => {
                                const style = window.getComputedStyle(descendant);
                                return (style.overflowY === "auto" ||
                                    style.overflowY === "scroll") &&
                                    descendant.scrollHeight > descendant.clientHeight;
                            }).length;
                        const ancestors = [];
                        let current = button.parentElement;
                        while (current && current !== document.body &&
                            current !== document.documentElement &&
                            current.tagName.toLowerCase() !== "main") {
                            ancestors.push(current);
                            current = current.parentElement;
                        }
                        const root = ancestors.find(element => scrollableCount(element) > 0) ||
                            ancestors.find(element =>
                                element.getAttribute("data-state") === "open") ||
                            ancestors.find(element =>
                                window.getComputedStyle(element).position === "fixed");
                        if (!root) return null;
                        return {
                            progressbar_count:
                                [...root.querySelectorAll("[role='progressbar']")]
                                    .filter(visible).length,
                            text_length: (root.innerText || root.textContent || "").length,
                        };
                    }"""
                )
                if isinstance(raw_state, dict):
                    raw_progressbar_count = raw_state.get("progressbar_count")
                    raw_text_length = raw_state.get("text_length")
                    if isinstance(raw_progressbar_count, int) and isinstance(
                        raw_text_length, int
                    ):
                        modal_root_found = True
                        last_progressbar_count = raw_progressbar_count
                        last_text_length = raw_text_length
            current_state = (
                last_visible_count,
                last_progressbar_count,
                last_text_length,
            )
            hydrated = (
                last_visible_count == 1
                and modal_root_found
                and last_progressbar_count == 0
                and last_text_length > 0
            )
            if hydrated and current_state == previous_state:
                stable_reads += 1
            elif hydrated:
                previous_state = current_state
                stable_reads = 1
            else:
                previous_state = None
                stable_reads = 0
            LOGGER.debug(
                "browser_user_turn_spike_modal modal_poll_count=%s "
                "modal_candidate_count=%s progressbar_count=%s "
                "modal_text_length=%s stable_reads=%s",
                poll_count,
                last_visible_count,
                last_progressbar_count,
                last_text_length,
                stable_reads,
                extra={
                    "operation": "browser.user_turn_spike.modal",
                    "status": "observed",
                },
            )
            if hydrated and stable_reads >= 2:
                return candidates.first
            if monotonic() >= deadline:
                break
            page.wait_for_timeout(USER_TURN_SPIKE_MODAL_POLL_INTERVAL_MS)
        raise IntegrityError(
            "BROWSER_USER_TURN_SPIKE_MODAL_HYDRATION_TIMEOUT",
            "Pasted-text attachment modal did not expose one stable close control",
            evidence={
                "modal_poll_count": poll_count,
                "modal_candidate_count": last_visible_count,
                "progressbar_count": last_progressbar_count,
                "modal_text_length": last_text_length,
                "stable_reads": stable_reads,
            },
        )

    def _wait_for_hydrated_user_turn(self, conversation_path: str) -> Any:
        deadline = monotonic() + USER_TURN_SPIKE_HYDRATION_TIMEOUT_MS / 1000
        poll_count = 0
        last_cardinality: int | None = None
        stable_reads = 0
        last_observed_path: str | None = None
        last_root_found = False
        last_candidate_count = 0
        while True:
            poll_count += 1
            page = self._start()
            last_observed_path = conversation_path_from_page_url(str(page.url))
            roots = page.locator(CONVERSATION_ROOT_SELECTOR)
            root_count = roots.count()
            last_root_found = (
                last_observed_path == conversation_path
                and isinstance(root_count, int)
                and root_count == 1
            )
            user_turns: Any | None = None
            last_candidate_count = 0
            if last_root_found:
                user_turns = roots.first.locator(USER_MESSAGE_SELECTOR)
                raw_candidate_count = user_turns.count()
                if isinstance(raw_candidate_count, int):
                    last_candidate_count = raw_candidate_count
                if last_cardinality == last_candidate_count:
                    stable_reads += 1
                else:
                    last_cardinality = last_candidate_count
                    stable_reads = 1
            else:
                last_cardinality = None
                stable_reads = 0
            LOGGER.debug(
                "browser_user_turn_spike_hydration hydration_poll_count=%s "
                "conversation_path=%s conversation_root_found=%s "
                "user_turn_candidate_count=%s stable_reads=%s",
                poll_count,
                last_observed_path,
                last_root_found,
                last_candidate_count,
                stable_reads,
                extra={
                    "operation": "browser.user_turn_spike.hydration",
                    "status": "observed",
                },
            )
            if (
                last_root_found
                and last_candidate_count == 1
                and stable_reads >= 2
                and user_turns is not None
            ):
                return user_turns.first
            if monotonic() >= deadline:
                break
            page.wait_for_timeout(USER_TURN_SPIKE_HYDRATION_POLL_INTERVAL_MS)
        raise IntegrityError(
            "BROWSER_USER_TURN_SPIKE_HYDRATION_TIMEOUT",
            "Conversation did not hydrate to exactly one stable user turn",
            evidence={
                "hydration_poll_count": poll_count,
                "conversation_path": last_observed_path,
                "conversation_root_found": last_root_found,
                "user_turn_candidate_count": last_candidate_count,
                "stable_reads": stable_reads,
            },
        )

    def list_conversation_paths(self) -> tuple[str, ...]:
        links = self._start().locator(CONVERSATION_LINK_SELECTOR)
        result: set[str] = set()
        for index in range(links.count()):
            href = links.nth(index).get_attribute("href")
            if href:
                parsed = urlparse(href)
                path = (
                    conversation_path_from_page_url(href)
                    if parsed.netloc
                    else parsed.path
                )
                if path is not None and is_real_conversation_path(path):
                    result.add(path)
        return tuple(sorted(result))

    def send_message(
        self,
        text: str,
        *,
        prefilled: bool = False,
        on_send_attempt_started: Callable[[], None] | None = None,
    ) -> str | None:
        if not text:
            raise IntegrityError("BROWSER_REQUEST_EMPTY", "Browser request text is empty")
        composer = self._composer()
        if composer.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS", "Expected exactly one ChatGPT message composer"
            )
        self._audit_composer(composer, phase="located")
        self._send_checkpoint("composer_found")
        composer = self._focus_composer()
        expected_fingerprint = transport_fingerprint(text)
        pasted_text_attachment_count_before = 0
        if not prefilled:
            self._ensure_empty_composer_before_insert()
            composer = self._composer()
            if len(text) > COMPOSER_FILL_MAX_CHARACTERS:
                pasted_text_attachment_count_before = (
                    self._composer_pasted_text_attachment_count(composer)
                )
            try:
                composer = self._insert_composer_text(composer, text)
            except HeliosError:
                raise
            except Exception as exc:
                raise ConflictError(
                    "BROWSER_COMPOSER_FILL_FAILED",
                    "The ChatGPT composer could not be filled",
                ) from exc
        if prefilled or len(text) <= COMPOSER_FILL_MAX_CHARACTERS:
            self._wait_for_normal_composer_insertion(
                composer,
                pasted_text_attachment_count_before=0,
            )
        pasted_text_attachment_count_after = pasted_text_attachment_count_before
        if len(text) > COMPOSER_FILL_MAX_CHARACTERS:
            pasted_text_attachment_count_after = (
                self._composer_pasted_text_attachment_count(self._composer())
            )
        big_paste_used = (
            pasted_text_attachment_count_after
            == pasted_text_attachment_count_before + 1
        )
        self._send_checkpoint("composer_filled")
        send_button = self._send_button()
        if send_button.count() != 1:
            raise ConflictError(
                "BROWSER_SEND_CONTROL_AMBIGUOUS",
                "Expected exactly one ChatGPT send button",
            )
        self._send_checkpoint("send_button_found")
        send_button = self._wait_for_send_button_enabled()
        self._send_checkpoint("send_button_enabled")
        pre_send_user_turn_count = self._capture_pre_send_user_turn_count()
        turn_anchor = self.pre_send_turn_anchor()
        if turn_anchor is None:
            raise ConflictError(
                "BROWSER_PRE_SEND_TURN_ANCHOR_UNAVAILABLE",
                "The stable pre-Send conversation tail anchor is unavailable",
            )
        raw_anchor_tail = turn_anchor.get("tail")
        structural_turn_anchor = (
            turn_anchor
            if isinstance(raw_anchor_tail, list) and len(raw_anchor_tail) == 2
            else None
        )
        self._structural_send_proof = StructuralSendProofState(
            expected_fingerprint=expected_fingerprint,
            pre_send_user_turn_count=pre_send_user_turn_count,
            big_paste_used=big_paste_used,
            turn_anchor=structural_turn_anchor,
        )
        if on_send_attempt_started is not None:
            on_send_attempt_started()
        self._send_checkpoint("send_trigger_started")
        try:
            send_button.click(timeout=self.timeout_ms)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_SEND_BUTTON_NOT_ACTIONABLE",
                "The ChatGPT send button click did not complete",
            ) from exc
        self._send_checkpoint("send_trigger_completed")
        page = self._start()
        deadline = monotonic() + self.timeout_ms / 1000
        while True:
            if not self._composer_has_text():
                self._send_checkpoint("composer_cleared")
                break
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_SEND_COMPOSER_NOT_CLEARED",
                    "The click completed but the request remained in the composer",
                )
            page.wait_for_timeout(100)
        return None

    def composer_probe(
        self, payloads: Mapping[str, str]
    ) -> dict[str, ComposerProbeCaseResult]:
        """Exercise composer insertion only; this method has no send-control code path."""
        if self.headless or self.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_COMPOSER_SPIKE_REQUIRES_HEADED_CHROME",
                "Composer spike requires headed Google Chrome with the dedicated profile",
            )
        if self.ensure_ready() is not SessionState.READY:
            raise IntegrityError(
                "BROWSER_LOGIN_REQUIRED",
                "The ChatGPT session is not ready; run the manual browser bootstrap",
            )
        self.create_conversation()
        self._clear_composer(pasted_text_attachment_baseline=0)
        results: dict[str, ComposerProbeCaseResult] = {}
        for case_name, payload in payloads.items():
            composer = self._composer()
            pasted_text_attachment_baseline = self._composer_pasted_text_attachment_count(
                composer
            )
            try:
                results[case_name] = self._probe_composer_payload(payload)
            finally:
                self._clear_composer(
                    pasted_text_attachment_baseline=pasted_text_attachment_baseline
                )
        return results

    def composer_text_diagnostic(
        self, expected_text: str, conversation_path: str
    ) -> dict[str, object]:
        """Compare safe metadata for representations of the currently filled editor."""
        editor = self._wait_for_hydrated_composer_read_only(conversation_path)
        raw = editor.evaluate(
            COMPOSER_TEXT_DIAGNOSTIC_SCRIPT,
            timeout=COMPOSER_INSERT_VERIFY_READ_TIMEOUT_MS,
        )
        if not isinstance(raw, dict):
            raise ConflictError(
                "BROWSER_COMPOSER_TEXT_SPIKE_READ_FAILED",
                "Composer representations could not be inspected",
            )
        expected_normalized = transport_normalize(expected_text)
        representations: dict[str, dict[str, object]] = {}
        comparable: list[tuple[str, str, dict[str, int | None], bool]] = []
        for name in (
            "textarea_value",
            "text_content",
            "inner_text",
            "reconstructed",
        ):
            value = raw.get(name)
            if not isinstance(value, str):
                continue
            normalized = transport_normalize(value)
            normalized_sha256 = sha256_bytes(normalized.encode("utf-8"))
            matches_expected = normalized == expected_normalized
            metadata = {
                "raw_length": len(value),
                "normalized_length": len(normalized),
                "normalized_sha256": normalized_sha256,
                "matches_expected": matches_expected,
            }
            representations[name] = metadata
            divergence = self._text_divergence_metadata(expected_normalized, normalized)
            comparable.append((name, normalized, divergence, matches_expected))
            LOGGER.debug(
                "browser_composer_text_spike representation=%s raw_length=%s "
                "normalized_length=%s normalized_sha256=%s matches_expected=%s",
                name,
                metadata["raw_length"],
                metadata["normalized_length"],
                normalized_sha256,
                matches_expected,
                extra={
                    "operation": "browser.composer_text_spike",
                    "status": "representation_observed",
                },
            )
        if not comparable:
            raise ConflictError(
                "BROWSER_COMPOSER_TEXT_SPIKE_READ_FAILED",
                "Composer exposed no comparable textual representation",
            )
        priority = {
            "textarea_value": 3,
            "reconstructed": 2,
            "inner_text": 1,
            "text_content": 0,
        }
        selected_name, _selected_text, divergence, _matches = max(
            comparable,
            key=lambda item: (
                item[3],
                int(item[2]["common_prefix_length"] or 0)
                + int(item[2]["common_suffix_length"] or 0),
                -abs(int(item[2]["length_delta"] or 0)),
                priority[item[0]],
            ),
        )
        result: dict[str, object] = {
            "expected_length": len(expected_text),
            "comparison_representation": selected_name,
            **divergence,
            "representations": representations,
        }
        LOGGER.debug(
            "browser_composer_text_spike expected_length=%s "
            "comparison_representation=%s first_divergence_index=%s "
            "common_prefix_length=%s common_suffix_length=%s length_delta=%s",
            result["expected_length"],
            selected_name,
            result["first_divergence_index"],
            result["common_prefix_length"],
            result["common_suffix_length"],
            result["length_delta"],
            extra={
                "operation": "browser.composer_text_spike",
                "status": "comparison_complete",
            },
        )
        return result

    def _wait_for_hydrated_composer_read_only(
        self,
        conversation_path: str,
        *,
        timeout_ms: int = COMPOSER_TEXT_SPIKE_HYDRATION_TIMEOUT_MS,
    ) -> Any:
        if not is_real_conversation_path(conversation_path):
            raise IntegrityError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Composer text spike requires a canonical /c/<uuid> conversation path",
            )
        page = self._start()
        if conversation_path_from_page_url(str(page.url)) != conversation_path:
            page.goto(
                f"{self.base_url}{conversation_path}",
                wait_until="domcontentloaded",
                timeout=min(timeout_ms, self.timeout_ms),
            )
        deadline = monotonic() + timeout_ms / 1000
        stable_signature: tuple[str, str | None, str | None, str | None] | None = None
        stable_reads = 0
        poll_count = 0
        candidate_count = 0
        while True:
            poll_count += 1
            candidate_count = 0
            try:
                if conversation_path_from_page_url(str(page.url)) == conversation_path:
                    candidate = self._focusable_composer()
                    candidate_count = candidate.count()
                    if candidate_count == 1 and self._is_focusable_editor(candidate):
                        metadata = self._composer_metadata(
                            candidate,
                            timeout_ms=COMPOSER_INSERT_VERIFY_READ_TIMEOUT_MS,
                        )
                        signature = (
                            metadata["tag_name"],
                            metadata["id"],
                            metadata["role"],
                            metadata["contenteditable"],
                        )
                        if signature == stable_signature:
                            stable_reads += 1
                        else:
                            stable_signature = signature
                            stable_reads = 1
                        if stable_reads >= 2:
                            return candidate
                    else:
                        stable_signature = None
                        stable_reads = 0
                else:
                    stable_signature = None
                    stable_reads = 0
            except Exception:
                stable_signature = None
                stable_reads = 0
                candidate_count = 0
            LOGGER.debug(
                "browser_composer_text_spike_hydration conversation_path=%s "
                "hydration_poll_count=%s editor_candidate_count=%s stable_reads=%s",
                conversation_path,
                poll_count,
                candidate_count,
                stable_reads,
                extra={
                    "operation": "browser.composer_text_spike.hydration",
                    "status": "poll",
                },
            )
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_COMPOSER_TEXT_SPIKE_EDITOR_NOT_FOUND",
                    "No stable visible editable composer appeared before timeout",
                    evidence={
                        "conversation_path": conversation_path,
                        "hydration_poll_count": poll_count,
                        "editor_candidate_count": candidate_count,
                        "stable_reads": stable_reads,
                    },
                )
            page.wait_for_timeout(COMPOSER_TEXT_SPIKE_HYDRATION_POLL_INTERVAL_MS)

    @staticmethod
    def _text_divergence_metadata(
        expected: str, observed: str
    ) -> dict[str, int | None]:
        prefix = 0
        prefix_limit = min(len(expected), len(observed))
        while prefix < prefix_limit and expected[prefix] == observed[prefix]:
            prefix += 1
        suffix = 0
        suffix_limit = min(len(expected) - prefix, len(observed) - prefix)
        while suffix < suffix_limit and expected[-(suffix + 1)] == observed[-(suffix + 1)]:
            suffix += 1
        return {
            "first_divergence_index": None if expected == observed else prefix,
            "common_prefix_length": prefix,
            "common_suffix_length": suffix,
            "length_delta": len(observed) - len(expected),
        }

    def current_conversation_path(self) -> str | None:
        return self._path()

    def inspect_turn(self, requested_fingerprint: str) -> TurnInspection:
        page = self._start()
        scan = self._scan_user_turn_proof(requested_fingerprint)
        conversation_path = self._path()
        evidence: dict[str, object] = {
            "user_turn_candidate_count": scan["candidate_count"],
            "user_turn_text_length": scan["text_length"],
            "pasted_text_attachment_found": scan["attachment_found"],
            "pasted_text_attachment_opened": scan["attachment_opened"],
            "pasted_text_content_length": scan["attachment_content_length"],
            "observed_fingerprint": scan["observed_fingerprint"],
            "expected_fingerprint": requested_fingerprint,
            "conversation_path": conversation_path,
        }
        self._log_user_turn_proof(evidence)
        matches = scan["match_indices"]
        if scan["ambiguous"] or len(matches) > 1:
            return TurnInspection(
                TurnState.AMBIGUOUS,
                conversation_path,
                evidence=evidence,
            )
        if scan["pending"] or not matches:
            return TurnInspection(
                TurnState.NOT_SENT,
                conversation_path,
                evidence=evidence,
            )
        turns = page.locator(TURN_SELECTOR)
        observed_fingerprint = requested_fingerprint
        next_index = matches[0] + 1
        if next_index >= turns.count():
            return TurnInspection(
                TurnState.STREAMING,
                conversation_path,
                evidence={**evidence, "assistant_started": False},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        assistant = turns.nth(next_index)
        if assistant.get_attribute("data-message-author-role") != "assistant":
            return TurnInspection(
                TurnState.AMBIGUOUS,
                conversation_path,
                evidence={**evidence, "assistant_turns": 0},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        if page.get_by_role("button", name=STOP_PATTERN).count():
            return TurnInspection(
                TurnState.STREAMING,
                conversation_path,
                evidence={**evidence, "assistant_started": True},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        first = assistant.inner_text()
        page.wait_for_timeout(250)
        second = assistant.inner_text()
        if first != second:
            return TurnInspection(
                TurnState.STREAMING,
                conversation_path,
                evidence={**evidence, "assistant_started": True},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        if not second.strip():
            return TurnInspection(
                TurnState.AMBIGUOUS,
                conversation_path,
                evidence=evidence,
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        return TurnInspection(
            TurnState.COMPLETE,
            conversation_path,
            second,
            evidence={**evidence, "assistant_started": True},
            observed_user_turn_fingerprint=observed_fingerprint,
        )

    def inspect_sent_turn_structure(self, requested_fingerprint: str) -> TurnInspection:
        """Prove the just-sent turn without reopening or reading pasted-text content."""
        state = self._structural_send_proof
        if state is None or state.expected_fingerprint != requested_fingerprint:
            raise ConflictError(
                "BROWSER_SEND_REQUIRES_RECONCILE",
                "The in-process pre-Send user-turn baseline is unavailable",
            )
        expected_user_turn_count = state.pre_send_user_turn_count + 1
        attachment_count = 0

        def attachment_signature(user_turn: Any) -> tuple[object, ...]:
            nonlocal attachment_count
            attachment_count = user_turn.locator(
                USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
            ).count()
            return (attachment_count,)

        current_path = self._path()
        conversation_path = current_path if current_path is not None else ""
        structure = (
            self._evaluate_local_turn_successor(
                conversation_path,
                state.turn_anchor,
                state,
                signature_extension=attachment_signature,
            )
            if state.turn_anchor is not None
            else self._evaluate_unit_turn_structure(
                conversation_path,
                state.pre_send_user_turn_count,
                state,
                signature_extension=attachment_signature,
            )
        )
        observed_path = structure.conversation_path
        evidence: dict[str, object] = {
            "proof_kind": (
                "post_send_local_successor_v1"
                if state.turn_anchor is not None
                else "post_send_structure_v1"
            ),
            "conversation_path": observed_path,
            "user_turn_count_before": state.pre_send_user_turn_count,
            "user_turn_candidate_count": structure.user_count,
            "expected_user_turn_count": expected_user_turn_count,
            "assistant_turn_candidate_count": structure.assistant_count,
            "turn_count": structure.turn_count,
            "turn_message_id_count": structure.turn_message_id_count,
            "selected_user_id": structure.selected_user_id,
            "selected_assistant_id": structure.selected_assistant_id,
            "big_paste_used": state.big_paste_used,
            "new_user_turn_attachment_count": attachment_count,
            "stable_reads": structure.stable_read_count,
            "failure_reason": structure.failure_reason,
        }
        if structure.failure_reason in UNIT_TURN_STRUCTURE_AMBIGUOUS_REASONS:
            return TurnInspection(TurnState.AMBIGUOUS, observed_path, evidence=evidence)
        if state.big_paste_used and attachment_count > 1:
            return TurnInspection(TurnState.AMBIGUOUS, observed_path, evidence=evidence)
        if structure.failure_reason != "structure_stable":
            return TurnInspection(TurnState.NOT_SENT, observed_path, evidence=evidence)
        if state.big_paste_used and attachment_count == 0:
            return TurnInspection(TurnState.NOT_SENT, observed_path, evidence=evidence)
        self._log_structural_user_turn_proof(evidence)
        turn_index = structure.selected_user_index
        assert turn_index is not None
        state.user_turn_index = turn_index
        state.user_turn_id = (
            structure.selected_user_id if state.turn_anchor is not None else None
        )
        state.assistant_turn_id = (
            structure.selected_assistant_id if state.turn_anchor is not None else None
        )

        try:
            assistant = self._assistant_for_structural_binding(
                state.user_turn_id,
                state.assistant_turn_id,
                fallback_user_index=turn_index,
            )
        except ConflictError:
            return TurnInspection(
                TurnState.AMBIGUOUS,
                observed_path,
                evidence={**evidence, "assistant_turns": 0},
                observed_user_turn_fingerprint=requested_fingerprint,
            )
        scoped = self._scoped_assistant_content_snapshot(assistant)
        evidence = {**evidence, **scoped.evidence(), "assistant_started": True}
        if scoped.failure_reason in {
            "associated_assistant_role_mismatch",
            "semantic_container_ambiguous",
        }:
            return TurnInspection(
                TurnState.AMBIGUOUS,
                observed_path,
                evidence=evidence,
                observed_user_turn_fingerprint=requested_fingerprint,
            )
        if state.completion_state is None:
            state.completion_state = UnitResponseCompletionState()
        _sha_same, stable_reads, completion_reason = (
            self._record_scoped_assistant_stability(
                state.completion_state,
                scoped,
                signature_prefix=(
                    observed_path,
                    state.user_turn_id,
                    state.assistant_turn_id,
                ),
            )
        )
        evidence["response_stable_reads"] = stable_reads
        evidence["response_completion_reason"] = completion_reason
        if completion_reason != "complete":
            return TurnInspection(
                TurnState.STREAMING,
                observed_path,
                evidence=evidence,
                observed_user_turn_fingerprint=requested_fingerprint,
            )
        assert scoped.response_text is not None
        return TurnInspection(
            TurnState.COMPLETE,
            observed_path,
            scoped.response_text,
            evidence=evidence,
            observed_user_turn_fingerprint=requested_fingerprint,
        )

    def capture_structural_response(self, requested_fingerprint: str) -> str:
        state = self._structural_send_proof
        if (
            state is None
            or state.expected_fingerprint != requested_fingerprint
            or state.user_turn_index is None
            or state.stable_reads < 2
            or state.completion_state is None
            or state.completion_state.stable_read_count < 2
        ):
            raise ConflictError(
                "BROWSER_SEND_REQUIRES_RECONCILE",
                "The structurally proven in-process user turn is unavailable",
            )
        assistant = self._assistant_for_structural_binding(
            state.user_turn_id,
            state.assistant_turn_id,
            fallback_user_index=state.user_turn_index,
        )
        return self._capture_assistant_response(
            assistant,
            expected_assistant_id=state.assistant_turn_id,
            expected_semantic_sha256=state.completion_state.semantic_sha256,
        )

    def _assistant_for_structural_binding(
        self,
        user_turn_id: str | None,
        assistant_turn_id: str | None,
        *,
        fallback_user_index: int | None = None,
    ) -> Any:
        roots = self._start().locator(CONVERSATION_ROOT_SELECTOR)
        if roots.count() != 1:
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The structurally proven conversation root is unavailable",
            )
        turns = roots.first.locator(TURN_SELECTOR)
        if user_turn_id is not None and assistant_turn_id is not None:
            matches: list[int] = []
            identities: list[str | None] = []
            roles: list[str | None] = []
            for index in range(turns.count()):
                turn = turns.nth(index)
                roles.append(turn.get_attribute("data-message-author-role"))
                identity = turn.get_attribute("data-message-id") or turn.get_attribute("id")
                identities.append(identity if isinstance(identity, str) and identity else None)
                if identity == user_turn_id:
                    matches.append(index)
            if len(matches) != 1:
                raise ConflictError(
                    "BROWSER_TURN_AMBIGUOUS",
                    "The structurally proven user turn identity is unavailable",
                )
            user_index = matches[0]
            assistant_index = user_index + 1
            if (
                assistant_index >= len(identities)
                or roles[user_index] != "user"
                or roles[assistant_index] != "assistant"
                or identities[assistant_index] != assistant_turn_id
            ):
                raise ConflictError(
                    "BROWSER_TURN_AMBIGUOUS",
                    "The structurally proven assistant successor is unavailable",
                )
            return turns.nth(assistant_index)
        if fallback_user_index is None or fallback_user_index + 1 >= turns.count():
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The structurally proven user turn has no assistant response",
            )
        assistant = turns.nth(fallback_user_index + 1)
        if assistant.get_attribute("data-message-author-role") != "assistant":
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The message after the structurally proven user turn is not an assistant turn",
            )
        return assistant

    def _turn_index_for_user_ordinal(self, ordinal: int) -> int | None:
        turns = self._start().locator(TURN_SELECTOR)
        observed_users = 0
        for index in range(turns.count()):
            if turns.nth(index).get_attribute("data-message-author-role") != "user":
                continue
            if observed_users == ordinal:
                return index
            observed_users += 1
        return None

    @staticmethod
    def _log_structural_user_turn_proof(evidence: dict[str, object]) -> None:
        LOGGER.debug(
            "browser_user_turn_structure conversation_path=%s "
            "user_turn_count_before=%s user_turn_candidate_count=%s "
            "big_paste_used=%s new_user_turn_attachment_count=%s stable_reads=%s",
            evidence["conversation_path"],
            evidence["user_turn_count_before"],
            evidence["user_turn_candidate_count"],
            evidence["big_paste_used"],
            evidence["new_user_turn_attachment_count"],
            evidence["stable_reads"],
            extra={
                "operation": "browser.inspect_sent_turn_structure",
                "status": "observed",
            },
        )

    def inspect_reconciliation_turn(self, requested_fingerprint: str) -> TurnInspection:
        page = self._start()
        turn = self._first_user_turn()
        if turn is None:
            return TurnInspection(TurnState.NOT_SENT, self._path())
        user_text = self._reconciliation_user_turn_text(turn, requested_fingerprint)
        observed_fingerprint = transport_fingerprint(user_text)
        if observed_fingerprint != requested_fingerprint:
            return TurnInspection(
                TurnState.NOT_SENT,
                self._path(),
                evidence={"first_user_turn_fingerprint": observed_fingerprint},
            )
        assistant = self._assistant_after_user_turn(turn)
        if assistant is None:
            return TurnInspection(
                TurnState.STREAMING,
                self._path(),
                evidence={"assistant_started": False},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        if page.get_by_role("button", name=STOP_PATTERN).count():
            return TurnInspection(
                TurnState.STREAMING,
                self._path(),
                evidence={"assistant_started": True},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        first = assistant.inner_text()
        page.wait_for_timeout(250)
        second = assistant.inner_text()
        if first != second:
            return TurnInspection(
                TurnState.STREAMING,
                self._path(),
                evidence={"assistant_started": True},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        if not second.strip():
            return TurnInspection(
                TurnState.AMBIGUOUS,
                self._path(),
                evidence={"assistant_started": True},
                observed_user_turn_fingerprint=observed_fingerprint,
            )
        return TurnInspection(
            TurnState.COMPLETE,
            self._path(),
            second,
            evidence={"assistant_started": True},
            observed_user_turn_fingerprint=observed_fingerprint,
        )

    def capture_reconciled_response(self, requested_fingerprint: str) -> str:
        turn = self._first_user_turn()
        if turn is None:
            raise ConflictError(
                "BROWSER_RECONCILE_USER_TURN_NOT_FOUND",
                "The candidate conversation has no first user turn",
            )
        user_text = self._reconciliation_user_turn_text(turn, requested_fingerprint)
        if transport_fingerprint(user_text) != requested_fingerprint:
            raise ConflictError(
                "BROWSER_RECONCILE_FINGERPRINT_MISMATCH",
                "The candidate first user turn does not match the prepared request",
            )
        assistant = self._assistant_after_user_turn(turn)
        if assistant is None:
            raise ConflictError(
                "BROWSER_RECONCILE_RESPONSE_MISSING",
                "The proven first user turn has no corresponding assistant response",
            )
        text = assistant.inner_text()
        if not isinstance(text, str) or not text.strip():
            raise IntegrityError(
                "BROWSER_RECONCILE_RESPONSE_EMPTY",
                "The reconciled rendered response is empty",
            )
        return text

    def inspect_reconciliation_unit_turn(
        self,
        turn_binding: int | dict[str, object],
        expected_conversation_path: str | None = None,
    ) -> TurnInspection:
        turn_anchor = turn_binding if isinstance(turn_binding, dict) else None
        raw_ordinal = (
            turn_anchor.get("pre_send_user_turn_count")
            if turn_anchor is not None
            else turn_binding
        )
        user_turn_ordinal = raw_ordinal if isinstance(raw_ordinal, int) else -1
        conversation_path = expected_conversation_path or self._path()
        if conversation_path is None or not is_real_conversation_path(conversation_path):
            evidence = UnitResponseCompletionObservation(
                conversation_path=conversation_path,
                ordinal=user_turn_ordinal,
                user_count=0,
                assistant_count=0,
                selected_user_index=None,
                selected_assistant_index=None,
                selected_user_id=None,
                selected_assistant_id=None,
                expected_user_count=user_turn_ordinal + 1,
                turn_count=0,
                turn_message_id_count=0,
                structure_stable_count=0,
                semantic_container_count=0,
                semantic_visible_count=0,
                semantic_length=0,
                semantic_sha256=None,
                semantic_sha_same=False,
                no_generation_indicator=True,
                no_stop=True,
                post_response_control_count=0,
                stable_read_count=0,
                failure_reason="conversation_path_not_loaded",
            ).evidence()
            return TurnInspection(TurnState.NOT_SENT, conversation_path, evidence=evidence)
        anchor_tail = self._turn_anchor_tail(turn_anchor) if turn_anchor is not None else None
        binding_key: object = (
            tuple(anchor_tail) if anchor_tail is not None else "invalid_anchor"
        ) if turn_anchor is not None else user_turn_ordinal
        key = (conversation_path, binding_key)
        state = self._unit_reconciliation_proofs.setdefault(
            key, UnitReconciliationProofState()
        )
        if state.completion_state is None:
            state.completion_state = UnitResponseCompletionState()
        if turn_anchor is not None:
            recovery_user_id = turn_anchor.get("recovery_selected_user_id")
            recovery_assistant_id = turn_anchor.get(
                "recovery_selected_assistant_id"
            )
            if (
                state.completion_state.user_turn_id is None
                and isinstance(recovery_user_id, str)
                and recovery_user_id
            ):
                state.completion_state.user_turn_id = recovery_user_id
            if (
                state.completion_state.assistant_turn_id is None
                and isinstance(recovery_assistant_id, str)
                and recovery_assistant_id
                and not recovery_assistant_id.startswith("request-placeholder-")
            ):
                state.completion_state.assistant_turn_id = recovery_assistant_id
        result = self._evaluate_unit_response_completion(
            conversation_path,
            user_turn_ordinal,
            state.completion_state,
            turn_anchor=turn_anchor,
        )
        state.stable_reads = result.stable_read_count
        state.user_turn_index = result.selected_user_index
        state.user_turn_id = state.completion_state.user_turn_id
        state.assistant_turn_id = state.completion_state.assistant_turn_id
        evidence = {
            "proof_kind": (
                "persisted_unit_local_successor_v1"
                if turn_anchor is not None
                else "persisted_unit_ordinal_v1"
            ),
            **result.evidence(),
        }
        LOGGER.debug(
            "browser_reconcile_unit_structure conversation_path=%s "
            "ordinal=%s user_count=%s assistant_count=%s "
            "selected_user_index=%s selected_assistant_index=%s "
            "expected_user_count=%s turn_count=%s turn_message_id_count=%s "
            "structure_stable_count=%s "
            "semantic_container_count=%s semantic_visible_count=%s "
            "semantic_length=%s semantic_sha_same=%s "
            "no_generation_indicator=%s no_stop=%s "
            "post_response_control_count=%s stable_read_count=%s "
            "failure_reason=%s",
            conversation_path,
            user_turn_ordinal,
            result.user_count,
            result.assistant_count,
            result.selected_user_index,
            result.selected_assistant_index,
            result.expected_user_count,
            result.turn_count,
            result.turn_message_id_count,
            result.structure_stable_count,
            result.semantic_container_count,
            result.semantic_visible_count,
            result.semantic_length,
            result.semantic_sha_same,
            result.no_generation_indicator,
            result.no_stop,
            result.post_response_control_count,
            result.stable_read_count,
            result.failure_reason,
            extra={
                "operation": "browser.reconcile.unit_structure",
                "status": "observed",
            },
        )
        ambiguous_reasons = {
            *UNIT_TURN_STRUCTURE_AMBIGUOUS_REASONS,
            "associated_assistant_role_mismatch",
            "semantic_container_ambiguous",
        }
        if result.failure_reason in ambiguous_reasons:
            return TurnInspection(
                TurnState.AMBIGUOUS,
                conversation_path,
                evidence=evidence,
            )
        if result.failure_reason != "complete":
            state_kind = (
                TurnState.NOT_SENT
                if result.failure_reason
                in {
                    "conversation_path_not_loaded",
                    "conversation_root_not_hydrated",
                    "turn_cardinality_hydrating",
                    "turn_anchor_missing",
                    "turn_successor_user_missing",
                    "structure_not_stable",
                }
                else TurnState.STREAMING
            )
            return TurnInspection(state_kind, conversation_path, evidence=evidence)
        return TurnInspection(
            TurnState.COMPLETE,
            conversation_path,
            result.response_text,
            evidence=evidence,
        )

    def capture_reconciled_unit_response(
        self, turn_binding: int | dict[str, object]
    ) -> str:
        conversation_path = self._path()
        if conversation_path is None:
            raise ConflictError(
                "BROWSER_RECONCILE_WRONG_CONVERSATION",
                "Unit reconciliation has no canonical conversation",
            )
        turn_anchor = turn_binding if isinstance(turn_binding, dict) else None
        anchor_tail = self._turn_anchor_tail(turn_anchor) if turn_anchor is not None else None
        binding_key: object = (
            tuple(anchor_tail) if anchor_tail is not None else "invalid_anchor"
        ) if turn_anchor is not None else turn_binding
        state = self._unit_reconciliation_proofs.get((conversation_path, binding_key))
        if (
            state is None
            or state.stable_reads < 2
            or (
                state.user_turn_index is None
                and (
                    state.user_turn_id is None
                    or state.assistant_turn_id is None
                )
            )
        ):
            raise ConflictError(
                "BROWSER_RECONCILE_UNIT_TURN_UNPROVEN",
                "Unit response capture requires a stable ordinal user-turn proof",
            )
        assistant = (
            self._assistant_for_latched_identity(state.assistant_turn_id)
            if state.assistant_turn_id is not None
            else self._assistant_for_structural_binding(
                state.user_turn_id,
                state.assistant_turn_id,
                fallback_user_index=state.user_turn_index,
            )
        )
        assert state.completion_state is not None
        return self._capture_assistant_response(
            assistant,
            expected_assistant_id=state.assistant_turn_id,
            expected_semantic_sha256=state.completion_state.semantic_sha256,
        )

    def _assistant_for_latched_identity(self, assistant_turn_id: str) -> Any:
        roots = self._start().locator(CONVERSATION_ROOT_SELECTOR)
        if roots.count() != 1:
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The conversation root for the latched assistant is unavailable",
            )
        turns = roots.first.locator(TURN_SELECTOR)
        matches: list[int] = []
        for index in range(turns.count()):
            turn = turns.nth(index)
            identity = turn.get_attribute("data-message-id") or turn.get_attribute("id")
            if identity == assistant_turn_id:
                matches.append(index)
        if len(matches) != 1:
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The latched assistant identity is absent or ambiguous during capture",
            )
        assistant = turns.nth(matches[0])
        if assistant.get_attribute("data-message-author-role") != "assistant":
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "The latched response identity is no longer an assistant turn",
            )
        return assistant

    def recapture_persisted_unit_response(self, user_turn_ordinal: int) -> str:
        """Read one persisted ordinal assistant response without page interaction."""
        if user_turn_ordinal < 0:
            raise IntegrityError(
                "BROWSER_RECAPTURE_ORDINAL_INVALID",
                "Persisted user-turn ordinal must be non-negative",
            )
        deadline = monotonic() + self.timeout_ms / 1000
        previous_signature: tuple[str | None, int, int, int, str] | None = None
        stable_reads = 0
        poll_count = 0
        last_path: str | None = None
        last_user_count = 0
        last_turn_count = 0
        last_assistant_index = -1
        while True:
            poll_count += 1
            page = self._start()
            last_path = self._path()
            user_turns = page.locator(USER_MESSAGE_SELECTOR)
            turns = page.locator(TURN_SELECTOR)
            last_user_count = user_turns.count()
            last_turn_count = turns.count()
            text: str | None = None
            if last_user_count > user_turn_ordinal:
                user_index = self._turn_index_for_user_ordinal(user_turn_ordinal)
                if user_index is not None and user_index + 1 < last_turn_count:
                    last_assistant_index = user_index + 1
                    assistant = turns.nth(last_assistant_index)
                    if assistant.get_attribute("data-message-author-role") == "assistant":
                        try:
                            text = self._capture_rendered_assistant_response(assistant)
                        except (ConflictError, IntegrityError):
                            text = None
            if text is not None:
                signature = (
                    last_path,
                    last_user_count,
                    last_turn_count,
                    last_assistant_index,
                    sha256_bytes(text.encode("utf-8", errors="strict")),
                )
                if signature == previous_signature:
                    stable_reads += 1
                else:
                    previous_signature = signature
                    stable_reads = 1
                if stable_reads >= 2:
                    return text
            else:
                previous_signature = None
                stable_reads = 0
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_RECAPTURE_TURN_HYDRATION_TIMEOUT",
                    "Persisted ordinal assistant response did not become stable and capturable",
                    evidence={
                        "poll_count": poll_count,
                        "conversation_path": last_path,
                        "user_turn_ordinal": user_turn_ordinal,
                        "user_turn_candidate_count": last_user_count,
                        "turn_candidate_count": last_turn_count,
                        "assistant_turn_index": last_assistant_index,
                        "stable_reads": stable_reads,
                    },
                )
            page.wait_for_timeout(250)

    def find_reconciliation_candidates(
        self, requested_fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]:
        del started_at  # The service validates the observed turn timestamp against the boundary.
        current_path = self._path()
        candidates: list[BootstrapCandidate] = []
        for path in self.list_conversation_paths():
            self.open_conversation(path)
            inspection = self.inspect_reconciliation_turn(requested_fingerprint)
            observed_at = self._first_user_turn_observed_at()
            if (
                inspection.observed_user_turn_fingerprint == requested_fingerprint
                and observed_at is not None
            ):
                candidates.append(BootstrapCandidate(path, requested_fingerprint, observed_at))
        if current_path is not None:
            self.open_conversation(current_path)
        return tuple(candidates)

    def find_bootstrap_candidates(
        self, requested_fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]:
        del started_at  # The caller additionally enforces the persisted operational baseline.
        current_path = self._path()
        candidates: list[BootstrapCandidate] = []
        for path in self.list_conversation_paths():
            self.open_conversation(path)
            inspection = self.inspect_turn(requested_fingerprint)
            if (
                inspection.state in {TurnState.STREAMING, TurnState.COMPLETE}
                and inspection.observed_user_turn_fingerprint == requested_fingerprint
            ):
                candidates.append(
                    BootstrapCandidate(
                        path, inspection.observed_user_turn_fingerprint, utc_now()
                    )
                )
        if current_path is not None:
            self.open_conversation(current_path)
        return tuple(candidates)

    def capture_spike(self, sample_kind: str) -> CaptureSpikeResult:
        if sample_kind not in {"acknowledgement", "long_unit"}:
            raise IntegrityError(
                "BROWSER_CAPTURE_SPIKE_KIND_INVALID", "Unknown capture spike sample kind"
            )
        if self.ensure_ready() is not SessionState.READY:
            raise IntegrityError(
                "BROWSER_LOGIN_REQUIRED",
                "The ChatGPT session is not ready; run the manual browser bootstrap",
            )
        self._spike_checkpoint("provider_ready")
        self.create_conversation()
        self._spike_checkpoint("conversation_ready")

        composer = self._composer()
        if composer.count() != 1:
            raise ConflictError(
                "BROWSER_SPIKE_COMPOSER_NOT_FOUND",
                "Expected exactly one composer for the capture spike",
            )
        self._spike_checkpoint("composer_found")
        spike_marker = f"HELIOS-SPIKE-{uuid4()}"
        prompt = SPIKE_PROMPTS[sample_kind].format(marker=spike_marker)
        try:
            composer.fill(prompt)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_SPIKE_COMPOSER_NOT_FOUND",
                "The spike composer could not be filled",
            ) from exc
        self._spike_checkpoint("composer_filled")
        try:
            self.send_message(prompt, prefilled=True)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_SPIKE_PROMPT_NOT_SENT",
                "The synthetic capture-spike prompt could not be sent",
            ) from exc
        if self._composer_has_text():
            raise ConflictError(
                "BROWSER_SPIKE_PROMPT_NOT_SENT",
                "The send action completed but the synthetic prompt remained in the composer",
            )
        self._spike_checkpoint("send_triggered")

        fingerprint = transport_fingerprint(prompt)
        inspection = self._wait_for_spike_user_turn(fingerprint)
        self._spike_checkpoint("user_turn_observed")
        inspection = self._wait_for_spike_response_start(fingerprint, inspection)
        self._spike_checkpoint("assistant_response_started")
        inspection = self._wait_for_spike_completion(fingerprint, inspection)
        self._spike_checkpoint("assistant_response_completed")
        if not inspection.response_text or not inspection.response_text.strip():
            raise IntegrityError(
                "BROWSER_SPIKE_RESPONSE_EMPTY", "The completed spike response is empty"
            )

        try:
            turn = self._assistant_for_fingerprint(fingerprint)
        except ConflictError as exc:
            raise ConflictError(
                "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND",
                "Cannot locate exactly one assistant turn for the synthetic spike prompt",
            ) from exc
        assistant = self._assistant_content(turn)
        if assistant.count() != 1:
            raise ConflictError(
                "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND",
                "Cannot locate exactly one assistant response for the synthetic spike prompt",
            )
        rendered_text = assistant.inner_text()
        if not isinstance(rendered_text, str) or not rendered_text.strip():
            raise IntegrityError(
                "BROWSER_SPIKE_RESPONSE_EMPTY", "The rendered spike response is empty"
            )
        rendered = rendered_text.encode("utf-8", errors="strict")
        self._spike_checkpoint("rendered_capture")

        copy_button = self._copy_button_for_assistant(assistant)
        if copy_button.count() != 1:
            raise ConflictError(
                "BROWSER_COPY_CAPTURE_UNAVAILABLE",
                "The Copy control is absent or ambiguous for the spike assistant turn",
            )
        clipboard = self._activate_copy_and_read(copy_button)
        if not isinstance(clipboard, str) or not clipboard.strip():
            raise IntegrityError(
                "BROWSER_SPIKE_RESPONSE_EMPTY", "The copied spike response is empty"
            )
        copied = clipboard.encode("utf-8", errors="strict")
        self._spike_checkpoint("copy_capture")
        result = CaptureSpikeResult(
            sample_kind=sample_kind,
            rendered_sha256=sha256_bytes(rendered),
            copied_sha256=sha256_bytes(copied),
            equivalent=copied == rendered,
            selected_method_version=self.capture_method_version,
            comparison=compare_capture_text(rendered_text, clipboard),
        )
        if self.capture_method_version not in {"rendered_text_v1", "copy_text_v1"}:
            raise IntegrityError(
                "BROWSER_CAPTURE_METHOD_UNSUPPORTED", "Unsupported capture method version"
            )
        if self.capture_method_version == "copy_text_v1":
            selected_size = len(copied)
        else:
            selected_size = len(rendered)
        if sample_kind == "acknowledgement" and selected_size > 2000:
            raise ConflictError(
                "BROWSER_CAPTURE_SPIKE_SAMPLE_INVALID",
                "Acknowledgement spike must use a short assistant response",
            )
        if sample_kind == "long_unit" and selected_size < 3000:
            raise ConflictError(
                "BROWSER_CAPTURE_SPIKE_SAMPLE_INVALID",
                "Long-unit spike must use a substantial assistant response",
            )
        marker: dict[str, object] = {"samples": {}}
        try:
            loaded = json.loads(self._capture_gate_path().read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                marker = loaded
        except (OSError, json.JSONDecodeError):
            pass
        samples = marker.get("samples")
        if not isinstance(samples, dict):
            samples = {}
        history = marker.get("history")
        if not isinstance(history, list):
            history = []
        previous_sample = samples.get(sample_kind)
        if isinstance(previous_sample, dict):
            history.append({"sample_kind": sample_kind, **previous_sample})
        samples[sample_kind] = {
            "comparison": result.comparison,
            "equivalent": result.equivalent,
            "rendered_sha256": result.rendered_sha256,
            "copied_sha256": result.copied_sha256,
        }
        marker = {
            "history": history,
            "samples": samples,
            "selected_method_version": result.selected_method_version,
        }
        self._capture_gate_path().parent.mkdir(parents=True, exist_ok=True)
        self._capture_gate_path().write_text(
            json.dumps(marker, sort_keys=True),
            encoding="utf-8",
        )
        return result

    def _wait_for_spike_user_turn(self, fingerprint: str) -> TurnInspection:
        deadline = monotonic() + min(15.0, self.timeout_ms / 1000)
        while monotonic() < deadline:
            inspection = self.inspect_turn(fingerprint)
            if inspection.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND",
                    "The spike turn structure is ambiguous",
                )
            if inspection.state is not TurnState.NOT_SENT:
                return inspection
            self._start().wait_for_timeout(100)
        LOGGER.debug(
            "browser_capture_spike checkpoint=user_turn_timeout evidence=%s",
            json.dumps(self._spike_dom_evidence(), sort_keys=True),
            extra={
                "operation": "browser.capture_spike",
                "status": "user_turn_timeout",
            },
        )
        raise IntegrityError(
            "BROWSER_SPIKE_USER_TURN_NOT_OBSERVED",
            "The synthetic prompt was triggered but its user turn was not observed",
        )

    def _wait_for_spike_response_start(
        self, fingerprint: str, inspection: TurnInspection
    ) -> TurnInspection:
        deadline = monotonic() + self.timeout_ms / 1000
        current = inspection
        while monotonic() < deadline:
            if current.state is TurnState.COMPLETE or bool(
                (current.evidence or {}).get("assistant_started")
            ):
                return current
            current = self.inspect_turn(fingerprint)
            if current.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND",
                    "The assistant turn became ambiguous before response start",
                )
            self._start().wait_for_timeout(100)
        LOGGER.debug(
            "browser_capture_spike checkpoint=response_start_timeout evidence=%s",
            json.dumps(self._spike_dom_evidence(), sort_keys=True),
            extra={
                "operation": "browser.capture_spike",
                "status": "response_start_timeout",
            },
        )
        raise IntegrityError(
            "BROWSER_SPIKE_RESPONSE_NOT_STARTED",
            "The user turn was observed but no assistant response started",
        )

    def _wait_for_spike_completion(
        self, fingerprint: str, inspection: TurnInspection
    ) -> TurnInspection:
        deadline = monotonic() + self.timeout_ms / 1000
        current = inspection
        while monotonic() < deadline:
            if current.state is TurnState.COMPLETE:
                return current
            if current.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND",
                    "The assistant turn became ambiguous during generation",
                )
            current = self.inspect_turn(fingerprint)
            self._start().wait_for_timeout(250)
        raise IntegrityError(
            "BROWSER_SPIKE_RESPONSE_TIMEOUT",
            "The spike response did not complete before the configured timeout",
        )

    @staticmethod
    def _spike_checkpoint(checkpoint: str) -> None:
        LOGGER.debug(
            "browser_capture_spike checkpoint=%s",
            checkpoint,
            extra={
                "operation": "browser.capture_spike",
                "status": checkpoint,
            },
        )

    @staticmethod
    def _send_checkpoint(checkpoint: str) -> None:
        LOGGER.debug(
            "browser_send checkpoint=%s",
            checkpoint,
            extra={
                "operation": "browser.send_message",
                "status": checkpoint,
            },
        )

    def _spike_dom_evidence(self) -> dict[str, object]:
        page = self._start()
        composer = self._composer()
        body_text = page.locator("body").inner_text(timeout=min(5000, self.timeout_ms))
        return {
            "alert_count": page.get_by_role("alert").count(),
            "assistant_role_count": page.locator(ASSISTANT_MESSAGE_SELECTOR).count(),
            "composer_count": composer.count(),
            "composer_has_text": self._composer_has_text(),
            "conversation_path": self._path(),
            "operational_error_detected": bool(OPERATIONAL_ERROR_PATTERN.search(body_text)),
            "retry_control_count": page.get_by_role("button", name=RETRY_PATTERN).count(),
            "stop_control_count": page.get_by_role("button", name=STOP_PATTERN).count(),
            "turn_count": page.locator(TURN_SELECTOR).count(),
            "user_role_count": page.locator(USER_MESSAGE_SELECTOR).count(),
        }

    def _first_user_turn(self) -> Any | None:
        users = self._start().locator(USER_MESSAGE_SELECTOR)
        return None if users.count() == 0 else users.first

    def _scan_user_turn_proof(self, requested_fingerprint: str) -> UserTurnProofScan:
        turns = self._start().locator(TURN_SELECTOR)
        candidate_count = 0
        max_text_length = 0
        attachment_found = False
        attachment_opened = False
        max_attachment_content_length = 0
        pending = False
        ambiguous = False
        observed_fingerprints: list[str] = []
        matches: list[tuple[int, int, bool, bool, int, str]] = []
        for index in range(turns.count()):
            message = turns.nth(index)
            if message.get_attribute("data-message-author-role") != "user":
                continue
            candidate_count += 1
            rendered = message.inner_text()
            rendered = rendered if isinstance(rendered, str) else ""
            max_text_length = max(max_text_length, len(rendered))
            rendered_fingerprint = transport_fingerprint(rendered)
            if rendered_fingerprint == requested_fingerprint:
                observed_fingerprints.append(rendered_fingerprint)
                matches.append(
                    (
                        index,
                        len(rendered),
                        False,
                        False,
                        0,
                        rendered_fingerprint,
                    )
                )
                continue
            attachments = message.locator(
                USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR
            )
            raw_attachment_count = attachments.count()
            attachment_count = (
                raw_attachment_count if isinstance(raw_attachment_count, int) else 0
            )
            if attachment_count > 1:
                attachment_found = True
                ambiguous = True
                continue
            candidate_attachment_found = attachment_count == 1
            candidate_attachment_opened = False
            attachment_content_length = 0
            if candidate_attachment_found:
                attachment_found = True
                group = attachments.first
                openers = group.locator(
                    USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR
                )
                raw_opener_count = openers.count()
                opener_count = raw_opener_count if isinstance(raw_opener_count, int) else 0
                if opener_count != 1:
                    pending = opener_count == 0
                    ambiguous = ambiguous or opener_count > 1
                    continue
                opener = openers.first
                group_label = group.get_attribute("aria-label")
                opener_label = opener.get_attribute("aria-label")
                if (
                    not isinstance(group_label, str)
                    or not group_label
                    or not isinstance(opener_label, str)
                    or opener_label != group_label
                ):
                    pending = True
                    continue
                try:
                    observed_text = self._read_user_turn_pasted_text_attachment(
                        opener, requested_fingerprint
                    )
                    candidate_attachment_opened = True
                    attachment_opened = True
                    attachment_content_length = len(observed_text)
                    max_attachment_content_length = max(
                        max_attachment_content_length, attachment_content_length
                    )
                    observed_fingerprint = transport_fingerprint(observed_text)
                except ConflictError as exc:
                    if exc.code != "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE":
                        raise
                    details = exc.context.evidence or {}
                    if "matching_structural_representation_count" in details:
                        candidate_attachment_opened = True
                        attachment_opened = True
                    pending = True
                    continue
            else:
                observed_fingerprint = rendered_fingerprint
            observed_fingerprints.append(observed_fingerprint)
            if observed_fingerprint == requested_fingerprint:
                matches.append(
                    (
                        index,
                        len(rendered),
                        candidate_attachment_found,
                        candidate_attachment_opened,
                        attachment_content_length,
                        observed_fingerprint,
                    )
                )
        if len(matches) == 1:
            (
                _,
                selected_text_length,
                selected_attachment_found,
                selected_attachment_opened,
                selected_attachment_content_length,
                observed_fingerprint,
            ) = matches[0]
            reported_fingerprint: str | None = observed_fingerprint
            max_text_length = selected_text_length
            attachment_found = attachment_found or selected_attachment_found
            attachment_opened = attachment_opened or selected_attachment_opened
            max_attachment_content_length = max(
                max_attachment_content_length, selected_attachment_content_length
            )
        else:
            reported_fingerprint = (
                observed_fingerprints[0]
                if len(observed_fingerprints) == 1
                else None
            )
        return UserTurnProofScan(
            match_indices=[item[0] for item in matches],
            candidate_count=candidate_count,
            text_length=max_text_length,
            attachment_found=attachment_found,
            attachment_opened=attachment_opened,
            attachment_content_length=max_attachment_content_length,
            observed_fingerprint=reported_fingerprint,
            pending=pending,
            ambiguous=ambiguous,
        )

    @staticmethod
    def _log_user_turn_proof(evidence: dict[str, object]) -> None:
        LOGGER.debug(
            "browser_user_turn_proof user_turn_candidate_count=%s "
            "user_turn_text_length=%s pasted_text_attachment_found=%s "
            "pasted_text_attachment_opened=%s pasted_text_content_length=%s "
            "observed_fingerprint=%s expected_fingerprint=%s conversation_path=%s",
            evidence["user_turn_candidate_count"],
            evidence["user_turn_text_length"],
            evidence["pasted_text_attachment_found"],
            evidence["pasted_text_attachment_opened"],
            evidence["pasted_text_content_length"],
            evidence["observed_fingerprint"],
            evidence["expected_fingerprint"],
            evidence["conversation_path"],
            extra={
                "operation": "browser.inspect_user_turn",
                "status": "user_turn_proof_observed",
            },
        )

    @staticmethod
    def _assistant_after_user_turn(user_turn: Any) -> Any | None:
        following = user_turn.locator(
            "xpath=following::*[@data-message-author-role][1]"
        )
        if following.count() != 1:
            return None
        if following.get_attribute("data-message-author-role") != "assistant":
            return None
        return following

    def _reconciliation_user_turn_text(
        self, user_turn: Any, requested_fingerprint: str
    ) -> str:
        rendered = user_turn.inner_text()
        if not isinstance(rendered, str):
            raise ConflictError(
                "BROWSER_RECONCILE_USER_TURN_UNREADABLE",
                "The first user turn exposed no readable rendered text",
            )
        if transport_fingerprint(rendered) == requested_fingerprint:
            return rendered
        attachments = user_turn.locator(COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR)
        if attachments.count() == 0:
            return rendered
        if attachments.count() != 1:
            raise ConflictError(
                "BROWSER_RECONCILE_ATTACHMENT_AMBIGUOUS",
                "The first user turn exposes an ambiguous pasted-text attachment",
            )
        return self._read_pasted_text_attachment(
            attachments.first, requested_fingerprint
        )

    def _read_pasted_text_attachment(
        self, opener: Any, requested_fingerprint: str
    ) -> str:
        page = self._start()
        try:
            opener.click(timeout=self.timeout_ms)
            dialog = page.get_by_role("dialog")
            dialog.wait_for(state="visible", timeout=self.timeout_ms)
            if dialog.count() != 1:
                raise ConflictError(
                    "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                    "The pasted-text attachment did not expose one visible dialog",
                )
            raw_candidates = dialog.evaluate(
                """root => {
                    const values = [];
                    const add = value => {
                        if (typeof value === "string" && value.length > 0) values.push(value);
                    };
                    for (const element of [root, ...root.querySelectorAll("*")]) {
                        if (element instanceof HTMLTextAreaElement ||
                            element instanceof HTMLInputElement) {
                            add(element.value);
                        }
                        add(element.innerText);
                        add(element.textContent);
                    }
                    return values;
                }"""
            )
            if not isinstance(raw_candidates, list):
                raise ConflictError(
                    "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                    "The pasted-text attachment dialog exposed no readable structure",
                )
            matches = {
                value
                for value in raw_candidates
                if isinstance(value, str)
                and transport_fingerprint(value) == requested_fingerprint
            }
            if len(matches) != 1:
                raise ConflictError(
                    "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                    "The pasted-text attachment content could not be proven integrally",
                    evidence={"matching_structural_representation_count": len(matches)},
                )
            return matches.pop()
        except HeliosError:
            raise
        except Exception as exc:
            raise ConflictError(
                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                "The pasted-text attachment could not be opened and read",
            ) from exc
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:
                LOGGER.debug(
                    "browser_reconcile attachment_dialog_close=false",
                    extra={
                        "operation": "browser.reconcile",
                        "status": "attachment_dialog_close_failed",
                    },
                )

    def _read_user_turn_pasted_text_attachment(
        self, opener: Any, requested_fingerprint: str
    ) -> str:
        page = self._start()
        attachment_label = opener.get_attribute("aria-label")
        if not isinstance(attachment_label, str) or not attachment_label:
            raise ConflictError(
                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                "The pasted-text attachment opener exposes no filename label",
            )
        proof_key = (requested_fingerprint, attachment_label)
        state = self._user_turn_attachment_proofs.setdefault(
            proof_key, UserTurnAttachmentProofState()
        )
        if state.completed_text is not None:
            return state.completed_text
        if state.terminal_error_code is not None:
            raise ConflictError(
                state.terminal_error_code,
                state.terminal_error_message or "Pasted-text modal proof failed",
                evidence=state.terminal_evidence,
            )
        deadline = monotonic() + USER_TURN_ATTACHMENT_MODAL_TIMEOUT_MS / 1000
        last_observed_fingerprint: str | None = None
        last_observed_length = 0
        stable_reads = 0
        progressbar_present = False
        if not state.click_issued:
            try:
                opener.click(timeout=self.timeout_ms)
            except Exception as exc:
                state.terminal_error_code = "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE"
                state.terminal_error_message = (
                    "The pasted-text attachment opener click failed"
                )
                state.terminal_evidence = {
                    "attachment_click_issued": False,
                    "modal_seen": False,
                    "modal_poll_count": state.modal_poll_count,
                    "progressbar_present": False,
                    "section_text_length": 0,
                    "stable_reads": 0,
                }
                self._log_user_turn_attachment_modal(
                    state,
                    progressbar_present=False,
                    section_text_length=0,
                    stable_reads=0,
                )
                raise ConflictError(
                    state.terminal_error_code,
                    state.terminal_error_message,
                    evidence=state.terminal_evidence,
                ) from exc
            state.click_issued = True
            self._log_user_turn_attachment_modal(
                state,
                progressbar_present=False,
                section_text_length=0,
                stable_reads=0,
            )
        try:
            while True:
                state.modal_poll_count += 1
                close_controls = page.get_by_role(
                    "button", name=PASTED_TEXT_MODAL_CLOSE_PATTERN
                )
                visible_close_controls = [
                    close_controls.nth(index)
                    for index in range(close_controls.count())
                    if close_controls.nth(index).is_visible()
                ]
                if state.modal_seen and not visible_close_controls:
                    self._fail_user_turn_attachment_proof(
                        state,
                        "BROWSER_USER_TURN_ATTACHMENT_MODAL_DISAPPEARED",
                        "Pasted-text attachment modal disappeared before proof completed",
                        progressbar_present=progressbar_present,
                        section_text_length=last_observed_length,
                        stable_reads=stable_reads,
                    )
                if len(visible_close_controls) > 1:
                    self._fail_user_turn_attachment_proof(
                        state,
                        "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                        "Pasted-text attachment exposed ambiguous modal close controls",
                        progressbar_present=progressbar_present,
                        section_text_length=last_observed_length,
                        stable_reads=stable_reads,
                    )
                representative: str | None = None
                close_control: Any | None = None
                if len(visible_close_controls) == 1:
                    close_control = visible_close_controls[0]
                    containers = close_control.locator(
                        USER_TURN_PASTED_TEXT_MODAL_CONTAINER_XPATH
                    )
                    if containers.count() == 1 and containers.first.is_visible():
                        state.modal_seen = True
                        container = containers.first
                        titles = container.locator(
                            USER_TURN_PASTED_TEXT_MODAL_TITLE_SELECTOR
                        )
                        contents = container.locator(
                            USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR
                        )
                        progressbars = container.locator(
                            USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR
                        )
                        visible_progressbar_count = sum(
                            1
                            for index in range(progressbars.count())
                            if progressbars.nth(index).is_visible()
                        )
                        progressbar_present = visible_progressbar_count > 0
                        if titles.count() == 1:
                            title = titles.first.inner_text()
                            if isinstance(title, str) and title and title != attachment_label:
                                self._fail_user_turn_attachment_proof(
                                    state,
                                    "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                                    "Pasted-text modal title differs from attachment filename",
                                    progressbar_present=progressbar_present,
                                    section_text_length=last_observed_length,
                                    stable_reads=stable_reads,
                                )
                            title_matches = title == attachment_label
                        else:
                            title_matches = False
                        if (
                            title_matches
                            and contents.count() == 1
                            and not progressbar_present
                        ):
                            raw_candidates = contents.first.evaluate(
                                """root => {
                                    const values = [];
                                    const add = value => {
                                        if (typeof value === "string" && value.length > 0) {
                                            values.push(value);
                                        }
                                    };
                                    for (const element of
                                        [root, ...root.querySelectorAll("*")]) {
                                        if (element instanceof HTMLTextAreaElement ||
                                            element instanceof HTMLInputElement) {
                                            add(element.value);
                                        }
                                        add(element.innerText);
                                        add(element.textContent);
                                    }
                                    return values;
                                }"""
                            )
                            candidates_by_normalized: dict[str, str] = {}
                            if isinstance(raw_candidates, list):
                                for value in raw_candidates:
                                    if isinstance(value, str):
                                        normalized = transport_normalize(value)
                                        if normalized:
                                            candidates_by_normalized.setdefault(
                                                normalized, value
                                            )
                            exact_matches = {
                                normalized
                                for normalized in candidates_by_normalized
                                if transport_fingerprint(normalized)
                                == requested_fingerprint
                            }
                            if len(exact_matches) == 1:
                                representative = candidates_by_normalized[
                                    exact_matches.pop()
                                ]
                            elif candidates_by_normalized:
                                representative_normalized = max(
                                    candidates_by_normalized,
                                    key=lambda value: (len(value), value),
                                )
                                representative = candidates_by_normalized[
                                    representative_normalized
                                ]
                if representative is not None:
                    observed_fingerprint = transport_fingerprint(representative)
                    last_observed_length = len(representative)
                    if observed_fingerprint == last_observed_fingerprint:
                        stable_reads += 1
                    else:
                        last_observed_fingerprint = observed_fingerprint
                        stable_reads = 1
                    if stable_reads >= 2:
                        if observed_fingerprint != requested_fingerprint:
                            self._fail_user_turn_attachment_proof(
                                state,
                                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                                "Stable pasted-text modal content fingerprint differs",
                                progressbar_present=progressbar_present,
                                section_text_length=last_observed_length,
                                stable_reads=stable_reads,
                            )
                        if close_control is None:
                            self._fail_user_turn_attachment_proof(
                                state,
                                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                                "Pasted-text modal close control was not reacquired",
                                progressbar_present=progressbar_present,
                                section_text_length=last_observed_length,
                                stable_reads=stable_reads,
                            )
                        try:
                            close_control.click(timeout=self.timeout_ms)
                        except Exception as exc:
                            self._fail_user_turn_attachment_proof(
                                state,
                                "BROWSER_USER_TURN_ATTACHMENT_MODAL_CLOSE_FAILED",
                                "Pasted-text attachment modal could not be closed once",
                                progressbar_present=progressbar_present,
                                section_text_length=last_observed_length,
                                stable_reads=stable_reads,
                                cause=exc,
                            )
                        state.close_issued = True
                        state.completed_text = representative
                        self._log_user_turn_attachment_modal(
                            state,
                            progressbar_present=progressbar_present,
                            section_text_length=last_observed_length,
                            stable_reads=stable_reads,
                        )
                        return representative
                else:
                    last_observed_fingerprint = None
                    last_observed_length = 0
                    stable_reads = 0
                self._log_user_turn_attachment_modal(
                    state,
                    progressbar_present=progressbar_present,
                    section_text_length=last_observed_length,
                    stable_reads=stable_reads,
                )
                if monotonic() >= deadline:
                    break
                page.wait_for_timeout(USER_TURN_ATTACHMENT_MODAL_POLL_INTERVAL_MS)
            if not state.modal_seen:
                self._fail_user_turn_attachment_proof(
                    state,
                    "BROWSER_USER_TURN_ATTACHMENT_MODAL_TIMEOUT",
                    "Pasted-text attachment modal did not appear after one click",
                    progressbar_present=progressbar_present,
                    section_text_length=last_observed_length,
                    stable_reads=stable_reads,
                )
            self._fail_user_turn_attachment_proof(
                state,
                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                "Pasted-text attachment modal content did not become provable",
                progressbar_present=progressbar_present,
                section_text_length=last_observed_length,
                stable_reads=stable_reads,
            )
        except HeliosError:
            raise
        except Exception as exc:
            self._fail_user_turn_attachment_proof(
                state,
                "BROWSER_RECONCILE_ATTACHMENT_INACCESSIBLE",
                "The pasted-text attachment modal could not be read",
                progressbar_present=progressbar_present,
                section_text_length=last_observed_length,
                stable_reads=stable_reads,
                cause=exc,
            )
        raise AssertionError("Unreachable pasted-text attachment proof state")

    @staticmethod
    def _log_user_turn_attachment_modal(
        state: UserTurnAttachmentProofState,
        *,
        progressbar_present: bool,
        section_text_length: int,
        stable_reads: int,
    ) -> None:
        LOGGER.debug(
            "browser_user_turn_attachment_modal attachment_click_issued=%s "
            "modal_seen=%s modal_poll_count=%s progressbar_present=%s "
            "section_text_length=%s stable_reads=%s",
            state.click_issued,
            state.modal_seen,
            state.modal_poll_count,
            progressbar_present,
            section_text_length,
            stable_reads,
            extra={
                "operation": "browser.inspect_user_turn.attachment_modal",
                "status": "observed",
            },
        )

    def _fail_user_turn_attachment_proof(
        self,
        state: UserTurnAttachmentProofState,
        code: str,
        message: str,
        *,
        progressbar_present: bool,
        section_text_length: int,
        stable_reads: int,
        cause: Exception | None = None,
    ) -> Never:
        evidence: dict[str, object] = {
            "attachment_click_issued": state.click_issued,
            "modal_seen": state.modal_seen,
            "modal_poll_count": state.modal_poll_count,
            "progressbar_present": progressbar_present,
            "section_text_length": section_text_length,
            "stable_reads": stable_reads,
        }
        state.terminal_error_code = code
        state.terminal_error_message = message
        state.terminal_evidence = evidence
        self._log_user_turn_attachment_modal(
            state,
            progressbar_present=progressbar_present,
            section_text_length=section_text_length,
            stable_reads=stable_reads,
        )
        error = ConflictError(code, message, evidence=evidence)
        if cause is None:
            raise error
        raise error from cause

    def _first_user_turn_observed_at(self) -> str | None:
        turn = self._first_user_turn()
        if turn is None:
            return None
        times = turn.locator("time[datetime]")
        if times.count() != 1:
            return None
        value = times.first.get_attribute("datetime")
        return value if isinstance(value, str) and value.strip() else None

    def capture_response(self, requested_fingerprint: str) -> str:
        turn = self._assistant_for_fingerprint(requested_fingerprint)
        return self._capture_assistant_response(turn)

    def _capture_assistant_response(
        self,
        turn: Any,
        *,
        expected_assistant_id: str | None = None,
        expected_semantic_sha256: str | None = None,
    ) -> str:
        assistant = self._assistant_content(turn)
        observed_assistant_id = assistant.get_attribute(
            "data-message-id"
        ) or assistant.get_attribute("id")
        if (
            expected_assistant_id is not None
            and observed_assistant_id != expected_assistant_id
        ):
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS",
                "Response capture no longer points to the structurally bound assistant turn",
                evidence={
                    "expected_assistant_id": expected_assistant_id,
                    "observed_assistant_id": observed_assistant_id,
                },
            )
        if self.capture_method_version == "rendered_text_v1":
            return self._capture_rendered_assistant_response(
                assistant,
                expected_semantic_sha256=expected_semantic_sha256,
            )
        if self.capture_method_version == "copy_text_v1":
            button = self._copy_button_for_assistant(assistant)
            if button.count() != 1:
                raise ConflictError(
                    "BROWSER_COPY_CAPTURE_UNAVAILABLE",
                    "Copy capture control is absent or ambiguous",
                )
            value = self._activate_copy_and_read(button)
            if not isinstance(value, str) or not value.strip():
                raise IntegrityError("BROWSER_RESPONSE_EMPTY", "Copied response is empty")
            return str(value)
        raise IntegrityError(
            "BROWSER_CAPTURE_METHOD_UNSUPPORTED", "Unsupported capture method version"
        )

    def _capture_rendered_assistant_response(
        self,
        assistant: Any,
        *,
        expected_semantic_sha256: str | None = None,
    ) -> str:
        scoped = self._scoped_assistant_content_snapshot(
            assistant, evaluate_completion_signals=False
        )
        if scoped.semantic_visible_count != 1:
            raise ConflictError(
                "BROWSER_RESPONSE_CONTENT_CONTAINER_AMBIGUOUS",
                "Assistant turn does not contain exactly one visible model-content container",
                evidence={
                    "assistant_message_id": scoped.assistant_message_id,
                    "content_container_candidate_count": scoped.semantic_container_count,
                    "visible_content_container_candidate_count": (
                        scoped.semantic_visible_count
                    ),
                    "semantic_candidate_hierarchy": list(scoped.candidate_hierarchy),
                },
            )
        text = scoped.response_text
        if not isinstance(text, str) or not text.strip():
            raise IntegrityError("BROWSER_RESPONSE_EMPTY", "Captured response is empty")
        if (
            expected_semantic_sha256 is not None
            and scoped.semantic_sha256 != expected_semantic_sha256
        ):
            raise ConflictError(
                "BROWSER_RESPONSE_CONTENT_CHANGED",
                "The bound assistant content changed after completion was proven",
                evidence={
                    "assistant_message_id": scoped.assistant_message_id,
                    "expected_semantic_sha256": expected_semantic_sha256,
                    "observed_semantic_sha256": scoped.semantic_sha256,
                },
            )
        return text

    def capture_gate_ready(self) -> bool:
        try:
            value = json.loads(self._capture_gate_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict):
            return False
        selected = value.get("selected_method_version")
        samples = value.get("samples")
        return (
            isinstance(selected, str)
            and selected == self.capture_method_version
            and isinstance(samples, dict)
            and {"acknowledgement", "long_unit"}.issubset(samples)
        )

    def close(self) -> None:
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    LOGGER.debug(
                        "Ignoring browser context error during interruption cleanup",
                        exc_info=True,
                    )
                finally:
                    self._context = None
                    self._page = None
        finally:
            try:
                if self._playwright is not None:
                    try:
                        self._playwright.stop()
                    except Exception:
                        LOGGER.debug(
                            "Ignoring Playwright stop error during interruption cleanup",
                            exc_info=True,
                        )
                    finally:
                        self._playwright = None
            finally:
                self._profile_lock.release()

    def _composer(self) -> Any:
        page = self._start()
        by_id = page.locator(COMPOSER_TEST_ID)
        candidates: list[Any] = []
        for index in range(by_id.count()):
            candidate = by_id.nth(index)
            if self._is_visible_editor(candidate):
                candidates.append(candidate)
                continue
            descendants = candidate.locator(COMPOSER_EDITOR_SELECTOR)
            candidates.extend(
                descendants.nth(child_index)
                for child_index in range(descendants.count())
                if self._is_visible_editor(descendants.nth(child_index))
            )
        if not candidates:
            textboxes = page.get_by_role("textbox")
            candidates.extend(
                textboxes.nth(index)
                for index in range(textboxes.count())
                if self._is_visible_editor(textboxes.nth(index))
            )
        if len(candidates) == 1:
            return candidates[0]
        return page.locator("__helios_missing_or_ambiguous_composer__")

    def _focus_composer(
        self, *, timeout_ms: int = COMPOSER_FOCUS_TIMEOUT_MS
    ) -> Any:
        deadline = monotonic() + timeout_ms / 1000
        stable_signature: tuple[str, str | None, str | None, str | None] | None = None
        stable_reads = 0
        focus_established = False
        poll_count = 0
        page = self._start()
        while True:
            poll_count += 1
            candidate = self._focusable_composer()
            metadata: ComposerMetadata | None = None
            try:
                if candidate.count() == 1 and self._is_focusable_editor(candidate):
                    metadata = self._composer_metadata(
                        candidate, timeout_ms=COMPOSER_FOCUS_ACTION_TIMEOUT_MS
                    )
            except Exception:
                metadata = None
            if metadata is None or not self._is_operational_composer_metadata(metadata):
                stable_signature = None
                stable_reads = 0
                focus_established = False
            else:
                signature = (
                    metadata["tag_name"],
                    metadata["id"],
                    metadata["role"],
                    metadata["contenteditable"],
                )
                action_timeout_ms = min(
                    COMPOSER_FOCUS_ACTION_TIMEOUT_MS,
                    max(1, self.timeout_ms),
                )
                try:
                    if not focus_established or signature != stable_signature:
                        candidate.click(timeout=action_timeout_ms)
                        candidate.focus(timeout=action_timeout_ms)
                    document_has_focus, composer_active = self._composer_focus_state(
                        candidate, timeout_ms=action_timeout_ms
                    )
                    focused_metadata = self._audit_composer(
                        candidate,
                        phase="focused",
                        timeout_ms=action_timeout_ms,
                    )
                    if (
                        document_has_focus
                        and composer_active
                        and focused_metadata["focused"]
                        and self._is_operational_composer_metadata(focused_metadata)
                    ):
                        if focus_established and signature == stable_signature:
                            stable_reads += 1
                        else:
                            stable_signature = signature
                            stable_reads = 1
                            focus_established = True
                        if stable_reads >= 2:
                            return candidate
                    else:
                        stable_signature = None
                        stable_reads = 0
                        focus_established = False
                except Exception:
                    stable_signature = None
                    stable_reads = 0
                    focus_established = False
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_COMPOSER_FOCUS_TIMEOUT",
                    "No stable focusable ChatGPT composer appeared before timeout",
                    evidence={
                        "composer_focus_poll_count": poll_count,
                        "composer_focus_stable_reads": stable_reads,
                    },
                )
            page.wait_for_timeout(COMPOSER_FOCUS_POLL_INTERVAL_MS)

    def _focusable_composer(self) -> Any:
        page = self._start()
        preferred = page.locator(
            "div#prompt-textarea[contenteditable='true'], "
            "div#prompt-textarea[contenteditable='plaintext-only'], "
            "div[contenteditable='true'][role='textbox'], "
            "div[contenteditable='plaintext-only'][role='textbox']"
        )
        candidates = [
            preferred.nth(index)
            for index in range(preferred.count())
            if self._is_focusable_editor(preferred.nth(index))
        ]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            return page.locator("__helios_ambiguous_focusable_composer__")
        return page.locator("__helios_missing_or_ambiguous_focusable_composer__")

    @staticmethod
    def _is_operational_composer_metadata(metadata: ComposerMetadata) -> bool:
        return bool(
            metadata["tag_name"] == "div"
            and metadata["role"] == "textbox"
            and metadata["contenteditable"] in {"true", "plaintext-only"}
            and metadata["is_content_editable"]
        )

    def _is_focusable_editor(self, candidate: Any) -> bool:
        try:
            timeout_ms = min(COMPOSER_FOCUS_ACTION_TIMEOUT_MS, max(1, self.timeout_ms))
            return bool(
                candidate.is_visible(timeout=timeout_ms)
                and candidate.is_enabled(timeout=timeout_ms)
                and candidate.is_editable(timeout=timeout_ms)
            )
        except Exception:
            return False

    @staticmethod
    def _composer_focus_state(
        composer: Any, *, timeout_ms: int
    ) -> tuple[bool, bool]:
        observed = composer.evaluate(
            """element => {
                const ownerDocument = element.ownerDocument;
                const activeElement = ownerDocument.activeElement;
                return {
                    document_has_focus: ownerDocument.hasFocus(),
                    composer_active: activeElement === element || element.contains(activeElement),
                };
            }""",
            timeout=timeout_ms,
        )
        if not isinstance(observed, dict):
            return False, False
        return (
            bool(observed.get("document_has_focus")),
            bool(observed.get("composer_active")),
        )

    def _send_button(self) -> Any:
        page = self._start()
        by_id = page.locator(SEND_BUTTON_TEST_ID)
        return by_id if by_id.count() else page.get_by_role("button", name=SEND_PATTERN)

    def _ensure_empty_composer_before_insert(self) -> Any:
        composer_text_length: int | None = None
        pasted_text_attachment_count: int | None = None
        try:
            composer = self._composer()
            if composer.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_AMBIGUOUS",
                    "Expected exactly one live editor before stale-draft inspection",
                )
            composer_text_length = len(self._composer_text(composer))
            pasted_text_attachment_count = self._composer_pasted_text_attachment_count(
                composer
            )
            self._log_composer_draft_observation(
                composer_text_length=composer_text_length,
                pasted_text_attachment_count=pasted_text_attachment_count,
            )
            if composer_text_length == 0 and pasted_text_attachment_count == 0:
                return composer

            self._clear_composer(pasted_text_attachment_baseline=0)
            composer = self._composer()
            if composer.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_AMBIGUOUS",
                    "Expected exactly one live editor after stale-draft cleanup",
                )
            composer_text_length = len(self._composer_text(composer))
            pasted_text_attachment_count = self._composer_pasted_text_attachment_count(
                composer
            )
            self._log_composer_draft_observation(
                composer_text_length=composer_text_length,
                pasted_text_attachment_count=pasted_text_attachment_count,
            )
            if composer_text_length != 0 or pasted_text_attachment_count != 0:
                raise ConflictError(
                    "BROWSER_COMPOSER_STALE_DRAFT_NOT_EMPTY",
                    "The composer remained non-empty after stale-draft cleanup",
                )
            return composer
        except Exception as exc:
            raise ConflictError(
                "BROWSER_COMPOSER_STALE_DRAFT_CLEAR_FAILED",
                "The pre-send composer baseline could not be proven empty",
                evidence={
                    "composer_text_length": composer_text_length,
                    "pasted_text_attachment_count": pasted_text_attachment_count,
                },
            ) from exc

    @staticmethod
    def _log_composer_draft_observation(
        *, composer_text_length: int, pasted_text_attachment_count: int
    ) -> None:
        LOGGER.debug(
            "browser_composer_draft composer_text_length=%s "
            "pasted_text_attachment_count=%s",
            composer_text_length,
            pasted_text_attachment_count,
            extra={"operation": "browser.composer.draft", "status": "observed"},
        )

    def _wait_for_send_button_enabled(
        self, *, timeout_ms: int = SEND_BUTTON_ENABLE_TIMEOUT_MS
    ) -> Any:
        deadline = monotonic() + timeout_ms / 1000
        poll_count = 0
        visible = False
        enabled = False
        stable_enabled_reads = 0
        page = self._start()
        while True:
            poll_count += 1
            send_button = self._send_button()
            visible = False
            enabled = False
            try:
                if send_button.count() == 1:
                    visible = bool(send_button.is_visible())
                    if visible:
                        enabled = bool(send_button.is_enabled())
            except Exception:
                visible = False
                enabled = False

            if visible and enabled:
                stable_enabled_reads += 1
            else:
                stable_enabled_reads = 0
            self._log_send_button_poll(
                poll_count=poll_count,
                visible=visible,
                enabled=enabled,
                stable_enabled_reads=stable_enabled_reads,
            )
            if stable_enabled_reads == 2:
                return send_button
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_SEND_BUTTON_ENABLE_TIMEOUT",
                    "The ChatGPT send button did not become stably enabled before timeout",
                    evidence={
                        "send_button_poll_count": poll_count,
                        "send_button_visible": visible,
                        "send_button_enabled": enabled,
                        "send_button_stable_enabled_reads": stable_enabled_reads,
                    },
                )
            page.wait_for_timeout(SEND_BUTTON_POLL_INTERVAL_MS)

    @staticmethod
    def _log_send_button_poll(
        *,
        poll_count: int,
        visible: bool,
        enabled: bool,
        stable_enabled_reads: int,
    ) -> None:
        LOGGER.debug(
            "browser_send_button_wait send_button_poll_count=%s "
            "send_button_visible=%s send_button_enabled=%s "
            "send_button_stable_enabled_reads=%s",
            poll_count,
            visible,
            enabled,
            stable_enabled_reads,
            extra={"operation": "browser.send_button_wait", "status": "poll"},
        )

    def _composer_has_text(self) -> bool:
        composer = self._composer()
        if composer.count() != 1:
            return False
        try:
            return bool(self._composer_text(composer).strip())
        except Exception:
            return False

    def _probe_composer_payload(self, payload: str) -> ComposerProbeCaseResult:
        if not payload:
            raise IntegrityError(
                "BROWSER_COMPOSER_SPIKE_PAYLOAD_EMPTY",
                "Composer probe payload must not be empty",
            )
        composer = self._composer()
        if composer.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one ChatGPT message composer",
            )
        self._audit_composer(composer, phase="probe_located")
        try:
            composer.click(timeout=self.timeout_ms)
            composer.focus(timeout=self.timeout_ms)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_COMPOSER_FOCUS_FAILED",
                "The composer probe could not focus the resolved editor",
            ) from exc
        metadata = self._audit_composer(composer, phase="probe_focused")
        if not metadata["focused"]:
            raise ConflictError(
                "BROWSER_COMPOSER_FOCUS_FAILED",
                "The composer probe editor did not retain focus",
            )
        pasted_text_attachment_count_before = self._composer_pasted_text_attachment_count(
            composer
        )
        try:
            composer = self._insert_composer_text(composer, payload)
        except HeliosError:
            raise
        except Exception as exc:
            raise ConflictError(
                "BROWSER_COMPOSER_SPIKE_FILL_FAILED",
                "The composer probe payload could not be inserted",
            ) from exc
        expected = transport_fingerprint(payload)
        pasted_text_attachment_count_after = self._composer_pasted_text_attachment_count(
            composer
        )
        attachment_added = (
            pasted_text_attachment_count_after == pasted_text_attachment_count_before + 1
        )
        observed_text = self._composer_text_for_expected(composer, expected)
        observed_length = len(observed_text)
        observed = transport_fingerprint(observed_text)
        if attachment_added:
            read_1 = observed
            read_2 = observed
        else:
            read_1, read_2, observed, observed_length = self._stable_composer_reads(
                composer, expected, reacquire=True
            )
        probe_metadata: dict[str, object] = dict(metadata)
        probe_metadata.update(
            big_paste_detected=attachment_added,
            pasted_text_attachment_count_before=pasted_text_attachment_count_before,
            pasted_text_attachment_count_after=pasted_text_attachment_count_after,
            attachment_added=attachment_added,
            composer_text_length=observed_length,
        )
        LOGGER.debug(
            "browser_composer_spike expected_length=%s observed_length=%s "
            "expected_fingerprint=%s observed_fingerprint=%s stable_read_1=%s "
            "stable_read_2=%s editor_metadata=%s",
            len(payload),
            observed_length,
            expected,
            observed,
            read_1,
            read_2,
            json.dumps(probe_metadata, sort_keys=True),
            extra={"operation": "browser.composer_spike", "status": "verified"},
        )
        return ComposerProbeCaseResult(
            expected_length=len(payload),
            observed_length=observed_length,
            expected_fingerprint=expected,
            observed_fingerprint=observed,
            stable_read_1=read_1,
            stable_read_2=read_2,
            editor_metadata=probe_metadata,
        )

    def _insert_composer_text(self, composer: Any, text: str) -> Any:
        live_editor = self._composer()
        if live_editor.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one live editor before composer insertion",
            )
        if len(text) <= COMPOSER_FILL_MAX_CHARACTERS:
            try:
                live_editor.fill(text, timeout=min(30_000, self.timeout_ms), force=True)
                return live_editor
            except Exception:
                live_editor = self._composer()
                if live_editor.count() == 1:
                    try:
                        observed = self._composer_text(live_editor)
                    except Exception:
                        observed = ""
                    if transport_fingerprint(observed) == transport_fingerprint(text):
                        LOGGER.debug(
                            "browser_composer_insert fill_timeout_but_verified=true",
                            extra={
                                "operation": "browser.composer.insert",
                                "status": "verified",
                            },
                        )
                        return live_editor
            return self._insert_small_composer_text_segmented(live_editor, text)
        LOGGER.debug(
            "browser_composer_insert fallback=native_paste",
            extra={
                "operation": "browser.composer.insert",
                "status": "fallback",
            },
        )
        return self._paste_composer_text(live_editor, text)

    def _insert_small_composer_text_segmented(self, composer: Any, text: str) -> Any:
        """Retain the established short-payload fallback; large payloads use paste."""
        live_editor = self._composer()
        if live_editor.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one live editor before fallback insertion",
            )
        metadata = self._audit_composer(live_editor, phase="fallback_focused")
        if not metadata["focused"]:
            raise ConflictError(
                "BROWSER_COMPOSER_FOCUS_FAILED",
                "Fallback insertion requires the proven focused editor",
            )
        action_timeout = min(5000, self.timeout_ms)
        live_editor.press("Control+A", timeout=action_timeout)
        live_editor.press("Backspace", timeout=action_timeout)
        keyboard = self._start().keyboard
        chunk_size = 4096
        for offset in range(0, len(text), chunk_size):
            chunk_end = min(offset + chunk_size, len(text))
            keyboard.insert_text(text[offset:chunk_end])
            self._wait_for_composer_prefix(text[:chunk_end])
        return live_editor

    def _wait_for_composer_prefix(self, expected_text: str) -> None:
        expected = transport_fingerprint(expected_text)
        deadline = monotonic() + min(30.0, self.timeout_ms / 1000)
        observed = transport_fingerprint("")
        observed_length = 0
        while True:
            composer = self._composer()
            if composer.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_AMBIGUOUS",
                    "Expected exactly one editor during segmented insertion",
                )
            value = self._composer_text_for_expected(composer, expected)
            observed = transport_fingerprint(value)
            observed_length = len(value)
            if observed == expected:
                return
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_COMPOSER_CHUNK_NOT_OBSERVED",
                    "Segmented composer insertion did not preserve the complete prefix",
                    evidence={
                        "expected_fingerprint": expected,
                        "observed_fingerprint": observed,
                        "expected_character_count": len(expected_text),
                        "observed_character_count": observed_length,
                    },
                )
            self._start().wait_for_timeout(100)

    def _paste_composer_text(self, composer: Any, text: str) -> Any:
        """Paste a large request through the native clipboard without sending it."""
        expected_sha = sha256_bytes(text.encode("utf-8", errors="strict"))
        expected_fingerprint = transport_fingerprint(text)
        previous_clipboard: str | None = None
        clipboard_populated = False
        try:
            previous_clipboard = self._windows_clipboard_read()
            self._windows_clipboard_write(text)
            clipboard_populated = True
            clipboard_observed = self._windows_clipboard_read()
            observed_sha = sha256_bytes(clipboard_observed.encode("utf-8", errors="strict"))
            self._log_native_paste(
                clipboard_expected_length=len(text),
                clipboard_expected_sha=expected_sha,
                clipboard_observed_length=len(clipboard_observed),
                clipboard_observed_sha=observed_sha,
            )
            if len(clipboard_observed) != len(text) or observed_sha != expected_sha:
                raise ConflictError(
                    "BROWSER_COMPOSER_CLIPBOARD_MISMATCH",
                    "The Windows clipboard did not retain the complete request before paste",
                    evidence={
                        "clipboard_expected_length": len(text),
                        "clipboard_expected_sha": expected_sha,
                        "clipboard_observed_length": len(clipboard_observed),
                        "clipboard_observed_sha": observed_sha,
                    },
                )

            dedicated_hwnd = self._ensure_windows_native_paste_foreground()
            live_editor = self._focused_composer_for_native_paste(dedicated_hwnd)
            pasted_text_attachment_count_before = (
                self._composer_pasted_text_attachment_count(live_editor)
            )
            self._log_native_paste(
                clipboard_expected_length=len(text),
                clipboard_expected_sha=expected_sha,
                clipboard_observed_length=len(clipboard_observed),
                clipboard_observed_sha=observed_sha,
                editor_focused_before_paste=True,
                paste_key_started=True,
            )
            try:
                self._windows_ctrl_v()
            except Exception as exc:
                raise ConflictError(
                    "BROWSER_COMPOSER_NATIVE_PASTE_FAILED",
                    "Windows native Ctrl+V could not be emitted into the focused composer",
                ) from exc
            self._log_native_paste(
                clipboard_expected_length=len(text),
                clipboard_expected_sha=expected_sha,
                clipboard_observed_length=len(clipboard_observed),
                clipboard_observed_sha=observed_sha,
                editor_focused_before_paste=True,
                paste_key_completed=True,
            )
            live_editor = self._wait_for_pasted_composer(
                text,
                expected_fingerprint,
                pasted_text_attachment_count_before=pasted_text_attachment_count_before,
            )
            live_editor = self._composer()
            if live_editor.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_AMBIGUOUS",
                    "Expected exactly one live editor after native paste",
                )
            metadata_after = self._audit_composer(live_editor, phase="paste_verified")
            self._log_native_paste(
                clipboard_expected_length=len(text),
                clipboard_expected_sha=expected_sha,
                clipboard_observed_length=len(clipboard_observed),
                clipboard_observed_sha=observed_sha,
                editor_focused_after_paste=metadata_after["focused"],
            )
            if not metadata_after["focused"]:
                raise ConflictError(
                    "BROWSER_COMPOSER_FOCUS_FAILED",
                    "The ChatGPT composer lost focus during native paste",
                )
            return live_editor
        except Exception as exc:
            if isinstance(exc, HeliosError):
                raise
            raise ConflictError(
                "BROWSER_COMPOSER_PASTE_FAILED",
                "The complete request could not be pasted into the ChatGPT composer",
            ) from exc
        finally:
            if clipboard_populated:
                try:
                    self._windows_clipboard_write(
                        previous_clipboard if previous_clipboard is not None else ""
                    )
                except Exception:
                    LOGGER.debug(
                        "browser_composer_insert clipboard_restore_failed=true",
                        extra={
                            "operation": "browser.composer.insert",
                            "status": "clipboard_restore_failed",
                        },
                    )

    def _focused_composer_for_native_paste(self, dedicated_hwnd: int) -> Any:
        live_editor = self._composer()
        if live_editor.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one live editor before native paste",
            )
        document_has_focus_before_click, composer_active_before_click = (
            self._native_paste_editor_focus_state(live_editor)
        )
        try:
            screen_x, screen_y = self._native_paste_composer_screen_point(
                live_editor, dedicated_hwnd
            )
            self._windows_click_left(screen_x, screen_y)
            self._start().wait_for_timeout(100)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_EDITOR_CLICK_FAILED",
                "The ChatGPT composer could not receive the required native Windows click",
            ) from exc
        live_editor = self._composer()
        if live_editor.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one live editor after native paste click",
            )
        document_has_focus_after_click, composer_active_after_click = (
            self._native_paste_editor_focus_state(live_editor)
        )
        self._log_native_paste_editor_focus(
            document_has_focus_before_click=document_has_focus_before_click,
            composer_active_before_click=composer_active_before_click,
            document_has_focus_after_click=document_has_focus_after_click,
            composer_active_after_click=composer_active_after_click,
            foreground_verified=True,
        )
        if not document_has_focus_after_click or not composer_active_after_click:
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_EDITOR_FOCUS_FAILED",
                "The ChatGPT composer did not receive native keyboard focus after click",
                evidence={
                    "document_has_focus_before_click": document_has_focus_before_click,
                    "composer_active_before_click": composer_active_before_click,
                    "document_has_focus_after_click": document_has_focus_after_click,
                    "composer_active_after_click": composer_active_after_click,
                    "foreground_verified": True,
                },
            )
        metadata = self._audit_composer(live_editor, phase="paste_focused")
        if not metadata["focused"]:
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_EDITOR_FOCUS_FAILED",
                "Native paste requires the proven focused editor",
            )
        return live_editor

    def _native_paste_composer_screen_point(
        self, composer: Any, dedicated_hwnd: int
    ) -> tuple[int, int]:
        box = composer.bounding_box(timeout=self.timeout_ms)
        if not isinstance(box, dict):
            raise ValueError("The composer has no visible bounding box")
        metrics = self._start().evaluate(
            """() => ({
                outer_width: window.outerWidth,
                outer_height: window.outerHeight,
                inner_width: window.innerWidth,
                inner_height: window.innerHeight,
            })"""
        )
        if not isinstance(metrics, dict):
            raise ValueError("The browser viewport metrics are unavailable")
        return self._composer_center_screen_point(
            box=box,
            window_rect=self._windows_window_rect(dedicated_hwnd),
            viewport_metrics=metrics,
        )

    @staticmethod
    def _composer_center_screen_point(
        *,
        box: Mapping[str, object],
        window_rect: tuple[int, int, int, int],
        viewport_metrics: Mapping[str, object],
    ) -> tuple[int, int]:
        def finite_positive(source: Mapping[str, object], key: str) -> float:
            value = source.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{key} must be numeric")
            numeric = float(value)
            if not math.isfinite(numeric) or numeric <= 0:
                raise ValueError(f"{key} must be finite and positive")
            return numeric

        def finite(source: Mapping[str, object], key: str) -> float:
            value = source.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{key} must be numeric")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"{key} must be finite")
            return numeric

        box_x = finite(box, "x")
        box_y = finite(box, "y")
        box_width = finite_positive(box, "width")
        box_height = finite_positive(box, "height")
        outer_width = finite_positive(viewport_metrics, "outer_width")
        outer_height = finite_positive(viewport_metrics, "outer_height")
        inner_width = finite_positive(viewport_metrics, "inner_width")
        inner_height = finite_positive(viewport_metrics, "inner_height")
        if inner_width > outer_width or inner_height > outer_height:
            raise ValueError("The viewport cannot exceed the dedicated window")

        left, top, right, bottom = window_rect
        window_width = right - left
        window_height = bottom - top
        if window_width <= 0 or window_height <= 0:
            raise ValueError("The dedicated window rectangle is invalid")

        composer_x = box_x + (box_width / 2)
        composer_y = box_y + (box_height / 2)
        if not 0 <= composer_x < inner_width or not 0 <= composer_y < inner_height:
            raise ValueError("The composer center is outside the renderer viewport")

        horizontal_frame = max((outer_width - inner_width) / 2, 0.0)
        renderer_top = max(outer_height - inner_height - horizontal_frame, 0.0)
        scale_x = window_width / outer_width
        scale_y = window_height / outer_height
        screen_x = round(left + ((horizontal_frame + composer_x) * scale_x))
        screen_y = round(top + ((renderer_top + composer_y) * scale_y))
        if not left <= screen_x < right or not top <= screen_y < bottom:
            raise ValueError("The composer center is outside the dedicated window")
        return screen_x, screen_y

    @staticmethod
    def _native_paste_editor_focus_state(composer: Any) -> tuple[bool, bool]:
        observed = composer.evaluate(
            """element => {
                const ownerDocument = element.ownerDocument;
                const activeElement = ownerDocument.activeElement;
                return {
                    document_has_focus: ownerDocument.hasFocus(),
                    composer_active: activeElement === element || element.contains(activeElement),
                };
            }"""
        )
        if not isinstance(observed, dict):
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_EDITOR_FOCUS_FAILED",
                "The ChatGPT composer focus state could not be inspected",
            )
        return (
            bool(observed.get("document_has_focus")),
            bool(observed.get("composer_active")),
        )

    def _windows_ctrl_v(self) -> None:
        control_pressed = False
        try:
            self._windows_send_virtual_key(VK_CONTROL, key_up=False)
            control_pressed = True
            self._windows_send_virtual_key(VK_V, key_up=False)
            self._windows_send_virtual_key(VK_V, key_up=True)
        finally:
            if control_pressed:
                self._windows_send_virtual_key(VK_CONTROL, key_up=True)

    def _ensure_windows_native_paste_foreground(self) -> int:
        hwnd_total_count = 0
        profile_matched_hwnd_count = 0
        expected_hwnd = 0
        foreground_before = 0
        foreground_after = 0
        try:
            windows = self._windows_visible_chrome_windows()
            hwnd_total_count = len(windows)
            candidates = tuple(
                hwnd
                for hwnd, process_id in windows
                if self._windows_process_uses_browser_profile(process_id)
            )
            profile_matched_hwnd_count = len(candidates)
            if not candidates:
                raise ConflictError(
                    "BROWSER_NATIVE_PASTE_WINDOW_NOT_FOUND",
                    "No visible Chrome window matches the dedicated browser profile",
                    evidence={
                        "hwnd_total_count": hwnd_total_count,
                        "profile_matched_hwnd_count": profile_matched_hwnd_count,
                    },
                )
            if len(candidates) != 1:
                raise ConflictError(
                    "BROWSER_NATIVE_PASTE_WINDOW_AMBIGUOUS",
                    "More than one visible Chrome window matches the dedicated browser profile",
                    evidence={
                        "hwnd_total_count": hwnd_total_count,
                        "profile_matched_hwnd_count": profile_matched_hwnd_count,
                    },
                )
            expected_hwnd = candidates[0]
            foreground_before = self._windows_foreground_window()
            if foreground_before != expected_hwnd:
                self._windows_set_foreground_window(expected_hwnd)
            foreground_after = self._windows_foreground_window()
        except HeliosError:
            self._log_native_paste_window(
                hwnd_total_count=hwnd_total_count,
                profile_matched_hwnd_count=profile_matched_hwnd_count,
                expected_hwnd=expected_hwnd,
                foreground_hwnd_before=foreground_before,
                foreground_hwnd_after=foreground_after,
                foreground_verified=False,
            )
            raise
        except Exception as exc:
            self._log_native_paste_window(
                hwnd_total_count=hwnd_total_count,
                profile_matched_hwnd_count=profile_matched_hwnd_count,
                expected_hwnd=expected_hwnd,
                foreground_hwnd_before=foreground_before,
                foreground_hwnd_after=foreground_after,
                foreground_verified=False,
            )
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_WINDOW_FOCUS_FAILED",
                "The Playwright Chrome window could not be activated before native paste",
            ) from exc
        verified = foreground_after == expected_hwnd
        self._log_native_paste_window(
            hwnd_total_count=hwnd_total_count,
            profile_matched_hwnd_count=profile_matched_hwnd_count,
            expected_hwnd=expected_hwnd,
            foreground_hwnd_before=foreground_before,
            foreground_hwnd_after=foreground_after,
            foreground_verified=verified,
        )
        if not verified:
            raise ConflictError(
                "BROWSER_NATIVE_PASTE_WINDOW_FOCUS_FAILED",
                "The Playwright Chrome window could not be proven foreground before native paste",
                evidence={
                    "expected_hwnd": expected_hwnd,
                    "foreground_hwnd_before": foreground_before,
                    "foreground_hwnd_after": foreground_after,
                },
            )
        return expected_hwnd

    @staticmethod
    def _windows_visible_chrome_windows() -> tuple[tuple[int, int], ...]:
        if sys.platform != "win32":
            raise OSError("Windows window enumeration is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        windows: list[tuple[int, int]] = []

        def collect(hwnd: wintypes.HWND, _lparam: int) -> bool:
            value = ctypes.cast(hwnd, ctypes.c_void_p).value
            class_name = ctypes.create_unicode_buffer(256)
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if (
                user32.IsWindowVisible(hwnd)
                and value is not None
                and user32.GetClassNameW(hwnd, class_name, len(class_name))
                and class_name.value == "Chrome_WidgetWin_1"
            ):
                windows.append((int(value), int(process_id.value)))
            return True

        callback = callback_type(collect)
        if not user32.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return tuple(windows)

    def _windows_process_uses_browser_profile(self, process_id: int) -> bool:
        command_line = self._windows_process_command_line(process_id)
        expected = self._windows_normalize_profile_path(str(self.profile_dir))
        return any(
            self._windows_normalize_profile_path(argument) == expected
            for argument in self._windows_user_data_dir_arguments(command_line)
        )

    @staticmethod
    def _windows_process_command_line(process_id: int) -> str:
        if sys.platform != "win32":
            raise OSError("Windows process command line inspection is unavailable on this platform")
        command = (
            f"(Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = {process_id}')"
            ".CommandLine"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return completed.stdout.strip()

    @staticmethod
    def _windows_user_data_dir_arguments(command_line: str) -> tuple[str, ...]:
        return tuple(
            next(value for value in match.groups() if value is not None)
            for match in USER_DATA_DIR_ARGUMENT.finditer(command_line)
        )

    @staticmethod
    def _windows_normalize_profile_path(value: str) -> str:
        return os.path.normcase(os.path.normpath(value.strip().strip("\"'")))

    @staticmethod
    def _windows_foreground_window() -> int:
        if sys.platform != "win32":
            raise OSError("Windows foreground inspection is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        handle = user32.GetForegroundWindow()
        value = ctypes.cast(handle, ctypes.c_void_p).value
        return int(value) if value is not None else 0

    @staticmethod
    def _windows_set_foreground_window(hwnd: int) -> None:
        if sys.platform != "win32":
            raise OSError("Windows foreground activation is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        if not user32.SetForegroundWindow(wintypes.HWND(hwnd)):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _windows_window_rect(hwnd: int) -> tuple[int, int, int, int]:
        if sys.platform != "win32":
            raise OSError("Windows window geometry is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def _windows_click_left(self, screen_x: int, screen_y: int) -> None:
        self._windows_set_cursor_position(screen_x, screen_y)
        mouse_down = False
        try:
            self._windows_send_mouse_button(key_up=False)
            mouse_down = True
            self._windows_send_mouse_button(key_up=True)
            mouse_down = False
        finally:
            if mouse_down:
                self._windows_send_mouse_button(key_up=True)

    @staticmethod
    def _windows_set_cursor_position(screen_x: int, screen_y: int) -> None:
        if sys.platform != "win32":
            raise OSError("Windows cursor positioning is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        if not user32.SetCursorPos(screen_x, screen_y):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _windows_send_mouse_button(*, key_up: bool) -> None:
        if sys.platform != "win32":
            raise OSError("Windows input is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        event = _Input(
            type=INPUT_MOUSE,
            payload=_InputUnion(
                mouse=_MouseInput(
                    dx=0,
                    dy=0,
                    mouse_data=0,
                    flags=MOUSEEVENTF_LEFTUP if key_up else MOUSEEVENTF_LEFTDOWN,
                    time=0,
                    extra_info=0,
                )
            ),
        )
        if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _windows_send_virtual_key(virtual_key: int, *, key_up: bool) -> None:
        if sys.platform != "win32":
            raise OSError("Windows input is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        event = _Input(
            type=INPUT_KEYBOARD,
            payload=_InputUnion(
                keyboard=_KeyboardInput(
                    virtual_key=virtual_key,
                    scan=0,
                    flags=KEYEVENTF_KEYUP if key_up else 0,
                    time=0,
                    extra_info=0,
                )
            ),
        )
        if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _log_native_paste(
        *,
        clipboard_expected_length: int,
        clipboard_expected_sha: str,
        clipboard_observed_length: int,
        clipboard_observed_sha: str,
        editor_focused_before_paste: bool | None = None,
        paste_key_started: bool | None = None,
        paste_key_completed: bool | None = None,
        editor_focused_after_paste: bool | None = None,
    ) -> None:
        LOGGER.debug(
            "browser_native_paste clipboard_expected_length=%s clipboard_expected_sha=%s "
            "clipboard_observed_length=%s clipboard_observed_sha=%s "
            "editor_focused_before_paste=%s paste_key_started=%s "
            "paste_key_completed=%s editor_focused_after_paste=%s",
            clipboard_expected_length,
            clipboard_expected_sha,
            clipboard_observed_length,
            clipboard_observed_sha,
            editor_focused_before_paste,
            paste_key_started,
            paste_key_completed,
            editor_focused_after_paste,
            extra={"operation": "browser.composer.native_paste", "status": "observed"},
        )

    @staticmethod
    def _log_native_paste_editor_focus(
        *,
        document_has_focus_before_click: bool,
        composer_active_before_click: bool,
        document_has_focus_after_click: bool,
        composer_active_after_click: bool,
        foreground_verified: bool,
    ) -> None:
        LOGGER.debug(
            "browser_native_paste_editor_focus document_has_focus_before_click=%s "
            "composer_active_before_click=%s document_has_focus_after_click=%s "
            "composer_active_after_click=%s foreground_verified=%s",
            document_has_focus_before_click,
            composer_active_before_click,
            document_has_focus_after_click,
            composer_active_after_click,
            foreground_verified,
            extra={"operation": "browser.composer.native_paste", "status": "editor_focus"},
        )

    @staticmethod
    def _log_native_paste_window(
        *,
        hwnd_total_count: int,
        profile_matched_hwnd_count: int,
        expected_hwnd: int,
        foreground_hwnd_before: int,
        foreground_hwnd_after: int,
        foreground_verified: bool,
    ) -> None:
        LOGGER.debug(
            "browser_native_paste_window hwnd_total_count=%s "
            "profile_matched_hwnd_count=%s expected_hwnd=%s foreground_hwnd_before=%s "
            "foreground_hwnd_after=%s foreground_verified=%s",
            hwnd_total_count,
            profile_matched_hwnd_count,
            expected_hwnd,
            foreground_hwnd_before,
            foreground_hwnd_after,
            foreground_verified,
            extra={"operation": "browser.composer.native_paste", "status": "window_focus"},
        )

    @staticmethod
    def _windows_clipboard_read() -> str:
        if sys.platform != "win32":
            raise OSError("Windows clipboard is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        if not user32.OpenClipboard(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            handle = user32.GetClipboardData(13)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    @staticmethod
    def _windows_clipboard_write(value: str) -> None:
        if sys.platform != "win32":
            raise OSError("Windows clipboard is unavailable on this platform")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        encoded = (value + "\0").encode("utf-16-le", errors="strict")
        handle = kernel32.GlobalAlloc(0x0002, len(encoded))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.OpenClipboard(None):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not user32.EmptyClipboard():
                    raise ctypes.WinError(ctypes.get_last_error())
                if not user32.SetClipboardData(13, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                handle = wintypes.HGLOBAL()
            finally:
                user32.CloseClipboard()
        finally:
            if handle:
                kernel32.GlobalFree(handle)

    def _clear_composer(self, *, pasted_text_attachment_baseline: int = 0) -> None:
        composer = self._composer()
        if composer.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_AMBIGUOUS",
                "Expected exactly one ChatGPT message composer while clearing draft",
            )
        self._audit_composer(composer, phase="probe_clear")
        deadline = monotonic() + self.timeout_ms / 1000
        while True:
            attachment_count = self._composer_pasted_text_attachment_count(composer)
            if attachment_count == pasted_text_attachment_baseline:
                break
            if attachment_count < pasted_text_attachment_baseline or monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                    "The composer probe could not restore the pasted-text attachment baseline",
                    evidence={
                        "pasted_text_attachment_baseline": pasted_text_attachment_baseline,
                        "pasted_text_attachment_count": attachment_count,
                    },
                )
            self._remove_one_pasted_text_attachment(composer)
            self._start().wait_for_timeout(COMPOSER_PASTE_POLL_INTERVAL_MS)
            composer = self._composer()
            if composer.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                    "The composer probe could not reacquire the editor after attachment removal",
                )
        try:
            composer.click(timeout=self.timeout_ms)
            composer.focus(timeout=self.timeout_ms)
            composer.press("Control+A", timeout=self.timeout_ms)
            composer.press("Backspace", timeout=self.timeout_ms)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                "The composer probe could not clear the editor",
            ) from exc
        empty = transport_fingerprint("")
        try:
            self._stable_composer_reads(composer, empty, reacquire=True)
        except ConflictError as exc:
            raise ConflictError(
                "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                "The composer probe could not prove the editor was empty",
                evidence=exc.context.evidence,
            ) from exc
        restored_count = self._composer_pasted_text_attachment_count(self._composer())
        if restored_count != pasted_text_attachment_baseline:
            raise ConflictError(
                "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                "The composer probe did not restore the pasted-text attachment baseline",
                evidence={
                    "pasted_text_attachment_baseline": pasted_text_attachment_baseline,
                    "pasted_text_attachment_count": restored_count,
                },
            )

    def _stable_composer_reads(
        self,
        composer: Any,
        expected: str,
        *,
        reacquire: bool = False,
        timeout_ms: int | None = None,
    ) -> tuple[str, str, str, int]:
        effective_timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        deadline = monotonic() + effective_timeout_ms / 1000
        stable_read_1: str | None = None
        observed = transport_fingerprint("")
        observed_length = 0
        while True:
            current = self._composer() if reacquire else composer
            if current.count() != 1:
                raise ConflictError(
                    "BROWSER_COMPOSER_AMBIGUOUS",
                    "Expected exactly one editor while verifying composer content",
                )
            value = self._composer_text_for_expected(current, expected)
            observed = transport_fingerprint(value)
            observed_length = len(value)
            if observed == expected:
                if stable_read_1 is not None:
                    return stable_read_1, observed, observed, observed_length
                stable_read_1 = observed
            else:
                stable_read_1 = None
            if monotonic() >= deadline and stable_read_1 is None:
                LOGGER.debug(
                    "browser_composer_verification_failed expected_fingerprint=%s "
                    "observed_fingerprint=%s observed_length=%s",
                    expected,
                    observed,
                    observed_length,
                    extra={
                        "operation": "browser.composer.verify",
                        "status": "failed",
                    },
                )
                raise ConflictError(
                    "BROWSER_COMPOSER_FILL_FAILED",
                    "The observed editor content did not match the complete request fingerprint",
                    evidence={
                        "expected_fingerprint": expected,
                        "observed_fingerprint": observed,
                        "observed_character_count": observed_length,
                    },
                )
            self._start().wait_for_timeout(100)

    def _wait_for_normal_composer_insertion(
        self,
        composer: Any,
        *,
        pasted_text_attachment_count_before: int,
        timeout_ms: int = COMPOSER_INSERT_VERIFY_TIMEOUT_MS,
    ) -> None:
        del composer  # Verification always reacquires the current hydrated editor.
        deadline = monotonic() + timeout_ms / 1000
        observed_length = 0
        stable_reads = 0
        composer_nonempty = False
        pasted_text_attachment_count_after = pasted_text_attachment_count_before
        page = self._start()
        while True:
            try:
                current = self._focusable_composer()
                if current.count() != 1 or not self._is_focusable_editor(current):
                    raise ConflictError(
                        "BROWSER_COMPOSER_AMBIGUOUS",
                        "Expected exactly one editor while verifying inserted content",
                    )
                value = self._composer_text(
                    current,
                    timeout_ms=COMPOSER_INSERT_VERIFY_READ_TIMEOUT_MS,
                )
                observed_length = len(value)
                composer_nonempty = bool(transport_normalize(value))
                pasted_text_attachment_count_after = (
                    self._composer_pasted_text_attachment_count(current)
                )
                if (
                    pasted_text_attachment_count_after
                    != pasted_text_attachment_count_before
                ):
                    raise ConflictError(
                        "BROWSER_COMPOSER_INSERT_UNEXPECTED_ATTACHMENT",
                        "Normal composer insertion created an unexpected attachment",
                        evidence={
                            "pasted_text_attachment_count_before": (
                                pasted_text_attachment_count_before
                            ),
                            "pasted_text_attachment_count_after": (
                                pasted_text_attachment_count_after
                            ),
                        },
                    )
                if composer_nonempty:
                    stable_reads += 1
                else:
                    stable_reads = 0
            except ConflictError as exc:
                if exc.code == "BROWSER_COMPOSER_INSERT_UNEXPECTED_ATTACHMENT":
                    raise
                composer_nonempty = False
                stable_reads = 0
            except Exception:
                composer_nonempty = False
                stable_reads = 0
            self._log_composer_insert_verification(
                observed_length=observed_length,
                stable_reads=stable_reads,
                composer_nonempty=composer_nonempty,
                pasted_text_attachment_count_before=(
                    pasted_text_attachment_count_before
                ),
                pasted_text_attachment_count_after=pasted_text_attachment_count_after,
            )
            if composer_nonempty and stable_reads >= 2:
                return
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_COMPOSER_INSERT_VERIFY_TIMEOUT",
                    "Inserted composer text did not become stably non-empty before timeout",
                    evidence={
                        "observed_length": observed_length,
                        "stable_reads": stable_reads,
                        "composer_nonempty": composer_nonempty,
                        "pasted_text_attachment_count_before": (
                            pasted_text_attachment_count_before
                        ),
                        "pasted_text_attachment_count_after": (
                            pasted_text_attachment_count_after
                        ),
                    },
                )
            page.wait_for_timeout(COMPOSER_INSERT_VERIFY_POLL_INTERVAL_MS)

    @staticmethod
    def _log_composer_insert_verification(
        *,
        observed_length: int,
        stable_reads: int,
        composer_nonempty: bool,
        pasted_text_attachment_count_before: int,
        pasted_text_attachment_count_after: int,
    ) -> None:
        LOGGER.debug(
            "browser_composer_insert_verify observed_length=%s stable_reads=%s "
            "composer_nonempty=%s pasted_text_attachment_count_before=%s "
            "pasted_text_attachment_count_after=%s",
            observed_length,
            stable_reads,
            composer_nonempty,
            pasted_text_attachment_count_before,
            pasted_text_attachment_count_after,
            extra={
                "operation": "browser.composer.insert_verify",
                "status": "poll",
            },
        )

    def _wait_for_pasted_composer(
        self,
        expected_text: str,
        _expected_fingerprint: str,
        *,
        pasted_text_attachment_count_before: int = 0,
        timeout_ms: int = COMPOSER_PASTE_VERIFICATION_TIMEOUT_MS,
    ) -> Any:
        deadline = monotonic() + timeout_ms / 1000
        last_observed_length = 0
        poll_count = 0
        consecutive_nonempty_reads = 0
        pasted_text_attachment_count_after = pasted_text_attachment_count_before
        self._start().wait_for_timeout(COMPOSER_PASTE_POLL_INTERVAL_MS)
        while True:
            poll_count += 1
            try:
                current = self._composer()
                if current.count() == 1:
                    observed_text = self._composer_text(current)
                    last_observed_length = len(observed_text)
                    pasted_text_attachment_count_after = (
                        self._composer_pasted_text_attachment_count(current)
                    )
                    attachment_added = (
                        pasted_text_attachment_count_after
                        == pasted_text_attachment_count_before + 1
                    )
                    if attachment_added:
                        self._log_big_paste_observation(
                            big_paste_detected=True,
                            pasted_text_attachment_count_before=(
                                pasted_text_attachment_count_before
                            ),
                            pasted_text_attachment_count_after=(
                                pasted_text_attachment_count_after
                            ),
                            attachment_added=True,
                            composer_text_length=last_observed_length,
                        )
                        return current
                    if (
                        pasted_text_attachment_count_after
                        != pasted_text_attachment_count_before
                    ):
                        raise ConflictError(
                            "BROWSER_COMPOSER_PASTE_ATTACHMENT_DELTA_INVALID",
                            "Native paste created an invalid attachment delta",
                            evidence={
                                "pasted_text_attachment_count_before": (
                                    pasted_text_attachment_count_before
                                ),
                                "pasted_text_attachment_count_after": (
                                    pasted_text_attachment_count_after
                                ),
                            },
                        )
                    if transport_normalize(observed_text):
                        consecutive_nonempty_reads += 1
                    else:
                        consecutive_nonempty_reads = 0
                    if consecutive_nonempty_reads >= 2:
                        self._log_big_paste_observation(
                            big_paste_detected=False,
                            pasted_text_attachment_count_before=(
                                pasted_text_attachment_count_before
                            ),
                            pasted_text_attachment_count_after=(
                                pasted_text_attachment_count_after
                            ),
                            attachment_added=False,
                            composer_text_length=last_observed_length,
                        )
                        return current
                else:
                    consecutive_nonempty_reads = 0
            except ConflictError as exc:
                if exc.code == "BROWSER_COMPOSER_PASTE_ATTACHMENT_DELTA_INVALID":
                    raise
                consecutive_nonempty_reads = 0
            except Exception:
                consecutive_nonempty_reads = 0

            if monotonic() >= deadline and consecutive_nonempty_reads < 2:
                self._log_big_paste_observation(
                    big_paste_detected=(
                        pasted_text_attachment_count_after
                        == pasted_text_attachment_count_before + 1
                    ),
                    pasted_text_attachment_count_before=pasted_text_attachment_count_before,
                    pasted_text_attachment_count_after=pasted_text_attachment_count_after,
                    attachment_added=(
                        pasted_text_attachment_count_after
                        == pasted_text_attachment_count_before + 1
                    ),
                    composer_text_length=last_observed_length,
                )
                LOGGER.debug(
                    "browser_composer_paste_verification_timeout expected_length=%s "
                    "last_observed_length=%s poll_count=%s",
                    len(expected_text),
                    last_observed_length,
                    poll_count,
                    extra={
                        "operation": "browser.composer.native_paste",
                        "status": "verification_timeout",
                    },
                )
                raise ConflictError(
                    "BROWSER_COMPOSER_PASTE_VERIFICATION_TIMEOUT",
                    "The pasted request produced neither stable non-empty text nor "
                    "one new attachment",
                    evidence={
                        "expected_length": len(expected_text),
                        "last_observed_length": last_observed_length,
                        "stable_nonempty_reads": consecutive_nonempty_reads,
                        "pasted_text_attachment_count_before": (
                            pasted_text_attachment_count_before
                        ),
                        "pasted_text_attachment_count_after": (
                            pasted_text_attachment_count_after
                        ),
                        "poll_count": poll_count,
                    },
                )
            self._start().wait_for_timeout(COMPOSER_PASTE_POLL_INTERVAL_MS)

    def _capture_pre_send_user_turn_count(self) -> int:
        timeout_ms = min(PRE_SEND_USER_TURN_BASELINE_TIMEOUT_MS, self.timeout_ms)
        deadline = monotonic() + timeout_ms / 1000
        last_count: int | None = None
        last_assistant_count: int | None = None
        last_signature: tuple[object, ...] | None = None
        stable_reads = 0
        poll_count = 0
        conversation_root_found = False
        conversation_path: str | None = None
        last_failure_reason = "conversation_root_not_hydrated"
        self._pending_pre_send_turn_anchor = None
        while True:
            poll_count += 1
            try:
                page = self._start()
                conversation_path = conversation_path_from_page_url(str(page.url))
                roots = page.locator(CONVERSATION_ROOT_SELECTOR)
                root_count = roots.count()
                conversation_root_found = isinstance(root_count, int) and root_count == 1
                if conversation_root_found:
                    root = roots.first
                    raw_count = root.locator(USER_MESSAGE_SELECTOR).count()
                    raw_assistant_count = root.locator(
                        ASSISTANT_MESSAGE_SELECTOR
                    ).count()
                    turns = root.locator(TURN_SELECTOR)
                    turn_count = turns.count()
                    last_count = raw_count if isinstance(raw_count, int) else None
                    last_assistant_count = (
                        raw_assistant_count
                        if isinstance(raw_assistant_count, int)
                        else None
                    )
                    tail: list[dict[str, str]] | None = None
                    if last_count == 0 and last_assistant_count == 0 and turn_count == 0:
                        tail = []
                    elif turn_count >= 2:
                        tail = []
                        for index in (turn_count - 2, turn_count - 1):
                            turn = turns.nth(index)
                            role = turn.get_attribute("data-message-author-role")
                            identity = turn.get_attribute(
                                "data-message-id"
                            ) or turn.get_attribute("id")
                            if (
                                role not in {"user", "assistant"}
                                or not isinstance(identity, str)
                                or not identity
                            ):
                                tail = None
                                break
                            tail.append({"role": role, "id": identity})
                        if tail is not None and [entry["role"] for entry in tail] != [
                            "user",
                            "assistant",
                        ]:
                            tail = None
                    if tail is None:
                        last_failure_reason = "turn_tail_identity_unavailable"
                        last_signature = None
                        stable_reads = 0
                    elif last_count is None or last_assistant_count is None:
                        last_failure_reason = "turn_cardinality_unavailable"
                        last_signature = None
                        stable_reads = 0
                    else:
                        signature = (
                            conversation_path,
                            last_count,
                            last_assistant_count,
                            tuple((entry["role"], entry["id"]) for entry in tail),
                        )
                        if signature == last_signature:
                            stable_reads += 1
                        else:
                            last_signature = signature
                            stable_reads = 1
                        last_failure_reason = "structure_not_stable"
                        if stable_reads >= 2:
                            self._pending_pre_send_turn_anchor = {
                                "version": 1,
                                "conversation_path": conversation_path,
                                "pre_send_user_turn_count": last_count,
                                "pre_send_assistant_turn_count": last_assistant_count,
                                "tail": tail,
                            }
                else:
                    last_count = None
                    last_assistant_count = None
                    last_signature = None
                    stable_reads = 0
                    last_failure_reason = "conversation_root_not_hydrated"
            except Exception:
                conversation_root_found = False
                last_count = None
                last_assistant_count = None
                last_signature = None
                stable_reads = 0
                last_failure_reason = "turn_tail_read_failed"
            LOGGER.debug(
                "browser_pre_send_baseline pre_send_baseline_poll_count=%s "
                "conversation_path=%s conversation_root_found=%s "
                "pre_send_user_turn_count=%s pre_send_assistant_turn_count=%s "
                "stable_reads=%s failure_reason=%s",
                poll_count,
                conversation_path,
                conversation_root_found,
                last_count,
                last_assistant_count,
                stable_reads,
                last_failure_reason,
                extra={
                    "operation": "browser.pre_send_user_turn_baseline",
                    "status": "poll",
                },
            )
            if (
                conversation_root_found
                and last_count is not None
                and self._pending_pre_send_turn_anchor is not None
                and stable_reads >= 2
            ):
                return last_count
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_PRE_SEND_USER_TURN_BASELINE_UNAVAILABLE",
                    "The hydrated conversation user-turn baseline is unavailable before Send",
                    evidence={
                        "pre_send_baseline_poll_count": poll_count,
                        "conversation_path": conversation_path,
                        "conversation_root_found": conversation_root_found,
                        "pre_send_user_turn_count": last_count,
                        "pre_send_assistant_turn_count": last_assistant_count,
                        "stable_reads": stable_reads,
                        "failure_reason": last_failure_reason,
                    },
                )
            self._start().wait_for_timeout(
                PRE_SEND_USER_TURN_BASELINE_POLL_INTERVAL_MS
            )

    def pre_send_turn_anchor(self) -> dict[str, object] | None:
        anchor = self._pending_pre_send_turn_anchor
        return None if anchor is None else dict(anchor)

    @staticmethod
    def _composer_pasted_text_attachment_count(composer: Any) -> int:
        form = composer.locator("xpath=ancestor::form[1]")
        if form.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_ATTACHMENT_SCOPE_AMBIGUOUS",
                "Expected one composer form while inspecting attachment metadata",
            )
        return int(form.locator(COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR).count())

    @staticmethod
    def _remove_one_pasted_text_attachment(composer: Any) -> None:
        form = composer.locator("xpath=ancestor::form[1]")
        attachment = form.locator(COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR).first
        group = attachment.locator("xpath=ancestor::*[@role='group'][1]")
        remove_control = group.locator(COMPOSER_PASTED_TEXT_ATTACHMENT_REMOVE_SELECTOR)
        if group.count() != 1 or remove_control.count() != 1:
            raise ConflictError(
                "BROWSER_COMPOSER_SPIKE_CLEAR_FAILED",
                "The pasted-text attachment did not expose one semantic removal control",
            )
        remove_control.click()

    @staticmethod
    def _log_big_paste_observation(
        *,
        big_paste_detected: bool,
        pasted_text_attachment_count_before: int,
        pasted_text_attachment_count_after: int,
        attachment_added: bool,
        composer_text_length: int,
    ) -> None:
        LOGGER.debug(
            "browser_composer_big_paste big_paste_detected=%s "
            "pasted_text_attachment_count_before=%s "
            "pasted_text_attachment_count_after=%s attachment_added=%s "
            "composer_text_length=%s",
            big_paste_detected,
            pasted_text_attachment_count_before,
            pasted_text_attachment_count_after,
            attachment_added,
            composer_text_length,
            extra={
                "operation": "browser.composer.native_paste",
                "status": "big_paste_observed",
            },
        )

    def _composer_text(
        self, composer: Any, *, timeout_ms: int | None = None
    ) -> str:
        metadata = (
            self._composer_metadata(composer)
            if timeout_ms is None
            else self._composer_metadata(composer, timeout_ms=timeout_ms)
        )
        if metadata["tag_name"] in {"input", "textarea"}:
            value = composer.input_value(
                timeout=self.timeout_ms if timeout_ms is None else timeout_ms
            )
        else:
            script = (
                """element => {
                    const blockTags = new Set([
                        "P", "DIV", "LI", "PRE", "BLOCKQUOTE"
                    ]);
                    const children = Array.from(element.children);
                    if (children.length && children.every(child => blockTags.has(child.tagName))) {
                        return children.map(child => child.innerText).join("\\n");
                    }
                    return element.innerText;
                }"""
            )
            value = (
                composer.evaluate(script)
                if timeout_ms is None
                else composer.evaluate(script, timeout=timeout_ms)
            )
        if not isinstance(value, str):
            raise ConflictError(
                "BROWSER_COMPOSER_READ_FAILED",
                "The resolved ChatGPT editor did not expose textual content",
            )
        return value

    def _composer_text_for_expected(
        self,
        composer: Any,
        expected: str,
        *,
        timeout_ms: int | None = None,
    ) -> str:
        primary = (
            self._composer_text(composer)
            if timeout_ms is None
            else self._composer_text(composer, timeout_ms=timeout_ms)
        )
        if transport_fingerprint(primary) == expected:
            return primary
        metadata = (
            self._composer_metadata(composer)
            if timeout_ms is None
            else self._composer_metadata(composer, timeout_ms=timeout_ms)
        )
        if metadata["tag_name"] in {"input", "textarea"}:
            return primary
        alternate = composer.text_content(
            timeout=self.timeout_ms if timeout_ms is None else timeout_ms
        )
        if isinstance(alternate, str) and transport_fingerprint(alternate) == expected:
            return alternate
        return primary

    def _is_visible_editor(self, candidate: Any) -> bool:
        try:
            if not candidate.is_visible():
                return False
            metadata = self._composer_metadata(candidate)
        except Exception:
            return False
        return bool(
            metadata["tag_name"] in {"input", "textarea"}
            or metadata["is_content_editable"]
            or metadata["contenteditable"] in {"true", "plaintext-only"}
        )

    def _audit_composer(
        self,
        composer: Any,
        *,
        phase: str,
        timeout_ms: int | None = None,
    ) -> ComposerMetadata:
        metadata = (
            self._composer_metadata(composer)
            if timeout_ms is None
            else self._composer_metadata(composer, timeout_ms=timeout_ms)
        )
        LOGGER.debug(
            "browser_composer_audit phase=%s tag_name=%s contenteditable=%s role=%s "
            "id=%s data_testid=%s is_content_editable=%s focused=%s "
            "editable_descendant_count=%s",
            phase,
            metadata["tag_name"],
            metadata["contenteditable"],
            metadata["role"],
            metadata["id"],
            metadata["data_testid"],
            metadata["is_content_editable"],
            metadata["focused"],
            metadata["editable_descendant_count"],
            extra={"operation": "browser.composer.audit", "status": phase},
        )
        return metadata

    @staticmethod
    def _composer_metadata(
        composer: Any, *, timeout_ms: int | None = None
    ) -> ComposerMetadata:
        script = (
            """element => {
                const active = element.ownerDocument.activeElement;
                const editableSelector =
                    "textarea, input:not([type='hidden']), [contenteditable='true'], " +
                    "[contenteditable='plaintext-only']";
                return {
                    tag_name: element.tagName.toLowerCase(),
                    contenteditable: element.getAttribute('contenteditable'),
                    role: element.getAttribute('role'),
                    id: element.id || null,
                    data_testid: element.getAttribute('data-testid'),
                    is_content_editable: element.isContentEditable,
                    focused: active === element || element.contains(active),
                    editable_descendant_count: element.querySelectorAll(editableSelector).length,
                };
            }"""
        )
        raw = (
            composer.evaluate(script)
            if timeout_ms is None
            else composer.evaluate(script, timeout=timeout_ms)
        )
        if not isinstance(raw, dict):
            raise ConflictError(
                "BROWSER_COMPOSER_AUDIT_FAILED",
                "The resolved ChatGPT editor did not expose DOM metadata",
            )
        return ComposerMetadata(
            tag_name=str(raw.get("tag_name") or ""),
            contenteditable=ChatGPTWebAdapter._optional_string(raw.get("contenteditable")),
            role=ChatGPTWebAdapter._optional_string(raw.get("role")),
            id=ChatGPTWebAdapter._optional_string(raw.get("id")),
            data_testid=ChatGPTWebAdapter._optional_string(raw.get("data_testid")),
            is_content_editable=bool(raw.get("is_content_editable")),
            focused=bool(raw.get("focused")),
            editable_descendant_count=int(raw.get("editable_descendant_count") or 0),
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    def _path(self) -> str | None:
        return conversation_path_from_page_url(str(self._start().url))

    def _assistant_for_fingerprint(self, requested_fingerprint: str) -> Any:
        scan = self._scan_user_turn_proof(requested_fingerprint)
        matches = scan["match_indices"]
        turns = self._start().locator(TURN_SELECTOR)
        if (
            scan["pending"]
            or scan["ambiguous"]
            or len(matches) != 1
            or matches[0] + 1 >= turns.count()
        ):
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS", "Cannot prove one assistant turn for the request"
            )
        assistant = turns.nth(matches[0] + 1)
        if assistant.get_attribute("data-message-author-role") != "assistant":
            raise ConflictError(
                "BROWSER_TURN_AMBIGUOUS", "The message after the request is not an assistant turn"
            )
        return assistant

    @staticmethod
    def _assistant_content(turn: Any) -> Any:
        if turn.get_attribute("data-message-author-role") == "assistant":
            return turn
        return turn.locator(ASSISTANT_MESSAGE_SELECTOR)

    @staticmethod
    def _copy_button_for_assistant(assistant: Any) -> Any:
        current = assistant
        for depth in range(8):
            assistant_count = (
                1 if depth == 0 else current.locator(ASSISTANT_MESSAGE_SELECTOR).count()
            )
            user_count = current.locator(USER_MESSAGE_SELECTOR).count()
            if assistant_count == 1 and user_count == 0:
                button = current.get_by_role("button", name=COPY_PATTERN)
                if button.count() == 1:
                    return button
            current = current.locator("xpath=..")
        return assistant.get_by_role("button", name=COPY_PATTERN)

    def _activate_copy_and_read(self, button: Any) -> str:
        page = self._start()
        try:
            before = page.evaluate("navigator.clipboard.readText()")
        except Exception:
            before = None
        try:
            button.press("Enter", timeout=min(5000, self.timeout_ms))
            deadline = monotonic() + min(5.0, self.timeout_ms / 1000)
            while monotonic() < deadline:
                value = page.evaluate("navigator.clipboard.readText()")
                if isinstance(value, str) and value.strip() and value != before:
                    return value
                page.wait_for_timeout(50)
        except Exception as exc:
            raise ConflictError(
                "BROWSER_COPY_CAPTURE_UNAVAILABLE",
                "The Copy control could not be activated or read",
            ) from exc
        raise ConflictError(
            "BROWSER_COPY_CAPTURE_UNAVAILABLE",
            "The Copy control did not produce new clipboard content",
        )

    def _capture_gate_path(self) -> Path:
        return self.profile_dir / "helios-capture-method.json"

    @staticmethod
    def _is_profile_in_use_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "processsingleton",
                "profile in use",
                "profile is in use",
                "user data directory is already in use",
            )
        )
