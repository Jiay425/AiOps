CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_incident_diagnosis` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `diagnosis_id` varchar(64) NOT NULL COMMENT 'diagnosis id',
  `session_id` varchar(64) NOT NULL COMMENT 'sse session id',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `start_time` varchar(32) NOT NULL COMMENT 'incident start time',
  `end_time` varchar(32) NOT NULL COMMENT 'incident end time',
  `problem` varchar(2000) NOT NULL COMMENT 'user problem',
  `trace_id` varchar(128) DEFAULT NULL COMMENT 'optional trace id',
  `status` varchar(32) NOT NULL COMMENT 'SUCCESS/FAILED',
  `request_json` longtext COMMENT 'request command json',
  `metric_evidence_json` longtext COMMENT 'metric evidence json',
  `log_evidence_json` longtext COMMENT 'log evidence json',
  `trace_evidence_json` longtext COMMENT 'trace evidence json',
  `evidence_chain_json` longtext COMMENT 'root cause candidates and evidence json',
  `runbook_json` longtext COMMENT 'matched runbook json',
  `report` longtext COMMENT 'final diagnosis report',
  `error_message` varchar(2000) DEFAULT NULL COMMENT 'error message',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_diagnosis_id` (`diagnosis_id`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_service_time` (`service_name`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops incident diagnosis review record';

