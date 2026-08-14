# Runbook RAG Stable Startup

This project uses external embedding and rerank services for Runbook RAG evaluation. Do not paste API keys into tracked YAML files.

## One-time setup

Copy the template and fill the three local secrets:

```powershell
Copy-Item docs/dev-ops/runbook-rag.env.example docs/dev-ops/runbook-rag.local.env
notepad docs/dev-ops/runbook-rag.local.env
```

`*.local.env` is ignored by git.

## Start app

```powershell
pwsh docs/dev-ops/scripts/start-runbook-rag-eval.ps1
```

The script does the boring parts:

- loads `docs/dev-ops/runbook-rag.local.env`
- stops any old process on port `8099`
- runs `mvn -q -DskipTests install` so single-module startup cannot load stale jars
- starts the app with the `full` profile
- waits until the app is ready
- lets `OpsRunbookVectorIndexer` check PGVector dimensions on startup

## Run Runbook RAG eval

```powershell
pwsh docs/dev-ops/scripts/start-runbook-rag-eval.ps1 -RunEval
```

The latest eval result is also written to:

```text
data/log/runbook-rag-eval-result.json
```

## Force rebuild vector index

Normally this is not needed. The app checks the vector table dimension automatically and rebuilds only if the embedding dimension does not match the table.

```powershell
pwsh docs/dev-ops/scripts/start-runbook-rag-eval.ps1 -RebuildVector
```

## Current retrieval chain

```text
Vector recall + BM25 chunk recall
-> RRF fusion
-> Cross-Encoder rerank
-> chunk-level explainable hits
```

There is no hand-written domain boost in the ranking path.
