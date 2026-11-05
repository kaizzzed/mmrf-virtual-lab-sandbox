"""The verifier (RFA rev 25, A2 verifier table; contract "Answer parts"; C-02).

Model text (Interpretation, Hypotheses, follow-ups) passes four rules, in this order; the first
that fails names the drop:

- V-REF: every placeholder is well formed and resolves to a fact of a finished step, every cite
  is a fact, every placeholder is cited, and every statement cites at least one fact.
- V-ENT: every cohort the text names ("… (ORION)", or a quoted name) is a run, ledger or
  vocabulary name; a follow-up's genes are panel genes or gene facts of the run.
- V-NUM: outside placeholders and allow-listed terms, no digit, no number word ("two groups"), no
  Roman numeral and no gene symbol. A follow-up may carry one integer from 1 to 100 when its kind
  takes a top N, and may name a known gene in plain text (it is asked as a new question).
- V-SEC: no causal or certainty word unless negated ("does not show a driver role"), no clinical
  or treatment advice; a hypothesis is a question ending in "?" or a hedged action.

Observed and Limitations are code-written, so only V-COMP and V-LIM apply to them, and code fixes
what they find: V-COMP inserts the journey's missing template sentence (J1 groups and log-rank p,
J2 cohort size and ranking, J3 groups, method and top-N table), V-LIM the missing limitation
line (plan limitations, rounding, ties, fewer than N, panel coverage, subset, empty, the hazard
ratio). On the first attempt a failing model statement asks for one re-compose (`retry`); on the
second it is dropped, leaving `{"segments": [], "dropped": "<rule>"}`. When no Interpretation
sentence survives, `observed_only` is set (the Interpretation-present gate, C-02).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, get_args

from pydantic import JsonValue

from ..catalog import APPS, AnalysisKind
from ..compiler import CompiledPlan, SessionLedger, StepSpec
from ..interfaces import (
    PLACEHOLDER,
    ComposerFollowUp,
    ComposerHypothesis,
    ComposerOutput,
    ComposerStatement,
    ObservedResult,
    RuleFinding,
    VerifiedPart,
    VerifierResult,
)
from ..models import (
    MAX_TOP_N_DE,
    FactRecord,
    FollowUp,
    Limitation,
    NearestApp,
    Segment,
    Sentence,
    VerifierRule,
    format_fact,
)
from ..tools import schemas
from ..vocab import Vocabulary
from .lexicon import (
    ACRONYMS,
    FIXED_TERMS,
    ORION_SUFFIX,
    app_titles,
    cohort_label,
    gene_like_tokens,
    has_digit,
    integers,
    json_list,
    number_words,
    panel_genes,
    placeholders,
    remove_terms,
    roman_numerals,
    tokens,
    vocabulary_acronyms,
    vocabulary_fields,
    vocabulary_terms,
    without_placeholders,
)

MODEL_RULES: Final[tuple[VerifierRule, ...]] = ("V-REF", "V-ENT", "V-NUM", "V-SEC")
TOP_N_KINDS: Final[frozenset[str]] = frozenset({"differential_expression", "top_mutated_genes"})
ANALYSIS_KINDS: Final = frozenset(get_args(AnalysisKind))
_NEGATORS: Final = frozenset(
    {"not", "no", "never", "cannot", "can't", "doesn't", "don't", "didn't", "isn't", "aren't", "wasn't", "won't", "nor",
     "without", "neither", "doesn’t", "don’t", "isn’t", "can’t"}
)
_NEGATION_WINDOW: Final = 4
_CAUSAL: Final = re.compile(
    r"\b(prov(?:e|es|ed|en|ing)|proof|caus(?:e|es|ed|ing|al|ally|ation|ative)|driv(?:e|es|en|ing|er|ers)|"
    r"determin(?:e|es|ed|ing)|confirm(?:s|ed|ing)?|demonstrat(?:e|es|ed|ing)|establish(?:es|ed|ing)?|"
    r"leads? to|led to|results? in|resulted in|due to|because of|responsible for|guarantee(?:s|d)?|"
    r"clearly|definitely|definitively|certainly|undoubtedly|conclusive(?:ly)?|unequivocal(?:ly)?)\b",
    re.IGNORECASE,
)
# An infinitive of purpose ("to determine whether") names a test, not a claim.
_PURPOSE_VERBS: Final = frozenset({"determine", "confirm", "establish", "demonstrate", "prove"})
_CLINICAL: Final = re.compile(
    r"\b(treat(?:s|ed|ing|ment|ments)?|therap(?:y|ies|eutic|eutics)|drugs?|medications?|doses?|dosing|dosage|"
    r"prescri(?:be|bes|bed|bing|ption|ptions)|recommend(?:s|ed|ing|ation|ations)?|inhibitors?|prognos(?:is|es|tic)|"
    r"standard of care|patient care|clinical (?:decision|decisions|care|practice|use|management)|"
    r"should (?:receive|be given|be treated|take|start|stop|avoid))\b",
    re.IGNORECASE,
)
_ACTION_VERBS: Final = frozenset(
    {"compare", "check", "test", "look", "explore", "ask", "examine", "assess", "see", "review", "rank", "count", "split",
     "repeat", "run", "open", "plot", "measure", "evaluate", "investigate", "consider", "inspect", "contrast", "restrict"}
)
_HEDGES: Final = frozenset({"whether", "if", "may", "might", "could", "would"})
_QUOTED: Final = re.compile(r"[\"“”]([^\"“”]{2,120})[\"“”]")
_BRACES: Final = re.compile(r"\{\{|\}\}|\{f\.")


# --- Per-run context -----------------------------------------------------------------------------


@dataclass
class _Context:
    facts: dict[str, FactRecord]
    terms: list[str]
    acronyms: frozenset[str]
    names: frozenset[str]  # casefolded known names (cohorts, vocabulary, apps)
    genes: frozenset[str]  # genes a follow-up may name in plain text
    findings: list[RuleFinding] = field(default_factory=list)


def _step_names(step: StepSpec) -> list[str]:
    names: list[str] = []
    args = step.args
    if step.tool == "save_cohorts":
        for cohort in json_list(args.get("cohorts")):
            if isinstance(cohort, dict) and isinstance(cohort.get("name"), str):
                names.append(str(cohort["name"]))
    if step.tool == "de_run":
        for group in json_list(args.get("groups")):
            if isinstance(group, dict) and isinstance(group.get("name"), str):
                names.append(str(group["name"]))
    if step.tool == "survival_compare":
        for group in json_list(args.get("groups")):
            if isinstance(group, dict) and isinstance(group.get("label"), str):
                names.append(str(group["label"]))
    return names


def plan_cohort_names(plan: CompiledPlan) -> list[str]:
    """The run's cohort names: the scope, the alternative scope and every name a step carries."""
    names = [plan.scope.name] + ([plan.alt_scope.name] if plan.alt_scope else [])
    for step in plan.steps:
        names += _step_names(step)
    return names


