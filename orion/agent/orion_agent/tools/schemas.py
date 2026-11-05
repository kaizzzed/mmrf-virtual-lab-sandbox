"""The tool and step catalog as types: every tool's Input and Observed, the registry, the facts each
tool yields and the code templates that cite them (orion/contracts/TOOLS.md; RFA rev 25, A2 "Tools").

Observed is what `ToolResult.observed` holds after a tool normalises its upstream response, never
the raw body. Each field's docstring names the upstream field it comes from. Every model here
serialises camelCase, like the rest of the tool layer (`ToolModel`), except the ProteinPaint DE
rows, which keep ProteinPaint's own snake_case names because `rank_top_n` keeps its source rows
unchanged and fact pointers such as `/observed/rows/0/row/fold_change` read them.

How a value reaches a tool:

- A server tool's `StepSpec.args` is its Input in which a slot may hold a `StepRef` (the value at
  a JSON pointer into an earlier step's call). The executor resolves every StepRef
  (`resolve_step_refs`), validates the result against the Input model and records it as the
  call's `request`; the Gateway Lambda never sees a reference. `de_run` and `save_cohorts` steps
  carry their own args models (`DeRunStepArgs`, `SaveCohortsStepArgs`) because they also read the
  confirm read-back.
- An executor function's Input names earlier steps by bare step id (`{"curves": "s4"}`), and the
  call's `request` records it that way, as the fixtures do.

Model text, Observed and limitation templates cite facts through placeholders, `{{f.s10.top.1.gene}}`
(`PLACEHOLDER`, identical to `interfaces.PLACEHOLDER`). A template pattern binds a step role,
`{{f.<survival>.p}}`, or a whole fact, `{{<cohort_count>}}`, before it becomes that grammar.

This module imports only `models`, `catalog` and `tools.base`, so any builder can import it.
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ..catalog import AppId
from ..models import (
    FINGERPRINT_SCHEMA,
    MAX_TOP_N_DE,
    MAX_TOP_N_GENES,
    VIEW_RANK_CAP,
    CaseId,
    CohortContext,
    CohortName,
    CohortRefId,
    ConfirmAction,
    DeMethod,
    DeThresholds,
    Gen3Filter,
    LimitationCode,
    PlatformCohortId,
    SaveCohortsArgs,
    SavedReadBack,
    Sha256Hex,
    StepId,
    StepKind,
    VizReport,
    VizSpec,
    fingerprint_sha256,
)
from .base import (
    CASES_PAGE_SIZE,
    GATEWAY_TARGET,
    GATEWAY_TOOL_SEPARATOR,
    KG_GATEWAY_TARGET,
    MAX_CASE_IDS,
    MAX_DE_SAMPLES,
    MAX_SAMPLE_LIST_BODY_BYTES,
    MAX_TOOL_RESPONSE_BYTES,
    MIN_DE_ELIGIBLE,
    MIN_SURVIVAL_GROUP,
    TOOL_HTTP_TIMEOUT_S,
    ClientActionName,
    ExecutorFunctionName,
    Guard,
    KmMedianResult,
    KmPoint,
    RankResult,
    ServerToolName,
    SetOp,
    SetOperationResult,
    SubsetResult,
    ToolModel,
    ToolName,
)

CATALOG_VERSION: Final = "1"

# Golden bodies live in orion/golden (orion/golden/README.md). The agent image does not copy them,
# so a tool reads them from ORION_GOLDEN_DIR (default: <sandbox repo>/orion/golden).
GOLDEN_DIR_ENV: Final = "ORION_GOLDEN_DIR"

# Engines, as facts record them (FactRecord.engine). The versions are the deployed sandbox's pins:
# sources.json commits for Gen3 Analysis and Guppy, the healthcheck's server version for ProteinPaint.
ENGINE_GEN3_ANALYSIS: Final = "gen3-analysis@ffa4069"
ENGINE_GUPPY: Final = "guppy@c687656"
ENGINE_PROTEINPAINT: Final = "proteinpaint@2.216.0"
ENGINE_FINGERPRINT: Final = "fixture-fingerprint@1"

# The six-gene mutation panel and the Ensembl ids the gene-centric index stores (vocab_supplement.json).
PanelGeneSymbol = Literal["KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC"]
PANEL_GENES: Final[tuple[PanelGeneSymbol, ...]] = get_args(PanelGeneSymbol)
PANEL_GENE_IDS: Final[dict[PanelGeneSymbol, str]] = {
    "KRAS": "ENSG00000133703",
    "NRAS": "ENSG00000213281",
    "TP53": "ENSG00000141510",
    "BRAF": "ENSG00000157764",
    "DIS3": "ENSG00000083520",
    "MYC": "ENSG00000136997",
}

SURVIVAL_TERM: Final = "overall_survival"
# The ProteinPaint survival server prints a log-rank p below double precision as "0" (it computes
# 1 - pchisq), so the chart's table shows p = 0 under this floor: VIEWER_P_ROUNDED (Assumption).
VIEWER_P_FLOOR: Final = 1e-16
# The largest upstream body a tool reads before it gives up (UPSTREAM_SHAPE). Observed itself stays
# below MAX_TOOL_RESPONSE_BYTES, Lambda's synchronous limit.
MAX_UPSTREAM_BYTES: Final = 16 * 1024 * 1024
# The suffix every name ORION proposes for a saved cohort carries; labels drop it.
ORION_SUFFIX: Final = " (ORION)"
# The tvslst ProteinPaint takes for "no native filter": the case scope travels in filter0 instead.
EMPTY_TVSLST: Final[dict[str, JsonValue]] = {"type": "tvslst", "in": True, "join": "", "lst": []}
ALL_CASES_FILTER: Final[dict[str, JsonValue]] = {"and": []}

DeGroupKey = Literal["control", "case"]
DE_GROUP_KEYS: Final[tuple[DeGroupKey, DeGroupKey]] = ("control", "case")


def gateway_tool_name(name: ServerToolName) -> str:
    """The MCP tool name on AgentCore Gateway: `vlab___<tool>`, or `kg___kg_query`."""
    target = KG_GATEWAY_TARGET if name == "kg_query" else GATEWAY_TARGET
    return f"{target}{GATEWAY_TOOL_SEPARATOR}{name}"


def case_id_filter(case_ids: Sequence[str]) -> dict[str, JsonValue]:
    """The Gen3 filter of a case-ID list, ids sorted: what C-23 and C-CASEIDS hand every later step."""
    ids: list[JsonValue] = [case for case in sorted(set(case_ids))]
    return {"and": [{"in": {"case_id": ids}}]}


def minus_log10(cutoff: float) -> float:
    """The viewer's -log10 p cutoff to two decimals, half away from zero (0.05 -> 1.3), exactly as
    derive_request_pinned writes the volcano pin, so the DE body and the card agree."""
    if not 0 < cutoff <= 1:
        raise ValueError("a p-value cutoff is in (0, 1]")
    return float(Decimal(repr(-math.log10(cutoff))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class CatalogModel(ToolModel):
    """ToolModel with field docstrings as JSON Schema descriptions (the MCP inputSchema carries them)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


JsonPointer = Annotated[str, Field(pattern=r"^(/([^/~]|~[01])*)+$")]
NonNegative = Annotated[int, Field(ge=0)]
SampleId = Annotated[int, Field(ge=1)]


# --- References inside StepSpec.args ------------------------------------------------------------


class StepRef(CatalogModel):
    """A value an earlier step produced, resolved by the executor before the call (StepSpec.args only).

    A JSON object whose keys are exactly `ref` and `pointer` (and optionally `intersect`) is a
    StepRef wherever it appears in args; no filter or body ever has that shape.
    """

    ref: StepId
    """The step whose last successful call holds the value."""
    pointer: JsonPointer
    """A JSON pointer into that call, starting /observed, as facts point (such as /observed/caseIds)."""
    intersect: StepId | None = Field(default=None, exclude_if=lambda value: value is None)
    """For a case-ID list: keep only the ids also in this step's /observed/caseIds (C-UNTESTED)."""


_STEP_REF_KEYS: Final = frozenset({"ref", "pointer", "intersect"})
StepValueReader = Callable[[str, str], JsonValue]


def is_step_ref(value: JsonValue) -> bool:
    return isinstance(value, dict) and {"ref", "pointer"} <= value.keys() <= _STEP_REF_KEYS


def resolve_step_refs(value: JsonValue, read: StepValueReader) -> JsonValue:
    """Replace every StepRef in `value` by what `read(step_id, pointer)` returns (deep, order kept).

    With `intersect`, the referenced value must be a list of case ids; the result keeps only the
    ids also in the intersect step's /observed/caseIds, sorted.
    """
    if isinstance(value, dict):
        if is_step_ref(value):
            ref = StepRef.model_validate(value)
            resolved = read(ref.ref, ref.pointer)
            if ref.intersect is None:
                return resolved
            other = read(ref.intersect, "/observed/caseIds")
            if not isinstance(resolved, list) or not isinstance(other, list):
                raise ValueError(f"{ref.ref}{ref.pointer} and {ref.intersect}/observed/caseIds must both be case-ID lists")
            keep = {item for item in other if isinstance(item, str)}
            inside: list[JsonValue] = [item for item in sorted({i for i in resolved if isinstance(i, str) and i in keep})]
            return inside
        return {key: resolve_step_refs(item, read) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_step_refs(item, read) for item in value]
    return value


# --- Server tools: Input and Observed ------------------------------------------------------------


class FixtureFingerprintInput(CatalogModel):
    """No input: s1 always reads the same four sources."""


class FingerprintSandbox(CatalogModel):
    status: str
    """GET /sandbox/status .status."""
    synthetic: bool
    """GET /sandbox/status .synthetic."""
    cases: NonNegative
    """GET /sandbox/status .cases (csrf is left out)."""


class FingerprintAnalysis(CatalogModel):
    version: str
    """GET /analysis/v0/_version .version."""


class FingerprintProteinPaint(CatalogModel):
    pkgver: str
    """GET /protein-paint/healthcheck .versionInfo.pkgver."""
    codedate: str
    """GET /protein-paint/healthcheck .versionInfo.codedate (launchdate is left out)."""


class MutatedGene(CatalogModel):
    gene: str
    """topMutatedGenes .genes[].gene."""
    cases: NonNegative
    """topMutatedGenes .genes[].mutationStat[dt == 1].count: SNV/indel-mutated cases."""


class FingerprintDocument(CatalogModel):
    """models.fingerprint_document's output: the document behind fingerprintSha256."""

    schema_: Literal["orion.fixture-fingerprint/v1"] = Field(alias="schema")
    sandbox: FingerprintSandbox
    analysis: FingerprintAnalysis
    proteinpaint: FingerprintProteinPaint
    ranking: list[MutatedGene]
    """The all-case topMutatedGenes ranking (golden body unchanged), in returned order."""


