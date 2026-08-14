# Kubernetes Pod CrashLoopBackOff 排查手册
## keywords

crashloopbackoff, pod restart, oomkilled, liveness probe, readiness probe, exit code, container restart, kubernetes, deployment rollout

## 适用场景

服务发布后 Pod 反复重启，Kubernetes 显示 CrashLoopBackOff，接口 5xx、实例数不足、滚动发布卡住或就绪实例下降。告警常见指标包括 container_restart_count 增长、kube_pod_container_status_restarts_total 异常、ready pod 数低于期望、deployment unavailable replicas 升高。

该类问题需要先判断重启来源：应用进程主动退出、JVM OOM、容器内存 OOMKilled、探针失败被 kubelet 杀掉、启动参数或配置错误。不同来源对应完全不同的修复路径。

## 精确定位片段：OOMKilled

如果 Kubernetes lastState 显示 reason=OOMKilled，exitCode=137，同时 Prometheus 中 container_memory_working_set_bytes 接近 memory limit，根因优先考虑容器内存限制不足、堆外内存、DirectBuffer、线程数过多或 JVM Xmx 与容器 limit 配置不匹配。

Java 服务不能只看 `jvm_memory_used_bytes`。如果堆内存不高但容器内存打满，要检查堆外内存、Metaspace、线程栈、Netty DirectBuffer、压缩/图片处理临时内存。若 Xmx 设置为 limit 的 90% 以上，容器留给 Metaspace、线程栈和 native memory 的空间不足，容易被 OOMKilled。

## 精确定位片段：探针失败

如果容器不是 OOMKilled，而是 liveness probe failed，需要查看探针路径是否依赖数据库、Redis、下游 RPC。活性探针不应检查强依赖，否则短暂下游抖动会导致应用被反复重启，引发雪崩。

readiness probe 可以表达“暂时不接流量”，liveness probe 只表达“进程已经不可恢复”。如果日志显示启动期间数据库连接慢、缓存预热慢、迁移脚本慢，同时 liveness initialDelaySeconds 太短，需要调整探针阈值，而不是修业务代码。

## 精确定位片段：启动配置错误

如果所有新 Pod 在启动 5 到 20 秒内退出，日志包含 `Could not resolve placeholder`、`Access denied`、`BeanCreationException`、`NoSuchMethodError`、`ClassNotFoundException`，通常是配置、镜像或依赖版本问题。

排查顺序是：最近一次 Deployment diff、镜像 tag、ConfigMap/Secret 版本、启动参数、环境变量、依赖 jar 冲突。若老 Pod 正常新 Pod 异常，优先比较新旧 revision，不要先怀疑运行时流量。

## 临时止血

如果是发布引入，先暂停 rollout，并回滚到上一稳定 revision。若只有部分实例异常，先将异常 revision 缩容到 0，确保 Service endpoints 只包含 ready 实例。若是 OOMKilled 且业务峰值正在发生，可以临时提高 memory limit 或扩容副本，但必须同步评估节点资源。

探针误杀导致重启时，临时提高 failureThreshold、periodSeconds、initialDelaySeconds，并把 liveness probe 改为轻量本地健康检查。不要让 liveness 访问数据库或远程服务。

## 长期治理

发布系统需要在 rollout 前做配置完整性检查和镜像启动烟测。关键服务应记录容器退出原因、lastState、restart count、探针失败原因，并把 Kubernetes event 纳入日志检索。

JVM 服务需要统一容器内存基线：Xmx 不超过 limit 的 60% 到 70%，保留 Metaspace、DirectMemory、线程栈和 native 开销。高并发服务要监控线程数、direct memory、GC、container RSS。

## 误判边界

CrashLoopBackOff 是结果，不是根因。只有看到 exitCode、lastState、events、应用启动日志和容器资源曲线，才能判断是否需要改代码。若根因是配置缺失或探针策略错误，自动代码修复应退出，转为发布配置风险建议。
