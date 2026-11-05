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

# The reason each server tool or pure function shows on its plan row (C-FIT's capability matrix).
TOOL_REASONS: Final[dict[str, str]] = {
    "cohort_count": "Cohort Builder counts filter cohorts",
    "cases_query": "Gen3 Analysis API lists case IDs",
    "gene_cases": "Gen3 Analysis API lists case IDs",
    "survival_compare": "Correlation Plot runs survival comparisons",
    "top_mutated_genes": "OncoMatrix ranks mutated genes",
    "set_operation": "ORION computes set operations in code",
}

APP_IDS: Final = frozenset(get_args(AppId))
NAMED_TOOLS: Final = frozenset(get_args(NamedToolName))


def app_title(app_id: AppId) -> str:
    return APPS[app_id].title
