CREATE database if NOT EXISTS `ops-autoagent-diagnosis` default character set utf8mb4 collate utf8mb4_0900_ai_ci;
use `ops-autoagent-diagnosis`;

CREATE TABLE IF NOT EXISTS `ops_agent_review` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'primary key',
  `review_id` varchar(64) NOT NULL COMMENT 'review id',
  `diagnosis_id` varchar(64) NOT NULL COMMENT 'diagnosis id',
  `state_id` varchar(64) DEFAULT NULL COMMENT 'incident state id',
  `plan_id` varchar(64) DEFAULT NULL COMMENT 'investigation plan id',
  `round` int NOT NULL DEFAULT 1 COMMENT 'review round',
  `review_status` varchar(64) NOT NULL COMMENT 'review status',
  `sufficient` tinyint(1) DEFAULT NULL COMMENT 'whether evidence is sufficient',
  `confidence` int DEFAULT NULL COMMENT 'review confidence score',
  `confirmed_facts_json` longtext COMMENT 'confirmed facts json',
  `weak_evidence_json` longtext COMMENT 'weak evidence json',
  `missing_evidence_json` longtext COMMENT 'missing evidence json',
  `next_actions_json` longtext COMMENT 'next actions json',
  `report_constraints_json` longtext COMMENT 'report constraints json',
  `stop_reason` varchar(500) DEFAULT NULL COMMENT 'stop or continuation reason',
  `reviewer_type` varchar(64) NOT NULL COMMENT 'RULE_BASED/LLM',
  `review_json` longtext COMMENT 'full review result json',
  `create_time` datetime NOT NULL COMMENT 'create time',
  `update_time` datetime NOT NULL COMMENT 'update time',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_review_id` (`review_id`),
  KEY `idx_diagnosis_round` (`diagnosis_id`, `round`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_state_id` (`state_id`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ops agent evidence review audit log';

