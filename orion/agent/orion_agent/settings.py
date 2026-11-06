"""Runtime settings, read once from the environment (orion/agent/README.md lists every variable).

ORION_MODE picks where records live (L: ORION_RUN_DIR; A and H: S3), ORION_PLANNER and
ORION_COMPOSER pick the model roles (stub or bedrock), ORION_EXECUTOR the step engine (engine, or
the Day 1 stub) and ORION_TOOLS how server tools are reached (in-process, or AgentCore Gateway).
"""

import os
from pathlib import Path
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tools.base import ServerToolName

DefaultScope = Literal["active_cohort", "all_cases"]
Mode = Literal["L", "A", "H"]
ModelChoice = Literal["stub", "bedrock"]
ExecutorChoice = Literal["stub", "engine"]
ToolsChoice = Literal["inprocess", "gateway"]
DeLauncher = Literal["deDirect", "deInput"]

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
    planner: ModelChoice = "stub"
    composer: ModelChoice = "stub"
    executor: ExecutorChoice = "engine"
    tools: ToolsChoice = "inprocess"
    default_scope: DefaultScope = "active_cohort"
    vlab_origin: str = "http://identity:8080"
    heartbeat_s: float = Field(default=15.0, gt=0)
    lease_renew_s: float = Field(default=30.0, gt=0)
    lease_ttl_s: float = Field(default=180.0, gt=0)
    # The executor has this long to abort its in-flight call after Stop before the controller cancels it.
    stop_grace_s: float = Field(default=5.0, gt=0)
    # A warmup turn waits at most this long for the model roles to warm their cached prefixes (C-41).
    warmup_timeout_s: float = Field(default=30.0, gt=0)
    # The DE card's launcher (TOOLS.md 5.6): the direct mount, or the DEinput fallback (cut 2).
    de_launcher: DeLauncher = "deDirect"
    # Modes A and H: where the Runtime writes run records (contract "Where records live").
    run_bucket: str | None = None
    run_prefix: str = "orion/"
    aws_region: str = "us-east-1"
    port: int = 8080
    mode: Mode = "L"
    test_runtime: bool = False
    faults: dict[str, str] = Field(default_factory=dict)

    @property
    def tool_transport(self) -> Literal["in_process", "gateway"]:
        return "gateway" if self.tools == "gateway" else "in_process"

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
            "ORION_COMPOSER": "composer",
            "ORION_EXECUTOR": "executor",
            "ORION_TOOLS": "tools",
            "ORION_DEFAULT_SCOPE": "default_scope",
            "ORION_VLAB_ORIGIN": "vlab_origin",
            "ORION_HEARTBEAT_S": "heartbeat_s",
            "ORION_LEASE_RENEW_S": "lease_renew_s",
            "ORION_LEASE_TTL_S": "lease_ttl_s",
            "ORION_STOP_GRACE_S": "stop_grace_s",
            "ORION_WARMUP_TIMEOUT_S": "warmup_timeout_s",
            "ORION_DE_LAUNCHER": "de_launcher",
            "ORION_RUN_BUCKET": "run_bucket",
            "ORION_RUN_PREFIX": "run_prefix",
            "AWS_REGION": "aws_region",
            "ORION_MODE": "mode",
            "ORION_TEST_RUNTIME": "test_runtime",
            "ORION_FAULTS": "faults",
        }
        for name, field in mapping.items():
            if env.get(name):
                values[field] = env[name]
        return cls.model_validate(values)
