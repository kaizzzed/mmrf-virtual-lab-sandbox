"""Tool-layer settings that are not on settings.Settings, read once from the environment.

| Variable | Meaning | Default |
| --- | --- | --- |
| `ORION_GATEWAY_URL` | The AgentCore Gateway MCP URL (`https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`) | required when `ORION_TOOLS=gateway` |
| `ORION_VLAB_ACCESS_FILE` | The repo's access file (`{"origin", "username", "password"}`) for a hosted sandbox | none |
| `ORION_VLAB_BASIC_SECRET_ARN` | Secrets Manager ARN of `orion/vlab-basic` (same JSON); in-process tools on AWS only (cut 1) | none |
| `ORION_GOLDEN_DIR` | Golden bodies (schemas.GOLDEN_DIR_ENV); the packaged snapshot when the directory is absent | none |
| `ORION_KG_TARGET_REGISTERED` | `1` once the Workstream 1 `kg` target exists (stretch) | off |

settings.Settings holds the rest: `ORION_TOOLS` (`inprocess` or `gateway`, as `tool_transport`),
`ORION_VLAB_ORIGIN`, `AWS_REGION`, `ORION_MODE`, `ORION_TEST_RUNTIME` and `ORION_FAULTS`.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

_TRUE: Final = frozenset({"1", "true", "yes", "on"})


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_url: str | None = None
    access_file: Path | None = None
    basic_secret_arn: str | None = None
    golden_dir: Path | None = None
    kg_registered: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ToolConfig":
        source = os.environ if env is None else env
        values: dict[str, object] = {}
        mapping = {
            "ORION_GATEWAY_URL": "gateway_url",
            "ORION_VLAB_ACCESS_FILE": "access_file",
            "ORION_VLAB_BASIC_SECRET_ARN": "basic_secret_arn",
            "ORION_GOLDEN_DIR": "golden_dir",
        }
        for name, field in mapping.items():
            if source.get(name):
                values[field] = source[name]
        values["kg_registered"] = source.get("ORION_KG_TARGET_REGISTERED", "").strip().lower() in _TRUE
        return cls.model_validate(values)