class FixtureFingerprintObserved(CatalogModel):
    document: FingerprintDocument
    fingerprint_sha256: Sha256Hex
    """models.fingerprint_sha256(document): RunView.environment.fingerprintSha256."""

    @model_validator(mode="after")
    def _hash_matches(self) -> "FixtureFingerprintObserved":
        if fingerprint_sha256(self.document.model_dump(mode="json", by_alias=True)) != self.fingerprint_sha256:
            raise ValueError("fingerprintSha256 is not the hash of the document")
        if self.document.schema_ != FINGERPRINT_SCHEMA:
            raise ValueError("unknown fingerprint schema")
        return self


class CohortCountInput(CatalogModel):
    filter: Gen3Filter
    """The Gen3 filter to count, `and`-wrapped; a filtered scope arrives as its case-ID clause (C-23)."""


class CohortCountObserved(CatalogModel):
    count: NonNegative
    """POST /guppy/graphql data.CaseCentric__aggregation.case_centric._totalCount."""


class CasesQueryInput(CatalogModel):
    filter: Gen3Filter
    """The Gen3 filter whose case IDs to list, `and`-wrapped."""
    page_size: int = Field(default=CASES_PAGE_SIZE, ge=1, le=CASES_PAGE_SIZE)
    """Sent as `size`; pages are read at offsets 0, page_size, ... while offset + size <= max_ids."""
    max_ids: int = Field(default=MAX_CASE_IDS, ge=1, le=MAX_CASE_IDS)
    """The most case IDs the step may return (C-GUARD); more is TOO_MANY_IDS."""


class CasesQueryObserved(CatalogModel):
    count: NonNegative
    """POST /analysis/v0/cases/ .pagination.total of the first page."""
    case_ids: list[CaseId] = Field(max_length=MAX_CASE_IDS)
    """Every page's .data[].case_id, deduplicated and sorted; empty when count > max_ids."""
    pages: NonNegative
    """How many pages were read."""
    complete: bool
    """Every matching case ID is listed (false only with TOO_MANY_IDS)."""

    @model_validator(mode="after")
    def _ids_match_count(self) -> "CasesQueryObserved":
        if self.complete != (len(self.case_ids) == self.count):
            raise ValueError("complete is true exactly when every counted case ID is listed")
        if self.case_ids != sorted(set(self.case_ids)):
            raise ValueError("caseIds are unique and sorted")
        return self


class GeneCasesInput(CatalogModel):
    gene: PanelGeneSymbol
    """The panel gene; its Ensembl id goes in gene_filter (PANEL_GENE_IDS)."""
    cohort_filter: Gen3Filter
    """Sent as cohort_filter: the scope's filter, `{"and": []}` for all cases."""


class GeneCasesObserved(CatalogModel):
    gene: PanelGeneSymbol
    """The gene asked for (echoed)."""
    count: NonNegative
    """len(caseIds)."""
    case_ids: list[CaseId] = Field(max_length=MAX_CASE_IDS)
    """POST /analysis/v0/genomic/gene_cases: the returned case-ID array, deduplicated and sorted."""

    @model_validator(mode="after")
    def _count_is_len(self) -> "GeneCasesObserved":
        if self.count != len(self.case_ids) or self.case_ids != sorted(set(self.case_ids)):
            raise ValueError("count is len(caseIds), and caseIds are unique and sorted")
        return self


class TopMutatedGenesInput(CatalogModel):
    filter0: Gen3Filter | None
    """The cohort as Gen3 filter0 (a case-ID list, C-CASEIDS); null for all cases. Pinned as the card's filter0."""
    gene_count: int = Field(ge=1, le=MAX_TOP_N_GENES)
    """How many genes the card shows: top N (sent as maxGenes = geneCount + 1, the extra row only detects a tie)."""


class TopMutatedGenesObserved(CatalogModel):
    genes: list[MutatedGene] = Field(max_length=MAX_TOP_N_GENES)
    """POST /protein-paint/termdb/topMutatedGenes .genes, the first geneCount rows in returned order."""
    next_gene: MutatedGene | None = Field(default=None, exclude_if=lambda value: value is None)
    """The (geneCount + 1)-th returned row, if any: read only so rank_top_n can see a tie at the cutoff."""


class SurvivalSplit(CatalogModel):
    field: str
    """The vocabulary field the groups split (C-SPLIT), such as cgs_risk_key_criteria.cgs_risk_category."""
    overlay: str
    """That field's ProteinPaint twin term id (vocab proteinpaintTwins), the chart's overlay term."""


class SurvivalGroupInput(CatalogModel):
    label: str = Field(min_length=1, max_length=200)
    """The group's display label: the split value, or the cohort's label."""
    filter: Gen3Filter
    """The group's complete Gen3 filter, the scope included; sent alone, then with the others."""


class SurvivalCompareInput(CatalogModel):
    """Exactly what derive_request_pinned("survival") reads: scope, term and split {field, overlay}."""

    scope: Gen3Filter | None
    """The scope as Gen3 filter (the scope step's case-ID clause); null for all cases. The card's filter0."""
    term: Literal["overall_survival"]
    """The survival term; Gen3 Analysis' default survivalType "overall" (the body sends none)."""
    split: SurvivalSplit | None
    """The split field and its twin; null when the groups are cohorts with no twin (no survival card)."""
    groups: list[SurvivalGroupInput] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _labels_unique(self) -> "SurvivalCompareInput":
        labels = [g.label for g in self.groups]
        if len(labels) != len(set(labels)):
            raise ValueError("survival group labels are unique")
        return self


class SurvivalGroupObserved(CatalogModel):
    label: str
    """The input group's label."""
    cases: NonNegative
    """len(results[0].donors) of the group's own call: cases with survival data."""
    events: NonNegative
    """Donors with censored false in the group's own call."""
    curve: list[KmPoint]
    """(0, 1.0), then one point per distinct donor time: the engine's estimate just after it (TOOLS.md)."""

    @model_validator(mode="after")
    def _events_within_cases(self) -> "SurvivalGroupObserved":
        if self.events > self.cases:
            raise ValueError("events cannot exceed cases")
        return self


class SurvivalCompareObserved(CatalogModel):
    groups: list[SurvivalGroupObserved] = Field(min_length=2, max_length=6)
    """One per input group, in input order."""
    p_value: float | None = Field(ge=0, le=1)
    """The combined call's overallStats.pValue (log-rank); null when fewer than two groups had curves."""
    engine: str
    """The engine that computed p, ENGINE_GEN3_ANALYSIS: the answer's p comes from Gen3 Analysis."""


class CaseIdGroup(CatalogModel):
    key: DeGroupKey
    """control (DE group 1) or case (DE group 2)."""
    case_ids: list[CaseId] = Field(max_length=MAX_CASE_IDS)
    """The group's case IDs, sent as filter0 = case_id_filter(caseIds)."""


def _control_then_case(keys: Sequence[str]) -> None:
    if tuple(keys) != DE_GROUP_KEYS:
        raise ValueError("the two DE groups are control, then case")


class SampleListsInput(CatalogModel):
    groups: list[CaseIdGroup] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _ordered(self) -> "SampleListsInput":
        _control_then_case([g.key for g in self.groups])
        return self


class SampleGroupObserved(CatalogModel):
    key: DeGroupKey
    count: NonNegative
    """len(sampleIds)."""
    sample_ids: list[SampleId]
    """POST /protein-paint/termdb (getsamplelist) .[].id, ascending: ProteinPaint integer sample ids."""

    @model_validator(mode="after")
    def _count_is_len(self) -> "SampleGroupObserved":
        if self.count != len(self.sample_ids) or self.sample_ids != sorted(set(self.sample_ids)):
            raise ValueError("count is len(sampleIds), and sampleIds are unique and ascending")
        return self


class SampleListsObserved(CatalogModel):
    groups: list[SampleGroupObserved] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _ordered(self) -> "SampleListsObserved":
        _control_then_case([g.key for g in self.groups])
        return self


class SampleIdGroup(CatalogModel):
    key: DeGroupKey
    """Also the samplelst group name sent, so pre-analysis never depends on a cohort's name."""
    sample_ids: list[SampleId]
    """The group's ProteinPaint sample ids (sample_lists), sent as samplelst values [{sampleId}]."""


class DePreanalysisInput(CatalogModel):
    groups: list[SampleIdGroup] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _ordered(self) -> "DePreanalysisInput":
        _control_then_case([g.key for g in self.groups])
        return self


class EligibleGroup(CatalogModel):
    key: DeGroupKey
    eligible: NonNegative
    """POST /protein-paint/termdb/DE (preAnalysis) .data[<group name>]: RNA-seq-eligible samples."""


class DePreanalysisObserved(CatalogModel):
    groups: list[EligibleGroup] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _ordered(self) -> "DePreanalysisObserved":
        _control_then_case([g.key for g in self.groups])
        return self


class DeRunGroup(CatalogModel):
    """One DE group, as derive_request_pinned reads it: name and sampleIds, plus cohortId and filter0 once saved."""

    name: CohortName
    """The read-back saved name after Create, else the proposed name (C-24): part of ProteinPaint's DE cache key."""
    sample_ids: list[SampleId]
    """The group's sample ids in sample_lists' order (ascending), sent as samplelst values."""
    cohort_id: PlatformCohortId | None = Field(default=None, exclude_if=lambda value: value is None)
    """The read-back cohort id, present only once the cohort is saved or reused (deInput)."""
    filter0: Gen3Filter | None = Field(default=None, exclude_if=lambda value: value is None)
    """The saved cohort's case-ID filter (case_id_filter), present with cohortId (deInput)."""

    @model_validator(mode="after")
    def _saved_together(self) -> "DeRunGroup":
        if (self.cohort_id is None) != (self.filter0 is None):
            raise ValueError("cohortId and filter0 are present together, once the cohort is saved")
        return self


class DeRunInput(CatalogModel):
    groups: list[DeRunGroup] = Field(min_length=2, max_length=2)
    """Control first, then case (C-DEFAULTS)."""
    method: DeMethod
    """Sent as method."""
    thresholds: DeThresholds | None = Field(default=None, exclude_if=lambda value: value is None)
    """Sent as volcanoRender.significanceThresholds (pValueCutoff as -log10); absent keeps the viewer's."""

    @model_validator(mode="after")
    def _names_distinct(self) -> "DeRunInput":
        if self.groups[0].name == self.groups[1].name:
            raise ValueError("the two DE groups have different names")
        return self


