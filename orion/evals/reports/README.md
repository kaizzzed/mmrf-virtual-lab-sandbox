# Eval reports

Generated, never committed (see `../.gitignore`):

- `orion-evals-<UTC stamp>.json` and `.md`: one `python -m orion_evals run` (schema
  `orion.eval-report/v1`): environment, gates, every eval with its checks, runs and oracle values.
- `oracle-<UTC stamp>.json` and `.md`: one `python -m orion_evals oracle`.
- `oracle-live-test-<UTC stamp>.json`: what `tests/test_oracle_live.py` computed.
- `timings-<mode>-<UTC stamp>.json`: one `python -m orion_evals timings` (schema
  `orion.bedrock-timings/v1`): per ask the plan-visible time, the Writing interpretation wait and
  each model call's time and tokens from the run record, and a summary.
