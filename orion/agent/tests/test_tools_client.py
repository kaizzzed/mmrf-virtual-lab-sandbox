"""The in-process ToolClient under the A4 budget: callIds, retries with identical bodies, the 429
backoff, the step cap and timeout, the per-run call budget, cancellation, fault injection (E11)
and the factory."""

import asyncio
import json
import unittest
from collections.abc import Coroutine
from typing import Any, TypeVar

from test_tools_fakes import (
    RUN_ID,
    FakeTransport,
    ManualClock,
    SentRequest,
    fake_stack,
    fixed,
    goldens,
    guppy,
    json_response,
    obj,
    raises,
    vlab,
)

from orion_agent.models import canonical_json, sha256_hex
from orion_agent.settings import Settings
from orion_agent.tools.base import MAX_TOOL_CALLS_PER_RUN, STEP_TIMEOUT_S
from orion_agent.tools.client import (
    CallBudget,
    GatewayToolClient,
    InProcessToolClient,
    make_tool_client,
    resolved_request,
)
from orion_agent.tools.config import ToolConfig
from orion_agent.tools.credentials import SecretsManagerCredential
from orion_agent.tools.gateway import GatewayConfigError
from orion_agent.tools.http import HttpResponse, HttpTimeout
from orion_agent.tools.records import RecordedOutcome, ToolCallCancelled, call_records
from orion_agent.tools.server import build_server_tools

T = TypeVar("T")
HIGH: dict[str, Any] = {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}
DE: dict[str, Any] = {"groups": [{"name": "KRAS not detected (ORION)", "sampleIds": [1, 3]}, {"name": "KRAS mutated (ORION)", "sampleIds": [2, 5]}], "method": "wilcoxon"}


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def client(transport: FakeTransport, clock: ManualClock | None = None, **kwargs: Any) -> InProcessToolClient:
    clock = clock or ManualClock()
    return InProcessToolClient(build_server_tools(vlab(transport), goldens(), monotonic=clock.monotonic), clock=clock.clock(), **kwargs)


async def call(c: Any, name: str, input: Any, *, step_id: str = "s2", first_call: int = 1, on_retry: Any = None, timeout_s: float = STEP_TIMEOUT_S) -> RecordedOutcome:
    outcome: RecordedOutcome = await c.call(name, input, timeout_s=timeout_s, run_id=RUN_ID, plan_version=1, step_id=step_id, first_call=first_call, on_retry=on_retry)
    return outcome