def ledger_cohort_names(ledger: SessionLedger | None) -> list[str]:
    if ledger is None:
        return []
    return [run.scope.name for run in ledger.runs] + [c.name for run in ledger.runs for c in run.cohorts]


def _viz_names(observed: ObservedResult) -> list[str]:
    names: list[str] = []
    for spec in observed.viz_specs:
        pinned = spec.pinned
        groups = getattr(pinned, "groups", None) if pinned is not None else None
        for group in groups or []:
            name = getattr(group, "name", None)
            if isinstance(name, str):
                names.append(name)
    return names


def _with_labels(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if name.strip():
            out.append(name)
            label = cohort_label(name)
            if label != name and label.strip():
                out.append(label)
    return out


def _resolve(text: str, facts: Mapping[str, FactRecord]) -> list[Segment]:
    """Text and fact segments; a fact segment's text is format_fact(value, unit), as Observed writes it."""
    segments: list[Segment] = []
    position = 0
    for match in PLACEHOLDER.finditer(text):
        if match.start() > position:
            segments.append(Segment(text=text[position : match.start()]))
        fact = facts[match.group(1)]
        segments.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
        position = match.end()
    if position < len(text):
        segments.append(Segment(text=text[position:]))
    merged: list[Segment] = []
    for segment in segments:
        if segment.fact_id is None and merged and merged[-1].fact_id is None:
            merged[-1] = Segment(text=merged[-1].text + segment.text)
        elif segment.fact_id is not None or segment.text:
            merged.append(segment)
    return merged


def _plain(text: str, facts: Mapping[str, FactRecord]) -> str:
    return PLACEHOLDER.sub(lambda m: format_fact(facts[m.group(1)].value, facts[m.group(1)].unit), text)


# --- Rules on model text -------------------------------------------------------------------------


def _check_ref(text: str, cites: Sequence[str] | None, ctx: _Context) -> str | None:
    if _BRACES.search(without_placeholders(text)):
        return "a placeholder is malformed; write {{<fact id>}} with a fact id from the request"
    unknown = [p for p in placeholders(text) if p not in ctx.facts]
    if unknown:
        return f"placeholder {unknown[0]} is not a fact of a finished step"
    if cites is None:
        return None
    if not cites:
        return "the statement cites no fact"
    missing = [c for c in cites if c not in ctx.facts]
    if missing:
        return f"cite {missing[0]} is not a fact of a finished step"
    uncited = [p for p in placeholders(text) if p not in cites]
    if uncited:
        return f"placeholder {uncited[0]} is not in cites"
    return None


def _check_ent(stripped: str, ctx: _Context) -> str | None:
    if ORION_SUFFIX.strip() in stripped:
        return "the text names a cohort that is not in this run, its ledger or the vocabulary"
    for match in _QUOTED.finditer(stripped):
        if match.group(1).strip().casefold() not in ctx.names:
            return f"the quoted name {match.group(1)!r} is not a cohort, field value or app of this run"
    return None


def _check_num(stripped: str, ctx: _Context) -> str | None:
    if has_digit(stripped):
        digits = [t for t in tokens(stripped) if has_digit(t)]
        return f"{digits[0] if digits else 'a digit'} is written outside a fact placeholder"
    words = number_words(stripped)
    if words:
        return f"the number word {words[0]!r} is written outside a fact placeholder"
    numerals = roman_numerals(stripped)
    if numerals:
        return f"the numeral {numerals[0]!r} is written outside a fact placeholder"
    genes = gene_like_tokens(stripped, ctx.acronyms)
    if genes:
        return f"{genes[0]} looks like a gene symbol written outside a fact placeholder"
    return None


def _causal(text: str) -> str | None:
    for match in _CAUSAL.finditer(text):
        before = [w.casefold() for w in tokens(text[: match.start()])]
        if any(w in _NEGATORS for w in before[-_NEGATION_WINDOW:]):
            continue
        term = match.group(1).casefold()
        if term in _PURPOSE_VERBS and before[-1:] == ["to"]:
            continue
        return term
    return None


def _check_sec(text: str, form: str | None) -> str | None:
    clinical = _CLINICAL.search(text)
    if clinical:
        return f"{clinical.group(1)!r} reads as clinical or treatment advice"
    term = _causal(text)
    if term:
        return f"{term!r} states a causal or certain claim; hedge it"
    if form == "question" and not text.rstrip().endswith("?"):
        return "a question hypothesis ends with a question mark"
    if form == "action":
        first = next(iter(tokens(without_placeholders(text))), "").casefold()
        hedged = any(w.casefold() in _HEDGES for w in tokens(text))
        if first not in _ACTION_VERBS and not hedged:
            return "an action hypothesis starts with a verb such as Compare or Check, or is hedged"
    return None


def _statement_failure(text: str, cites: Sequence[str] | None, form: str | None, ctx: _Context) -> tuple[VerifierRule, str] | None:
    detail = _check_ref(text, cites, ctx)
    if detail:
        return "V-REF", detail
    stripped = remove_terms(without_placeholders(text), ctx.terms)
    detail = _check_ent(stripped, ctx)
    if detail:
        return "V-ENT", detail
    detail = _check_num(stripped, ctx)
    if detail:
        return "V-NUM", detail
    detail = _check_sec(text, form)
    if detail:
        return "V-SEC", detail
    return None


def _record(ctx: _Context, part: VerifiedPart, index: int, failure: tuple[VerifierRule, str] | None) -> None:
    failed_rule = failure[0] if failure else None
    for rule in MODEL_RULES:
        if failed_rule is not None and MODEL_RULES.index(rule) > MODEL_RULES.index(failed_rule):
            break  # later rules were not reached
        passed = rule != failed_rule
        detail = failure[1] if failure and not passed else "passed"
        ctx.findings.append(RuleFinding(rule=rule, part=part, index=index, passed=passed, detail=detail))


def _statements(
    items: Sequence[ComposerStatement | ComposerHypothesis], part: VerifiedPart, ctx: _Context
) -> tuple[list[Sentence], bool]:
    sentences: list[Sentence] = []
    failed = False
    for index, item in enumerate(items):
        form = item.form if isinstance(item, ComposerHypothesis) else None
        failure = _statement_failure(item.text, item.cites, form, ctx)
        _record(ctx, part, index, failure)
        if failure:
            failed = True
            sentences.append(Sentence(segments=[], dropped=failure[0]))
        else:
            sentences.append(Sentence(segments=_resolve(item.text, ctx.facts), cites=list(item.cites)))
    return sentences, failed


def _follow_up_failure(item: ComposerFollowUp, ctx: _Context) -> tuple[VerifierRule, str] | None:
    if item.kind not in ANALYSIS_KINDS:
        return "V-ENT", f"{item.kind} is not an analysis kind ORION runs"
    texts = (item.label, item.question)
    for text in texts:
        detail = _check_ref(text, None, ctx)
        if detail:
            return "V-REF", detail
    numeric = [p for text in texts for p in placeholders(text) if not isinstance(ctx.facts[p].value, str)]
    stripped = [remove_terms(without_placeholders(text), [*ctx.terms, *sorted(ctx.genes)]) for text in texts]
    for text in stripped:
        detail = _check_ent(text, ctx)
        if detail:
            return "V-ENT", detail
        unknown = gene_like_tokens(remove_terms(text, [*ctx.acronyms]), ctx.acronyms)
        if unknown:
            return "V-ENT", f"{unknown[0]} is not a panel gene or a gene of this run"
    if numeric:
        return "V-NUM", f"{numeric[0]} puts a number in a follow-up chip, which shows no citation"
    found = sorted({n for text in stripped for n in integers(text)})
    allowed = found if item.kind in TOP_N_KINDS and len(found) == 1 and 1 <= found[0] <= MAX_TOP_N_DE else []
    for text in stripped:
        rest = text
        for n in allowed:
            rest = re.sub(rf"(?<![A-Za-z0-9.]){n}(?![A-Za-z0-9.])", " ", rest)
        if has_digit(rest):
            return "V-NUM", "a follow-up carries a number other than one top N from 1 to 100"
        words = number_words(rest)
        if words:
            return "V-NUM", f"the number word {words[0]!r} is in a follow-up"
        numerals = roman_numerals(rest)
        if numerals:
            return "V-NUM", f"the numeral {numerals[0]!r} is in a follow-up"
    for text in texts:
        clinical = _CLINICAL.search(text)
        if clinical:
            return "V-SEC", f"{clinical.group(1)!r} reads as clinical or treatment advice"
    return None


def _follow_ups(items: Sequence[ComposerFollowUp], ctx: _Context) -> tuple[list[FollowUp], bool]:
    kept: list[FollowUp] = []
    seen: set[str] = set()
    failed = False
    for index, item in enumerate(items):
        failure = _follow_up_failure(item, ctx)
        _record(ctx, "follow_ups", index, failure)
        if failure:
            failed = True
            continue
        label, question = _plain(item.label, ctx.facts), _plain(item.question, ctx.facts)
        key = question.casefold()
        if key in seen or len(label) > 60 or len(question) > 200:
            continue
        seen.add(key)
        kept.append(FollowUp(label=label, question=question, kind=item.kind))
    return kept, failed


def _open_links(app_ids: Sequence[str]) -> list[NearestApp]:
    """Open <app> links: catalog apps ORION opens on a cohort; driven and out-of-scope apps are left out."""
    links: list[NearestApp] = []
    for app_id in app_ids:
        app = APPS.get(app_id)  # type: ignore[call-overload]
        if app is not None and app.role == "opens" and all(link.app_id != app.app_id for link in links):
            links.append(NearestApp.of(app.app_id))
    return links


# --- V-COMP and V-LIM on Observed and Limitations -------------------------------------------------


def _cites(sentence: Sentence) -> set[str]:
    return {s.fact_id for s in sentence.segments if s.fact_id}


def _text_of(sentence: Sentence) -> str:
    return "".join(s.text for s in sentence.segments)


def _render(
    template_id: str,
    facts: Mapping[str, FactRecord],
    *,
    steps: Mapping[str, str] | None = None,
    values: Mapping[str, str] | None = None,
    items: Sequence[tuple[str, Mapping[str, str]]] = (),
) -> list[Segment] | None:
    """A template's segments, or None when a fact it cites does not exist (the template does not apply)."""
    try:
        parts = schemas.template_parts(schemas.TEMPLATES[template_id], steps=steps, values=values, items=items)
    except (KeyError, ValueError):
        return None
    segments: list[Segment] = []
    for kind, text in parts:
        if kind == "fact":
            fact = facts.get(text)
            if fact is None:
                return None
            segments.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
        else:
            segments.append(Segment(text=text))
    return segments


def _limitation(template_id: str, segments: list[Segment], values: Mapping[str, str] | None = None) -> Limitation:
    template = schemas.TEMPLATES[template_id]
    assert template.code is not None
    text = "".join(s.text for s in segments)
    unblock = schemas.fill_text(template.unblock, values or {}) if template.unblock else None
    has_fact = any(s.fact_id for s in segments)
    if has_fact:
        return Limitation(code=template.code, text=text, unblock=unblock, segments=segments)
    return Limitation(code=template.code, text=text, unblock=unblock)


def _refs(value: JsonValue) -> list[str]:
    if isinstance(value, dict):
        if schemas.is_step_ref(value):
            ref = value.get("ref")
            return [ref] if isinstance(ref, str) else []
        return [r for item in value.values() for r in _refs(item)]
    if isinstance(value, list):
        return [r for item in value for r in _refs(item)]
    return []


def _positions(facts: Mapping[str, FactRecord], step_id: str) -> int:
    n = 0
    while f"f.{step_id}.top.{n + 1}.gene" in facts:
        n += 1
    return n


def _lowered(label: str) -> str:
    return label[:1].lower() + label[1:] if label[:2] != label[:2].upper() else label


@dataclass
class _Observed:
    facts: Mapping[str, FactRecord]
    plan: CompiledPlan
    observed: ObservedResult
    findings: list[RuleFinding] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)

    def cited(self, fact_id: str) -> bool:
        return any(fact_id in _cites(s) for s in [*self.observed.observed, *self.sentences])

    def has_code(self, code: str) -> bool:
        return any(lim.code == code for lim in [*self.observed.limitations, *self.limitations])

    def comp(self, need: str, present: bool, segments: list[Segment] | None) -> None:
        if present:
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=True, detail=f"{need}: present"))
        elif segments is None:
            detail = f"{need}: missing, and its facts are absent, so no sentence was inserted"
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=False, detail=detail))
        else:
            self.sentences.append(Sentence(segments=segments))
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=False, detail=f"{need}: inserted"))

    def lim(self, need: str, code: str, make: Limitation | None) -> None:
        if self.has_code(code):
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=True, detail=f"{need}: present"))
        elif make is None:
            detail = f"{need}: missing, and its facts are absent, so no line was inserted"
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=False, detail=detail))
        else:
            self.limitations.append(make)
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=False, detail=f"{need}: inserted"))

    def steps(self, tool: str) -> list[StepSpec]:
        return [s for s in self.plan.steps if s.tool == tool]

    def step(self, step_id: str) -> StepSpec | None:
        return next((s for s in self.plan.steps if s.id == step_id), None)


