---
title: "证书续期 operator 的使用与维护：开关、巡检与救火手册"
date: 2026-11-21T10:30:00+08:00
draft: false
description: "设计篇的姊妹篇：CR 里每个开关该什么时候动、日常巡检查什么、五条告警各自意味着什么、续期失败后怎么手动回滚，以及这个高权限 DaemonSet 的安全边界在哪。"
tags: ["Kubernetes", "Operator", "证书", "运维", "Runbook"]
series: "kubeadm 证书续期 operator"
---

[设计篇](/posts/kubeadm-cert-renewal-operator-design/)讲了状态机为什么长那样，
这篇讲怎么用它：部署的正确姿势、每个开关的使用场景、日常巡检查什么、
出事时怎么救火。所有命令基于 [kubeadm-cert-renewal-operator](https://github.com/Jekbon/kubeadm-cert-renewal-operator) v0.2.0。

## 一、部署：先暂停，观察，再放开

### Helm Chart（推荐）

Chart 发布在 GHCR 的 OCI 仓库，公开包可匿名拉取：

```bash
helm install cert-renewal \
  oci://ghcr.io/jekbon/charts/kubeadm-cert-renewal-operator \
  --version 0.2.0 \
  --namespace cert-manager-system --create-namespace \
  --set manualOverride.paused=true
```

一次 install 装齐：Namespace、ServiceAccount、ClusterRole、DaemonSet、
metrics Service，以及一个 `CertificateAutoRenewal` CR
（`cr.create: true` 默认开启，**CR 的 spec 全部由 values 顶层键生成**——
`renewalThreshold`、`blackoutWindows`、`safety`、`notification` 等都是 values 键，
改配置就是改 values，不需要单独维护 CR 文件）。
DaemonSet 默认带 control-plane 的 nodeSelector 与 tolerations，
`hostNetwork` / `hostPID` 已开启（etcd 健康检查与 kubelet 访问需要）。

参数多于三四个就用 values 文件，别堆 `--set`（升级时 `helm upgrade -f` 能完整保持配置）：

```yaml
# values.yaml
image:
  tag: ""                    # 留空 = 跟随 Chart appVersion，升 chart 即升镜像
renewalThreshold: 30
manualOverride:
  paused: true               # 首次部署先暂停，见下文
blackoutWindows:
  - start: "09:00"
    end: "18:00"
    daysOfWeek: [Mon, Tue, Wed, Thu, Fri]
notification:
  enabled: true
  webhook:
    type: DingTalk           # DingTalk | Slack | Generic
    secretRef:
      name: cert-renewal-webhook   # token 放 Secret，不要明文写进 url
      key: token
monitoring:
  enabled: true              # 一并装 PrometheusRule + ServiceMonitor
```

```bash
helm install cert-renewal \
  oci://ghcr.io/jekbon/charts/kubeadm-cert-renewal-operator \
  --version 0.2.0 -n cert-manager-system --create-namespace -f values.yaml
```

验证（install 结束时的 NOTES 也会打印这几条）：

```bash
kubectl get ds -n cert-manager-system
kubectl logs -n cert-manager-system -l app.kubernetes.io/name=kubeadm-cert-renewal-operator -f
kubectl get certificateautorenewal cluster-cert-renewal -o yaml
```

### 裸 yaml

不用 Helm 时按顺序 apply，CR 需要自己维护：

```bash
kubectl apply -f config/crd/bases/certs.jekbon.github.io_certificateautorenewals.yaml
kubectl apply -f config/rbac/role.yaml
kubectl apply -f config/manager/manager.yaml
kubectl apply -f config/samples/certs_v1alpha1_certificateautorenewal.yaml
```

### 首次部署：先暂停，观察，再放开

**不要一装上就开自动续期。** 上面两种方式的示例都带了 `paused: true`，
部署后观察 operator 日志和 `status` 里的证书检查结果，确认：

- 十张证书都被正确解析（phase 不是 Error）
- 剩余天数与 `kubeadm certs check-expiration` 的人工核对一致
- 没有 Lease 获取失败的循环报错

确认无误再把 `paused` 改回 false（Helm 方式改 values 后 `helm upgrade`，
或直接 patch CR）。自动化接管控制面证书之前，先让它「只看不动」跑一段时间，
是成本最低的信任建立方式（`dryRun: true` 也能达到同样效果，且会发 Warning 事件便于确认决策路径）。

### 升级与卸载的顺序

升级走常规路径：`helm upgrade cert-renewal oci://... --version <新版> -f values.yaml`；
`image.tag` 留空时镜像版本跟随 Chart 的 appVersion。

**卸载有一个顺序坑**：`helm uninstall` 会把 CR 一起删（chart 没给 CR 加保留注解），
而 Helm 删除资源不保证 controller 先于 CR 消失——一旦 DaemonSet 先死，
CR 上的 finalizer 就没人处理，CR 永久卡在 Terminating，uninstall 挂住或留下残留。
正确顺序是**先删 CR 等它消失，再 uninstall**：

```bash
kubectl delete certificateautorenewal cluster-cert-renewal
kubectl wait --for=delete certificateautorenewal/cluster-cert-renewal --timeout=60s
helm uninstall cert-renewal -n cert-manager-system
```

已经卡住的按第六节手动清 finalizer 放行。

## 二、CR 开关速览：什么时候动哪个

```yaml
apiVersion: certs.jekbon.github.io/v1alpha1
kind: CertificateAutoRenewal
metadata:
  name: cluster-cert-renewal
spec:
  renewalThreshold: 30          # 到期前 N 天触发续期
  managedCertificates:
    mode: All                   # All | Selective
    include: []
    exclude: []
  restartBehavior:
    autoRestart: true
    staggerDelaySeconds: 10
  safety:
    backupEnabled: true
    backupPath: /etc/kubernetes/pki-backup
    backupRetention: 5
    rollbackOnFailure: true
    maxRetries: 3
  blackoutWindows:
    - start: "09:00"
      end: "18:00"
      daysOfWeek: ["Mon","Tue","Wed","Thu","Fri"]
  notification:
    enabled: false
    webhook:
      url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"   # token 走 Secret，别明文
      type: DingTalk            # DingTalk | Slack | Generic
  manualOverride:
    paused: false
  forceRenew: false
```

| 开关 | 默认 | 什么时候动它 |
|---|---|---|
| `renewalThreshold` | 30 | 审批/变更流程长于一个月的组织调到 45~60，给人工介入留窗口 |
| `managedCertificates.mode` | All | 只想托管部分证书（如 etcd 系留给人工）时用 Selective + include/exclude |
| `autoRestart` | true | 想自己控制重启窗口时设 false：续期完成后组件仍跑旧证书，由你在维护窗口手动轮转 manifest |
| `staggerDelaySeconds` | 10 | 控制面组件多或 etcd 敏感时调大，拉长轮转间隔 |
| `backupRetention` | 5 | 磁盘紧张调小；经历过多次救火的集群调大 |
| `rollbackOnFailure` | true | **几乎不该关**。关掉意味着续期失败时留下新旧混用状态 |
| `blackoutWindows` | 空 | 配业务高峰期；支持跨午夜（`22:00-06:00`） |
| `paused` | false | 紧急止血、排查 operator 自身问题、变更冻结期 |
| `forceRenew` | false | 立即触发一次全量续期（忽略 threshold 与黑窗），完成后自动重置 |

两个方向的人工干预要分清：**`paused` 是刹车**（停止一切自动动作），
**`forceRenew` 是油门**（立刻跑一轮，跳过阈值和黑窗）。
两者同时为 true 时 paused 优先。

`forceRenew` 的 patch 一行搞定：

```bash
kubectl patch certificateautorenewal cluster-cert-renewal \
  --type merge -p '{"spec":{"forceRenew":true}}'
```

## 三、日常巡检：三个层面

**1. 证书状态**（最直接）：

```bash
kubectl get certificateautorenewal cluster-cert-renewal -o yaml
# 看 status.nodes[*].certificates[*].phase：
# Valid / ExpiringSoon(≤30d) / ExpiringCritical(≤7d) / Expired / Error
# 以及整体 status.phase：Healthy / Warning / Critical / Error / Renewing
```

**2. 事件**（看动作与失败原因）：

```bash
kubectl get events -n cert-manager-system --sort-by='.lastTimestamp'
```

关键事件名：`RenewalStarted` / `RenewalComplete` / `RollbackSuccess` /
`LeaseHeld`（别人在续期，本节点退让）/ `LeaseFailed` / `BackupFailed` /
`VerifyFailed` / `HealthCheckFailed` / `BlackoutWindow` / `DryRun` / `Paused` /
`KubeletRestart`。其中两个是真正的救火信号：
`RollbackFailed`（**自动回滚本身也失败了**，节点可能停在新旧混用状态，
直接走第四节手动回滚，全部事件里优先级最高）和
`ManifestRecoveryFailed`（孤儿 manifest 恢复失败，Phase 7 的幂等前提被破坏）。
`LeaseHeld` 则相反，是**正常现象**不是故障——HA 集群里非持锁节点每轮都会发它。

**3. 指标与告警**（看趋势与 operator 自身存活）：

七个指标里最有用的是 `cert_days_remaining{node,cert}`（趋势图）和
`cert_renewal_total{node,cert,result}`（续期成功率）。
随仓库提供的 PrometheusRule 有五条告警，各自含义：

| 告警 | 含义 | 第一反应 |
|---|---|---|
| `CertsExpiringSoon` | 任一证书 < 30 天 | 确认 operator 在续期队列里处理它 |
| `CertsExpiringCritical` | < 7 天 | 检查为何阈值没提前触发（paused? 黑窗? 续期一直失败?） |
| `CertsExpired` | 已过期 | 集群可能已在故障中，走救火流程 |
| `CertRenewalFailed` | 续期失败 | 看事件与日志，确认是否已自动回滚 |
| `NoRecentCertCheck` | 2h 窗口内检查增量为 0（持续 30m） | 先判断是否误报：窗口小于平稳期巡检间隔，见下文 |

最后一条要特别注意，它带着一个**与巡检节奏不匹配的坑**：规则是
`increase(cert_check_total[2h]) == 0` 持续 30 分钟，而证书检查只发生在 Reconcile 的
Phase 2，平稳期（最小剩余 > 90 天）的 requeue 间隔是 24h——也就是说
**健康集群在平稳期也会持续触发这条告警**。它想抓的是「operator 进程死了、
Reconcile 完全不跑」，但 2h 的窗口小于最大巡检间隔，抓死进程的同时也抓健康集群。

两个处理方向：按自己集群的实际 requeue 阶梯把窗口调到大于最大间隔
（平稳期集群改成 26h 一类），或者换 dead-man's switch 思路——
对当前时间与 `cert_last_renewal_timestamp_seconds` 的差值设阈。
**接入任何自带告警规则之前，先把自己的巡检节奏代入算一遍**，
这条是现成的例子：规则本身没错，错在阈值假设与实现节奏不一致。

## 四、救火手册

### 续期后集群异常：手动回滚

自动回滚（`rollbackOnFailure`）覆盖 Phase 5/6 的失败，但 Phase 8 健康检查之后才发现的问题
需要人工介入。在出问题的 control-plane 节点上：

```bash
# 1. 看有哪些备份
ls /etc/kubernetes/pki-backup/

# 2. 恢复指定时间点的 pki 与 kubeconfig
cp -r /etc/kubernetes/pki-backup/<timestamp>/pki/* /etc/kubernetes/pki/
cp /etc/kubernetes/pki-backup/<timestamp>/*.conf /etc/kubernetes/

# 3. 轮转 manifest 触发 static pod 重启（移出再移回）
mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/ \
  && sleep 5 \
  && mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/
```

回滚期间先 `paused: true` 停掉 operator，否则它下一轮可能又把「问题证书」续回去。

### 紧急止血

```bash
kubectl patch certificateautorenewal cluster-cert-renewal \
  --type merge -p '{"spec":{"manualOverride":{"paused":true}}}'
```

### 怀疑 operator 判断错误

在节点上用 kubeadm 原生命令交叉验证：

```bash
kubeadm certs check-expiration
```

它的输出与 operator 的 `status` 不一致时，先查**节点时钟**——
证书有效期判断完全依赖本地时间，control-plane 节点时钟不同步会产生
「operator 说还有 30 天、kubeadm 说 3 天」这类分歧。NTP/chrony 是前置条件，不是可选项。

## 五、安全边界与维护注意

**这个 DaemonSet 权限极高**：root 运行、挂载宿主机 `/etc/kubernetes` 与
`/var/lib/kubelet`、开 `hostNetwork` + `hostPID` + `privileged`。
它的安全边界不在 Pod 内部，而在**谁能访问 `cert-manager-system` 命名空间**——
能改 CR 的人等于能触发控制面组件重启。RBAC 上要把它当准 admin 资源管。

备份目录 `/etc/kubernetes/pki-backup/` 里是**证书私钥**，目录权限 0700，
纳入备份策略时注意它本身就是最高密级对象；通知 webhook 的 token 走 Kubernetes
Secret 引用，不要明文写进 CR。

维护层面三条容易踩的：

- **kubelet 重启只发生在 kubeconfig 类证书（admin.conf 等）续期时**——
  这是全流程里最破坏性的一步。PKI 文件证书续期只轮转对应 static pod。
  知道这一点才能正确评估一次续期的影响面
- **Lease TTL 600s**：续期耗时超过它会由后台 goroutine 自动续约，
  但如果 operator 在持锁期间被 kill -9，其他节点要等锁过期（最长 600s）才能接管
- **API 是 v1alpha1**：升级到 v1beta1/v1 时关注 CR 字段迁移说明，
  跨版本升级 operator 前先备份 CR 与 pki-backup 目录

## 六、卸载：顺序错了 CR 会永远卡在 Terminating

卸载的顺序很重要：**先删 CR，等它真正消失，再删 DaemonSet、RBAC、CRD**。
反过来（先删 controller 再删 CR）就没有人处理 finalizer 了。
Helm 部署的完整卸载命令与 `helm uninstall` 的顺序坑见第一节；
本节讲 finalizer 本身的机制和卡住后的手动放行。

**正常路径**（operator 还活着）：删除 CR 后 controller 的 `cleanupOnDeletion`
会移除 finalizer `certs.jekbon.github.io/cert-renewal-finalizer`，同时做两件事——
释放 Lease 锁（其他节点不必等 600s 过期）、在日志里记录备份目录位置。
它**不会自动删除备份**（备份可能还要用于应急恢复），这是刻意的。

**卡住的路径**（operator 已死或已先被删除）：CR 会停在 Terminating，
因为 finalizer 还在而没有任何 controller 来处理它。确认方式：

```bash
kubectl get certificateautorenewal cluster-cert-renewal -o jsonpath='{.metadata.finalizers}'
# 输出 ["certs.jekbon.github.io/cert-renewal-finalizer"] 且 DELETIONTIMESTAMP 非空 = 卡住
```

手动移除 finalizer 放行删除：

```bash
kubectl patch certificateautorenewal cluster-cert-renewal \
  --type=merge -p '{"metadata":{"finalizers":null}}'
```

手动移除的代价要清楚：`cleanupOnDeletion` 不会执行，所以 **Lease 锁不会被释放**——
如果之后还要在别的节点跑 operator，它要等锁自然过期（最长 600s）才能接管；
备份位置也不会被记录到日志。仅在你确认 operator 不会再起来、或愿意等锁过期时用这条路。

最后清理残留：

```bash
kubectl delete -f config/manager/manager.yaml     # DaemonSet
kubectl delete -f config/rbac/role.yaml           # RBAC
kubectl delete -f config/crd/bases/certs.jekbon.github.io_certificateautorenewals.yaml
```

卸载不动节点上的证书与备份目录——每个 control-plane 节点的
`/etc/kubernetes/pki-backup/` 里是含私钥的历史备份，要人工清理或按密级归档。

---

设计篇回答「为什么这样设计」，这篇回答「平时怎么用、出事怎么办」。
两篇配合读，中间缺的那块（代码实现）在仓库的
`internal/controller/` 下，八个 phase 各自对应 `cert/` 目录里一个单一职责的文件，
读起来比大多数 operator 项目友好。