class DeGene(BaseModel):
    """One ProteinPaint DE row past the thresholds, with ProteinPaint's own field names."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    gene_name: str
    """data.dots[].gene_name."""
    fold_change: float
    """data.dots[].fold_change: log2(case / control)."""
    original_p_value: float = Field(ge=0, le=1)
    """data.dots[].original_p_value."""
    adjusted_p_value: float = Field(ge=0, le=1)
    """data.dots[].adjusted_p_value."""


class DeSampleSizes(CatalogModel):
    control: NonNegative
    """sample_size1."""
    case: NonNegative
    """sample_size2."""


class DeTotals(CatalogModel):
    tested: NonNegative
    """data.totalRows: genes tested."""
    significant: NonNegative
    """data.totalSignificantRows: genes past the thresholds (len(genes))."""
    up: NonNegative
    """data.totalSignificantUp."""
    down: NonNegative
    """data.totalSignificantDown."""


class DeRunObserved(CatalogModel):
    method: DeMethod
    """.method."""
    sample_sizes: DeSampleSizes
    totals: DeTotals
    genes: list[DeGene]
    """data.dots in returned order, without index, pixel_x and pixel_y; volcanoPng and plotExtent are dropped."""
    cache_id: str | None
    """data.cacheId: ProteinPaint's DE cache entry, which the viewer's identical request reads."""

    @model_validator(mode="after")
    def _totals_match(self) -> "DeRunObserved":
        if self.totals.significant != len(self.genes):
            raise ValueError("totals.significant is the number of genes returned")
        return self


class KgQueryInput(CatalogModel):
    question: str = Field(min_length=1, max_length=2000)
    """The question for the Workstream 1 knowledge-graph agent."""
    genes: list[PanelGeneSymbol] = Field(default_factory=list, max_length=6)
    """Genes the question is about."""


class KgStatement(CatalogModel):
    text: str
    """A statement the knowledge-graph agent returned."""
    source: str
    """Its citation, as the agent names it."""


class KgQueryObserved(CatalogModel):
    statements: list[KgStatement]


# --- Executor pure functions: Input (results are base.py's) --------------------------------------


class SetOperationInput(CatalogModel):
    op: SetOp
    a: StepId
    """The step whose /observed/caseIds is the left set."""
    b: StepId | None = Field(default=None, exclude_if=lambda value: value is None)
    """The right set's step; absent for op size."""

    @model_validator(mode="after")
    def _operands(self) -> "SetOperationInput":
        if (self.op == "size") != (self.b is None):
            raise ValueError("op size takes one set; every other op takes two")
        return self


class CheckSubsetInput(CatalogModel):
    inner: StepId
    """The step whose /observed/caseIds must lie inside outer's (the gene-mutated cases)."""
    outer: StepId
    """The containing step (the mutation-tested cases)."""


RankBy = Literal["adjusted_p_value", "original_p_value", "abs(fold_change)", "cases"]
RankKey = Literal["gene_name", "gene"]
RankSourceTool = Literal["de_run", "top_mutated_genes"]


class RankTopNInput(CatalogModel):
    rows: StepId
    """The step whose rows are ranked (RANK_SOURCES gives the pointer per tool)."""
    key: RankKey
    """The row field that names a row: gene_name (DE) or gene (top mutated genes)."""
    rank_by: RankBy
    """The ranking value: a row field, or abs(<field>)."""
    n: int = Field(ge=1, le=MAX_TOP_N_DE)
    """Top N (the plan's topN)."""
    ascending: bool
    """True for p-values, false for fold change and case counts."""
    tie_break: RankKey
    """Orders rows with equal values (code point order); ties at the cutoff are kept either way."""
    thresholds: DeThresholds | None = Field(default=None, exclude_if=lambda value: value is None)
    """DE only: rows must pass p < cutoff (of pValueType) and |fold_change| > cutoff before ranking."""


class DeriveKmMedianInput(CatalogModel):
    curves: StepId
    """The survival_compare step whose /observed/groups[].curve are read, labels kept."""


@dataclass(frozen=True)
class RankSource:
    """Where rank_top_n reads its rows for one source tool."""

    pointer: str
    extra_pointer: str | None  # one more row appended after `pointer`'s rows when present
    key: RankKey


RANK_SOURCES: Final[dict[RankSourceTool, RankSource]] = {
    "de_run": RankSource(pointer="/observed/genes", extra_pointer=None, key="gene_name"),
    "top_mutated_genes": RankSource(pointer="/observed/genes", extra_pointer="/observed/nextGene", key="gene"),
}
# IR rank_by (ir.DifferentialExpression) to rank_top_n's rankBy and direction.
DE_RANK_BY: Final[dict[str, tuple[RankBy, bool]]] = {
    "adjusted_p": ("adjusted_p_value", True),
    "p_value": ("original_p_value", True),
    "fold_change": ("abs(fold_change)", False),
}


# --- Step args that read the confirm read-back (de_run, save_cohorts) ------------------------------


class DeRunStepGroup(CatalogModel):
    name: CohortName
    """The proposed cohort name: an edit renames it here (contract "What an edit changes")."""
    cohort: CohortRefId
    """The planned cohort the group is; its read-back replaces name and adds cohortId and filter0."""
    sample_ids: StepRef
    """sample_lists' /observed/groups/<i>/sampleIds."""


class DeRunStepArgs(CatalogModel):
    groups: list[DeRunStepGroup] = Field(min_length=2, max_length=2)
    method: DeMethod
    thresholds: DeThresholds | None = Field(default=None, exclude_if=lambda value: value is None)


@dataclass(frozen=True)
class SavedGroup:
    """A planned cohort after Create: the read-back name and id, and its case-ID filter."""

    name: str
    cohort_id: str
    filter0: dict[str, JsonValue]


def de_run_input(args: DeRunStepArgs, read: StepValueReader, saved: Mapping[str, SavedGroup]) -> DeRunInput:
    """The DE request: read-back names for saved (or reused) cohorts, the proposed names otherwise (C-24)."""
    groups: list[DeRunGroup] = []
    for group in args.groups:
        sample_ids = read(group.sample_ids.ref, group.sample_ids.pointer)
        if not isinstance(sample_ids, list):
            raise ValueError(f"{group.sample_ids.ref}{group.sample_ids.pointer} is not a sample-id list")
        hit = saved.get(group.cohort)
        if hit is None:
            groups.append(DeRunGroup.model_validate({"name": group.name, "sampleIds": sample_ids}))
        else:
            groups.append(
                DeRunGroup.model_validate(
                    {"name": hit.name, "sampleIds": sample_ids, "cohortId": hit.cohort_id, "filter0": hit.filter0}
                )
            )
    return DeRunInput(groups=groups, method=args.method, thresholds=args.thresholds)


class SaveStepCohort(CatalogModel):
    ref: CohortRefId
    """The planned cohort; its definition, defSha256 and count come from the step that defined it."""
    name: CohortName
    """The proposed name, " (ORION)" suffixed; Rename edits it."""
    eligible: StepRef | None = Field(default=None, exclude_if=lambda value: value is None)
    """de_preanalysis' /observed/groups/<i>/eligible, for the card's eligible count (J3); absent for J2."""


class SaveCohortsStepArgs(CatalogModel):
    cohorts: list[SaveStepCohort] = Field(min_length=1, max_length=6)
    """In confirmation-card order: control first for DE."""


# --- Client actions ---------------------------------------------------------------------------------


class ReadCohortContextArgs(CatalogModel):
    """No args: the browser reads its cohort selectors on every turn (models.CohortContext)."""


class SaveCohortsObserved(CatalogModel):
    """What the write step's call records after a verified Create (models.ConfirmAction's read-back)."""

    decision: Literal["create"]
    saved: list[SavedReadBack] = Field(min_length=1, max_length=6)
    """Every pending cohort once, read back from the cohort store."""
    make_active: CohortRefId | None
    """The cohort the browser made active inside the click, recorded only."""


class SetActiveCohortArgs(CatalogModel):
    cohort_id: PlatformCohortId | None
    """The cohort to make active after an existence check; null for all cases."""
    name: str = Field(min_length=1, max_length=200)
    """Shown in the undo toast."""
    origin: Literal["scope_chip", "make_active_and_open", "confirm_make_active"]
    """The click that ran it: the composer's scope chip, a hand-off, or Create with make-active."""


class SetActiveCohortResult(CatalogModel):
    cohort_id: PlatformCohortId | None
    previous_cohort_id: PlatformCohortId | None
    """What Undo restores."""
    undone: bool


class OpenFullToolArgs(CatalogModel):
    app_id: AppId
    """Navigates the same tab to /?app=<appId>."""
    cohort_id: PlatformCohortId | None
    """The card's pinned cohort; null opens the tool with no cohort switch."""
    make_active: bool
    """True when the button reads "Make active and open" (a set_active_cohort with undo runs first)."""


# --- Upstream request templates ---------------------------------------------------------------------


@dataclass(frozen=True)
class Substitution:
    pointer: str  # a JSON pointer into the template payload
    source: str  # the Input field (camelCase) or rule that fills it


@dataclass(frozen=True)
class RequestTemplate:
    """One upstream request a server tool sends through the identity gateway.

    The payload starts as the golden request (`orion/golden/<golden>`, `requests[request_index]`,
    its `query` or `body`) or as `inline`; `substitutions` replace values at their pointers and
    `fixed` pointers keep the captured value. Every request goes out as a JSON POST of that payload
    except a payload-free GET (the gateway turns ProteinPaint GETs into the same POST anyway).
    """

    method: Literal["GET", "POST"]
    path: str
    golden: str | None
    request_index: int
    payload: Literal["query", "body", "none"]
    inline: JsonValue
    substitutions: tuple[Substitution, ...]
    fixed: tuple[str, ...]
    calls: str  # how many times one tool call sends it

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path}"


COHORT_COUNT_QUERY: Final = (
    "query ($filter: JSON) { CaseCentric__aggregation { case_centric(filter: $filter, accessibility: accessible)"
    " { _totalCount } } }"
)

