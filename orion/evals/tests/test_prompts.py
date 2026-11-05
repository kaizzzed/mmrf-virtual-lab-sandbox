"""The eval prompts: shape, the fork's reference prompts, and the C-31 exclusion check."""

import os
import re
import tempfile
import unittest
from pathlib import Path

from orion_evals.cli import all_prompts, load_prompts, missing_from_agent
from orion_evals.evals import EVALS, FR_CHECKS

PROMPTS = load_prompts()


def fork_root() -> Path | None:
    for candidate in (os.environ.get("ORION_EVAL_FRONTEND_ROOT"), "/fork"):
        if candidate and Path(candidate, "src/features/orion/constants.ts").is_file():
            return Path(candidate)
    return None


class Prompts(unittest.TestCase):
    def test_shape(self) -> None:
        self.assertEqual(sorted(PROMPTS["reference"]), ["J1", "J2", "J3"])
        for journey in PROMPTS["reference"].values():
            self.assertEqual(sorted(journey), ["open-ended", "tool-named"])
        self.assertEqual({j: len(v) for j, v in PROMPTS["heldOut"].items()}, {"J1": 3, "J2": 3, "J3": 3})
        self.assertEqual(len(PROMPTS["outOfKind"]), 3)
        texts = [" ".join(q.casefold().split()) for q in all_prompts(PROMPTS)]
        self.assertEqual(len(texts), len(set(texts)))

    def test_the_e9_question_matches_its_oracle_inputs(self) -> None:
        question = PROMPTS["edge"]["E9"]
        inputs = PROMPTS["oracleInputs"]["E9"]
        self.assertIn(inputs["gene"], question)
        for clause in inputs["scope"]:
            self.assertIn(clause["values"][0].casefold(), question.casefold())

    @unittest.skipIf(fork_root() is None, "the fork is not mounted (ORION_EVAL_FRONTEND_ROOT or /fork)")
    def test_reference_prompts_equal_the_forks_starters(self) -> None:
        root = fork_root()
        assert root is not None
        text = (root / "src/features/orion/constants.ts").read_text(encoding="utf-8")
        found = re.findall(r"journey:\s*'(J\d)',\s*phrasing:\s*'([a-z-]+)',\s*text:\s*'([^']+)'", text)
        self.assertEqual(len(found), 6)
        for journey, phrasing, prompt in found:
            self.assertEqual(PROMPTS["reference"][journey][phrasing], prompt)

    def test_missing_from_agent_reads_concatenated_literals_without_importing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eval_prompts.py"
            path.write_text(
                "raise SystemExit('never imported')\n"
                "EVAL_PROMPTS = (EvalPrompt('Compare survival of CGS high-risk vs. CGS standard-risk patients', 'x'),\n"
                "    EvalPrompt('Use Cohort Builder to build a cohort of Black or African American patients, then show its top 3 most frequently '\n"
                "               'mutated genes in OncoMatrix', 'y'))\n"
            )
            missing = missing_from_agent(PROMPTS, path)
        self.assertNotIn(PROMPTS["reference"]["J1"]["open-ended"], missing)
        self.assertNotIn(PROMPTS["reference"]["J2"]["tool-named"], missing)
        self.assertIn(PROMPTS["heldOut"]["J1"][0], missing)
        self.assertEqual(len(missing), len(all_prompts(PROMPTS)) - 2)


def agent_prompts_file() -> Path | None:
    root = os.environ.get("ORION_AGENT_SRC")
    path = Path(root, "orion_agent/model/eval_prompts.py") if root else None
    return path if path is not None and path.is_file() else None


class AgentExclusion(unittest.TestCase):
    @unittest.skipIf(agent_prompts_file() is None, "the agent source is not mounted (ORION_AGENT_SRC)")
    def test_the_agent_excludes_every_eval_prompt_from_its_examples(self) -> None:
        """C-31: the planner's exclusion list (orion_agent/model/eval_prompts.py) holds every prompt the evals ask."""
        path = agent_prompts_file()
        assert path is not None
        self.assertEqual(missing_from_agent(PROMPTS, path), [])


class Registry(unittest.TestCase):
    def test_every_eval_quotes_its_row_and_its_frs(self) -> None:
        self.assertEqual([s.id for s in EVALS], [f"E{i}" for i in range(1, 25)])
        for spec in EVALS:
            with self.subTest(spec.id):
                self.assertTrue(spec.passes_when)
                self.assertTrue(spec.fr)
                for fr in spec.fr:
                    self.assertIn(fr, FR_CHECKS)
        self.assertEqual([s.id for s in EVALS if s.stretch], ["E13"])


if __name__ == "__main__":
    unittest.main()
