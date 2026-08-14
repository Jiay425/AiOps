CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_tool_policy` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `tool_name` varchar(64) NOT NULL COMMENT 'tool name, for example query_prometheus',
  `agent_role` varchar(64) NOT NULL DEFAULT '*' COMMENT 'agent role, exact role or *',
  `enabled` tinyint(1) NOT NULL DEFAULT 1 COMMENT 'whether this tool is enabled',
  `max_calls_per_diagnosis` int NOT NULL DEFAULT 2 COMMENT 'max calls per diagnosis for this tool',
  `timeout_seconds` int NOT NULL DEFAULT 30 COMMENT 'recommended timeout seconds',
  `required_severity` varchar(16) DEFAULT NULL COMMENT 'minimum incident severity required, for example P1/P2/P3',
  `allow_auto_execute` tinyint(1) NOT NULL DEFAULT 1 COMMENT 'whether agent can auto execute this tool',
  `requires_approval` tinyint(1) NOT NULL DEFAULT 0 COMMENT 'whether manual approval is required',
  `description` varchar(512) DEFAULT NULL COMMENT 'policy description',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'create time',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_tool_role` (`tool_name`, `agent_role`),
  KEY `idx_tool_name` (`tool_name`),
  KEY `idx_enabled` (`enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops tool policy';

INSERT INTO `ops_tool_policy`
(`tool_name`, `agent_role`, `enabled`, `max_calls_per_diagnosis`, `timeout_seconds`, `required_severity`, `allow_auto_execute`, `requires_approval`, `description`)
VALUES
('query_prometheus', 'Metrics Agent', 1, 3, 15, 'P3', 1, 0, 'Prometheus metrics collection is allowed for alert diagnosis'),
('query_elasticsearch', 'Logs Agent', 1, 3, 20, 'P3', 1, 0, 'Elasticsearch log collection is allowed for alert diagnosis'),
('query_skywalking_trace', 'Trace Agent', 1, 3, 20, 'P3', 1, 0, 'SkyWalking trace collection is allowed for alert diagnosis'),
('query_runbook', 'Runbook Agent', 1, 2, 10, 'P3', 1, 0, 'Runbook and skill retrieval is allowed for diagnosis'),
('llm_evidence_reviewer', 'evidence-reviewer-agent', 1, 2, 30, 'P3', 1, 0, 'LLM evidence review is allowed when ChatModel is available'),
('llm', 'report-writer-agent', 1, 2, 60, 'P3', 1, 0, 'LLM report generation is allowed when ChatModel is available')
ON DUPLICATE KEY UPDATE
  `description` = VALUES(`description`),
  `update_time` = CURRENT_TIMESTAMP;

