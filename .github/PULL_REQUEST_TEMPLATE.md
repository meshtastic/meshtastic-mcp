## Summary

<!-- One sentence: what changed and why. -->

## Test plan

<!-- One sentence. Gates: `ruff check . && ruff format --check . && mypy && pytest tests/unit`.
     Note any hardware/firmware/emulator tier you ran. -->

## Checklist

- [ ] Gates pass (ruff, mypy — no new `ignore_errors`/`# noqa`, pytest unit tier)
- [ ] New MCP tools have read/destructive/openWorld annotations; destructive ones take `confirm`
- [ ] Core changes import/run with no firmware checkout
- [ ] DCO sign-off (`git commit -s`)
