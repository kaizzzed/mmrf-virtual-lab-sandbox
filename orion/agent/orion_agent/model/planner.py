"""The planner role on Bedrock (RFA rev 25, A2 "Planning: from question to signed plan", A4).

Claude Opus 5.5 at effort low turns the question, the cohort context and the session ledger into
the IR (`ir.PlannerOutput`) through structured output, behind a cached prefix of rules,
vocabulary, catalog and examples. Then code:

- validates the reply against the full IR (constraints the API schema cannot carry included) and
  allows one repair call when it fails (A2);
- keeps only what the question states (C-12): a DE method, a ranking or a top N the question
  does not name is dropped, so the compiler's defaults and assumptions apply and both phrasings
  of a journey sign the same plan;
- holds a clarification's answer fixed: a survival split the researcher picked is the split.

Both models declining gives `declined` (the run ends Failed, PLANNER_DECLINED: "Could not plan").
Any other failure raises ModelRoleError (INTERNAL_ERROR) carrying every call made.
"""

import json
import re
import time
from typing import Final

from pydantic import JsonValue

from .. import ir
from ..compiler import SessionLedger
from ..interfaces import Clarification, PlannerResult
from ..models import CohortContext, ModelCallRecord
from ..vocab import Vocabulary
from .client import CallResult, ModelClient, ModelRequest
from .config import WARMUP_MAX_TOKENS, ModelConfig
from .errors import ModelRoleError
from .lexicon import NUMBER_WORDS, model_display_name
from .prompts import RepairNote, planner_system_prefix, planner_user_message
from .schema import PLANNER_OUTPUT_KEY, planner_output_schema
from .warmup import WarmUpResult, warm_up_result

_METHOD_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "wilcoxon": ("wilcoxon", "rank-sum", "rank sum", "mann-whitney"),
    "edgeR": ("edger",),
    "limma": ("limma", "voom"),
}
_RANK_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "adjusted_p": ("adjusted", "fdr", "q-value", "q value", "padj", "benjamini"),
    "p_value": ("p-value", "p value", "pvalue", "raw p", "nominal p", "unadjusted"),
    "fold_change": ("fold change", "fold-change", "log2fc", "effect size", "logfc"),
}
_NUMBER: Final = re.compile(r"(?<![A-Za-z])[0-9]+(?![0-9])")
WARMUP_QUESTION: Final = "Warm-up call: how many cases are in the active cohort?"


def _states_a_number(question: str) -> bool:
    words = re.findall(r"[a-z]+", question.casefold())
    return bool(_NUMBER.search(question)) or any(word in NUMBER_WORDS for word in words)


def _mentions(question: str, words: tuple[str, ...]) -> bool:
    folded = question.casefold()
    return any(word in folded for word in words)


def keep_stated(output: ir.PlannerOutput, question: str) -> ir.PlannerOutput:
    """C-12: drop settings the question does not state, so the compiler's defaults apply."""
    if not isinstance(output, ir.Plan):
        return output
    analyses: list[ir.SurvivalCompare | ir.TopMutatedGenes | ir.DifferentialExpression | ir.CohortSize] = []
    numbered = _states_a_number(question)
    for analysis in output.analyses:
        if isinstance(analysis, ir.DifferentialExpression):
            update: dict[str, object] = {}
            if analysis.method is not None and not _mentions(question, _METHOD_WORDS[analysis.method]):
                update["method"] = None
            if analysis.rank_by is not None and not _mentions(question, _RANK_WORDS[analysis.rank_by]):
                update["rank_by"] = None
            if analysis.top_n is not None and not numbered:
                update["top_n"] = None
            analyses.append(analysis.model_copy(update=update) if update else analysis)
        elif isinstance(analysis, ir.TopMutatedGenes) and analysis.n is not None and not numbered:
            analyses.append(analysis.model_copy(update={"n": None}))
        else:
            analyses.append(analysis)
    return output.model_copy(update={"analyses": analyses})


def hold_clarification(output: ir.PlannerOutput, clarification: Clarification | None) -> ir.PlannerOutput:
    """The researcher's answer is a fixed slot: a picked survival split replaces the model's."""
    if clarification is None or clarification.field is None or clarification.slot != "split_term":
        return output
    if not isinstance(output, ir.Plan):
        return output
    analyses: list[ir.SurvivalCompare | ir.TopMutatedGenes | ir.DifferentialExpression | ir.CohortSize] = []
    for analysis in output.analyses:
        if isinstance(analysis, ir.SurvivalCompare) and analysis.groups is None and analysis.split_term != clarification.field:
            analysis = analysis.model_copy(update={"split_term": clarification.field, "split_values": None})
        analyses.append(analysis)
    return output.model_copy(update={"analyses": analyses})