FINGERPRINT_REQUESTS: Final = (
    RequestTemplate("GET", "/sandbox/status", None, 0, "none", None, (), (), "once"),
    RequestTemplate("GET", "/analysis/v0/_version", None, 0, "none", None, (), (), "once"),
    RequestTemplate("GET", "/protein-paint/healthcheck", None, 0, "none", None, (), (), "once"),
    RequestTemplate(
        "POST", "/protein-paint/termdb/topMutatedGenes", "proteinpaint/termdb-topMutatedGenes.json", 0, "query", None, (),
        ("/genome", "/dslabel", "/maxGenes", "/geneFilter", "/filter0"), "once: the all-case ranking, golden body unchanged",
    ),
)
COHORT_COUNT_REQUEST: Final = RequestTemplate(
    "POST", "/guppy/graphql", None, 0, "body", {"query": COHORT_COUNT_QUERY, "variables": {"filter": ALL_CASES_FILTER}},
    (Substitution("/variables/filter", "filter"),), ("/query",), "once",
)
CASES_QUERY_REQUEST: Final = RequestTemplate(
    "POST", "/analysis/v0/cases/", None, 0, "body",
    {"filter": ALL_CASES_FILTER, "fields": ["case_id"], "size": CASES_PAGE_SIZE, "offset": 0},
    (Substitution("/filter", "filter"), Substitution("/size", "pageSize"), Substitution("/offset", "page index x pageSize")),
    ("/fields",), "once per page, while offset + size <= maxIds",
)
GENE_CASES_REQUEST: Final = RequestTemplate(
    "POST", "/analysis/v0/genomic/gene_cases", None, 0, "body",
    {"cohort_filter": ALL_CASES_FILTER, "gene_filter": {"and": [{"in": {"gene_id": [PANEL_GENE_IDS["KRAS"]]}}]}, "ssm_filter": {}},
    (Substitution("/cohort_filter", "cohortFilter"), Substitution("/gene_filter/and/0/in/gene_id/0", "PANEL_GENE_IDS[gene]")),
    ("/gene_filter/and/0/in", "/ssm_filter"), "once",
)
TOP_MUTATED_GENES_REQUEST: Final = RequestTemplate(
    "POST", "/protein-paint/termdb/topMutatedGenes", "proteinpaint/termdb-topMutatedGenes.json", 0, "query", None,
    (Substitution("/maxGenes", "geneCount + 1"), Substitution("/filter0", "filter0, or {\"and\": []} when null")),
    ("/genome", "/dslabel", "/geneFilter"), "once",
)
SURVIVAL_REQUEST: Final = RequestTemplate(
    "POST", "/analysis/v0/survival/", "analysis/survival.json", 1, "body", None,
    (Substitution("/filters", "[groups[i].filter] for each group, then every group's filter in order"),),
    (), "once per group, then once with all groups (the log-rank p)",
)
SAMPLE_LISTS_REQUEST: Final = RequestTemplate(
    "POST", "/protein-paint/termdb", "proteinpaint/termdb-getsamplelist.json", 0, "query", None,
    (Substitution("/filter", "EMPTY_TVSLST"), Substitution("/filter0", "case_id_filter(groups[i].caseIds)")),
    ("/genome", "/dslabel", "/getsamplelist", "/mapParent2Children"), "once per group, control first",
)
DE_PREANALYSIS_REQUEST: Final = RequestTemplate(
    "POST", "/protein-paint/termdb/DE", "proteinpaint/termdb-DE-preAnalysis.json", 0, "body", None,
    (
        Substitution("/samplelst/groups/0/name", "groups[0].key (control)"),
        Substitution("/samplelst/groups/0/values", "[{sampleId}] of groups[0].sampleIds"),
        Substitution("/samplelst/groups/1/name", "groups[1].key (case)"),
        Substitution("/samplelst/groups/1/values", "[{sampleId}] of groups[1].sampleIds"),
    ),
    ("/genome", "/dslabel", "/samplelst/groups/0/in", "/samplelst/groups/1/in", "/filter", "/filter0", "/preAnalysis"),
    "once",
)
DE_RUN_REQUEST: Final = RequestTemplate(
    "POST", "/protein-paint/termdb/DE", "proteinpaint/termdb-DE-wilcoxon.json", 0, "body", None,
    (
        Substitution("/method", "method"),
        Substitution("/samplelst/groups/0/name", "groups[0].name"),
        Substitution("/samplelst/groups/0/values", "[{sampleId}] of groups[0].sampleIds"),
        Substitution("/samplelst/groups/1/name", "groups[1].name"),
        Substitution("/samplelst/groups/1/values", "[{sampleId}] of groups[1].sampleIds"),
        Substitution("/filter0", "null, as the direct DE card sends it (same cache key)"),
        Substitution("/volcanoRender/significanceThresholds/pValueCutoff", "minus_log10(thresholds.pValueCutoff)"),
        Substitution("/volcanoRender/significanceThresholds/pValueType", "thresholds.pValueType"),
        Substitution("/volcanoRender/significanceThresholds/foldChangeCutoff", "thresholds.foldChangeCutoff"),
    ),
    (
        "/kind", "/genome", "/dslabel", "/min_count", "/min_total_count", "/cpm_cutoff", "/filter",
        "/samplelst/groups/0/in", "/samplelst/groups/1/in", "/volcanoRender/pixelWidth", "/volcanoRender/pixelHeight",
        "/volcanoRender/colorSignificant", "/volcanoRender/colorSignificantUp", "/volcanoRender/colorSignificantDown",
        "/volcanoRender/colorNonsignificant", "/volcanoRender/dotRadius", "/volcanoRender/maxInteractiveDots",
        "/volcanoRender/devicePixelRatio",
    ),
    "once; a retry resends the identical body",
)


# --- Guards -------------------------------------------------------------------------------------------

GuardEffect = Literal["stop", "report"]
GuardCheckedBy = Literal["executor", "tool"]


@dataclass(frozen=True)
class GuardSpec:
    """A guard with how the run reacts. `stop` ends the run Limited once the step settles (its
    dependents are Skipped); `report` adds the limitation and the run goes on."""

    guard: Guard
    effect: GuardEffect
    checked_by: GuardCheckedBy  # the executor before the call (pre) or the tool on its own result
    step_status: Literal["Skipped", "Empty", "Done"]  # the step's status when the guard fails
    meaning: str


def _guard(
    rule: str, phase: Literal["pre", "post"], on_fail: LimitationCode, effect: GuardEffect, by: GuardCheckedBy,
    status: Literal["Skipped", "Empty", "Done"], meaning: str,
) -> GuardSpec:
    return GuardSpec(Guard(rule=rule, on_fail=on_fail, phase=phase), effect, by, status, meaning)


G_NONEMPTY: Final = _guard("count>0", "post", "EMPTY_COHORT", "stop", "tool", "Empty", "A zero count: status empty, the zero is a fact")
G_MAX_IDS: Final = _guard(
    "case_ids<=10000", "post", "TOO_MANY_IDS", "stop", "tool", "Done", "More matches than one case-ID list carries; caseIds empty"
)
G_PANEL: Final = _guard("gene_in_panel", "pre", "OUT_OF_PANEL", "stop", "tool", "Skipped", "Six-gene panel only (C-PANEL stops it first)")
G_BRIDGE_SAFE: Final = _guard(
    "filter0_bridge_safe", "pre", "UNSUPPORTED_FIELD", "stop", "executor", "Skipped",
    "filter0 names only case_id or fields the ProteinPaint bridge reads (vocab es_only false)",
)
G_SURVIVAL_GROUP: Final = _guard(
    f"group_cases>={MIN_SURVIVAL_GROUP}", "pre", "INELIGIBLE_GROUP", "stop", "executor", "Skipped",
    "Each group's cohort_count (dependsOn) is at least 10",
)
G_SURVIVAL_CURVES: Final = _guard(
    "every_group_has_a_curve", "post", "EMPTY_COHORT", "stop", "tool", "Empty", "A group's own call returned no curve"
)
G_CASE_LISTS: Final = _guard("case_lists_nonempty", "pre", "EMPTY_COHORT", "stop", "tool", "Skipped", "Both case-ID lists are non-empty")
G_BODY_SIZE: Final = _guard(
    f"body_bytes<={MAX_SAMPLE_LIST_BODY_BYTES}", "pre", "REQUEST_TOO_LARGE", "stop", "tool", "Skipped",
    "Each sample-list body is under the gateway's 8 MiB",
)
G_SAMPLES_FOUND: Final = _guard("samples>0_per_group", "post", "EMPTY_COHORT", "stop", "tool", "Empty", "Each group maps to a sample")
G_DE_SAMPLES: Final = _guard(
    f"samples<={MAX_DE_SAMPLES}", "pre", "REQUEST_TOO_LARGE", "stop", "tool", "Skipped", "Both groups together have at most 4,000 samples"
)
G_ELIGIBLE: Final = _guard(
    f"eligible>={MIN_DE_ELIGIBLE}_per_group", "post", "INELIGIBLE_GROUP", "stop", "tool", "Done",
    "Each group has at least 10 RNA-seq-eligible samples; stops before the save",
)
G_SUBSET: Final = _guard(
    "inner_subset_of_outer", "post", "NOT_SUBSET", "report", "tool", "Done", "Mutated cases outside the tested cases are dropped and reported"
)
G_ROWS: Final = _guard("rows>=n", "post", "FEWER_THAN_N", "report", "tool", "Done", "Fewer rows than top N (zero rows: status empty)")
G_TIES: Final = _guard("no_ties_at_cutoff", "post", "TIES_AT_CUTOFF", "report", "tool", "Done", "Rows tied with the N-th are kept and named")
G_GENES: Final = _guard("genes>0", "post", "FEWER_THAN_N", "report", "tool", "Empty", "No mutated gene: status empty; the ranking reports it")
G_KG: Final = _guard("kg_target_registered", "pre", "KG_NOT_CONNECTED", "stop", "tool", "Skipped", "The kg Gateway target exists")


# --- Facts ----------------------------------------------------------------------------------------------

# The fact id grammar: f.<step id>.<suffix>, the suffix one or more [A-Za-z0-9_-] segments joined by
# dots. A fact id is also its placeholder: {{f.s10.top.1.gene}} (interfaces.PLACEHOLDER).
FACT_ID: Final = re.compile(r"^f\.(s[1-9][0-9]*)((?:\.[A-Za-z0-9_-]+)+)$")
FACT_SEGMENT: Final = re.compile(r"^[A-Za-z0-9_-]+$")
PLACEHOLDER: Final = re.compile(r"\{\{(f\.s[1-9][0-9]*(?:\.[A-Za-z0-9_-]+)+)\}\}")

FactValueType = Literal["int", "number", "str"]
FactFormat = Literal["number", "p", "log2FC", "string"]
FactRepeat = Literal["one", "group", "row"]
FactWhen = Literal["always", "present", "rank_count"]
FactUnit = Literal["cases", "events", "p", "days", "samples", "genes", "gene", "log2FC"]


def fact_id(step_id: str, suffix: str) -> str:
    out = f"f.{step_id}.{suffix}"
    if not FACT_ID.match(out):
        raise ValueError(f"{out!r} is not a fact id")
    return out


def split_fact_id(value: str) -> tuple[str, str]:
    """(step id, suffix) of a fact id such as f.s4.events.high -> ("s4", "events.high")."""
    match = FACT_ID.match(value)
    if match is None:
        raise ValueError(f"{value!r} is not a fact id")
    return match.group(1), match.group(2)[1:]


@dataclass(frozen=True)
class FactSpec:
    """One fact a tool yields. `${...}` in suffix, pointer and label are bound per fact (TOOLS.md)."""

    suffix: str  # such as "events.${key}"
    pointer: str  # into the call, such as "/observed/groups/${index}/events"
    value_type: FactValueType
    unit: FactUnit
    label: str  # such as "${group} events"
    format: FactFormat  # the format_fact rule the unit selects
    repeat: FactRepeat = "one"  # one fact, one per group, or one per ranked row shown
    when: FactWhen = "always"  # present: only when the pointer's value is not null


