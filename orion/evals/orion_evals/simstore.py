"""A deterministic stand-in for the browser's cohort store: the **simulated browser**.

The harness has no browser, so it does what the contract's "On Create" says the browser does,
in memory, and every report labels the result "simulated browser":

1. Recompute `defSha256` for every saved cohort and reuse one that matches a pending cohort.
2. Write a save ledger keyed by the nonce, then create each cohort still missing with the
   FilterSet the contract specifies and the proposed name made unique, and read back id and name.
   A resumed Create creates only cohorts missing from the ledger.
3. Recompute `defSha256` from each saved cohort's stored filters and build the `confirm` read-back.

Cohort ids are `sim-<n>`, assigned in creation order, so a scenario replays identically.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .canonical import def_sha256, filterset_def_sha256, filterset_from_definition

LABEL: Final = "simulated browser"
NAME_MAX: Final = 100

Counter = Callable[[Mapping[str, Any]], int | None]


@dataclass
class SimCohort:
    id: str
    name: str
    filters: dict[str, Any]
    case_count: int | None
    origin: str  # "seed", or "create:<nonce>"

    def context_entry(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "filters": self.filters, "caseCount": self.case_count}


@dataclass
class CreateOutcome:
    """What one Create click did: the read-back the browser sends, and what it wrote."""

    nonce: str
    saved: list[dict[str, Any]]
    created: list[str]
    reused: list[str]
    from_ledger: list[str]
    make_active: str | None
    definition_mismatches: list[str]


@dataclass
class SimulatedCohortStore:
    counter: Counter | None = None
    cohorts: list[SimCohort] = field(default_factory=list)
    active_id: str | None = None
    ledger: dict[str, dict[str, str]] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    _next: int = 0

    # --- the store ---------------------------------------------------------------------------

    def _new_id(self) -> str:
        self._next += 1
        return f"sim-{self._next:04d}"

    def by_id(self, cohort_id: str) -> SimCohort | None:
        return next((c for c in self.cohorts if c.id == cohort_id), None)

    def by_name(self, name: str) -> SimCohort | None:
        return next((c for c in self.cohorts if c.name == name), None)

    def seed(self, name: str, filters: Mapping[str, Any], case_count: int | None) -> SimCohort:
        """A cohort that exists before the scenario (saved earlier by the researcher)."""
        cohort = SimCohort(self._new_id(), name, dict(filters), case_count, "seed")
        self.cohorts.append(cohort)
        return cohort

    def set_active(self, cohort_id: str | None, origin: str = "scope chip") -> None:
        """`set_active_cohort`: a client write, logged like every other one."""
        if cohort_id is not None and self.by_id(cohort_id) is None:
            raise KeyError(cohort_id)
        self.active_id = cohort_id
        self.writes.append({"write": "set_active_cohort", "cohortId": cohort_id, "origin": origin})

    def cohort_context(self) -> dict[str, Any]:
        """The snapshot every `ask` and scope edit carries (`cohortContext`)."""
        active = self.by_id(self.active_id) if self.active_id else None
        if active is None:
            total = self.counter({"kind": "filter", "clauses": []}) if self.counter else None
            active_entry: dict[str, Any] = {"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": total}
        else:
            active_entry = active.context_entry()
        return {"active": active_entry, "saved": [c.context_entry() for c in self.cohorts]}

    def unique_name(self, proposed: str) -> str:
        """The proposed name, or the first free "<name> (n)": the Save Cohort modal refuses duplicates."""
        taken = {c.name.strip().lower() for c in self.cohorts}
        if proposed.strip().lower() not in taken:
            return proposed
        n = 2
        while True:
            suffix = f" ({n})"
            candidate = proposed[: NAME_MAX - len(suffix)] + suffix
            if candidate.strip().lower() not in taken:
                return candidate
            n += 1

    def matching(self, sha: str) -> SimCohort | None:
        return next((c for c in self.cohorts if filterset_def_sha256(c.filters) == sha), None)

    # --- Create --------------------------------------------------------------------------------

    def create(self, pending: Mapping[str, Any], make_active_ref: str | None = None) -> CreateOutcome:
        """The Create click on a confirmation card, given the `save_cohorts` args (every definition)."""
        nonce = str(pending["nonce"])
        entry = self.ledger.setdefault(nonce, {})
        created: list[str] = []
        reused: list[str] = []
        from_ledger: list[str] = []
        mismatches: list[str] = []
        chosen: dict[str, tuple[SimCohort, bool]] = {}
        for item in pending["cohorts"]:
            ref = str(item["ref"])
            definition = item.get("definition")
            if definition is None:
                raise ValueError(f"cohort {ref} has no definition: read it from the save_cohorts args or the view route")
            if def_sha256(definition) != item["defSha256"]:
                mismatches.append(ref)
            if ref in entry and (existing := self.by_id(entry[ref])) is not None:
                chosen[ref] = (existing, False)
                from_ledger.append(ref)
                continue
            match = self.matching(str(item["defSha256"]))
            if match is not None:
                entry[ref] = match.id
                chosen[ref] = (match, True)
                reused.append(ref)
                continue
            filters = filterset_from_definition(definition)
            count = self.counter(definition) if self.counter else item.get("caseCount")
            cohort = SimCohort(self._new_id(), self.unique_name(str(item["name"])), filters, count, f"create:{nonce}")
            self.cohorts.append(cohort)
            entry[ref] = cohort.id
            chosen[ref] = (cohort, False)
            created.append(ref)
            self.writes.append({"write": "createNewCohort", "nonce": nonce, "ref": ref, "cohortId": cohort.id, "name": cohort.name})
        if make_active_ref is not None:
            self.set_active(chosen[make_active_ref][0].id, origin=f"create:{nonce}")
        saved: list[dict[str, Any]] = []
        for item in pending["cohorts"]:
            chosen_cohort, was_reused = chosen[str(item["ref"])]
            read_back = filterset_def_sha256(chosen_cohort.filters)
            saved.append(
                {"ref": item["ref"], "cohortId": chosen_cohort.id, "name": chosen_cohort.name, "defSha256": read_back, "reused": was_reused}
            )
        return CreateOutcome(nonce, saved, created, reused, from_ledger, make_active_ref, mismatches)

    def created_by(self, nonce: str) -> list[SimCohort]:
        return [c for c in self.cohorts if c.origin == f"create:{nonce}"]

    def duplicates(self) -> list[list[str]]:
        """Groups of cohorts with the same defSha256 (FR-08: an identical cohort is reused, not duplicated)."""
        groups: dict[str, list[str]] = {}
        for cohort in self.cohorts:
            sha = filterset_def_sha256(cohort.filters)
            if sha is not None:
                groups.setdefault(sha, []).append(cohort.id)
        return [ids for ids in groups.values() if len(ids) > 1]

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": LABEL,
            "active": self.active_id,
            "cohorts": [{"id": c.id, "name": c.name, "caseCount": c.case_count, "origin": c.origin, "defSha256": filterset_def_sha256(c.filters)} for c in self.cohorts],
            "writes": list(self.writes),
        }
