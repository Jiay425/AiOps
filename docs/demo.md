# CodeOps Case Study Demo

默认模式不需要 `OPENAI_API_KEY`、Prometheus、ELK、SkyWalking 或数据库账号，
但会真实运行仓库中的 `CodeOpsGraph`。只有模型输出被替换为确定性的本地适配器；
父图路由、子图契约、只读工具循环、Scope Guard、PatchSandbox、Maven 验证、
独立 Review 和 LangGraph `interrupt/resume` 都使用生产实现。

## 运行离线回归

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m ops_autoagent.demo
```

也可以使用安装后的命令：

```powershell
.venv/Scripts/ops-autoagent-demo.exe
```

在 Python 3.11+ 上，默认演示走“批准交付”分支。交付模式只生成补丁制品，
不会修改目标仓库。要演示拒绝分支：

```powershell
.venv/Scripts/python.exe -m ops_autoagent.demo --reject
```

项目要求 Python 3.11+；如果临时使用旧版 Python 运行离线 Demo，图会自动关闭审批
Interrupt，只验证到交付边界，并在输出中标注原因。

## 运行真实 LLM Case

真实模式使用 OpenAI-compatible 客户端。以 DeepSeek V4.1 Flash 为例，模型名使用
`deepseek-flash`，密钥只放在当前 PowerShell 进程环境中，不要写入 `.env`、代码或日志：

```powershell
$env:OPENAI_API_KEY = "<your-deepseek-api-key>"
$env:OPENAI_BASE_URL = "https://api.deepseek.com"
$env:OPENAI_MODEL = "deepseek-flash"
.venv311/Scripts/python.exe -m ops_autoagent.demo --real-llm
```

这条命令会让真实模型参与 Agent Loop 定位、补丁生成和独立 Review；订单幂等竞态
Case 的观测输入仍明确标记为仓库内的 `TEST_SIMULATED_DATA` fixture，Java 编译和
JUnit 验证在 PatchSandbox 中执行。`delivery_only` 保证即使审批通过也只交付补丁，
不会写入目标仓库。

## 演示 Case

场景是订单服务的幂等竞态：`OrderSubmitService` 先调用
`alreadyProcessed(requestId)`，之后再调用 `markProcessed(requestId)`，并发请求可能
同时通过检查。离线模型先调用只读 `repo.search_text`，再输出两个目标文件和目标方法。

补丁把状态拥有者改为原子 `markProcessedIfAbsent`，并让提交服务只调用这一个操作；
提交仓库里的 `IdempotencyServiceAtomicityTest` 验证同一个 requestId 只有一个请求成功。

## 预期看到的流程

```text
ops_diagnosis
  -> OpsEvidenceSubgraph: 3-Sigma / EWMA / rule detection -> structured anomaly signals
agent_loop_investigation
repo_understanding
engineering_knowledge_rag
bug_fix -> PatchSandbox
test_verification -> Maven test
release_risk_analysis -> IndependentReviewSubgraph
  -> riskGovernance (blast radius + sandbox dry-run + rollback/metrics + approval decision)
auto_approve_patch -> apply_approved_patch
  (高置信且所有门禁通过)
human_approval -> deliver_patch / apply_approved_patch
  (低置信或高风险)
```

输出中的 `target repository write: BLOCKED (delivery_only)` 是预期结果：
`apply_approved_patch` 是唯一允许写入目标仓库的节点，而本 Demo 只批准交付补丁制品。
`risk governance` 一行来自确定性门禁，而不是模型自报：它汇总 Scope Guard、PatchSandbox、
编译、测试、影响范围、回滚计划和观测指标，并决定是否需要人工审批。

`OpsEvidenceSubgraph` 会先对已采集的指标、日志和链路做确定性检测：数值序列使用 3-Sigma
和 EWMA，文本证据使用明确阈值与错误模式规则。结果以带 `signalId`、`detector`、`severity`、
`baseline` 和证据摘要的结构化信号写回工作记忆，随后才提供给代码定位 Agent；它不是模型推测，
也不会因为没有发现异常就宣称系统健康。

## 为什么这个 Demo 有用

- 新贡献者可以在 5 分钟内看到项目不是静态 Prompt，而是可恢复的 LangGraph 工作流。
- 没有外部服务时仍能复现安全边界和失败分支，适合 CI、截图和项目演示。
- 切换到真实环境只需要替换 LLM 和观测配置，不需要重写图的状态和路由。
