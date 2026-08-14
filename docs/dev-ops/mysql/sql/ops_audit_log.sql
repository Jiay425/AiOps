CREATE TABLE IF NOT EXISTS `ops_audit_log` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT 'Primary key',
  `audit_id` varchar(64) NOT NULL COMMENT 'Audit id',
  `session_id` varchar(64) DEFAULT NULL COMMENT 'Session id',
  `diagnosis_id` varchar(64) DEFAULT NULL COMMENT 'Diagnosis id',
  `operator_id` varchar(64) DEFAULT NULL COMMENT 'Operator id',
  `client_ip` varchar(64) DEFAULT NULL COMMENT 'Client ip',
  `action` varchar(128) NOT NULL COMMENT 'Action',
  `resource` varchar(255) DEFAULT NULL COMMENT 'Resource',
  `request_json` text DEFAULT NULL COMMENT 'Request json',
  `result` varchar(64) DEFAULT NULL COMMENT 'Result',
  `reason` varchar(512) DEFAULT NULL COMMENT 'Reason',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_audit_id` (`audit_id`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_diagnosis_id` (`diagnosis_id`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
