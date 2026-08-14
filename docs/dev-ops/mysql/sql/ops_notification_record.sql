CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_notification_record` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `notification_id` varchar(64) NOT NULL COMMENT 'notification id',
  `diagnosis_id` varchar(64) DEFAULT NULL COMMENT 'diagnosis id',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `channel` varchar(32) NOT NULL COMMENT 'EMAIL/WECOM/DINGTALK',
  `receiver` varchar(256) DEFAULT NULL COMMENT 'notification receiver',
  `severity` varchar(32) DEFAULT NULL COMMENT 'alert severity',
  `subject` varchar(512) DEFAULT NULL COMMENT 'notification subject',
  `send_status` varchar(32) NOT NULL COMMENT 'SUCCESS/FAILED/SKIPPED',
  `retry_count` int NOT NULL DEFAULT 0 COMMENT 'retry count',
  `error_message` varchar(2000) DEFAULT NULL COMMENT 'send error message',
  `send_time` datetime NOT NULL COMMENT 'send time',
  `create_time` datetime NOT NULL COMMENT 'create time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_notification_id` (`notification_id`),
  KEY `idx_diagnosis_id` (`diagnosis_id`),
  KEY `idx_service_time` (`service_name`, `send_time`),
  KEY `idx_channel_status` (`channel`, `send_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops notification send record';

