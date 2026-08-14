CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_historical_incident_memory` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `memory_id` varchar(64) NOT NULL COMMENT 'historical memory id',
  `diagnosis_id` varchar(64) NOT NULL COMMENT 'source diagnosis id',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `alert_rule` varchar(500) DEFAULT NULL COMMENT 'alert rule or incident problem',
  `severity` varchar(32) DEFAULT NULL COMMENT 'alert severity',
  `symptom_summary` varchar(1000) DEFAULT NULL COMMENT 'compact symptom summary',
  `evidence_summary` text COMMENT 'compact evidence summary',
  `root_cause_category` varchar(128) DEFAULT NULL COMMENT 'root cause category',
  `root_cause_summary` varchar(1500) DEFAULT NULL COMMENT 'root cause summary',
  `remediation_summary` text COMMENT 'remediation summary',
  `confidence` int DEFAULT 0 COMMENT 'root cause confidence',
  `review_status` varchar(64) DEFAULT NULL COMMENT 'review status',
  `time_window_json` varchar(500) DEFAULT NULL COMMENT 'incident time window',
  `tags` varchar(1000) DEFAULT NULL COMMENT 'search tags',
  `similarity_text` longtext COMMENT 'text used for similar incident retrieval',
  `source_record_json` longtext COMMENT 'source diagnosis record snapshot',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_memory_id` (`memory_id`),
  UNIQUE KEY `uk_diagnosis_id` (`diagnosis_id`),
  KEY `idx_service_time` (`service_name`, `create_time`),
  KEY `idx_root_cause` (`root_cause_category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops historical incident memory cards';

