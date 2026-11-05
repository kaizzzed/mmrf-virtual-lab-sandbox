"""Observed renderer: code templates filled from facts (RFA rev 25, A2 "Observed renderer"; TOOLS.md 7, 8).

Observed and Limitations are code-written and reach the screen before the composer runs (C-22).
Every tool-returned number is a fact segment whose text is `models.format_fact(value, unit)`; a
template applies only when every fact it cites exists, so a Partial, Limited, Cancelled or
Interrupted run renders exactly the sentences its finished steps support. A zero is reported as a
zero, with the filter used and both possible causes (C-40). Limitations name what is missing, why,
and what would unblock it; the Unsupported variant names the nearest app (controller).

The renderer reads the analyses off the compiled steps (TOOLS.md 5): a `survival_compare` step and
its count steps and median; a `top_mutated_genes` step and its ranking; a `de_run` step with its
sample lists, pre-analysis and ranking; a "Count ..." row. Viz specs pin the cited call's own
request (`models.derive_request_pinned`), so `pinnedHash` equals `requestPinnedHash`.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

from pydantic import JsonValue

from .compiler import CompiledPlan, StepSpec
from .interfaces import ObservedResult
from .models import (
    FROZEN_ROWS_CAP,
    CallRecord,
    CohortRef,
    Fact,
    FactRecord,
    FrozenTable,
    Limitation,
    NearestApp,
    PinnedKind,
    Segment,
    Sentence,
    StepStatus,
    StepView,
    VizSpec,
    derive_request_pinned,
    format_fact,
    pinned_hash,
)
from .tools import schemas as catalog
from .tools.base import MIN_SURVIVAL_GROUP
from .vocab import Vocabulary

TEMPLATE_VERSION: Final = "observed@1"
DeLauncher = Literal["deDirect", "deInput"]
# Steps whose output is a cohort's size (their `count` fact).
COUNT_TOOLS: Final = frozenset({"cohort_count", "cases_query", "gene_cases", "set_operation"})
FINISHED: Final[frozenset[StepStatus]] = frozenset({"Done", "Empty", "Reused"})
DONE: Final[frozenset[StepStatus]] = frozenset({"Done", "Reused"})
DEFAULT_THRESHOLDS: Final[dict[str, JsonValue]] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}

# Lines the catalog's limitation templates do not cover; the executor and controller write them.
WRITE_VERIFY_FAILED_TEXT: Final = "The saved cohorts did not match the plan when read back ({detail}), so nothing after the save ran."
WRITE_VERIFY_FAILED_UNBLOCK: Final = "Check the cohorts in Cohort Builder, then ask again."
CALL_BUDGET_TEXT: Final = "This run reached its limit of {budget} tool calls, so the remaining steps did not run."
CALL_BUDGET_UNBLOCK: Final = "Ask a narrower question with fewer steps."


def write_verify_failed(detail: str) -> Limitation:
    return Limitation(
        code="WRITE_VERIFY_FAILED", text=WRITE_VERIFY_FAILED_TEXT.format(detail=detail), unblock=WRITE_VERIFY_FAILED_UNBLOCK
    )


def call_budget(budget: int) -> Limitation:
    return Limitation(code="CALL_BUDGET", text=CALL_BUDGET_TEXT.format(budget=budget), unblock=CALL_BUDGET_UNBLOCK)


def bridge_unsafe(field_label: str) -> Limitation:
    template = catalog.TEMPLATES["filter.not_bridge_safe"]
    return Limitation(
        code="UNSUPPORTED_FIELD",
        text=catalog.fill_text(template.pattern, {"field": field_label}),
        unblock=template.unblock,
    )


def save_skipped(cohort_count: int) -> Limitation:
    """The WRITE_SKIPPED line after Don't save: `save.skipped` for DE's two cohorts, `save.skipped_one` for one."""
    template = catalog.TEMPLATES["save.skipped" if cohort_count > 1 else "save.skipped_one"]
    assert template.code is not None
    return Limitation(code=template.code, text=template.pattern, unblock=template.unblock)


def failure_code(call: CallRecord | None) -> str:
    """The ToolErrorCode an attempt's `error` starts with ("TIMEOUT: ..."), or FAILED for an internal error."""
    if call is None or not call.error:
        return "FAILED"
    head = call.error.split(":", 1)[0].strip()
    return head if head in catalog.FAILURE_PHRASES else "FAILED"


