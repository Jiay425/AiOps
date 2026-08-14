CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_incident_state` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `state_id` varchar(64) NOT NULL COMMENT 'state id',
  `diagnosis_id` varchar(64) NOT NULL COMMENT 'diagnosis id',
  `session_id` varchar(64) NOT NULL COMMENT 'session id',
  `event_id` varchar(64) DEFAULT NULL COMMENT 'alert event id',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `severity` varchar(32) DEFAULT NULL COMMENT 'alert severity',
  `alert_rule` varchar(128) DEFAULT NULL COMMENT 'alert rule',
  `time_window_json` varchar(1000) DEFAULT NULL COMMENT 'diagnosis time window',
  `current_round` int NOT NULL DEFAULT 1 COMMENT 'current planning round',
  `max_rounds` int NOT NULL DEFAULT 2 COMMENT 'max planning rounds',
  `plan_json` longtext COMMENT 'latest investigation plan json',
  `metrics_evidence_json` longtext COMMENT 'metrics evidence json',
  `log_evidence_json` longtext COMMENT 'log evidence json',
  `trace_evidence_json` longtext COMMENT 'trace evidence json',
  `runbook_evidence_json` longtext COMMENT 'runbook evidence json',
  `candidate_root_causes_json` longtext COMMENT 'candidate root causes json',
  `missing_evidence_json` longtext COMMENT 'missing evidence json',
  `tool_history_json` longtext COMMENT 'tool history json',
  `review_status` varchar(32) DEFAULT NULL COMMENT 'review status',
  `final_report` longtext COMMENT 'final report',
  `status` varchar(32) NOT NULL COMMENT 'INIT/PLANNED/COLLECTING/SUCCESS/FAILED/DEGRADED',
  `error_message` varchar(2000) DEFAULT NULL COMMENT 'error message',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_state_id` (`state_id`),
  UNIQUE KEY `uk_diagnosis_id` (`diagnosis_id`),
  KEY `idx_event_id` (`event_id`),
  KEY `idx_service_status` (`service_name`, `status`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops agent incident state';

