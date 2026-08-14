use `ops-autoagent-diagnosis`;

ALTER TABLE `ops_tool_call_log`
  ADD COLUMN `logical_tool_name` varchar(128) DEFAULT NULL COMMENT 'logical tool name, for example query_elasticsearch',
  ADD COLUMN `protocol` varchar(64) DEFAULT NULL COMMENT 'tool protocol, for example ELASTICSEARCH_MCP',
  ADD COLUMN `governance_decision` varchar(64) DEFAULT NULL COMMENT 'governance decision, for example ALLOW/DENY/SUCCESS/FAILED';

