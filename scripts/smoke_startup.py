"""Start the complete FastAPI lifespan once and verify the health endpoint."""

from fastapi.testclient import TestClient

from ops_autoagent.api import app, settings


def main() -> int:
    settings.mysql_url = ""
    settings.langgraph_checkpoint_backend = "memory"
    settings.ops_runbook_vector_enabled = False
    settings.ops_demo_auto_seed_enabled = False
    with TestClient(app) as client:
        response = client.get("/actuator/health")
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "UP":
            raise RuntimeError(f"Unexpected health response: {payload}")
    print("FastAPI lifespan startup/shutdown smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