class RetryTests(unittest.TestCase):
    def test_a_success_is_one_attempt_with_its_call_id(self) -> None:
        outcome = run(call(client(fake_stack()), "cohort_count", {"filter": HIGH}, first_call=3))
        self.assertEqual((outcome.transport, outcome.tool, outcome.tool_version), ("in_process", "cohort_count", "cohort_count@1"))
        self.assertEqual([a.call_id for a in outcome.attempts], [f"{RUN_ID}.v1.s2.c3"])
        self.assertEqual((outcome.attempts[0].http_status, outcome.attempts[0].backoff_ms, outcome.result.observed), (200, 0, {"count": 7}))
        self.assertEqual(outcome.request, {"filter": HIGH})
        self.assertEqual(outcome.request_sha256, sha256_hex(canonical_json({"filter": HIGH}, portable=False)))
        self.assertEqual(outcome.response_sha256, sha256_hex(canonical_json({"count": 7}, portable=False)))

    def test_a_429_backs_off_10_to_20_s_and_retries_the_identical_body(self) -> None:
        clock = ManualClock()
        transport = fake_stack()
        transport.routes["/protein-paint/termdb/DE"] = [fixed(429, {"error": "CACHE_BUSY"}), transport.routes["/protein-paint/termdb/DE"]]  # type: ignore[list-item]
        retries: list[tuple[int, int]] = []

        async def on_retry(attempt: int, backoff_ms: int) -> None:
            retries.append((attempt, backoff_ms))
            self.assertEqual(clock.sleeps, [])  # reported before the backoff starts

        outcome = run(call(client(transport, clock, jitter=lambda: 0.25), "de_run", DE, step_id="s9", first_call=1, on_retry=on_retry))
        self.assertEqual(outcome.result.status, "ok")
        self.assertEqual([a.call_id for a in outcome.attempts], [f"{RUN_ID}.v1.s9.c1", f"{RUN_ID}.v1.s9.c2"])
        self.assertEqual([(a.attempt, a.status, a.http_status, a.backoff_ms) for a in outcome.attempts], [(1, "error", 429, 12500), (2, "ok", 200, 0)])
        self.assertEqual((retries, clock.sleeps), ([(2, 12500)], [12.5]))
        self.assertEqual(transport.sent[0].body, transport.sent[1].body)
        self.assertEqual(outcome.attempts[0].error.code, "CACHE_BUSY")  # type: ignore[union-attr]
        records = call_records(outcome, plan_version=1, step_id="s9")
        self.assertEqual([r.call_id for r in records], [a.call_id for a in outcome.attempts])
        self.assertEqual([(r.status, r.limitation) for r in records], [("error", None), ("ok", None)])
        self.assertEqual(records[0].error, outcome.attempts[0].error and f"CACHE_BUSY: {outcome.attempts[0].error.message}")
        self.assertEqual(len(records[0].provenance), 1)
        self.assertIsNone(records[0].observed)
        self.assertEqual(records[0].request_sha256, records[1].request_sha256)

    def test_the_backoff_spans_10_to_20_s(self) -> None:
        for jitter, expected in ((0.0, 10.0), (0.999999, 19.99999)):
            clock = ManualClock()
            transport = FakeTransport({"/guppy/graphql": fixed(429, {})})
            run(call(client(transport, clock, jitter=lambda j=jitter: j), "cohort_count", {"filter": HIGH}))
            self.assertAlmostEqual(clock.sleeps[0], expected, places=4)

    def test_502_503_504_and_timeouts_retry_once_then_fail(self) -> None:
        for responder, code in ((fixed(502, {}), "HTTP_ERROR"), (fixed(503, {}), "HTTP_ERROR"), (fixed(504, {}), "HTTP_ERROR"), (raises(HttpTimeout("x")), "TIMEOUT")):
            clock = ManualClock()
            transport = FakeTransport({"/guppy/graphql": responder})
            outcome = run(call(client(transport, clock), "cohort_count", {"filter": HIGH}))
            self.assertEqual((outcome.result.status, outcome.result.error and outcome.result.error.code), ("error", code))
            self.assertEqual(len(outcome.attempts), 2)
            self.assertEqual(len(transport.sent), 2)
            self.assertEqual(clock.sleeps, [] if code == "TIMEOUT" else [1.0])
            self.assertEqual(outcome.result.limitation, "TOOL_FAILED")
            records = call_records(outcome, plan_version=1, step_id="s2")
            self.assertEqual([r.limitation for r in records], [None, "TOOL_FAILED"])
            self.assertEqual([r.error.split(":")[0] if r.error else None for r in records], [code, code])
            self.assertEqual([len(r.provenance) for r in records], [1, 1])

    def test_non_retryable_failures_are_one_attempt(self) -> None:
        for responder in (fixed(400, {"error": "Invalid sandbox request"}), fixed(500, {}), fixed(200, {"errors": [{"message": "x"}]})):
            transport = FakeTransport({"/guppy/graphql": responder})
            outcome = run(call(client(transport), "cohort_count", {"filter": HIGH}))
            self.assertEqual((len(outcome.attempts), len(transport.sent)), (1, 1))
        outcome = run(call(client(FakeTransport()), "kg_query", {"question": "q"}))
        self.assertEqual((len(outcome.attempts), outcome.result.status), (1, "unavailable"))
        outcome = run(call(client(FakeTransport()), "gene_cases", {"gene": "EGFR", "cohortFilter": {"and": []}}))
        self.assertEqual((len(outcome.attempts), outcome.result.limitation), (1, "OUT_OF_PANEL"))

    def test_a_retry_that_succeeds_keeps_both_attempts(self) -> None:
        transport = FakeTransport({"/guppy/graphql": [fixed(503, {}), guppy(5)]})
        outcome = run(call(client(transport), "cohort_count", {"filter": HIGH}))
        self.assertEqual((outcome.result.observed, [a.status for a in outcome.attempts]), ({"count": 5}, ["error", "ok"]))
        self.assertEqual(len(outcome.attempt_results), 2)

    def test_no_retry_when_the_step_cap_leaves_no_room(self) -> None:
        clock = ManualClock()

        def slow(request: SentRequest) -> HttpResponse:
            clock.advance(128.5)  # 1.5 s left of the 130 s cap, less the 1 s backoff: no room for a retry
            return json_response(503, {})

        outcome = run(call(client(FakeTransport({"/guppy/graphql": slow}), clock), "cohort_count", {"filter": HIGH}, timeout_s=200))
        self.assertEqual(len(outcome.attempts), 1)

    def test_the_second_attempt_gets_what_remains_of_the_step_cap(self) -> None:
        clock = ManualClock()
        budgets: list[float] = []

        def slow(request: SentRequest) -> HttpResponse:
            budgets.append(request.timeout_s)
            clock.advance(100.0)
            return json_response(504, {})

        run(call(client(FakeTransport({"/guppy/graphql": slow}), clock), "cohort_count", {"filter": HIGH}, timeout_s=200))
        self.assertEqual(budgets[0], 55.0)
        self.assertLessEqual(budgets[1], 130.0 - 100.0 - 1.0)

    def test_an_attempt_over_the_step_timeout_is_a_timeout(self) -> None:
        async def hang(request: SentRequest) -> HttpResponse:
            await asyncio.sleep(10)
            return json_response(200, {})

        transport = FakeTransport({"/guppy/graphql": hang})
        c = InProcessToolClient(build_server_tools(vlab(transport), goldens()))
        outcome = run(call(c, "cohort_count", {"filter": HIGH}, timeout_s=0.05))
        self.assertEqual((outcome.result.error and outcome.result.error.code, len(outcome.attempts)), ("TIMEOUT", 2))
        self.assertEqual([[p.http_status for p in r.provenance] for r in outcome.attempt_results], [[None], [None]])


