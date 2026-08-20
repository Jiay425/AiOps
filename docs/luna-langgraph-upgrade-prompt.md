# Luna 实施提示词：LangGraph AIOps 升级（M2–M3）

将下方完整提示词原样发送给 Luna，并让它在当前仓库根目录执行。

~~~text
你是一名资深 Python、LangGraph、FastAPI、异步系统、Agent 评估和安全自动化工程师。请在仓库：
E:\DeskTop\java_project\ops-autoagent-diagnosis-python
中继续实施 LangGraph AIOps 升级的 M2 和 M3。

唯一设计规格是 docs/langgraph-upgrade-roadmap.md。开始前必须完整阅读：
1. pyproject.toml
2. docs/langgraph-migration.md
3. docs/langgraph-upgrade-roadmap.md
4. src/ops_autoagent/graphs/ops.py
5. src/ops_autoagent/graphs/codeops.py
6. src/ops_autoagent/graphs/state_models.py（若 M0/M1 已新建）
7. src/ops_autoagent/api.py
8. src/ops_autoagent/persistence.py
9. src/ops_autoagent/config.py 和 .env.example
10. src/ops_autoagent/codeops/runtime.py
11. src/ops_autoagent/codeops/services.py
12. src/ops_autoagent/codeops/test_verification.py
13. src/ops_autoagent/schemas.py
14. tests/

前置条件：
- M0/M1 声称已完成。不要假设它们正确；先审计真实 interrupt/resume、审批后的唯一 effect boundary、只读通用 AgentLoop、reducer State、Ops fan-out/fan-in、CodeOps SSE。
- 如发现 M0/M1 存在阻断问题，例如未审批写入、resume 无效、并发 State 冲突、事件泄密或测试失败，先做最小修复并报告；不得在不安全基础上堆叠 M2/M3。
- 保留旧 HTTP 路由、camelCase DTO、legacy Store 映射、Spring 迁移兼容、既有 fixtures、业务 Case 和 M0/M1 的行为。

本轮目标：
实施 M2：子图化与 Independent Reviewer -> Repair 闭环；
实施 M3：可观测性、运行时评估、灰度发布和回滚治理。
不增加无约束群聊 Agent，不修改已有评估分数，不伪造模型/测试/外部服务结果。

严格不变量：
- 父图是唯一控制平面，负责预算、审批、最终状态和跨子图路由。
- 多 Agent 分工保持清楚：Ops/调查、修复、独立审查分离；工具、校验、测试运行不冒充 Agent。
- 子图是领域边界，不是形式化拆分。仅抽取具有独立输入、输出、循环与测试边界的能力。
- Ops 与仓库调查子图只读；Repair 子图最多写受管 PatchSandbox；目标仓库写入仍只能由 M0 审批后的 apply effect node 完成。
- Reviewer 不得修改 PatchProposal、调用写工具或覆盖 deterministic patch facts；只输出结构化 verdict、风险和 retryInstructions。
- RETRY_REPAIR 必须带结构化反馈回到 Repair；不得只生成报告。
- 所有修复循环受 repairAttempt、maxToolCalls、maxRounds、LLM budget、patchDigest 去重和 Scope Guard 约束。
- 定位不足、Scope Guard 失败、重复补丁、真实测试连续失败、预算耗尽或审批拒绝都必须有确定的停止/人工接管状态。
- State 保留摘要和 artifact IDs；完整 prompt、代码 diff、大工具响应和敏感日志写 Store/工件，不无限复制到 checkpoint。
- 不重命名会影响已暂停线程恢复的 node；如必须兼容，提供适配节点。
- 不使用 destructive git 命令，也不覆盖用户已有改动。

阶段 A：前置审计，先不编辑
1. 检查 git status、feature flags、当前 graph node/edge、State schema 与 M0/M1 测试。
2. 验证真实 approval interrupt/resume、无未审批目标仓库写入、reducer 并行采证、CodeOps SSE/replay。
3. 绘制当前父图和候选子图边界，列出每个子图的输入 State、输出 artifact、允许工具和副作用。
4. 审计 Release Risk 当前输出与路由，说明 RETRY_REPAIR 是否真的能够影响 Repair。
5. 输出文件级改动计划、风险、兼容策略和新增测试清单。
6. 无阻断后继续，不等待人工确认。

