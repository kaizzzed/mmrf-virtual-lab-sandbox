"""Compose, verify, re-compose once: the Answering step's model half (RFA rev 25, A2 topology; C-02).

The controller calls `write_answer` once every step has finished (Limited and Partial runs skip
it), inside `calls.model_call_scope(run_id, existing=record.model_calls)`:

1. The composer writes, with the verifier's allow-list (`Verifier.allow_list`).
2. The verifier checks; a model-text failure on the first attempt re-composes once with the
   failed findings as feedback, and the second attempt's verdict is final (its failing
   sentences are dropped). If the re-compose itself declines or fails, the first verdict stands.
3. V-COMP and V-LIM insert into Observed and Limitations on every path, the declined ones too,
   because Observed and Limitations are shown whatever the model does.

The outcome is Complete with all four parts, or ObservedOnly with `withheld_reason` when both
models decline (code MODEL_DECLINED and a limitation naming the models), when the composer fails
(the error's plain reason), or when no Interpretation sentence survives verification.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from ..compiler import CompiledPlan, SessionLedger
from ..interfaces import Composer, ComposerResult, ObservedResult, RuleFinding, Verifier, VerifierResult
from ..models import Code, FactRecord, FollowUp, Limitation, ModelCallRecord, NearestApp, Sentence
from ..vocab import Vocabulary
from .errors import ModelRoleError
from .verifier import check_observed, model_text_feedback

log = logging.getLogger("orion.model")
WITHHELD_UNVERIFIED: Final = "no interpretation sentence passed verification"
WITHHELD_UNSCHEMA: Final = "the interpretation did not match its schema"


@dataclass(frozen=True)
class AnswerOutcome:
    status: Literal["Complete", "ObservedOnly"]
    code: Code | None
    # Null when withheld (ObservedOnly); a dropped sentence stays as a marker in a Complete answer.
    interpretation: list[Sentence] | None
    hypotheses: list[Sentence] | None
    follow_ups: list[FollowUp]
    open_links: list[NearestApp]
    withheld_reason: str | None
    # Code-written additions (V-COMP, V-LIM): append them to the view's Observed and Limitations.
    inserted_observed: list[Sentence]
    inserted_limitations: list[Limitation]
    model_calls: list[ModelCallRecord]
    verifier_results: list[VerifierResult] = field(default_factory=list)
    findings: list[RuleFinding] = field(default_factory=list)
    serving_model: str | None = None


def _declined_limitation(reason: str) -> Limitation:
    return Limitation(
        code="MODEL_DECLINED",
        text=f"{reason[:1].upper()}{reason[1:]}, so the answer shows the observed results only.",
        unblock="Ask again, or rephrase the question.",
    )


def _withheld(
    reason: str,
    code: Code | None,
    calls: list[ModelCallRecord],
    facts: Sequence[FactRecord],
    plan: CompiledPlan,
    observed: ObservedResult,
    *,
    extra: Sequence[Limitation] = (),
    results: Sequence[VerifierResult] = (),
    serving_model: str | None = None,
) -> AnswerOutcome:
    findings, sentences, limitations = check_observed(facts, plan, observed)
    return AnswerOutcome(
        status="ObservedOnly", code=code, interpretation=None, hypotheses=None, follow_ups=[], open_links=[],
        withheld_reason=reason, inserted_observed=sentences, inserted_limitations=[*limitations, *extra],
        model_calls=calls, verifier_results=list(results), findings=findings, serving_model=serving_model,
    )


def _from_verdict(result: VerifierResult, calls: list[ModelCallRecord], results: list[VerifierResult], serving: str | None) -> AnswerOutcome:
    if result.observed_only:
        return AnswerOutcome(
            status="ObservedOnly", code=None, interpretation=None, hypotheses=None, follow_ups=[], open_links=[],
            withheld_reason=WITHHELD_UNVERIFIED, inserted_observed=result.inserted_observed,
            inserted_limitations=result.inserted_limitations, model_calls=calls, verifier_results=results,
            findings=result.findings, serving_model=serving,
        )
    return AnswerOutcome(
        status="Complete", code=None, interpretation=result.interpretation, hypotheses=result.hypotheses,
        follow_ups=result.follow_ups, open_links=result.open_links, withheld_reason=None,
        inserted_observed=result.inserted_observed, inserted_limitations=result.inserted_limitations, model_calls=calls,
        verifier_results=results, findings=result.findings, serving_model=serving,
    )


async def write_answer(
    composer: Composer,
    verifier: Verifier,
    *,
    question: str,
    facts: Sequence[FactRecord],
    plan: CompiledPlan,
    observed: ObservedResult,
    ledger: SessionLedger | None,
    vocab: Vocabulary,
) -> AnswerOutcome:
    """Interpretation and Hypotheses for a run whose steps all finished; never raises ModelRoleError."""
    allow_list = verifier.allow_list(vocab, ledger, plan)
    calls: list[ModelCallRecord] = []

    async def compose(feedback: Sequence[RuleFinding]) -> ComposerResult | ModelRoleError:
        try:
            result = await composer.compose(question, facts, plan, observed, allow_list=allow_list, ledger=ledger, feedback=feedback)
        except ModelRoleError as error:
            calls.extend(error.model_calls)
            return error
        calls.extend(result.model_calls)
        return result

    first = await compose(())
    if isinstance(first, ModelRoleError):
        return _withheld(first.reason, None, calls, facts, plan, observed)
    if first.declined or first.output is None:
        reason = first.decline_reason or "the model declined to interpret these results"
        if reason == WITHHELD_UNSCHEMA:
            return _withheld(reason, None, calls, facts, plan, observed, serving_model=first.serving_model)
        return _withheld(reason, "MODEL_DECLINED", calls, facts, plan, observed, extra=[_declined_limitation(reason)])

    def verify(result: ComposerResult, attempt: int) -> VerifierResult:
        assert result.output is not None
        return verifier.verify(
            result.output, facts, ledger, vocab, plan=plan, observed=observed, attempt=attempt, allow_list=allow_list
        )

    verdict = verify(first, 1)
    results = [verdict]
    serving = first.serving_model
    if verdict.retry:
        feedback = model_text_feedback(verdict)
        log.info("re-compose after %s", finding_summary(feedback))
        second = await compose(feedback)
        if isinstance(second, ComposerResult) and not second.declined and second.output is not None:
            verdict = verify(second, 2)
            results.append(verdict)
            serving = second.serving_model
            if failed := model_text_feedback(verdict):
                log.info("dropped after the re-compose: %s", finding_summary(failed))
    return _from_verdict(verdict, calls, results, serving)


def finding_summary(findings: Sequence[RuleFinding], *, detail_chars: int = 160) -> str:
    """Failed findings for a log line: rule, part and index, and the start of the detail."""
    return "; ".join(
        f"{f.rule} {f.part}[{'' if f.index is None else f.index}]: {f.detail[:detail_chars]}" for f in findings if not f.passed
    )
