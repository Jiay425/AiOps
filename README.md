<div align="center">

# Ops AutoAgent Diagnosis

### A durable LangGraph control plane for evidence-grounded incident-to-fix automation

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#quick-start)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2.9-1C3C3C?logo=langchain&logoColor=white)](#langgraph-runtime)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](#api-surface)
[![Checkpoints](https://img.shields.io/badge/Execution-SQLite%20%7C%20PostgreSQL-336791?logo=postgresql&logoColor=white)](#durable-execution)
[![Eval](https://img.shields.io/badge/Evaluation-52%2B%20business%20cases-6E40C9)](#evaluation)

**From an operational signal to a traceable diagnosis, reviewable patch, and test-gated delivery.**

</div>

---

## Why this project?

Production repair is not a single LLM call. A useful AIOps system has to collect
evidence from several operational surfaces, preserve the reasoning context,
inspect the real repository, constrain the patch, verify the result, and stop
at a human-controlled effect boundary.

Ops AutoAgent Diagnosis is a Python-native migration of a Spring AI/Spring Boot
runtime. It uses LangGraph as the execution model rather than treating the LLM
as an unbounded chatbot:

~~~text
Incident / code task
        │
        ▼
Evidence bundle ──► structured diagnosis ──► repository investigation
        │                                      │
        │                                      ▼
        └──────────────────────────────► patch proposal
                                               │
                                               ▼
                                      compile / test verification
                                               │
                                               ▼
                                      independent release review
                                               │
                               human approval / delivery-only output
~~~

The result is a stateful agent harness designed for inspection, replay,
bounded retries, and safe hand-off—not a model with direct write access to a
production repository.

## Highlights

| | Capability | What it means |
| --- | --- | --- |
| 🧭 | Evidence-grounded diagnosis | Metrics, logs, traces, and runbooks are collected as explicit evidence before a diagnosis is emitted. |
| 🧩 | Graph-native orchestration | Typed state, conditional edges, fan-out/fan-in, nested subgraphs, and bounded feedback loops are first-class runtime concepts. |
| 🧠 | Three-role remediation path | The incident-to-fix path separates diagnosis, repair, and independent review responsibilities. |
| 🧱 | Contracted subgraphs | Evidence, repository investigation, repair proposal, verification, and review each publish a validated domain contract. |
| ⏸️ | Durable human-in-the-loop | <code>interrupt()</code> pauses execution; the same <code>thread_id</code> resumes it with <code>Command(resume=...)</code>. |
| 🔐 | Single effect boundary | Models can propose changes, but only <code>apply_approved_patch</code> can mutate the target repository. |
| 🧪 | Test-gated delivery | Patch scope, compile/test results, review facts, approval state, and patch digests are carried through the graph. |
| 📊 | Traceable evaluation | 52 business E2E cases, 10 runtime safety/reliability cases, event replay, artifacts, and runtime metrics are kept separate from raw prompts. |

## Architecture

The runtime has two top-level graph surfaces. <code>CodeOpsGraph</code> is the remediation
control plane; <code>OpsDiagnosisGraph</code> remains a standalone observability-compatible
diagnosis surface for incident analysis and SSE streaming. The CodeOps path can
reuse the evidence contract without making an observability tool directly
responsible for repository changes.

~~~mermaid
flowchart LR
    U[Alert / Issue / API request] --> F[FastAPI]
    F --> C[CodeOpsGraph]

    C --> P[plan]
    P --> O[orchestrate]
    O --> D[Diagnosis stage]
    D --> E[OpsEvidenceSubgraph]
    E --> X[Metrics / Logs / Traces / Runbooks]
    D --> I[RepositoryInvestigationSubgraph]
    I --> R[Repair stage]
    R --> S[RepairProposalSubgraph]
    S --> V[VerificationSubgraph]
    V --> Q[IndependentReviewSubgraph]

    Q -->|ACCEPT / HUMAN_REVIEW| H[interrupt: human approval]
    Q -->|RETRY_REPAIR| R
    Q -->|REJECT / NO_CODE_FIX| Z[summarize]
    H -->|approve delivery| L[deliver_patch]
    H -->|approve apply| A[apply_approved_patch]
    H -->|reject| N[rejected]
    L --> Z
    A --> Z
    N --> Z

    C -. durable state .-> K[(LangGraph checkpointer)]
    C -. events / artifacts / metrics .-> M[(Store)]
    A -. only production write .-> T[Target repository]

    OD[OpsDiagnosisGraph] --> OE[fixed observability collection]
    OE --> OR[evidence review / report]
    OR --> OS[SSE stream]
~~~

### The three agent responsibilities

The graph separates business responsibilities even when the implementation
routes them through multiple specialized skill nodes:

| Role | Input | Output | Effect boundary |
| --- | --- | --- | --- |
| **Diagnosis agent** | Incident context and fixed operational sources | Evidence bundle, root-cause candidates, confidence, missing-evidence and remediation constraints | Read-only |
| **Repair agent** | Diagnosis contract and repository context | Localized files/methods, patch proposal, patch digest, verification plan | Managed patch sandbox only |
| **Review agent** | Patch facts, scope guard, compile/test result, risk context | Structured release verdict and retry / approval constraints | Read-only |

The parent graph owns sequencing, budgets, approval, retry policy, and side
effects. The agents do not bypass those policies by calling tools directly.

## Graph topology

### <code>CodeOpsGraph</code> — remediation control plane

~~~text
START
  → plan
  → orchestrate
      ├─ ops_diagnosis
      ├─ agent_loop_investigation
      ├─ repo_understanding
      ├─ engineering_knowledge_rag
      ├─ bug_fix
      ├─ test_verification
      ├─ release_risk_analysis
      └─ pr_review
  → finish
      ├─ retry_repair → repair_feedback → bug_fix
      ├─ approval → prepare_approval → human_approval
      │                         ├─ deliver_patch
      │                         ├─ apply_approved_patch
      │                         └─ rejected
      └─ summarize → END
~~~

The orchestrator chooses the next skill from task type, working memory,
completed skills, focus areas, and remaining budgets. Every route returns to
the parent graph, so the parent remains the source of truth for task status,
attempt count, and effects.

### Reusable subgraphs

Each subgraph is a compiled <code>StateGraph</code> with its own small state
contract. The parent invokes it with a task-scoped <code>thread_id</code>, then
attaches only the validated output and artifact references to the parent state.

| Subgraph | Internal shape | Responsibility |
| --- | --- | --- |
| <code>OpsEvidenceSubgraph</code> | <code>prepare_input → collect_and_review_evidence → publish_contract</code> | Normalize operational evidence and evidence sufficiency without writing code. |
| <code>RepositoryInvestigationSubgraph</code> | <code>prepare_input → readonly_investigation → publish_contract</code> | Read-only repository search, snapshots, file snippets, diffs, history, tests, and engineering knowledge. |
| <code>RepairProposalSubgraph</code> | <code>prepare_input → sandbox_repair_proposal → publish_contract</code> | Produce and validate a patch proposal inside the managed patch sandbox. |
| <code>VerificationSubgraph</code> | <code>prepare_input → run_verification → publish_contract</code> | Normalize compile/test/background-task facts into a verification contract. |
| <code>IndependentReviewSubgraph</code> | <code>prepare_input → review_patch_facts → publish_contract</code> | Validate an independent release-review contract and downgrade unsafe release claims. |

This decomposition makes the workflow inspectable: a checkpoint can show both
the parent node and the domain artifact produced by the child graph.

## LangGraph runtime

This project uses LangGraph as a durable state machine, not only as a prompt
router.

| LangGraph capability | Where it appears | Why it matters |
| --- | --- | --- |
| Typed graph state | <code>CodeOpsState</code>, <code>OpsState</code>, <code>SubgraphState</code> | Nodes exchange structured state instead of opaque chat messages. |
| Conditional edges | <code>orchestrate</code>, <code>finish</code>, <code>human_approval</code>, <code>apply_approved_patch</code> | Routing decisions are explicit and testable. |
| Reducers | <code>events</code>, <code>tool_trace</code>, <code>effect_log</code> use additive list reducers | Parallel branches and repeated attempts append history instead of overwriting it. |
| Fan-out / fan-in | Parallel metrics, logs, and traces collection plus an evidence barrier | Independent evidence sources can run concurrently and join deterministically. |
| Nested subgraphs | Five CodeOps domain subgraphs | Domain contracts stay small while the parent retains global policy control. |
| Checkpointing | Memory, SQLite, or PostgreSQL saver | A task can be inspected, resumed, and reconciled across process boundaries. |
| Interrupt / resume | <code>interrupt()</code> and <code>Command(resume=...)</code> | Human approval pauses the graph without losing the execution state. |
| Streaming | <code>astream(..., stream_mode="updates")</code> and FastAPI SSE | Clients receive node events and can replay a task timeline. |
| Bounded feedback loops | Repair feedback, round limits, tool budgets, and retry budgets | Agent autonomy is constrained by deterministic limits. |

### Durable execution

Every CodeOps task uses its task ID as the LangGraph <code>thread_id</code>. The
checkpointer stores the state needed to inspect the current node, pending
interrupt, approval identity, patch digest, and retry context:

~~~json
{
  "threadId": "task-id",
  "currentNode": ["human_approval"],
  "status": "WAITING_APPROVAL",
  "approvalId": "approval-id",
  "interruptPending": true
}
~~~

Supported backends are selected through configuration:

~~~text
memory      local process tests and short-lived development runs
sqlite      default durable local deployment
postgres    multi-process / service deployment
~~~

Terminal in-memory checkpoints are pruned; resumable approval checkpoints are
retained. This keeps local development predictable without changing the
durable backend contract.

## Data and safety boundary

The model proposes intent. The runtime validates and materializes effects.

~~~text
LLM intent
  → typed diagnosis / investigation / patch / review contract
  → scope guard + patch validation
  → managed patch sandbox
  → compile and test verification
  → independent review
  → human approval interrupt
  → delivery artifact or apply_approved_patch
  → target repository
~~~

- The model never receives a direct production-repository write tool.
- Patch generation is isolated behind <code>RepairProposalSubgraph</code> and
  <code>PatchSandbox</code>.
- <code>apply_approved_patch</code> is the only production mutation boundary.
- <code>CODEOPS_APPLY_MODE=delivery_only</code> remains the conservative default.
- Scope Guard, patch validation, baseline digests, and patch digests prevent
  an approval from silently drifting to another repository state.
- Reviewer output is structured and checked against deterministic patch/test
  facts; unsafe <code>RELEASE_READY</code> claims are downgraded to human review.
- Trace, SSE, event, and evaluation projections are redacted and bounded.
- Secrets belong in an untracked local <code>.env</code>, never in source,
  fixtures, or committed evaluation output.

## Evidence layer

~~~text
Prometheus metrics ─┐
ELK log evidence   ─┼──► evidence signals ───► diagnosis contract
SkyWalking traces  ─┤
Runbook retrieval  ─┘
~~~

Evidence is represented with provenance, availability, negative evidence, and
review constraints. A source that was successfully queried but returned no
anomaly is preserved as negative evidence; it is not silently treated as a
missing source.

The CodeOps parent consumes the resulting contract as input to repository
investigation. The standalone Ops graph additionally supports serial or
parallel collection, an evidence barrier, review-driven supplementation, and
SSE event streaming for incident diagnosis.

## Evaluation

The repository contains a catalog and execution harness rather than a
single hand-picked demo:

| Suite | Coverage | Status |
| --- | --- | --- |
| Business E2E | 52 cases: 16 legacy baseline + 36 expansion cases | Catalogued as <code>E2E_BUSINESS</code> |
| Runtime safety / reliability | 10 cases | Reported separately from business outcomes |
| Fixture provenance | Incident telemetry and sample repositories | Explicit fixture references and reuse metadata |

The business cases cover distributed consistency, database and infrastructure
failures, configuration, code-quality and issue-to-patch tasks, release risk,
scope governance, verification, and reviewer feedback.

Evaluation reports keep these dimensions distinct:

~~~text
evidence quality
root-cause / localization quality
patch proposal quality
verification result
review verdict
repair attempts and budgets
checkpoint / SSE replay behavior
scope-guard and unauthorized-write safety
~~~

Fixture-backed runs are labeled as such. If a real LLM or external service is
unavailable, the harness reports the unavailable stage instead of fabricating
review, Maven, or production-observability success.

## API surface

The FastAPI application exposes the graph as ordinary HTTP and SSE contracts:

| Surface | Endpoint |
| --- | --- |
| Service health | <code>GET /actuator/health</code> |
| OpenAPI | <code>GET /docs</code> |
| Ops diagnosis stream | <code>POST /api/v1/ops/incident/analyze</code> |
| CodeOps task submission | <code>POST /api/v1/codeops/task/submit</code> |
| CodeOps task stream / replay | <code>GET /api/v1/codeops/task/{task_id}/events</code> |
| Approval status | <code>GET /api/v1/codeops/evaluation/approval/{task_id}</code> |
| Evaluation catalog | <code>GET /api/v1/codeops/evaluation/cases</code> |
| Evaluation summary | <code>GET /api/v1/codeops/evaluation/summary</code> |
| Runtime safety cases | <code>GET /api/v1/codeops/evaluation/runtime/cases</code> |
| Prometheus metrics | <code>GET /actuator/prometheus</code> |

The SSE projection contains bounded event summaries, artifact references,
subgraph/node identity, attempt number, and replay metadata—not full prompts,
secrets, or unrestricted tool responses.

## Repository layout

~~~text
ops-autoagent-diagnosis-python/
├── src/ops_autoagent/
│   ├── graphs/
│   │   ├── codeops.py       # CodeOps parent graph and routing policy
│   │   ├── ops.py           # standalone incident diagnosis graph
│   │   ├── subgraphs.py     # domain subgraphs and contracts
│   │   └── state_models.py  # reducers, digests, durable state helpers
│   ├── codeops/             # tools, repair services, evaluator, policies
│   ├── ops/                 # observability, runbook, and incident services
│   ├── api.py               # FastAPI, SSE, approval, and evaluation routes
│   ├── persistence.py       # LangGraph checkpointer selection
│   ├── schemas.py            # Pydantic contracts
│   └── config.py             # environment-backed settings
├── tests/                    # graph, API, safety, and regression tests
├── fixtures/incident/        # redacted incident and evaluation fixtures
├── samples/                  # sample repositories and verification assets
├── docs/                     # migration, architecture, and runbook notes
├── pyproject.toml
└── .env.example
~~~

## Quick start

Requires Python 3.11 or newer.

~~~powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv/Scripts/python.exe -m ops_autoagent.main
~~~

The local server listens on <code>http://127.0.0.1:8099</code> by default:

~~~text
console      /
OpenAPI      /docs
health       /actuator/health
metrics      /actuator/prometheus
~~~

Configure local credentials and service addresses only in <code>.env</code>. The default
development posture is fixture-friendly, checkpointed, and delivery-only.

## Configuration

| Variable | Purpose |
| --- | --- |
| <code>OPENAI_BASE_URL</code> / <code>OPENAI_API_KEY</code> / <code>OPENAI_MODEL</code> | OpenAI-compatible model endpoint and model selection |
| <code>LANGGRAPH_CHECKPOINT_BACKEND</code> | <code>memory</code>, <code>sqlite</code>, or <code>postgres</code> |
| <code>LANGGRAPH_CHECKPOINT_PATH</code> | SQLite checkpoint file |
| <code>LANGGRAPH_CHECKPOINT_POSTGRES_URL</code> | PostgreSQL checkpoint connection |
| <code>CODEOPS_HITL_APPROVAL_ENABLED</code> | Enable approval interrupt before delivery/apply |
| <code>CODEOPS_APPLY_MODE</code> | Conservative default: <code>delivery_only</code> |
| <code>CODEOPS_SUBGRAPHS_ENABLED</code> | Enable domain subgraph contracts |
| <code>OPS_FIXTURE_FALLBACK</code> | Allow deterministic fixtures for local validation |
| <code>PROMETHEUS_BASE_URL</code> / <code>ELK_BASE_URL</code> / <code>SKYWALKING_GRAPHQL_URL</code> | External observability sources |
| <code>OPS_RUNBOOK_PATH</code> / <code>PGVECTOR_URL</code> | Runbook source and optional vector retrieval |

Never place production credentials, private keys, or bearer tokens in this
repository. Use an ignored <code>.env</code> file or the deployment secret manager.

## Verification

Run the repository's verification script:

~~~powershell
powershell -File scripts/verify.ps1 -Python .venv/Scripts/python.exe
~~~

For focused checks:

~~~powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m compileall src
git diff --check
~~~

The latest migration/evaluation validation recorded in the repository passed
the Python test suite, compilation check, diff check, and sample Maven
verification. External LLM, Prometheus, ELK, SkyWalking, and Maven results
remain environment-dependent and are never inferred from fixture presence.

## Migration note

The project keeps the original Spring AI domain intent—incident evidence,
runbook context, repository repair, risk review, and verification—while moving
execution semantics to Python and LangGraph:

~~~text
Spring AI / Spring Boot services
          ↓ migration
Python 3.11 + LangGraph + FastAPI
~~~

The important change is not the programming language alone. State, routing,
checkpointing, interrupts, subgraphs, streaming, and effect boundaries are now
visible in the graph itself, making each run easier to inspect, replay, test,
and explain.

## Scope

Ops AutoAgent Diagnosis is an engineering automation and decision-support
harness. It is not an unattended production deployer, a replacement for SRE
approval, or a guarantee that every incident has a code fix. When evidence,
model access, repository context, or verification capability is insufficient,
the runtime keeps the limitation visible and stops at the appropriate boundary.