def step_refs(value: JsonValue) -> Iterator[catalog.StepRef]:
    """Every StepRef inside a step's args, depth first."""
    if isinstance(value, dict):
        if catalog.is_step_ref(value):
            yield catalog.StepRef.model_validate(value)
            return
        for item in value.values():
            yield from step_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from step_refs(item)


def thresholds_edited(args: Mapping[str, JsonValue]) -> bool:
    thresholds = args.get("thresholds")
    return isinstance(thresholds, dict) and any(thresholds.get(k) != v for k, v in DEFAULT_THRESHOLDS.items())


def answer_markdown(
    observed: Sequence[Sentence],
    interpretation: Sequence[Sentence] | None,
    hypotheses: Sequence[Sentence] | None,
    withheld_reason: str | None,
    limitations: Sequence[Limitation],
    notice: str,
) -> str:
    """TEXT_MESSAGE's markdown: Observed, Interpretation, Hypotheses and next steps, the withheld line,
    Limitations, then the notice; numbers as plain text written by format_fact, no chips."""

    def plain(sentence: Sentence) -> str:
        return "".join(segment.text for segment in sentence.segments)

    def kept(items: Sequence[Sentence] | None) -> list[Sentence]:
        return [s for s in items or () if s.dropped is None]  # a dropped sentence has no text

    lines = ["**Observed**", *(f"- {plain(s)}" for s in observed)]
    if kept(interpretation):
        lines += ["", "**Interpretation**", *(f"- {plain(s)}" for s in kept(interpretation))]
    if kept(hypotheses):
        lines += ["", "**Hypotheses and next steps**", *(f"- {plain(s)}" for s in kept(hypotheses))]
    if withheld_reason:
        lines += ["", f"_Interpretation withheld: {withheld_reason}_"]
    lines += ["", "**Limitations**", *(f"- {item.text}" for item in limitations), "", f"_{notice}_"]
    return "\n".join(lines)


@dataclass
class _Lines:
    """Observed sentences in render order; limitations anchored at a step so they sort in plan order."""

    observed: list[Sentence]
    limitations: list[tuple[int, int, Limitation]]
    viz: list[VizSpec]

    def limit(self, anchor: int, item: Limitation | None) -> None:
        if item is not None:
            self.limitations.append((anchor, len(self.limitations), item))


