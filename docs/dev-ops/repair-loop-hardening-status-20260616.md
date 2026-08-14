# Repair Loop Hardening Status

Date: 2026-06-16

## Completed

- Localization Blocking is connected to the main orchestrator.
- Code Localization now emits an explicit `repairPlan`.
- `BugFixSkill` forwards `repairPlan` to the BugFix LLM input.
- Added `FailureDiagnosticEntity`.
- Added `FailureDiagnosticParserService`.
- Reflection failure handling now stores structured `failureDiagnostic` back into the failed step output and reflection context.
- Existing PatchAttemptLoop remains capped at 3 rounds and now consumes structured diagnostics.
- Added `ExactReplaceBlockPatchEntity`.
- BugFix output supports `exactReplaceBlocks`.
- `BugFixSkill` converts exact replace blocks into whole-file diffs so existing sandbox, guard, compile and rollback gates still apply.
- Trace highlights now expose `repairPlan`, `exactReplaceBlocks`, `exactReplaceApply`, and `failureDiagnostic`.
- README documents the Repair Loop Harness and the real LLM single-case script.
- Added `docs/dev-ops/scripts/run-codeops-real-llm-case.ps1`.
- The real LLM script now loads the ignored local env file before checking model variables.
- The real LLM script now performs a tiny chat-completions preflight before starting the app. If the model account has no balance, it fails fast instead of spending minutes booting the full harness.
- The real LLM script now uses the CodeOps task API as readiness check, avoiding false failures when optional actuator checks such as mail return `DOWN`.
- Repair-scope method constraints are normalized before entering Guard. Natural-language reasoning can no longer leak into `repairScope.targetMethods`.
- `FULL_FILE` / cross-file Guard semantics now enforce file boundaries while treating method lists as guidance instead of a hard method whitelist.
- `NEED_MORE_EVIDENCE` is treated as a localization blocker and is no longer converted into `NO_CODE_FIX` just because code repair is disabled.

## Verified

Compile passed:

```powershell
$env:JAVA_HOME='D:\Java\jdk17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -q -DskipTests compile
```

Secret scan was run. Matches were environment variable placeholders/config keys only; no real API key was found in tracked changes.

## Real LLM Runs

The real LLM case was executed with local env values loaded from the ignored `docs/dev-ops/runbook-rag.local.env` file.

- Case: `scope-expansion-cross-file-idempotency`
- First completed run artifacts: `data/codeops-real-llm-runs/20260616-222500-scope-expansion-cross-file-idempotency`
- Result: `FAILED`
- Finding: Code Localization and patch intent were good, but `repairScope.targetMethods` contained natural-language reasoning. PatchScopeGuard rejected the patch with `HALLUCINATED_SCOPE`.
- Fix applied: method constraints are now normalized, and `FULL_FILE` scope does not treat target methods as a hard whitelist.

- Second completed run artifacts: `data/codeops-real-llm-runs/20260616-223316-scope-expansion-cross-file-idempotency`
- Result: `FAILED`
- Finding: the model provider returned `402 Payment Required / Insufficient Balance` during `agent_loop_investigation`, so the run could not prove the full LLM repair loop.
- Fix applied: `NEED_MORE_EVIDENCE` and failed localization are now blocking states instead of being mislabeled as `NO_CODE_FIX`.

- Third run attempt: preflight only
- Result: failed fast before app boot
- Finding: the same model provider still returned `402 Payment Required / Insufficient Balance`.
- Evidence: `docs/dev-ops/scripts/run-codeops-real-llm-case.ps1` now reports `LLM preflight failed HTTP 402`.

- Fourth run attempt: preflight only
- Result: failed fast before app boot
- Finding: the same model provider still returned `402 Payment Required / Insufficient Balance`.
- Conclusion: final proof run is externally blocked until the configured chat model account has available balance or `docs/dev-ops/runbook-rag.local.env` is updated to a working compatible chat endpoint.

## Pending

The final proof run still needs a model account with available balance. Re-run:

```powershell
.\docs\dev-ops\scripts\run-codeops-real-llm-case.ps1 -CaseId scope-expansion-cross-file-idempotency
```

Expected evidence:

- Case reaches `SUCCESS`, `WAITING_APPROVAL`, or an informative `FAILED` with `failureDiagnostic`.
- Report/trace shows `repairPlan`.
- BugFix trace shows `exactReplaceBlocks` or file rewrites.
- Any compile/test/patch failure is represented as `failureDiagnostic`.
- Artifacts are written under `data/codeops-real-llm-runs/`.
