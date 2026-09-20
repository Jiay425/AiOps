## Summary

## Verification

- [ ] `python -m pytest -q`
- [ ] `python -m compileall -q src`
- [ ] `git diff --check`

## Safety review

- [ ] No secret, token, customer data, or full production prompt is included.
- [ ] LangGraph checkpoint / resume compatibility is preserved or migrated.
- [ ] Any external effect remains behind the existing approved boundary.
- [ ] Kafka consumer idempotency and Outbox behavior are covered when applicable.
