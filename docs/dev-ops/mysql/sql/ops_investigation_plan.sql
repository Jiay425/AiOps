CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_investigation_plan` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `plan_id` varchar(64) NOT NULL COMMENT 'plan id',
  `diagnosis_id` varchar(64) NOT NULL COMMENT 'diagnosis id',
  `state_id` varchar(64) NOT NULL COMMENT 'state id',
  `round` int NOT NULL DEFAULT 1 COMMENT 'planning round',
  `alert_type` varchar(64) NOT NULL COMMENT 'classified alert type',
  `hypotheses_json` longtext COMMENT 'hypotheses json',
  `steps_json` longtext COMMENT 'investigation steps json',
  `required_tools_json` longtext COMMENT 'required tools json',
  `expected_evidence_json` longtext COMMENT 'expected evidence json',
  `risk_level` varchar(32) DEFAULT NULL COMMENT 'risk level',
  `budget_json` varchar(1000) DEFAULT NULL COMMENT 'plan budget json',
  `plan_json` longtext COMMENT 'full plan json',
  `planner_type` varchar(64) NOT NULL COMMENT 'RULE_BASED/LLM',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_plan_id` (`plan_id`),
  KEY `idx_diagnosis_round` (`diagnosis_id`, `round`),
  KEY `idx_state_id` (`state_id`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops agent investigation plan';

