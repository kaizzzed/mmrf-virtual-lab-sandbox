"""Upstream request bodies, built from each tool's request template (TOOLS.md section 2).

A body starts as the template's golden payload (or its inline body) and changes only at the
template's substitution pointers; every other value, the fixed pointers included, is sent as
captured. `fill` refuses a pointer the template does not declare, so a builder cannot drift from
the catalog, and the contract tests diff each built body against its golden.
"""

import copy
from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from ..models import Gen3Filter
from .goldens import GoldenStore
from .schemas import (
    ALL_CASES_FILTER,
    CASES_QUERY_REQUEST,
    COHORT_COUNT_REQUEST,
    DE_PREANALYSIS_REQUEST,
    DE_RUN_REQUEST,
    EMPTY_TVSLST,
    FINGERPRINT_REQUESTS,
    GENE_CASES_REQUEST,
    PANEL_GENE_IDS,
    SAMPLE_LISTS_REQUEST,
    SURVIVAL_REQUEST,
    TOP_MUTATED_GENES_REQUEST,
    CaseIdGroup,
    CasesQueryInput,
    CohortCountInput,
    DePreanalysisInput,
    DeRunInput,
    GeneCasesInput,
    RequestTemplate,
    SampleIdGroup,
    SurvivalCompareInput,
    TopMutatedGenesInput,
    case_id_filter,
    minus_log10,
)

FINGERPRINT_RANKING_REQUEST = FINGERPRINT_REQUESTS[3]


class BodyError(ValueError):
    """A substitution the template does not declare, or a pointer the payload does not have."""


def _tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise BodyError(f"{pointer!r} is not a JSON pointer")
    return [raw.replace("~1", "/").replace("~0", "~") for raw in pointer[1:].split("/")]


def set_pointer(document: JsonValue, pointer: str, value: JsonValue) -> None:
    """Replace the value at an existing pointer (RFC 6901). The parent must exist; so must the key."""
    tokens = _tokens(pointer)
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise BodyError(f"{pointer} does not resolve in the template payload")
    last = tokens[-1]
    if isinstance(current, dict) and last in current:
        current[last] = value
    elif isinstance(current, list) and last.isdigit() and int(last) < len(current):
        current[int(last)] = value
    else:
        raise BodyError(f"{pointer} does not resolve in the template payload")


def fill(template: RequestTemplate, goldens: GoldenStore, values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The template's payload with `values` set at their pointers, each a declared substitution."""
    declared = {s.pointer for s in template.substitutions}
    unknown = sorted(set(values) - declared)
    if unknown:
        raise BodyError(f"{template.endpoint} does not substitute {', '.join(unknown)}")
    payload = goldens.payload(template)
    if not isinstance(payload, dict):
        raise BodyError(f"{template.endpoint}'s template payload is not an object")
    for pointer, value in values.items():
        set_pointer(payload, pointer, copy.deepcopy(value))
    return payload


def _filter(value: Gen3Filter) -> JsonValue:
    out: JsonValue = copy.deepcopy(dict(value))
    return out


def cohort_count_body(inp: CohortCountInput, goldens: GoldenStore) -> dict[str, JsonValue]:
    return fill(COHORT_COUNT_REQUEST, goldens, {"/variables/filter": _filter(inp.filter)})


def cases_query_body(inp: CasesQueryInput, goldens: GoldenStore, *, offset: int, size: int) -> dict[str, JsonValue]:
    """One page: `size` is pageSize, shortened on the last page so offset + size never passes maxIds."""
    if offset < 0 or size < 1 or offset + size > inp.max_ids:
        raise BodyError("a page must satisfy 0 <= offset and offset + size <= maxIds")
    return fill(CASES_QUERY_REQUEST, goldens, {"/filter": _filter(inp.filter), "/size": size, "/offset": offset})


def gene_cases_body(inp: GeneCasesInput, goldens: GoldenStore) -> dict[str, JsonValue]:
    return fill(
        GENE_CASES_REQUEST, goldens,
        {"/cohort_filter": _filter(inp.cohort_filter), "/gene_filter/and/0/in/gene_id/0": PANEL_GENE_IDS[inp.gene]},
    )


def top_mutated_genes_body(inp: TopMutatedGenesInput, goldens: GoldenStore) -> dict[str, JsonValue]:
    filter0: JsonValue = _filter(inp.filter0) if inp.filter0 is not None else copy.deepcopy(ALL_CASES_FILTER)
    return fill(TOP_MUTATED_GENES_REQUEST, goldens, {"/maxGenes": inp.gene_count + 1, "/filter0": filter0})


def fingerprint_ranking_body(goldens: GoldenStore) -> dict[str, JsonValue]:
    """The all-case ranking: the golden topMutatedGenes query, unchanged."""
    return fill(FINGERPRINT_RANKING_REQUEST, goldens, {})


def survival_bodies(inp: SurvivalCompareInput, goldens: GoldenStore) -> list[dict[str, JsonValue]]:
    """One body per group (its own curve), then one with every group (the log-rank p)."""
    filters: list[JsonValue] = [_filter(group.filter) for group in inp.groups]
    bodies = [fill(SURVIVAL_REQUEST, goldens, {"/filters": [f]}) for f in filters]
    bodies.append(fill(SURVIVAL_REQUEST, goldens, {"/filters": filters}))
    return bodies


def sample_list_body(group: CaseIdGroup, goldens: GoldenStore) -> dict[str, JsonValue]:
    return fill(
        SAMPLE_LISTS_REQUEST, goldens,
        {"/filter": copy.deepcopy(EMPTY_TVSLST), "/filter0": case_id_filter(group.case_ids)},
    )


def _samplelst_values(sample_ids: Sequence[int]) -> JsonValue:
    values: list[JsonValue] = [{"sampleId": sample_id} for sample_id in sample_ids]
    return values


def de_preanalysis_body(inp: DePreanalysisInput, goldens: GoldenStore) -> dict[str, JsonValue]:
    control, case = inp.groups
    return fill(DE_PREANALYSIS_REQUEST, goldens, _group_values(control, case))


def _group_values(control: SampleIdGroup, case: SampleIdGroup) -> dict[str, JsonValue]:
    return {
        "/samplelst/groups/0/name": control.key,
        "/samplelst/groups/0/values": _samplelst_values(control.sample_ids),
        "/samplelst/groups/1/name": case.key,
        "/samplelst/groups/1/values": _samplelst_values(case.sample_ids),
    }


def de_run_body(inp: DeRunInput, goldens: GoldenStore) -> dict[str, JsonValue]:
    """The viewer's DE body: names, sample ids, method, filter0 null and, when set, the thresholds."""
    control, case = inp.groups
    values: dict[str, JsonValue] = {
        "/method": inp.method,
        "/samplelst/groups/0/name": control.name,
        "/samplelst/groups/0/values": _samplelst_values(control.sample_ids),
        "/samplelst/groups/1/name": case.name,
        "/samplelst/groups/1/values": _samplelst_values(case.sample_ids),
        "/filter0": None,
    }
    if inp.thresholds is not None:
        values["/volcanoRender/significanceThresholds/pValueCutoff"] = minus_log10(inp.thresholds.p_value_cutoff)
        values["/volcanoRender/significanceThresholds/pValueType"] = inp.thresholds.p_value_type
        values["/volcanoRender/significanceThresholds/foldChangeCutoff"] = inp.thresholds.fold_change_cutoff
    return fill(DE_RUN_REQUEST, goldens, values)
