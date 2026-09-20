# Contributing

Use Python 3.11+, run `python -m pytest -q`, `python -m compileall -q src`, and `git diff --check` before opening a pull request.

Never commit `.env`, deployment credentials, full production telemetry, API keys, or customer identifiers. New agent behavior must include an Eval or regression test and must not add another route that writes a target repository outside `apply_approved_patch`.