class BudgetAndIdsTests(unittest.TestCase):
    def test_the_run_has_at_most_20_tool_calls(self) -> None:
        transport = fake_stack()
        c = client(transport)

        async def go() -> list[RecordedOutcome]:
            return [await call(c, "cohort_count", {"filter": HIGH}, step_id=f"s{i}") for i in range(1, MAX_TOOL_CALLS_PER_RUN + 2)]

        outcomes = run(go())
        self.assertTrue(all(o.result.status == "ok" for o in outcomes[:-1]))
        last = outcomes[-1]
        self.assertEqual((last.result.error and last.result.error.code, last.result.limitation), ("GUARD_FAILED", "CALL_BUDGET"))
        self.assertEqual(len(transport.sent), MAX_TOOL_CALLS_PER_RUN)
        self.assertEqual(c.budget.used(RUN_ID), MAX_TOOL_CALLS_PER_RUN)
        other = run(c.call("cohort_count", {"filter": HIGH}, timeout_s=60, run_id="f" * 32, plan_version=1, step_id="s1", first_call=1))
        self.assertEqual(other.result.status, "ok")

    def test_the_budget_forgets_old_runs(self) -> None:
        budget = CallBudget(2, remember=2)
        self.assertTrue(budget.take("a") and budget.take("a"))
        self.assertFalse(budget.take("a"))
        budget.take("b")
        budget.take("c")
        self.assertEqual(budget.used("a"), 0)

    def test_the_request_is_the_validated_input_with_its_defaults(self) -> None:
        request, digest = resolved_request("cases_query", {"filter": {"and": []}})
        self.assertEqual(request, {"filter": {"and": []}, "pageSize": 1000, "maxIds": 10000})
        self.assertEqual(digest, sha256_hex(canonical_json(request, portable=False)))
        raw, _ = resolved_request("gene_cases", {"gene": "EGFR", "cohortFilter": {"and": []}})
        self.assertEqual(raw, {"gene": "EGFR", "cohortFilter": {"and": []}})

    def test_arguments_are_checked(self) -> None:
        c = client(fake_stack())
        with self.assertRaises(ValueError):
            run(c.call("set_operation", {}, timeout_s=60, run_id=RUN_ID, plan_version=1, step_id="s1", first_call=1))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run(c.call("cohort_count", {}, timeout_s=60, run_id=RUN_ID, plan_version=1, step_id="s1", first_call=0))


