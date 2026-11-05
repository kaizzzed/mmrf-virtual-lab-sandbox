"""The ten server tools (TOOLS.md section 2): one ServerTool each, run in-process (mode L) or by the
Gateway Lambda (modes A and H) through the same `run` method.

One call of `run` is one attempt: it validates the resolved Input, checks the tool's own
pre-guards, sends its requests through the identity gateway (each given what remains of the
attempt's `ctx.timeout_s`), normalises the responses into the Observed model, applies the
post-guards and returns the ToolResult envelope with one Provenance per request. A tool never
raises for a tool failure: every failure is an `error` (or `unavailable`) result with a ToolError,
mapped as TOOLS.md "Error mapping" says. Retries and the step budget belong to the ToolClient.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, JsonValue, ValidationError

from ..models import Code, Provenance, ToolStatus, canonical_json, fingerprint_document, fingerprint_sha256, sha256_hex
from .base import (
    RETRY_HTTP_STATUSES,
    TOOL_HTTP_TIMEOUT_S,
    Guard,
    ServerToolName,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolResult,
)
from .bodies import (
    FINGERPRINT_RANKING_REQUEST,
    cases_query_body,
    cohort_count_body,
    de_preanalysis_body,
    de_run_body,
    fingerprint_ranking_body,
    gene_cases_body,
    sample_list_body,
    survival_bodies,
    top_mutated_genes_body,
)
from .credentials import CredentialError
from .goldens import GoldenStore
from .guards import (
    GuardViolation,
    body_within_limit,
    case_lists_nonempty,
    de_samples_within_limit,
    eligible_enough,
    filter0_bridge_safe,
)
from .http import HttpBodyTooLarge, HttpTimeout
from .normalize import (
    ShapeError,
    analysis_version,
    cases_page,
    eligible_counts,
    gene_case_ids,
    guppy_count,
    healthcheck,
    mutated_genes,
    sample_ids,
    sandbox_status,
    survival_curve,
    survival_donors,
    survival_p_value,
)
from .normalize import de_result as read_de_result
from .schemas import (
    CASES_QUERY_REQUEST,
    COHORT_COUNT_REQUEST,
    DE_GROUP_KEYS,
    DE_PREANALYSIS_REQUEST,
    DE_RUN_REQUEST,
    ENGINE_GEN3_ANALYSIS,
    FINGERPRINT_REQUESTS,
    GENE_CASES_REQUEST,
    MAX_OBSERVED_BYTES,
    PANEL_GENES,
    SAMPLE_LISTS_REQUEST,
    SURVIVAL_REQUEST,
    TOP_MUTATED_GENES_REQUEST,
    CasesQueryInput,
    CasesQueryObserved,
    CohortCountInput,
    CohortCountObserved,
    DePreanalysisInput,
    DePreanalysisObserved,
    DeRunInput,
    DeRunObserved,
    EligibleGroup,
    FixtureFingerprintInput,
    FixtureFingerprintObserved,
    GeneCasesInput,
    GeneCasesObserved,
    KgQueryInput,
    MutatedGene,
    RequestTemplate,
    SampleGroupObserved,
    SampleListsInput,
    SampleListsObserved,
    SurvivalCompareInput,
    SurvivalCompareObserved,
    SurvivalGroupObserved,
    ToolSpec,
    TopMutatedGenesInput,
    TopMutatedGenesObserved,
    case_id_filter,
    input_json_schema,
    tool_spec,
)
from .vlab import Method, VlabClient, VlabRequestError

LOG: Final = logging.getLogger("orion_agent.tools")
ERROR_SNIPPET_CHARS: Final = 200
InputT = TypeVar("InputT", bound=BaseModel)


class ToolFailure(Exception):
    """A failed attempt, as the ToolError the result carries (TOOLS.md "Error mapping")."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        limitation: Code | None = "TOOL_FAILED",
        status: ToolStatus = "error",
    ) -> None:
        super().__init__(message)
        self.error = ToolError(code=code, message=message[:500], http_status=http_status, retryable=retryable)
        self.limitation = limitation
        self.status: ToolStatus = status


def guard_failure(violation: GuardViolation) -> ToolFailure:
    """A pre-guard stopped the call: GUARD_FAILED, the guard's code as the limitation."""
    return ToolFailure("GUARD_FAILED", f"{violation.rule}: {violation.message}", limitation=violation.code)


def failure_result(failure: ToolFailure, provenance: Sequence[Provenance]) -> ToolResult:
    return ToolResult(
        status=failure.status, observed=None, warnings=[], limitation=failure.limitation, provenance=list(provenance), error=failure.error
    )