def _survival(o: _Observed) -> None:
    for step in o.steps("survival_compare"):
        groups = [g for g in json_list(step.args.get("groups")) if isinstance(g, dict)]
        labels = [str(g.get("label", "")) for g in groups]
        keys = schemas.group_keys(labels) if labels else []
        counts = step.depends_on if len(step.depends_on) == len(groups) else []
        ran = f"f.{step.id}.p" in o.facts or any(f"f.{step.id}.events.{k}" in o.facts for k in keys)
        for i, (label, key) in enumerate(zip(labels, keys)):
            events = f"f.{step.id}.events.{key}"
            if events not in o.facts:
                continue
            segments = None
            if counts:
                segments = _render("survival.group", o.facts, steps={"count": counts[i], "survival": step.id},
                                   values={"group": label, "key": key})
            o.comp(f"J1 {label} cases and events", o.cited(events), segments)
        p_fact = o.facts.get(f"f.{step.id}.p")
        if p_fact is not None:
            o.comp("J1 log-rank p", o.cited(p_fact.id), _render("survival.p", o.facts, steps={"survival": step.id}))
            card = any(v.launcher == "survival" and step.id in v.cites_steps for v in o.observed.viz_specs)
            if card and isinstance(p_fact.value, (int, float)) and p_fact.value < schemas.VIEWER_P_FLOOR:
                o.lim("viewer rounding", "VIEWER_P_ROUNDED", _limitation("survival.viewer_p", _render("survival.viewer_p", o.facts) or []))
        if ran:
            o.lim("no hazard ratio", "STAT_UNAVAILABLE",
                  _limitation("survival.no_hazard_ratio", _render("survival.no_hazard_ratio", o.facts) or []))


