CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_diagnosis_dispatch` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `dispatch_id` varchar(64) NOT NULL COMMENT 'dispatch id',
  `event_id` varchar(64) NOT NULL COMMENT 'alert event id',
  `diagnosis_id` varchar(64) DEFAULT NULL COMMENT 'linked diagnosis id',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `dedup_key` varchar(512) NOT NULL COMMENT 'dedup key',
  `dispatch_status` varchar(32) NOT NULL COMMENT 'NEW/RUNNING/SKIPPED/SUCCESS/FAILED',
  `skip_reason` varchar(1000) DEFAULT NULL COMMENT 'reason when skipped or failed',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `start_time` datetime DEFAULT NULL COMMENT 'dispatch start time',
  `end_time` datetime DEFAULT NULL COMMENT 'dispatch end time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dispatch_id` (`dispatch_id`),
  KEY `idx_event_id` (`event_id`),
  KEY `idx_dedup_time` (`dedup_key`, `create_time`),
  KEY `idx_service_status` (`service_name`, `dispatch_status`),
  KEY `idx_diagnosis_id` (`diagnosis_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops diagnosis dispatch job';

