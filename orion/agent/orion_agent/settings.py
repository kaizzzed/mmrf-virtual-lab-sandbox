"""Runtime settings, read once from the environment."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DefaultScope = Literal["active_cohort", "all_cases"]


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
        }
        for name, field in mapping.items():
            if env.get(name):
                values[field] = env[name]
        return cls.model_validate(values)
