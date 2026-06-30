# Evals

Lightweight, human-runnable scoring of how reliably an agent drives the MCP tool surface and the
bundled `meshtastic-e2e` skill. Pairs with the machine-parseable `LOOP … PASS|FAIL` verdicts the
skill helper emits, so results are gradeable without bespoke harness code.

- `canonical-tasks.md` — the task list an agent is scored against (Tier 1 = no device, Tier 2 = device/lab).
- `tool-selection.csv` — `intent,expected_tool` dataset for the `select` eval (the agent maps each
  intent to a tool; score = accuracy). `tests/unit/test_evals_dataset.py` keeps it honest: every
  `expected_tool` must be a registered MCP tool, so a rename/removal fails CI instead of rotting the eval.
- `results-template.csv` — copy per run; one row per task.
- `score.sh` — summarizes a results CSV (pass rate per category).

The Tier-1 `select`/`knowledge` tasks need no hardware and can run in CI with an agent in the loop;
the Tier-2 device/e2e tasks run before tagging a release.
