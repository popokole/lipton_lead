"""Фильтры и подбор правил (ТЗ §8, §9)."""

from __future__ import annotations

import uuid

import pytest

from app.models import ChatType, MediaType, RuleScope
from app.rules.engine import CooldownSpec, evaluate_rule, select_matches
from app.rules.filters import MessageFilterSpec, SelfGuard, check_message
from tests.builders import ACCOUNT_ID, CHAT_ID, message, rule


class TestSelfGuard:
    def test_own_outgoing_message_is_blocked(self) -> None:
        verdict = SelfGuard().check(message(is_outgoing=True))
        assert verdict.passed is False
        assert verdict.reason == "own outgoing message"

    def test_message_from_another_of_our_accounts_is_blocked(self) -> None:
        """Иначе два наших аккаунта в одном чате зацикливаются друг на друге."""
        guard = SelfGuard(own_ids=[999])
        assert guard.check(message(sender_tg_id=999)).passed is False

    def test_stranger_passes(self) -> None:
        guard = SelfGuard(own_ids=[999])
        assert guard.check(message(sender_tg_id=12345)).passed is True

    def test_registry_can_be_updated(self) -> None:
        guard = SelfGuard()
        assert guard.check(message(sender_tg_id=555)).passed is True

        guard.update([555])
        assert guard.check(message(sender_tg_id=555)).passed is False
        assert guard.own_ids == frozenset({555})

    def test_message_without_sender_is_not_treated_as_ours(self) -> None:
        guard = SelfGuard(own_ids=[999])
        assert guard.check(message(sender_tg_id=None)).passed is True


class TestMessageFilters:
    def test_incoming_only_blocks_outgoing(self) -> None:
        spec = MessageFilterSpec(incoming_only=True)
        assert check_message(message(is_outgoing=True), spec).passed is False

    def test_text_only_blocks_media_without_caption(self) -> None:
        spec = MessageFilterSpec(text_only=True)
        assert check_message(message(text="", media_type=MediaType.PHOTO), spec).passed is False
        assert check_message(message(text="подпись"), spec).passed is True

    def test_forwarded_can_be_excluded(self) -> None:
        spec = MessageFilterSpec(allow_forwarded=False)
        assert check_message(message(is_forwarded=True), spec).passed is False
        assert check_message(message(is_forwarded=False), spec).passed is True

    def test_chat_types_restrict_where_rule_applies(self) -> None:
        spec = MessageFilterSpec(chat_types=(ChatType.PRIVATE,))
        assert check_message(message(chat_type=ChatType.SUPERGROUP), spec).passed is False
        assert check_message(message(chat_type=ChatType.PRIVATE), spec).passed is True

    def test_sender_allow_list(self) -> None:
        spec = MessageFilterSpec(sender_ids=(111,))
        assert check_message(message(sender_tg_id=222), spec).passed is False
        assert check_message(message(sender_tg_id=111), spec).passed is True

    def test_sender_deny_list(self) -> None:
        spec = MessageFilterSpec(exclude_sender_ids=(222,))
        assert check_message(message(sender_tg_id=222), spec).passed is False

    def test_defaults_pass_ordinary_incoming_message(self) -> None:
        assert check_message(message(), MessageFilterSpec()).passed is True

    def test_spec_from_dict(self) -> None:
        spec = MessageFilterSpec.from_dict(
            {
                "incoming_only": False,
                "text_only": True,
                "forwarded": False,
                "chat_types": ["private", "supergroup", "nonsense"],
                "sender_ids": [1, "2", "abc"],
                "languages": ["RU"],
            }
        )
        assert spec.incoming_only is False
        assert spec.text_only is True
        assert spec.allow_forwarded is False
        assert spec.chat_types == (ChatType.PRIVATE, ChatType.SUPERGROUP)
        assert spec.sender_ids == (1, 2)
        assert spec.languages == ("ru",)


