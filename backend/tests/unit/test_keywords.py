"""Поиск ключевых слов (ТЗ §10)."""

from __future__ import annotations

import pytest

from app.rules.keywords import KeywordMatcher, KeywordSpec, MatchMode, compile_regex


def matcher(**kwargs: object) -> KeywordMatcher:
    return KeywordMatcher(KeywordSpec(**kwargs))  # type: ignore[arg-type]


class TestSubstring:
    def test_finds_phrase_inside_sentence(self) -> None:
        assert matcher(terms=("нужен дизайнер",)).matches("Всем привет, нужен дизайнер срочно")

    def test_is_case_insensitive_by_default(self) -> None:
        assert matcher(terms=("Дизайнер",)).matches("ищу дизайнера")

    def test_case_sensitive_mode_respects_case(self) -> None:
        strict = matcher(terms=("Дизайнер",), case_sensitive=True)
        assert strict.matches("Дизайнер нужен") is True
        assert strict.matches("дизайнер нужен") is False

    def test_returns_positions_of_all_hits(self) -> None:
        hits = matcher(terms=("дизайн",)).find("дизайн и ещё дизайн")
        assert [hit.start for hit in hits] == [0, 13]
        assert {hit.term.lower() for hit in hits} == {"дизайн"}

    def test_no_match_returns_empty(self) -> None:
        assert matcher(terms=("бухгалтер",)).matches("ищу дизайнера") is False


class TestWholeWord:
    def test_matches_standalone_word(self) -> None:
        assert matcher(terms=("дизайн",), mode=MatchMode.WHOLE_WORD).matches("нужен дизайн срочно")

    def test_does_not_match_inside_longer_word(self) -> None:
        spec = matcher(terms=("дизайн",), mode=MatchMode.WHOLE_WORD)
        assert spec.matches("дизайнерская студия") is False

    def test_handles_punctuation_as_boundary(self) -> None:
        spec = matcher(terms=("дизайн",), mode=MatchMode.WHOLE_WORD)
        assert spec.matches("нужен дизайн, срочно!") is True

    def test_latin_words_too(self) -> None:
        spec = matcher(terms=("design",), mode=MatchMode.WHOLE_WORD)
        assert spec.matches("need design now") is True
        assert spec.matches("redesigned") is False


class TestExact:
    def test_matches_whole_message_only(self) -> None:
        spec = matcher(terms=("привет",), mode=MatchMode.EXACT)
        assert spec.matches("привет") is True
        assert spec.matches("  привет  ") is True
        assert spec.matches("привет, как дела") is False


class TestRegexMode:
    def test_pattern_matches(self) -> None:
        spec = matcher(terms=(r"ищ[уе]м?\s+дизайнера",), mode=MatchMode.REGEX)
        assert spec.matches("мы ищем дизайнера") is True
        assert spec.matches("ищу дизайнера") is True

    def test_invalid_pattern_is_reported(self) -> None:
        with pytest.raises(ValueError, match="invalid keyword pattern"):
            matcher(terms=("([",), mode=MatchMode.REGEX).matches("что угодно")


class TestExclusions:
    def test_excluded_term_blocks_the_match(self) -> None:
        spec = matcher(terms=("дизайнер",), exclude=("резюме",))
        assert spec.matches("нужен дизайнер") is True
        assert spec.matches("нужен дизайнер, шлите резюме") is False

    def test_exclusion_works_without_include_terms(self) -> None:
        spec = matcher(exclude=("спам",))
        assert spec.matches("обычное сообщение") is True
        assert spec.matches("это спам") is False

    def test_find_returns_nothing_when_excluded(self) -> None:
        spec = matcher(terms=("дизайнер",), exclude=("резюме",))
        assert spec.find("дизайнер и резюме") == []


class TestEmptySpec:
    def test_empty_spec_matches_everything(self) -> None:
        assert matcher().matches("любой текст") is True

    def test_empty_text_never_matches_terms(self) -> None:
        assert matcher(terms=("дизайнер",)).matches("") is False

    def test_is_empty_flag(self) -> None:
        assert KeywordSpec().is_empty is True
        assert KeywordSpec(terms=("a",)).is_empty is False


class TestSpecParsing:
    def test_from_dict_reads_all_fields(self) -> None:
        spec = KeywordSpec.from_dict(
            {
                "terms": ["  дизайнер ", "", "верстальщик"],
                "exclude": ["резюме"],
                "mode": "whole_word",
                "case_sensitive": True,
            }
        )
        assert spec.terms == ("дизайнер", "верстальщик")
        assert spec.exclude == ("резюме",)
        assert spec.mode is MatchMode.WHOLE_WORD
        assert spec.case_sensitive is True

    def test_from_dict_defaults(self) -> None:
        spec = KeywordSpec.from_dict(None)
        assert spec.is_empty is True
        assert spec.mode is MatchMode.SUBSTRING

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown keyword match mode"):
            KeywordSpec.from_dict({"mode": "fuzzy"})


class TestRulePattern:
    def test_compile_regex_returns_none_for_empty(self) -> None:
        assert compile_regex(None) is None
        assert compile_regex("") is None

    def test_compile_regex_is_case_insensitive(self) -> None:
        pattern = compile_regex("ДИЗАЙНЕР")
        assert pattern is not None
        assert pattern.search("нужен дизайнер") is not None

    def test_invalid_rule_regex_is_reported(self) -> None:
        with pytest.raises(ValueError, match="invalid rule regex"):
            compile_regex("(unclosed")

    def test_compilation_is_cached(self) -> None:
        first = compile_regex("дизайнер")
        second = compile_regex("дизайнер")
        assert first is second
