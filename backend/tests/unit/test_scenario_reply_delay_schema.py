"""Своя задержка ответа сценария: схема API, ограничения модели, миграция.

Проверки API и базы должны совпадать: то, что пропустила схема, не должно
падать CHECK-ограничением (500 вместо понятного 422).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.models import Scenario
from app.schemas.resources import (
    REPLY_DELAY_MAX_SECONDS,
    ScenarioCreate,
    ScenarioOut,
    ScenarioUpdate,
    check_reply_delay_order,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _create(**fields: object) -> ScenarioCreate:
    return ScenarioCreate(name="Продажи", system_prompt="Ты менеджер", **fields)  # type: ignore[arg-type]


class TestScenarioCreate:
    def test_delay_is_optional(self) -> None:
        payload = _create()
        assert payload.reply_delay_min_seconds is None
        assert payload.reply_delay_max_seconds is None

    @pytest.mark.parametrize(
        ("low", "high"),
        [
            (0, 0),
            (10, 60),
            (30, 30),
            (0, REPLY_DELAY_MAX_SECONDS),
            (REPLY_DELAY_MAX_SECONDS, REPLY_DELAY_MAX_SECONDS),
            (15, None),
            (None, 90),
        ],
    )
    def test_valid_ranges(self, low: int | None, high: int | None) -> None:
        payload = _create(reply_delay_min_seconds=low, reply_delay_max_seconds=high)
        assert payload.reply_delay_min_seconds == low
        assert payload.reply_delay_max_seconds == high

    def test_min_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="«от» не может быть больше «до»"):
            _create(reply_delay_min_seconds=61, reply_delay_max_seconds=60)

    def test_min_with_zero_max_is_rejected(self) -> None:
        """max=0 — «без задержки»; min > 0 при этом — противоречие, как и в CHECK."""
        with pytest.raises(ValidationError):
            _create(reply_delay_min_seconds=5, reply_delay_max_seconds=0)

    @pytest.mark.parametrize("field", ["reply_delay_min_seconds", "reply_delay_max_seconds"])
    @pytest.mark.parametrize("value", [-1, REPLY_DELAY_MAX_SECONDS + 1])
    def test_out_of_bounds_is_rejected(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            _create(**{field: value})


class TestScenarioUpdate:
    def test_single_bound_is_accepted(self) -> None:
        payload = ScenarioUpdate(reply_delay_max_seconds=120)
        assert payload.model_dump(exclude_unset=True) == {"reply_delay_max_seconds": 120}

    def test_clearing_the_delay_is_accepted(self) -> None:
        payload = ScenarioUpdate(reply_delay_min_seconds=None, reply_delay_max_seconds=None)
        assert payload.model_dump(exclude_unset=True) == {
            "reply_delay_min_seconds": None,
            "reply_delay_max_seconds": None,
        }

    def test_min_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScenarioUpdate(reply_delay_min_seconds=100, reply_delay_max_seconds=10)

    def test_out_of_bounds_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScenarioUpdate(reply_delay_max_seconds=REPLY_DELAY_MAX_SECONDS + 1)


class TestOrderCheck:
    """Та же проверка вызывается в PATCH на слитом с базой состоянии."""

    @pytest.mark.parametrize(
        ("low", "high"), [(None, None), (None, 5), (5, None), (5, 5), (0, 3600)]
    )
    def test_accepts(self, low: int | None, high: int | None) -> None:
        check_reply_delay_order(low, high)

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="«от»"):
            check_reply_delay_order(30, 10)


def test_scenario_out_exposes_delay() -> None:
    scenario = Scenario(
        id=uuid.uuid4(),
        name="Продажи",
        system_prompt="Ты менеджер",
        fallback_texts=[],
        require_knowledge_grounding=False,
        human_handoff_enabled=True,
        reply_in_dm=False,
        review_when_uncertain=False,
        one_shot=False,
        enabled=True,
        reply_delay_min_seconds=20,
        reply_delay_max_seconds=45,
    )
    out = ScenarioOut.model_validate(scenario)
    assert out.reply_delay_min_seconds == 20
    assert out.reply_delay_max_seconds == 45


class TestModelAndMigration:
    def test_model_has_named_check_constraints(self) -> None:
        names = {
            constraint.name
            for constraint in Scenario.__table__.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        assert "ck_scenarios_reply_delay_range" in names
        assert "ck_scenarios_reply_delay_order" in names

    def test_columns_are_nullable_integers(self) -> None:
        for name in ("reply_delay_min_seconds", "reply_delay_max_seconds"):
            column = Scenario.__table__.c[name]
            assert column.nullable is True
            assert isinstance(column.type, sa.Integer)

    def test_migration_chains_on_vv20_without_forking_heads(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        scripts = ScriptDirectory.from_config(config)

        # Одна голова: иначе `alembic upgrade head` на проде не выполнится.
        assert len(scripts.get_heads()) == 1
        assert "xx22delay" in {script.revision for script in scripts.walk_revisions()}
        revision = scripts.get_revision("xx22delay")
        assert revision is not None
        assert revision.down_revision == "vv20react"
        assert Path(revision.path).name == "xx22_scenario_reply_delay.py"

    def test_migration_has_downgrade(self) -> None:
        source = (BACKEND_ROOT / "alembic/versions/xx22_scenario_reply_delay.py").read_text(
            encoding="utf-8"
        )
        assert "def downgrade" in source
        assert 'drop_column("scenarios", "reply_delay_min_seconds")' in source
        assert 'drop_column("scenarios", "reply_delay_max_seconds")' in source
