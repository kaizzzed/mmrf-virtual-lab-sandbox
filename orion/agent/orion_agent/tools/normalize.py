"""Upstream response bodies to each tool's Observed (TOOLS.md section 2). Pure functions.

Each reader checks the shape it relies on and raises ShapeError otherwise, which the tool reports
as UPSTREAM_SHAPE: a 2xx body that is not what the tool expects is never read as a zero.
"""

import re
from collections.abc import Sequence
from typing import Final

from pydantic import JsonValue

from .base import KmPoint
from .schemas import DeGene, DeSampleSizes, DeTotals, MutatedGene

SNV_INDEL_DT: Final = 1  # ProteinPaint's dt for SNV/indel, the only class the sandbox has
_CASE_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class ShapeError(ValueError):
    """A 2xx response body that does not have the shape the tool reads."""


def _object(value: JsonValue, what: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ShapeError(f"{what} is not an object")
    return value


def _list(value: JsonValue, what: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ShapeError(f"{what} is not a list")
    return value


def _int(value: JsonValue, what: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShapeError(f"{what} is not an integer >= {minimum}")
    return value


def _number(value: JsonValue, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ShapeError(f"{what} is not a number")
    return float(value)


def _str(value: JsonValue, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ShapeError(f"{what} is not a non-empty string")
    return value


def _at(value: JsonValue, path: Sequence[str], what: str) -> JsonValue:
    current = value
    for key in path:
        current = _object(current, what).get(key)
    return current


def case_ids(values: JsonValue, what: str) -> list[str]:
    """Case IDs deduplicated and sorted by code point."""
    out: set[str] = set()
    for item in _list(values, what):
        if not isinstance(item, str) or not _CASE_ID.match(item):
            raise ShapeError(f"{what} holds a value that is not a case ID")
        out.add(item)
    return sorted(out)


# --- Gen3 and Guppy -------------------------------------------------------------------------------


def guppy_count(body: JsonValue) -> int:
    """POST /guppy/graphql: data.CaseCentric__aggregation.case_centric._totalCount. A GraphQL
    `errors` array (such as "Field path not found") at HTTP 200 is a shape error, never a zero."""
    document = _object(body, "the GraphQL response")
    errors = document.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else None
        raise ShapeError(f"GraphQL error: {str(message)[:200] if message is not None else 'unknown'}")
    return _int(_at(document, ("data", "CaseCentric__aggregation", "case_centric", "_totalCount"), "data"), "_totalCount")


def cases_page(body: JsonValue) -> tuple[int, list[str]]:
    """POST /analysis/v0/cases/: (pagination.total, data[].case_id in returned order)."""
    document = _object(body, "the cases response")
    total = _int(_at(document, ("pagination", "total"), "pagination"), "pagination.total")
    ids: list[str] = []
    for row in _list(document.get("data"), "data"):
        value = _object(row, "a data row").get("case_id")
        if not isinstance(value, str) or not _CASE_ID.match(value):
            raise ShapeError("a data row has no case_id")
        ids.append(value)
    return total, ids


def gene_case_ids(body: JsonValue) -> list[str]:
    """POST /analysis/v0/genomic/gene_cases: the returned case-ID array."""
    return case_ids(body, "the gene_cases response")


# --- ProteinPaint ---------------------------------------------------------------------------------


def mutated_genes(body: JsonValue) -> tuple[list[MutatedGene], list[str]]:
    """topMutatedGenes .genes in returned order, `cases` from the dt 1 (SNV/indel) mutationStat.

    Any other mutation class is dropped with a warning (the sandbox has none); a gene with no
    dt 1 entry keeps its place with 0 cases, so the order stays the viewer's.
    """
    genes: list[MutatedGene] = []
    warnings: list[str] = []
    for row in _list(_object(body, "the topMutatedGenes response").get("genes"), "genes"):
        gene = _object(row, "a gene row")
        symbol = _str(gene.get("gene"), "genes[].gene")
        cases = 0
        for stat in _list(gene.get("mutationStat"), f"{symbol} mutationStat"):
            entry = _object(stat, f"{symbol} mutationStat entry")
            dt = _int(entry.get("dt"), f"{symbol} mutationStat dt")
            count = _int(entry.get("count"), f"{symbol} mutationStat count")
            if dt == SNV_INDEL_DT:
                cases = count
            else:
                warnings.append(f"{symbol}: mutation class dt {dt} ({count} cases) is not counted; only SNV/indel (dt 1) is")
        genes.append(MutatedGene(gene=symbol, cases=cases))
    return genes, warnings


def sample_ids(body: JsonValue) -> list[int]:
    """POST /protein-paint/termdb getsamplelist: [].id, deduplicated and ascending."""
    out: set[int] = set()
    for row in _list(body, "the sample list"):
        out.add(_int(_object(row, "a sample").get("id"), "a sample id", minimum=1))
    return sorted(out)


def eligible_counts(body: JsonValue, names: Sequence[str]) -> list[int]:
    """POST /protein-paint/termdb/DE with preAnalysis: data[<group name>] per group, in order."""
    data = _object(_object(body, "the pre-analysis response").get("data"), "data")
    return [_int(data.get(name), f"data[{name!r}]") for name in names]


def de_result(body: JsonValue) -> tuple[str, DeSampleSizes, DeTotals, list[DeGene], str | None]:
    """POST /protein-paint/termdb/DE: method, sample sizes, totals, dots (returned order) and cacheId.
    volcanoPng, plotExtent and each dot's index, pixel_x and pixel_y are dropped."""
    document = _object(body, "the DE response")
    data = _object(document.get("data"), "data")
    method = _str(document.get("method"), "method")
    sizes = DeSampleSizes(
        control=_int(document.get("sample_size1"), "sample_size1"), case=_int(document.get("sample_size2"), "sample_size2")
    )
    totals = DeTotals(
        tested=_int(data.get("totalRows"), "data.totalRows"),
        significant=_int(data.get("totalSignificantRows"), "data.totalSignificantRows"),
        up=_int(data.get("totalSignificantUp"), "data.totalSignificantUp"),
        down=_int(data.get("totalSignificantDown"), "data.totalSignificantDown"),
    )
    genes: list[DeGene] = []
    for raw in _list(data.get("dots"), "data.dots"):
        dot = _object(raw, "a dot")
        genes.append(
            DeGene(
                gene_name=_str(dot.get("gene_name"), "gene_name"),
                fold_change=_number(dot.get("fold_change"), "fold_change"),
                original_p_value=_number(dot.get("original_p_value"), "original_p_value"),
                adjusted_p_value=_number(dot.get("adjusted_p_value"), "adjusted_p_value"),
            )
        )
    cache_id = data.get("cacheId")
    if cache_id is not None and not isinstance(cache_id, str):
        raise ShapeError("data.cacheId is not a string")
    return method, sizes, totals, genes, cache_id


# --- Gen3 Analysis survival -----------------------------------------------------------------------


def survival_donors(body: JsonValue) -> list[dict[str, JsonValue]] | None:
    """POST /analysis/v0/survival/ with one filter: results[0].donors, or None when the engine
    dropped the group (no case with survival data: `results` is empty)."""
    results = _list(_object(body, "the survival response").get("results"), "results")
    if not results:
        return None
    donors = _list(_object(results[0], "results[0]").get("donors"), "results[0].donors")
    out: list[dict[str, JsonValue]] = []
    for raw in donors:
        donor = _object(raw, "a donor")
        _number(donor.get("time"), "donor time")
        _number(donor.get("survivalEstimate"), "donor survivalEstimate")
        if not isinstance(donor.get("censored"), bool):
            raise ShapeError("donor censored is not a boolean")
        out.append(donor)
    return out


def _estimate(donor: dict[str, JsonValue]) -> float:
    return _number(donor.get("survivalEstimate"), "survivalEstimate")


def survival_curve(donors: Sequence[dict[str, JsonValue]]) -> list[KmPoint]:
    """The engine's curve (TOOLS.md 2.6): (0, 1.0), then for each distinct donor time t ascending
    the engine's estimate just after t. Gen3 Analysis gives a censored donor the estimate after
    its time and an event donor the estimate before its event, so the value just after t is:
    a censored donor's estimate at t; else an event donor's at the next time; else a censored
    donor's at the next time (no event there, so no drop); else 0 (the last time, all events)."""
    by_time: dict[float, list[dict[str, JsonValue]]] = {}
    for donor in donors:
        by_time.setdefault(_number(donor.get("time"), "donor time"), []).append(donor)
    times = sorted(by_time)
    points = [KmPoint(time=0.0, survival=1.0)]
    for i, t in enumerate(times):
        here = by_time[t]
        censored = [d for d in here if d.get("censored") is True]
        if censored:
            value = _estimate(censored[0])
        elif i + 1 < len(times):
            following = by_time[times[i + 1]]
            events = [d for d in following if d.get("censored") is False]
            value = _estimate(events[0]) if events else _estimate(following[0])
        else:
            value = 0.0
        if t < 0 or not 0.0 <= value <= 1.0:
            raise ShapeError("a survival point is outside time >= 0 and 0 <= survival <= 1")
        points.append(KmPoint(time=t, survival=value))
    return points


def survival_p_value(body: JsonValue) -> float | None:
    """The combined call's overallStats.pValue (log-rank); None when the engine reports none."""
    stats = _object(_object(body, "the survival response").get("overallStats"), "overallStats")
    value = stats.get("pValue")
    if value is None:
        return None
    p = _number(value, "overallStats.pValue")
    if not 0.0 <= p <= 1.0:
        raise ShapeError("overallStats.pValue is outside [0, 1]")
    return p


# --- Fixture fingerprint --------------------------------------------------------------------------


def sandbox_status(body: JsonValue) -> dict[str, JsonValue]:
    document = _object(body, "GET /sandbox/status")
    _str(document.get("status"), "status")
    if not isinstance(document.get("synthetic"), bool):
        raise ShapeError("synthetic is not a boolean")
    _int(document.get("cases"), "cases")
    return document


def analysis_version(body: JsonValue) -> dict[str, JsonValue]:
    document = _object(body, "GET /analysis/v0/_version")
    _str(document.get("version"), "version")
    return document


def healthcheck(body: JsonValue) -> dict[str, JsonValue]:
    document = _object(body, "GET /protein-paint/healthcheck")
    info = _object(document.get("versionInfo"), "versionInfo")
    _str(info.get("pkgver"), "versionInfo.pkgver")
    _str(info.get("codedate"), "versionInfo.codedate")
    return document