class CancellationTests(unittest.TestCase):
    def test_cancelling_aborts_the_attempt_and_records_it_cancelled(self) -> None:
        started = asyncio.Event()

        async def hang(request: SentRequest) -> HttpResponse:
            started.set()
            await asyncio.sleep(30)
            return json_response(200, {})

        transport = fake_stack()
        transport.routes["/protein-paint/termdb/DE"] = hang
        c = client(transport)

        async def go() -> ToolCallCancelled:
            task = asyncio.create_task(call(c, "de_run", DE, step_id="s9"))
            await started.wait()
            task.cancel()
            try:
                await task
            except ToolCallCancelled as error:
                self.assertTrue(task.cancelled())
                return error
            raise AssertionError("not cancelled")

        error = run(go())
        self.assertIsInstance(error, asyncio.CancelledError)
        attempts = error.outcome.attempts
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts[0].status, attempts[0].error and attempts[0].error.code), ("error", "CANCELLED"))
        record = call_records(error.outcome, plan_version=1, step_id="s9")[0]
        self.assertTrue((record.error or "").startswith("CANCELLED"))
        self.assertEqual((record.http_status, record.response_sha256), (None, None))
        self.assertEqual([(p.path, p.http_status) for p in record.provenance], [("/protein-paint/termdb/DE", None)])

    def test_cancelling_during_the_backoff_keeps_the_first_attempt(self) -> None:
        class SlowSleep(ManualClock):
            async def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                await asyncio.sleep(30)

        clock = SlowSleep()
        transport = FakeTransport({"/guppy/graphql": fixed(429, {})})
        c = client(transport, clock)

        async def go() -> ToolCallCancelled:
            task = asyncio.create_task(call(c, "cohort_count", {"filter": HIGH}))
            while not clock.sleeps:
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except ToolCallCancelled as error:
                return error
            raise AssertionError("not cancelled")

        error = run(go())
        self.assertEqual([a.attempt for a in error.outcome.attempts], [1])
        self.assertEqual(len(transport.sent), 1)


class FaultInjectionTests(unittest.TestCase):
    def factory(self, faults: str, transport: FakeTransport, clock: ManualClock | None = None, **settings: Any) -> Any:
        clock = clock or ManualClock()
        return make_tool_client(
            Settings.model_validate({"faults": faults, **settings}), config=ToolConfig(), http=transport, goldens=goldens(), clock=clock.clock(),
            jitter=lambda: 0.5,
        )

    def test_a_de_run_timeout_fails_both_attempts_without_a_request(self) -> None:
        transport = fake_stack()
        clock = ManualClock()
        c = self.factory("de_run:timeout", transport, clock)
        outcome = run(call(c, "de_run", DE, step_id="s9"))
        self.assertEqual((outcome.result.error and outcome.result.error.code, len(outcome.attempts)), ("TIMEOUT", 2))
        self.assertEqual((transport.sent, clock.sleeps), ([], []))
        self.assertEqual(run(call(c, "cohort_count", {"filter": HIGH})).result.observed, {"count": 7})

    def test_an_empty_cohort_count_is_a_zero_with_its_limitation(self) -> None:
        transport = fake_stack()
        outcome = run(call(self.factory("cohort_count:empty", transport), "cohort_count", {"filter": HIGH}))
        self.assertEqual((outcome.result.status, outcome.result.observed, outcome.result.limitation), ("empty", {"count": 0}, "EMPTY_COHORT"))
        self.assertEqual(transport.sent, [])

    def test_error_cache_busy_and_the_other_empty_forms(self) -> None:
        transport = fake_stack()
        clock = ManualClock()
        c = self.factory("survival_compare:error,de_preanalysis:cache_busy,gene_cases:empty,sample_lists:empty,top_mutated_genes:empty", transport, clock)
        survival = run(call(c, "survival_compare", {"scope": None, "term": "overall_survival", "split": None,
                                                    "groups": [{"label": "A", "filter": HIGH}, {"label": "B", "filter": HIGH}]}))
        self.assertEqual((survival.result.error and survival.result.error.code, len(survival.attempts)), ("HTTP_ERROR", 1))
        pre = run(call(c, "de_preanalysis", {"groups": [{"key": "control", "sampleIds": [1]}, {"key": "case", "sampleIds": [2]}]}))
        self.assertEqual((pre.result.error and pre.result.error.code, len(pre.attempts), clock.sleeps), ("CACHE_BUSY", 2, [15.0]))
        genes = run(call(c, "gene_cases", {"gene": "KRAS", "cohortFilter": {"and": []}}))
        self.assertEqual((genes.result.status, genes.result.observed), ("empty", {"gene": "KRAS", "count": 0, "caseIds": []}))
        samples = run(call(c, "sample_lists", {"groups": [{"key": "control", "caseIds": ["SYNTH-CASE-0001"]}, {"key": "case", "caseIds": ["SYNTH-CASE-0002"]}]}))
        self.assertEqual((samples.result.status, samples.result.limitation), ("empty", "EMPTY_COHORT"))
        top = run(call(c, "top_mutated_genes", {"filter0": None, "geneCount": 3}))
        self.assertEqual((top.result.status, top.result.limitation), ("empty", "FEWER_THAN_N"))
        self.assertEqual(transport.sent, [])

    def test_the_fingerprint_keeps_its_version_reads_when_only_its_ranking_is_empty(self) -> None:
        transport = fake_stack()
        outcome = run(call(self.factory("fixture_fingerprint:empty", transport), "fixture_fingerprint", {}, step_id="s1"))
        self.assertEqual(outcome.result.status, "ok")
        self.assertEqual(obj(obj(outcome.result.observed)["document"])["ranking"], [])
        self.assertEqual(transport.paths(), ["/sandbox/status", "/analysis/v0/_version", "/protein-paint/healthcheck"])

    def test_faults_are_refused_outside_mode_l_and_a_test_runtime(self) -> None:
        with self.assertRaises(ValueError):
            Settings.model_validate({"mode": "A", "faults": "de_run:timeout"})
        transport = FakeTransport()
        c = make_tool_client(
            Settings.model_validate(
                {"mode": "A", "tools": "gateway", "test_runtime": True, "faults": "de_run:timeout", "vlab_origin": "https://d1abc.cloudfront.net"}
            ),
            config=ToolConfig(gateway_url="http://127.0.0.1:9/mcp"), http=transport, goldens=goldens(), aws_credentials=lambda: None,
        )
        self.assertIsInstance(c, GatewayToolClient)
        outcome = run(call(c, "de_run", DE, step_id="s9"))
        self.assertEqual((outcome.transport, outcome.result.error and outcome.result.error.code, transport.sent), ("gateway", "TIMEOUT", []))