class _Run:
    """One render's view of the plan, its steps, facts, calls and cohorts."""

    def __init__(
        self,
        plan: CompiledPlan,
        steps: Sequence[StepView],
        facts: Sequence[Fact],
        calls: Sequence[CallRecord],
        cohorts: Sequence[CohortRef],
    ) -> None:
        self.plan = plan
        self.specs = {s.id: s for s in plan.steps}
        self.order = {s.id: i for i, s in enumerate(plan.steps)}
        self.views = {s.id: s for s in steps}
        self.facts: dict[str, Fact] = {f.id: f for f in facts}
        self.calls = calls
        self.cohorts = list(cohorts)

    def status(self, step_id: str | None) -> StepStatus | None:
        view = self.views.get(step_id or "")
        return None if view is None else view.status

    def code(self, step_id: str) -> str | None:
        view = self.views.get(step_id)
        return None if view is None else view.code

    def call(self, step_id: str) -> CallRecord | None:
        """The call whose result the step uses: a Reused step's origin, else this version's last ok or empty attempt."""
        view = self.views.get(step_id)
        version, sid = self.plan.version, step_id
        if view is not None and view.reused_from is not None:
            version, sid = view.reused_from.plan_version, view.reused_from.step_id
        found = [c for c in self.calls if c.plan_version == version and c.step_id == sid and c.status in ("ok", "empty")]
        return found[-1] if found else None

    def last_attempt(self, step_id: str) -> CallRecord | None:
        found = [c for c in self.calls if c.plan_version == self.plan.version and c.step_id == step_id]
        return found[-1] if found else None

    def first(self, tool: str, predicate: "Predicate | None" = None) -> StepSpec | None:
        return next((s for s in self.plan.steps if s.tool == tool and (predicate is None or predicate(s))), None)

    def ref_of(self, value: JsonValue) -> catalog.StepRef | None:
        return next(step_refs(value), None)

    def count_fact(self, step_id: str | None) -> str | None:
        if step_id is None:
            return None
        fact_id = f"f.{step_id}.count"
        return fact_id if fact_id in self.facts else None

    def base(self) -> CohortRef | None:
        return next((c for c in self.cohorts if c.ref == "base"), None)

    def cohort_sized_by(self, step_id: str) -> CohortRef | None:
        fact_id = f"f.{step_id}.count"
        return next((c for c in self.cohorts if c.case_count_fact_id == fact_id), None)

    def value(self, fact_id: str) -> int | float | str | None:
        fact = self.facts.get(fact_id)
        return None if fact is None else fact.value

    def bind(
        self,
        template_id: str,
        *,
        steps: Mapping[str, str] | None = None,
        values: Mapping[str, str] | None = None,
        items: Sequence[tuple[str, Mapping[str, str]]] = (),
    ) -> list[catalog.TemplatePart] | None:
        """The template's parts, or None when a fact it cites does not exist (or a role is unbound)."""
        try:
            parts = catalog.template_parts(catalog.TEMPLATES[template_id], steps=steps, values=values, items=items)
        except (KeyError, ValueError):
            return None
        if any(kind == "fact" and text not in self.facts for kind, text in parts):
            return None
        return parts

    def segments(self, parts: Sequence[catalog.TemplatePart]) -> list[Segment]:
        out: list[Segment] = []
        for kind, text in parts:
            if kind == "fact":
                fact = self.facts[text]
                out.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
            else:
                out.append(Segment(text=text))
        return out

    def chip(self, fact_id: str) -> Segment:
        fact = self.facts[fact_id]
        return Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id)

    def sentence(
        self,
        template_id: str,
        *,
        steps: Mapping[str, str] | None = None,
        values: Mapping[str, str] | None = None,
        items: Sequence[tuple[str, Mapping[str, str]]] = (),
    ) -> Sentence | None:
        parts = self.bind(template_id, steps=steps, values=values, items=items)
        return None if parts is None else Sentence(segments=self.segments(parts))

    def limitation(
        self,
        template_id: str,
        *,
        steps: Mapping[str, str] | None = None,
        values: Mapping[str, str] | None = None,
        items: Sequence[tuple[str, Mapping[str, str]]] = (),
        nearest: Sequence[NearestApp] = (),
    ) -> Limitation | None:
        template = catalog.TEMPLATES[template_id]
        parts = self.bind(template_id, steps=steps, values=values, items=items)
        if parts is None or template.code is None:
            return None
        cites = any(kind == "fact" for kind, _ in parts)
        texts = {k: format_fact(f.value, f.unit) for k, f in self.facts.items()}
        unblock = None if template.unblock is None else catalog.fill_text(template.unblock, values or {})
        line = Limitation(code=template.code, text=catalog.template_text(parts, texts), unblock=unblock, nearest_apps=list(nearest))
        # `segments` is present exactly when the line cites a fact (an absent optional is omitted, never null).
        return line.model_copy(update={"segments": self.segments(parts)}) if cites else line

    def text(self, template_id: str, values: Mapping[str, str] | None = None) -> str:
        parts = catalog.template_parts(catalog.TEMPLATES[template_id], values=values)
        return "".join(text for _, text in parts)

    def shown_rows(self, ranking: str) -> int:
        n = 0
        while f"f.{ranking}.top.{n + 1}.gene" in self.facts:
            n += 1
        return n


class Predicate(Protocol):
    def __call__(self, step: StepSpec) -> bool: ...


def _arg_str(args: Mapping[str, JsonValue], key: str, default: str = "") -> str:
    value = args.get(key)
    return value if isinstance(value, str) else default