class TestRuleEvaluation:
    def test_keyword_match(self) -> None:
        verdict, hits = evaluate_rule(rule(terms=("нужен дизайнер",)), message(), CHAT_ID)
        assert verdict.passed is True
        assert [hit.term for hit in hits] == ["нужен дизайнер"]

    def test_keyword_mismatch(self) -> None:
        verdict, hits = evaluate_rule(rule(terms=("бухгалтер",)), message(), CHAT_ID)
        assert verdict.passed is False
        assert hits == []

    def test_regex_must_also_match(self) -> None:
        target = rule(terms=("дизайнер",), regex=r"срочно")
        assert evaluate_rule(target, message("нужен дизайнер"), CHAT_ID)[0].passed is False
        assert evaluate_rule(target, message("нужен дизайнер срочно"), CHAT_ID)[0].passed is True

    def test_rule_limited_to_other_account_does_not_apply(self) -> None:
        target = rule(account_ids=frozenset({uuid.uuid4()}))
        assert evaluate_rule(target, message(), CHAT_ID)[0].passed is False

    def test_rule_limited_to_our_account_applies(self) -> None:
        target = rule(account_ids=frozenset({ACCOUNT_ID}))
        assert evaluate_rule(target, message(), CHAT_ID)[0].passed is True

    def test_rule_limited_to_other_chat_does_not_apply(self) -> None:
        target = rule(chat_ids=frozenset({uuid.uuid4()}))
        assert evaluate_rule(target, message(), CHAT_ID)[0].passed is False

    def test_empty_scope_means_every_account_and_chat(self) -> None:
        assert evaluate_rule(rule(), message(), None)[0].passed is True

    def test_exclusion_wins_over_keyword(self) -> None:
        target = rule(terms=("дизайнер",), exclude=("резюме",))
        assert evaluate_rule(target, message("дизайнер, вот резюме"), CHAT_ID)[0].passed is False


class TestSelectMatches:
    def test_first_matching_rule_stops_the_search(self) -> None:
        high = rule(name="высокий", priority=200)
        low = rule(name="низкий", priority=10)

        matches = select_matches([high, low], message(), CHAT_ID)

        assert [match.rule.name for match in matches] == ["высокий"]

    def test_non_stopping_rule_lets_the_search_continue(self) -> None:
        notify = rule(name="уведомить", stop_on_match=False)
        reply = rule(name="ответить")

        matches = select_matches([notify, reply], message(), CHAT_ID)

        assert [match.rule.name for match in matches] == ["уведомить", "ответить"]

    def test_no_match_returns_empty_list(self) -> None:
        assert select_matches([rule(terms=("бухгалтер",))], message(), CHAT_ID) == []

    def test_matched_terms_are_reported(self) -> None:
        matches = select_matches([rule(terms=("дизайнер", "верстальщик"))], message(), CHAT_ID)
        assert matches[0].matched_terms == ("дизайнер",)

    def test_empty_rule_set(self) -> None:
        assert select_matches([], message(), CHAT_ID) == []


class TestCooldownSpec:
    def test_defaults_apply_the_global_user_cooldown(self) -> None:
        spec = CooldownSpec.from_dict(None, default_user=600)
        assert spec.user == 600
        assert spec.chat == 0

    def test_explicit_values_win(self) -> None:
        spec = CooldownSpec.from_dict({"user": 60, "chat": 10}, default_user=600)
        assert (spec.user, spec.chat) == (60, 10)

    def test_zero_is_respected_not_replaced_by_default(self) -> None:
        assert CooldownSpec.from_dict({"user": 0}, default_user=600).user == 0

    def test_negative_values_are_clamped(self) -> None:
        assert CooldownSpec.from_dict({"user": -5}, default_user=600).user == 0

    def test_garbage_falls_back_to_default(self) -> None:
        assert CooldownSpec.from_dict({"user": "часто"}, default_user=600).user == 600


@pytest.mark.parametrize("scope", [RuleScope.CHAT_MONITOR, RuleScope.DIALOG])
def test_rule_scope_is_carried_through(scope: RuleScope) -> None:
    assert rule(scope=scope).scope is scope
