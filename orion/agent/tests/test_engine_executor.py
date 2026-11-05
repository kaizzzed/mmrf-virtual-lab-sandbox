"""The engine's runtime guards, budget, stop grace and pure functions (TOOLS.md 1-3; RFA A3 "Step lifecycle")."""

import asyncio
import unittest
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from support import GatedExecutor, action_body, ask_body, asgi_call, check_stream, context
from test_engine_fakes import ALL, J3_QUESTION, FakeTools, compile_plan, j1_ir, j3_ir
from test_engine_streams import EngineCase

from orion_agent import controller as controller_module
from orion_agent.app import create_app
from orion_agent.controller import Controller
from orion_agent.executor import (
    Engine,
    check_subset,
    derive_km_median,
    input_hashes,
    passes_thresholds,
    plan_cohorts,
    rank_top_n,
    reuse_map,
    set_operation,
    verify_read_back,
)
from orion_agent.ids import new_id
from orion_agent.models import ConfirmAction, ConfirmPending, PendingCohort, CaseIdsDefinition, def_sha256
from orion_agent.run_store import RunStore
from orion_agent.settings import Settings
from orion_agent.tools.base import KmCurve, KmPoint


class GuardTests(EngineCase):
    async def test_a_small_survival_group_stops_before_the_survival_call(self) -> None:
        _, app = self.build(tools=FakeTools({"s2": "small"}))
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "INELIGIBLE_GROUP"))
        self.assertEqual([(s.id, s.status, s.code) for s in final.steps[3:]], [("s4", "Skipped", "INELIGIBLE_GROUP"), ("s5", "Skipped", None)])
        self.assertNotIn("survival_compare", [c[0] for c in self.tools.calls])
        line = next(lim for lim in final.limitations if lim.code == "INELIGIBLE_GROUP")
        self.assertEqual(line.text, "Too few cases to compare survival: High risk has 5 cases, and each group needs at least 10.")
        assert line.segments is not None
        self.assertEqual([s.fact_id for s in line.segments if s.fact_id], ["f.s2.count"])
        self.assertEqual(final.withheld_reason, "a limitation stopped the run")

    async def test_a_zero_count_is_reported_with_its_filter_and_both_causes(self) -> None:
        """C-40: the zero is a fact, Observed states it with the filter used, the limitation names both causes."""
        _, app = self.build(tools=FakeTools({"s2": "empty"}))
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "EMPTY_COHORT"))
        self.assertEqual(final.steps[1].status, "Empty")
        self.assertEqual(final.steps[3].output_summary, "Not run: s2 found no cases")
        texts = ["".join(s.text for s in o.segments) for o in final.observed]
        self.assertEqual(texts[0], "High risk: 0 cases match CGS risk category = High risk.")
        self.assertEqual(final.observed[0].segments[1].fact_id, "f.s2.count")
        empty = next(lim for lim in final.limitations if lim.code == "EMPTY_COHORT")
        self.assertIn("either no case has these values, or the API returned an empty set", empty.text)

    async def test_too_few_eligible_samples_stop_the_run_before_any_write(self) -> None:
        _, app = self.build([j3_ir()], tools=FakeTools({"de_preanalysis": "ineligible"}))
        frames = await self.turn(app, ask_body(new_id(), new_id(), question=J3_QUESTION))
        self.assertNotIn("TOOL_CALL_START", [f["type"] for f in frames])
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "INELIGIBLE_GROUP"))
        self.assertEqual([s.status for s in final.steps[6:]], ["Done", "Skipped", "Skipped", "Skipped"])
        line = next(lim for lim in final.limitations if lim.code == "INELIGIBLE_GROUP")
        assert line.segments is not None
        self.assertEqual([s.fact_id for s in line.segments if s.fact_id], ["f.s7.eligible.control", "f.s7.eligible.case"])

    async def test_more_than_ten_thousand_ids_stops_the_steps_that_need_the_list(self) -> None:
        _, app = self.build([j3_ir()], tools=FakeTools({"s3": "too_many"}))
        frames = await self.turn(app, ask_body(new_id(), new_id(), question=J3_QUESTION))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "TOO_MANY_IDS"))
        self.assertEqual((final.steps[2].status, final.steps[2].code), ("Done", "TOO_MANY_IDS"))
        self.assertIn("TOO_MANY_IDS", [lim.code for lim in final.limitations])

    async def test_a_tool_error_fails_the_step_and_the_run_ends_partial(self) -> None:
        _, app = self.build(tools=FakeTools({"s2": "error"}))
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Partial", "TOOL_FAILED"))
        self.assertEqual(final.steps[1].output_summary, "Returned an error")
        self.assertEqual(final.limitations[0].text, "The case count returned an error, so this group's count is missing.")

    async def test_the_tool_clients_call_cap_is_explained_under_limitations(self) -> None:
        _, app = self.build(tools=FakeTools({"s4": "budget"}))
        frames = await self.turn(app, ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "CALL_BUDGET"))
        self.assertEqual((final.steps[3].status, final.steps[3].code), ("Skipped", "CALL_BUDGET"))
        self.assertEqual([lim.code for lim in final.limitations].count("CALL_BUDGET"), 1, "one line says why the run stopped")

    async def test_the_call_budget_skips_the_rest(self) -> None:
        settings = Settings(run_dir=self.run_dir, fields_path=self.fields())
        store = RunStore(self.run_dir)
        from test_engine_fakes import FakeComposer, FakePlanner, FakeVerifier, compiler
        from support import vocabulary

        controller = Controller(settings, store, compiler(), FakePlanner([j1_ir()]), Engine(FakeTools(), vocab=vocabulary(), call_budget=3),
                                composer=FakeComposer(), verifier=FakeVerifier())
        frames = await self.turn(create_app(controller), ask_body(new_id(), new_id()))
        final = self.last_view(frames)
        self.assertEqual((final.status, final.code), ("Limited", "CALL_BUDGET"))
        self.assertEqual([(s.status, s.code) for s in final.steps[3:]], [("Skipped", "CALL_BUDGET"), ("Skipped", None)])
        self.assertEqual(final.limitations[-1].text, "This run reached its limit of 3 tool calls, so the remaining steps did not run.")

    async def test_the_controller_cancels_an_executor_that_ignores_stop(self) -> None:
        executor = GatedExecutor()
        settings = Settings(run_dir=self.run_dir, fields_path=self.fields(), stop_grace_s=0.1)
        from test_engine_fakes import FakePlanner, compiler

        controller = Controller(settings, RunStore(self.run_dir), compiler(), FakePlanner([j1_ir()]), executor)
        app = create_app(controller)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(executor.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        check_stream(self, (await ask).text, expect_run_id=run_id)
        final = self.last_view(frames)
        self.assertEqual(final.status, "Cancelled")
        self.assertTrue(all(s.status == "Skipped" and s.output_summary == "Not run: stopped" for s in final.steps))
        self.assertNotIn(run_id, controller_module.RUNS)

    def fields(self) -> Path:
        from support import FIELDS_PATH

        return FIELDS_PATH


class SubsetTests(EngineCase):
    async def test_mutated_cases_outside_the_tested_ones_are_dropped_and_reported(self) -> None:
        tools = FakeTools()
        original = tools.observed

        def observed(name: str, input: JsonValue, behaviour: Any) -> tuple[JsonValue, Any, Any]:  # Any: the fake's own types
            data, status, limitation = original(name, input, behaviour)
            if name == "cases_query" and isinstance(data, dict):
                ids: list[JsonValue] = [i for i in ALL[5:]]
                data = {"count": len(ids), "caseIds": ids, "pages": 1, "complete": True}
            return data, status, limitation

        tools.observed = observed  # type: ignore[method-assign]
        _, app = self.build([j3_ir()], tools=tools)
        frames = await self.turn(app, ask_body(new_id(), new_id(), question=J3_QUESTION))
        view = self.last_view(frames)
        self.assertEqual(view.status, "AwaitingConfirmation")
        self.assertEqual((view.steps[3].code, view.steps[3].output_summary), ("NOT_SUBSET", "5 of 480 outside the mutation-tested cases"))
        assert isinstance(view.pending, ConfirmPending)
        mutated = next(c for c in view.pending.cohorts if c.ref == "kras_mut")
        self.assertEqual(mutated.case_count, 475, "the plan uses only the cases inside")
        sample_request: Any = next(c[2] for c in tools.calls if c[0] == "sample_lists")  # Any: walked as JSON
        self.assertEqual(len(sample_request["groups"][1]["caseIds"]), 475)


class PureFunctionTests(unittest.TestCase):
    def test_set_operations(self) -> None:
        self.assertEqual(set_operation("difference", ["c", "a", "b"], ["b"]).case_ids, ["a", "c"])
        self.assertEqual(set_operation("intersection", ["a", "b"], ["b", "c"]).count, 1)
        self.assertEqual(set_operation("union", ["a"], ["b"]).case_ids, ["a", "b"])
        size = set_operation("size", ["a", "b", "b"])
        self.assertEqual((size.count, size.case_ids, size.b_count), (2, [], None))

    def test_check_subset_lists_the_first_outside_ids(self) -> None:
        result = check_subset([f"x{i:02d}" for i in range(15)], [])
        self.assertEqual((result.is_subset, result.outside_count, len(result.outside_sample)), (False, 15, 10))
        self.assertTrue(check_subset(["a"], ["a", "b"]).is_subset)

    def test_rank_top_n_keeps_ties_at_the_cutoff_and_breaks_ties_by_code_point(self) -> None:
        rows: list[dict[str, JsonValue]] = [{"gene": g, "cases": c} for g, c in (("B", 5), ("A", 5), ("C", 3), ("D", 3), ("E", 1))]
        result = rank_top_n(rows, key="gene", rank_by="cases", n=3, ascending=False, tie_break="gene")
        self.assertEqual([(r.rank, r.key) for r in result.rows], [(1, "A"), (1, "B"), (3, "C"), (3, "D")])
        self.assertEqual((result.ties_at_cutoff, result.count, result.total_rows), (["D"], 4, 5))
        fold = rank_top_n([{"gene_name": "x", "fold_change": -2.0}, {"gene_name": "y", "fold_change": 1.0}], key="gene_name",
                          rank_by="abs(fold_change)", n=1, ascending=False, tie_break="gene_name")
        self.assertEqual([r.key for r in fold.rows], ["x"])

    def test_thresholds_filter_de_rows(self) -> None:
        limits: dict[str, JsonValue] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}
        self.assertTrue(passes_thresholds({"adjusted_p_value": 0.01, "fold_change": -0.5}, limits))
        self.assertFalse(passes_thresholds({"adjusted_p_value": 0.01, "fold_change": 0.2}, limits))
        self.assertFalse(passes_thresholds({"adjusted_p_value": 0.2, "fold_change": 2.0}, limits))

    def test_km_median_is_the_first_time_at_or_below_half(self) -> None:
        reached = KmCurve(label="a", points=[KmPoint(time=0, survival=1), KmPoint(time=10, survival=0.5), KmPoint(time=20, survival=0.2)])
        never = KmCurve(label="b", points=[KmPoint(time=0, survival=1), KmPoint(time=10, survival=0.7)])
        result = derive_km_median([reached, never], unit="days")
        self.assertEqual([(g.median, g.reached) for g in result.groups], [(10, True), (None, False)])