def _arg_int(args: Mapping[str, JsonValue], key: str) -> int | None:
    value = args.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _groups(args: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    value = args.get("groups")
    return [g for g in value if isinstance(g, dict)] if isinstance(value, list) else []


class TemplateRenderer:
    """interfaces.ObservedRenderer over the catalog's templates (TOOLS.md 7) and viz specs (TOOLS.md 8)."""

    def __init__(self, vocab: Vocabulary | None = None, *, de_launcher: DeLauncher = "deDirect") -> None:
        self.vocab = vocab
        self.de_launcher = de_launcher

    def render(
        self,
        plan: CompiledPlan,
        steps: Sequence[StepView],
        facts: Sequence[FactRecord],
        calls: Sequence[CallRecord],
        cohorts: Sequence[CohortRef],
    ) -> ObservedResult:
        run = _Run(plan, steps, facts, calls, cohorts)
        lines = _Lines(observed=[], limitations=[], viz=[])
        survival_counts: set[str] = set()
        for spec in plan.steps:
            if spec.tool == "survival_compare":
                survival_counts.update(spec.depends_on)
                self._survival(run, spec, lines)
            elif spec.tool == "top_mutated_genes":
                self._genes(run, spec, lines)
            elif spec.tool == "de_run":
                self._de(run, spec, lines)
        self._sizes(run, survival_counts, lines)
        self._step_lines(run, lines)
        ordered = [item for _, _, item in sorted(lines.limitations, key=lambda t: (t[0], t[1]))]
        unique: list[Limitation] = []
        for item in ordered:
            if all((u.code, u.text) != (item.code, item.text) for u in unique):
                unique.append(item)
        specs = [spec.model_copy(update={"viz_id": f"v{i}"}) for i, spec in enumerate(lines.viz, start=1)]
        return ObservedResult(template_version=TEMPLATE_VERSION, observed=lines.observed, limitations=unique, viz_specs=specs)

    # -- labels -------------------------------------------------------------------------------

    def field_label(self, field_id: str) -> str:
        spec = self.vocab.field(field_id) if self.vocab is not None else None
        return spec.label if spec is not None else field_id.rsplit(".", 1)[-1].replace("_", " ").capitalize()

    # -- survival (J1) --------------------------------------------------------------------------

    def _survival(self, run: _Run, spec: StepSpec, lines: _Lines) -> None:
        anchor = run.order[spec.id]
        labels = [_arg_str(g, "label") for g in _groups(spec.args)]
        counts = [d for d in spec.depends_on if d in run.specs and run.specs[d].tool in COUNT_TOOLS]
        if not labels or len(counts) != len(labels):
            return
        keys = catalog.group_keys(labels)
        done = run.status(spec.id) in DONE
        for label, key, count in zip(labels, keys, counts):
            if run.status(count) == "Empty":
                sentence = run.sentence(
                    "empty.count", steps={"count": f"f.{count}.count"}, values={"cohort": label, "filter": run.views[count].input_summary}
                )
            elif done:
                sentence = run.sentence("survival.group", steps={"count": count, "survival": spec.id}, values={"group": label, "key": key})
            else:
                sentence = run.sentence("survival.group_count", steps={"count": count}, values={"group": label})
            if sentence is not None:
                lines.observed.append(sentence)
        p = run.sentence("survival.p", steps={"survival": spec.id})
        if p is not None:
            lines.observed.append(p)
        median = run.first("derive_km_median", _Curves(spec.id))
        if median is not None and run.status(median.id) in DONE:
            items: list[tuple[str, Mapping[str, str]]] = [
                ("reached" if f"f.{median.id}.median.{key}" in run.facts else "not_reached", {"group": label, "key": key})
                for label, key in zip(labels, keys)
            ]
            sentence = run.sentence("survival.median", steps={"median": median.id}, items=items)
            if sentence is not None:
                lines.observed.append(sentence)
        p_value = run.value(f"f.{spec.id}.p")
        rounded = isinstance(p_value, (int, float)) and p_value < catalog.VIEWER_P_FLOOR
        if done:
            if rounded:
                lines.limit(anchor, run.limitation("survival.viewer_p"))
            lines.limit(anchor, run.limitation("survival.no_hazard_ratio"))
        elif run.status(spec.id) == "Skipped" and run.code(spec.id) == "INELIGIBLE_GROUP":
            small = next(
                (
                    (label, f"f.{count}.count")
                    for label, count in zip(labels, counts)
                    if isinstance(run.value(f"f.{count}.count"), int) and int(run.value(f"f.{count}.count") or 0) < MIN_SURVIVAL_GROUP
                ),
                None,
            )
            if small is not None:
                lines.limit(anchor, run.limitation("survival.small_group", steps={"count": small[1]}, values={"group": small[0]}))
        split = spec.args.get("split")
        call = run.call(spec.id)
        if not done or not isinstance(split, dict) or call is None or not isinstance(call.request, dict):
            return
        pinned = derive_request_pinned("survival", call.request)
        frozen_rows: list[list[Segment]] = []
        for label, key, count in zip(labels, keys, counts):
            cells = [Segment(text=label)]
            for fact_id in (f"f.{count}.count", f"f.{spec.id}.events.{key}"):
                cells.append(run.chip(fact_id) if fact_id in run.facts else Segment(text="not available"))
            median_id = f"f.{median.id}.median.{key}" if median is not None else ""
            if median_id in run.facts:
                cells.append(run.chip(median_id))
            else:
                cells.append(Segment(text="not reached" if median is not None and run.status(median.id) in DONE else "not available"))
            frozen_rows.append(cells)
        note = run.bind("viz.survival.note", steps={"survival": spec.id})
        lines.viz.append(
            VizSpec(
                viz_id="v1",
                app_id="Correlation",
                launcher="survival",
                cites_steps=[spec.id],
                pinned=pinned,
                pinned_hash=pinned_hash(pinned),
                title=run.text("viz.survival.title", {"split": self.field_label(_arg_str(split, "field"))}),
                caption=run.text("viz.survival.caption") if rounded else None,
                request_pinned_hash=pinned_hash(derive_request_pinned("survival", call.request)),
                frozen=FrozenTable(
                    columns=list(catalog.VIZ_TEMPLATES["survival"].columns),
                    rows=frozen_rows[:FROZEN_ROWS_CAP],
                    note=None if note is None else run.segments(note),
                ),
                cohort_refs=["base"],
            )
        )

    # -- top mutated genes (J2 and the scope form) ------------------------------------------------

    def _genes(self, run: _Run, spec: StepSpec, lines: _Lines) -> None:
        anchor = run.order[spec.id]
        ranking = run.first("rank_top_n", _Rows(spec.id))
        source = run.ref_of(spec.args.get("filter0"))
        cohort = run.cohort_sized_by(source.ref) if source is not None else run.base()
        cohort_count = run.count_fact(source.ref) if source is not None else (cohort.case_count_fact_id if cohort else None)
        roles = {"cohort_count": cohort_count} if cohort_count is not None else {}
        status = run.status(spec.id)
        if status in FINISHED:
            sentence = run.sentence("genes.cohort", steps=roles)
            if sentence is not None:
                lines.observed.append(sentence)
        if status == "Empty":
            sentence = run.sentence("genes.none")
            if sentence is not None:
                lines.observed.append(sentence)
        shown = run.shown_rows(ranking.id) if ranking is not None else 0
        if ranking is not None and run.status(ranking.id) in FINISHED and shown:
            items: list[tuple[str, Mapping[str, str]]] = [("first" if pos == 1 else "rest", {"pos": str(pos)}) for pos in range(1, shown + 1)]
            sentence = run.sentence("genes.top" if shown > 1 else "genes.top_one", steps={**roles, "ranking": ranking.id}, items=items)
            if sentence is not None:
                lines.observed.append(sentence)
            self._rank_cap(run, ranking.id, shown, lines)
        if status in FINISHED:
            lines.limit(anchor, run.limitation("panel.ranking"))
        if status == "Empty":
            lines.limit(anchor, run.limitation("rank.none_genes"))
        if ranking is not None:
            self._rank_limits(run, ranking, "rank.ties_oncomatrix", lines, empty_template=None if status == "Empty" else "rank.none_genes")
        call = run.call(spec.id)
        if status not in DONE or call is None or not isinstance(call.request, dict):
            return
        try:
            pinned = derive_request_pinned("oncomatrix", call.request, call.observed)
        except (KeyError, ValueError):
            return
        name = cohort.name if cohort is not None else run.plan.scope.name
        frozen = (
            [[run.chip(f"f.{ranking.id}.top.{pos}.gene"), run.chip(f"f.{ranking.id}.top.{pos}.cases")]
             for pos in range(1, min(shown, FROZEN_ROWS_CAP) + 1) if f"f.{ranking.id}.top.{pos}.cases" in run.facts]
            if ranking is not None else []
        )
        lines.viz.append(
            VizSpec(
                viz_id="v1",
                app_id="OncoMatrix",
                launcher="oncomatrix",
                cites_steps=[spec.id],
                pinned=pinned,
                pinned_hash=pinned_hash(pinned),
                title=run.text("viz.oncomatrix.title", {"cohort": catalog.cohort_label(name)}),
                caption=None,
                request_pinned_hash=pinned_hash(derive_request_pinned("oncomatrix", call.request, call.observed)),
                frozen=FrozenTable(columns=list(catalog.VIZ_TEMPLATES["oncomatrix"].columns), rows=frozen, note=None),
                cohort_refs=[cohort.ref if cohort is not None else "base"],
            )
        )

    def _rank_cap(self, run: _Run, ranking: str, shown: int, lines: _Lines) -> None:
        count = run.value(f"f.{ranking}.count")
        if isinstance(count, int) and count > shown:
            sentence = run.sentence("rank.cap", steps={"ranking": ranking})
            if sentence is not None:
                lines.observed.append(sentence)

    def _rank_limits(self, run: _Run, ranking: StepSpec, ties_template: str, lines: _Lines, *, empty_template: str | None) -> None:
        anchor = run.order[ranking.id]
        status = run.status(ranking.id)
        n = _arg_int(ranking.args, "n") or 0
        if status == "Empty" and empty_template is not None:
            lines.limit(anchor, run.limitation(empty_template))
        count = run.value(f"f.{ranking.id}.count")
        if isinstance(count, int) and 0 < count < n:
            lines.limit(anchor, run.limitation("rank.fewer", steps={"ranking": ranking.id}, values={"n": str(n)}))
        call = run.call(ranking.id)
        observed = call.observed if call is not None else None
        ties = observed.get("tiesAtCutoff") if isinstance(observed, dict) else None
        rows = observed.get("rows") if isinstance(observed, dict) else None
        if status in DONE and isinstance(ties, list) and ties and isinstance(rows, list):
            tied = {t for t in ties if isinstance(t, str)}
            positions = [
                pos for pos, row in enumerate(rows, start=1)
                if pos > n and isinstance(row, dict) and row.get("key") in tied and f"f.{ranking.id}.top.{pos}.gene" in run.facts
            ]
            if positions:
                items: list[tuple[str, Mapping[str, str]]] = [("tied", {"pos": str(pos)}) for pos in positions]
                lines.limit(anchor, run.limitation(ties_template, steps={"ranking": ranking.id}, values={"n": str(n)}, items=items))

    # -- differential expression (J3) -----------------------------------------------------------

    def _de(self, run: _Run, spec: StepSpec, lines: _Lines) -> None:
        anchor = run.order[spec.id]
        groups = _groups(spec.args)
        if len(groups) != 2:
            return
        ranking = run.first("rank_top_n", _Rows(spec.id))
        sample_ref = run.ref_of(groups[0].get("sampleIds"))
        samples = run.specs.get(sample_ref.ref) if sample_ref is not None else None
        pre = run.first("de_preanalysis", _ReadsFrom(samples.id)) if samples is not None else None
        call = run.call(spec.id)
        sent = _groups(call.request) if call is not None and isinstance(call.request, dict) else []
        names = [_arg_str(g, "name") for g in (sent if len(sent) == 2 else groups)]
        refs = [_arg_str(g, "cohort") for g in groups]
        control, case = names
        # Gene-defined groups (C-UNTESTED): control = tested minus mutated.
        if samples is not None:
            sample_groups = _groups(samples.args)
            control_ref = run.ref_of(sample_groups[0].get("caseIds")) if sample_groups else None
            difference = run.specs.get(control_ref.ref) if control_ref is not None else None
            if difference is not None and difference.tool == "set_operation" and _arg_str(difference.args, "op") == "difference":
                tested, mutated = _arg_str(difference.args, "a"), _arg_str(difference.args, "b")
                mutated_spec = run.specs.get(mutated)
                if mutated_spec is not None and mutated_spec.tool == "gene_cases":
                    self._gene_groups(run, difference, tested, mutated_spec, lines)
        if pre is not None and run.status(pre.id) in DONE:
            sentence = run.sentence("de.groups", steps={"pre": pre.id}, values={"control": control, "case": case})
            if sentence is not None:
                lines.observed.append(sentence)
        rank_by = _arg_str(ranking.args, "rankBy", "adjusted_p_value") if ranking is not None else "adjusted_p_value"
        rank_label = catalog.RANK_LABELS.get(rank_by, "adjusted p-value")  # type: ignore[call-overload]
        status = run.status(spec.id)
        if status in ("Done", "Reused", "Empty", "Failed"):
            method = _arg_str(spec.args, "method", "wilcoxon")
            sentence = run.sentence(
                "de.method",
                values={"method": catalog.DE_METHOD_LABELS.get(method, method), "rank": rank_label, "case": case, "control": control},
            )
            if sentence is not None:
                lines.observed.append(sentence)
        edited = thresholds_edited(spec.args)
        if status in DONE:
            sentence = run.sentence("de.totals_edited" if edited else "de.totals", steps={"de": spec.id})
            if sentence is not None:
                lines.observed.append(sentence)
        shown = run.shown_rows(ranking.id) if ranking is not None else 0
        if ranking is not None and run.status(ranking.id) in DONE and shown:
            item = catalog.DE_TOP_ITEM.get(rank_by, "adjusted")  # type: ignore[call-overload]
            items: list[tuple[str, Mapping[str, str]]] = [(item, {"pos": str(pos)}) for pos in range(1, shown + 1)]
            sentence = run.sentence("de.top", steps={"ranking": ranking.id}, values={"rank": rank_label}, items=items)
            if sentence is not None:
                lines.observed.append(sentence)
            self._rank_cap(run, ranking.id, shown, lines)
        # Guards and limits on the DE path.
        if pre is not None and run.code(pre.id) == "INELIGIBLE_GROUP":
            lines.limit(run.order[pre.id], run.limitation("de.ineligible", steps={"pre": pre.id}, values={"control": control, "case": case}))
        for guarded in (pre, spec):
            if guarded is not None and run.code(guarded.id) == "REQUEST_TOO_LARGE" and samples is not None:
                lines.limit(run.order[guarded.id], run.limitation("de.too_many_samples", steps={"samples": samples.id}))
        if samples is not None:
            self._sample_limits(run, samples, (control, case), lines)
        if ranking is not None:
            self._rank_limits(run, ranking, "rank.ties", lines, empty_template="rank.none_de")
        if status not in DONE or ranking is None or run.status(ranking.id) not in DONE or call is None or not isinstance(call.request, dict):
            return
        launcher: PinnedKind = "deDirect"
        if self.de_launcher == "deInput" and len(sent) == 2 and all(isinstance(g.get("cohortId"), str) for g in sent):
            launcher = "deInput"
        try:
            pinned = derive_request_pinned(launcher, call.request)
        except (KeyError, ValueError):
            return
        frozen = [
            [run.chip(f"f.{ranking.id}.top.{pos}.{suffix}") for suffix in ("gene", "log2fc", "padj")]
            for pos in range(1, min(shown, FROZEN_ROWS_CAP) + 1)
            if all(f"f.{ranking.id}.top.{pos}.{suffix}" in run.facts for suffix in ("gene", "log2fc", "padj"))
        ]
        note = run.bind(
            "viz.de.note_edited" if edited else "viz.de.note",
            steps={"de": spec.id},
            values={"rank_short": catalog.RANK_SHORT.get(rank_by, "adjusted p")},  # type: ignore[call-overload]
        )
        known = {c.ref for c in run.cohorts}
        cohort_refs = [ref for ref in refs if ref in known] or ["base"]
        lines.viz.append(
            VizSpec(
                viz_id="v1",
                app_id="DE",
                launcher=launcher,
                cites_steps=[spec.id, ranking.id],
                pinned=pinned,
                pinned_hash=pinned_hash(pinned),
                title=run.text("viz.de.title", {"case": catalog.cohort_label(case), "control": catalog.cohort_label(control)}),
                caption=run.text("viz.de.caption" if launcher == "deDirect" else "viz.de_input.caption"),
                request_pinned_hash=pinned_hash(derive_request_pinned(launcher, call.request)),
                frozen=FrozenTable(
                    columns=list(catalog.VIZ_TEMPLATES[launcher].columns), rows=frozen, note=None if note is None else run.segments(note)
                ),
                cohort_refs=cohort_refs,
            )
        )
        del anchor

    def _gene_groups(self, run: _Run, difference: StepSpec, tested: str, mutated: StepSpec, lines: _Lines) -> None:
        gene = _arg_str(mutated.args, "gene")
        if run.status(difference.id) in DONE:
            sentence = run.sentence(
                "de.cohorts", steps={"tested": tested, "mutated": mutated.id, "difference": difference.id}, values={"gene": gene}
            )
            if sentence is not None:
                lines.observed.append(sentence)
        base = run.base()
        scope_count = base.case_count_fact_id if base is not None else None
        tested_count, scope_value = run.value(f"f.{tested}.count"), run.value(scope_count or "")
        if (
            scope_count is not None
            and isinstance(tested_count, int)
            and isinstance(scope_value, int)
            and tested_count < scope_value
        ):
            sentence = run.sentence("de.untested", steps={"tested": tested, "scope_count": scope_count})
            if sentence is not None:
                lines.observed.append(sentence)
        if run.status(mutated.id) in FINISHED:
            lines.limit(run.order[mutated.id], run.limitation("panel.gene", values={"gene": gene}))
        subset = run.first("check_subset", _Subset(mutated.id, tested))
        outside = run.value(f"f.{subset.id}.outside") if subset is not None else None
        if subset is not None and isinstance(outside, int) and outside > 0:
            inner = run.facts.get(f"f.{mutated.id}.count")
            outer = run.facts.get(f"f.{tested}.count")
            lines.limit(
                run.order[subset.id],
                run.limitation(
                    "subset.outside",
                    steps={"subset": subset.id},
                    values={
                        "inner": inner.label if inner is not None else f"{gene}-mutated cases",
                        "outer": (outer.label[:1].lower() + outer.label[1:]) if outer is not None else "tested cases",
                    },
                ),
            )

    def _sample_limits(self, run: _Run, samples: StepSpec, names: tuple[str, str], lines: _Lines) -> None:
        anchor = run.order[samples.id]
        call = run.call(samples.id)
        observed = call.observed if call is not None else None
        if run.status(samples.id) == "Empty" and isinstance(observed, dict) and isinstance(observed.get("groups"), list):
            groups = observed["groups"]
            assert isinstance(groups, list)
            for name, group in zip(names, groups):
                if isinstance(group, dict) and group.get("count") == 0:
                    lines.limit(anchor, run.limitation("empty.samples", values={"group": catalog.cohort_label(name)}))
                    break
        if run.status(samples.id) == "Skipped" and run.code(samples.id) == "REQUEST_TOO_LARGE":
            lines.limit(anchor, run.limitation("samples.too_large", values={"group": " and ".join(catalog.cohort_label(n) for n in names)}))

    # -- cohort size --------------------------------------------------------------------------------

    def _sizes(self, run: _Run, survival_counts: set[str], lines: _Lines) -> None:
        for row in run.plan.rows:
            if not row.label.startswith("Count ") or row.tool not in COUNT_TOOLS:
                continue
            counted = [sid for sid in row.step_ids if sid in run.specs and run.specs[sid].tool in COUNT_TOOLS and sid not in survival_counts]
            if not counted:
                continue
            step = counted[-1]
            spec = run.specs[step]
            if spec.tool == "set_operation" and _arg_str(spec.args, "op") not in ("size", ""):
                continue
            name = row.label.removeprefix("Count ")
            if run.status(step) == "Empty":
                sentence = run.sentence(
                    "empty.count", steps={"count": f"f.{step}.count"}, values={"cohort": name, "filter": run.views[step].input_summary}
                )
            else:
                sentence = run.sentence("size.count", steps={"count": f"f.{step}.count"}, values={"cohort": name})
            if sentence is not None:
                lines.observed.append(sentence)

    # -- per-step lines: failures, zeros, ID limits, the write step -----------------------------------

    def _step_lines(self, run: _Run, lines: _Lines) -> None:
        for spec in run.plan.steps:
            anchor = run.order[spec.id]
            status, code = run.status(spec.id), run.code(spec.id)
            if status == "Failed" and code == "TOOL_FAILED":
                if spec.kind == "client":
                    lines.limit(anchor, run.limitation("tool.failed_write", values={"missing": "the steps after the save did not run"}))
                    continue
                failure = failure_code(run.last_attempt(spec.id))
                lines.limit(
                    anchor,
                    run.limitation(
                        "tool.failed",
                        values={
                            "tool": catalog.TOOL_PHRASES.get(spec.tool, spec.tool.replace("_", " ")),
                            "failure": catalog.FAILURE_PHRASES[failure],
                            "missing": catalog.MISSING_PHRASES.get(spec.tool, "its result is missing"),
                        },
                    ),
                )
            elif status == "Empty" and spec.tool in COUNT_TOOLS:
                lines.limit(anchor, run.limitation("empty.cohort", values={"filter": run.views[spec.id].input_summary}))
            elif code == "TOO_MANY_IDS" and f"f.{spec.id}.count" in run.facts:
                cohort = run.cohort_sized_by(spec.id)
                name = cohort.name if cohort is not None else run.plan.scope.name
                lines.limit(
                    anchor, run.limitation("ids.too_many", steps={"count": f"f.{spec.id}.count"}, values={"cohort": catalog.cohort_label(name)})
                )
            elif status == "Skipped" and code == "KG_NOT_CONNECTED":
                lines.limit(anchor, run.limitation("kg.not_connected"))
            elif status == "Skipped" and code == "WRITE_SKIPPED":
                cohorts = spec.args.get("cohorts")
                lines.limit(anchor, save_skipped(len(cohorts) if isinstance(cohorts, list) else 1))


@dataclass(frozen=True)
class _Curves:
    survival: str

    def __call__(self, step: StepSpec) -> bool:
        return step.args.get("curves") == self.survival


@dataclass(frozen=True)
class _Rows:
    source: str

    def __call__(self, step: StepSpec) -> bool:
        return step.args.get("rows") == self.source


@dataclass(frozen=True)
class _ReadsFrom:
    source: str

    def __call__(self, step: StepSpec) -> bool:
        return any(ref.ref == self.source for ref in step_refs(step.args.get("groups")))


@dataclass(frozen=True)
class _Subset:
    inner: str
    outer: str

    def __call__(self, step: StepSpec) -> bool:
        return step.args.get("inner") == self.inner and step.args.get("outer") == self.outer
