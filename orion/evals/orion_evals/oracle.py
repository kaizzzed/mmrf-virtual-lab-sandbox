"""The independent oracle (RFA "Observability and evaluation"): raw HTTP written apart from the
tool code, computing expected values live, never from hard-coded numbers.

It reads the local stack through the identity gateway with its own requests and does its own
arithmetic; it imports nothing from `orion_agent` and reads no golden body. Where it differs from
the tools on purpose:

- **J1.** Group sizes, events and survival times come from per-case clinical fields
  (`/analysis/v0/cases/`: vital status, days to death, days to last follow-up), not from Guppy or
  the survival engine; the log-rank p is the oracle's own Mantel-Cox test (`stats.logrank`). The
  engine's p is fetched only as corroboration.
- **J2.** The cohort's case IDs come from `/analysis/v0/cases/`; each panel gene's mutated cases
  from `gene_cases` over all cases, intersected with the cohort in code; the ranking, ties and the
  denominator are the oracle's. ProteinPaint's `topMutatedGenes` is fetched only as corroboration.
- **J3.** Mutation-tested and gene-mutated case sets, their difference and the subset check are
  computed in code; the KRAS dictionary term's sample counts corroborate the group sizes. Sample
  ids, RNA-seq-eligible counts and the DE table come from ProteinPaint (there is no second engine),
  sent as the oracle's own requests with its own group names (so ProteinPaint's DE cache entry is
  not the agent's); thresholds, ranking and ties are applied by the oracle.

Credentials: none locally (the identity gateway issues its own synthetic session). For a hosted
gateway the oracle reads the repo's access file (`generated/aws/access.json`, `{origin, username,
password}`, as `scripts/sandbox_connection.py` does) at run time and never prints it.
"""

import base64
import http.client
import json
import math
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from .canonical import canonical_json, fingerprint_document, fingerprint_sha256, sha256_hex
from .stats import Observation, km_median, logrank

DEFAULT_ORIGIN: Final = "http://identity:8080"
PAGE_SIZE: Final = 1000
MAX_WINDOW: Final = 10_000  # Guppy rejects offset + size > 10,000
PANEL_GENE_IDS: Final[Mapping[str, str]] = {
    "KRAS": "ENSG00000133703",
    "NRAS": "ENSG00000213281",
    "TP53": "ENSG00000141510",
    "BRAF": "ENSG00000157764",
    "DIS3": "ENSG00000083520",
    "MYC": "ENSG00000136997",
}
CGS_FIELD: Final = "cgs_risk_key_criteria.cgs_risk_category"
RACE_FIELD: Final = "demographic.race"
SURVIVAL_FIELDS: Final = ["case_id", "demographic.vital_status", "demographic.days_to_death", "diagnoses.days_to_last_follow_up"]
GENOME: Final = "hg38"
DSLABEL: Final = "MMRF"
EMPTY_TVSLST: Final[Mapping[str, Any]] = {"type": "tvslst", "in": True, "join": "", "lst": []}
DE_DEFAULT_THRESHOLDS: Final[Mapping[str, Any]] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}
ELIGIBLE_MIN: Final = 10

Clause = Mapping[str, Any]


class OracleError(RuntimeError):
    """The stack answered something the oracle cannot use."""


# --- HTTP ---------------------------------------------------------------------------------


@dataclass
class RequestLog:
    method: str
    path: str
    status: int
    elapsed_ms: int
    bytes: int
    request_sha256: str | None