_COUNT_LABEL: Final = "${cases_label}"

FACT_SPECS: Final[dict[ToolName, tuple[FactSpec, ...]]] = {
    "fixture_fingerprint": (FactSpec("cases", "/observed/document/sandbox/cases", "int", "cases", "Cases in the sandbox", "number"),),
    "cohort_count": (FactSpec("count", "/observed/count", "int", "cases", _COUNT_LABEL, "number"),),
    "cases_query": (FactSpec("count", "/observed/count", "int", "cases", _COUNT_LABEL, "number"),),
    "gene_cases": (FactSpec("count", "/observed/count", "int", "cases", "${gene}-mutated cases", "number"),),
    "top_mutated_genes": (),
    "survival_compare": (
        FactSpec("events.${key}", "/observed/groups/${index}/events", "int", "events", "${group} events", "number", "group"),
        FactSpec("p", "/observed/pValue", "number", "p", "Log-rank p", "p", when="present"),
    ),
    "sample_lists": (
        FactSpec("samples.${key}", "/observed/groups/${index}/count", "int", "samples", "${group} samples", "number", "group"),
    ),
    "de_preanalysis": (
        FactSpec(
            "eligible.${key}", "/observed/groups/${index}/eligible", "int", "samples", "${group} RNA-seq-eligible samples", "number", "group"
        ),
    ),
    "de_run": (
        FactSpec("tested", "/observed/totals/tested", "int", "genes", "Genes tested", "number"),
        FactSpec("significant", "/observed/totals/significant", "int", "genes", "Genes past the thresholds", "number"),
    ),
    "kg_query": (),
    "set_operation": (FactSpec("count", "/observed/count", "int", "cases", _COUNT_LABEL, "number"),),
    "check_subset": (FactSpec("outside", "/observed/outsideCount", "int", "cases", "${inner} outside the tested cases", "number"),),
    "rank_top_n": (),  # per source and rankBy: rank_fact_specs
    "derive_km_median": (
        FactSpec(
            "median.${key}", "/observed/groups/${index}/median", "number", "days", "${group} median overall survival", "number",
            "group", "present",
        ),
    ),
    "read_cohort_context": (),
    "save_cohorts": (),
    "set_active_cohort": (),
    "mount_viz": (),
    "open_full_tool": (),
}

_ROW: Final = "/observed/rows/${index}"
_RANK_GENE: Final = FactSpec("top.${pos}.gene", f"{_ROW}/key", "str", "gene", "Rank ${rank} gene", "string", "row")
_RANK_LOG2FC: Final = FactSpec("top.${pos}.log2fc", f"{_ROW}/row/fold_change", "number", "log2FC", "${gene} log2 fold change", "log2FC", "row")
_RANK_COUNT: Final = FactSpec("count", "/observed/count", "int", "genes", "Ranked genes", "number", when="rank_count")
RANK_FACT_SPECS: Final[dict[tuple[RankSourceTool, RankBy], tuple[FactSpec, ...]]] = {
    ("de_run", "adjusted_p_value"): (
        _RANK_GENE,
        _RANK_LOG2FC,
        FactSpec("top.${pos}.padj", f"{_ROW}/value", "number", "p", "${gene} adjusted p", "p", "row"),
        _RANK_COUNT,
    ),
    ("de_run", "original_p_value"): (
        _RANK_GENE,
        _RANK_LOG2FC,
        FactSpec("top.${pos}.p", f"{_ROW}/value", "number", "p", "${gene} p-value", "p", "row"),
        FactSpec("top.${pos}.padj", f"{_ROW}/row/adjusted_p_value", "number", "p", "${gene} adjusted p", "p", "row"),
        _RANK_COUNT,
    ),
    ("de_run", "abs(fold_change)"): (
        _RANK_GENE,
        _RANK_LOG2FC,
        FactSpec("top.${pos}.padj", f"{_ROW}/row/adjusted_p_value", "number", "p", "${gene} adjusted p", "p", "row"),
        _RANK_COUNT,
    ),
    ("top_mutated_genes", "cases"): (
        _RANK_GENE,
        FactSpec("top.${pos}.cases", f"{_ROW}/value", "int", "cases", "${gene} mutated cases", "number", "row"),
        _RANK_COUNT,
    ),
}


def fact_specs(tool: ToolName, *, source: RankSourceTool | None = None, rank_by: RankBy | None = None) -> tuple[FactSpec, ...]:
    """The facts `tool` yields; rank_top_n's depend on its source tool and rankBy."""
    if tool != "rank_top_n":
        return FACT_SPECS[tool]
    if source is None or rank_by is None:
        raise ValueError("rank_top_n's facts depend on its source tool and rankBy")
    try:
        return RANK_FACT_SPECS[(source, rank_by)]
    except KeyError:
        raise ValueError(f"rank_top_n cannot rank {source} rows by {rank_by}") from None


class PointerError(LookupError):
    """A JSON pointer that does not resolve."""


def resolve_pointer(document: JsonValue, pointer: str) -> JsonValue:
    """RFC 6901 lookup ("" is the whole document; ~1 is /, ~0 is ~)."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise PointerError(f"{pointer!r} does not start with /")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise PointerError(f"{pointer}: no key {token!r}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")) or int(token) >= len(current):
                raise PointerError(f"{pointer}: no index {token!r}")
            current = current[int(token)]
        else:
            raise PointerError(f"{pointer}: {token!r} is below a scalar")
    return current


def cohort_label(name: str) -> str:
    """A cohort's name without ORION's " (ORION)" suffix, for labels and titles."""
    return name.removesuffix(ORION_SUFFIX)


def cases_label(name: str) -> str:
    """A count fact's label: "High risk" -> "High risk cases"; "KRAS not detected (ORION)" -> "KRAS not detected cases"."""
    label = cohort_label(name)
    return label if label.casefold().endswith("cases") else f"{label} cases"


def group_keys(labels: Sequence[str]) -> list[str]:
    """Fact-id keys for survival groups: lowercase words, minus those every label shares.

    ["High risk", "Standard risk"] -> ["high", "standard"]; ["Stage I", "Stage II"] -> ["i", "ii"].
    Falls back to g1, g2, ... when a key would be empty or repeated.
    """
    words = [re.findall(r"[a-z0-9]+", label.casefold()) for label in labels]
    shared = set.intersection(*(set(w) for w in words)) if len(words) > 1 else set()
    keys = ["_".join(w for w in ws if w not in shared) for ws in words]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        return [f"g{i}" for i in range(1, len(labels) + 1)]
    return keys


@dataclass(frozen=True)
class FactContext:
    """What a fact's label needs beyond the tool's output; the extractor fills it from the plan."""

    cohort: str | None = None  # count tools: the cohort (or split value) counted
    inner: str | None = None  # check_subset: the inner step's count label, such as "KRAS-mutated cases"
    group_labels: tuple[str, ...] = ()  # sample_lists and de_preanalysis: cohort_label of control, case
    rank_source: RankSourceTool | None = None
    rank_by: RankBy | None = None
    rank_cap: int = VIEW_RANK_CAP  # ranked rows this ranking may still show (VIEW_RANK_CAP in all)


@dataclass(frozen=True)
class FactDraft:
    """A fact before the extractor adds its step, row, tool, call and hashes (models.FactRecord)."""

    id: str
    label: str
    value: int | float | str
    unit: str
    json_pointer: str


_VAR: Final = re.compile(r"\$\{([a-z_]+)\}")


def fill_text(template: str, values: Mapping[str, str]) -> str:
    """Replace each `${name}` in code text (labels, unblock lines) by values[name]."""

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"{template!r} needs ${{{name}}}")
        return values[name]

    return _VAR.sub(one, template)


def _typed(spec: FactSpec, value: JsonValue, pointer: str) -> int | float | str:
    if spec.value_type == "str":
        if isinstance(value, str):
            return value
    elif isinstance(value, bool):
        pass
    elif spec.value_type == "int" and isinstance(value, int):
        return value
    elif spec.value_type == "int" and isinstance(value, float) and value.is_integer():
        return int(value)  # rank_top_n's value is a float (base.RankedRow); a case count is written as an int
    elif spec.value_type == "number" and isinstance(value, (int, float)) and math.isfinite(value):
        return value
    raise ValueError(f"{pointer}: {value!r} is not a {spec.value_type}")


def _bindings(spec: FactSpec, observed: Mapping[str, JsonValue], ctx: FactContext) -> list[dict[str, str]]:
    if spec.repeat == "one":
        return [{}]
    if spec.repeat == "group":
        groups = observed.get("groups")
        if not isinstance(groups, list):
            raise ValueError("a per-group fact needs /observed/groups")
        if all(isinstance(g, dict) and isinstance(g.get("key"), str) for g in groups):
            keys = [str(g["key"]) for g in groups if isinstance(g, dict)]  # DE groups: control, case
            labels = list(ctx.group_labels)
            if len(labels) != len(keys):
                raise ValueError("DE group facts need the two groups' labels")
        else:
            labels = [str(g["label"]) for g in groups if isinstance(g, dict)]
            keys = group_keys(labels)
        return [{"index": str(i), "key": key, "group": label} for i, (key, label) in enumerate(zip(keys, labels))]
    rows = observed.get("rows")
    if not isinstance(rows, list):
        raise ValueError("a per-row fact needs /observed/rows")
    out: list[dict[str, str]] = []
    for i, row in enumerate(rows[: max(ctx.rank_cap, 0)]):
        if not isinstance(row, dict):
            raise ValueError("ranked rows are objects")
        out.append({"index": str(i), "pos": str(i + 1), "rank": str(row["rank"]), "gene": str(row["key"])})
    return out


def _label_values(spec: FactSpec, tool: ToolName, observed: Mapping[str, JsonValue], ctx: FactContext) -> dict[str, str]:
    values: dict[str, str] = {}
    if "${cases_label}" in spec.label:
        if ctx.cohort is None:
            raise ValueError(f"{tool}'s count label needs the cohort it counts")
        values["cases_label"] = cases_label(ctx.cohort)
    if "${inner}" in spec.label:
        if ctx.inner is None:
            raise ValueError("check_subset's label needs the inner step's count label")
        values["inner"] = ctx.inner
    if tool == "gene_cases":
        values["gene"] = str(observed.get("gene"))
    return values


