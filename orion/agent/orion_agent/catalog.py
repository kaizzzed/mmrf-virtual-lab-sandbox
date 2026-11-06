"""The Virtual Lab app catalog and ORION's coverage boundary (RFA rev 25, R3 and A2 C-FIT).

App ids are the 15 `appId` values in the fork's config/gen3/analysisTools.json.
"""

from dataclasses import dataclass
from typing import Final, Literal, get_args

AppId = Literal[
    "Projects",
    "CohortBuilder",
    "Repository",
    "MutationFrequency",
    "cDave",
    "CohortComparison",
    "Correlation",
    "GB",
    "ProteinPaint",
    "OncoMatrix",
    "GeneExpression",
    "DE",
    "SC",
    "Umap",
    "SetOperations",
]
Role = Literal["drives", "opens", "out_of_scope"]
NamedToolName = Literal[
    "gen3_analysis_api",
    "cohort_builder",
    "correlation_plot",
    "oncomatrix",
    "differential_expression",
    "mutation_frequency",
    "cohort_comparison",
    "clinical_data_analysis",
    "genome_browser",
    "proteinpaint",
    "gene_expression_clustering",
    "single_cell_rnaseq",
    "bulk_rnaseq_umap",
    "set_operations",
    "repository",
    "projects",
]
AnalysisKind = Literal["survival_compare", "top_mutated_genes", "differential_expression", "cohort_size"]


@dataclass(frozen=True)
class App:
    app_id: AppId
    title: str
    role: Role
    reason: str


APPS: Final[dict[AppId, App]] = {
    app.app_id: app
    for app in (
        App("CohortBuilder", "Cohort Builder", "drives", "Builds and counts filter cohorts, then saves them after a click"),
        App("Correlation", "Correlation Plot", "drives", "Runs the survival overlay"),
        App("OncoMatrix", "OncoMatrix", "drives", "Displays top mutated genes"),
        App("DE", "Differential Gene Expression", "drives", "Runs differential expression and its viewer"),
        App("MutationFrequency", "Mutation Frequency", "opens", "Its gene ranking overlaps OncoMatrix"),
        App("cDave", "Clinical Data Analysis", "opens", "Clinical-variable statistics have no analysis kind"),
        App("CohortComparison", "Cohort Comparison", "opens", "Its survival view overlaps Correlation Plot"),
        App("GB", "Genome Browser", "opens", "Region views have no analysis kind"),
        App("ProteinPaint", "ProteinPaint", "opens", "Protein-domain mutation views have no analysis kind"),
        App("GeneExpression", "Gene Expression Clustering", "opens", "Clustering has no analysis kind"),
        App("SC", "Single Cell RNA-seq", "opens", "Cell-level views have no analysis kind"),
        App("Umap", "Bulk RNA-seq UMAP", "opens", "Embedding views have no analysis kind"),
        App("SetOperations", "Set Operations", "opens", "ORION computes set differences in code"),
        App("Repository", "Repository", "opens", "File browsing and downloads stay with the researcher"),
        App("Projects", "Projects", "out_of_scope", "It lists projects and reads no cohort"),
    )
}

# gen3_analysis_api is the API behind the server tools, not a catalog app.
NAMED_TOOL_APPS: Final[dict[NamedToolName, AppId | None]] = {
    "gen3_analysis_api": None,
    "cohort_builder": "CohortBuilder",
    "correlation_plot": "Correlation",
    "oncomatrix": "OncoMatrix",
    "differential_expression": "DE",
    "mutation_frequency": "MutationFrequency",
    "cohort_comparison": "CohortComparison",
    "clinical_data_analysis": "cDave",
    "genome_browser": "GB",
    "proteinpaint": "ProteinPaint",
    "gene_expression_clustering": "GeneExpression",
    "single_cell_rnaseq": "SC",
    "bulk_rnaseq_umap": "Umap",
    "set_operations": "SetOperations",
    "repository": "Repository",
    "projects": "Projects",
}

ANALYSIS_APPS: Final[dict[AnalysisKind, AppId]] = {
    "survival_compare": "Correlation",
    "top_mutated_genes": "OncoMatrix",
    "differential_expression": "DE",
    "cohort_size": "CohortBuilder",
}

# The reason each row tool shows on its plan row (C-FIT's capability matrix; TOOLS.md 6.3).
TOOL_REASONS: Final[dict[str, str]] = {
    "cohort_count": "Cohort Builder counts filter cohorts",
    "cases_query": "Gen3 Analysis API lists case IDs",
    "gene_cases": "Gen3 Analysis API lists case IDs",
    "survival_compare": "Correlation Plot runs survival comparisons",
    "top_mutated_genes": "OncoMatrix ranks mutated genes",
    "set_operation": "ORION computes set operations in code",
    "de_preanalysis": "Differential Expression checks eligible samples before any save",
    "save_cohorts": "Cohort Builder saves cohorts after your click",
    "de_run": "Differential Expression runs the comparison in its own viewer",
}

FitOutcome = Literal["fits", "hand_off", "substitution"]


