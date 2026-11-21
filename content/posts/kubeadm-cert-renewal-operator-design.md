---
title: "kubeadm 的证书一年就过期：一个续期 operator 的状态机设计"
date: 2026-11-21T10:00:00+08:00
draft: false
description: "kubeadm 集群的叶证书只有一年寿命，过期即全集群不可用。完整记录一个续期 operator 的设计：部署形态的三个硬约束、兼容矩阵的三层推理、按节点分片的 status、八阶段状态机与全量回滚、Lease 锁的四个场景、派生状态与动态巡检。"
tags: ["Kubernetes", "Operator", "证书", "状态机", "设计"]
series: "kubeadm 证书续期 operator"
---

kubeadm 签发的集群证书有效期只有一年。过期当天的症状不是某个功能变慢，
而是集群整体消失：kubectl 连不上、etcd 集群通信中断、kubelet 与 API Server 失联。
CA 证书有十年寿命所以很少被想起，真正倒计时的是七张叶证书和三个 kubeconfig
（共十个纳管对象）。

这个 operator 做的事一句话能说完：自动完成证书的检查、备份、续期、验证、
组件重启和健康检查。但「一句话能说完」和「能安全地做」之间隔着全套设计取舍，
这篇完整记录这些取舍。使用与维护见[姊妹篇](/posts/kubeadm-cert-renewal-operator-ops/)，
实现与 API 参考见
[kubeadm-cert-renewal-operator](https://github.com/Jekbon/kubeadm-cert-renewal-operator)。

## 一、威胁模型：半失败比过期更糟

设计之前先排故障的严重度。**证书过期是确定的故障**：症状明确、
原因唯一、处理路径清楚（续期加重启）。而**续期过程中的半失败是不确定的故障**：
apiserver 用新证书、etcd 用旧证书这种混合状态，TLS 握手会在想不到的地方失败，
诊断时连「当前到底哪张证书是新的」都难以回答。

这个排序决定了全文最重要的一个设计决策（第五节的全量回滚），
也解释了为什么 operator 的每个阶段都要有明确出口而不是「继续往下走看看」。

## 二、先排除现成方案

**cert-manager 不管这件事。** 它签发和管理的是工作负载证书（Ingress TLS、mTLS），
而 kubeadm 的 PKI 是控制面组件自用的静态文件，落在每个 control-plane 节点的
`/etc/kubernetes/pki/` 下，续期必须调用 `kubeadm certs renew` 并重启对应的 static pod。
这是节点本地的文件操作加进程管理，不是集群内的证书签发。

**cronjob 加脚本能跑通一半。** 它能定时执行 `kubeadm certs renew`，但缺四样东西：
续期失败后的回滚、多 control-plane 节点的互斥、续期后的健康确认、
以及「哪些组件该重启」的精确映射。缺任何一样，自动化就可能把集群带进
第一节说的不确定故障里。

所以形态是一个 operator：有状态记录（CR status）、有事件、有重试语义、
有可以被人类干预的开关。

## 三、部署形态：三个硬约束，和一个不得不接受的权限

**约束一：必须碰宿主机文件。** 证书在节点的 `/etc/kubernetes/pki/`，
static pod manifest 在 `/etc/kubernetes/manifests/`。容器里做这些事只能靠 hostPath
挂载，而「每个 control-plane 节点都要管理自己的证书」天然对应 **DaemonSet 加节点选择**，
不是 Deployment。一个中心化的 Pod 远程操作各节点文件会引入 SSH 或 agent 通道，
复杂度远高于每节点一个本地 Pod。

**约束二：必须用节点上安装的 kubeadm。** 续期命令是 `kubeadm certs renew`，
行为与节点上 kubeadm 的版本绑定。operator 镜像里内置一份 kubeadm 会引入版本错配风险
（镜像里的 kubeadm 比节点新或旧都可能改写字段），所以选择 hostPath 挂载宿主机的
`/usr/bin/kubeadm` 直接 exec。这个决定附带一个好处，见第四节。

**约束三：多个 control-plane 不能同时续期。** etcd 证书在三节点上同时被替换会打断
集群通信。协调机制见第六节。

**权限是这套约束的必然结果，不是疏忽。** DaemonSet 以 root 运行、挂载
`/etc/kubernetes` 与 `/var/lib/kubelet`、开 `hostNetwork` + `hostPID` + `privileged`——
因为不存在更低权限的路径能改 PKI 文件、轮转 manifest、重启 kubelet。
设计上的应对不是降权（降不了），而是**把安全边界移到命名空间 RBAC**：
能改 `cert-manager-system` 里 CR 的人等于能触发控制面组件重启，
所以这个命名空间要当准 admin 资源管。接受高权限 Pod 的前提是把它的影响面
限制在一个被严格准入的命名空间里。

整体拓扑：每个 control-plane 节点一个 Operator Pod，exec 宿主机的 kubeadm，
状态只写回 CR 里本节点的分片，指标与通知向外发：

![整体架构：每节点一个 Operator Pod，exec 宿主机 kubeadm 续期 PKI 并轮转 static pod manifest，HA 用 Lease 串行化，状态写回 CR 的本节点分片，Prometheus 抓取指标并外发通知](/images/cert-renewal-architecture.svg)

## 四、兼容矩阵是推出来的，不是测出来的

支持声明覆盖 Kubernetes 1.15 到 1.33。这个宽度不是靠逐版本测试得到的，
而是三层推理：

1. **命令语义稳定**：`kubeadm certs renew` 于 v1.7 引入（alpha）、v1.15 稳定、
   v1.21 移除 alpha 子命令。1.15 之后语义未变，这是下界
2. **客户端向下兼容**：operator 用到的基础 API 资源（CRD v1、Lease v1、Event v1、Secret v1）
   全部在 1.16 之前已稳定；controller-runtime 的 client-go 遵循 Kubernetes API 的向下兼容策略
3. **镜像与集群版本解耦**：因为约束二选择 exec 宿主机的 kubeadm，
   续期行为由**节点**的 kubeadm 版本决定，与 operator 镜像构建时用的版本无关——
   所以不必为每个 K8s 版本出一个镜像 tag

第三层是关键：它把「operator 兼容哪些集群」这个问题，从「operator 镜像内置了什么」
转换成「节点上的 kubeadm 支持什么命令」。兼容矩阵因此跟随上游命令的稳定性，
而不是跟随我们的发布节奏。

## 五、API 形状：status 按节点分片

CR 是 cluster-scoped 的 `CertificateAutoRenewal`（证书是集群级资产，不该 belong 到某命名空间）。
spec 按关注点分组：`safety`（备份/回滚/重试）、`restartBehavior`（自动重启与间隔）、
`blackoutWindows`、`manualOverride`、`notification`。

**status 的结构是一个容易被忽略但重要的决策**：`status.nodes[*]` 按节点分片，
每个 DaemonSet Pod 只写自己那一片的证书状态与 phase。
如果 status 是扁平的全集群结构，三个节点的 Pod 会并发写同一个资源，
更新冲突会成为常态；分片之后每个写者拥有不相交的字段集合，
乐观并发退化为几乎不冲突。聚合的整体 `status.phase` 由每片派生（第七节）。

finalizer 保证删除 CR 时先释放 Lease 锁并记录备份位置，再放行删除——
否则卸载可能让其他节点白等 600s 锁过期。

## 六、八阶段状态机

主链八个阶段，每个阶段的失败都有明确出口，没有「继续往下走看看」的分支：

| Phase | 做什么 | 失败的出口 |
|---|---|---|
| 1 前置检查 | paused 开关、黑窗时间（forceRenew 可跳过黑窗） | paused → Healthy + requeue 1h；黑窗 → requeue 30m |
| 2 检查证书 | 解析 PEM 与 kubeconfig，算剩余天数，挑待续期集合 | 集合空 → 动态 requeue；dryRun → 只发事件不写 |
| 3 HA 协调 | 获取 Lease 锁 | 获取出错 → requeue 5m；他人持有 → requeue 2m |
| 4 备份 | 复制 pki/ 与 *.conf | 失败 → phase=Error，requeue 10m，**不进入续期** |
| 5 续期 | `kubeadm certs renew` 逐个执行 | 任一失败 → 全量回滚（见下） |
| 6 验证 | 解析新证书：有效期 + CA 链 | 验证失败 → 全量回滚 |
| 7 重启 | 按映射轮转 static pod manifest，必要时重启 kubelet | autoRestart=false 时跳过 |
| 8 健康检查 | `/healthz` + `etcdctl endpoint health` | 不健康 → phase=Error + 事件 |

![八阶段状态机：主链从前置检查到健康检查，Phase 3 取 Lease 锁，Phase 5 与 6 失败触发全量回滚，autoRestart 时回滚后重跑 Phase 7 加载旧证书](/images/cert-renewal-state-machine.svg)

决策流里几个不显眼但必要的分支：

- **进入 Phase 1 之前**先跑 `recoverOrphanedManifests`，收拾上次中断的现场（第八节）
- **CR 带 DeletionTimestamp** 时不走状态机，走 cleanup：释放 Lease、记录备份位置、
  移除 finalizer
- **dryRun 的事件强制 Warning**：dryRun 下即使整体健康，「将会续期哪些」也用 Warning
  级别发事件——预演的目的就是要被人看见，Normal 级别会被淹没
- **收尾阶段（FINAL）做四件事**：重置 `forceRenew`（status patch，防止一次触发黏住）、
  写 per-cert status、清 `RenewalInProgress`、发通知。forceRenew 的自动重置放在这里
  而不是触发时，是因为只有走完全流程才算「这次强制续期被消费了」

### 三个最重要的安全决策

**备份先于一切写操作。** Phase 4 在任何证书被改动之前完成全量备份，
备份失败就直接终止本轮。回滚能力是整个设计的地基：没有它，
Phase 5 的失败就只能靠人肉救火。备份默认保留最近 5 份、目录权限 0700
（里面是私钥），`safety.maxRetries` 控制单轮内的重试次数。

**回滚是全量，不做部分保留。** Phase 5 里只要有一张证书续期失败，
就把**全部**已续期的证书从备份恢复，而不是保留成功的那几张。
理由回到第一节：混合状态比「全部还是旧证书」难诊断得多，也可能直接造成
组件间 TLS 握手失败。续期要么整体前进，要么整体不动。

**回滚之后还要让组件加载回旧证书。** 恢复文件不等于恢复运行态——
如果 Phase 5 失败前已经有组件重启过（或 manifest 已轮转），内存里跑的是新证书。
所以回滚路径在 `autoRestart=true` 时会再执行一次 Phase 7，把组件重启回旧证书。
这一步容易被漏掉，漏掉的表现是「文件已回滚但集群仍异常」。

## 七、HA 协调：Lease 锁的四个场景

多 control-plane 节点用 Kubernetes 原生的 **Lease**（`kube-system/cert-renewal-coordination`）
串行化续期。选 Lease 而不是自研 CR 或 annotation 抢锁，是因为它天生带
`renewTime` + `leaseDurationSeconds` 语义和 ResourceVersion 乐观并发，
leader election 的边角（过期判定、CAS）都由 API server 保证。

四个场景的行为：

| 场景 | 行为 |
|---|---|
| 首抢（Lease 不存在） | Create，holder=本节点，duration=600s |
| 他人持锁且未过期 | 返回 false，事件 `LeaseHeld`，requeue 2m；**不阻塞不报错** |
| 持锁期间 | 后台 goroutine 每 60s 用 CAS 续约 renewTime |
| 持锁方崩溃未释放 | 其他节点在 600s 过期后 CAS 接管；接管冲突（又有人抢先）则再等一轮 |

完整时序，含正常释放与崩溃接管两条路径：

![Lease 抢锁时序：首抢创建、他人持有退让 requeue 2m、持锁期间每 60s CAS 续约、defer 释放；持锁方崩溃时他节点等 600s 过期后 CAS 接管，接管冲突则再等一轮](/images/cert-renewal-lease-sequence.svg)

两个参数的关系值得说清：**600s 的锁有效期必须大于单次续期的典型耗时**，
否则持锁方还在干活锁就过期了，会出现双持窗口；60s 的续约间隔则保证
「持锁方活着」这个信号足够新鲜。续期耗时若可能超过 600s（大集群、慢盘），
后台续约是兜底——但崩溃接管的最坏延迟也就是 600s，这是可用性与互斥性的交换。

`LeaseHeld` 事件在 HA 集群里每轮都会由非持锁节点发出，**是正常现象不是故障**；
运维篇把它列进事件清单就是为了避免值班同学被它叫醒。

## 八、崩溃重入：Phase 7 的幂等

Phase 7 轮转 static pod manifest 时会先写 `.bak` 再替换。
如果 operator 恰好死在这两者之间，下次 Reconcile 面对的是半轮转状态。
所以每轮 Reconcile 进入 Phase 1 之前先跑 `recoverOrphanedManifests`：
把上次中断残留的 `.bak` 恢复回原位，让 Phase 7 从一致的起点开始。
恢复失败发 `ManifestRecoveryFailed` 事件——这个事件意味着幂等前提被破坏，
需要人工看一眼，不能静默跳过。

**状态机的幂等不靠「小心别中断」，靠每次重入先收拾上一次中断的现场。**

## 九、状态是派生的，不是存储的

每张证书的 `phase`（Valid / ExpiringSoon ≤30d / ExpiringCritical ≤7d / Expired / Error）
每轮由剩余天数重新计算，不持久化、不维护转移表。
派生状态没有「存的状态和现实不一致」这一类 bug：证书文件是唯一的真相源。

整体 `status.phase` 的聚合优先级里有一个刻意的选择：**Expired 优先于 Error**。
只要有任一证书已过期，整体就是 Critical，即使同时另有证书解析失败（Error）。
因为过期意味着集群已经在掉线或即将掉线，而「读不到某个文件」可能只是权限或路径问题
—— 两者的紧急程度不该被平均掉。

另一个细节：Phase 3 拿到锁之后直接写 `status.phase = Renewing`，绕过聚合逻辑，
直到 Phase 8 结束才由健康检查结果决定落到 Healthy 还是 Error。
否则续期进行中的中间态会被聚合函数算成别的值，监控上会闪烁。

两级判定的完整逻辑：

![证书状态判定：单证书 phase 每轮由剩余天数派生，整体 phase 按优先级聚合且 Expired 优先于 Error，Phase 3 拿锁后覆盖为 Renewing 直到 Phase 8 结束](/images/cert-renewal-status-derive.svg)

## 十、巡检节奏：不做固定轮询

固定间隔轮询在这个场景两头不讨好：间隔太长，证书可能在两轮之间滑过阈值；
间隔太短，每天几千次无意义的 PEM 解析。改为按**最紧迫的证书**决定下次 requeue：

| 最小剩余天数 | requeue |
|---|---|
| > 90 天 | 24h |
| > 30 天 | 12h |
| > 7 天 | 6h |
| ≤ 7 天 | 1h |
| 任一证书检查出错 | 1h（安全兜底，加密巡检） |

距离过期越近巡检越密，平稳期一天只看一次。三个配套开关各自解决一个问题：

- **黑窗时间**：续期伴随组件重启，业务高峰不允许。落在黑窗内 requeue 30m 等窗口过去；
  支持跨午夜窗口。黑窗是「默认不做」，forceRenew 是显式的「这次要做」
- **forceRenew**：不等阈值，把全部纳管证书纳入本轮；完成后在 FINAL 阶段自动重置，
  不会黏住。它与 paused 同时为真时 **paused 优先**——刹车必须赢过油门
- **dryRun**：走完检查与决策、发「将会续期哪些」的事件，但不做任何写操作。
  首次部署时用它确认决策路径，比直接放开自动续期安全一个量级

## 十一、重启的精确映射

续期本身不生效于运行中的进程，必须重启加载新证书的组件。映射是写死的表，不是猜测：

| 续期了 | 重启 |
|---|---|
| apiserver | kube-apiserver |
| apiserver-kubelet-client | kube-apiserver |
| apiserver-etcd-client | kube-apiserver |
| front-proxy-client | kube-apiserver |
| etcd-server | etcd |
| etcd-peer | etcd |
| etcd-healthcheck-client | etcd |
| admin.conf | kubelet（无对应 static pod） |
| controller-manager.conf | kube-controller-manager + kubelet |
| scheduler.conf | kube-scheduler + kubelet |

重启按映射**逐个轮转并带 staggerDelay（默认 10s）间隔**，避免同一时刻多个控制面组件
一起消失。只续期了 apiserver 系证书时不会去碰 etcd——重启范围严格等于受影响范围。

表里最破坏性的一行是 kubeconfig 类触发的 **kubelet 重启**：它影响节点上所有 Pod 的
管理通道，而不只是控制面组件。知道「什么时候会发生 kubelet 重启」
是评估一次续期影响面的关键，运维篇把它单列为注意事项。

CA 证书（ca.crt、front-proxy-ca.crt、etcd/ca.crt）十年有效期，**只监控不续期**：
CA 轮换是另一个量级的工程（涉及全集群信任链重建），把它塞进自动续期是不负责任的，
快到期时告警、留给人决策才是正确边界。

## 十二、可观测性与代码组织

指标按 `node` + `cert` 双维度打 label（`cert_days_remaining{node,cert}` 等七个）。
双维度不是冗余：单看集群级聚合会把「某节点某张证书 3 天到期」稀释成
「平均剩余 200 天」，而证书故障从来都是单点形的。告警规则与 Grafana 大盘
都建立在这个维度上。

续期结果通过 webhook 通知（DingTalk / Slack / Generic 三种格式）。
无人值守系统的最后一环不是自动化本身，是**自动化做完之后有人知道它做完了**——
否则「operator 静默失败」和「operator 静默成功」在值班视角里没有区别。

代码组织与状态机一一对应：`internal/controller/cert/` 下六个单一职责文件
（checker / backup / renewer / verifier / restarter / types）分别承担
Phase 2 / 4 / 5 / 6 / 7 的实现，`coordination/lease.go` 承担 Phase 3。
 phase 与文件对齐的好处是每个阶段可以独立单测（解析、备份、回滚、验证各自有
确定性输入输出），状态机测试只验证编排不验证细节。

## 十三、边界

- 仅支持 kubeadm 部署的集群（证书布局与 `kubeadm certs renew` 语义是前提）
- 不做 CA 轮换（见第十一节）
- 不做 worker 节点的 kubelet 客户端证书——那由 kubelet 自身的证书旋转机制管理，
  重复接管只会制造冲突
- API 为 v1alpha1，字段可能变更；v1beta1 与 v1 的演进会提供 CR 迁移工具

一个 operator 的价值不在于它能自动做多少事，而在于它**拒绝自动做**哪些事：
CA 轮换拒绝、部分回滚拒绝、黑窗内续期拒绝、备份失败后续期拒绝、
paused 时 forceRenew 拒绝。这些「不」加在一起，才让剩下那些「是」
可以放心地无人值守。
