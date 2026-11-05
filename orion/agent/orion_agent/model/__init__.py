"""ORION's model roles (RFA rev 25, A2 "Agent topology", A4 "Model configuration on Bedrock").

The planner and composer (Claude on Bedrock, or stubs in mode L), the code verifier, the
answer loop and the warm-up. Entry points: `factory.make_planner`, `factory.make_composer`,
`factory.make_verifier`, `answer.write_answer`, `warmup.warm_up_roles` and `calls.model_call_scope`.
Only `client`, `planner` and `composer` import the Anthropic SDK; the factory imports them when
a role is configured as `bedrock`.
"""
