"""ORION's tool layer: ten read-only server tools, four executor pure functions and five client
actions (RFA rev 25, A2 "Tools"; orion/contracts/TOOLS.md).

`base` holds the shared contract and `schemas` the catalog (both frozen). The rest:

| Module | What it holds |
| --- | --- |
| `client` | `make_tool_client(settings)`, the in-process and Gateway ToolClients and the A4 budget |
| `server` | The server tools (`ServerToolBase.run`: one attempt, envelope and provenance) |
| `functions` | `set_operation`, `check_subset`, `rank_top_n`, `derive_km_median`, and `run_function` / `call_function` |
| `guards` | Pre-guard checks, the ProteinPaint bridge rule (OQ11) and `executor_pre_guard` |
| `records` | `call_records(outcome)`, `RecordedOutcome` and `ToolCallCancelled` |
| `bodies`, `goldens`, `normalize` | Request bodies from the goldens, the golden store, response readers |
| `http`, `vlab`, `credentials`, `gateway`, `faults`, `config` | Transport, allow-list, the Basic credential, MCP and SigV4, ORION_FAULTS, env |

This package imports nothing at import time, so the Gateway Lambda bundles only what its handler uses.
"""
