CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_service_owner` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `service_name` varchar(128) NOT NULL COMMENT 'service name',
  `owner_name` varchar(128) DEFAULT NULL COMMENT 'owner name',
  `owner_email` varchar(256) DEFAULT NULL COMMENT 'primary owner email',
  `owner_wecom` varchar(128) DEFAULT NULL COMMENT 'primary owner wecom id',
  `owner_dingtalk` varchar(128) DEFAULT NULL COMMENT 'primary owner dingtalk id',
  `backup_owner_email` varchar(256) DEFAULT NULL COMMENT 'backup owner email',
  `enabled` tinyint(1) NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_service_name` (`service_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops service owner mapping';

INSERT INTO `ops_service_owner` (
  `service_name`, `owner_name`, `owner_email`, `backup_owner_email`, `enabled`, `create_time`, `update_time`
) VALUES (
  'ops-demo-service', 'ops-owner', 'ops-owner@example.com', 'ops-owner@example.com', 1, NOW(), NOW()
)
ON DUPLICATE KEY UPDATE
  `owner_name` = VALUES(`owner_name`),
  `owner_email` = VALUES(`owner_email`),
  `backup_owner_email` = VALUES(`backup_owner_email`),
  `enabled` = VALUES(`enabled`),
  `update_time` = NOW();

