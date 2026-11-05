"""Browser-only checks: the harness has no browser, so it records which frontend test covers each.

A check is "covered by frontend test <file>" when that file exists in the fork (and, when a test
name pattern is given, the file contains it); otherwise "not covered". The harness does not run
the frontend tests: they run with jest in the fork (see the README).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .checks import Check

ORION_SRC: Final = "src/features/orion"


@dataclass(frozen=True)
class BrowserCheck:
    name: str
    files: tuple[str, ...]  # candidates, relative to the fork root, first match wins
    pattern: str | None = None  # a regex the test file must contain (a test name)


BROWSER_CHECKS: Final[dict[str, BrowserCheck]] = {
    "viewer_mounts": BrowserCheck(
        "the viewer mounts in the card",
        (f"{ORION_SRC}/viz/OrionPpCard.unit.test.tsx", f"{ORION_SRC}/viz/liveViz.unit.test.ts"),
    ),
    "opens_in_cohort_builder": BrowserCheck(
        "a chat-made cohort opens in Cohort Builder",
        (f"{ORION_SRC}/cohort/useCohortContext.unit.test.ts", f"{ORION_SRC}/cohort/cohortAdapter.unit.test.ts"),
        r"Cohort Builder|createNewCohort|set_active_cohort|setCurrentCohort",
    ),
    "enter_saves_nothing": BrowserCheck(
        "Enter in the composer as the confirmation card appears saves nothing",
        (f"{ORION_SRC}/cards/ConfirmationCard.unit.test.tsx",),
        r"Enter never saves|\{Enter\}",
    ),
    "unsupported_offers_open": BrowserCheck(
        "the Unsupported card offers Open on this cohort",
        (f"{ORION_SRC}/AssistantApp.unit.test.tsx", f"{ORION_SRC}/cards/LimitationCard.unit.test.tsx"),
        r"nearest app|Open on this cohort",
    ),
    "could_not_plan_card": BrowserCheck(
        "a planner refusal shows the Could not plan card",
        (f"{ORION_SRC}/cards/FailedCard.unit.test.tsx", f"{ORION_SRC}/AssistantApp.unit.test.tsx"),
        r"Could not plan",
    ),
    "stopped_by_you": BrowserCheck(
        "a cancelled run reads Stopped by you",
        (f"{ORION_SRC}/status.unit.test.ts", f"{ORION_SRC}/AssistantApp.unit.test.tsx"),
        r"Stopped by you",
    ),
    "handoff_disabled_when_skipped": BrowserCheck(
        "Don't save disables Open with this cohort",
        (f"{ORION_SRC}/viz/OrionPpCard.unit.test.tsx", f"{ORION_SRC}/cards/AnswerCard.unit.test.tsx"),
        r"WRITE_SKIPPED|Don't save|Don\\'t save",
    ),
    "thread_restore": BrowserCheck(
        "a reopened thread restores its runs from the view route",
        (f"{ORION_SRC}/AssistantApp.unit.test.tsx", f"{ORION_SRC}/state/storage.unit.test.ts"),
        r"restores the thread|view route",
    ),
}


def coverage(key: str, frontend_root: Path | None) -> Check:
    spec = BROWSER_CHECKS[key]
    if frontend_root is None:
        return Check(spec.name, "not-covered", "no frontend fork mounted (--frontend-root)", {"candidates": list(spec.files)}, "browser")
    for relative in spec.files:
        path = frontend_root / relative
        if not path.is_file():
            continue
        if spec.pattern is not None and not re.search(spec.pattern, path.read_text(encoding="utf-8", errors="replace")):
            continue
        return Check(spec.name, "covered", f"covered by frontend test {relative}", {"file": relative, "pattern": spec.pattern}, "browser")
    return Check(spec.name, "not-covered", "not covered: no frontend test file found", {"candidates": list(spec.files), "pattern": spec.pattern}, "browser")


def coverage_all(keys: Sequence[str], frontend_root: Path | None) -> list[Check]:
    return [coverage(key, frontend_root) for key in keys]


def agent_test_coverage(name: str, agent_tests_root: Path | None, pattern: str) -> Check:
    """A lease-expiry case the harness cannot drive live is still covered when an agent test names it."""
    if agent_tests_root is None:
        return Check(name, "not-covered", "no agent tests mounted (--agent-tests-root)", {}, "harness")
    for path in sorted(agent_tests_root.glob("test_*.py")):
        if re.search(pattern, path.read_text(encoding="utf-8", errors="replace")):
            return Check(name, "covered", f"covered by agent test {path.name}", {"file": path.name, "pattern": pattern}, "harness")
    return Check(name, "not-covered", "not covered: no agent test matches", {"pattern": pattern}, "harness")
