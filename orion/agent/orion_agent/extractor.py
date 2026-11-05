"""Extractor: one call's output to facts (RFA rev 25, A2 "Extractor"; TOOLS.md 1 "Facts").

Each fact carries its step, row and tool, the JSON pointer into the call it came from
(`/observed/...`), the callId of the attempt whose result was used, both record hashes and the
engine that produced the value. Ids, pointers, units and labels come from the catalog's fact specs
(`tools.schemas.draft_facts`), so the extractor writes nothing of its own; values are written on
screen only through `models.format_fact`.
"""

from typing import cast

from pydantic import JsonValue

from .compiler import StepSpec
from .models import CallRecord, FactRecord
from .tools import schemas as catalog
from .tools.base import ToolCallOutcome, ToolName


class ExtractionError(ValueError):
    """A successful call whose output does not carry the facts its catalog entry promises."""


def _engine(tool: str) -> str | None:
    try:
        return catalog.tool_spec(tool).engine
    except KeyError:
        return None


class FactExtractor:
    """interfaces.Extractor over the catalog's fact specs.

    `context` names what a label needs beyond the output (the cohort counted, the DE group labels,
    the ranking's source and how many ranked rows the view may still show).
    """

    def extract(
        self,
        step: StepSpec,
        outcome: ToolCallOutcome,
        *,
        plan_version: int,
        context: catalog.FactContext = catalog.FactContext(),
    ) -> list[FactRecord]:
        if outcome.result.status not in ("ok", "empty") or outcome.response_sha256 is None:
            return []
        last = outcome.attempts[-1]
        return self.from_output(
            step,
            outcome.result.observed,
            plan_version=plan_version,
            call_id=last.call_id,
            request_sha256=outcome.request_sha256,
            response_sha256=outcome.response_sha256,
            context=context,
        )

    def from_call(self, step: StepSpec, call: CallRecord, *, context: catalog.FactContext = catalog.FactContext()) -> list[FactRecord]:
        """Facts of a recorded call: pure functions and client writes are recorded as CallRecords directly."""
        if call.status not in ("ok", "empty") or call.response_sha256 is None:
            return []
        return self.from_output(
            step,
            call.observed,
            plan_version=call.plan_version,
            call_id=call.call_id,
            request_sha256=call.request_sha256,
            response_sha256=call.response_sha256,
            context=context,
        )

    def from_output(
        self,
        step: StepSpec,
        observed: JsonValue,
        *,
        plan_version: int,
        call_id: str,
        request_sha256: str,
        response_sha256: str,
        context: catalog.FactContext,
    ) -> list[FactRecord]:
        if not isinstance(observed, dict):
            raise ExtractionError(f"{step.id} {step.tool}: the output is not an object")
        if step.tool not in catalog.TOOL_REGISTRY:
            return []
        try:
            drafts = catalog.draft_facts(cast(ToolName, step.tool), step.id, observed, context)
        except (KeyError, ValueError, LookupError) as error:
            raise ExtractionError(f"{step.id} {step.tool}: {error}") from error
        engine = _engine(step.tool)
        return [
            FactRecord(
                id=draft.id,
                label=draft.label,
                value=draft.value,
                unit=draft.unit,
                step_id=step.id,
                row=step.row,
                tool=step.tool,
                json_pointer=draft.json_pointer,
                plan_version=plan_version,
                call_id=call_id,
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                engine=engine,
            )
            for draft in drafts
        ]
