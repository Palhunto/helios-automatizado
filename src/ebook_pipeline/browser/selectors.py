from __future__ import annotations

import re

LOGIN_PATTERN = re.compile(r"log in|sign in|entrar", re.IGNORECASE)
CHALLENGE_PATTERN = re.compile(
    r"captcha|verify you are human|security check|two.factor|c[oó]digo de verifica", re.IGNORECASE
)
STOP_PATTERN = re.compile(r"stop generating|parar de gerar|stop", re.IGNORECASE)
COPY_PATTERN = re.compile(r"copy|copiar", re.IGNORECASE)
SEND_PATTERN = re.compile(r"send|enviar", re.IGNORECASE)
RETRY_PATTERN = re.compile(
    r"retry|try again|tentar novamente|regenerate|gerar novamente", re.IGNORECASE
)
OPERATIONAL_ERROR_PATTERN = re.compile(
    r"something went wrong|network error|houve um erro|ocorreu um erro|"
    r"unusual activity|too many requests|tente novamente",
    re.IGNORECASE,
)

COMPOSER_TEST_ID = "#prompt-textarea"
COMPOSER_EDITOR_SELECTOR = (
    "textarea, input:not([type='hidden']), [contenteditable='true'], "
    "[contenteditable='plaintext-only']"
)
COMPOSER_PASTED_TEXT_ATTACHMENT_SELECTOR = (
    "button[aria-label^='Abrir anexo de texto colado']"
)
COMPOSER_PASTED_TEXT_ATTACHMENT_REMOVE_SELECTOR = (
    "button[aria-label^='Remover ficheiro']"
)
SEND_BUTTON_TEST_ID = "button[data-testid='send-button']"
TURN_SELECTOR = (
    "[data-message-author-role='user'], [data-message-author-role='assistant']"
)
USER_MESSAGE_SELECTOR = "[data-message-author-role='user']"
USER_TURN_PASTED_TEXT_ATTACHMENT_GROUP_SELECTOR = "div[role='group'][aria-label]"
USER_TURN_PASTED_TEXT_ATTACHMENT_BUTTON_SELECTOR = "button[aria-label]"
USER_TURN_PASTED_TEXT_MODAL_CONTAINER_XPATH = (
    "xpath=ancestor::div[.//header//h2 and .//section and .//footer][1]"
)
USER_TURN_PASTED_TEXT_MODAL_TITLE_SELECTOR = "header h2"
USER_TURN_PASTED_TEXT_MODAL_CONTENT_SELECTOR = "section"
USER_TURN_PASTED_TEXT_MODAL_PROGRESS_SELECTOR = "[role='progressbar']"
ASSISTANT_MESSAGE_SELECTOR = "[data-message-author-role='assistant']"
# Select the response's top-level semantic root inside one assistant turn. Nested
# markdown regions belong to that same response and must not become independent
# capture candidates.
ASSISTANT_RENDERED_CONTENT_SELECTOR = (
    ".markdown.prose:not(:where(.markdown.prose .markdown.prose))"
)
ASSISTANT_GENERATION_INDICATOR_SELECTOR = (
    "[aria-busy='true'], [data-state='streaming'], "
    "[data-is-streaming='true'], [role='progressbar']"
)
ASSISTANT_POST_RESPONSE_CONTROL_SELECTOR = "button, [role='button']"
CONVERSATION_LINK_SELECTOR = "a[href^='/c/']"
CONVERSATION_ROOT_SELECTOR = "main"
