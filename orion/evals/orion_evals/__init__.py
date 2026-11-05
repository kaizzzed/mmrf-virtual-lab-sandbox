"""ORION eval harness (RFA rev 25, "Observability and evaluation", E1-E24).

A black-box harness: it drives the agent through the wire contract
(`orion/contracts/README.md` v1.2) and checks what it sees against an independent oracle that
reads the local stack with its own requests and its own arithmetic. Nothing here imports
`orion_agent`; the contract's hash and format rules are reimplemented and tested against the
shared vectors in `orion/contracts/fixtures`.
"""

HARNESS_VERSION = "1.0.0"
