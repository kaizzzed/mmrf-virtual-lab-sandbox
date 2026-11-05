"""The composer for mode L without Bedrock (ORION_COMPOSER=stub).

Deterministic text from the plan's shape and the run's facts, written to the same rules the
verifier applies to Claude's: numbers and gene symbols only through fact placeholders, cites on
every statement, hedged wording, hypotheses as questions, follow-ups only from analysis kinds the
compiler runs (C-06). J1, J2 and J3 therefore reach Complete with all four parts and pass the
verifier, so the walking skeleton exercises the answer path end to end. The serving model is
recorded as "stub": one ModelCallRecord per compose when a run scope is active, with no tokens.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from ..catalog import AppId
from ..compiler import CompiledPlan, SessionLedger, StepSpec
from ..interfaces import (
    ComposerFollowUp,
    ComposerHypothesis,
    ComposerOutput,
    ComposerResult,
    ComposerStatement,
    ObservedResult,
    RuleFinding,
)
from ..models import MAX_TOP_N_DE, FactRecord, ModelCallRecord
from ..tools import schemas
from .calls import current_scope, iso_now
from .config import Effort
from .lexicon import json_list
from .warmup import WarmUpResult

STUB_MODEL: Final = "stub"
# Survival splits with a ProteinPaint twin, offered as the next survival follow-up (vocab_supplement.json).
_SURVIVAL_SPLITS: Final = (
    ("demographic.gender", "gender"),
    ("demographic.race", "race"),
    ("cgs_risk_key_criteria.cgs_risk_category", "CGS risk category"),
)
_RANK_PHRASES: Final[dict[str, str]] = {
    "adjusted_p_value": "the smallest adjusted p-value",
    "original_p_value": "the smallest p-value",
    "abs(fold_change)": "the largest absolute log2FC",
}
MAX_TEXT: Final = 300


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass
class _Draft:
    facts: Mapping[str, FactRecord]
    interpretation: list[ComposerStatement] = field(default_factory=list)
    hypotheses: list[ComposerHypothesis] = field(default_factory=list)
    follow_ups: list[ComposerFollowUp] = field(default_factory=list)
    apps: list[AppId] = field(default_factory=list)

    def has(self, *fact_ids: str) -> bool:
        return all(f in self.facts for f in fact_ids)

    def say(self, text: str, cites: Sequence[str]) -> None:
        cited = [c for c in dict.fromkeys(cites) if c in self.facts]
        if cited and len(text) <= MAX_TEXT:
            self.interpretation.append(ComposerStatement(text=text, cites=cited))

    def ask(self, text: str, cites: Sequence[str]) -> None:
        cited = [c for c in dict.fromkeys(cites) if c in self.facts]
        if cited and len(text) <= MAX_TEXT:
            self.hypotheses.append(ComposerHypothesis(text=text, cites=cited, form="question"))

    def follow(self, follow_up: ComposerFollowUp) -> None:
        if all(f.question != follow_up.question for f in self.follow_ups):
            self.follow_ups.append(follow_up)

    def open(self, app_id: AppId) -> None:
        if app_id not in self.apps:
            self.apps.append(app_id)


def _step(plan: CompiledPlan, step_id: object) -> StepSpec | None:
    return next((s for s in plan.steps if s.id == step_id), None)


def _survival(plan: CompiledPlan, draft: _Draft) -> None:
    for step in (s for s in plan.steps if s.tool == "survival_compare"):
        groups = [g for g in json_list(step.args.get("groups")) if isinstance(g, dict)]
        labels = [str(g.get("label", "")) for g in groups]
        keys = schemas.group_keys(labels) if labels else []
        p = f"f.{step.id}.p"
        counts = [f"f.{dep}.count" for dep in step.depends_on]
        if draft.has(p):
            draft.say(f"In this synthetic cohort, the log-rank test comparing the survival curves gives p = {{{{{p}}}}}.", [p])
        median_step = next((s for s in plan.steps if s.tool == "derive_km_median" and s.args.get("curves") == step.id), None)
        if median_step is not None:
            reached = [(label, f"f.{median_step.id}.median.{key}") for label, key in zip(labels, keys)]
            reached = [(label, fid) for label, fid in reached if draft.has(fid)]
            if reached:
                parts = [f"{{{{{fid}}}}} days in the {label} group" for label, fid in reached]
                draft.say(f"Median overall survival is {_join(parts)}.", [fid for _, fid in reached])
        draft.say("This compares the groups of a synthetic dataset only and carries no clinical meaning.", counts or [p])
        if draft.has(p):
            draft.ask("Does the difference in survival hold within other clinical subgroups of this cohort?", [p])
        split = step.args.get("split")
        field_id = split.get("field") if isinstance(split, dict) else None
        other = next((label for fid, label in _SURVIVAL_SPLITS if fid != field_id), None)
        if other is not None:
            draft.follow(ComposerFollowUp(kind="survival_compare", label=f"Survival by {other}", question=f"Compare survival by {other}"))
        draft.follow(
            ComposerFollowUp(
                kind="top_mutated_genes", label="Top mutated genes in this cohort",
                question="Which genes are most frequently mutated in this cohort?",
            )
        )
        draft.open("CohortComparison")


def _ranking_source(plan: CompiledPlan, ranking: StepSpec) -> StepSpec | None:
    return _step(plan, ranking.args.get("rows"))


def _genes(plan: CompiledPlan, draft: _Draft) -> None:
    for ranking in (s for s in plan.steps if s.tool == "rank_top_n"):
        source = _ranking_source(plan, ranking)
        if source is None or source.tool != "top_mutated_genes":
            continue
        gene1, cases1 = f"f.{ranking.id}.top.1.gene", f"f.{ranking.id}.top.1.cases"
        gene2 = f"f.{ranking.id}.top.2.gene"
        if not draft.has(gene1):
            continue
        draft.say(
            f"Among the profiled panel genes, {{{{{gene1}}}}} is mutated in the most cases of this cohort; "
            "mutation frequency alone does not show a driver role.",
            [gene1, cases1],
        )
        if draft.has(gene2):
            draft.say(f"{{{{{gene1}}}}} and {{{{{gene2}}}}} lead the ranking, which covers the six-gene panel only.", [gene1, gene2])
        draft.ask(f"Is {{{{{gene1}}}}} mutated as often in the cases outside this cohort?", [gene1])
        draft.follow(
            ComposerFollowUp(
                kind="survival_compare", label=f"Survival by {{{{{gene1}}}}} mutation",
                question=f"Compare survival between {{{{{gene1}}}}}-mutated and other mutation-tested cases",
            )
        )
        draft.open("MutationFrequency")


def _de_names(plan: CompiledPlan, de: StepSpec, observed: ObservedResult) -> tuple[str, str] | None:
    for spec in observed.viz_specs:
        if spec.launcher in ("deDirect", "deInput") and de.id in spec.cites_steps and spec.pinned is not None:
            names = [getattr(g, "name", None) for g in getattr(spec.pinned, "groups", None) or []]
            if len(names) == 2 and all(isinstance(n, str) for n in names):
                return str(names[0]), str(names[1])
    names = [g.get("name") for g in json_list(de.args.get("groups")) if isinstance(g, dict)]
    if len(names) == 2 and all(isinstance(n, str) for n in names):
        return str(names[0]), str(names[1])
    return None


def _de(plan: CompiledPlan, observed: ObservedResult, draft: _Draft) -> None:
    for ranking in (s for s in plan.steps if s.tool == "rank_top_n"):
        de = _ranking_source(plan, ranking)
        if de is None or de.tool != "de_run":
            continue
        names = _de_names(plan, de, observed)
        gene1, fc1 = f"f.{ranking.id}.top.1.gene", f"f.{ranking.id}.top.1.log2fc"
        significant, tested = f"f.{de.id}.significant", f"f.{de.id}.tested"
        rank_by = str(ranking.args.get("rankBy", "adjusted_p_value"))
        if draft.has(gene1) and names is not None:
            control, case = names
            fold = draft.facts.get(fc1)
            value = fold.value if fold is not None else None
            direction = ""
            if isinstance(value, (int, float)) and value > 0:
                direction = f", with higher expression in {case} than in {control}"
            elif isinstance(value, (int, float)) and value < 0:
                direction = f", with lower expression in {case} than in {control}"
            rank = _RANK_PHRASES.get(rank_by, "the top rank")
            cites = [gene1, fc1, f"f.{ranking.id}.top.1.padj", f"f.{ranking.id}.top.1.p"]
            draft.say(f"In this synthetic dataset, {{{{{gene1}}}}} has {rank} between the groups{direction}.", cites)
        if draft.has(significant, tested):
            draft.say(
                f"{{{{{significant}}}}} of {{{{{tested}}}}} tested genes passed the thresholds; differential expression alone "
                "does not establish a mechanism, and these results carry no clinical meaning.",
                [significant, tested],
            )
        if draft.has(gene1):
            draft.ask(f"Is {{{{{gene1}}}}} expression also associated with survival in these cohorts?", [gene1])
        n = ranking.args.get("n")
        if isinstance(n, int) and 2 * n <= MAX_TOP_N_DE:
            draft.follow(
                ComposerFollowUp(kind="differential_expression", label=f"Show the top {2 * n}", question=f"Show the top {2 * n} genes instead")
            )
        gene = next((s.args.get("gene") for s in plan.steps if s.tool == "gene_cases"), None)
        if names is not None and isinstance(gene, str):
            control, case = names
            draft.follow(
                ComposerFollowUp(
                    kind="survival_compare", label=f"Survival by {gene} status",
                    question=f"Compare survival between the {case} and {control} cohorts",
                )
            )
        draft.open("GeneExpression")


def _counts(plan: CompiledPlan, draft: _Draft) -> None:
    count = next((f"f.{s.id}.count" for s in plan.steps if draft.has(f"f.{s.id}.count")), None) or (
        "f.s1.cases" if draft.has("f.s1.cases") else None
    )
    if count is None:
        return
    draft.say("This count describes a synthetic cohort only and carries no clinical meaning.", [count])
    draft.ask("Would a narrower cohort change this count?", [count])
    draft.follow(
        ComposerFollowUp(
            kind="top_mutated_genes", label="Top mutated genes in this cohort", question="Which genes are most frequently mutated in this cohort?"
        )
    )
    draft.open("cDave")


def stub_output(plan: CompiledPlan, facts: Sequence[FactRecord], observed: ObservedResult) -> ComposerOutput | None:
    """The stub's answer for this plan and these facts, or None when there is nothing to cite."""
    draft = _Draft(facts={f.id: f for f in facts})
    _survival(plan, draft)
    _genes(plan, draft)
    _de(plan, observed, draft)
    if not draft.interpretation:
        _counts(plan, draft)
    if not draft.interpretation:
        return None
    return ComposerOutput(
        interpretation=draft.interpretation[:4],
        hypotheses=draft.hypotheses[:3],
        follow_ups=draft.follow_ups[:3],
        open_app_ids=draft.apps[:3],
    )


