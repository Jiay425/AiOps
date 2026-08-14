CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_audit_log` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `audit_id` varchar(64) NOT NULL COMMENT 'audit id',
  `session_id` varchar(64) DEFAULT NULL COMMENT 'sse session id',
  `diagnosis_id` varchar(64) DEFAULT NULL COMMENT 'diagnosis id',
  `operator_id` varchar(128) DEFAULT NULL COMMENT 'operator id',
  `client_ip` varchar(64) DEFAULT NULL COMMENT 'client ip',
  `action` varchar(64) NOT NULL COMMENT 'action',
  `resource` varchar(256) DEFAULT NULL COMMENT 'resource',
  `request_json` longtext COMMENT 'masked request json',
  `result` varchar(32) NOT NULL COMMENT 'ALLOW/DENY/SUCCESS/FAILED',
  `reason` varchar(1000) DEFAULT NULL COMMENT 'reason',
  `create_time` datetime NOT NULL COMMENT 'create time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_audit_id` (`audit_id`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_diagnosis_id` (`diagnosis_id`),
  KEY `idx_action_time` (`action`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops audit log';

CREATE TABLE IF NOT EXISTS `ops_tool_call_log` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `call_id` varchar(64) NOT NULL COMMENT 'tool call id',
  `session_id` varchar(64) DEFAULT NULL COMMENT 'sse session id',
  `diagnosis_id` varchar(64) DEFAULT NULL COMMENT 'diagnosis id',
  `tool_name` varchar(64) NOT NULL COMMENT 'tool name',
  `logical_tool_name` varchar(128) DEFAULT NULL COMMENT 'logical tool name, for example query_elasticsearch',
  `protocol` varchar(64) DEFAULT NULL COMMENT 'tool protocol, for example ELASTICSEARCH_MCP',
  `governance_decision` varchar(64) DEFAULT NULL COMMENT 'governance decision, for example ALLOW/DENY/SUCCESS/FAILED',
  `target` varchar(256) DEFAULT NULL COMMENT 'target endpoint or resource',
  `request_summary` longtext COMMENT 'masked request summary',
  `response_summary` longtext COMMENT 'masked response summary',
  `status_code` int DEFAULT NULL COMMENT 'http status code',
  `cost_millis` bigint DEFAULT NULL COMMENT 'call cost millis',
  `success` varchar(16) NOT NULL COMMENT 'true/false',
  `error_message` varchar(2000) DEFAULT NULL COMMENT 'error message',
  `create_time` datetime NOT NULL COMMENT 'create time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_call_id` (`call_id`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_diagnosis_id` (`diagnosis_id`),
  KEY `idx_tool_time` (`tool_name`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops tool call log';

