"""Verify every environment placeholder in the legacy full profile is accepted by Settings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ops_autoagent.config import Settings


ROOT = Path(__file__).resolve().parents[1]
FULL_PROFILE = ROOT / "legacy-spring-ai" / "ops-autoagent-app" / "src" / "main" / "resources" / "application-full.yml"

LEGACY_ENV_TO_FIELD = {
    "OPS_MAIL_HOST": "ops_mail_host", "OPS_MAIL_PORT": "ops_mail_port",
    "OPS_MAIL_USERNAME": "ops_mail_username", "OPS_MAIL_PASSWORD": "ops_mail_password",
    "OPS_MAIL_AUTH": "ops_mail_auth", "OPS_MAIL_STARTTLS": "ops_mail_starttls",
    "MYSQL_USERNAME": "mysql_username", "MYSQL_PASSWORD": "mysql_password", "MYSQL_URL": "mysql_url",
    "PGVECTOR_USERNAME": "pgvector_username", "PGVECTOR_PASSWORD": "pgvector_password",
    "PGVECTOR_URL": "pgvector_url", "OPENAI_VECTOR_TABLE": "pgvector_table",
    "OPENAI_BASE_URL": "openai_base_url", "OPENAI_API_KEY": "openai_api_key",
    "OPENAI_CHAT_MODEL": "openai_model", "OPENAI_EMBEDDING_MODEL": "ops_embedding_model",
    "OPENAI_EMBEDDING_DIMENSIONS": "ops_embedding_dimensions",
    "CODEOPS_LLM_FLASH_MODEL": "codeops_llm_flash_model",
    "CODEOPS_LLM_PRO_MODEL": "codeops_llm_pro_model",
    "CODEOPS_LLM_PRO_ESCALATION_ENABLED": "codeops_llm_pro_escalation_enabled",
    "CODEOPS_TEST_EXECUTION_ENABLED": "codeops_test_execution_enabled",
    "CODEOPS_TEST_EXECUTION_TIMEOUT_MS": "codeops_test_execution_timeout_ms",
    "CODEOPS_PATCH_SANDBOX_ENABLED": "codeops_patch_sandbox_enabled",
    "CODEOPS_PATCH_SANDBOX_BASE_DIR": "codeops_patch_sandbox_base_dir",
    "CODEOPS_PATCH_SANDBOX_PREFER_GIT_WORKTREE": "codeops_patch_sandbox_prefer_git_worktree",
    "CODEOPS_PATCH_SANDBOX_TIMEOUT_MS": "codeops_patch_sandbox_timeout_ms",
    "OPS_AGENT_SKILL_BASE_PATH": "ops_agent_skill_base_path", "OPS_RUNBOOK_BASE_PATH": "ops_runbook_path",
    "OPS_RUNBOOK_CHUNK_SIZE": "ops_runbook_chunk_size",
    "OPS_RUNBOOK_CHUNK_MIN_SIZE_CHARS": "ops_runbook_chunk_min_size_chars",
    "OPS_RUNBOOK_CHUNK_MIN_LENGTH_TO_EMBED": "ops_runbook_chunk_min_length_to_embed",
    "OPS_RUNBOOK_CHUNK_MAX_NUM_CHUNKS": "ops_runbook_chunk_max_num_chunks",
    "OPS_RUNBOOK_CHUNK_KEEP_SEPARATOR": "ops_runbook_chunk_keep_separator",
    "OPS_RUNBOOK_EMBEDDING_BASE_URL": "ops_embedding_base_url",
    "OPS_RUNBOOK_EMBEDDING_API_KEY": "ops_embedding_api_key",
    "OPS_RUNBOOK_EMBEDDING_MODEL": "ops_embedding_model",
    "OPS_RUNBOOK_EMBEDDING_DIMENSIONS": "ops_embedding_dimensions",
    "OPS_RUNBOOK_VECTOR_SCHEMA_CHECK_ON_STARTUP": "ops_runbook_vector_schema_check_on_startup",
    "OPS_RUNBOOK_VECTOR_REBUILD_ON_STARTUP": "ops_runbook_vector_rebuild_on_startup",
    "OPS_RUNBOOK_VECTOR_FALLBACK_TO_FILE": "ops_runbook_vector_fallback_to_file",
    "OPS_RUNBOOK_VECTOR_INDEX_BATCH_SIZE": "ops_runbook_vector_index_batch_size",
    "OPS_RUNBOOK_VECTOR_INDEX_BATCH_RETRIES": "ops_runbook_vector_index_batch_retries",
    "OPS_RUNBOOK_HYBRID_ENABLED": "ops_runbook_hybrid_enabled",
    "OPS_RUNBOOK_HYBRID_RRF_K": "ops_runbook_hybrid_rrf_k",
    "OPS_RUNBOOK_HYBRID_VECTOR_WEIGHT": "ops_runbook_hybrid_vector_weight",
    "OPS_RUNBOOK_HYBRID_KEYWORD_WEIGHT": "ops_runbook_hybrid_keyword_weight",
    "OPS_RUNBOOK_RERANK_ENABLED": "ops_runbook_rerank_enabled",
    "OPS_RUNBOOK_RERANK_ENDPOINT": "ops_runbook_rerank_endpoint",
    "OPS_RUNBOOK_RERANK_API_KEY": "ops_runbook_rerank_api_key",
    "OPS_RUNBOOK_RERANK_MODEL": "ops_runbook_rerank_model",
    "OPS_RUNBOOK_RERANK_CANDIDATE_TOP_N": "ops_runbook_rerank_candidate_top_n",
    "OPS_RUNBOOK_RERANK_TIMEOUT_MS": "ops_runbook_rerank_timeout_ms",
    "GRAFANA_PROMETHEUS_DATASOURCE_UID": "ops_mcp_grafana_datasource_uid",
    "ELASTICSEARCH_MCP_SEARCH_TOOL": "ops_mcp_elasticsearch_search_tool_name",
}


def audit() -> dict[str, object]:
    placeholders = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", FULL_PROFILE.read_text(encoding="utf-8")))
    fields = set(Settings.model_fields)
    missing_mapping = sorted(placeholders - LEGACY_ENV_TO_FIELD.keys())
    invalid_fields = sorted(f"{key}->{field}" for key, field in LEGACY_ENV_TO_FIELD.items()
                            if key in placeholders and field not in fields)
    return {"legacyPlaceholders": len(placeholders), "mappedPlaceholders": len(placeholders) - len(missing_mapping),
            "missing": missing_mapping, "invalidFields": invalid_fields}


def main() -> int:
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["missing"] or result["invalidFields"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