def draft_facts(
    tool: ToolName, step_id: str, observed: Mapping[str, JsonValue], ctx: FactContext = FactContext()
) -> list[FactDraft]:
    """Every fact `tool`'s Observed (camelCase JSON) yields at `step_id`, in spec order, per group or row.

    Pointers are into the call (`/observed/...`). A `present` fact is skipped when its value is
    null (a median not reached, no p). rank_top_n's `count` fact is yielded only when the ranking
    has more rows than it shows or fewer than asked (the rank.cap and rank.fewer templates cite it).
    """
    document: JsonValue = {"observed": dict(observed)}
    out: list[FactDraft] = []
    specs = fact_specs(tool, source=ctx.rank_source, rank_by=ctx.rank_by)
    i = 0
    while i < len(specs):
        spec = specs[i]
        if spec.repeat == "row":
            # Consecutive per-row facts are written row by row: rank 1's gene, log2fc, padj, then rank 2's.
            end = i
            while end < len(specs) and specs[end].repeat == "row":
                end += 1
            run = specs[i:end]
            for binding in _bindings(spec, observed, ctx):
                for row_spec in run:
                    _draft(out, row_spec, tool, step_id, document, {**_label_values(row_spec, tool, observed, ctx), **binding})
            i += len(run)
            continue
        if spec.when == "rank_count":
            count, requested = observed.get("count"), observed.get("nRequested")
            if not isinstance(count, int) or not isinstance(requested, int):
                raise ValueError("rank_top_n's output carries count and nRequested")
            if count <= ctx.rank_cap and count >= requested:
                i += 1
                continue  # every row is shown and none is missing: no sentence cites the count
        label_values = _label_values(spec, tool, observed, ctx)
        for binding in _bindings(spec, observed, ctx):
            _draft(out, spec, tool, step_id, document, {**label_values, **binding})
        i += 1
    return out


def _draft(out: list[FactDraft], spec: FactSpec, tool: ToolName, step_id: str, document: JsonValue, values: Mapping[str, str]) -> None:
    pointer = fill_text(spec.pointer, values)
    raw = resolve_pointer(document, pointer)
    if raw is None and spec.when == "present":
        return
    out.append(
        FactDraft(
            id=fact_id(step_id, fill_text(spec.suffix, values)),
            label=fill_text(spec.label, values),
            value=_typed(spec, raw, pointer),
            unit=spec.unit,
            json_pointer=pointer,
        )
    )


# --- Observed, limitation and viz templates ------------------------------------------------------------

TemplatePart = tuple[Literal["text", "fact"], str]
_TOKEN: Final = re.compile(r"\{\{(?:\$\{[a-z_]+\}|[^{}])*\}\}|\$\{[a-z_]+\}")
_STEP_ROLE: Final = re.compile(r"^\{\{f\.<([a-z_]+)>\.(.+)\}\}$")
_FACT_ROLE: Final = re.compile(r"^\{\{<([a-z_]+)>\}\}$")
_LITERAL_FACT: Final = re.compile(r"^\{\{(f\.s[1-9][0-9]*(?:\.[A-Za-z0-9_-]+)+)\}\}$")


@dataclass(frozen=True)
class Template:
    """A code template: Observed sentences, limitation lines and viz notes (TOOLS.md "Templates").

    `pattern` is text with `${var}` (code text, never parsed again), `{{f.<role>.suffix}}` (a fact
    of the step bound to <role>; `${var}` inside the suffix must bind fact-id segments) and
    `{{<role>}}` (a whole fact id bound to <role>). `${items}` expands `items`, each item one of
    `item_patterns`, joined by `joiner` (`last_joiner` before the last). After binding, every fact
    reference is a fact id in interfaces.PLACEHOLDER's grammar.
    """

    id: str
    part: Literal["observed", "limitation", "viz_note", "viz_title", "viz_caption"]
    pattern: str
    when: str
    code: LimitationCode | None = None
    unblock: str | None = None
    item_patterns: Mapping[str, str] = field(default_factory=dict)
    joiner: str = "; "
    last_joiner: str | None = None


def _fact_part(token: str, steps: Mapping[str, str], values: Mapping[str, str]) -> str:
    whole = _FACT_ROLE.match(token)
    if whole is not None:
        target = steps[whole.group(1)]
    else:
        step = _STEP_ROLE.match(token)
        literal = _LITERAL_FACT.match(token)
        if step is not None:
            suffix = _VAR.sub(lambda m: values[m.group(1)], step.group(2))
            target = f"f.{steps[step.group(1)]}.{suffix}"
        elif literal is not None:
            target = literal.group(1)
        else:
            raise ValueError(f"{token!r} is not a fact reference")
    if not FACT_ID.match(target):
        raise ValueError(f"{token!r} bound to {target!r}, which is not a fact id")
    return target


def _parts(pattern: str, steps: Mapping[str, str], values: Mapping[str, str]) -> list[TemplatePart]:
    out: list[TemplatePart] = []
    position = 0
    for match in _TOKEN.finditer(pattern):
        if match.start() > position:
            out.append(("text", pattern[position : match.start()]))
        token = match.group(0)
        if token.startswith("${"):
            name = token[2:-1]
            if name not in values:
                raise KeyError(f"{pattern!r} needs ${{{name}}}")
            out.append(("text", values[name]))
        else:
            out.append(("fact", _fact_part(token, steps, values)))
        position = match.end()
    if position < len(pattern):
        out.append(("text", pattern[position:]))
    return out


def merge_text(parts: Sequence[TemplatePart]) -> list[TemplatePart]:
    """Adjacent text parts joined; empty text dropped. Text segmentation carries no meaning."""
    out: list[TemplatePart] = []
    for kind, text in parts:
        if kind == "text" and not text:
            continue
        if kind == "text" and out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + text)
        else:
            out.append((kind, text))
    return out


def template_parts(
    template: Template,
    *,
    steps: Mapping[str, str] | None = None,
    values: Mapping[str, str] | None = None,
    items: Sequence[tuple[str, Mapping[str, str]]] = (),
) -> list[TemplatePart]:
    """Bind a template into text and fact parts (merged). `steps` maps roles to step or fact ids;
    `items` lists (item pattern name, its values) for `${items}`. The renderer turns each fact
    part into a Segment whose text is format_fact(value, unit)."""
    steps, values = steps or {}, values or {}
    out: list[TemplatePart] = []
    for n, piece in enumerate(template.pattern.split("${items}")):
        if n:  # an ${items} stood before this piece
            for i, (name, item_values) in enumerate(items):
                if i:
                    is_last = i == len(items) - 1
                    out.append(("text", template.last_joiner if is_last and template.last_joiner is not None else template.joiner))
                out += _parts(template.item_patterns[name], steps, {**values, **item_values})
        out += _parts(piece, steps, values)
    return merge_text(out)


def template_text(parts: Sequence[TemplatePart], facts: Mapping[str, str]) -> str:
    """The plain line: each fact part replaced by its formatted text (facts: id -> format_fact text)."""
    return "".join(facts[text] if kind == "fact" else text for kind, text in parts)


def _t(
    id: str, part: Literal["observed", "limitation", "viz_note", "viz_title", "viz_caption"], pattern: str, when: str, *,
    code: LimitationCode | None = None, unblock: str | None = None, items: Mapping[str, str] | None = None,
    joiner: str = "; ", last_joiner: str | None = None,
) -> Template:
    return Template(id, part, pattern, when, code, unblock, items or {}, joiner, last_joiner)


_DE_ITEM_ADJ: Final = "{{f.<ranking>.top.${pos}.gene}} (log2FC {{f.<ranking>.top.${pos}.log2fc}}, adjusted p {{f.<ranking>.top.${pos}.padj}})"
_DE_ITEM_P: Final = (
    "{{f.<ranking>.top.${pos}.gene}} (log2FC {{f.<ranking>.top.${pos}.log2fc}}, p {{f.<ranking>.top.${pos}.p}}, "
    "adjusted p {{f.<ranking>.top.${pos}.padj}})"
)
_GENE_ITEM: Final = "{{f.<ranking>.top.${pos}.gene}} ({{f.<ranking>.top.${pos}.cases}})"
_TIED_ITEM: Final = "{{f.<ranking>.top.${pos}.gene}}"