def _ranking_source(o: _Observed, ranking: StepSpec) -> StepSpec | None:
    rows = ranking.args.get("rows")
    return o.step(rows) if isinstance(rows, str) else None


def _rank_limits(o: _Observed, ranking: StepSpec, oncomatrix: bool) -> None:
    n_value = ranking.args.get("n")
    n = n_value if isinstance(n_value, int) else None
    shown = _positions(o.facts, ranking.id)
    count = o.facts.get(f"f.{ranking.id}.count")
    if n is None:
        return
    if count is not None and isinstance(count.value, int) and count.value < n:
        if count.value == 0:
            template = "rank.none_genes" if oncomatrix else "rank.none_de"
            o.lim("an empty ranking", "FEWER_THAN_N", _limitation(template, _render(template, o.facts) or []))
        else:
            segments = _render("rank.fewer", o.facts, steps={"ranking": ranking.id}, values={"n": str(n)})
            o.lim("fewer rows than asked", "FEWER_THAN_N", _limitation("rank.fewer", segments) if segments else None)
    if shown > n:
        template = "rank.ties_oncomatrix" if oncomatrix else "rank.ties"
        tied = [("tied", {"pos": str(pos)}) for pos in range(n + 1, shown + 1)]
        segments = _render(template, o.facts, steps={"ranking": ranking.id}, values={"n": str(n)}, items=tied)
        o.lim("ties at the cutoff", "TIES_AT_CUTOFF", _limitation(template, segments) if segments else None)