def _access_header(access_file: Path, origin: str) -> str:
    access = json.loads(access_file.read_text(encoding="utf-8"))
    if str(access.get("origin", "")).rstrip("/") != origin.rstrip("/"):
        raise OracleError("the access file belongs to another origin")
    username, password = access.get("username"), access.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or ":" in username:
        raise OracleError("the access file has no usable username and password")
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class Gateway:
    """JSON over the identity gateway; logs method, path, status, size and the body hash, never headers."""

    def __init__(self, origin: str = DEFAULT_ORIGIN, access_file: Path | None = None, timeout_s: float = 180.0) -> None:
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("the oracle needs an http(s) origin without credentials in it")
        if parts.path not in ("", "/"):
            raise ValueError("the origin has no path")
        self.origin = origin.rstrip("/")
        self._parts = parts
        self._timeout = timeout_s
        self._auth = _access_header(access_file, self.origin) if access_file else None
        self.log: list[RequestLog] = []

    @property
    def authenticated(self) -> bool:
        return self._auth is not None

    def request(self, method: str, path: str, body: Any = None) -> Any:
        parts = self._parts
        connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = connection_type(parts.hostname or "", parts.port, timeout=self._timeout)
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth
        started = time.monotonic()
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as error:
            raise OracleError(f"{method} {path}: {type(error).__name__}: {error}") from error
        finally:
            conn.close()
        self.log.append(
            RequestLog(
                method,
                path,
                response.status,
                int((time.monotonic() - started) * 1000),
                len(raw),
                None if body is None else sha256_hex(canonical_json(body, portable=False)),
            )
        )
        if response.status != 200:
            raise OracleError(f"{method} {path}: HTTP {response.status}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise OracleError(f"{method} {path}: the body is not JSON") from error

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    def reachable(self) -> bool:
        try:
            status = self.get("/sandbox/status")
        except OracleError:
            return False
        return isinstance(status, dict) and status.get("status") == "OK"


# --- Filters and small helpers --------------------------------------------------------------------


def gen3_filter(clauses: Sequence[Clause]) -> dict[str, Any]:
    """`{"and": [...]}` from canonical clauses (`in` and the comparison operators)."""
    items: list[dict[str, Any]] = []
    for clause in clauses:
        op, field_name, values = clause["op"], clause["field"], list(clause["values"])
        if op == "in":
            items.append({"in": {field_name: values}})
        else:
            items.append({op: {field_name: values[0]}})
    return {"and": items}


def case_id_filter(case_ids: Sequence[str]) -> dict[str, Any]:
    return {"and": [{"in": {"case_id": sorted(case_ids)}}]}


def group_keys(labels: Sequence[str]) -> list[str]:
    """Lowercase words minus those every label shares ("High risk", "Standard risk" -> high, standard)."""
    words = [label.lower().split() for label in labels]
    shared = set(words[0]).intersection(*words[1:]) if words else set()
    keys = ["_".join(w for w in ws if w not in shared) for ws in words]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        return [f"g{i + 1}" for i in range(len(labels))]
    return keys


def rank_with_ties(
    rows: Sequence[tuple[str, float]], n: int, descending: bool, order: Sequence[str] = ()
) -> tuple[list[tuple[str, float]], list[str]]:
    """Sort by value, then by `order` (keys in that order first, as ProteinPaint listed them) and then by key; keep
    the top n plus every row equal to the n-th; return (kept, tied keys)."""
    position = {key: i for i, key in enumerate(order)}
    ordered = sorted(rows, key=lambda r: ((-r[1] if descending else r[1]), position.get(r[0], len(position)), r[0]))
    if len(ordered) <= n:
        return list(ordered), []
    cutoff = ordered[n - 1][1]
    kept = list(ordered[:n])
    tied = [row for row in ordered[n:] if row[1] == cutoff]
    return kept + tied, [row[0] for row in tied]


# --- Results ------------------------------------------------------------------------------------


@dataclass
class SurvivalGroup:
    label: str
    key: str
    filter: dict[str, Any]
    cases: int
    with_survival: int
    events: int
    median_days: float | None


@dataclass
class J1Expected:
    scope_clauses: list[dict[str, Any]]
    scope_cases: int
    groups: list[SurvivalGroup]
    statistic: float
    df: int
    p_value: float
    engine_p_value: float | None
    method: str = "Mantel-Cox log-rank with hypergeometric variance, computed by the oracle"


@dataclass
class J2Expected:
    cohort_clauses: list[dict[str, Any]]
    scope_clauses: list[dict[str, Any]]
    cohort_size: int
    denominator: int
    mutation_tested_in_cohort: int
    n: int
    counts: dict[str, int]
    ranking: list[tuple[str, int]]
    shown: list[tuple[str, int]]
    ties: list[str]
    engine_ranking: list[tuple[str, int]] | None


@dataclass
class DeRow:
    gene: str
    log2fc: float
    padj: float
    p: float


@dataclass
class J3Expected:
    gene: str
    scope_clauses: list[dict[str, Any]]
    scope_cases: int
    tested: int
    mutated_in_scope: int
    mutated_tested: int
    outside_tested: int
    not_detected: int
    samples: dict[str, int]
    eligible: dict[str, int]
    eligible_ok: bool
    term_counts: dict[str, int] | None
    de: dict[str, Any] | None
    control_case_ids: list[str] = field(repr=False, default_factory=list)
    case_case_ids: list[str] = field(repr=False, default_factory=list)


def to_json(value: Any) -> Any:
    """Dataclasses as plain JSON, without the long case-ID lists."""
    data = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    if isinstance(data, dict):
        data.pop("control_case_ids", None)
        data.pop("case_case_ids", None)
    return data


# --- The oracle ---------------------------------------------------------------------------------


class Oracle:
    def __init__(self, gateway: Gateway) -> None:
        self.gw = gateway

    # Sources ---------------------------------------------------------------------------------

    def fingerprint(self) -> dict[str, Any]:
        """The four-source fixture fingerprint, read and hashed by the oracle."""
        status = self.gw.get("/sandbox/status")
        version = self.gw.get("/analysis/v0/_version")
        health = self.gw.get("/protein-paint/healthcheck")
        top = self.gw.post(
            "/protein-paint/termdb/topMutatedGenes",
            {"genome": GENOME, "dslabel": DSLABEL, "maxGenes": 50, "geneFilter": "CGC", "filter0": {"and": []}},
        )
        ranking = [{"gene": g["gene"], "cases": self._snv_count(g)} for g in top.get("genes", [])]
        document = fingerprint_document(status, version, health, ranking)
        return {"document": document, "sha256": fingerprint_sha256(document)}

    @staticmethod
    def _snv_count(gene: Mapping[str, Any]) -> int:
        return sum(int(s["count"]) for s in gene.get("mutationStat", []) if s.get("dt") == 1)

    def cases(self, flt: Mapping[str, Any], fields: Sequence[str]) -> tuple[int, list[dict[str, Any]]]:
        """Every case matching the filter, paged within Guppy's 10,000 window; (total, rows)."""
        rows: list[dict[str, Any]] = []
        offset, total = 0, None
        while True:
            body = {"filter": dict(flt), "fields": list(fields), "size": PAGE_SIZE, "offset": offset}
            page = self.gw.post("/analysis/v0/cases/", body)
            total = int(page["pagination"]["total"])
            rows.extend(page["data"])
            offset += PAGE_SIZE
            if offset >= total or offset + PAGE_SIZE > MAX_WINDOW:
                break
        if len(rows) < total:
            raise OracleError(f"{total} cases exceed the {MAX_WINDOW} the oracle can page")
        return total, rows

    def case_ids(self, flt: Mapping[str, Any]) -> list[str]:
        _, rows = self.cases(flt, ["case_id"])
        return sorted({str(r["case_id"]) for r in rows})

    def mutated(self, gene: str) -> set[str]:
        """Cases with a mutation in a panel gene, over all cases (the gene filter is and-wrapped on gene_id)."""
        body = {
            "cohort_filter": {"and": []},
            "gene_filter": {"and": [{"in": {"gene_id": [PANEL_GENE_IDS[gene]]}}]},
            "ssm_filter": {},
        }
        ids = self.gw.post("/analysis/v0/genomic/gene_cases", body)
        if not isinstance(ids, list):
            raise OracleError("gene_cases did not return a list of case ids")
        return {str(i) for i in ids}

    def sample_ids(self, case_ids: Sequence[str]) -> list[int]:
        body = {
            "genome": GENOME,
            "dslabel": DSLABEL,
            "getsamplelist": 1,
            "filter": dict(EMPTY_TVSLST),
            "filter0": case_id_filter(case_ids),
            "mapParent2Children": True,
        }
        rows = self.gw.post("/protein-paint/termdb", body)
        if not isinstance(rows, list):
            raise OracleError("getsamplelist did not return a list")
        return sorted(int(r["id"]) for r in rows)

    def term_sample_count(self, term_id: str, value: str) -> int:
        """Samples with a ProteinPaint dictionary value, such as KRAS mutation status = Mutated."""
        tvs = {"term": {"id": term_id, "type": "categorical"}, "values": [{"key": value, "label": value}]}
        body = {
            "genome": GENOME,
            "dslabel": DSLABEL,
            "getsamplelist": 1,
            "filter": {"type": "tvslst", "in": True, "join": "", "lst": [{"type": "tvs", "tvs": tvs}]},
            "filter0": {"and": []},
            "mapParent2Children": True,
        }
        rows = self.gw.post("/protein-paint/termdb", body)
        if not isinstance(rows, list):
            raise OracleError("getsamplelist did not return a list")
        return len(rows)

    # J1 ----------------------------------------------------------------------------------------

    @staticmethod
    def _observation(row: Mapping[str, Any]) -> Observation | None:
        demographic = row.get("demographic") or {}
        if demographic.get("vital_status") == "Dead" and demographic.get("days_to_death") is not None:
            return Observation(float(demographic["days_to_death"]), True)
        follow_ups = [d.get("days_to_last_follow_up") for d in row.get("diagnoses") or []]
        times = [float(t) for t in follow_ups if t is not None]
        return Observation(max(times), False) if times else None

    def j1(
        self,
        values: Sequence[str] = ("High risk", "Standard risk"),
        split_field: str = CGS_FIELD,
        scope_clauses: Sequence[Clause] = (),
    ) -> J1Expected:
        scope = [dict(c) for c in scope_clauses]
        scope_cases = len(self.case_ids(gen3_filter(scope)))
        keys = group_keys(list(values))
        groups: list[SurvivalGroup] = []
        observations: list[list[Observation]] = []
        for value, key in zip(values, keys, strict=True):
            flt = gen3_filter([*scope, {"field": split_field, "op": "in", "values": [value]}])
            total, rows = self.cases(flt, SURVIVAL_FIELDS)
            obs = [o for row in rows if (o := self._observation(row)) is not None]
            observations.append(obs)
            groups.append(SurvivalGroup(value, key, flt, total, len(obs), sum(o.event for o in obs), km_median(obs)))
        result = logrank(observations)
        engine = self.gw.post("/analysis/v0/survival/", {"filters": [g.filter for g in groups]})
        engine_p = (engine.get("overallStats") or {}).get("pValue")
        return J1Expected(
            scope,
            scope_cases,
            groups,
            result.statistic,
            result.df,
            result.p_value,
            None if engine_p is None else float(engine_p),
        )

    # J2 ----------------------------------------------------------------------------------------

    def j2(self, cohort_clauses: Sequence[Clause], n: int = 3, scope_clauses: Sequence[Clause] = ()) -> J2Expected:
        scope = [dict(c) for c in scope_clauses]
        clauses = [*scope, *(dict(c) for c in cohort_clauses)]
        cohort = set(self.case_ids(gen3_filter(clauses)))
        tested = set(self.case_ids(gen3_filter([*clauses, {"field": "available_variation_data", "op": "in", "values": ["ssm"]}])))
        counts = {gene: len(self.mutated(gene) & cohort) for gene in PANEL_GENE_IDS}
        rows = [(gene, float(c)) for gene, c in counts.items() if c > 0]
        engine = self.gw.post(
            "/protein-paint/termdb/topMutatedGenes",
            {"genome": GENOME, "dslabel": DSLABEL, "maxGenes": 50, "geneFilter": "CGC", "filter0": case_id_filter(sorted(cohort))},
        )
        engine_ranking = [(str(g["gene"]), self._snv_count(g)) for g in engine.get("genes", [])]
        # Equal counts keep ProteinPaint's order (TOOLS.md rank_top_n tieBreak `source`): the top n are the genes
        # OncoMatrix shows, and a tie names a gene it leaves out. The counts stay the oracle's own.
        order = [g for g, _ in engine_ranking]
        ranking = [(g, int(v)) for g, v in rank_with_ties(rows, len(rows), descending=True, order=order)[0]]
        shown, ties = rank_with_ties(rows, n, descending=True, order=order)
        return J2Expected(
            [dict(c) for c in cohort_clauses],
            scope,
            len(cohort),
            len(cohort),
            len(tested),
            n,
            counts,
            ranking,
            [(g, int(v)) for g, v in shown],
            ties,
            engine_ranking,
        )

    # J3 ----------------------------------------------------------------------------------------

    def de(
        self,
        control_samples: Sequence[int],
        case_samples: Sequence[int],
        method: str = "wilcoxon",
        thresholds: Mapping[str, Any] = DE_DEFAULT_THRESHOLDS,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """ProteinPaint's DE table for the two groups; thresholds, ranking and ties applied here."""
        groups = [
            {"name": "oracle control", "in": True, "values": [{"sampleId": i} for i in control_samples]},
            {"name": "oracle case", "in": True, "values": [{"sampleId": i} for i in case_samples]},
        ]
        cutoff = float(thresholds["pValueCutoff"])
        sent_cutoff = round(-math.log10(cutoff), 2)
        body = {
            "kind": "DE",
            "genome": GENOME,
            "dslabel": DSLABEL,
            "method": method,
            "min_count": 10,
            "min_total_count": 15,
            "cpm_cutoff": 1,
            "samplelst": {"groups": groups},
            "filter": dict(EMPTY_TVSLST),
            "filter0": None,
            "volcanoRender": {
                "significanceThresholds": {
                    "pValueCutoff": sent_cutoff,
                    "pValueType": thresholds["pValueType"],
                    "foldChangeCutoff": thresholds["foldChangeCutoff"],
                },
                "pixelWidth": 400,
                "pixelHeight": 400,
                "colorSignificant": "#ff0000",
                "colorSignificantUp": "#d95f02",
                "colorSignificantDown": "#1b9e77",
                "colorNonsignificant": "#000000",
                "dotRadius": 5,
                "maxInteractiveDots": 5000,
                "devicePixelRatio": 2,
            },
        }
        answer = self.gw.post("/protein-paint/termdb/DE", body)
        data = answer.get("data") or {}
        adjusted = thresholds["pValueType"] == "adjusted"
        fc_cutoff = float(thresholds["foldChangeCutoff"])
        rows = [
            DeRow(str(d["gene_name"]), float(d["fold_change"]), float(d["adjusted_p_value"]), float(d["original_p_value"]))
            for d in data.get("dots", [])
        ]
        # The viewer's rule on the value sent (TOOLS.md rank_top_n): -log10 p above the rounded cutoff (0.05 -> 1.3, so
        # p < 0.0501), p = 0 passing, and |log2FC| above its cutoff; the genes ProteinPaint marks significant.
        passing = [
            r for r in rows
            if ((p := r.padj if adjusted else r.p) <= 0 or -math.log10(p) > sent_cutoff) and abs(r.log2fc) > fc_cutoff
        ]
        by_gene = {r.gene: r for r in passing}
        kept, ties = rank_with_ties([(r.gene, r.padj) for r in passing], top_n, descending=False)
        return {
            "method": answer.get("method"),
            "sampleSizes": {"control": answer.get("sample_size1"), "case": answer.get("sample_size2")},
            "tested": data.get("totalRows"),
            "significant": data.get("totalSignificantRows"),
            "up": data.get("totalSignificantUp"),
            "down": data.get("totalSignificantDown"),
            "passing": len(passing),
            "top": [asdict(by_gene[g]) for g, _ in kept],
            "ties": ties,
            "engine": "proteinpaint DE (no second engine); thresholds, ranking and ties applied by the oracle",
        }

    def j3(
        self,
        gene: str = "KRAS",
        scope_clauses: Sequence[Clause] = (),
        top_n: int = 10,
        method: str = "wilcoxon",
        thresholds: Mapping[str, Any] = DE_DEFAULT_THRESHOLDS,
        run_de: bool = True,
    ) -> J3Expected:
        scope = [dict(c) for c in scope_clauses]
        scope_ids = set(self.case_ids(gen3_filter(scope)))
        tested = set(self.case_ids(gen3_filter([*scope, {"field": "available_variation_data", "op": "in", "values": ["ssm"]}])))
        mutated = self.mutated(gene) & scope_ids
        mutated_tested = mutated & tested
        not_detected = tested - mutated
        control = sorted(not_detected)
        case = sorted(mutated_tested)
        control_samples = self.sample_ids(control) if control else []
        case_samples = self.sample_ids(case) if case else []
        eligible = {"control": 0, "case": 0}
        if control_samples and case_samples:
            body = {
                "genome": GENOME,
                "dslabel": DSLABEL,
                "samplelst": {
                    "groups": [
                        {"name": "control", "in": True, "values": [{"sampleId": i} for i in control_samples]},
                        {"name": "case", "in": True, "values": [{"sampleId": i} for i in case_samples]},
                    ]
                },
                "filter": dict(EMPTY_TVSLST),
                "filter0": None,
                "preAnalysis": True,
            }
            pre = self.gw.post("/protein-paint/termdb/DE", body).get("data") or {}
            eligible = {"control": int(pre.get("control", 0)), "case": int(pre.get("case", 0))}
        eligible_ok = min(eligible.values()) >= ELIGIBLE_MIN
        term_counts = None
        if not scope and gene == "KRAS":
            term_counts = {
                "control": self.term_sample_count("kras_status", "No mutation"),
                "case": self.term_sample_count("kras_status", "Mutated"),
            }
        de = self.de(control_samples, case_samples, method, thresholds, top_n) if run_de and eligible_ok else None
        return J3Expected(
            gene,
            scope,
            len(scope_ids),
            len(tested),
            len(mutated),
            len(mutated_tested),
            len(mutated - tested),
            len(not_detected),
            {"control": len(control_samples), "case": len(case_samples)},
            eligible,
            eligible_ok,
            term_counts,
            de,
            control,
            case,
        )

    def count(self, clauses: Sequence[Clause]) -> int:
        return len(self.case_ids(gen3_filter(clauses)))