TEMPLATES: Final[dict[str, Template]] = {
    t.id: t
    for t in (
        # Observed: survival (J1)
        _t("survival.group", "observed", "${group}: {{f.<count>.count}} cases, {{f.<survival>.events.${key}}} events.",
           "Per group, in group order, once survival_compare is Done"),
        _t("survival.group_count", "observed", "${group}: {{f.<count>.count}} cases.",
           "Per group when survival_compare did not finish Done (Partial, Cancelled, Interrupted, Limited)"),
        _t("survival.p", "observed", "Log-rank p = {{f.<survival>.p}} (Gen3 Analysis).", "When the p fact exists"),
        _t("survival.median", "observed",
           "Median overall survival: ${items} (Kaplan–Meier median derived by ORION from the engine's curves).",
           "Once derive_km_median is Done", items={"reached": "${group} {{f.<median>.median.${key}}} days", "not_reached": "${group} not reached"}),
        # Observed: top mutated genes (J2 and the scope form)
        _t("genes.cohort", "observed", "The cohort has {{<cohort_count>}} cases.", "Once the ranked cohort is sized"),
        _t("genes.top", "observed", "The most frequently mutated genes are ${items}.",
           "Two or more ranked rows (first item cites the cohort count)",
           items={"first": "{{f.<ranking>.top.${pos}.gene}} ({{f.<ranking>.top.${pos}.cases}} of {{<cohort_count>}} cases)", "rest": _GENE_ITEM},
           joiner=", ", last_joiner=" and "),
        _t("genes.top_one", "observed", "The most frequently mutated gene is ${items}.", "Exactly one ranked row",
           items={"first": "{{f.<ranking>.top.${pos}.gene}} ({{f.<ranking>.top.${pos}.cases}} of {{<cohort_count>}} cases)"}),
        _t("genes.none", "observed", "No gene in the six-gene panel is mutated in this cohort.", "top_mutated_genes Empty"),
        # Observed: differential expression (J3)
        _t("de.cohorts", "observed",
           "Mutation-tested cases: {{f.<tested>.count}}; ${gene}-mutated: {{f.<mutated>.count}}; ${gene} not detected: {{f.<difference>.count}}.",
           "J3 shape (C-UNTESTED groups), once the difference step is Done"),
        _t("de.untested", "observed",
           "Cases without mutation data are left out: {{f.<tested>.count}} of {{<scope_count>}} cases are mutation-tested.",
           "J3 shape, when the tested count is below the scope count (C-UNTESTED)"),
        _t("de.groups", "observed",
           "Groups: ${control} with {{f.<pre>.eligible.control}} RNA-seq-eligible samples (control), and ${case} with "
           "{{f.<pre>.eligible.case}} (case).", "Once de_preanalysis is Done; names as de_run sends them"),
        _t("de.method", "observed",
           "Method: ${method}; genes ranked by ${rank}; fold change is log2(${case} / ${control}), so a positive value is higher in ${case}.",
           "Every DE answer (V-COMP: method, ranking rule, direction)"),
        _t("de.totals", "observed", "{{f.<de>.significant}} of {{f.<de>.tested}} tested genes passed the viewer's thresholds.",
           "de_run Done with the default thresholds"),
        _t("de.totals_edited", "observed", "{{f.<de>.significant}} of {{f.<de>.tested}} tested genes passed the thresholds set on the plan.",
           "de_run Done after a thresholds edit"),
        _t("de.top", "observed", "Top-ranked genes by ${rank}: ${items}.", "rank_top_n Done with rows",
           items={"adjusted": _DE_ITEM_ADJ, "original": _DE_ITEM_P, "fold": _DE_ITEM_ADJ}),
        _t("rank.cap", "observed",
           "Only the first part of the ranking is shown; all {{f.<ranking>.count}} ranked genes are in the run record.",
           "The ranking has more rows than the view shows (VIEW_RANK_CAP in all)"),
        # Observed: cohort size and the zero result (C-40)
        _t("size.count", "observed", "${cohort}: {{<count>}} cases.", "cohort_size, once its count step is Done"),
        _t("empty.count", "observed", "${cohort}: {{<count>}} cases match ${filter}.", "A count step ended Empty: the zero, with the filter"),
        # Limitations
        _t("survival.viewer_p", "limitation", "The survival chart shows this p-value as p = 0; the value above comes from Gen3 Analysis.",
           "p < VIEWER_P_FLOOR", code="VIEWER_P_ROUNDED"),
        _t("survival.no_hazard_ratio", "limitation", "The platform reports no hazard ratio for this comparison, so the answer gives none.",
           "Every survival answer", code="STAT_UNAVAILABLE"),
        _t("survival.small_group", "limitation", "Too few cases to compare survival: ${group} has {{<count>}} cases, and each group needs at least 10.",
           "The survival pre-guard failed", code="INELIGIBLE_GROUP", unblock="Choose a split with larger groups, or a broader scope."),
        _t("tool.failed", "limitation", "The ${tool} ${failure}, so ${missing}.", "A step Failed after its retry",
           code="TOOL_FAILED", unblock="Retry step reruns the ${tool} with the same request."),
        _t("tool.failed_write", "limitation", "Saving the cohorts failed, so ${missing}.", "The write step failed", code="TOOL_FAILED",
           unblock="Ask again to save the cohorts."),
        _t("empty.cohort", "limitation",
           "No case matched ${filter}: either no case has these values, or the API returned an empty set for a field it does not index.",
           "A count step ended Empty", code="EMPTY_COHORT", unblock="Check the values on the plan, or ask about a broader cohort."),
        _t("empty.samples", "limitation", "${group} has no ProteinPaint sample, so differential expression cannot run.",
           "sample_lists found no sample for a group", code="EMPTY_COHORT", unblock="Choose groups with RNA-seq data."),
        _t("ids.too_many", "limitation",
           "${cohort} has {{<count>}} cases, more than the 10,000 case IDs one step can carry, so the steps that need its case list did not run.",
           "cases_query over 10,000 matches", code="TOO_MANY_IDS", unblock="Narrow the cohort with another filter."),
        _t("subset.outside", "limitation", "{{f.<subset>.outside}} ${inner} are outside the ${outer}; only the cases inside were used.",
           "check_subset found mutated cases outside the tested cases", code="NOT_SUBSET"),
        _t("de.ineligible", "limitation",
           "Too few RNA-seq-eligible samples for differential expression: ${control} has {{f.<pre>.eligible.control}} and ${case} has "
           "{{f.<pre>.eligible.case}}; each group needs at least 10, so nothing was saved.",
           "de_preanalysis post-guard failed", code="INELIGIBLE_GROUP", unblock="Choose larger groups, such as a broader scope."),
        _t("de.too_many_samples", "limitation",
           "The groups have {{f.<samples>.samples.control}} and {{f.<samples>.samples.case}} samples, more than the 4,000 one differential "
           "expression run takes.", "de_preanalysis or de_run pre-guard failed", code="REQUEST_TOO_LARGE",
           unblock="Narrow the groups, such as with a smaller scope."),
        _t("samples.too_large", "limitation", "The sample lookup for ${group} would exceed the 8 MiB the gateway accepts.",
           "sample_lists pre-guard failed", code="REQUEST_TOO_LARGE", unblock="Narrow the group."),
        _t("filter.not_bridge_safe", "limitation", "${field} cannot filter ProteinPaint directly, so this step did not run.",
           "filter0 named an Elasticsearch-only field", code="UNSUPPORTED_FIELD", unblock="Remove that filter, or ask about all cases."),
        _t("rank.fewer", "limitation", "Only {{f.<ranking>.count}} genes ranked, fewer than the ${n} asked for.",
           "0 < rows < top N", code="FEWER_THAN_N"),
        _t("rank.none_de", "limitation", "No gene passed the thresholds, so there is no ranking.", "DE ranking with zero rows",
           code="FEWER_THAN_N"),
        _t("rank.none_genes", "limitation", "No gene in the panel is mutated in this cohort, so there is no ranking.",
           "top_mutated_genes Empty", code="FEWER_THAN_N"),
        _t("rank.ties", "limitation", "${items} tied with the last gene in the top ${n}, so the list is longer than ${n}.",
           "TIES_AT_CUTOFF with every tied row drawn", code="TIES_AT_CUTOFF", items={"tied": _TIED_ITEM}, joiner=", ", last_joiner=" and "),
        _t("rank.ties_oncomatrix", "limitation", "${items} tied with the last gene in the top ${n}; OncoMatrix shows only the first ${n}.",
           "TIES_AT_CUTOFF on top mutated genes", code="TIES_AT_CUTOFF", items={"tied": _TIED_ITEM}, joiner=", ", last_joiner=" and "),
        _t("panel.gene", "limitation",
           "${gene} status comes from the six-gene mutation panel (KRAS, NRAS, TP53, BRAF, DIS3, MYC); other mutations are not profiled.",
           "Every plan with a gene-defined cohort", code="PANEL_COVERAGE"),
        _t("panel.ranking", "limitation", "Mutation data covers the six-gene panel only.", "Every top mutated genes answer", code="PANEL_COVERAGE"),
        _t("save.skipped", "limitation",
           "You chose Don't save, so no cohort was saved: the chart uses the unsaved groups and Open with this cohort is off.",
           "confirm dont_save, DE", code="WRITE_SKIPPED", unblock="Ask again and choose Create to save the cohorts."),
        _t("save.skipped_one", "limitation", "You chose Don't save, so the cohort was not saved and Open with this cohort is off.",
           "confirm dont_save, one cohort (J2)", code="WRITE_SKIPPED", unblock="Ask again and choose Create to save the cohort."),
        _t("kg.not_connected", "limitation", "The knowledge-graph agent is not connected yet, so no knowledge-graph result is shown.",
           "kg_query before its target is registered", code="KG_NOT_CONNECTED"),
        # Viz specs
        _t("viz.survival.title", "viz_title", "Overall survival by ${split}", "survival card"),
        _t("viz.survival.caption", "viz_caption", "The chart's log-rank table shows p = 0; the answer's p-value comes from Gen3 Analysis.",
           "With VIEWER_P_ROUNDED; else no caption"),
        _t("viz.survival.note", "viz_note", "Log-rank p = {{f.<survival>.p}} (Gen3 Analysis)", "survival frozen table note"),
        _t("viz.oncomatrix.title", "viz_title", "Top mutated genes: ${cohort}", "OncoMatrix card"),
        _t("viz.de.title", "viz_title", "Differential expression: ${case} vs. ${control}", "DE card (labels without the ORION suffix)"),
        _t("viz.de.caption", "viz_caption", "Table sorted by fold change. Sort by adjusted p-value to match the answer.", "deDirect card"),
        _t("viz.de_input.caption", "viz_caption",
           "Choose Wilcoxon and click Run to draw the chart. Table sorted by fold change; sort by adjusted p-value to match the answer.",
           "deInput card"),
        _t("viz.de.note", "viz_note", "Ranked by ${rank_short}; {{f.<de>.significant}} genes passed the viewer's thresholds.",
           "DE frozen table note, default thresholds"),
        _t("viz.de.note_edited", "viz_note", "Ranked by ${rank_short}; {{f.<de>.significant}} genes passed the thresholds set on the plan.",
           "DE frozen table note after a thresholds edit"),
    )
}

# Words the DE templates use, by plan setting.
DE_METHOD_LABELS: Final[dict[str, str]] = {"wilcoxon": "Wilcoxon rank-sum", "edgeR": "edgeR", "limma": "limma"}
DE_METHOD_SHORT: Final[dict[str, str]] = {"wilcoxon": "Wilcoxon", "edgeR": "edgeR", "limma": "limma"}
RANK_LABELS: Final[dict[RankBy, str]] = {
    "adjusted_p_value": "adjusted p-value", "original_p_value": "p-value", "abs(fold_change)": "absolute fold change", "cases": "mutated cases",
}
RANK_SHORT: Final[dict[RankBy, str]] = {
    "adjusted_p_value": "adjusted p", "original_p_value": "p", "abs(fold_change)": "absolute log2FC", "cases": "mutated cases",
}
DE_TOP_ITEM: Final[dict[RankBy, str]] = {"adjusted_p_value": "adjusted", "original_p_value": "original", "abs(fold_change)": "fold"}
# tool.failed's words per tool, and per ToolErrorCode.
TOOL_PHRASES: Final[dict[str, str]] = {
    "fixture_fingerprint": "fingerprint check", "cohort_count": "case count", "cases_query": "case list",
    "gene_cases": "mutated-case list", "top_mutated_genes": "mutated-gene ranking", "survival_compare": "survival comparison",
    "sample_lists": "sample lookup", "de_preanalysis": "eligibility check", "de_run": "differential expression run",
    "kg_query": "knowledge-graph query", "set_operation": "set operation", "check_subset": "subset check",
    "rank_top_n": "ranking", "derive_km_median": "median calculation",
}
FAILURE_PHRASES: Final[dict[str, str]] = {
    "TIMEOUT": "timed out on both attempts", "CACHE_BUSY": "stayed busy on both attempts", "HTTP_ERROR": "returned an error",
    "UPSTREAM_SHAPE": "returned a response ORION could not read", "INVALID_INPUT": "could not be sent",
    "NOT_CONNECTED": "is not connected", "GUARD_FAILED": "was stopped by a check",
    "FAILED": "failed",  # an executor function raised: its CallRecord error starts INTERNAL_ERROR
}
MISSING_PHRASES: Final[dict[str, str]] = {
    "fixture_fingerprint": "the data and service versions are not recorded",
    "cohort_count": "this group's count is missing",
    "cases_query": "the case list and the steps that need it are missing",
    "gene_cases": "the mutated cases and the steps that need them are missing",
    "top_mutated_genes": "the gene ranking and the chart are missing",
    "survival_compare": "events, the log-rank p-value and the medians are missing",
    "sample_lists": "the sample lists and the steps that need them are missing",
    "de_preanalysis": "the eligible counts are missing and nothing was saved",
    "de_run": "the gene ranking and the chart are missing",
    "kg_query": "no knowledge-graph result is shown",
    "set_operation": "the case list and the steps that need it are missing",
    "check_subset": "the subset check and the steps after it are missing",
    "rank_top_n": "the ranking is missing",
    "derive_km_median": "the medians are missing",
}