def _genes(o: _Observed) -> None:
    for ranking in o.steps("rank_top_n"):
        source = _ranking_source(o, ranking)
        if source is None or source.tool != "top_mutated_genes":
            continue
        refs = _refs(source.args)
        count_id = next((f"f.{r}.count" for r in refs if f"f.{r}.count" in o.facts), None)
        if count_id is None and not refs and "f.s1.cases" in o.facts:
            count_id = "f.s1.cases"
        if count_id is not None:
            alone = any(_cites(s) == {count_id} for s in [*o.observed.observed, *o.sentences])
            o.comp("J2 cohort size", alone, _render("genes.cohort", o.facts, steps={"cohort_count": count_id}))
        shown = _positions(o.facts, ranking.id)
        if shown and count_id is not None:
            template = "genes.top" if shown > 1 else "genes.top_one"
            items = [("first" if pos == 1 else "rest", {"pos": str(pos)}) for pos in range(1, shown + 1)]
            segments = _render(template, o.facts, steps={"ranking": ranking.id, "cohort_count": count_id}, items=items)
            o.comp("J2 ranking counts", o.cited(f"f.{ranking.id}.top.1.gene"), segments)
        o.lim("panel coverage", "PANEL_COVERAGE", _limitation("panel.ranking", _render("panel.ranking", o.facts) or []))
        _rank_limits(o, ranking, oncomatrix=True)


