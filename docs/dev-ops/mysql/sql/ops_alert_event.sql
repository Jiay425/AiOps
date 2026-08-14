CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_alert_event` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `event_id` varchar(64) NOT NULL COMMENT 'internal alert event id',
  `source` varchar(32) NOT NULL COMMENT 'alert source, for example alertmanager',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `alert_rule` varchar(128) NOT NULL COMMENT 'alert rule name',
  `severity` varchar(32) DEFAULT NULL COMMENT 'severity such as P1/P2/P3',
  `status` varchar(32) NOT NULL COMMENT 'firing/resolved',
  `fingerprint` varchar(256) DEFAULT NULL COMMENT 'upstream alert fingerprint',
  `trace_id` varchar(128) DEFAULT NULL COMMENT 'optional trace id',
  `starts_at` datetime DEFAULT NULL COMMENT 'alert start time',
  `ends_at` datetime DEFAULT NULL COMMENT 'alert end time',
  `labels_json` longtext COMMENT 'normalized labels json',
  `annotations_json` longtext COMMENT 'normalized annotations json',
  `raw_payload` longtext COMMENT 'raw webhook payload',
  `received_time` datetime NOT NULL COMMENT 'event received time',
  `create_time` datetime NOT NULL COMMENT 'create time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_event_id` (`event_id`),
  KEY `idx_service_received` (`service_name`, `received_time`),
  KEY `idx_fingerprint` (`fingerprint`),
  KEY `idx_rule_status_time` (`alert_rule`, `status`, `received_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops alert webhook event';