ViewerKind = Literal["survival", "oncomatrix", "deDirect", "deInput"]


@dataclass(frozen=True)
class VizTemplate:
    """The viz spec an analysis kind emits (TOOLS.md "Viz specs")."""

    launcher: ViewerKind
    app_id: AppId
    cites: str  # which steps citesSteps lists, first the one requestPinnedHash derives from
    title: str  # Template id
    caption: str | None  # Template id, or None
    columns: tuple[str, ...]
    note: str | None  # Template id, or None
    cohort_refs: str


VIZ_TEMPLATES: Final[dict[ViewerKind, VizTemplate]] = {
    "survival": VizTemplate("survival", "Correlation", "[survival_compare step]", "viz.survival.title", "viz.survival.caption",
                            ("Group", "Cases", "Events", "Median (days)"), "viz.survival.note", "[\"base\"]"),
    "oncomatrix": VizTemplate("oncomatrix", "OncoMatrix", "[top_mutated_genes step]", "viz.oncomatrix.title", None,
                              ("Gene", "Mutated cases"), None, "[the ranked cohort's ref, or \"base\"]"),
    "deDirect": VizTemplate("deDirect", "DE", "[de_run step, rank_top_n step]", "viz.de.title", "viz.de.caption",
                            ("Gene", "log2FC", "Adjusted p"), "viz.de.note", "[control ref, case ref]"),
    "deInput": VizTemplate("deInput", "DE", "[de_run step, rank_top_n step]", "viz.de.title", "viz.de_input.caption",
                           ("Gene", "log2FC", "Adjusted p"), "viz.de.note", "[control ref, case ref]"),
}


# --- The registry ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One catalog entry: what the harness validates, guards, renders and audits for a tool."""

    name: ToolName
    kind: StepKind
    version: str  # "<name>@1", recorded on every call
    input_model: type[BaseModel]  # validates the resolved request (the MCP inputSchema)
    observed_model: type[BaseModel] | None  # validates ToolResult.observed; None when nothing is recorded
    writes: bool
    retryable: bool  # one retry with an identical body on 429, 502, 503, 504 or a timeout
    guards: tuple[GuardSpec, ...]
    summary: str
    endpoints: tuple[str, ...] = ()  # "METHOD /path" through the identity gateway
    requests: tuple[RequestTemplate, ...] = ()
    gateway_target: str | None = None
    engine: str | None = None
    timeout_s: float | None = None  # per HTTP attempt
    requires_click: bool = False
    reported_model: type[BaseModel] | None = None  # client actions: what the browser reports back
    step_args_model: type[BaseModel] | None = None  # StepSpec.args before resolution, when not the Input

    @property
    def golden(self) -> str | None:
        """The golden file (under orion/golden) of the tool's main request, or None."""
        goldens = [r.golden for r in self.requests if r.golden is not None]
        return goldens[-1] if goldens else None

    @property
    def gateway_name(self) -> str | None:
        return None if self.gateway_target is None else f"{self.gateway_target}{GATEWAY_TOOL_SEPARATOR}{self.name}"

    def contract_guards(self) -> list[Guard]:
        """The guards as ToolContract.guards lists them."""
        return [g.guard for g in self.guards]


def _server(
    name: ServerToolName, input_model: type[BaseModel], observed_model: type[BaseModel], requests: tuple[RequestTemplate, ...],
    guards: tuple[GuardSpec, ...], engine: str, summary: str, *, step_args_model: type[BaseModel] | None = None,
) -> ToolSpec:
    endpoints = tuple(dict.fromkeys(r.endpoint for r in requests))
    return ToolSpec(
        name=name, kind="server", version=f"{name}@{CATALOG_VERSION}", input_model=input_model, observed_model=observed_model,
        writes=False, retryable=True, guards=guards, summary=summary, endpoints=endpoints, requests=requests,
        gateway_target=GATEWAY_TARGET, engine=engine, timeout_s=TOOL_HTTP_TIMEOUT_S, step_args_model=step_args_model,
    )


def _exec(name: ExecutorFunctionName, input_model: type[BaseModel], observed_model: type[BaseModel], guards: tuple[GuardSpec, ...], summary: str) -> ToolSpec:
    return ToolSpec(
        name=name, kind="exec", version=f"{name}@{CATALOG_VERSION}", input_model=input_model, observed_model=observed_model,
        writes=False, retryable=False, guards=guards, summary=summary,
    )


def _client(
    name: ClientActionName, input_model: type[BaseModel], observed_model: type[BaseModel] | None, reported: type[BaseModel] | None, *,
    writes: bool, click: bool, summary: str, step_args_model: type[BaseModel] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name, kind="client", version=f"{name}@{CATALOG_VERSION}", input_model=input_model, observed_model=observed_model,
        writes=writes, retryable=False, guards=(), summary=summary, requires_click=click, reported_model=reported,
        step_args_model=step_args_model,
    )


TOOL_REGISTRY: Final[dict[ToolName, ToolSpec]] = {
    spec.name: spec
    for spec in (
        _server("fixture_fingerprint", FixtureFingerprintInput, FixtureFingerprintObserved, FINGERPRINT_REQUESTS, (), ENGINE_FINGERPRINT,
                "Records the data and service versions (s1, folded)"),
        _server("cohort_count", CohortCountInput, CohortCountObserved, (COHORT_COUNT_REQUEST,), (G_NONEMPTY,), ENGINE_GUPPY,
                "Counts the cases a Gen3 filter matches"),
        _server("cases_query", CasesQueryInput, CasesQueryObserved, (CASES_QUERY_REQUEST,), (G_NONEMPTY, G_MAX_IDS), ENGINE_GEN3_ANALYSIS,
                "Lists the case IDs a Gen3 filter matches, in pages of 1,000"),
        _server("gene_cases", GeneCasesInput, GeneCasesObserved, (GENE_CASES_REQUEST,), (G_PANEL, G_NONEMPTY), ENGINE_GEN3_ANALYSIS,
                "Lists the cases with a mutation in one panel gene"),
        _server("top_mutated_genes", TopMutatedGenesInput, TopMutatedGenesObserved, (TOP_MUTATED_GENES_REQUEST,), (G_BRIDGE_SAFE, G_GENES),
                ENGINE_PROTEINPAINT, "Ranks a cohort's genes by mutated cases, as OncoMatrix does"),
        _server("survival_compare", SurvivalCompareInput, SurvivalCompareObserved, (SURVIVAL_REQUEST,), (G_SURVIVAL_GROUP, G_SURVIVAL_CURVES),
                ENGINE_GEN3_ANALYSIS, "Kaplan–Meier curves per group and the log-rank p"),
        _server("sample_lists", SampleListsInput, SampleListsObserved, (SAMPLE_LISTS_REQUEST,), (G_CASE_LISTS, G_BRIDGE_SAFE, G_BODY_SIZE, G_SAMPLES_FOUND),
                ENGINE_PROTEINPAINT, "Maps both DE groups' case IDs to ProteinPaint samples"),
        _server("de_preanalysis", DePreanalysisInput, DePreanalysisObserved, (DE_PREANALYSIS_REQUEST,), (G_DE_SAMPLES, G_ELIGIBLE),
                ENGINE_PROTEINPAINT, "Counts RNA-seq-eligible samples per group, before any save"),
        _server("de_run", DeRunInput, DeRunObserved, (DE_RUN_REQUEST,), (G_DE_SAMPLES,), ENGINE_PROTEINPAINT,
                "Runs differential expression with the viewer's own request body", step_args_model=DeRunStepArgs),
        ToolSpec(
            name="kg_query", kind="server", version=f"kg_query@{CATALOG_VERSION}", input_model=KgQueryInput, observed_model=KgQueryObserved,
            writes=False, retryable=False, guards=(G_KG,), summary="Workstream 1 knowledge-graph agent (stretch; KG_NOT_CONNECTED)",
            endpoints=(), requests=(), gateway_target=KG_GATEWAY_TARGET, engine=None, timeout_s=TOOL_HTTP_TIMEOUT_S,
        ),
        _exec("set_operation", SetOperationInput, SetOperationResult, (G_NONEMPTY,), "Difference, intersection, union or size of case-ID lists"),
        _exec("check_subset", CheckSubsetInput, SubsetResult, (G_SUBSET,), "Confirms one case-ID list lies inside another"),
        _exec("rank_top_n", RankTopNInput, RankResult, (G_ROWS, G_TIES), "Top N rows, ties at the cutoff kept and named"),
        _exec("derive_km_median", DeriveKmMedianInput, KmMedianResult, (), "Kaplan–Meier median per curve"),
        _client("read_cohort_context", ReadCohortContextArgs, None, CohortContext, writes=False, click=False,
                summary="The browser's cohort snapshot, sent with every turn"),
        _client("save_cohorts", SaveCohortsArgs, SaveCohortsObserved, ConfirmAction, writes=True, click=True,
                summary="Saves or reuses the plan's cohorts after Create; the run waits", step_args_model=SaveCohortsStepArgs),
        _client("set_active_cohort", SetActiveCohortArgs, None, SetActiveCohortResult, writes=True, click=True,
                summary="Makes a cohort active after an existence check, with undo"),
        _client("mount_viz", VizSpec, None, VizReport, writes=False, click=False, summary="Mounts a viz card from its pinned inputs"),
        _client("open_full_tool", OpenFullToolArgs, None, None, writes=False, click=True, summary="Opens the full app on the card's cohort"),
    )
}

SERVER_TOOLS: Final = frozenset(get_args(ServerToolName))
EXEC_FUNCTIONS: Final = frozenset(get_args(ExecutorFunctionName))
CLIENT_ACTIONS: Final = frozenset(get_args(ClientActionName))
# Steps a compiled plan can contain (read_cohort_context, set_active_cohort, mount_viz and
# open_full_tool run outside the step list).
STEP_TOOLS: Final = SERVER_TOOLS | EXEC_FUNCTIONS | frozenset({"save_cohorts"})


_BY_NAME: Final[dict[str, ToolSpec]] = {str(name): spec for name, spec in TOOL_REGISTRY.items()}


def tool_spec(name: str) -> ToolSpec:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"{name!r} is not in the tool catalog") from None


def input_json_schema(name: str) -> dict[str, JsonValue]:
    """The tool's MCP inputSchema: its Input model's JSON Schema with camelCase names."""
    schema: dict[str, JsonValue] = tool_spec(name).input_model.model_json_schema(by_alias=True)
    return schema


MAX_OBSERVED_BYTES: Final = MAX_TOOL_RESPONSE_BYTES
