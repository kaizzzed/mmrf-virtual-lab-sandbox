"""Each tool's request against its golden: the body built equals the golden except at the template's
substitutions; the snapshot equals orion/golden; and the request-pinned vectors run through the
request builders and the tools (the Day 2 PM parity checks)."""

import asyncio
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from test_tools_fakes import RUN_ID, FakeTransport, arr, fixed, goldens, json_response, obj, top_genes, vlab

from orion_agent.models import derive_request_pinned, pinned_hash
from orion_agent.tools.base import ServerToolName
from orion_agent.tools import bodies, goldens as golden_module
from orion_agent.tools.client import InProcessToolClient
from orion_agent.tools.goldens import GOLDEN_REQUESTS, SNAPSHOT_PATH, GoldenError, GoldenStore, payload_sha256, snapshot_text
from orion_agent.tools.schemas import (
    ALL_CASES_FILTER,
    CASES_QUERY_REQUEST,
    COHORT_COUNT_REQUEST,
    DE_PREANALYSIS_REQUEST,
    DE_RUN_REQUEST,
    EMPTY_TVSLST,
    GENE_CASES_REQUEST,
    GOLDEN_DIR_ENV,
    SAMPLE_LISTS_REQUEST,
    SURVIVAL_REQUEST,
    TOOL_REGISTRY,
    TOP_MUTATED_GENES_REQUEST,
    CaseIdGroup,
    CasesQueryInput,
    CohortCountInput,
    DePreanalysisInput,
    DeRunInput,
    GeneCasesInput,
    RequestTemplate,
    SurvivalCompareInput,
    TopMutatedGenesInput,
    resolve_pointer,
    tool_spec,
)
from orion_agent.tools.server import build_server_tools
from orion_agent.tools.vlab import wire_json

CONTRACTS = Path(os.environ.get("ORION_CONTRACTS_DIR", "/contracts"))
VECTORS = CONTRACTS / "fixtures" / "request-pinned-vectors.json"


def _golden_dir() -> Path | None:
    named = os.environ.get(GOLDEN_DIR_ENV)
    for candidate in ([Path(named)] if named else []) + [Path("/golden"), Path(__file__).resolve().parents[2] / "golden"]:
        if candidate.is_dir():
            return candidate
    return None


GOLDEN_DIR = _golden_dir()


def key_paths(value: Any) -> list[str]:
    """Every object key as a pointer (array items as *), in the order the objects hold them."""

    def walk(node: Any, at: str) -> list[str]:
        if isinstance(node, dict):
            return [p for key, child in node.items() for p in [f"{at}/{key}", *walk(child, f"{at}/{key}")]]
        if isinstance(node, list):
            return [p for child in node for p in walk(child, f"{at}/*")]
        return []

    return list(dict.fromkeys(walk(value, "")))


