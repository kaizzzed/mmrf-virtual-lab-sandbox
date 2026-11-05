"""Builders for the AG-UI events ORION emits, over the ag_ui.core 1.0.0 classes."""

from datetime import UTC, datetime

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from ag_ui.encoder import EventEncoder
from pydantic import JsonValue

from .models import SNAPSHOT_LIMIT_BYTES, Code, FinishStatus, RunView

HEARTBEAT_NAME = "orion.heartbeat"
_ENCODER = EventEncoder()
_ELLIPSIS = "…"


class SnapshotTooLarge(Exception):
    """The view cannot be trimmed under the snapshot limit."""


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def encode(event: BaseEvent) -> str:
    return _ENCODER.encode(event)


def run_started(thread_id: str, run_id: str) -> RunStartedEvent:
    return RunStartedEvent(thread_id=thread_id, run_id=run_id)


def run_finished(thread_id: str, run_id: str, status: FinishStatus, code: Code | None) -> RunFinishedEvent:
    return RunFinishedEvent(thread_id=thread_id, run_id=run_id, result={"status": status, "code": code})


def run_error(code: Code, message: str) -> RunErrorEvent:
    return RunErrorEvent(message=message, code=code)


def step_started(step_id: str) -> StepStartedEvent:
    return StepStartedEvent(step_name=step_id)


def step_finished(step_id: str) -> StepFinishedEvent:
    return StepFinishedEvent(step_name=step_id)


def heartbeat(moment: datetime) -> CustomEvent:
    return CustomEvent(name=HEARTBEAT_NAME, value={"ts": iso_utc(moment)})


def state_snapshot(view: RunView) -> StateSnapshotEvent:
    return StateSnapshotEvent(snapshot=fit_view(view))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + _ELLIPSIS


def _trimmed(view: RunView, summary_cap: int, text_cap: int | None) -> RunView:
    steps = [
        step.model_copy(
            update={
                "input_summary": _clip(step.input_summary, summary_cap),
                "output_summary": None if step.output_summary is None else _clip(step.output_summary, summary_cap),
            }
        )
        for step in view.steps
    ]
    update: dict[str, object] = {"steps": steps}
    if text_cap is not None:
        update["question"] = _clip(view.question, text_cap)
        update["limitations"] = [
            item.model_copy(update={"text": _clip(item.text, text_cap)}) for item in view.limitations
        ]
        if view.plan is not None:
            update["plan"] = view.plan.model_copy(
                update={
                    "paraphrase": _clip(view.plan.paraphrase, text_cap),
                    "assumptions": [
                        item.model_copy(update={"text": _clip(item.text, text_cap)}) for item in view.plan.assumptions
                    ],
                    "rows": [
                        row.model_copy(update={"label": _clip(row.label, text_cap), "reason": _clip(row.reason, text_cap)})
                        for row in view.plan.rows
                    ],
                }
            )
    return view.model_copy(update=update)


def _frame_bytes(snapshot: dict[str, JsonValue]) -> int:
    return len(encode(StateSnapshotEvent(snapshot=snapshot)).encode("utf-8"))


def fit_view(view: RunView) -> dict[str, JsonValue]:
    """Serialize the view so its whole SSE frame stays within 32 KB, trimming summaries first.

    The run record keeps the untrimmed text; the snapshot is only what fits on screen.
    """
    attempts: tuple[tuple[int, int | None], ...] = ((240, None), (80, None), (24, None), (0, None), (0, 200), (0, 60))
    for candidate in (view, *(_trimmed(view, summary, text) for summary, text in attempts)):
        snapshot: dict[str, JsonValue] = candidate.model_dump(mode="json")
        if _frame_bytes(snapshot) <= SNAPSHOT_LIMIT_BYTES:
            return snapshot
    raise SnapshotTooLarge(f"run {view.run_id} revision {view.revision} exceeds {SNAPSHOT_LIMIT_BYTES} bytes")
