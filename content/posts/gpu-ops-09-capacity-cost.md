---
title: "GPU 集群运维（09）：容量、成本与效率运营"
date: 2026-11-15T09:00:00+08:00
draft: false
description: "容量规划方法、多租户治理完整方案、配额借还与公平调度原理、利用率分析体系与周报模板、成本核算，以及电力与散热运营。"
tags: ["容量", "成本", "多租户"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下版本是本册所有命令、YAML、脚本的参考基准，与 `00-版本基线.md` 保持一致。

| 组件 | 基线版本 | 本册重点用法 |
|---|---|---|
| K8s | 1.31.x | ResourceQuota/LimitRange/PriorityClass/CronJob(DaemonSet) |
| Kueue | v0.9.x（kueue.x-k8s.io/v1beta1） | ClusterQueue/LocalQueue/Cohort/borrowingLimit |
| Volcano | v1.10.x（scheduling.volcano.sh/v1beta1） | Queue/guarantee/binpack plugin |
| Prometheus | 2.55 LTS（kube-prometheus-stack chart v66.x） | recording rule / PromQL / AlertRule |
| DCGM | 3.3.9 / 4.2.x（dcgm-exporter 4.1.0-4.2.0） | DCGM_FI_PROF_SM_ACTIVE / DCGM_FI_DEV_GPU_TEMP |
| GPU Operator | Helm chart v24.9.2 | time-slicing 配置 |
| 驱动（Hopper/Blackwell） | R570 生产分支，570.124.04（示例） | nvidia-smi -pl 功率封顶 |
| 驱动（Ampere 存量） | R550 生产分支，550.127.08（示例） | 同上 |
| CUDA（容器内） | 12.8（Hopper 主基线）/ 12.4（Ampere 兼容基线） | DaemonSet 批量脚本镜像 |
| OS | Ubuntu 22.04.5 LTS / Rocky 9.5 | 双基线 |
| Python（系统） | 3.10（Ubuntu 22.04） | 僵尸清理脚本 |
| Thanos | 0.37.x | 长期存储/成本数据归档 |

---

# 09 容量成本与效率运营

## 1 容量规划方法

### 1.1 训练算力估算公式

核心公式（Chinchilla Scaling Law 推导）：

```text
总 FLOPs ≈ 6 × N × D
```

- N = 模型参数量（parameters）
- D = 训练 token 数（tokens）
- 6 = 经验系数（前向 2 + 反向 4，含激活重计算则更高）

**完整算例：训练 70B 模型，2T tokens，使用 H100 SXM 集群**（假设：driver R550+，CUDA 12.4+，PyTorch 2.5+，Megatron-LM 框架）

```text
步骤 1：总 FLOPs
  = 6 × 70×10⁹ × 2×10¹²
  = 8.4 × 10²³ FLOPs
  = 840,000 PFLOPs（Peta FLOPs）

步骤 2：单卡有效算力
  H100 SXM BF16 峰值 = 989 TFLOPS（以 NVIDIA 官方 spec 为参考）
  MFU（Model FLOPs Utilization）实测典型值 = 40%（大模型 Megatron-LM 训练参考）
  有效算力 = 989 × 0.40 = 395.6 TFLOPS/卡

步骤 3：单卡单日 FLOPs
  = 395.6 × 10¹² × 86400
  = 3.418 × 10¹⁹ FLOPs/卡·天

步骤 4：所需卡·天
  = 8.4 × 10²³ / 3.418 × 10¹⁹
  = 24,575 卡·天
  ≈ 67.3 卡·年

步骤 5：给定工期推算卡数
  若要求 30 天完成：
  卡数 = 24,575 / 30 = 820 卡（不含故障冗余）
  加 5% 故障/重启冗余：820 × 1.05 ≈ 861 卡

  若要求 60 天完成：
  卡数 = 24,575 / 60 = 410 卡
  加 5% 冗余：≈ 431 卡
```

**不同规模速查表**（假设条件：H100 SXM 80GB，driver R550+，CUDA 12.4+，Megatron-LM 框架，MFU=40%；BF16 稠密峰值 989 TFLOPS 来自 NVIDIA H100 Tensor Core GPU Datasheet，BF16 稀疏峰值为 1979 TFLOPS 但未用于本表）：

| 模型规模 | Tokens | 总 FLOPs | 卡·天 | 30 天需卡数 | 60 天需卡数 |
|---------|--------|----------|-------|------------|------------|
| 7B | 150B | 6.3×10²¹ | 184 | 7 | 4 |
| 13B | 300B | 2.3×10²² | 686 | 24 | 12 |
| 70B | 2T | 8.4×10²³ | 24,575 | 861 | 431 |
| 175B | 300B | 3.2×10²³ | 9,218 | 323 | 162 |
| 405B | 15T | 3.6×10²⁵ | 1,053,198 | 36,880 | 18,440 |

### 1.2 推理算力估算要点

```text
单请求 FLOPs ≈ 2 × N × (input_tokens + output_tokens)
              + KV_cache_memory_overhead

并发吞吐估算：
  峰值并发 QPS × 平均单请求 FLOPs = 所需推理 TFLOPS

示例：70B 模型，平均 input 512 tokens，output 128 tokens
  单请求 FLOPs ≈ 2 × 70×10⁹ × 640 = 8.96×10¹³ FLOPs
  若峰值 QPS = 100：
    所需算力 = 100 × 8.96×10¹³ = 8.96 PFLOPS
    H100 有效推理 ≈ 989 × 0.50 = 494.5 TFLOPS（推理 MFU 通常高于训练）
    最少卡数 = 8960 / 494.5 ≈ 19 卡（不含 latency SLO 约束）
```

> **A100/H100 理论峰值 FLOPs 精度说明**：上表中 H100 SXM BF16 稠密峰值 = 989 TFLOPS（NVIDIA 官方 datasheet，非稀疏）；A100 SXM BF16 稠密峰值 = 312 TFLOPS。稀疏（2:4 structured sparsity）可利用 2 倍加速但不适用于通用训练负载。MFU=40% 来自大模型 Megatron-LM/GPT 系列实测参考（PaLM 540B 论文报告 MFU 46.2%，Llama 3 训练 MFU 约 37~42%），实际因模型结构、并行策略、通信拓扑而异。

推理还需额外考虑：
- **KV Cache 显存**：长上下文场景下显存瓶颈先于算力
- **Batch 策略**：continuous batching 提高利用率但增加 latency
- **TTFT / TBT SLO**：首 token 延迟和逐 token 延迟约束实际并发上限

### 1.3 电力与机柜硬约束核算

```text
单卡功耗参考（以 NVIDIA 官方 TDP 为参考）：
  H100 SXM：700W
  A100 SXM：400W
  H200 SXM：700W

单机柜可部署卡数（供电约束）：
  标准机柜供电：10~15kW（传统风冷 IDC）
  高密机柜供电：30~50kW（定制供电母线）
  液冷机柜供电：70~120kW（含 CDU 电力）

H100 8 卡整机（如 DGX H100）：
  整机典型功耗 ≈ 10.2kW（8×700W + CPU + 网络 + 存储 + 风扇）
  10kW 机柜：1 台/柜（需独占）
  30kW 机柜：2~3 台/柜
  50kW 机柜：4~5 台/柜
  液冷 100kW 机柜：8~10 台/柜

供电改造周期参考：
  现有 IDC 加装 PDU/母线：4~8 周
  新增变压器/配电柜：3~6 个月
  液冷管路部署：2~4 个月
```

### 1.4 存储容量规划

```text
数据集存储：
  预训练语料（如 15T tokens）≈ 30~60TB（视 tokenizer 压缩比）
  年增长预估：2~5×（持续爬取 + 合成数据）
  建议预留：当前量 × 3 的 18 个月 buffer

Checkpoint 周转计算（70B 模型为例）：
  单次 ckpt 大小 ≈ 70B × 2 bytes (BF16) = 140GB
  优化器状态（Adam）≈ 70B × 8 bytes = 560GB（FSDP/ZeRO 分片后按卡数分摊）
  完整训练状态 ≈ 700GB~1TB/次（含 scheduler state、RNG state）

  保存频率：每 500 steps
  训练总步数：2T / (global_batch_size × seq_len)
    = 2×10¹² / (2048 × 4096) ≈ 238,416 steps
  ckpt 总数：238,416 / 500 ≈ 477 次
  滚动保留最近 10 个 + 每 5000 steps 一个永久保留
  存储占用 ≈ 10 × 1TB + 47 × 1TB = 57TB

  临时 ckpt（断点续训用）：保留最近 3 个 ≈ 3TB
  总 ckpt 存储规划 ≈ 60TB

推荐存储分层：
  Hot（NVMe SSD）：当前训练 ckpt + 数据集 shard → 20TB
  Warm（HDD/NAS）：历史 ckpt + 全量数据集 → 200TB
  Cold（对象存储/磁带）：归档模型 + 实验日志 → 500TB+
```

### 1.5 网络端口与交换机规划

```text
典型 InfiniBand/RoCE 训练网络（以 NVIDIA Quantum-2 交换机为参考）：
  每卡 1 个 NDR 400Gb/s 端口（DGX H100 自带 8×ConnectX-7）
  交换机端口数：64 口 QSFP（Quantum-2 QM9700）

Fat-tree 拓扑计算：
  单交换机可直连：64 / 2 = 32 台 8 卡机（半端口上行）
  两层 Fat-tree（leaf-spine）：
    Leaf 交换机数 = ceil(节点数 / 32)
    Spine 交换机数 = ceil(Leaf 数 × 32 / 64)
    示例：64 台 DGX → 2 leaf + 1 spine = 3 台交换机
    示例：256 台 DGX → 8 leaf + 4 spine = 12 台交换机

端口规划表：
  训练网络（IB/RoCE）：每卡 1 口，NDR 400Gb/s
  存储网络：每节点 2×100GbE（多路径冗余）
  管理网络：每节点 1×25GbE（BMC/IPMI + PXE + 监控）
  业务网络：每节点 2×25GbE bond（K8s Pod 流量）
```

---

## 2 多租户治理完整方案

### 2.1 Namespace + ResourceQuota + LimitRange

```yaml
# tenant-quota.yaml
# 适用: K8s 1.31 / v1 ResourceQuota & LimitRange（v1 自 K8s 1.0 GA，稳定 API）
apiVersion: v1
kind: Namespace
metadata:
  name: team-nlp
  labels:
    tenant: team-nlp
    cost-center: "CC-2024-NLP"
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-nlp-quota
  namespace: team-nlp
spec:
  hard:
    requests.cpu: "200"
    requests.memory: "800Gi"
    requests.nvidia.com/gpu: "64"
    limits.cpu: "400"
    limits.memory: "1600Gi"
    limits.nvidia.com/gpu: "64"
    pods: "200"
    services: "20"
    persistentvolumeclaims: "50"
    requests.storage: "50Ti"
---
apiVersion: v1
kind: LimitRange
metadata:
  name: team-nlp-limits
  namespace: team-nlp
spec:
  limits:
    # Pod 级别默认值
    - type: Container
      default:
        cpu: "4"
        memory: "16Gi"
        nvidia.com/gpu: "1"
      defaultRequest:
        cpu: "2"
        memory: "8Gi"
        nvidia.com/gpu: "1"
      max:
        cpu: "64"
        memory: "256Gi"
        nvidia.com/gpu: "8"
      min:
        cpu: "500m"
        memory: "1Gi"
        nvidia.com/gpu: "0"
```

### 2.2 队列体系（Kueue ClusterQueue + Cohort）

```yaml
# kueue-cluster-queues.yaml
# 适用: K8s 1.31 / Kueue v0.9.x（kueue.x-k8s.io/v1beta1，v1beta1 自 Kueue v0.5 GA）
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: cluster-queue-main
spec:
  cohort: gpu-cluster          # 同一 cohort 内的队列可互相借用
  namespaceSelector: {}        # 匹配所有 namespace
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: h100-flavor
          resources:
            - name: "cpu"
              nominalQuota: "400"
              borrowingLimit: "100"
              lendingLimit: "80"
            - name: "memory"
              nominalQuota: "1600Gi"
              borrowingLimit: "400Gi"
              lendingLimit: "320Gi"
            - name: "nvidia.com/gpu"
              nominalQuota: "128"
              borrowingLimit: "32"
              lendingLimit: "16"
        - name: a100-flavor
          resources:
            - name: "nvidia.com/gpu"
              nominalQuota: "64"
              borrowingLimit: "16"
              lendingLimit: "8"
  preemption:
    reclaimWithinCohort: Any   # 可回收 cohort 内任何借用资源
    borrowWithinCohort:
      policy: LowerPriority
      maxPriorityThreshold: 1000
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: team-nlp-queue
  namespace: team-nlp
spec:
  clusterQueue: cluster-queue-main
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: team-cv-queue
  namespace: team-cv
spec:
  clusterQueue: cluster-queue-main
```

**Volcano 队列方案（可选替代）**：

```yaml
# volcano-queues.yaml
# 适用: K8s 1.31 / Volcano v1.10.x（scheduling.volcano.sh/v1beta1，Queue API 自 Volcano v1.0 稳定）
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: team-nlp
spec:
  weight: 40                   # 权重占比（所有队列权重之和为分母）
  capability:
    cpu: "200"
    memory: "800Gi"
    nvidia.com/gpu: "64"
  reclaimable: true            # 允许被回收
  guarantee:                   # 最低保证资源（不可被抢占）
    resource:
      cpu: "100"
      memory: "400Gi"
      nvidia.com/gpu: "32"
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: team-cv
spec:
  weight: 30
  capability:
    cpu: "150"
    memory: "600Gi"
    nvidia.com/gpu: "48"
  reclaimable: true
  guarantee:
    resource:
      cpu: "80"
      memory: "320Gi"
      nvidia.com/gpu: "24"
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: team-infra
spec:
  weight: 10
  capability:
    cpu: "100"
    memory: "400Gi"
    nvidia.com/gpu: "16"
  reclaimable: true
  guarantee:
    resource:
      cpu: "20"
      memory: "80Gi"
      nvidia.com/gpu: "4"
```

### 2.3 优先级与抢占（PriorityClass 分层设计）

```yaml
# priority-classes.yaml
# 适用: K8s 1.31 / scheduling.k8s.io/v1 PriorityClass（v1 自 K8s 1.14 GA，稳定 API）
# 第一层：集群关键服务（不可被抢占）
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: system-critical
value: 1000000
globalDefault: false
description: "集群核心组件（GPU Operator、监控、调度器），不可抢占"
preemptionPolicy: Never
---
# 第二层：生产推理服务
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: production-inference
value: 100000
globalDefault: false
description: "线上推理服务，仅可抢占开发和实验作业"
preemptionPolicy: PreemptLowerPriority
---
# 第三层：生产训练作业
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: production-training
value: 50000
globalDefault: false
description: "计划内的大规模训练，可抢占低优先级作业"
preemptionPolicy: PreemptLowerPriority
---
# 第四层：开发调试
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: development
value: 10000
globalDefault: false
description: "开发调试用，可被生产作业抢占"
preemptionPolicy: PreemptLowerPriority
---
# 第五层：实验 / 低优先级 / 可中断
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: best-effort
value: 1000
globalDefault: true
description: "弹性实验作业，最先被抢占，默认优先级"
preemptionPolicy: PreemptLowerPriority
```

**抢占规则与公告模板**：

```text
【GPU 集群资源抢占公告】

触发时间：2026-03-15 14:32:08 CST
触发原因：production-inference 作业 pod-infer-llama3-7b-xxx 需要 8×H100，
         当前队列无可用资源，触发抢占。
被抢占作业：
  - team-nlp/pod-exp-john-001 (development, 4×H100, 运行 2h15m)
  - team-nlp/pod-exp-jane-003 (best-effort, 2×H100, 运行 0h42m)
释放资源：6×H100 + 借用 team-cv 队列 2×H100
恢复预计：被抢占作业将在资源空闲后自动重新调度（需开启 retryPolicy）
建议：
  - 开发/实验作业请设置 checkpoint 间隔 ≤ 15 分钟
  - 长期训练请申请 production-training 优先级
联系人：GPU 平台组 @oncall（企业微信/Slack 频道 #gpu-cluster-ops）
```

### 2.4 租户隔离检查清单

| 检查项 | 检查命令 / 方法 | 通过标准 |
|--------|----------------|---------|
| Namespace 已创建且标签完整 | `kubectl get ns team-nlp -o yaml` | 有 tenant + cost-center label |
| ResourceQuota 已配置 | `kubectl get quota -n team-nlp` | GPU/CPU/Mem/Storage 全部设限 |
| LimitRange 已配置 | `kubectl get limitrange -n team-nlp` | 有 max GPU 限制 |
| NetworkPolicy 隔离租户网络 | `kubectl get networkpolicy -n team-nlp` | 默认 deny-all + 白名单 ingress |
| RBAC 限制跨 namespace 访问 | `kubectl get rolebinding -n team-nlp` | 无 cluster-admin binding |
| PVC StorageClass 受控 | `kubectl get sc` | 租户只能使用指定 StorageClass |
| Pod 安全标准 | `kubectl get ns team-nlp -o yaml` | `pod-security.kubernetes.io/enforce: restricted` |
| 队列绑定正确 | `kubectl get localqueue -n team-nlp` | 指向正确的 ClusterQueue |

---

## 3 配额借还与公平调度原理

### 3.1 Fair-share 算法思路

```text
公平份额定义：
  fair_share_i = (weight_i / Σweight) × total_resources

瞬时借用/出借计算：
  若 tenant_i 当前使用 < fair_share_i → 有多余可出借
  若 tenant_i 当前使用 > nominal_quota_i → 正在借用他人配额

借用优先级排序（Kueue/Volcano 均采用）：
  1. 归还优先级：谁被借走的最多，先归还谁
  2. 借用上限：borrowingLimit 字段限制单租户最大借用量
  3. 借用时间衰减：部分实现支持 "借用越久优先级越低" 的软约束
```

### 3.2 借用与回收（Reclaim）机制

```text
场景示例（Kueue）：
  集群总量：128×H100
  team-nlp nominal: 64，borrowingLimit: 32，lendingLimit: 16
  team-cv  nominal: 48，borrowingLimit: 16，lendingLimit: 8
  team-infra nominal: 16，borrowingLimit: 8，lendingLimit: 4

  T0 时刻：
    team-nlp 使用 80 卡（超出 nominal 16 卡，从 team-cv 借 12 + team-infra 借 4）
    team-cv 使用 40 卡（未超 nominal）
    team-infra 使用 8 卡（未超 nominal）

  T1 时刻：team-cv 提交新作业需要 50 卡
    → 触发 reclaim：team-nlp 被要求归还 12 卡（从 team-cv 借的部分）
    → team-nlp 有 10 分钟 grace period 做 checkpoint
    → grace period 结束后强制驱逐低优先级 pod
```

### 3.3 闲置配额识别

```bash
# 识别过去 7 天 GPU 分配率 < 30% 的 namespace
# 适用: K8s 1.31 / kubectl + awk 解析；Prometheus 2.55 LTS PromQL（subquery 语法自 Prometheus 2.9+）
kubectl get pods --all-namespaces \
  -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.spec.containers[*].resources.requests.nvidia\.com/gpu}{"\n"}{end}' \
  | awk '{ns[$1]+=$2} END {for (n in ns) print n, ns[n]}' \
  | sort -k2 -rn

# Prometheus 查询：过去 7 天 namespace GPU 使用峰值
# 在 Grafana 中使用以下 PromQL
max_over_time(
  sum by (namespace) (
    kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  )[7d:1h]
)
```

---

## 4 利用率分析体系

### 4.1 分配率 vs 真实利用率定义

```text
分配率（Allocation Rate）：
  = 已分配 GPU 卡数 / 集群总 GPU 卡数
  含义：资源被"占位"的程度，不代表真正在算

真实利用率（True Utilization / SM_ACTIVE）：
  = GPU SM（Streaming Multiprocessor）活跃时间占比
  采集方式：DCGM Exporter → DCGM_FI_PROF_SM_ACTIVE 指标
  健康阈值：训练 ≥ 70%，推理 ≥ 40%（参考值，因模型而异）
```

**PromQL 查询**（Prometheus 2.55 LTS；`* on (pod, namespace) group_left()` join 语法自 Prometheus 2.x 起稳定）：

```promql
# 适用: Prometheus 2.55 LTS / kube-state-metrics 2.13.x / dcgm-exporter 4.x
# 集群整体分配率
sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
  / sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})

# 单节点真实利用率（SM_ACTIVE，5 分钟均值）
avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])

# 按 namespace 统计真实利用率（需 join pod 与 DCGM 指标）
avg by (namespace) (
  DCGM_FI_PROF_SM_ACTIVE
  * on (pod, namespace) group_left()
  kube_pod_info
)

# 集群整体真实利用率
avg(DCGM_FI_PROF_SM_ACTIVE)
```

### 4.2 空转卡识别与治理流程

**空转卡定义**：分配了 GPU 但 SM_ACTIVE < 5%（阈值可调）持续超过 30 分钟的 Pod。

**完整识别 PromQL**（Prometheus 2.55 LTS；`avg_over_time` subquery 语法稳定，`and on()` 集合运算符自 Prometheus 2.x 起可用）：

```promql
# 适用: Prometheus 2.55 LTS / dcgm-exporter 4.x（DCGM_FI_PROF_SM_ACTIVE 指标名前缀在 3.x/4.x 相同）
# 空转卡识别：SM_ACTIVE 持续低于 5%，过去 30 分钟
(
  DCGM_FI_PROF_SM_ACTIVE < 0.05
)
and on (pod, namespace)
(
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
)
# 持续 30 分钟（用 subquery 判断）
and (
  avg_over_time(DCGM_FI_PROF_SM_ACTIVE[30m]) < 0.05
)
```

**治理 SOP（发现→通报→宽限→回收）**：

```text
SOP-UTIL-001：空转 GPU 卡治理流程

步骤 1 [自动] 发现（每分钟执行）
  - Prometheus AlertRule 触发：sm_active < 5% 持续 30m
  - 自愈系统通过 webhook 接收告警
  - 查询 pod 的 owner、namespace、已运行时长

步骤 2 [自动] 通报（发现后 1 分钟内）
  - 向 pod owner 发送企业微信/Slack 通知
  - 通知内容：pod 名称、GPU 卡数、当前 SM_ACTIVE、已空转时长
  - 同时通知团队管理员

步骤 3 [人工/自动] 宽限期（通知后 60 分钟）
  - Pod annotation 标记：gpu-platform/empty-idle-since=<timestamp>
  - 宽限期内 owner 可恢复使用（SM_ACTIVE 回升则自动取消标记）
  - 如 owner 回复"继续使用"原因，延长宽限至 4 小时

步骤 4 [自动] 回收（宽限期结束后）
  - 先触发作业 checkpoint（发 SIGUSR1 信号，作业自行保存）
  - 等待 10 分钟让 checkpoint 完成
  - 执行 kubectl delete pod（保留 Job 对象，状态标记为 IdleKilled）
  - 释放的 GPU 回到队列可分配池
  - 记录回收事件到审计日志
```

### 4.3 利用率周报模板

```markdown
## GPU 集群利用率周报（第 W 周：YYYY-MM-DD ~ YYYY-MM-DD）

### 集群总览

| 指标 | 本周 | 上周 | 环比 |
|------|------|------|------|
| 总 GPU 卡数 | 512 | 512 | - |
| 平均分配率 | 89.2% | 86.5% | +2.7pp |
| 平均真实利用率（SM_ACTIVE） | 61.3% | 58.7% | +2.6pp |
| 空转卡时（GPU·hour） | 1,247 | 2,103 | -40.7% |
| 碎片率 | 4.2% | 5.1% | -0.9pp |

### 按团队明细

| 团队 | 配额 | 分配率 | 真实利用率 | 空转卡时 | 趋势 | 备注 |
|------|------|--------|-----------|---------|------|------|
| team-nlp | 64×H100 | 95% | 72% | 186 | ↑ | 70B 训练收尾 |
| team-cv | 48×H100 | 88% | 65% | 312 | → | 稳定 |
| team-rec | 32×H100 | 76% | 41% | 523 | ↓ | 数据清洗中 |
| team-infra | 16×H100 | 62% | 28% | 226 | ↓ | 空闲待分配 |

### 本周重点事件
1. team-rec 连续 3 天空转超 40 卡时，已通报整改
2. team-nlp 70B 训练第 15 天 checkpoint 异常重启 1 次（XID 79，已自愈）
3. 碎片整理窗口（周日 02:00~06:00）成功合并 12 张碎片卡

### 下周建议
- team-rec 完成数据清洗后释放 16 卡临时配额
- team-nlp 启动第二阶段训练前确认 checkpoint 策略
```

### 4.4 碎片率计算与碎片整理策略

```text
碎片率定义：
  碎片率 = 1 - (最大可调度连续 GPU 块 / 空闲 GPU 总数)

  示例：节点 A 有 2 卡空闲，节点 B 有 1 卡空闲，节点 C 有 3 卡空闲
    空闲总数 = 6
    最大连续块 = 3（节点 C）
    碎片率 = 1 - 3/6 = 50%

碎片率 PromQL（需自定义指标或 node-exporter 脚本；Prometheus 2.55 LTS，`max by ()` 全局聚合语法稳定）：
  1 - (
    max by () (
      sum by (node) (
        kube_node_status_allocatable{resource="nvidia_com_gpu"}
        - sum by (node) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
      )
    )
    / sum(kube_node_status_allocatable{resource="nvidia_com_gpu"} -
          sum by (node) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"}))
  )
```

**碎片整理策略**：

```text
策略 1：Binpack 调度（减少碎片产生）
  - 调度器优先填满已有部分占用的节点
  - Volcano binpack plugin / Kueue 配合 node-label 打分
  - 配置示例（Volcano v1.10.x scheduler-config.yaml 片段，binpack plugin 自 Volcano v1.4+ 稳定）：
    plugins:
      binpack:
        weight: 10
        binpack.weight: 10
        binpack.cpu: 1
        binpack.memory: 1
        binpack.resources: nvidia.com/gpu
        binpack.resources.nvidia.com/gpu: 10

策略 2：小作业驱逐重排窗口
  - 每周固定时间窗口（如周日 02:00~06:00）
  - 识别只使用 1~2 卡的小作业
  - 通知 owner 后驱逐并重新调度，腾出连续大块
  - 窗口期间禁止新的大作业提交

策略 3：节点预留池
  - 保留 10% 节点作为 "大块预留池"
  - 只允许 ≥ 4 卡的作业调度到预留节点
  - 小作业强制调度到普通节点
```

---

## 5 利用率提升手段详解表

| 手段 | 适用场景 | 实施方式 | 预期提升 | 风险/注意事项 |
|------|---------|---------|---------|-------------|
| MIG 切分（A100/H100） | 开发调试、小模型实验 | `nvidia-smi mig -cg 0 -c 1g.5gb` 创建 1g.5gb 实例（driver R550+，A100/H100 数据中心卡） | 单卡利用率 +30~60% | MIG 实例间无 P2P；不支持 NVLink 跨实例 |
| 在离线混部（潮汐调度） | 推理白天高峰 + 训练夜间空闲 | 白天在线推理 + 夜间 batch 训练共享同一节点 | 整体利用率 +15~25% | 需严格的资源隔离（cgroup v2 + CPU pinning） |
| Backfill | 大作业等待时填充小作业 | Volcano backfill plugin 或 Kueue 自动 backfill | 等待期利用率 +10~20% | 小作业必须能在 gap 时间内完成 |
| 僵尸作业自动清理 | 长期 Running 但 GPU 空闲 | CronJob 定时扫描 + 通知/回收 | 释放 5~15% 被浪费的卡 | 误杀需有 checkpoint 恢复机制 |
| 弹性训练 | 训练作业可容忍卡数变化 | PyTorch Elastic（torchrun）+ Kueue elastic workload | 部分卡故障时继续训练 | 需模型/框架支持弹性 |
| GPU 共享（Time-slicing） | 推理/开发多用户共享 | GPU Operator v24.9.2 time-slicing 配置 | 开发环境利用率 +40% | 不支持显存隔离，需信任用户 |

### 5.1 在离线混部（潮汐调度）实施要点

```text
原理：
  在线服务（推理）白天流量高、夜间流量低
  离线作业（训练）对延迟不敏感、可抢占
  夜间将空闲推理节点用于训练，白天训练让位给推理

实施要点：
  1. 资源隔离：cgroup v2 + CPU pinning 确保离线不抢占在线 CPU
  2. GPU 隔离：在线服务绑定特定 GPU（nodeSelector/affinity）
  3. 网络隔离：离线训练走独立 RDMA 网口，不影响推理 SLO
  4. 调度策略：
     - 在线 Pod：PriorityClass = production-inference，不可抢占
     - 离线 Pod：PriorityClass = best-effort，可被在线自动抢占
  5. 监控：SM_ACTIVE + P99 latency 双指标，latency 超阈值自动驱逐离线

CronJob 定时切换示例（06:00 驱逐所有离线训练；适用 K8s 1.31 / kubectl 1.31）：
  0 6 * * * kubectl delete job -l tier=offline-batch --all-namespaces
```

### 5.2 僵尸作业自动清理（CronJob 完整实现）

```yaml
# zombie-job-cleaner.yaml
# 适用: K8s 1.31 / batch/v1 CronJob（v1 自 K8s 1.21 GA）
# 脚本依赖: kubectl 1.31 / DCGM Exporter 4.x（Prometheus 指标 DCGM_FI_PROF_SM_ACTIVE）/ python3 3.10（Ubuntu 22.04）/ curl / bc
apiVersion: batch/v1
kind: CronJob
metadata:
  name: gpu-zombie-cleaner
  namespace: gpu-platform
spec:
  schedule: "*/15 * * * *"     # 每 15 分钟扫描一次
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          serviceAccountName: gpu-zombie-cleaner
          restartPolicy: Never
          containers:
            - name: cleaner
              image: bitnami/kubectl:1.31  # 与集群 K8s 1.31 对齐（bitnami 镜像 tag 与 K8s minor 同步）
              command: ["/bin/bash", "-c"]
              args:
                - |
                  #!/bin/bash
                  set -euo pipefail

                  IDLE_THRESHOLD=5        # SM_ACTIVE < 5% 视为空闲
                  IDLE_DURATION_MIN=30    # 持续 30 分钟
                  DRY_RUN="${DRY_RUN:-true}"
                  NOTIFY_WEBHOOK="${NOTIFY_WEBHOOK:-}"

                  echo "[$(date)] Starting GPU zombie scan..."

                  # 获取所有使用 GPU 的 Running pod
                  PODS=$(kubectl get pods --all-namespaces \
                    -l 'gpu-platform/managed=true' \
                    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.startTime}{"\n"}{end}')

                  CLEANED=0
                  NOTIFIED=0

                  while IFS=$'\t' read -r NS NAME UID START_TIME; do
                    [ -z "$NS" ] && continue

                    # 计算运行时长（分钟）
                    START_EPOCH=$(date -d "$START_TIME" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$START_TIME" +%s)
                    NOW_EPOCH=$(date +%s)
                    RUN_MIN=$(( (NOW_EPOCH - START_EPOCH) / 60 ))

                    # 跳过运行不足 IDLE_DURATION_MIN 分钟的 pod
                    [ "$RUN_MIN" -lt "$IDLE_DURATION_MIN" ] && continue

                    # 查询该 pod 的 GPU SM_ACTIVE（通过 DCGM Exporter → Prometheus）
                    SM_ACTIVE=$(curl -s "http://prometheus:9090/api/v1/query" \
                      --data-urlencode "query=avg_over_time(DCGM_FI_PROF_SM_ACTIVE{pod=\"$NAME\", namespace=\"$NS\"}[${IDLE_DURATION_MIN}m])" \
                      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['result'][0]['value'][1] if d['data']['result'] else '1')" 2>/dev/null || echo "1")

                    SM_PCT=$(echo "$SM_ACTIVE * 100" | bc | cut -d. -f1)

                    if [ "$SM_PCT" -lt "$IDLE_THRESHOLD" ]; then
                      echo "[$(date)] ZOMBIE: $NS/$NAME SM_ACTIVE=${SM_PCT}% runtime=${RUN_MIN}min"

                      # 发送通知
                      if [ -n "$NOTIFY_WEBHOOK" ]; then
                        curl -s -X POST "$NOTIFY_WEBHOOK" \
                          -H "Content-Type: application/json" \
                          -d "{\"text\":\"[GPU僵尸告警] $NS/$NAME SM_ACTIVE=${SM_PCT}% 已运行${RUN_MIN}分钟，将被回收\"}"
                      fi

                      if [ "$DRY_RUN" = "false" ]; then
                        # 发 SIGUSR1 触发 checkpoint（需作业自行处理此信号）
                        kubectl exec -n "$NS" "$NAME" -- kill -USR1 1 2>/dev/null || true
                        sleep 30  # 等待 checkpoint

                        # 删除 pod（Job controller 会记录状态）
                        kubectl delete pod -n "$NS" "$NAME" --grace-period=120
                        CLEANED=$((CLEANED + 1))
                      else
                        NOTIFIED=$((NOTIFIED + 1))
                      fi
                    fi
                  done <<< "$PODS"

                  echo "[$(date)] Scan complete. Notified: $NOTIFIED, Cleaned: $CLEANED (DRY_RUN=$DRY_RUN)"
              env:
                - name: DRY_RUN
                  value: "true"           # 生产环境改为 false
                - name: NOTIFY_WEBHOOK
                  value: "https://hooks.slack.com/services/xxx/yyy/zzz"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gpu-zombie-cleaner
  namespace: gpu-platform
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: gpu-zombie-cleaner
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "delete", "exec"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: gpu-zombie-cleaner
subjects:
  - kind: ServiceAccount
    name: gpu-zombie-cleaner
    namespace: gpu-platform
roleRef:
  kind: ClusterRole
  name: gpu-zombie-cleaner
  apiGroup: rbac.authorization.k8s.io
```

---

## 6 成本核算

### 6.1 卡时计费模型设计

**不同卡型加权系数表**（以 H100 SXM 为基准 1.0；BF16 稠密峰值 TFLOPS 来自 NVIDIA 各卡型官方 datasheet，非稀疏值；功耗为 TDP/TGP 典型值）：

| 卡型 | 算力参考（BF16 TFLOPS） | 功耗（W） | 加权系数 | 说明 |
|------|------------------------|-----------|---------|------|
| H100 SXM 80GB | 989 | 700 | 1.000 | 基准卡型 |
| H100 NVL 94GB | 835 | 400 | 0.850 | 功耗低但算力略低 |
| H200 SXM 141GB | 989 | 700 | 1.150 | 显存翻倍溢价 |
| A100 SXM 80GB | 312 | 400 | 0.450 | 上一代旗舰 |
| A100 PCIe 80GB | 312 | 300 | 0.400 | PCIe 版略低 |
| A100 SXM 40GB | 312 | 400 | 0.350 | 显存较少 |
| L40S | 362 | 350 | 0.380 | 推理优化卡 |
| A30 | 165 | 165 | 0.180 | 入门推理 |
| MIG 1g.5gb (A100) | ~40 | ~57 | 0.064 | 1/7 A100 |
| MIG 3g.20gb (A100) | ~120 | ~171 | 0.193 | 3/7 A100 |

```text
标准卡时 = 实际卡时 × 加权系数
单价参考：H100 标准卡时 = ¥30~50/卡时（内部核算参考，非市场价）
```

### 6.2 按 namespace/队列统计卡时的实现

```yaml
# prometheus-recording-rules.yaml
# 适用: K8s 1.31 / Prometheus 2.55 LTS（recording rule 语法；increase() / label_replace() 自 Prometheus 2.x 起稳定）
# PrometheusRule CRD: monitoring.coreos.com/v1（prometheus-operator v0.63+ 支持）
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: gpu-cost-recording-rules
  namespace: monitoring
spec:
  groups:
    - name: gpu_allocation_recording
      interval: 1m
      rules:
        # 每分钟记录每个 pod 的 GPU 请求数
        - record: gpu:pod_requests:count
          expr: |
            sum by (namespace, pod, node) (
              kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
            )

        # 每分钟的 GPU 卡·分钟数（用于累加卡时）
        - record: gpu:namespace:gpu_minutes_per_hour
          expr: |
            sum by (namespace) (
              increase(gpu:pod_requests:count[1h])
            ) / 60

        # 按 namespace 的 H100 等效卡时（每小时快照）
        - record: gpu:namespace:h100_equivalent_hours
          expr: |
            sum by (namespace) (
              gpu:pod_requests:count
              * on (node) group_left()
              label_replace(
                label_replace(
                  kube_node_labels,
                  "gpu_type", "$1", "label_nvidia_com_gpu_product", "(.*)"
                ),
                "weight", "1.0", "gpu_type", "H100.*"
              )
            )

        # 空转卡时统计（SM_ACTIVE < 5%）
        - record: gpu:namespace:idle_gpu_hours
          expr: |
            sum by (namespace) (
              (DCGM_FI_PROF_SM_ACTIVE < 0.05)
              * on (pod, namespace) group_left()
              kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
            ) * (1/60)

    - name: gpu_cost_daily
      interval: 1h
      rules:
        # 每日 namespace 成本（H100 标准卡时 × 单价）
        - record: gpu:namespace:daily_cost_yuan
          expr: |
            sum by (namespace) (
              increase(gpu:pod_requests:count[24h])
            ) / 60 * 40
          labels:
            cost_unit: "CNY"
            gpu_base_price: "40"
```

### 6.3 内部账单报表生成思路

```text
数据流：
  Prometheus recording rules → 每小时聚合 → Thanos/Cortex 长期存储
                                              ↓
                              Grafana Dashboard（实时看板）
                                              ↓
                              每日 CronJob → 查询 Prometheus → 写入 PostgreSQL
                                              ↓
                              月度报表（按团队/项目/卡型汇总）

月度账单 SQL 示例：
  SELECT
    namespace AS team,
    SUM(gpu_hours) AS total_gpu_hours,
    SUM(gpu_hours * weight) AS h100_equiv_hours,
    SUM(gpu_hours * weight * 40) AS cost_cny,
    AVG(sm_active) AS avg_utilization
  FROM gpu_usage_daily
  WHERE date BETWEEN '2026-03-01' AND '2026-03-31'
  GROUP BY namespace
  ORDER BY cost_cny DESC;
```

### 6.4 TCO 概念与电费计算示例

```text
TCO（Total Cost of Ownership）= 硬件采购 + 电力 + 制冷 + 机房 + 运维人力 + 网络

电费计算示例（1000 张 H100 集群）：
  单卡 TDP：700W
  整机（8 卡 + CPU + 网络）：≈ 10.2kW
  125 台 DGX H100 整机：125 × 10.2kW = 1,275kW

  PUE（Power Usage Effectiveness）= 1.3（液冷参考值，风冷通常 1.5~1.8）
  总用电功率 = 1,275kW × 1.3 = 1,657.5kW

  年用电量 = 1,657.5kW × 8,760h = 14,519,700 kWh ≈ 1,452 万度

  电价参考（工商业用电，含基本电费）：≈ ¥0.8/度
  年电费 = 14,519,700 × 0.8 = ¥11,615,760 ≈ ¥1,162 万/年

  对比参考：
    1000 张 H100 采购成本（含整机 + 交换机 + 布线）≈ ¥3~4 亿
    年电费占硬件采购成本 ≈ 3~4%
    5 年 TCO 电费占比 ≈ 15~20%
```

---

## 7 电力与散热运营

### 7.1 PUE 定义与测量

```text
PUE = 数据中心总用电功率 / IT 设备用电功率

  PUE = 1.0 → 理论最优（无制冷/照明/UPS 损耗）
  PUE = 1.3 → 液冷数据中心优秀水平
  PUE = 1.5 → 传统风冷数据中心一般水平
  PUE = 1.8+ → 老旧 IDC 或制冷效率差

测量方法：
  1. IT 功率：从 PDU（Power Distribution Unit）读取各机柜实际功率
  2. 总功率：从 UPS 输出侧或变压器二次侧读取
  3. 计算公式：PUE = UPS_output / Σ(PDU_per_rack)

  实时监控方案：
    - 智能 PDU 通过 SNMP/Modbus 上报功率（每 10s 采样）
    - Prometheus snmp_exporter 采集 PDU 指标
    - Grafana 仪表盘展示实时 PUE 趋势
```

### 7.2 功率封顶实操

```bash
# 单卡功率封顶（限制为 500W，从 700W 降低）
# nvidia-smi -pl 支持范围：数据中心卡（A100/H100/H200 等）driver R450+；不适用于消费级 GeForce
# 基线驱动：R570（H100/Blackwell）或 R550（Ampere 存量）；参数值必须在 min/max power limit 范围内（nvidia-smi -q 可查看）
nvidia-smi -pl 500

# 查看当前功率限制
nvidia-smi --query-gpu=power.limit,power.draw --format=csv

# 恢复默认功率限制
nvidia-smi -pl 700       # H100 默认 700W
nvidia-smi -r              # 重置为默认值
```

**K8s 节点级批量设置脚本**：

```bash
#!/bin/bash
# gpu-power-cap.sh - 批量设置 GPU 功率封顶
# 适用: K8s 1.31 / 数据中心 GPU（H100/A100）driver R550+ / nvidia-smi -pl 命令
# 注意：nvidia-smi -pl 仅支持数据中心卡（Tesla/Data Center），GeForce 卡不支持此参数
# 用法: ./gpu-power-cap.sh <target_watts> [node_label_selector]
#
# 示例: ./gpu-power-cap.sh 500 "node-role=gpu-worker"

set -euo pipefail

TARGET_WATTS="${1:?Usage: $0 <watts> [label_selector]}"
LABEL_SELECTOR="${2:-gpu=true}"
LOG_FILE="/var/log/gpu-power-cap-$(date +%Y%m%d).log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# 获取目标节点列表
NODES=$(kubectl get nodes -l "$LABEL_SELECTOR" -o jsonpath='{.items[*].metadata.name}')

if [ -z "$NODES" ]; then
  log "ERROR: No nodes found with label: $LABEL_SELECTOR"
  exit 1
fi

log "Setting power cap to ${TARGET_WATTS}W on $(echo $NODES | wc -w) nodes"

# 创建 DaemonSet Job 来执行 nvidia-smi
cat <<EOF | kubectl apply -f -
# 适用: K8s 1.31 / apps/v1 DaemonSet（v1 自 K8s 1.9 GA）
# 镜像: nvidia/cuda:12.4.0-base-ubuntu22.04（CUDA 12.4 base 镜像仅含 nvidia-smi，不含完整 toolkit；tag 钉死，禁 latest）
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: gpu-power-cap
  namespace: gpu-platform
  labels:
    app: gpu-power-cap
spec:
  selector:
    matchLabels:
      app: gpu-power-cap
  template:
    metadata:
      labels:
        app: gpu-power-cap
    spec:
      nodeSelector:
        $(echo "$LABEL_SELECTOR" | tr '=' ': ')
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      hostPID: true
      containers:
        - name: power-cap
          image: nvidia/cuda:12.4.0-base-ubuntu22.04
          command:
            - /bin/bash
            - -c
            - |
              nvidia-smi -pl ${TARGET_WATTS} && \
              nvidia-smi --query-gpu=index,power.limit --format=csv,noheader && \
              echo "Power cap set successfully" && \
              sleep infinity
          securityContext:
            privileged: true
          volumeMounts:
            - name: device-plugins
              mountPath: /var/lib/kubelet/device-plugins
      volumes:
        - name: device-plugins
          hostPath:
            path: /var/lib/kubelet/device-plugins
      terminationGracePeriodSeconds: 5
EOF

# 等待 DaemonSet ready
kubectl rollout status daemonset/gpu-power-cap -n gpu-platform --timeout=300s

log "Power cap DaemonSet deployed. Verifying..."

# 验证（从每个节点读取当前 power limit）
kubectl get pods -n gpu-platform -l app=gpu-power-cap -o name | while read pod; do
  RESULT=$(kubectl exec -n gpu-platform "$pod" -- nvidia-smi --query-gpu=index,power.limit --format=csv,noheader 2>/dev/null)
  NODE=$(kubectl get pod -n gpu-platform "$pod" -o jsonpath='{.spec.nodeName}')
  log "Node $NODE: $RESULT"
done

# 清理 DaemonSet（设置是一次性操作，nvidia-smi -pl 持久化到驱动）
kubectl delete daemonset gpu-power-cap -n gpu-platform
log "Cleanup complete. Power cap persists across reboots until nvidia-smi -r is run."
```

**功率封顶对性能影响参考数据**（以 H100 SXM 700W 为基准）：

| 功率限制 | 相对性能（BF16 TFLOPS） | 性能损失 | 节电比例 | 适用场景 |
|---------|------------------------|---------|---------|---------|
| 700W（默认） | 989 TFLOPS | 0% | 基准 | 高性能训练 |
| 600W | ~915 TFLOPS | ~7.5% | ~14% | 性价比优化 |
| 500W | ~815 TFLOPS | ~17.6% | ~29% | 非紧迫训练 |
| 400W | ~680 TFLOPS | ~31.2% | ~43% | 开发/推理 |
| 300W | ~510 TFLOPS | ~48.4% | ~57% | 轻量推理 |

> 注：以上数据为基于 NVIDIA power-cap 曲线的估算参考值，实际因 workload 特征（compute-bound vs memory-bound）而异。

### 7.3 液冷运维要点

```text
冷板式液冷关键参数：
  冷却液温度（供水）：18~25°C（推荐 20°C）
  冷却液流量：每机柜 15~25 L/min
  冷却液压差：供回水压差 0.3~0.8 bar
  GPU 结温（junction temp）：< 85°C（报警），< 95°C（降频）

漏液检测系统：
  - 漏液绳（leak detection rope）沿冷板下方敷设
  - 检测到漏液 → 立即关闭该节点 CDU 阀门 → 告警 → 工单
  - 测试频率：每季度模拟漏液测试一次

水质管理：
  - 电导率：< 20 μS/cm（超标→腐蚀风险）
  - pH 值：8.0~9.5（冷却液通常为丙二醇+缓蚀剂）
  - 颗粒度：< 5μm（过滤器每季度更换）
  - 生物污染：每季度添加杀菌剂（如 DBNPA）

CDU（Cooling Distribution Unit）运维：
  - 泵运行状态：转速/电流/振动监控
  - 换热器效率：一次侧/二次侧温差（ΔT < 3°C → 效率下降告警）
  - 冗余泵切换测试：每月一次
  - 冷却液补液：液位低于 MIN 时自动补液 + 告警
```

### 7.4 温度与降频联动监控

```promql
# 适用: Prometheus 2.55 LTS / dcgm-exporter 4.x（DCGM_FI_DEV_GPU_TEMP / DCGM_FI_DEV_PSTATE 指标名在 3.x/4.x 相同）
# GPU 温度监控（DCGM Exporter 指标）
# 当前温度
DCGM_FI_DEV_GPU_TEMP

# 降频检测（Performance State 降低 = 降频）
DCGM_FI_DEV_PSTATE < 0    # P0 = 正常，P2/P8 = 降频

# 温度阈值告警规则
- alert: GPUTemperatureHigh
  expr: DCGM_FI_DEV_GPU_TEMP > 85
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "GPU {{ $labels.gpu }} on {{ $labels.node }} 温度 {{ $value }}°C"

- alert: GPUTemperatureCritical
  expr: DCGM_FI_DEV_GPU_TEMP > 95
  for: 30s
  labels:
    severity: critical
  annotations:
    summary: "GPU {{ $labels.gpu }} on {{ $labels.node }} 温度 {{ $value }}°C，即将降频"

# 降频事件关联查询
# 当温度超过 85°C 时检查 PSTATE 是否下降
(DCGM_FI_DEV_GPU_TEMP > 85) and (DCGM_FI_DEV_PSTATE > 0)
```
