CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_agent_skill` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `skill_id` varchar(128) NOT NULL COMMENT 'skill id',
  `name` varchar(256) NOT NULL COMMENT 'skill name',
  `category` varchar(64) NOT NULL COMMENT 'skill category',
  `matched_alert_rules` longtext COMMENT 'matched alert keywords json',
  `symptoms` longtext COMMENT 'symptoms json',
  `recommended_tools` longtext COMMENT 'recommended tools json',
  `key_metrics` longtext COMMENT 'key metrics json',
  `log_patterns` longtext COMMENT 'log patterns json',
  `trace_patterns` longtext COMMENT 'trace patterns json',
  `root_cause_rules` longtext COMMENT 'root cause rules json',
  `temporary_fixes` longtext COMMENT 'temporary fixes json',
  `long_term_fixes` longtext COMMENT 'long term fixes json',
  `runbook_path` varchar(512) DEFAULT NULL COMMENT 'mapped runbook path',
  `content` longtext COMMENT 'skill markdown content',
  `status` tinyint(1) NOT NULL DEFAULT 1 COMMENT '1 enabled, 0 disabled',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_skill_id` (`skill_id`),
  KEY `idx_category_status` (`category`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops agent structured skill metadata';

