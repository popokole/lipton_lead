"""Сборка промпта генерации (ТЗ §13)."""

from __future__ import annotations

from app.ai.prompts import build_generate_messages
from app.ai.provider import GenerateRequest


def _request(**kwargs: object) -> GenerateRequest:
    base: dict[str, object] = {"system_prompt": "Ты менеджер.", "message_text": "привет"}
    base.update(kwargs)
    return GenerateRequest(**base)


class TestRecentRepliesInPrompt:
    def test_recent_replies_add_a_system_message(self) -> None:
        messages = build_generate_messages(
            _request(recent_replies=("могу посоветовать психолога",))
        )
        joined = "\n".join(m.content for m in messages if m.role == "system")
        assert "недавно уже отправлялись" in joined
        assert "могу посоветовать психолога" in joined

    def test_no_system_message_when_recent_replies_empty(self) -> None:
        messages = build_generate_messages(_request())
        joined = "\n".join(m.content for m in messages if m.role == "system")
        assert "недавно уже отправлялись" not in joined

    def test_recent_replies_come_before_context_history(self) -> None:
        from app.ai.provider import ChatMessage

        messages = build_generate_messages(
            _request(
                recent_replies=("уже отвечали так",),
                context=[ChatMessage(role="user", content="старое сообщение")],
            )
        )
        roles_content = [(m.role, m.content) for m in messages]
        recent_idx = next(i for i, (_, c) in enumerate(roles_content) if "уже отвечали так" in c)
        history_idx = next(
            i for i, (_, c) in enumerate(roles_content) if c == "старое сообщение"
        )
        assert recent_idx < history_idx
