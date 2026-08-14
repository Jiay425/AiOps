# Ops AutoAgent Diagnosis

The active runtime is Python 3.11 + official LangGraph + FastAPI. It preserves the complete Ops and CodeOps workflow surface, live SSE, persistent approval resume, legacy MySQL compatibility, PGVector RAG, alert scheduling, notification, governance, and evaluation chains. Spring AI modules are retained under `legacy-spring-ai/` only as an auditable migration baseline and are not part of the build or deployment.

See [README.md](README.md) for architecture, setup, configuration, and verification commands.

The unified verification gate covers 33 tests, all 42 legacy HTTP routes, all 55 legacy full-profile environment placeholders, all 362 legacy public Java types, and 34 CodeOps/Ops/RAG behavior fixtures.