class FactoryTests(unittest.TestCase):
    GATEWAY = "https://gw-1.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

    def test_orion_tools_picks_in_process_or_gateway(self) -> None:
        local = make_tool_client(Settings(), config=ToolConfig(), http=FakeTransport(), goldens=goldens())
        self.assertEqual((type(local), local.transport), (InProcessToolClient, "in_process"))
        for mode in ("A", "H"):
            remote = make_tool_client(Settings(mode=mode, tools="gateway"), config=ToolConfig(gateway_url=self.GATEWAY),
                                      http=FakeTransport(), goldens=goldens(), aws_credentials=lambda: None)
            self.assertEqual((type(remote), remote.transport), (GatewayToolClient, "gateway"))
            with self.assertRaises(GatewayConfigError):
                make_tool_client(Settings(mode=mode, tools="gateway"), config=ToolConfig(), http=FakeTransport(), goldens=goldens())
        cut = make_tool_client(Settings(mode="A", vlab_origin="https://d1abc.cloudfront.net"), config=ToolConfig(), http=FakeTransport(), goldens=goldens())
        self.assertEqual(cut.transport, "in_process")

    def test_the_in_process_client_reads_the_secret_on_aws(self) -> None:
        arn = "arn:aws:secretsmanager:us-east-1:000000000000:secret:orion/vlab-basic"
        c = make_tool_client(Settings(mode="A", vlab_origin="https://d1abc.cloudfront.net"), config=ToolConfig(basic_secret_arn=arn),
                             http=FakeTransport(), goldens=goldens())
        assert isinstance(c, InProcessToolClient)
        self.assertIsInstance(c._tools["cohort_count"]._vlab._credential, SecretsManagerCredential)  # noqa: SLF001

    def test_tool_config_reads_its_environment(self) -> None:
        config = ToolConfig.from_env({
            "ORION_GATEWAY_URL": self.GATEWAY, "ORION_VLAB_ACCESS_FILE": "/run/access.json",
            "ORION_VLAB_BASIC_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:000000000000:secret:orion/vlab-basic", "ORION_KG_TARGET_REGISTERED": "1",
        })
        self.assertEqual((config.gateway_url, str(config.access_file), config.kg_registered), (self.GATEWAY, "/run/access.json", True))
        self.assertEqual(ToolConfig.from_env({}), ToolConfig())

    def test_the_in_process_client_is_a_tool_client(self) -> None:
        c = make_tool_client(Settings(), config=ToolConfig(), http=fake_stack(), goldens=goldens())
        outcome = run(c.call("cohort_count", {"filter": HIGH}, timeout_s=60, run_id=RUN_ID, plan_version=1, step_id="s2", first_call=1))
        self.assertEqual(json.loads(json.dumps(outcome.result.observed)), {"count": 7})


if __name__ == "__main__":
    unittest.main()