阶段 B：M2 子图化
使用 StateGraph 编译子图，由父图调用。保持稳定 node 名、显式输入/输出合同和 artifact 引用。

B1. OpsEvidenceSubgraph
- 输入：incident request、tool policy、预算、fixture/live 配置、已有 evidence references。
- 输出：EvidenceBundle，包含 metrics/logs/traces、provenance、signals、runbooks、EvidenceReviewContract、artifact refs。
- 保留 M1 fan-out/fan-in、reducer、只读观测工具和预算治理。
- 不生成代码补丁；父图只接收摘要/ID，完整证据落 Store。

B2. RepositoryInvestigationSubgraph
- 输入：goal、repository baseline、Ops evidence summary、focus areas、只读工具预算。
- 输出：InvestigationDecision：targetFiles、targetMethods、supportingEvidence、negativeEvidence、missingEvidence、scope 建议、shouldEnterCodeRepair。
- 仅执行只读 ReAct/Agent Loop；禁止 repo.exact_replace 和任何目标仓库写入。
- 低置信度或证据不足返回 NEED_MORE_EVIDENCE/LOCALIZATION_BLOCKED，由父图选择补证、风险交付或人工接管，不能直接进 Repair。

B3. RepairProposalSubgraph
- 输入：已授权 scope、可见代码摘要、engineering knowledge、上一轮 review feedback、repairAttempt。
- 输出：PatchProposalContract、patchDigest、Scope Guard、PatchValidation、PatchDiffAnalysis、sandbox/compile result、test proposal。
- 仅操作受管 PatchSandbox；M0 目标仓库 effect boundary 不变。
- retry 必须携带 failureType、mustFix、mustAvoid、nextAttemptConstraints、previousPatchDigest。
- Scope 扩展显式记录 request/decision，并重新运行 Scope Guard。

B4. VerificationSubgraph
- 输入：sandbox artifact、patchDigest、test plan、测试预算。
- 输出：真实 test result、failure diagnostic、background task state、验证 artifact refs。
- 复用 TestVerificationService；SKIPPED 不得等同真实通过；后台任务形成可恢复、可审计终态。

B5. IndependentReviewSubgraph
- 输入：Ops evidence、定位结论、patch facts、test facts、knowledge refs、历史 repair feedback。
- 输出：ReleaseReviewContract，至少包含 reviewVerdict、patchDecision、riskLevel、rootCauseAddressed、scopeSafe、testSufficient、retryInstructions、mustReview、humanApprovalPoints。
- 所有 LLM 输出先通过 Pydantic model_validate；fallback 明确标识 REVIEW_UNAVAILABLE，绝不伪装 RELEASE_READY。
- Reviewer 不修改 patch、不调用写工具、不违背 deterministic patch facts。

阶段 C：父图路由和 Reviewer -> Repair 闭环
1. 父图调用子图、写轻量 artifact reference、更新 budget/attempt，再通过条件边或 Command(update/goto) 路由。
2. 将 reviewer 结果映射为确定路线：
   - RELEASE_READY/ACCEPT：进入 M0 审批或交付；
   - ACCEPT_WITH_HUMAN_REVIEW/HUMAN_REVIEW：进入 M0 审批；
   - RETRY_REPAIR：未超 attempt、工具和 LLM 预算时，写 repair_feedback 后回 RepairProposalSubgraph；
   - REJECT：结束为 REVIEW_REJECTED 或转人工补证；
   - NO_CODE_FIX：输出运维/配置交付，不进入 PatchSandbox；
   - REVIEW_UNAVAILABLE：进入人工审批或明确失败，绝不自动应用。
3. 新增 CODEOPS_MAX_REPAIR_ATTEMPTS，默认 3；写入 config.py、.env.example 和任务快照。
4. 同一 patchDigest 被 Reviewer 或测试拒绝后，除非有新增证据或显式 scope 调整，否则阻止重复提交并要求人工接管。
5. repair feedback 必须结构化持久化，供下一 Repair、task trace、SSE 和评估使用。
6. 任何自动停止原因必须出现在任务状态、最终摘要、事件流和审计中。

阶段 D：M3 可观测性、评估、发布与回滚

