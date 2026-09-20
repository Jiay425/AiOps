import pytest

from ops_autoagent.config import Settings
from ops_autoagent.runbook_actions import RunbookActionRequest, RunbookActionService


@pytest.mark.asyncio
async def test_runbook_action_is_denied_when_not_allowlisted():
    service = RunbookActionService(Settings(ops_runbook_action_allowlist="scale_workload"))
    result = await service.dry_run(RunbookActionRequest(action="restart_workload", serviceName="order-service"))
    assert result["status"] == "DENIED"
    assert result["productionWrite"] is False


@pytest.mark.asyncio
async def test_runbook_action_requires_real_kubernetes_endpoint_not_a_fake_success():
    service = RunbookActionService(Settings(ops_runbook_action_allowlist="scale_workload",
                                            ops_runbook_kubernetes_api_base=""))
    result = await service.dry_run(RunbookActionRequest(action="scale_workload", serviceName="order-service", replicas=3))
    assert result["status"] == "NOT_CONFIGURED"
    assert result["productionWrite"] is False