@dataclass(frozen=True)
class Capability:
    """One row of C-FIT's capability matrix (RFA rev 25, A2): what a named tool can do in a plan.

    `fits` are the analysis kinds the tool runs itself; `steps` the step tools it covers (Cohort
    Builder counts and saves, the Gen3 Analysis API lists case IDs, counts and survival); `hand_off`
    the kinds that run in another app while the named one opens on the run's cohort. A tool named
    for none of the plan's jobs is a substitution, and the plan waits for consent (FR-02).
    """

    tool: NamedToolName
    app_id: AppId | None
    title: str
    does: str  # what the tool does, after its name: "OncoMatrix ranks mutated genes"
    fits: frozenset[AnalysisKind] = frozenset()
    steps: frozenset[str] = frozenset()
    hand_off: frozenset[AnalysisKind] = frozenset()


def _cap(
    tool: NamedToolName, app_id: AppId | None, title: str, does: str, *, fits: tuple[AnalysisKind, ...] = (),
    steps: tuple[str, ...] = (), hand_off: tuple[AnalysisKind, ...] = (),
) -> Capability:
    return Capability(tool, app_id, title, does, frozenset(fits), frozenset(steps), frozenset(hand_off))


CAPABILITIES: Final[dict[NamedToolName, Capability]] = {
    cap.tool: cap
    for cap in (
        _cap("correlation_plot", "Correlation", "Correlation Plot", "runs survival comparisons", fits=("survival_compare",)),
        _cap("oncomatrix", "OncoMatrix", "OncoMatrix", "ranks mutated genes", fits=("top_mutated_genes",)),
        _cap("cohort_builder", "CohortBuilder", "Cohort Builder", "builds, counts and saves cohorts", fits=("cohort_size",),
             steps=("cohort_count", "cases_query", "save_cohorts")),
        _cap("gen3_analysis_api", None, "Gen3 Analysis API", "lists case IDs, counts cases and computes survival statistics",
             fits=("cohort_size", "survival_compare"), steps=("cases_query", "gene_cases", "cohort_count", "survival_compare")),
        _cap("differential_expression", "DE", "Differential Gene Expression", "runs differential expression between two groups",
             fits=("differential_expression",)),
        _cap("mutation_frequency", "MutationFrequency", "Mutation Frequency", "ranks mutated genes in its own tables",
             hand_off=("top_mutated_genes",)),
        _cap("cohort_comparison", "CohortComparison", "Cohort Comparison", "compares cohorts' survival and facets",
             hand_off=("survival_compare",)),
        _cap("clinical_data_analysis", "cDave", "Clinical Data Analysis", "summarizes clinical variables"),
        _cap("genome_browser", "GB", "Genome Browser", "shows genomic regions"),
        _cap("proteinpaint", "ProteinPaint", "ProteinPaint", "shows protein-domain mutation views"),
        _cap("gene_expression_clustering", "GeneExpression", "Gene Expression Clustering", "clusters gene expression"),
        _cap("single_cell_rnaseq", "SC", "Single Cell RNA-seq", "shows cell-level expression"),
        _cap("bulk_rnaseq_umap", "Umap", "Bulk RNA-seq UMAP", "shows expression embeddings"),
        _cap("set_operations", "SetOperations", "Set Operations", "compares saved cohorts in a Venn view"),
        _cap("repository", "Repository", "Repository", "browses and downloads files"),
        _cap("projects", "Projects", "Projects", "lists the projects"),
    )
}


@dataclass(frozen=True)
class AnalysisAsk:
    """How the plan card and the consent card name an analysis kind (code templates)."""

    verb: str  # after "cannot": "plot survival"
    proposed: str  # after the proposed app's title: "runs the Kaplan–Meier survival comparison you asked for"


ANALYSIS_ASKS: Final[dict[AnalysisKind, AnalysisAsk]] = {
    "survival_compare": AnalysisAsk("plot survival", "runs the Kaplan–Meier survival comparison you asked for"),
    "top_mutated_genes": AnalysisAsk("rank mutated genes", "ranks the mutated genes you asked for"),
    "differential_expression": AnalysisAsk("run differential expression", "runs the differential expression you asked for"),
    "cohort_size": AnalysisAsk("count a cohort", "counts the cohort you asked about"),
}

UnsupportedReason = Literal["UNSUPPORTED_ANALYSIS", "CLINICAL_ADVICE", "OUT_OF_SCOPE"]
# The Unsupported card's text (C-37: the planner returns a code, code templates write the words).
UNSUPPORTED_TEXTS: Final[dict[UnsupportedReason, str]] = {
    "UNSUPPORTED_ANALYSIS": "ORION cannot run this analysis. It compares survival, ranks mutated genes, runs differential expression and counts cohorts.",
    "CLINICAL_ADVICE": "ORION gives no clinical or treatment advice.",
    "OUT_OF_SCOPE": "This question is outside what ORION covers in the Virtual Lab.",
}

APP_IDS: Final = frozenset(get_args(AppId))
NAMED_TOOLS: Final = frozenset(get_args(NamedToolName))


def app_title(app_id: AppId) -> str:
    return APPS[app_id].title
