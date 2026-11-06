"""Builders for the AG-UI events ORION emits, over the ag_ui.core 1.0.0 classes."""

from collections.abc import Iterator
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
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder
from pydantic import JsonValue

from .models import (
    SNAPSHOT_LIMIT_BYTES,
    Code,
    ConfirmPending,
    FinishStatus,
    RunView,
    SaveCohortsArgs,
    save_cohorts_args_chunks,
    text_message_chunks,
)

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


def save_cohorts_call(args: SaveCohortsArgs) -> list[BaseEvent]:
    """The confirmation pause's tool call: TOOL_CALL_START, the args JSON in chunks of at most 8,192
    characters, TOOL_CALL_END; the toolCallId is the pending nonce (contract "save_cohorts tool call")."""
    frames: list[BaseEvent] = [ToolCallStartEvent(tool_call_id=args.nonce, tool_call_name="save_cohorts")]
    frames += [ToolCallArgsEvent(tool_call_id=args.nonce, delta=delta) for delta in save_cohorts_args_chunks(args)]
    frames.append(ToolCallEndEvent(tool_call_id=args.nonce))
    return frames


def text_message(message_id: str, markdown: str) -> list[BaseEvent]:
    """The verified answer as markdown, in deltas of at most 8,192 characters (messageId `<runId>.v<n>`)."""
    frames: list[BaseEvent] = [TextMessageStartEvent(message_id=message_id, role="assistant")]
    frames += [TextMessageContentEvent(message_id=message_id, delta=delta) for delta in text_message_chunks(markdown)]
    frames.append(TextMessageEndEvent(message_id=message_id))
    return frames


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


def _json_size(value: object) -> int:
    return 0 if value is None else len(str(value))


def _candidates(view: RunView) -> Iterator[RunView]:
    """The contract's trim order ("Size: 32 KB"), each candidate trimming a little more than the last.

    Step summaries (clipped to 240, 80, 24, then 0 characters); then a pending confirm cohort's
    definition, set to null, largest first; then a viz spec's pinned inputs, set to null, largest
    first; then the question, paraphrase, row labels and reasons, assumption and limitation texts
    (200, then 60 characters); last, fact labels, set to "". Nothing else is ever trimmed.
    """
    yield view
    for cap in (240, 80, 24, 0):
        yield _trimmed(view, cap, None)
    current = _trimmed(view, 0, None)
    confirm = current.pending
    if isinstance(confirm, ConfirmPending):
        sizes = [_json_size(c.definition.model_dump_json() if c.definition else None) for c in confirm.cohorts]
        for index in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
            if confirm.cohorts[index].definition is None:
                continue
            cohorts = list(confirm.cohorts)
            cohorts[index] = cohorts[index].model_copy(update={"definition": None})
            confirm = confirm.model_copy(update={"cohorts": cohorts})
            current = current.model_copy(update={"pending": confirm})
            yield current
    sizes = [_json_size(s.pinned.model_dump_json() if s.pinned else None) for s in current.viz_specs]
    for index in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
        if current.viz_specs[index].pinned is None:
            continue
        specs = list(current.viz_specs)
        specs[index] = specs[index].model_copy(update={"pinned": None})
        current = current.model_copy(update={"viz_specs": specs})
        yield current
    last = current
    for text in (200, 60):
        last = _trimmed(current, 0, text)
        yield last
    yield last.model_copy(update={"facts": [f.model_copy(update={"label": ""}) for f in last.facts]})


def fit_view(view: RunView) -> dict[str, JsonValue]:
    """Serialize the view so its whole SSE frame stays within 32 KB, trimming in the contract's order.

    The run record keeps the untrimmed view (the BFF view route serves it); the snapshot is only
    what fits. A null pending definition is read from the save_cohorts args or the view route, a
    null `pinned` from the view route.
    """
    for candidate in _candidates(view):
        snapshot: dict[str, JsonValue] = candidate.model_dump(mode="json")
        if _frame_bytes(snapshot) <= SNAPSHOT_LIMIT_BYTES:
            return snapshot
    raise SnapshotTooLarge(f"run {view.run_id} revision {view.revision} exceeds {SNAPSHOT_LIMIT_BYTES} bytes")