def cancelled_error() -> ToolError:
    return ToolError(code="CANCELLED", message="Stopped before the tool answered", retryable=False)


@dataclass
class Settled:
    """A tool's 2xx outcome before the envelope: `empty` is a zero result, never success with no rows."""

    observed: BaseModel
    status: Literal["ok", "empty"] = "ok"
    limitation: Code | None = None
    warnings: list[str] = field(default_factory=list)


def _snippet(parsed: JsonValue) -> str:
    if isinstance(parsed, dict):
        for key in ("error", "message", "status"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return ": " + value[:ERROR_SNIPPET_CHARS]
    return ""


class Requests:
    """The HTTP requests of one attempt: each gets what remains of the budget; each adds a Provenance."""

    def __init__(self, vlab: VlabClient, budget_s: float, sink: list[Provenance], monotonic: Callable[[], float]) -> None:
        self._vlab = vlab
        self._monotonic = monotonic
        self._deadline = monotonic() + budget_s
        self.provenance = sink

    def remaining(self) -> float:
        return self._deadline - self._monotonic()

    async def json(self, method: Method, path: str, payload: JsonValue) -> JsonValue:
        remaining = self.remaining()
        if remaining <= 0:
            raise ToolFailure("TIMEOUT", f"no time left for {method} {path}", retryable=True)
        started = self._monotonic()
        try:
            exchange = await self._vlab.send(method, path, payload, timeout_s=remaining)
        except asyncio.CancelledError:
            # Stop (or the step timeout) aborted the request in flight: it was still sent, so record it.
            self.provenance.append(
                Provenance(
                    method=method, path=path, request_sha256=sha256_hex(canonical_json(payload, portable=False)), http_status=None,
                    response_sha256=None, elapsed_ms=max(0, round((self._monotonic() - started) * 1000)),
                )
            )
            raise
        except VlabRequestError as error:
            self.provenance.append(error.provenance)
            if isinstance(error.cause, HttpTimeout):
                raise ToolFailure("TIMEOUT", f"{method} {path} did not answer in time", retryable=True) from error
            if isinstance(error.cause, HttpBodyTooLarge):
                raise ToolFailure("UPSTREAM_SHAPE", f"{method} {path}: the response is over the size cap") from error
            raise ToolFailure("HTTP_ERROR", f"{method} {path}: {error.cause}", retryable=True) from error
        except CredentialError as error:
            raise ToolFailure("INVALID_INPUT", f"the sandbox credential could not be read: {error}") from error
        except PermissionError as error:
            raise ToolFailure("INVALID_INPUT", str(error)) from error
        self.provenance.append(exchange.provenance)
        status = exchange.response.status
        if 200 <= status < 300:
            if not exchange.is_json:
                raise ToolFailure("UPSTREAM_SHAPE", f"{method} {path} answered {status} without a JSON body", http_status=status)
            return exchange.parsed
        detail = _snippet(exchange.parsed)
        if status == 429:
            raise ToolFailure("CACHE_BUSY", f"{method} {path} is busy (429){detail}", retryable=True, http_status=status)
        retryable = status in RETRY_HTTP_STATUSES
        raise ToolFailure("HTTP_ERROR", f"HTTP {status} from {method} {path}{detail}", retryable=retryable, http_status=status)

    async def send(self, template: RequestTemplate, payload: JsonValue) -> JsonValue:
        return await self.json(template.method, template.path, payload)


def _observed_json(observed: BaseModel) -> JsonValue:
    data: JsonValue = observed.model_dump(mode="json", by_alias=True)
    size = len(canonical_json(data, portable=False).encode("utf-8"))
    if size > MAX_OBSERVED_BYTES:
        raise ToolFailure("UPSTREAM_SHAPE", f"the tool's output is {size} bytes, over the {MAX_OBSERVED_BYTES}-byte cap")
    return data


class ServerToolBase(Generic[InputT]):
    """One server tool. Subclasses set `name` and `input_model`, and implement `_call`."""

    name: ClassVar[ServerToolName]
    input_model: ClassVar[type[BaseModel]]

    def __init__(self, vlab: VlabClient, goldens: GoldenStore, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._vlab = vlab
        self._goldens = goldens
        self._monotonic = monotonic
        self.spec: ToolSpec = tool_spec(self.name)

    # --- ToolContract / ServerTool (base.py) ---

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def input_schema(self) -> Mapping[str, JsonValue]:
        return input_json_schema(self.name)

    @property
    def output_schema(self) -> Mapping[str, JsonValue]:
        model = self.spec.observed_model
        schema: dict[str, JsonValue] = {} if model is None else model.model_json_schema(by_alias=True)
        return schema

    @property
    def guards(self) -> Sequence[Guard]:
        return self.spec.contract_guards()

    @property
    def endpoint(self) -> str:
        return ", ".join(self.spec.endpoints)

    @property
    def golden_template(self) -> JsonValue:
        mains = [t for t in self.spec.requests if t.golden is not None]
        return self._goldens.payload(mains[-1]) if mains else None

    @property
    def timeout_s(self) -> float:
        return self.spec.timeout_s or TOOL_HTTP_TIMEOUT_S

    async def invoke(self, input: JsonValue, ctx: ToolContext) -> ToolResult:
        return await self.run(input, ctx, [])

    # --- One attempt ---

    async def run(self, input: JsonValue, ctx: ToolContext, sink: list[Provenance]) -> ToolResult:
        """One attempt. `sink` receives each request's Provenance as it ends, so a caller that
        cancels the attempt still has what was sent."""
        requests = Requests(self._vlab, min(ctx.timeout_s, self.timeout_s), sink, self._monotonic)
        try:
            self._check_raw(input)
            try:
                model = cast(InputT, self.input_model.model_validate(input))
            except ValidationError as error:
                raise ToolFailure("INVALID_INPUT", f"{self.name} input: {_first_error(error)}") from error
            settled = await self._call(model, requests)
            observed = _observed_json(settled.observed)
            result = ToolResult(
                status=settled.status, observed=observed, warnings=settled.warnings, limitation=settled.limitation,
                provenance=list(sink), error=None,
            )
        except ToolFailure as failure:
            result = failure_result(failure, sink)
        except (ShapeError, ValidationError) as error:
            message = _first_error(error) if isinstance(error, ValidationError) else str(error)
            result = failure_result(ToolFailure("UPSTREAM_SHAPE", f"{self.name}: {message}"), sink)
        LOG.info(
            "tool %s call %s attempt %d: %s%s", self.name, ctx.call_id, ctx.attempt, result.status,
            f" {result.error.code}" if result.error else "",
        )
        return result

    def _check_raw(self, input: JsonValue) -> None:
        """A pre-guard that must see the input before validation (gene_cases' panel check)."""

    async def _call(self, inp: InputT, requests: Requests) -> Settled:
        raise NotImplementedError


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = "/".join(str(part) for part in first.get("loc", ())) or "input"
    return f"{where}: {first.get('msg', 'invalid')}"[:300]


# --- The tools ------------------------------------------------------------------------------------


class FixtureFingerprint(ServerToolBase[FixtureFingerprintInput]):
    name = "fixture_fingerprint"
    input_model = FixtureFingerprintInput

    async def _call(self, inp: FixtureFingerprintInput, requests: Requests) -> Settled:
        status_t, version_t, health_t, _ = FINGERPRINT_REQUESTS
        status = sandbox_status(await requests.send(status_t, None))
        version = analysis_version(await requests.send(version_t, None))
        health = healthcheck(await requests.send(health_t, None))
        genes, warnings = mutated_genes(await requests.send(FINGERPRINT_RANKING_REQUEST, fingerprint_ranking_body(self._goldens)))
        ranking: list[Mapping[str, JsonValue]] = [{"gene": g.gene, "cases": g.cases} for g in genes]
        document = fingerprint_document(status, version, health, ranking)
        observed = FixtureFingerprintObserved.model_validate({"document": document, "fingerprintSha256": fingerprint_sha256(document)})
        return Settled(observed, warnings=warnings)


class CohortCount(ServerToolBase[CohortCountInput]):
    name = "cohort_count"
    input_model = CohortCountInput

    async def _call(self, inp: CohortCountInput, requests: Requests) -> Settled:
        count = guppy_count(await requests.send(COHORT_COUNT_REQUEST, cohort_count_body(inp, self._goldens)))
        observed = CohortCountObserved(count=count)
        return Settled(observed, "empty", "EMPTY_COHORT") if count == 0 else Settled(observed)


class CasesQuery(ServerToolBase[CasesQueryInput]):
    """Pages of pageSize case IDs while offset + size <= maxIds (Guppy rejects more with a 500)."""

    name = "cases_query"
    input_model = CasesQueryInput

    async def _call(self, inp: CasesQueryInput, requests: Requests) -> Settled:
        size = min(inp.page_size, inp.max_ids)
        total, ids = cases_page(await requests.send(CASES_QUERY_REQUEST, cases_query_body(inp, self._goldens, offset=0, size=size)))
        pages = 1
        if total == 0:
            return Settled(CasesQueryObserved(count=0, case_ids=[], pages=1, complete=True), "empty", "EMPTY_COHORT")
        if total > inp.max_ids:
            observed = CasesQueryObserved(count=total, case_ids=[], pages=1, complete=False)
            return Settled(observed, limitation="TOO_MANY_IDS")
        collected = list(ids)
        offset = size
        while len(set(collected)) < total and offset < inp.max_ids:
            page_size = min(inp.page_size, inp.max_ids - offset)
            body = cases_query_body(inp, self._goldens, offset=offset, size=page_size)
            page_total, page = cases_page(await requests.send(CASES_QUERY_REQUEST, body))
            pages += 1
            if page_total != total:
                raise ShapeError(f"the case count changed while paging ({total} then {page_total})")
            if not page:
                break
            collected.extend(page)
            offset += page_size
        unique = sorted(set(collected))
        if len(unique) != total:
            raise ShapeError(f"the pages listed {len(unique)} case IDs for a count of {total}")
        return Settled(CasesQueryObserved(count=total, case_ids=unique, pages=pages, complete=True))


class GeneCases(ServerToolBase[GeneCasesInput]):
    name = "gene_cases"
    input_model = GeneCasesInput

    def _check_raw(self, input: JsonValue) -> None:
        gene = input.get("gene") if isinstance(input, dict) else None
        if isinstance(gene, str) and gene not in PANEL_GENES:
            raise guard_failure(
                GuardViolation("gene_in_panel", "OUT_OF_PANEL", f"{gene} is not in the six-gene panel ({', '.join(PANEL_GENES)})")
            )

    async def _call(self, inp: GeneCasesInput, requests: Requests) -> Settled:
        ids = gene_case_ids(await requests.send(GENE_CASES_REQUEST, gene_cases_body(inp, self._goldens)))
        observed = GeneCasesObserved(gene=inp.gene, count=len(ids), case_ids=ids)
        return Settled(observed, "empty", "EMPTY_COHORT") if not ids else Settled(observed)


class TopMutatedGenes(ServerToolBase[TopMutatedGenesInput]):
    """Asks for geneCount + 1 genes: the extra row (nextGene) only shows rank_top_n a tie at the cutoff."""

    name = "top_mutated_genes"
    input_model = TopMutatedGenesInput

    async def _call(self, inp: TopMutatedGenesInput, requests: Requests) -> Settled:
        violation = filter0_bridge_safe(inp.filter0)
        if violation is not None:
            raise guard_failure(violation)
        genes, warnings = mutated_genes(await requests.send(TOP_MUTATED_GENES_REQUEST, top_mutated_genes_body(inp, self._goldens)))
        shown: list[MutatedGene] = genes[: inp.gene_count]
        extra = genes[inp.gene_count] if len(genes) > inp.gene_count else None
        observed = TopMutatedGenesObserved(genes=shown, next_gene=extra)
        if not shown:
            return Settled(observed, "empty", "FEWER_THAN_N", warnings)
        return Settled(observed, warnings=warnings)


class SurvivalCompare(ServerToolBase[SurvivalCompareInput]):
    """One call per group (its curve, cases and events), then one with every group (the log-rank p)."""

    name = "survival_compare"
    input_model = SurvivalCompareInput

    async def _call(self, inp: SurvivalCompareInput, requests: Requests) -> Settled:
        *own, combined = survival_bodies(inp, self._goldens)
        groups: list[SurvivalGroupObserved] = []
        for group, body in zip(inp.groups, own):
            donors = survival_donors(await requests.send(SURVIVAL_REQUEST, body)) or []
            events = sum(1 for d in donors if d.get("censored") is False)
            curve = survival_curve(donors) if donors else []
            groups.append(SurvivalGroupObserved(label=group.label, cases=len(donors), events=events, curve=curve))
        if any(not g.curve for g in groups):
            # Gen3 Analysis drops a group with no survival data, so its own empty results is its zero.
            observed = SurvivalCompareObserved(groups=groups, p_value=None, engine=ENGINE_GEN3_ANALYSIS)
            return Settled(observed, "empty", "EMPTY_COHORT")
        p_value = survival_p_value(await requests.send(SURVIVAL_REQUEST, combined))
        return Settled(SurvivalCompareObserved(groups=groups, p_value=p_value, engine=ENGINE_GEN3_ANALYSIS))


class SampleLists(ServerToolBase[SampleListsInput]):
    """Each group's case IDs as filter0; the gateway bridges it into a native sample list."""

    name = "sample_lists"
    input_model = SampleListsInput

    async def _call(self, inp: SampleListsInput, requests: Requests) -> Settled:
        bodies = [sample_list_body(g, self._goldens) for g in inp.groups]
        checks: list[GuardViolation | None] = [case_lists_nonempty([(g.key, g.case_ids) for g in inp.groups])]
        for group, body in zip(inp.groups, bodies):
            checks += [filter0_bridge_safe(case_id_filter(group.case_ids)), body_within_limit(group.key, body)]
        violation = next((v for v in checks if v is not None), None)
        if violation is not None:
            raise guard_failure(violation)
        observed_groups: list[SampleGroupObserved] = []
        for group, body in zip(inp.groups, bodies):
            ids = sample_ids(await requests.send(SAMPLE_LISTS_REQUEST, body))
            observed_groups.append(SampleGroupObserved(key=group.key, count=len(ids), sample_ids=ids))
        observed = SampleListsObserved(groups=observed_groups)
        if any(g.count == 0 for g in observed_groups):
            return Settled(observed, "empty", "EMPTY_COHORT")
        return Settled(observed)


class DePreanalysis(ServerToolBase[DePreanalysisInput]):
    name = "de_preanalysis"
    input_model = DePreanalysisInput

    async def _call(self, inp: DePreanalysisInput, requests: Requests) -> Settled:
        violation = de_samples_within_limit([len(g.sample_ids) for g in inp.groups])
        if violation is not None:
            raise guard_failure(violation)
        counts = eligible_counts(await requests.send(DE_PREANALYSIS_REQUEST, de_preanalysis_body(inp, self._goldens)), DE_GROUP_KEYS)
        observed = DePreanalysisObserved(groups=[EligibleGroup(key=k, eligible=n) for k, n in zip(DE_GROUP_KEYS, counts)])
        short = eligible_enough(dict(zip(DE_GROUP_KEYS, counts)))
        return Settled(observed, limitation=short.code if short else None)


class DeRun(ServerToolBase[DeRunInput]):
    """The viewer's own DE body (golden), so the card's request hits the same ProteinPaint cache entry."""

    name = "de_run"
    input_model = DeRunInput

    async def _call(self, inp: DeRunInput, requests: Requests) -> Settled:
        violation = de_samples_within_limit([len(g.sample_ids) for g in inp.groups])
        if violation is not None:
            raise guard_failure(violation)
        method, sizes, totals, genes, cache_id = read_de_result(await requests.send(DE_RUN_REQUEST, de_run_body(inp, self._goldens)))
        if method != inp.method:
            raise ShapeError(f"ProteinPaint ran {method!r}, not the requested {inp.method!r}")
        # Past the volcano's dot cap ProteinPaint returns only the DE_MAX_DOTS genes with the smallest p: `capped`.
        observed = DeRunObserved(
            method=inp.method, sample_sizes=sizes, totals=totals, genes=genes, cache_id=cache_id,
            capped=totals.significant > len(genes),
        )
        return Settled(observed)


class KgQuery(ServerToolBase[KgQueryInput]):
    """The Workstream 1 knowledge-graph target is not registered: KG_NOT_CONNECTED, no call."""

    name = "kg_query"
    input_model = KgQueryInput

    async def _call(self, inp: KgQueryInput, requests: Requests) -> Settled:
        raise ToolFailure(
            "NOT_CONNECTED", "kg_target_registered: the knowledge-graph target is not registered",
            limitation="KG_NOT_CONNECTED", status="unavailable",
        )


ServerTool = ServerToolBase[Any]
_CLASSES: Final[tuple[type[ServerTool], ...]] = (
    FixtureFingerprint, CohortCount, CasesQuery, GeneCases, TopMutatedGenes, SurvivalCompare, SampleLists, DePreanalysis, DeRun, KgQuery,
)
TOOL_CLASSES: Final[dict[ServerToolName, type[ServerTool]]] = {cls.name: cls for cls in _CLASSES}
# The nine tools behind the Gateway target `vlab` (kg_query is the separate stretch target `kg`).
VLAB_TOOLS: Final[frozenset[ServerToolName]] = frozenset(name for name in TOOL_CLASSES if name != "kg_query")


def build_server_tools(
    vlab: VlabClient | Callable[[ServerToolName], VlabClient],
    goldens: GoldenStore,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[ServerToolName, ServerTool]:
    """Every server tool over `vlab`, or over a per-tool client (fault injection gives a faulted
    tool its own transport)."""
    tools: dict[ServerToolName, ServerTool] = {}
    for name, cls in TOOL_CLASSES.items():
        client = vlab if isinstance(vlab, VlabClient) else vlab(name)
        tools[name] = cls(client, goldens, monotonic=monotonic)
    return tools
