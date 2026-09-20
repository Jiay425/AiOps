"""Guarded Runbook action planning and Kubernetes server-side dry-run.

No method in this module executes a production mutation. Kubernetes receives
`dryRun=All`, so admission, policy and schema checks run without persistence.
"""
from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunbookActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    action: Literal["restart_workload", "scale_workload", "rollback_release"]
    service_name: str = Field(alias="serviceName", min_length=1, max_length=100)
    namespace: str = Field(default="", max_length=100)
    replicas: int | None = Field(default=None, ge=0, le=100)
    change_ref: str = Field(default="", alias="changeRef", max_length=160)

    @field_validator("service_name", "namespace", "change_ref")
    @classmethod
    def clean(cls, value: str) -> str:
        return str(value or "").strip()


class RunbookActionService:
    def __init__(self, settings: Any):
        self.settings = settings
        allowed = str(getattr(settings, "ops_runbook_action_allowlist", "") or "")
        self.allowed = {item.strip() for item in allowed.split(",") if item.strip()}

    async def dry_run(self, request: RunbookActionRequest) -> dict[str, Any]:
        namespace = request.namespace or str(getattr(self.settings, "ops_runbook_kubernetes_namespace", "default"))
        base = str(getattr(self.settings, "ops_runbook_kubernetes_api_base", "") or "").rstrip("/")
        enabled = bool(getattr(self.settings, "ops_runbook_server_dry_run_enabled", True))
        result = {"action": request.action, "serviceName": request.service_name, "namespace": namespace,
                  "executionMode": "KUBERNETES_SERVER_DRY_RUN", "productionWrite": False,
                  "approvalRequired": True, "blastRadius": {"level": "MEDIUM", "services": [request.service_name]},
                  "status": "NOT_CONFIGURED", "checks": []}
        if request.action not in self.allowed:
            return {**result, "status": "DENIED", "blockingReasons": ["Action is not in OPS_RUNBOOK_ACTION_ALLOWLIST."]}
        if not enabled or not base:
            return {**result, "blockingReasons": ["Kubernetes server dry-run is not configured."]}
        token = str(getattr(self.settings, "ops_runbook_kubernetes_token", "") or "")
        headers = {"Content-Type": "application/merge-patch+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body, path = self._manifest(request, namespace)
        try:
            async with httpx.AsyncClient(timeout=15, verify=True) as client:
                response = await client.patch(f"{base}{path}", params={"dryRun": "All", "fieldManager": "ops-autoagent"},
                                              headers=headers, json=body)
            status = "PASSED" if response.is_success else "FAILED"
            return {**result, "status": status, "requestPath": path, "checks": [
                {"name": "kubernetes_admission_dry_run", "passed": response.is_success, "httpStatus": response.status_code}],
                    "blockingReasons": [] if response.is_success else [response.text[:1000]]}
        except httpx.HTTPError as exc:
            return {**result, "status": "FAILED", "blockingReasons": [f"Kubernetes dry-run request failed: {type(exc).__name__}"]}

    @staticmethod
    def _manifest(request: RunbookActionRequest, namespace: str) -> tuple[dict[str, Any], str]:
        path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{request.service_name}"
        if request.action == "scale_workload":
            return {"spec": {"replicas": request.replicas}}, path
        if request.action == "restart_workload":
            return {"spec": {"template": {"metadata": {"annotations": {"ops-autoagent/dry-run-restart": "true"}}}}}, path
        return {"metadata": {"annotations": {"ops-autoagent/dry-run-rollback-to": request.change_ref}}}, path
