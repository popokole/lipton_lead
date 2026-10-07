"""CLI ручного сообщения: предпросмотр разметки до отправки."""

from __future__ import annotations

from app.tools.send_message import describe


def test_describe_strips_tags_and_counts_quotes() -> None:
    plain, summary = describe(
        "🎓 <b>заголовок</b>\n<blockquote><i>мысль</i></blockquote>\n"
        "<blockquote expandable>длинный список</blockquote>"
    )

    assert plain == "🎓 заголовок\nмысль\nдлинный список"
    assert "Bold: 1" in summary
    assert "Italic: 1" in summary
    assert "Blockquote: 1" in summary
    assert "Blockquote (expandable): 1" in summary


def test_describe_plain_text_has_no_markup() -> None:
    plain, summary = describe("просто текст")

    assert plain == "просто текст"
    assert summary == []
