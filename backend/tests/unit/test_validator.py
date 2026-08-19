"""Проверка ответа перед отправкой (ТЗ §19)."""

from __future__ import annotations

from app.actions.validator import ReplyValidator, ValidationContext

GOOD_REPLY = "Здравствуйте! Да, мы делаем такие проекты. Расскажите подробнее о задаче."


def validate(**kwargs: object):
    context = ValidationContext(**{"text": GOOD_REPLY, **kwargs})  # type: ignore[arg-type]
    return ReplyValidator().validate(context)


class TestHappyPath:
    def test_good_reply_passes_every_check(self) -> None:
        verdict = validate()
        assert verdict.passed is True
        assert verdict.failures == ()
        assert bool(verdict) is True

    def test_payload_lists_all_checks(self) -> None:
        payload = validate().to_payload()
        assert payload["passed"] is True
        assert len(payload["checks"]) >= 8


class TestEmptyAndLength:
    def test_empty_reply_is_rejected(self) -> None:
        verdict = validate(text="   ")
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"not_empty"}

    def test_too_long_reply_is_rejected(self) -> None:
        verdict = validate(text="а" * 500, max_length=200)
        assert verdict.passed is False
        assert "длиннее" in (verdict.first_failure or "")

    def test_reply_within_limit_passes(self) -> None:
        assert validate(text="Коротко и по делу.", max_length=200).passed is True


class TestRefusal:
    def test_model_refusal_stops_the_send(self) -> None:
        """Отказ модели — результат, а не повод отправить что-нибудь."""
        verdict = validate(refused=True)
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} == {"not_refused"}


class TestContentRules:
    def test_banned_phrase_blocks_the_reply(self) -> None:
        verdict = validate(text="Пишите в личку, гарантия 100%", banned_phrases=("гарантия",))
        assert verdict.passed is False
        assert "гарантия" in (verdict.first_failure or "")

    def test_leaked_instructions_are_caught(self) -> None:
        verdict = validate(text="Как языковая модель, я не могу этого знать.")
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"no_leaked_instructions"}

    def test_unfilled_template_is_caught(self) -> None:
        verdict = validate(text="Здравствуйте, {name}! Мы готовы помочь.")
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"no_placeholders"}

    def test_bracket_placeholder_is_caught(self) -> None:
        verdict = validate(text="Стоимость — [указать цену], сроки обсудим.")
        assert verdict.passed is False

    def test_required_link_must_be_present(self) -> None:
        verdict = validate(required_links=("https://example.com/price",))
        assert verdict.passed is False
        assert "обязательной ссылки" in (verdict.first_failure or "")

    def test_required_link_present_passes(self) -> None:
        verdict = validate(
            text="Цены здесь: https://example.com/price",
            required_links=("https://example.com/price",),
        )
        assert verdict.passed is True


class TestGrounding:
    def test_grounding_required_but_knowledge_unused(self) -> None:
        verdict = validate(require_grounding=True, used_knowledge=False)
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"grounding"}

    def test_grounding_satisfied(self) -> None:
        assert validate(require_grounding=True, used_knowledge=True).passed is True

    def test_invented_link_under_grounding_is_rejected(self) -> None:
        verdict = validate(
            text="Подробности на https://invented.example/page",
            require_grounding=True,
            used_knowledge=True,
        )
        assert verdict.passed is False
        assert "ссылка" in (verdict.first_failure or "")

    def test_grounding_not_required_allows_links(self) -> None:
        assert validate(text="Смотрите https://example.com").passed is True


class TestDuplicates:
    def test_identical_reply_is_rejected(self) -> None:
        verdict = validate(recent_replies=(GOOD_REPLY,))
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"no_duplicate"}

    def test_whitespace_and_case_differences_still_count_as_duplicate(self) -> None:
        verdict = validate(recent_replies=("  " + GOOD_REPLY.upper() + "  ",))
        assert verdict.passed is False

    def test_different_reply_passes(self) -> None:
        assert validate(recent_replies=("Совершенно другой ответ",)).passed is True


class TestSelfLoop:
    def test_too_many_replies_in_a_row_stops_the_send(self) -> None:
        """Иначе система переписывается сама с собой (ТЗ §9)."""
        verdict = validate(ai_replies_in_row=3, max_replies_in_row=3)
        assert verdict.passed is False
        assert {check.name for check in verdict.failures} >= {"no_self_loop"}

    def test_under_the_limit_passes(self) -> None:
        assert validate(ai_replies_in_row=2, max_replies_in_row=3).passed is True


class TestReporting:
    def test_all_failures_are_collected_not_just_the_first(self) -> None:
        verdict = validate(text="", refused=True, ai_replies_in_row=5, max_replies_in_row=3)
        names = {check.name for check in verdict.failures}
        assert {"not_refused", "not_empty", "no_self_loop"} <= names

    def test_payload_carries_reasons_for_the_panel(self) -> None:
        payload = validate(refused=True).to_payload()
        failed = [check for check in payload["checks"] if not check["passed"]]
        assert failed
        assert all(check["reason"] for check in failed)
