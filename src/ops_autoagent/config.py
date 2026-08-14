from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore", populate_by_name=True)

    ops_host: str = "0.0.0.0"
    ops_port: int = 8099
    thread_pool_core_size: int = 20
    thread_pool_max_size: int = 50
    thread_pool_keep_alive_ms: int = 5000
    thread_pool_queue_size: int = 5000
    thread_pool_rejection_policy: str = "CallerRunsPolicy"
    ops_database_path: Path = Path(".data/ops-autoagent.db")
    langgraph_checkpoint_backend: str = "sqlite"
    langgraph_checkpoint_path: Path = Path(".data/langgraph-checkpoints.db")
    langgraph_checkpoint_postgres_url: str = ""
    ops_runbook_path: Path = Field(Path("docs/dev-ops/runbook"),
                                   validation_alias=AliasChoices("OPS_RUNBOOK_PATH", "OPS_RUNBOOK_BASE_PATH"))
    ops_fixture_fallback: bool = True
    ops_api_token: str = ""
    ops_security_enabled: bool = False
    ops_rate_limit_enabled: bool = True
    ops_rate_limit_max_requests: int = 20
    ops_rate_limit_window_seconds: int = 60
    ops_alert_dedup_window_minutes: int = 5
    ops_alert_max_step: int = 9
    ops_agent_enabled: bool = True
    ops_agent_max_rounds: int = 2
    ops_agent_max_tool_calls: int = 12
    ops_agent_reviewer_min_confidence: int = 75
    ops_agent_planner_enabled: bool = True
    ops_agent_reviewer_enabled: bool = True
    ops_agent_chat_enabled: bool = True
    ops_agent_chat_required: bool = True
    ops_agent_chat_use_configured_client: bool = True
    ops_agent_chat_client_ready_timeout_ms: int = 120000
    ops_agent_chat_planner_client_id: str = "4101"
    ops_agent_chat_reviewer_client_id: str = "4102"
    ops_agent_chat_report_writer_client_id: str = "4103"
    ops_agent_plan_driven_enabled: bool = True
    ops_agent_tool_policy_enabled: bool = True
    ops_agent_skill_enabled: bool = True
    ops_agent_skill_base_path: Path = Path("docs/dev-ops/skills")
    ops_agent_evaluation_enabled: bool = True
    ops_tool_policy_disabled_tools: str = ""
    ops_tool_policy_max_repeat_per_tool: int = 2
    openai_base_url: str = "https://api.pie-xian.com"
    openai_api_key: str = ""
    openai_model: str = Field("deepseek-v4-flash",
                              validation_alias=AliasChoices("OPENAI_MODEL", "OPENAI_CHAT_MODEL"))
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_username: str = ""
    prometheus_password: str = ""
    elk_base_url: str = ""
    elk_index_pattern: str = "logs-*"
    elk_username: str = ""
    elk_password: str = ""
    skywalking_graphql_url: str = ""
    skywalking_username: str = ""
    skywalking_password: str = ""
    integration_timeout_seconds: float = 15.0
    integration_connect_timeout_seconds: float = 5.0
    ops_mcp_prefer: bool = False
    ops_mcp_fallback_http: bool = True
    ops_mcp_grafana_url: str = ""
    ops_mcp_elasticsearch_url: str = ""
    ops_mcp_grafana_id: int = 5008
    ops_mcp_elasticsearch_id: int = 5007
    ops_mcp_grafana_query_tool_name: str = "query_prometheus"
    ops_mcp_grafana_datasource_uid: str = Field(
        "", validation_alias=AliasChoices("OPS_MCP_GRAFANA_DATASOURCE_UID", "GRAFANA_PROMETHEUS_DATASOURCE_UID"))
    ops_mcp_elasticsearch_search_tool_name: str = Field(
        "search", validation_alias=AliasChoices("OPS_MCP_ELASTICSEARCH_SEARCH_TOOL_NAME",
                                                 "ELASTICSEARCH_MCP_SEARCH_TOOL"))
    ops_runbook_chunk_size: int = 180
    ops_runbook_chunk_overlap: int = 150
    ops_runbook_chunk_min_size_chars: int = 120
    ops_runbook_chunk_min_length_to_embed: int = 60
    ops_runbook_chunk_max_num_chunks: int = 80
    ops_runbook_chunk_keep_separator: bool = True
    ops_runbook_hybrid_enabled: bool = True
    ops_runbook_hybrid_keyword_weight: float = 1.3
    ops_runbook_hybrid_vector_weight: float = 1.0
    ops_runbook_hybrid_rrf_k: int = 60
    ops_runbook_rerank_enabled: bool = True
    ops_runbook_rerank_endpoint: str = "https://api.pie-xian.com/v1/rerank"
    ops_runbook_rerank_api_key: str = ""
    ops_runbook_rerank_model: str = "qwen3-reranker-8b"
    ops_runbook_rerank_candidate_top_n: int = 20
    ops_runbook_rerank_timeout_ms: int = 30000
    ops_runbook_vector_fallback_to_file: bool = True
    ops_embedding_base_url: str = Field(
        "", validation_alias=AliasChoices("OPS_EMBEDDING_BASE_URL", "OPS_RUNBOOK_EMBEDDING_BASE_URL"))
    ops_embedding_api_key: str = Field(
        "", validation_alias=AliasChoices("OPS_EMBEDDING_API_KEY", "OPS_RUNBOOK_EMBEDDING_API_KEY"))
    ops_embedding_model: str = Field(
        "qwen3-embedding-8b", validation_alias=AliasChoices("OPS_EMBEDDING_MODEL",
                                                            "OPS_RUNBOOK_EMBEDDING_MODEL",
                                                            "OPENAI_EMBEDDING_MODEL"))
    ops_embedding_dimensions: int = Field(
        768, validation_alias=AliasChoices("OPS_EMBEDDING_DIMENSIONS",
                                           "OPS_RUNBOOK_EMBEDDING_DIMENSIONS",
                                           "OPENAI_EMBEDDING_DIMENSIONS"))
    ops_runbook_vector_enabled: bool = True
    ops_runbook_vector_rebuild_on_startup: bool = False
    ops_runbook_vector_schema_check_on_startup: bool = True
    ops_runbook_vector_fail_fast: bool = False
    ops_runbook_vector_index_batch_size: int = 8
    ops_runbook_vector_index_batch_retries: int = 3
    pgvector_url: str = ""
    pgvector_username: str = "postgres"
    pgvector_password: str = "postgres"
    pgvector_table: str = Field("vector_store_openai",
                                validation_alias=AliasChoices("PGVECTOR_TABLE", "OPENAI_VECTOR_TABLE"))
    mysql_url: str = ""
    mysql_username: str = "root"
    mysql_password: str = ""
    mysql_pool_min_size: int = 15
    mysql_pool_max_size: int = 25
    mysql_pool_recycle_seconds: int = 1800
    mysql_pool_connect_timeout_seconds: int = 30
    ops_mail_host: str = "smtp.qq.com"
    ops_mail_port: int = 587
    ops_mail_username: str = ""
    ops_mail_password: str = ""
    ops_mail_auth: bool = True
    ops_mail_starttls: bool = True
    ops_mail_timeout_seconds: float = 10.0
    ops_notify_enabled: bool = True
    ops_notify_email_enabled: bool = True
    ops_notify_subject_prefix: str = "[AutoAgent]"
    ops_notify_app_base_url: str = "http://127.0.0.1:8099"
    ops_demo_auto_seed_enabled: bool = True
    ops_demo_auto_seed_app_base_url: str = "http://127.0.0.1:8099"
    ops_demo_auto_seed_elasticsearch_index: str = "ops-demo-service-log-auto-demo"
    ops_demo_auto_seed_service_name: str = "ops-demo-service"
    ops_demo_auto_seed_start_delay_seconds: int = 3
    ops_demo_auto_seed_error_count: int = 12
    ops_demo_auto_seed_slow_count: int = 6
    ops_demo_auto_seed_db_count: int = 3
    ops_demo_auto_seed_trace_id: str = ""
    ops_demo_auto_seed_elasticsearch_max_attempts: int = 12
    ops_demo_auto_seed_elasticsearch_retry_interval_seconds: int = 5
    codeops_patch_sandbox_enabled: bool = True
    codeops_patch_sandbox_base_dir: str = ""
    codeops_patch_sandbox_prefer_git_worktree: bool = False
    codeops_patch_sandbox_timeout_ms: int = 30000
    codeops_llm_flash_model: str = ""
    codeops_llm_pro_model: str = ""
    codeops_llm_pro_escalation_enabled: bool = False
    codeops_test_execution_enabled: bool = False
    codeops_test_execution_timeout_ms: int = 120000
    codeops_agent_test_verification_llm_enabled: bool = True
    codeops_agent_test_patch_llm_enabled: bool = True
    codeops_agent_test_patch_max_snippets: int = 4
    codeops_agent_bugfix_llm_enabled: bool = True
    codeops_agent_bugfix_max_snippets: int = 12
    codeops_agent_bugfix_max_knowledge: int = 5
    codeops_agent_release_risk_llm_enabled: bool = True
    codeops_agent_release_risk_max_knowledge: int = 5
    codeops_bugfix_compile_timeout_ms: int = 300000
    codeops_incident_to_fix_alert_enabled: bool = True
    codeops_scheduler_max_concurrent: int = 3
    codeops_scheduler_max_per_service: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
