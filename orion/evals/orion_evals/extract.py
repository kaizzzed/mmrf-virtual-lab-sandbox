"""Read each journey's numbers off a view, the way the researcher reads them: from fact chips.

Every value returned carries the fact id it came from, so a report can cite it. Steps are found by
tool (TOOLS.md section 5), facts by the catalog's fact grammar (`f.<step>.<suffix>`).
"""

from collections.abc import Mapping
from typing import Any

from .checks import JsonObj, facts_by_id, steps_with_tool


def _fact(view: Mapping[str, Any], fact_id: str | None) -> JsonObj | None:
    return None if fact_id is None else facts_by_id(view).get(fact_id)


def _value(view: Mapping[str, Any], fact_id: str | None) -> Any:
    fact = _fact(view, fact_id)
    return None if fact is None else fact["value"]


def _step_fact(view: Mapping[str, Any], tool: str, suffix: str, index: int = -1) -> tuple[str | None, Any]:
    steps = steps_with_tool(view, tool)
    if not steps:
        return None, None
    fact_id = f"f.{steps[index]['id']}.{suffix}"
    fact = _fact(view, fact_id)
    return (fact_id, fact["value"]) if fact else (None, None)


def scope(view: Mapping[str, Any]) -> JsonObj:
    """The scope line and the base cohort's sized count."""
    plan = view.get("plan") or {}
    plan_scope = plan.get("scope") or {}
    base: JsonObj = next((c for c in view.get("cohorts", []) if c.get("ref") == "base"), {})
    fact_id = base.get("caseCountFactId")
    return {
        "source": plan_scope.get("source"),
        "name": plan_scope.get("name"),
        "caseCount": plan_scope.get("caseCount"),
        "baseFact": fact_id,
        "baseCount": _value(view, fact_id),
        "line": f"Scope: {plan_scope.get('name')} · {plan_scope.get('caseCount') if plan_scope.get('caseCount') is not None else 'count pending'} cases",
        "altScope": plan.get("altScope"),
        "altScopeChoice": plan.get("altScopeChoice"),
    }


# --- J1 ----------------------------------------------------------------------------------------


def j1(view: Mapping[str, Any]) -> JsonObj:
    """Groups (label, cases, events, median) and the log-rank p, from the survival card's frozen
    table when there is one, else from the `survival.group` Observed sentences."""
    groups: list[JsonObj] = []
    card = next((s for s in view.get("vizSpecs", []) if s.get("launcher") == "survival"), None)
    frozen = None if card is None else card.get("frozen")
    if frozen:
        for row in frozen.get("rows", []):
            cells = list(row) + [{}] * 4
            groups.append(
                {
                    "label": cells[0].get("text"),
                    "casesFact": cells[1].get("factId"),
                    "eventsFact": cells[2].get("factId"),
                    "medianFact": cells[3].get("factId"),
                }
            )
    else:
        for sentence in view.get("observed", []):
            segments = sentence.get("segments", [])
            chips = [s for s in segments if "factId" in s]
            if segments and str(segments[0].get("text", "")).endswith(": ") and chips and ".count" in str(chips[0]["factId"]):
                groups.append(
                    {
                        "label": str(segments[0]["text"])[:-2],
                        "casesFact": chips[0]["factId"],
                        "eventsFact": chips[1]["factId"] if len(chips) > 1 else None,
                        "medianFact": None,
                    }
                )
    for group in groups:
        group["cases"] = _value(view, group["casesFact"])
        group["events"] = _value(view, group["eventsFact"])
        group["medianDays"] = _value(view, group["medianFact"])
    p_fact, p_value = _step_fact(view, "survival_compare", "p")
    return {"groups": groups, "pFact": p_fact, "p": p_value, "card": None if card is None else card.get("vizId")}


# --- J2 ----------------------------------------------------------------------------------------