class PlanHelperTests(unittest.TestCase):
    def test_a_rename_changes_only_the_write_and_what_follows_it(self) -> None:
        plan = compile_plan(j3_ir())
        renamed = plan.model_copy(deep=True)
        for step in renamed.steps:
            for key in ("cohorts", "groups"):
                items = step.args.get(key)
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict) and item.get("name") == "KRAS not detected (ORION)":
                        item["name"] = "KRAS wild-type (ORION)"
        before, after = input_hashes(plan), input_hashes(renamed)
        self.assertEqual([s for s in before if before[s] != after[s]], ["s8", "s9", "s10"])
        views = [s.queued().model_copy(update={"status": "Done"}) for s in plan.steps[:7]] + [s.queued() for s in plan.steps[7:]]
        self.assertEqual(sorted(reuse_map(renamed.model_copy(update={"version": 2}), plan, views)), ["s1", "s2", "s3", "s4", "s5", "s6", "s7"])

    def test_plan_cohorts_follow_the_compiler(self) -> None:
        planned = plan_cohorts(compile_plan(j3_ir()), context())
        self.assertEqual([c.ref for c in planned.cohorts], ["base", "tested", "kras_mut", "kras_wt"])
        self.assertEqual(planned.steps, {"base": "s1", "tested": "s3", "kras_mut": "s2", "kras_wt": "s5"})
        self.assertEqual(planned.ids["kras_mut"].intersect, "s3")
        self.assertIsNotNone(planned.cohorts[1].def_sha256, "a filter cohort shows its hash at plan time")

    def test_read_back_checks(self) -> None:
        ids = CaseIdsDefinition(kind="case_ids", case_ids=["SYNTH-CASE-0001"])
        cohort = PendingCohort(ref="a", name="A (ORION)", definition_text="t", definition=ids, def_sha256=def_sha256(ids), case_count=1,
                               eligible_count=None, reuse=None)
        pending = ConfirmPending(kind="confirm", nonce=new_id(), plan_signature="0" * 64, plan_version=1, step_id="s5", cohorts=[cohort])

        def confirm(sha: str) -> ConfirmAction:
            return ConfirmAction.model_validate({"action": "confirm", "nonce": pending.nonce, "decision": "create", "makeActive": None,
                                                 "saved": [{"ref": "a", "cohortId": "id-1", "name": "A (ORION)", "defSha256": sha, "reused": False}]})

        self.assertEqual(verify_read_back(pending, confirm(cohort.def_sha256)), [])
        self.assertEqual(verify_read_back(pending, confirm("f" * 64)), ["A (ORION) was saved with a different definition"])
