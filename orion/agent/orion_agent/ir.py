"""The planner's typed Analysis Request (RFA rev 25, A2 "Planning: from question to signed plan").

The planner fills only what the question states; the compiler fills every default.
Structured output allows no recursion, so cohorts refer to each other by `ref` strings.
"""

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from .catalog import AppId, NamedToolName

PanelGene = Literal["KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC", "OTHER"]
FilterOp = Literal["in", "=", "!=", ">", ">=", "<", "<="]
BASE_REF: Final = "base"

Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
FieldId = Annotated[str, Field(min_length=1, max_length=120)]
Value = str | int | float


class IRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)


class BaseCohort(IRModel):
    source: Literal["all_cases", "active_cohort", "saved_cohort", "thread_cohort"]
    cohort_name: str | None = Field(default=None, max_length=200)
    ledger_ref: str | None = Field(default=None, max_length=64)


class Clause(IRModel):
    field: FieldId
    op: FilterOp
    values: list[Value] = Field(min_length=1, max_length=10_000)


class FilterDef(IRModel):
    kind: Literal["filter"]
    clauses: list[Clause] = Field(min_length=1, max_length=10)


class GeneMutatedDef(IRModel):
    kind: Literal["gene_mutated"]
    gene: PanelGene
    within: Ref


class MutationTestedDef(IRModel):
    kind: Literal["mutation_tested"]
    within: Ref


class DifferenceDef(IRModel):
    kind: Literal["difference"]
    a: Ref
    b: Ref


class IntersectionDef(IRModel):
    kind: Literal["intersection"]
    a: Ref
    b: Ref


class SavedDef(IRModel):
    kind: Literal["saved"]
    cohort_name: str = Field(min_length=1, max_length=200)


class ThreadDef(IRModel):
    kind: Literal["thread"]
    ledger_ref: str = Field(min_length=1, max_length=64)


CohortDefinition = Annotated[
    FilterDef | GeneMutatedDef | MutationTestedDef | DifferenceDef | IntersectionDef | SavedDef | ThreadDef,
    Field(discriminator="kind"),
]


class CohortDef(IRModel):
    ref: Ref
    definition: CohortDefinition = Field(validation_alias="def", serialization_alias="def")

    def references(self) -> tuple[str, ...]:
        match self.definition:
            case GeneMutatedDef(within=within) | MutationTestedDef(within=within):
                return (within,)
            case DifferenceDef(a=a, b=b) | IntersectionDef(a=a, b=b):
                return (a, b)
            case _:
                return ()


class SurvivalCompare(IRModel):
    kind: Literal["survival_compare"]
    split_term: FieldId | None = None
    split_values: list[Value] | None = Field(default=None, min_length=2, max_length=6)
    groups: list[Ref] | None = Field(default=None, min_length=2, max_length=6)


class TopMutatedGenes(IRModel):
    kind: Literal["top_mutated_genes"]
    cohort: Ref
    n: int | None = Field(default=None, ge=1, le=50)


class DifferentialExpression(IRModel):
    kind: Literal["differential_expression"]
    group1: Ref
    group2: Ref
    method: Literal["wilcoxon", "edgeR", "limma"] | None = None
    rank_by: Literal["adjusted_p", "p_value", "fold_change"] | None = None
    top_n: int | None = Field(default=None, ge=1, le=100)


class CohortSize(IRModel):
    kind: Literal["cohort_size"]
    cohort: Ref


Analysis = Annotated[
    SurvivalCompare | TopMutatedGenes | DifferentialExpression | CohortSize, Field(discriminator="kind")
]


class NamedTool(IRModel):
    tool: NamedToolName


class Plan(IRModel):
    kind: Literal["plan"]
    base_cohort: BaseCohort | None = None
    follows: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cohorts: list[CohortDef] = Field(default_factory=list, max_length=6)
    analyses: list[Analysis] = Field(min_length=1, max_length=3)
    named_tools: list[NamedTool] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _refs_resolve_in_order(self) -> "Plan":
        """Each ref is unique and names `base` or an earlier cohort, which also rules out cycles."""
        known: set[str] = {BASE_REF}
        for cohort in self.cohorts:
            if cohort.ref in known:
                raise ValueError(f"cohort ref {cohort.ref!r} is defined twice or shadows {BASE_REF!r}")
            missing = [ref for ref in cohort.references() if ref not in known]
            if missing:
                raise ValueError(f"cohort {cohort.ref!r} refers to undefined {missing}")
            known.add(cohort.ref)
        for analysis in self.analyses:
            used = analysis_refs(analysis)
            missing = [ref for ref in used if ref not in known]
            if missing:
                raise ValueError(f"analysis {analysis.kind!r} refers to undefined {missing}")
        return self


class Clarify(IRModel):
    kind: Literal["clarify"]
    slot: Literal["split_term", "cohort_field", "gene", "scope"]
    option_ids: list[FieldId] = Field(min_length=2, max_length=5)


class Unsupported(IRModel):
    kind: Literal["unsupported"]
    reason: Literal["UNSUPPORTED_ANALYSIS", "CLINICAL_ADVICE", "OUT_OF_SCOPE"]
    nearest_app_ids: list[AppId] = Field(default_factory=list, max_length=3)


PlannerOutput = Annotated[Plan | Clarify | Unsupported, Field(discriminator="kind")]
PLANNER_OUTPUT: Final[TypeAdapter[PlannerOutput]] = TypeAdapter(PlannerOutput)


def analysis_refs(analysis: SurvivalCompare | TopMutatedGenes | DifferentialExpression | CohortSize) -> tuple[str, ...]:
    match analysis:
        case SurvivalCompare(groups=groups):
            return tuple(groups or ())
        case TopMutatedGenes(cohort=cohort) | CohortSize(cohort=cohort):
            return (cohort,)
        case DifferentialExpression(group1=group1, group2=group2):
            return (group1, group2)


def parse_planner_output(data: str | bytes) -> PlannerOutput:
    return PLANNER_OUTPUT.validate_json(data)


def planner_output_json_schema() -> dict[str, JsonValue]:
    """The JSON schema for the planner's structured output, with the `def` alias on the wire."""
    return PLANNER_OUTPUT.json_schema(by_alias=True, mode="validation")