def _de_names(o: _Observed, de: StepSpec) -> tuple[str, str] | None:
    for spec in o.observed.viz_specs:
        if spec.launcher in ("deDirect", "deInput") and de.id in spec.cites_steps and spec.pinned is not None:
            groups = getattr(spec.pinned, "groups", None) or []
            names = [getattr(g, "name", None) for g in groups]
            if len(names) == 2 and all(isinstance(n, str) for n in names):
                return str(names[0]), str(names[1])
    groups = [g for g in json_list(de.args.get("groups")) if isinstance(g, dict)]
    names = [g.get("name") for g in groups]
    if len(names) == 2 and all(isinstance(n, str) for n in names):
        return str(names[0]), str(names[1])
    return None


def _de(o: _Observed) -> None:
    for ranking in o.steps("rank_top_n"):
        de = _ranking_source(o, ranking)
        if de is None or de.tool != "de_run":
            continue
        names = _de_names(o, de)
        pre = next((s for s in reversed(o.plan.steps[: o.plan.steps.index(de)]) if s.tool == "de_preanalysis"), None)
        if pre is not None and names is not None and f"f.{pre.id}.eligible.control" in o.facts:
            segments = _render("de.groups", o.facts, steps={"pre": pre.id}, values={"control": names[0], "case": names[1]})
            o.comp("J3 group sizes", o.cited(f"f.{pre.id}.eligible.control"), segments)
        rank_by = ranking.args.get("rankBy")
        method = de.args.get("method")
        if names is not None and isinstance(rank_by, str) and rank_by in schemas.RANK_LABELS and isinstance(method, str):
            values = {
                "method": schemas.DE_METHOD_LABELS.get(method, method),
                "rank": schemas.RANK_LABELS[rank_by],  # type: ignore[index]
                "control": names[0],
                "case": names[1],
            }
            present = any(_text_of(s).startswith("Method: ") for s in [*o.observed.observed, *o.sentences])
            o.comp("J3 method, ranking rule and direction", present, _render("de.method", o.facts, values=values))
            shown = _positions(o.facts, ranking.id)
            item = schemas.DE_TOP_ITEM.get(rank_by)  # type: ignore[call-overload]
            if shown and item:
                items = [(item, {"pos": str(pos)}) for pos in range(1, shown + 1)]
                segments = _render("de.top", o.facts, steps={"ranking": ranking.id}, values={"rank": values["rank"]}, items=items)
                o.comp("J3 top-N table", o.cited(f"f.{ranking.id}.top.1.gene"), segments)
        _rank_limits(o, ranking, oncomatrix=False)