def j2(view: Mapping[str, Any]) -> JsonObj:
    """The cohort size, the denominator, the ranking and the disclosed ties."""
    cohort_fact = denominator_fact = None
    for sentence in view.get("observed", []):
        segments = sentence.get("segments", [])
        text = "".join(str(s.get("text", "")) for s in segments)
        if text.startswith("The cohort has ") and any("factId" in s for s in segments):
            cohort_fact = next(s["factId"] for s in segments if "factId" in s)
        if text.startswith("The most frequently mutated gene"):
            for previous, segment in zip(segments, segments[1:], strict=False):
                if "factId" in segment and str(previous.get("text", "")).endswith(" of "):
                    denominator_fact = segment["factId"]
                    break
    ranking: list[JsonObj] = []
    rank_steps = steps_with_tool(view, "rank_top_n")
    if rank_steps:
        sid = rank_steps[-1]["id"]
        pos = 1
        while (gene := _fact(view, f"f.{sid}.top.{pos}.gene")) is not None:
            cases_id = f"f.{sid}.top.{pos}.cases"
            ranking.append({"gene": gene["value"], "cases": _value(view, cases_id), "geneFact": gene["id"], "casesFact": cases_id})
            pos += 1
    ties = next((lim for lim in view.get("limitations", []) if lim.get("code") == "TIES_AT_CUTOFF"), None)
    tied_genes: list[str] = []
    if ties:
        tied_genes = [str(_value(view, s["factId"])) for s in ties.get("segments") or [] if "factId" in s and str(s["factId"]).endswith(".gene")]
    card = next((s for s in view.get("vizSpecs", []) if s.get("launcher") == "oncomatrix"), None)
    return {
        "cohortFact": cohort_fact,
        "cohortSize": _value(view, cohort_fact),
        "denominatorFact": denominator_fact,
        "denominator": _value(view, denominator_fact),
        "ranking": ranking,
        "tiesLimitation": ties is not None,
        "tiedGenes": tied_genes,
        "cardGenes": None if card is None or card.get("pinned") is None else card["pinned"].get("genes"),
        "card": None if card is None else card.get("vizId"),
    }


# --- J3 ----------------------------------------------------------------------------------------


def _cohort_fact(view: Mapping[str, Any], ref: str) -> str | None:
    cohort = next((c for c in view.get("cohorts", []) if c.get("ref") == ref), None)
    return None if cohort is None else cohort.get("caseCountFactId")


def j3_card(pause_view: Mapping[str, Any] | None) -> JsonObj:
    """The confirmation card at the save pause: each cohort's count and eligible count, control first."""
    pending = None if pause_view is None else pause_view.get("pending")
    if not isinstance(pending, Mapping) or pending.get("kind") != "confirm":
        return {"present": False, "cohorts": []}
    cohorts = []
    for item in pending.get("cohorts", []):
        cohorts.append(
            {
                "ref": item.get("ref"),
                "name": item.get("name"),
                "caseCount": item.get("caseCount"),
                "caseCountFactId": item.get("caseCountFactId"),
                "caseCountFact": _value(pause_view or {}, item.get("caseCountFactId")),
                "eligibleCount": item.get("eligibleCount"),
                "eligibleCountFactId": item.get("eligibleCountFactId"),
                "eligibleCountFact": _value(pause_view or {}, item.get("eligibleCountFactId")),
                "reuse": item.get("reuse"),
            }
        )
    return {"present": True, "nonce": pending.get("nonce"), "cohorts": cohorts}


def j3(view: Mapping[str, Any]) -> JsonObj:
    tested_fact = _cohort_fact(view, "tested")
    if tested_fact is None:
        tested_fact, _ = _step_fact(view, "cases_query", "count")
    mutated_fact, _ = _step_fact(view, "gene_cases", "count")
    outside_fact, _ = _step_fact(view, "check_subset", "outside")
    difference_fact, _ = _step_fact(view, "set_operation", "count")
    out: JsonObj = {
        "tested": _value(view, tested_fact),
        "testedFact": tested_fact,
        "mutated": _value(view, mutated_fact),
        "mutatedFact": mutated_fact,
        "outside": _value(view, outside_fact),
        "outsideFact": outside_fact,
        "notDetected": _value(view, difference_fact),
        "notDetectedFact": difference_fact,
    }
    for group in ("control", "case"):
        sid, value = _step_fact(view, "sample_lists", f"samples.{group}")
        out[f"samples.{group}"], out[f"samples.{group}Fact"] = value, sid
        sid, value = _step_fact(view, "de_preanalysis", f"eligible.{group}")
        out[f"eligible.{group}"], out[f"eligible.{group}Fact"] = value, sid
    for name in ("tested", "significant"):
        sid, value = _step_fact(view, "de_run", name)
        out[f"de.{name}"], out[f"de.{name}Fact"] = value, sid
    top: list[JsonObj] = []
    rank_steps = steps_with_tool(view, "rank_top_n")
    if rank_steps:
        sid = rank_steps[-1]["id"]
        pos = 1
        while (gene := _fact(view, f"f.{sid}.top.{pos}.gene")) is not None:
            top.append(
                {
                    "gene": gene["value"],
                    "log2fc": _value(view, f"f.{sid}.top.{pos}.log2fc"),
                    "padj": _value(view, f"f.{sid}.top.{pos}.padj"),
                    "geneFact": gene["id"],
                }
            )
            pos += 1
    out["top"] = top
    out["card"] = next((s.get("vizId") for s in view.get("vizSpecs", []) if s.get("appId") == "DE"), None)
    return out
