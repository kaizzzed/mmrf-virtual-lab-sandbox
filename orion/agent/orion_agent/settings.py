"""Runtime settings, read once from the environment."""

import os
from pathlib import Path
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tools.base import ServerToolName

DefaultScope = Literal["active_cohort", "all_cases"]
Mode = Literal["L", "A", "H"]

# Fault injection for E11 (contract "Fault injection"): ORION_FAULTS="de_run:timeout,planner:refuse".
# A tool fault replaces that tool's HTTP call; `refuse` makes the model role return a refusal from
# both models. Honoured only in mode L or a test Runtime (ORION_TEST_RUNTIME=1), and recorded in
# the run record's environment.faults.
FaultTarget = ServerToolName | Literal["planner", "composer"]
FaultKind = Literal["timeout", "error", "empty", "cache_busy", "refuse"]
MODEL_FAULTS: Final[frozenset[str]] = frozenset({"refuse"})
_TARGETS: Final[frozenset[str]] = frozenset(get_args(ServerToolName)) | {"planner", "composer"}
_KINDS: Final[frozenset[str]] = frozenset(get_args(FaultKind))


def parse_faults(text: str) -> dict[str, str]:
    """`<target>:<kind>` pairs, comma-separated. Model roles take only `refuse`; tools take the rest."""
    faults: dict[str, str] = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        target, _, kind = item.partition(":")
        if target not in _TARGETS or kind not in _KINDS:
            raise ValueError(f"unknown fault {item!r}")
        if (target in ("planner", "composer")) != (kind in MODEL_FAULTS):
            raise ValueError(f"{target} cannot take the fault {kind!r}")
        if target in faults:
            raise ValueError(f"{target} has two faults")
        faults[target] = kind
    return faults


def fault_list(faults: dict[str, str]) -> list[str]:
    """The faults as the record's environment lists them: sorted `<target>:<kind>` strings."""
    return sorted(f"{target}:{kind}" for target, kind in faults.items())


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: Path = Path("/data/runs")
    fields_path: Path = Path("/config/cohort-fields.json")
    planner: Literal["stub"] = "stub"
    executor: Literal["stub"] = "stub"
    default_scope: DefaultScope = "active_cohort"
    vlab_origin: str = "http://identity:8080"
    heartbeat_s: float = Field(default=15.0, gt=0)
    lease_renew_s: float = Field(default=30.0, gt=0)
    lease_ttl_s: float = Field(default=180.0, gt=0)
    port: int = 8080
    mode: Mode = "L"
    test_runtime: bool = False
    faults: dict[str, str] = Field(default_factory=dict)

    @field_validator("faults", mode="before")
    @classmethod
    def _parse_faults(cls, value: object) -> object:
        return parse_faults(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _faults_only_in_test_runs(self) -> "Settings":
        if self.faults:
            parse_faults(",".join(fault_list(self.faults)))
            if self.mode != "L" and not self.test_runtime:
                raise ValueError("ORION_FAULTS is honoured only in mode L or with ORION_TEST_RUNTIME=1")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        values: dict[str, object] = {}
        mapping = {
            "ORION_RUN_DIR": "run_dir",
            "ORION_FIELDS_PATH": "fields_path",
            "ORION_PLANNER": "planner",
            "ORION_EXECUTOR": "executor",
            "ORION_DEFAULT_SCOPE": "default_scope",
            "ORION_VLAB_ORIGIN": "vlab_origin",
            "ORION_HEARTBEAT_S": "heartbeat_s",
            "ORION_LEASE_RENEW_S": "lease_renew_s",
            "ORION_LEASE_TTL_S": "lease_ttl_s",
            "ORION_MODE": "mode",
            "ORION_TEST_RUNTIME": "test_runtime",
            "ORION_FAULTS": "faults",
        }
        for name, field in mapping.items():
            if env.get(name):
                values[field] = env[name]
        return cls.model_validate(values)