def diff_pointers(a: Any, b: Any, at: str = "") -> list[str]:
    """Every JSON pointer where a and b differ (the deepest differing node)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            token = str(key).replace("~", "~0").replace("/", "~1")
            if key not in a or key not in b:
                out.append(f"{at}/{token}")
            else:
                out += diff_pointers(a[key], b[key], f"{at}/{token}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in diff_pointers(x, y, f"{at}/{i}")]
    return [] if a == b and type(a) is type(b) else [at]


class GoldenStoreTests(unittest.TestCase):
    def test_the_snapshot_holds_every_golden_request_the_catalog_names(self) -> None:
        store = GoldenStore.from_snapshot()
        self.assertEqual(GOLDEN_REQUESTS, tuple(sorted({(t.golden, t.request_index) for s in TOOL_REGISTRY.values() for t in s.requests if t.golden})))
        for file, index in GOLDEN_REQUESTS:
            request = store.request(file, index)
            self.assertEqual(payload_sha256(request.payload), request.payload_sha256)

    def test_the_de_body_caps_the_dots_at_de_max_dots(self) -> None:
        """DeRunObserved accepts min(significant, DE_MAX_DOTS) genes: the cap is the golden body's own, a fixed pointer."""
        from orion_agent.tools.schemas import DE_MAX_DOTS, DE_RUN_REQUEST

        payload = GoldenStore.from_snapshot().payload(DE_RUN_REQUEST)
        assert isinstance(payload, dict) and isinstance(payload["volcanoRender"], dict)
        self.assertEqual(payload["volcanoRender"]["maxInteractiveDots"], DE_MAX_DOTS)
        self.assertIn("/volcanoRender/maxInteractiveDots", DE_RUN_REQUEST.fixed)

    @unittest.skipIf(GOLDEN_DIR is None, "orion/golden is not mounted (mount it at /golden or set ORION_GOLDEN_DIR)")
    def test_the_snapshot_is_the_goldens_verbatim(self) -> None:
        assert GOLDEN_DIR is not None
        from_dir = GoldenStore.from_dir(GOLDEN_DIR)
        self.assertEqual(snapshot_text(from_dir), SNAPSHOT_PATH.read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(golden_module.main(["check", str(GOLDEN_DIR)]), 0)
        self.assertIn("matches", printed.getvalue())

    def test_a_tampered_payload_is_refused(self) -> None:
        document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        document["requests"][0]["body"]["filters"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "snapshot.json")
            path.write_text(json.dumps(document))
            with self.assertRaises(GoldenError):
                GoldenStore.from_snapshot(path)
            document["requests"] = document["requests"][1:]
            path.write_text(json.dumps(document))
            with self.assertRaises(GoldenError):
                GoldenStore.from_snapshot(path)

    def test_load_prefers_an_existing_golden_dir_and_falls_back_to_the_snapshot(self) -> None:
        self.assertEqual(GoldenStore.load(Path("/nonexistent/golden")).source, SNAPSHOT_PATH.name)
        if GOLDEN_DIR is not None:
            self.assertEqual(GoldenStore.load(GOLDEN_DIR).source, str(GOLDEN_DIR))

    def test_payload_returns_a_fresh_copy(self) -> None:
        store = goldens()
        first = store.payload(TOP_MUTATED_GENES_REQUEST)
        assert isinstance(first, dict)
        first["maxGenes"] = 999
        self.assertEqual(obj(store.payload(TOP_MUTATED_GENES_REQUEST))["maxGenes"], 50)


class BodiesAgainstGoldenTests(unittest.TestCase):
    """The body built equals the template payload except at its declared substitutions."""

    def assert_only_substituted(self, template: RequestTemplate, body: Any, changed: set[str] | None = None) -> None:
        start = goldens().payload(template)
        declared = {s.pointer for s in template.substitutions}
        differing = diff_pointers(start, body)
        for pointer in differing:
            self.assertTrue(any(pointer == d or pointer.startswith(d + "/") for d in declared), f"{template.endpoint}: {pointer} is not a substitution")
        for pointer in template.fixed:
            # A fixed pointer keeps its captured value; one with a substitution below it (gene_cases'
            # `in` holding the substituted gene id) keeps everything but that substitution.
            if not any(d.startswith(pointer + "/") for d in declared):
                self.assertEqual(resolve_pointer(body, pointer), resolve_pointer(start, pointer), pointer)
            else:
                self.assertEqual(type(resolve_pointer(body, pointer)), type(resolve_pointer(start, pointer)), pointer)
        if changed is not None:
            self.assertEqual({d for d in declared if any(p == d or p.startswith(d + "/") for p in differing)}, changed)

    def test_cohort_count(self) -> None:
        filter_ = {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}
        body = bodies.cohort_count_body(CohortCountInput.model_validate({"filter": filter_}), goldens())
        self.assert_only_substituted(COHORT_COUNT_REQUEST, body, {"/variables/filter"})
        self.assertEqual(body["variables"], {"filter": filter_})

    def test_cases_query_pages_and_the_short_last_page(self) -> None:
        inp = CasesQueryInput.model_validate({"filter": {"and": [{"in": {"available_variation_data": ["ssm"]}}]}, "pageSize": 1000, "maxIds": 2500})
        body = bodies.cases_query_body(inp, goldens(), offset=2000, size=500)
        self.assert_only_substituted(CASES_QUERY_REQUEST, body, {"/filter", "/size", "/offset"})
        self.assertEqual((body["size"], body["offset"], body["fields"]), (500, 2000, ["case_id"]))
        with self.assertRaises(bodies.BodyError):
            bodies.cases_query_body(inp, goldens(), offset=2000, size=1000)

    def test_gene_cases_wraps_the_gene_filter_in_and(self) -> None:
        body = bodies.gene_cases_body(GeneCasesInput.model_validate({"gene": "TP53", "cohortFilter": {"and": []}}), goldens())
        self.assert_only_substituted(GENE_CASES_REQUEST, body, {"/gene_filter/and/0/in/gene_id/0"})
        self.assertEqual(body["gene_filter"], {"and": [{"in": {"gene_id": ["ENSG00000141510"]}}]})
        self.assertEqual(body["ssm_filter"], {})

    def test_top_mutated_genes_asks_for_one_more_gene_and_sends_the_scope(self) -> None:
        all_cases = bodies.top_mutated_genes_body(TopMutatedGenesInput.model_validate({"filter0": None, "geneCount": 3}), goldens())
        self.assert_only_substituted(TOP_MUTATED_GENES_REQUEST, all_cases, {"/maxGenes"})
        self.assertEqual((all_cases["maxGenes"], all_cases["filter0"]), (4, ALL_CASES_FILTER))
        scoped = {"and": [{"in": {"case_id": ["SYNTH-CASE-0003", "SYNTH-CASE-0001"]}}]}
        body = bodies.top_mutated_genes_body(TopMutatedGenesInput.model_validate({"filter0": scoped, "geneCount": 50}), goldens())
        self.assert_only_substituted(TOP_MUTATED_GENES_REQUEST, body, {"/maxGenes", "/filter0"})
        self.assertEqual((body["maxGenes"], body["filter0"], body["geneFilter"]), (51, scoped, "CGC"))

    def test_the_fingerprint_ranking_is_the_golden_unchanged(self) -> None:
        self.assertEqual(diff_pointers(goldens().payload(TOP_MUTATED_GENES_REQUEST), bodies.fingerprint_ranking_body(goldens())), [])

    def test_survival_sends_each_group_then_all_groups(self) -> None:
        groups = [{"label": v, "filter": {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": [v]}}]}} for v in ("High risk", "Standard risk")]
        inp = SurvivalCompareInput.model_validate({"scope": None, "term": "overall_survival", "split": None, "groups": groups})
        built = bodies.survival_bodies(inp, goldens())
        self.assertEqual(len(built), 3)
        for body in built:
            self.assert_only_substituted(SURVIVAL_REQUEST, body)
            self.assertNotIn("survivalType", body)
        self.assertEqual([b["filters"] for b in built], [[groups[0]["filter"]], [groups[1]["filter"]], [g["filter"] for g in groups]])

    def test_sample_lists_send_the_case_ids_as_filter0_and_no_native_filter(self) -> None:
        group = CaseIdGroup.model_validate({"key": "control", "caseIds": ["SYNTH-CASE-0009", "SYNTH-CASE-0002"]})
        body = bodies.sample_list_body(group, goldens())
        self.assert_only_substituted(SAMPLE_LISTS_REQUEST, body, {"/filter", "/filter0"})
        self.assertEqual(body["filter"], EMPTY_TVSLST)
        self.assertEqual(body["filter0"], {"and": [{"in": {"case_id": ["SYNTH-CASE-0002", "SYNTH-CASE-0009"]}}]})
        self.assertEqual((body["getsamplelist"], body["mapParent2Children"]), (1, True))

    def test_de_preanalysis_names_the_groups_control_and_case(self) -> None:
        inp = DePreanalysisInput.model_validate({"groups": [{"key": "control", "sampleIds": [3, 7]}, {"key": "case", "sampleIds": [1]}]})
        body = bodies.de_preanalysis_body(inp, goldens())
        self.assert_only_substituted(DE_PREANALYSIS_REQUEST, body)
        groups = obj(body["samplelst"])["groups"]
        self.assertEqual([(g["name"], g["values"], g["in"]) for g in groups], [("control", [{"sampleId": 3}, {"sampleId": 7}], True), ("case", [{"sampleId": 1}], True)])
        self.assertEqual((body["preAnalysis"], body["filter0"]), (True, {"and": []}))

    def test_de_run_is_the_viewer_body_with_names_samples_method_and_thresholds(self) -> None:
        groups = [{"name": "KRAS not detected (ORION)", "sampleIds": [3, 7]}, {"name": "KRAS mutated (ORION)", "sampleIds": [1, 2]}]
        with_thresholds = DeRunInput.model_validate(
            {"groups": groups, "method": "edgeR", "thresholds": {"pValueCutoff": 0.01, "pValueType": "original", "foldChangeCutoff": 0.5}}
        )
        body = bodies.de_run_body(with_thresholds, goldens())
        self.assert_only_substituted(DE_RUN_REQUEST, body)
        self.assertEqual((body["method"], body["filter0"], body["kind"]), ("edgeR", None, "DE"))
        self.assertEqual(
            [(g["name"], g["values"]) for g in obj(body["samplelst"])["groups"]],
            [("KRAS not detected (ORION)", [{"sampleId": 3}, {"sampleId": 7}]), ("KRAS mutated (ORION)", [{"sampleId": 1}, {"sampleId": 2}])],
        )
        self.assertEqual(obj(body["volcanoRender"])["significanceThresholds"], {"pValueCutoff": 2.0, "pValueType": "original", "foldChangeCutoff": 0.5})
        default = bodies.de_run_body(DeRunInput.model_validate({"groups": groups, "method": "wilcoxon"}), goldens())
        golden = goldens().payload(DE_RUN_REQUEST)
        self.assertEqual(default["volcanoRender"], obj(golden)["volcanoRender"])
        self.assertNotIn("/volcanoRender/significanceThresholds/pValueCutoff", diff_pointers(golden, default))

    def test_de_run_sends_the_viewer_body_in_the_viewer_key_order(self) -> None:
        # ProteinPaint's DE cache key (data.cacheId) depends on the body's key order, so the body
        # de_run sends keeps the golden's order at every level: only then does the DE card's own
        # request read the cache entry s9 made ("same entry as s9" in the card's request list).
        golden = goldens().payload(DE_RUN_REQUEST)
        self.assertEqual(list(obj(golden))[:4], ["kind", "genome", "dslabel", "method"])
        groups = [{"name": "KRAS not detected (ORION)", "sampleIds": [3, 7]}, {"name": "KRAS mutated (ORION)", "sampleIds": [1, 2]}]
        body = bodies.de_run_body(DeRunInput.model_validate({"groups": groups, "method": "wilcoxon"}), goldens())
        # filter0 is a substitution (null for the golden's {"and": []}), so its inside is not compared.
        expected = [p for p in key_paths(golden) if not p.startswith("/filter0/")]
        self.assertEqual(key_paths(body), expected)
        self.assertEqual(key_paths(json.loads(wire_json(body))), expected)

    @unittest.skipIf(GOLDEN_DIR is None, "orion/golden is not mounted (mount it at /golden or set ORION_GOLDEN_DIR)")
    def test_the_snapshot_keeps_each_golden_payload_in_its_captured_key_order(self) -> None:
        assert GOLDEN_DIR is not None
        from_dir, from_snapshot = GoldenStore.from_dir(GOLDEN_DIR), GoldenStore.from_snapshot()
        for file, index in GOLDEN_REQUESTS:
            with self.subTest(golden=f"{file}#{index}"):
                self.assertEqual(wire_json(from_snapshot.request(file, index).payload), wire_json(from_dir.request(file, index).payload))

    def test_fill_refuses_an_undeclared_pointer(self) -> None:
        with self.assertRaises(bodies.BodyError):
            bodies.fill(TOP_MUTATED_GENES_REQUEST, goldens(), {"/geneFilter": "all"})
        with self.assertRaises(bodies.BodyError):
            bodies.set_pointer({"a": {}}, "/a/b", 1)


def _vectors() -> list[dict[str, Any]]:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors: list[dict[str, Any]] = data["vectors"]
    return vectors


@unittest.skipUnless(VECTORS.is_file(), f"{VECTORS} is not mounted")
class RequestPinnedParityTests(unittest.TestCase):
    """The request each viz-feeding tool records and sends is the one derive_request_pinned expects."""

    TOOL = {"survival": "survival_compare", "oncomatrix": "top_mutated_genes", "deDirect": "de_run", "deInput": "de_run"}

    def test_every_vector_request_is_its_tool_input_byte_for_byte(self) -> None:
        for vector in _vectors():
            with self.subTest(vector["name"]):
                model = tool_spec(self.TOOL[vector["launcher"]]).input_model
                dumped = model.model_validate(vector["request"]).model_dump(mode="json", by_alias=True)
                self.assertEqual(dumped, vector["request"])
                pinned = derive_request_pinned(vector["launcher"], dumped, vector.get("observed"))
                self.assertEqual(pinned_hash(pinned), vector["requestPinnedHash"])

    def test_the_upstream_body_carries_what_the_card_pins(self) -> None:
        for vector in _vectors():
            launcher, request, pinned = vector["launcher"], vector["request"], vector["pinned"]
            with self.subTest(vector["name"]):
                if launcher == "oncomatrix":
                    body = bodies.top_mutated_genes_body(TopMutatedGenesInput.model_validate(request), goldens())
                    self.assertEqual(body["filter0"], pinned["filter0"] if pinned["filter0"] is not None else ALL_CASES_FILTER)
                elif launcher == "survival":
                    built = bodies.survival_bodies(SurvivalCompareInput.model_validate(request), goldens())
                    self.assertEqual(built[-1]["filters"], [g["filter"] for g in request["groups"]])
                    self.assertEqual(pinned["filter0"], request["scope"])
                    self.assertEqual(pinned["overlay"], request["split"]["overlay"])
                else:
                    body = bodies.de_run_body(DeRunInput.model_validate(request), goldens())
                    sent = obj(body["samplelst"])["groups"]
                    self.assertEqual([g["name"] for g in sent], [g["name"] for g in pinned["groups"]])
                    if launcher == "deDirect":
                        self.assertEqual([[v["sampleId"] for v in g["values"]] for g in sent], [g["sampleIds"] for g in pinned["groups"]])
                        self.assertEqual(body["method"], pinned["method"])
                        thresholds = obj(body["volcanoRender"])["significanceThresholds"]
                        if "volcano" in pinned:
                            volcano = pinned["volcano"]
                            self.assertEqual(
                                (thresholds["pValueCutoff"], thresholds["pValueType"], thresholds["foldChangeCutoff"]),
                                (volcano["pValue"], volcano["pValueType"], volcano["foldChangeCutoff"]),
                            )
                        else:
                            self.assertEqual(thresholds, obj(obj(goldens().payload(DE_RUN_REQUEST))["volcanoRender"])["significanceThresholds"])

    def test_the_tools_record_requests_and_outputs_that_reproduce_request_pinned_hash(self) -> None:
        for vector in _vectors():
            launcher = vector["launcher"]
            with self.subTest(vector["name"]):
                transport = FakeTransport()
                if launcher == "oncomatrix":
                    transport.routes["/protein-paint/termdb/topMutatedGenes"] = top_genes([(g["gene"], g["cases"]) for g in vector["observed"]["genes"]])
                elif launcher == "survival":
                    rows = [{"time": 10, "id": "SYNTH-CASE-0001", "submitter_id": "SYNTH-CASE-0001", "project_id": "SYNTHETIC-MM", "survivalEstimate": 1.0, "censored": True}]
                    transport.routes["/analysis/v0/survival/"] = fixed(200, {"results": [{"meta": {}, "donors": rows}], "overallStats": {"pValue": 0.5}})
                else:
                    groups = vector["request"]["groups"]
                    answer = {"method": vector["request"]["method"], "sample_size1": len(groups[0]["sampleIds"]), "sample_size2": len(groups[1]["sampleIds"]),
                              "data": {"dots": [], "totalRows": 10, "totalSignificantRows": 0, "totalSignificantUp": 0, "totalSignificantDown": 0, "cacheId": "c"}}
                    transport.routes["/protein-paint/termdb/DE"] = fixed(200, answer)
                client = InProcessToolClient(build_server_tools(vlab(transport), goldens()))
                outcome = asyncio.run(
                    client.call(cast(ServerToolName, self.TOOL[launcher]), copy.deepcopy(vector["request"]), timeout_s=60, run_id=RUN_ID, plan_version=1, step_id="s3", first_call=1)
                )
                self.assertIn(outcome.result.status, ("ok", "empty"), outcome.result.error)
                assert isinstance(outcome.request, dict)
                pinned = derive_request_pinned(launcher, outcome.request, outcome.result.observed)
                self.assertEqual(pinned_hash(pinned), vector["requestPinnedHash"])


if __name__ == "__main__":
    unittest.main()
