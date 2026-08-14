-- Ops AutoAgent clean ChatClient configuration.
-- This file is safe to commit: it contains no real API keys.
-- Values like ${OPENAI_API_KEY:} are resolved from environment variables by AiClientApiNode at startup.

CREATE TABLE IF NOT EXISTS `ai_client_api` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `api_id` varchar(64) NOT NULL COMMENT 'API config id',
  `base_url` varchar(255) NOT NULL COMMENT 'OpenAI compatible base url',
  `api_key` varchar(255) NOT NULL COMMENT 'API key or environment placeholder',
  `completions_path` varchar(128) NOT NULL DEFAULT '/v1/chat/completions' COMMENT 'Chat completions path',
  `embeddings_path` varchar(128) NOT NULL DEFAULT '/v1/embeddings' COMMENT 'Embeddings path',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_api_id` (`api_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_model` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `model_id` varchar(64) NOT NULL COMMENT 'Model config id',
  `api_id` varchar(64) NOT NULL COMMENT 'API config id',
  `model_name` varchar(128) NOT NULL COMMENT 'Model name',
  `model_type` varchar(32) NOT NULL DEFAULT 'chat' COMMENT 'chat/embedding',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_model_id` (`model_id`),
  KEY `idx_api_id` (`api_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `client_id` varchar(64) NOT NULL COMMENT 'ChatClient id',
  `client_name` varchar(128) NOT NULL COMMENT 'ChatClient name',
  `description` varchar(512) DEFAULT NULL COMMENT 'Description',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_client_id` (`client_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_system_prompt` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `prompt_id` varchar(64) NOT NULL COMMENT 'Prompt id',
  `prompt_name` varchar(128) NOT NULL COMMENT 'Prompt name',
  `prompt_content` text NOT NULL COMMENT 'System prompt',
  `description` varchar(512) DEFAULT NULL COMMENT 'Description',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_prompt_id` (`prompt_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_advisor` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `advisor_id` varchar(64) NOT NULL COMMENT 'Advisor id',
  `advisor_name` varchar(128) NOT NULL COMMENT 'Advisor name',
  `advisor_type` varchar(64) NOT NULL COMMENT 'Advisor type',
  `order_num` int NOT NULL DEFAULT 0 COMMENT 'Execution order',
  `ext_param` text DEFAULT NULL COMMENT 'Extension parameters',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_advisor_id` (`advisor_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_tool_mcp` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `mcp_id` varchar(64) NOT NULL COMMENT 'MCP id',
  `mcp_name` varchar(128) NOT NULL COMMENT 'MCP name',
  `transport_type` varchar(32) NOT NULL COMMENT 'stdio/sse',
  `transport_config` text NOT NULL COMMENT 'Transport config json',
  `request_timeout` int NOT NULL DEFAULT 30 COMMENT 'Request timeout seconds',
  `status` tinyint NOT NULL DEFAULT 0 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_mcp_id` (`mcp_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_rag_order` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `rag_id` varchar(64) NOT NULL COMMENT 'RAG id',
  `rag_name` varchar(128) NOT NULL COMMENT 'RAG name',
  `knowledge_tag` varchar(128) NOT NULL COMMENT 'Knowledge tag',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_rag_id` (`rag_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ai_client_config` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `source_type` varchar(32) NOT NULL COMMENT 'client/model/prompt/tool_mcp/advisor/api',
  `source_id` varchar(64) NOT NULL COMMENT 'Source id',
  `target_type` varchar(32) NOT NULL COMMENT 'client/model/prompt/tool_mcp/advisor/api',
  `target_id` varchar(64) NOT NULL COMMENT 'Target id',
  `ext_param` text DEFAULT NULL COMMENT 'Extension parameters',
  `status` tinyint NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_source` (`source_type`, `source_id`),
  KEY `idx_target` (`target_type`, `target_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `ai_client_api` (`api_id`, `base_url`, `api_key`, `completions_path`, `embeddings_path`, `status`)
VALUES ('1001', '${OPENAI_BASE_URL:https://api.openai.com}', '${OPENAI_API_KEY:}', '/v1/chat/completions', '/v1/embeddings', 1)
ON DUPLICATE KEY UPDATE
  `base_url` = VALUES(`base_url`),
  `api_key` = VALUES(`api_key`),
  `completions_path` = VALUES(`completions_path`),
  `embeddings_path` = VALUES(`embeddings_path`),
  `status` = VALUES(`status`);

INSERT INTO `ai_client_model` (`model_id`, `api_id`, `model_name`, `model_type`, `status`)
VALUES ('2001', '1001', 'gpt-4o-mini', 'chat', 1)
ON DUPLICATE KEY UPDATE
  `api_id` = VALUES(`api_id`),
  `model_name` = VALUES(`model_name`),
  `model_type` = VALUES(`model_type`),
  `status` = VALUES(`status`);

INSERT INTO `ai_client_system_prompt` (`prompt_id`, `prompt_name`, `prompt_content`, `description`, `status`)
VALUES
  ('3001', 'PlannerAgentPrompt', 'You are the planning agent for an ops incident. Produce a structured investigation plan with hypotheses, allowed tools, expected evidence and call budget.', 'Planner agent system prompt', 1),
  ('3002', 'EvidenceReviewerAgentPrompt', 'You are the evidence reviewer agent. Judge whether collected metrics, logs, traces and runbooks are sufficient. Return root-cause status, missing evidence and supplemental tools only when useful.', 'Evidence reviewer agent system prompt', 1),
  ('3003', 'ReportWriterAgentPrompt', 'You are the report writer agent. Generate a concise diagnosis report grounded only in supplied evidence, reviewer judgement and runbook references.', 'Report writer agent system prompt', 1)
ON DUPLICATE KEY UPDATE
  `prompt_name` = VALUES(`prompt_name`),
  `prompt_content` = VALUES(`prompt_content`),
  `description` = VALUES(`description`),
  `status` = VALUES(`status`);

INSERT INTO `ai_client` (`client_id`, `client_name`, `description`, `status`)
VALUES
  ('4101', 'PlannerAgent', 'Plans investigation steps for Alertmanager incidents.', 1),
  ('4102', 'EvidenceReviewerAgent', 'Reviews evidence sufficiency and supplemental evidence needs.', 1),
  ('4103', 'ReportWriterAgent', 'Writes the final diagnosis report.', 1),
  ('4104', 'OpsFallbackAgent', 'Reserved fallback chat client for manual diagnosis tasks.', 1),
  ('5101', 'EvalPlannerAgent', 'Evaluation planner agent.', 1),
  ('5102', 'EvalEvidenceReviewerAgent', 'Evaluation evidence reviewer agent.', 1),
  ('5103', 'EvalReportWriterAgent', 'Evaluation report writer agent.', 1),
  ('5104', 'EvalFallbackAgent', 'Evaluation fallback chat client.', 1)
ON DUPLICATE KEY UPDATE
  `client_name` = VALUES(`client_name`),
  `description` = VALUES(`description`),
  `status` = VALUES(`status`);

DELETE FROM `ai_client_config`
WHERE `source_type` = 'client'
  AND `source_id` IN ('4101', '4102', '4103', '4104', '5101', '5102', '5103', '5104');

INSERT INTO `ai_client_config` (`source_type`, `source_id`, `target_type`, `target_id`, `status`)
VALUES
  ('client', '4101', 'model', '2001', 1),
  ('client', '4101', 'prompt', '3001', 1),
  ('client', '4102', 'model', '2001', 1),
  ('client', '4102', 'prompt', '3002', 1),
  ('client', '4103', 'model', '2001', 1),
  ('client', '4103', 'prompt', '3003', 1),
  ('client', '4104', 'model', '2001', 1),
  ('client', '4104', 'prompt', '3002', 1),
  ('client', '5101', 'model', '2001', 1),
  ('client', '5101', 'prompt', '3001', 1),
  ('client', '5102', 'model', '2001', 1),
  ('client', '5102', 'prompt', '3002', 1),
  ('client', '5103', 'model', '2001', 1),
  ('client', '5103', 'prompt', '3003', 1),
  ('client', '5104', 'model', '2001', 1),
  ('client', '5104', 'prompt', '3002', 1);
