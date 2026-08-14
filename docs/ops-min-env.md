# MySQL + Prometheus/Grafana 最小闭环

这套最小闭环用于演示“生产报错智能诊断”主场景：

```text
用户输入故障
-> 生成测试故障流量
-> Prometheus 采集应用指标
-> 智能诊断接口汇总证据链
-> 生成报告
-> MySQL 落库复盘记录、审计日志、工具调用日志
```

## 1. 启动基础环境

在项目根目录执行：

```bash
docker compose -f docs/dev-ops/docker-compose-ops-min.yml up -d
```

服务地址：

| 服务 | 地址 | 账号 |
| --- | --- | --- |
| MySQL | `127.0.0.1:13306` | `root / 123456` |
| Prometheus | `http://127.0.0.1:9090` | 无 |
| Grafana | `http://127.0.0.1:4000` | `admin / admin` |
| node-exporter | `http://127.0.0.1:9100/metrics` | 无 |

## 2. 启动 Spring Boot 应用

使用 `dev` 配置启动应用，应用端口是：

```text
http://127.0.0.1:8099
```

应用启动后会暴露 Prometheus 指标：

```text
http://127.0.0.1:8099/actuator/prometheus
```

Prometheus 已配置抓取：

```text
host.docker.internal:8099/actuator/prometheus
```

应用启动后等待 15 到 30 秒，在 Prometheus Targets 页面确认 `ops-demo-service` 为 `UP`：

```text
http://127.0.0.1:9090/targets
```

## 3. 打开统一演示入口

浏览器打开：

```text
http://127.0.0.1:8099/ops-console.html
```

推荐直接点：

```text
一键演示闭环
```

它会自动完成：

```text
检查应用、MySQL、Prometheus、Prometheus Target
-> 制造 500 错误
-> 制造慢请求
-> 占用数据库连接
-> 等待 Prometheus 采集
-> 发起智能诊断
-> 展示 SSE 执行过程和最终报告
```

诊断结束后，点击：

```text
查询诊断记录
```

可以看到 MySQL 中落库的复盘记录。

## 4. 当前闭环能证明什么

已经能证明：

```text
Spring Boot 应用可用
MySQL 可用
Prometheus 能抓到应用指标
故障诊断 SSE 能输出过程
证据链能生成
runbook 能匹配
报告能生成
诊断记录能落库
审计日志、工具调用日志能落库
```

当前没有接入 ELK 和 SkyWalking，所以页面里对应步骤会显示未配置。这不是流程失败，而是最小闭环阶段只验证 MySQL + Prometheus/Grafana。

## 5. 数据库关键表

```text
ops-autoagent-diagnosis.ops_incident_diagnosis
ops-autoagent-diagnosis.ops_audit_log
ops-autoagent-diagnosis.ops_tool_call_log
```

## 6. 停止环境

```bash
docker compose -f docs/dev-ops/docker-compose-ops-min.yml down
```

如果要清空 MySQL 和 Prometheus 数据：

```bash
docker compose -f docs/dev-ops/docker-compose-ops-min.yml down -v
```

