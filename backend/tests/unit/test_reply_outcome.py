"""ReplyOutcome.processed_status: маппинг действия в статус сообщения.

Без этого маппинга IGNORE/ESCALATE_TO_HUMAN неотличимы в панели от «ещё
обрабатывается» (см. app/pipeline/monitor_pipeline.py).
"""

from __future__ import annotations

import pytest

from app.models import ActionStatus, ActionType, ProcessedStatus
from app.pipeline.reply_pipeline import ReplyOutcome


@pytest.mark.parametrize(
    ("action", "status", "expected"),
    [
        (ActionType.REPLY, ActionStatus.SENT, ProcessedStatus.REPLIED),
        (ActionType.IGNORE, ActionStatus.SENT, ProcessedStatus.IGNORED),
        (ActionType.ESCALATE_TO_HUMAN, ActionStatus.SENT, ProcessedStatus.ESCALATED),
        (ActionType.NOTIFY_ADMIN, ActionStatus.SENT, ProcessedStatus.ACTED),
        (ActionType.SAVE_LEAD, ActionStatus.SENT, ProcessedStatus.ACTED),
        (ActionType.TAG_USER, ActionStatus.SENT, ProcessedStatus.ACTED),
        (ActionType.REPLY, ActionStatus.FAILED, ProcessedStatus.FAILED),
        (ActionType.IGNORE, ActionStatus.FAILED, ProcessedStatus.FAILED),
        (ActionType.TAG_USER, ActionStatus.REJECTED, ProcessedStatus.FAILED),
    ],
)
def test_processed_status_mapping(
    action: ActionType, status: ActionStatus, expected: ProcessedStatus
) -> None:
    outcome = ReplyOutcome(action=action, status=status)
    assert outcome.processed_status is expected
