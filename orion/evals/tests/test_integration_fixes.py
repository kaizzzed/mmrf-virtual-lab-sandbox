"""Harness fixes found when the harness first ran against the finished agent (mode L, 2026-11-05)."""

import os
import tempfile
import unittest
from pathlib import Path

from helpers import fixture_evidence, fixture_json

from orion_evals.checks import unexplained_digits
from orion_evals.cli import _records
from orion_evals.client import BffRecordSource, DirRecordSource, NoRecordSource
from orion_evals.evals import reuse_of_saved
from orion_evals.evidence import EvalContext
from orion_evals.frontend import BROWSER_CHECKS, coverage
from orion_evals.runner import environment


class RecordSourceTests(unittest.TestCase):
    def test_auto_reads_a_fresh_run_volume_that_has_no_runs_yet(self) -> None:
        """The agent writes runs/ on its first run, after the harness chose its record source."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(_records("auto", tmp, None), DirRecordSource)
            self.assertIsInstance(_records("auto", tmp, "http://frontend:3000"), BffRecordSource, "a BFF wins over an empty volume")
            Path(tmp, "runs").mkdir()
            self.assertIsInstance(_records("auto", tmp, "http://frontend:3000"), DirRecordSource)
        self.assertIsInstance(_records("auto", "/no/such/dir", None), NoRecordSource)


class SavedCohortReuseTests(unittest.TestCase):
    def test_compile_time_reuse_passes_and_a_fresh_save_does_not(self) -> None:
        """TOOLS.md choice 15: the oncomatrix fixture reuses the saved cohort at compile time (no save step, no pause)."""
        reused = fixture_evidence("oncomatrix", ("oncomatrix.events.jsonl", "ask"))
        view = reused.view or {}
        ids = {str(c.get("cohortId")) for c in view.get("cohorts") or [] if c.get("save") == "reused"}
        ok, detail = reuse_of_saved(view, reused.pauses(), ids)
        self.assertTrue(ok, detail)
        self.assertFalse(reuse_of_saved(view, reused.pauses(), set())[0], "a cohort reused from elsewhere is not the seeded one")
        fresh = fixture_evidence("j2", ("j2-save-pause.events.jsonl", "ask"))
        self.assertFalse(reuse_of_saved(fresh.view or {}, fresh.pauses(), ids)[0], "a pause that offers no reuse fails")


class EnvironmentTests(unittest.TestCase):
    def test_the_report_names_the_records_composer_role_and_its_served_models(self) -> None:
        """Contract v1.2 records environment.composer, so a run that never composed still names it."""
        record = fixture_json("run-record.j1-complete.json")
        ev = fixture_evidence("j1", ("j1-complete.events.jsonl", "ask"), record=record)
        env = environment(EvalContext(agent=None, fault_agents={}, oracle=None), [ev], None, "test")
        self.assertEqual((env["composer"], env["composerModels"]), ("bedrock", ["us.anthropic.claude-opus-5-5"]))
        record["environment"]["composer"] = None
        record["modelCalls"] = []
        bare = fixture_evidence("j1", ("j1-complete.events.jsonl", "ask"), record=record)
        self.assertEqual(environment(EvalContext(agent=None, fault_agents={}, oracle=None), [bare], None, "test")["composer"], "none recorded")


class DigitTokenTests(unittest.TestCase):
    def view(self, text: str) -> dict[str, object]:
        return {"observed": [{"segments": [{"text": text}, {"text": "7", "factId": "f.s5.count"}]}], "limitations": [], "facts": []}

    def test_a_panel_gene_in_a_hyphenated_label_is_not_a_number(self) -> None:
        """E9's Observed reads "TP53-mutated: 7" (TP53 is in the contract's allowed list); the old tokenizer kept the compound."""
        self.assertEqual(unexplained_digits(self.view("Mutation-tested: ; TP53-mutated: "), "TP53 mutation"), [])

    def test_unexplained_compounds_and_signed_numbers_still_fail(self) -> None:
        self.assertEqual([t["token"] for t in unexplained_digits(self.view("ABC12-mutated: "))], ["ABC12-mutated"])
        self.assertEqual([t["token"] for t in unexplained_digits(self.view("log2FC -1.78 here"))], ["1.78"])
        self.assertEqual([t["token"] for t in unexplained_digits(self.view("from 2024-2025"))], ["2024-2025"])


def fork_root() -> Path | None:
    for candidate in (os.environ.get("ORION_EVAL_FRONTEND_ROOT"), "/fork"):
        if candidate and Path(candidate, "src/features/orion").is_dir():
            return Path(candidate)
    return None


class BrowserCoverageTests(unittest.TestCase):
    @unittest.skipIf(fork_root() is None, "the fork is not mounted (ORION_EVAL_FRONTEND_ROOT or /fork)")
    def test_every_browser_only_check_finds_its_test_in_the_fork(self) -> None:
        """The candidates followed the fork's layout as built (cohort/saveCohorts, cards/LimitationCard,
        components/RunItem, viz/VizCard), so E3, E4, E11 and E24 are not reported not-run for a missing path."""
        for key in BROWSER_CHECKS:
            with self.subTest(key):
                check = coverage(key, fork_root())
                self.assertEqual(check.status, "covered", check.detail)


if __name__ == "__main__":
    unittest.main()