class StubComposer:
    """interfaces.Composer without a model: deterministic, placeholder-only text."""

    def __init__(self, *, effort: Effort = "medium") -> None:
        self._effort: Effort = effort

    async def compose(
        self,
        question: str,
        facts: Sequence[FactRecord],
        plan: CompiledPlan,
        observed: ObservedResult,
        *,
        allow_list: Sequence[str],
        ledger: SessionLedger | None,
        feedback: Sequence[RuleFinding] = (),
    ) -> ComposerResult:
        started, at = time.monotonic(), iso_now()
        output = stub_output(plan, facts, observed)
        calls: list[ModelCallRecord] = []
        scope = current_scope()
        if scope is not None:
            calls.append(
                ModelCallRecord(
                    call_id=scope.next_call_id("composer", plan.version), role="composer", model=STUB_MODEL, fallback=False,
                    effort=self._effort, attempt=1, started_at=at, elapsed_ms=int((time.monotonic() - started) * 1000),
                    outcome="ok" if output is not None else "refused", stop_reason=None, input_tokens=None, output_tokens=None,
                    cache_read_tokens=None, cache_write_tokens=None,
                )
            )
            scope.add(calls)
        if output is None:
            return ComposerResult(
                output=None, model_calls=calls, serving_model=STUB_MODEL, declined=True,
                decline_reason="there is no fact to interpret",
            )
        return ComposerResult(output=output, model_calls=calls, serving_model=STUB_MODEL, declined=False, decline_reason=None)

    async def warm_up(self) -> WarmUpResult:
        return WarmUpResult("composer", STUB_MODEL, self._effort, "skipped", 0, detail="the stub composer makes no model call")