def _panel_genes(o: _Observed) -> None:
    for step in o.steps("gene_cases"):
        gene = step.args.get("gene")
        if isinstance(gene, str):
            o.lim(f"{gene} panel coverage", "PANEL_COVERAGE",
                  _limitation("panel.gene", _render("panel.gene", o.facts, values={"gene": gene}) or []))


def _subsets(o: _Observed) -> None:
    for step in o.steps("check_subset"):
        outside = o.facts.get(f"f.{step.id}.outside")
        if outside is None or not isinstance(outside.value, (int, float)) or outside.value <= 0:
            continue
        inner, outer = step.args.get("inner"), step.args.get("outer")
        inner_fact = o.facts.get(f"f.{inner}.count") if isinstance(inner, str) else None
        outer_fact = o.facts.get(f"f.{outer}.count") if isinstance(outer, str) else None
        segments = None
        if inner_fact is not None and outer_fact is not None:
            values = {"inner": inner_fact.label, "outer": _lowered(outer_fact.label)}
            segments = _render("subset.outside", o.facts, steps={"subset": step.id}, values=values)
        o.lim("cases outside the tested cases", "NOT_SUBSET", _limitation("subset.outside", segments) if segments else None)


_COUNT_TOOLS: Final = frozenset({"cohort_count", "cases_query", "gene_cases", "set_operation"})


def _empties(o: _Observed) -> None:
    for step in o.plan.steps:
        fact = o.facts.get(f"f.{step.id}.count")
        if step.tool in _COUNT_TOOLS and fact is not None and fact.value == 0:
            values = {"filter": step.input_summary or "the plan's filter"}
            segments = _render("empty.cohort", o.facts, values=values) or []
            o.lim(f"{step.id} found no case", "EMPTY_COHORT", _limitation("empty.cohort", segments, values))


def _plan_limitations(o: _Observed) -> None:
    """The compiler's limitations (a NAMED_TOOL_MISMATCH that stays after consent, a fallen-back scope)."""
    for limitation in o.plan.limitations:
        o.lim(f"plan limitation {limitation.code}", limitation.code, limitation)


def check_observed(facts: Sequence[FactRecord], plan: CompiledPlan, observed: ObservedResult) -> tuple[list[RuleFinding], list[Sentence], list[Limitation]]:
    """V-COMP and V-LIM: what code inserts into Observed and Limitations, with one finding per need."""
    o = _Observed(facts={f.id: f for f in facts}, plan=plan, observed=observed)
    _plan_limitations(o)
    _survival(o)
    _genes(o)
    _de(o)
    _panel_genes(o)
    _subsets(o)
    _empties(o)
    return o.findings, o.sentences, o.limitations


