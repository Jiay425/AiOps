# EvidenceGraph Localization Status

Date: 2026-06-14

## Goal

Upgrade CodeOps localization from plain code search to evidence-driven code localization.

The target behavior:

1. Alert/log/trace/metric signals become evidence nodes.
2. Repository search matches become code evidence nodes.
3. Code snippets expose method/file nodes.
4. Visible same-package service/repository calls become relation edges.
5. `CodeLocalizationAgent` receives the graph and must explain localization through an evidence chain.

## Implemented

Added:

- `EvidenceGraphEntity`
- `EvidenceGraphBuilderService`

Changed:

- `CodeLocalizationAgentInput`
  - added `evidenceGraph`
- `CodeLocalizationAgentService`
  - preserves `evidenceGraph` during normalization
- `CodeLocalizationPrompts`
  - requires use of graph nodes/edges when present
  - clarifies that non-alert classes can become candidates only through code relations
- `RepoUnderstandingSkill`
  - builds graph from task, ops diagnosis, code hints, search matches, and snippets
  - writes `evidenceGraph`, `evidenceGraphSummary`, and `evidenceGraphRankedCodeNodes` into raw output
- `EngineeringTaskTraceService`
  - exposes evidence graph summary/ranked nodes/full graph in working-memory summary, localization artifacts, stage artifacts, and highlights
- `CodeOpsEvalReportBuilder`
  - marks repo understanding key artifact as `evidenceGraph + targetFiles + targetMethods`
- `README.md`
  - documents evidence-driven code localization in English and Chinese

## Verification Done

Compile passed:

```powershell
$env:JAVA_HOME='D:\Java\jdk17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -q -DskipTests compile
```

Secret scan had not been rerun after the README/status doc update yet.

## Pending Verification

Run the cross-file case and check trace:

```powershell
& .\docs\dev-ops\scripts\start-eval-app.ps1 -SkipInstall

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8099/api/v1/codeops/evaluation/run/scope-expansion-cross-file-idempotency `
  -TimeoutSec 900 | ConvertTo-Json -Depth 12
```

Expected:

- case status is `SUCCESS`
- repo_understanding step raw output contains:
  - `evidenceGraphSummary`
  - `evidenceGraphRankedCodeNodes`
  - `evidenceGraph`
- incident fix view localization artifacts expose the same fields

## Commit Scope Recommendation

Commit only EvidenceGraph-related files unless other pre-existing local changes are intentionally included.

Expected files:

- `README.md`
- `docs/dev-ops/evidence-graph-localization-status.md`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/evidence/EvidenceGraphBuilderService.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/model/entity/EvidenceGraphEntity.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/localization/CodeLocalizationAgentInput.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/localization/CodeLocalizationAgentService.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/localization/CodeLocalizationPrompts.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/skill/RepoUnderstandingSkill.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/service/EngineeringTaskTraceService.java`
- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/eval/CodeOpsEvalReportBuilder.java`