def parse_planner_reply(text: str) -> ir.PlannerOutput:
    data = json.loads(text)
    if not isinstance(data, dict) or PLANNER_OUTPUT_KEY not in data:
        raise ValueError(f"the reply is not an object with {PLANNER_OUTPUT_KEY!r}")
    payload: JsonValue = data[PLANNER_OUTPUT_KEY]
    return ir.PLANNER_OUTPUT.validate_python(payload)


def decline_reason(result: CallResult[ir.PlannerOutput], fallback_model: str | None) -> str:
    models = [model_display_name(m) for m in result.declined_models]
    if fallback_model and fallback_model not in result.declined_models and result.declined_models:
        return f"{models[0]} declined to plan this question, and the fallback model could not be reached."
    if len(models) >= 2:
        return f"{models[0]} and its fallback, {models[-1]}, declined to plan this question."
    if models:
        return f"{models[0]} declined to plan this question."
    return "The model declined to plan this question."


class BedrockPlanner:
    """interfaces.Planner on Bedrock. Run its calls inside `calls.model_call_scope(run_id, ...)`."""

    def __init__(self, client: ModelClient, config: ModelConfig, vocab: Vocabulary) -> None:
        self._client = client
        self._config = config
        self._prefix = planner_system_prefix(vocab)
        self._schema = planner_output_schema()

    @property
    def system_prefix(self) -> str:
        return self._prefix

    def _request(self, user_text: str, *, max_tokens: int | None = None) -> ModelRequest:
        return ModelRequest(
            role="planner",
            model=self._config.planner_model,
            effort=self._config.planner_effort,
            system_prefix=self._prefix,
            user_text=user_text,
            schema=self._schema,
            max_tokens=max_tokens or self._config.max_tokens,
            timeout_s=self._config.planner_timeout_s,
            cache_ttl=self._config.cache_ttl,
        )

    async def plan(
        self,
        question: str,
        cohort_context: CohortContext,
        ledger: SessionLedger | None,
        *,
        clarification: Clarification | None = None,
    ) -> PlannerResult:
        calls: list[ModelCallRecord] = []

        def finish(result: CallResult[ir.PlannerOutput], repaired: bool) -> PlannerResult | None:
            if result.refused:
                return PlannerResult(
                    output=None, model_calls=calls, serving_model=None, declined=True,
                    decline_reason=decline_reason(result, self._config.fallback_model), repaired=repaired,
                )
            if result.value is None:
                return None
            output = hold_clarification(keep_stated(result.value, question), clarification)
            return PlannerResult(
                output=output, model_calls=calls, serving_model=result.served_model, declined=False,
                decline_reason=None, repaired=repaired,
            )

        user = planner_user_message(question, cohort_context, ledger, clarification)
        try:
            first = await self._client.call(self._request(user), parse_planner_reply)
            calls += first.records
            done = finish(first, repaired=False)
            if done is not None:
                return done
            # One repair call when the IR fails validation (A2).
            note = RepairNote(previous_output=first.text or "", errors=first.invalid_detail or "")
            repair_user = planner_user_message(question, cohort_context, ledger, clarification, repair=note)
            second = await self._client.call(self._request(repair_user), parse_planner_reply)
            calls += second.records
        except ModelRoleError as error:
            raise error.with_calls(calls + error.model_calls) from error.__cause__
        done = finish(second, repaired=True)
        if done is not None:
            return done
        return PlannerResult(
            output=None, model_calls=calls, serving_model=second.served_model, declined=True,
            decline_reason="The planner's answer did not match the Analysis Request schema, even after one repair.",
            repaired=True,
        )

    async def warm_up(self) -> WarmUpResult:
        """A small real call at the planner's effort, writing the cached prefix (C-41). Not recorded."""
        context = CohortContext.model_validate(
            {"active": {"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": None}, "saved": []}
        )
        user = planner_user_message(WARMUP_QUESTION, context, None)
        started = time.monotonic()
        return await warm_up_result(
            "planner", self._config.planner_model, self._config.planner_effort, started,
            self._client.call(self._request(user, max_tokens=WARMUP_MAX_TOKENS), parse_planner_reply, record=False),
        )