D1. 可观测性
建立统一 Task/Artifact/Event 关联。task trace 与 dashboard 至少展示：
- taskId、threadId、runId、subgraph、node、attempt、artifactId、toolCallId；
- approvalId、approval status、patchDigest、reviewVerdict、blocked reason；
- 真实测试状态、sandbox、子图状态、恢复状态。
不得泄露 prompt、密钥、未脱敏工具参数或完整敏感响应。

新增运行时指标，不能替换已有业务指标：
- 各阶段/子图耗时、并行采证 latency、队列等待；
- LLM 调用数、模型/tier、成本估算、结构化输出失败；
- 工具调用、拒绝、超时、重试；
- evidence 覆盖、fixture fallback；
- repairAttempt、patchDigest 变化、重复补丁阻止、Scope Guard 拒绝；
- sandbox、真实 compile/test、后台任务等待；
- approval 等待、批准/拒绝、stale approval；
- checkpoint resume、SSE replay；
- 未授权目标仓库写入数，必须始终为 0。

D2. 评估
保留全部既有 50+ 业务 Case。新增 runtime reliability/safety Case：
- Reviewer RETRY_REPAIR 带反馈重试；
- Reviewer REJECT、NO_CODE_FIX、REVIEW_UNAVAILABLE；
- 同 patchDigest 拦截、Scope 扩展复验、定位不足不生成补丁；
- repair attempt 三次后停止；
- 子图 checkpoint/restart 恢复；
- M0 审批和 M1 并行/SSE 回归；
- trace/SSE 脱敏；
- 未授权写入计数为零。

报告必须分开输出：
1. 业务效果：检索、根因、定位、补丁、测试、风险；
2. 运行时安全：未授权写入、scope 拦截、敏感泄露；
3. 可靠性：interrupt resume、checkpoint 恢复、事件补发；
4. 效率：latency、cost、工具调用、修复轮次；
5. 人工负担：审批比例、等待时长、拒绝原因。

D3. feature flags、发布与回滚
- 补全 CODEOPS_MAX_REPAIR_ATTEMPTS=3、LANGGRAPH_STATE_SCHEMA_VERSION、子图/评估开关，并写入 .env.example；默认保守。
- 保持 CODEOPS_APPLY_MODE=delivery_only，不能默认打开目标仓库 apply。
- 设计 shadow/gray rollout：先 shadow reviewer route，再启用 retry，再有限开启审批交付。
- 回滚只关闭 feature flag，不删除 checkpoint schema。
- 已暂停 thread 必须有稳定 node 名或兼容适配，不能因重构而无法恢复。

阶段 E：测试与验证
新增或扩展测试，至少覆盖：
1. 每个子图的输入/输出合同和只读/沙箱副作用边界；
2. RETRY_REPAIR 使 Repair 获得结构化 feedback；
3. Reviewer 不可修改 patch；
4. repair attempt、预算和 patchDigest 去重；
5. NO_CODE_FIX/定位不足不进入 PatchSandbox；
6. REVIEW_UNAVAILABLE 不会自动应用；
7. 子图 checkpoint 后恢复；
8. trace/dashboard 包含子图、attempt、verdict、blocked reason；
9. 新指标正确记录；
10. 敏感值不出现在 SSE/trace；
11. M0/M1 全部回归仍通过；
12. 全量 pytest 通过。

执行要求：
- 先跑直接相关测试，再跑完整 pytest。
- 本地缺少依赖、Maven、数据库或外部服务时，报告精确命令、原因和已完成的验证；绝不虚报通过。
- 每个行为改动都需要测试；使用小而可审查的补丁。
- 不修改已有评估数值或简历指标。

最终交付：
1. M0/M1 前置审计结论；
2. 子图架构与每个子图的 State/Artifact/Tool/Effect 边界；
3. 父图路由和 Reviewer -> Repair 状态机；
4. 实际修改文件与行为变化；
5. 新 feature flags、指标、评估 Case、发布/回滚策略；
6. 测试命令、结果和无法运行的依赖；
7. API、DTO、Store、checkpoint、M0/M1 SSE 的兼容性说明；
8. 已知限制和后续优化；不得夸大未验证能力。

现在开始执行 M2 和 M3。优先保证子图边界、独立审查、受预算反馈环、安全 effect、可恢复性、可观测性和测试证据。
~~~