# --- The verifier ---------------------------------------------------------------------------------


class CodeVerifier:
    """interfaces.Verifier: code rules only, no model call."""

    def allow_list(self, vocab: Vocabulary, ledger: SessionLedger | None, plan: CompiledPlan) -> list[str]:
        """Terms model text may write outside placeholders (V-NUM): ORION's terms with digits
        (log2FC, Gen3 Analysis, six-gene panel), app names, the vocabulary's terms with digits,
        numerals or upper-case codes ("Stage II"), and the run's and the ledger's cohort names."""
        terms = [
            *FIXED_TERMS,
            *app_titles(),
            *vocabulary_terms(vocab),
            *sorted(vocabulary_acronyms(vocab)),
            *_with_labels(plan_cohort_names(plan)),
            *_with_labels(ledger_cohort_names(ledger)),
        ]
        genes = {g.casefold() for g in panel_genes(vocab)}
        unique: dict[str, str] = {}
        for term in terms:
            text = term.strip()
            # A bare gene symbol or number never becomes an allowed term, even as a cohort name.
            if text and text.casefold() not in genes and not re.fullmatch(r"[0-9.,\s]+", text):
                unique.setdefault(text.casefold(), text)
        return sorted(unique.values(), key=str.casefold)

    def _context(
        self, facts: Sequence[FactRecord], ledger: SessionLedger | None, vocab: Vocabulary, plan: CompiledPlan,
        observed: ObservedResult, allow_list: Sequence[str],
    ) -> _Context:
        by_id = {f.id: f for f in facts}
        viz = _with_labels(_viz_names(observed))
        terms = [*allow_list, *viz]
        names = {
            *(n.casefold() for n in _with_labels(plan_cohort_names(plan))),
            *(n.casefold() for n in _with_labels(ledger_cohort_names(ledger))),
            *(n.casefold() for n in viz),
            *(t.casefold() for t in app_titles()),
        }
        for spec in vocabulary_fields(vocab):
            names.add(spec.label.casefold())
            names.update(str(v).casefold() for v in spec.values or ())
        gene_facts = {str(f.value) for f in facts if f.unit == "gene" and isinstance(f.value, str)}
        name_genes = {t for n in [*plan_cohort_names(plan), *ledger_cohort_names(ledger), *viz] for t in gene_like_tokens(n)}
        genes = frozenset({*panel_genes(vocab), *gene_facts, *name_genes})
        return _Context(
            facts=by_id, terms=terms, acronyms=ACRONYMS | vocabulary_acronyms(vocab), names=frozenset(names), genes=genes,
        )

    def verify(
        self,
        composer_output: ComposerOutput,
        facts: Sequence[FactRecord],
        ledger: SessionLedger | None,
        vocab: Vocabulary,
        *,
        plan: CompiledPlan,
        observed: ObservedResult,
        attempt: int,
        allow_list: Sequence[str],
    ) -> VerifierResult:
        ctx = self._context(facts, ledger, vocab, plan, observed, allow_list)
        interpretation, bad_i = _statements(composer_output.interpretation, "interpretation", ctx)
        hypotheses, bad_h = _statements(composer_output.hypotheses, "hypotheses", ctx)
        follow_ups, bad_f = _follow_ups(composer_output.follow_ups, ctx)
        open_links = _open_links(composer_output.open_app_ids)
        code_findings, inserted_observed, inserted_limitations = check_observed(facts, plan, observed)
        model_failed = bad_i or bad_h or bad_f
        return VerifierResult(
            passed=not model_failed and not inserted_observed and not inserted_limitations,
            retry=model_failed and attempt <= 1,
            findings=ctx.findings + code_findings,
            interpretation=interpretation,
            hypotheses=hypotheses,
            follow_ups=follow_ups,
            open_links=open_links,
            inserted_observed=inserted_observed,
            inserted_limitations=inserted_limitations,
            observed_only=not any(s.dropped is None for s in interpretation),
        )


def model_text_feedback(result: VerifierResult) -> list[RuleFinding]:
    """The failed model-text findings, for the one re-compose."""
    return [f for f in result.findings if not f.passed and f.part in ("interpretation", "hypotheses", "follow_ups")]
