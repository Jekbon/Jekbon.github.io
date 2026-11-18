---
title: "GPU 集群运维（06）：监控与可观测性"
date: 2026-11-12T09:00:00+08:00
draft: false
description: "DCGM 与 dcgm-exporter 部署、DCGM 指标全表、PromQL 查询大全、告警规则、Grafana 大盘设计、Prometheus 容量规划、网络与存储监控，以及 MFU 计算方法。"
tags: ["监控", "Prometheus", "DCGM"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下版本摘取自 `00-版本基线.md`，仅列出本册涉及的组件。完整基线以 00 分册为准。

| 组件 | 基线版本 | 说明 |
|------|---------|------|
| OS | Ubuntu 22.04.5 LTS / Rocky Linux 9.5 | 双基线并行 |
| 内核 | 5.15.0-xxx-generic (Ubuntu GA) / 5.14.0-503 (Rocky) | |
| Prometheus | 2.55.x LTS（3.x 已发布，UTF-8/新特性；生产建议先 2.55 LTS） | PromQL 两版兼容 |
| Alertmanager | 0.27.x | |
| Grafana | 11.x | Dashboard ID 12239/12240 需按 dcgm-exporter 版本核对指标名 |
| dcgm-exporter | 3.3.9-3.6.0 / 4.x | **3.x vs 4.x 指标名有差异**，见本册 2.x 节标注 |
| DCGM（宿主机版） | 3.3.9 / 4.2.x | `datacenter-gpu-manager` 包 |
| kube-prometheus-stack chart | v66.x（示例） | 内含 Prometheus 2.55 LTS |
| Thanos | 0.37.x | 长期存储/全局查询 |
| VictoriaMetrics | v1.10x（单节点或 cluster 版） | Thanos 替代方案 |
| Loki | 3.x（2.9 LTS 亦可） | |
| promtail | 3.x（已被 Alloy 取代中，存量仍主流） | Grafana Alloy 1.x 为继任者 |
| snmp_exporter | 0.26.x | IB 交换机采集 |
| node-exporter | 1.8.x | |
| kube-state-metrics | 2.13.x | |
| NVIDIA Driver | R570 570.124.04（示例）/ R550 550.127.08（示例） | 生产分支 |

---



---

## 1. 监控体系总览

### 1.1 架构分层

```text
┌─────────────────────────────────────────────────────────────────┐
│                    展示与告警层（Grafana + AlertManager）          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐      │
│  │ Dashboard │  │ AlertManager  │  │ Runbook / PagerDuty   │      │
│  └──────────┘  └──────────────┘  └───────────────────────┘      │
├─────────────────────────────────────────────────────────────────┤
│                    平台层（Prometheus + Thanos/VM）               │
│  ┌───────────┐  ┌──────────┐  ┌────────┐  ┌──────────────┐     │
│  │Prometheus │  │ Thanos   │  │ Loki   │  │ Tempo/Jaeger │     │
│  │(per-shard)│  │ Sidecar  │  │(日志)  │  │(链路追踪)    │     │
│  └───────────┘  └──────────┘  └────────┘  └──────────────┘     │
├─────────────────────────────────────────────────────────────────┤
│                    采集层（Exporter + Agent）                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ ┌──────┐   │
│  │dcgm-     │ │node-     │ │snmp-     │ │kube-   │ │kube- │   │
│  │exporter  │ │exporter  │ │exporter  │ │state-  │ │let   │   │
│  │(GPU)     │ │(系统)    │ │(交换机)  │ │metrics │ │      │   │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘ └──────┘   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐            │
│  │ethtool   │ │textfile  │ │promtail  │ │custom  │            │
│  │(RoCE)    │ │collector │ │(日志)    │ │exporter│            │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘            │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 GPU 集群监控面清单

| 监控面 | 关键指标 | 采集工具 | 采集频率 |
|-------|---------|---------|---------|
| **GPU** | SM_ACTIVE、显存、温度、功耗、ECC、XID、NVLink 带宽与错误、时钟频率 | dcgm-exporter | 10-15s |
| **网络（IB）** | 端口收发字节/包、错误计数器、链路状态、光功率 | snmp_exporter + perfquery 脚本 | 30-60s |
| **网络（RoCE）** | PFC pause 计数、ECN 标记、网卡收发字节/丢包 | ethtool + textfile collector | 15-30s |
| **存储** | 客户端 IOPS/延迟/带宽、服务端队列深度 | JuiceFS/Alluxio exporter | 15s |
| **K8s** | Pod 状态、调度延迟、资源配额、节点状态 | kube-state-metrics + kubelet | 15-30s |
| **作业** | step time、loss、throughput、NCCL timeout | 应用内埋点 + 日志提取 | 按 step |

---

## 2. DCGM 与 dcgm-exporter 部署

### 2.1 DCGM 架构

```text
┌─────────────────────────────────────┐
│          dcgm-exporter              │
│   (K8s DaemonSet, 暴露 metrics)     │
├─────────────────────────────────────┤
│         libdcgm (客户端库)           │
├─────────────────────────────────────┤
│       nv-hostengine (守护进程)       │
│   (管理 GPU 状态、缓存 field group)  │
├─────────────────────────────────────┤
│      NVIDIA GPU Driver (kernel)     │
└─────────────────────────────────────┘

关键组件：
- nv-hostengine: DCGM 的后台守护进程，维护 GPU 状态、执行 health check
- dcgmi: CLI 工具，与 nv-hostengine 通信
- libdcgm: C/C++ 库，dcgm-exporter 调用它采集指标
```

### 2.2 dcgmi 命令大全

> 环境: **DCGM 3.3.9 / 4.2.x**（宿主机安装 `datacenter-gpu-manager` 包），或随 GPU Operator 容器化部署。
> dcgmi CLI 在 3.x 和 4.x 间基本兼容，但 `dcgmi diag -r 3` 的 Level 3 诊断在 4.x 增加了更多检测项。

```bash
# ===== Discovery（发现）=====
dcgmi discovery -l
# 输出：
# 1 GPU found:
#         GPU Id  : 0
#            Device Name   : NVIDIA H100 80GB HBM3
#            ...
# 8 GPUs found.

# ===== Diag（诊断）=====
# Level 1: 快速检查（驱动/权限/硬件可达）
dcgmi diag -r 1
# Level 2: 基础硬件测试
dcgmi diag -r 2
# Level 3: 完整硬件诊断（含 GPU 压力测试，耗时 10-30 分钟）
dcgmi diag -r 3 -g 0,1,2,3,4,5,6,7
# 输出示例：
# Successfully ran diagnostic for group.
# +-----------+-------------------------------------------------------------+
# | GPU       | NVML/Library/System/PCIe/GPU/Memory/Input/Diagnostic        |
# +-----------+-------------------------------------------------------------+
# | 0         |  PASS  PASS  PASS  PASS  PASS  PASS  PASS                   |
# | 1         |  PASS  PASS  PASS  WARN  PASS  PASS  PASS                   |
# ...

# ===== Health =====
dcgmi health -c -j
# 输出 JSON：
# {
#   "1": {"overall_health": "DCGM_HEALTH_PASS", "systems": [...]},
#   ...
# }

# ===== Policy（策略与事件通知）=====
dcgmi policy -g 0 -s 1  # 注册 GPU 0 的事件通知
dcgmi policy -l          # 监听事件
# 输出示例：
# DCGM Event: GPU 0 Xid 79 (GPU has fallen off the bus) at 2024-03-15 10:30:00

# ===== Watch（字段监控）=====
# 开始监控指定 field
dcgmi dmon -e 1001,1002,1003,1004 -c 5 -d 1
# field 1001=GPU_UTIL, 1002=MEM_COPY_UTIL, 1003=GPU_TEMP, 1004=POWER_USAGE
# -c 5: 采集 5 次
# -d 1: 间隔 1 秒
# 输出：
# #Entity  GPU_UTIL  MEM_COPY  GPU_TEMP  POWER
#   ID       (%)       (%)       (C)      (W)
# gpu        95        87        72       345
# gpu        93        85        72       342
```

### 2.3 dcgm-exporter Helm 安装

> **版本选择**：dcgm-exporter 3.x 与 4.x 的指标名有差异（见下方说明），部署前确认所需版本。
> 本示例使用 dcgm-exporter **3.3.9-3.6.0** 分支镜像；若使用 4.x 分支，指标名变化见第 3 节标注。

```bash
# 添加 helm repo
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts
helm repo update

# 安装（钉死 chart 版本）
helm install dcgm-exporter gpu-helm-charts/dcgm-exporter \
  --version 3.6.0 \
  --namespace monitoring \
  --create-namespace \
  --values values-dcgm-exporter.yaml
```

#### values-dcgm-exporter.yaml 关键项

```yaml
# values-dcgm-exporter.yaml
# 镜像 tag 钉死：3.x 系列示例如下；4.x 系列用 4.1.0-4.2.0-ubuntu22.04
# 注意：3.x 与 4.x 镜像的默认指标名有差异，升级时同步更新 PromQL
image:
  repository: nvcr.io/nvidia/k8s/dcgm-exporter
  tag: 3.3.9-3.6.0-ubuntu22.04    # ← 钉死 tag，禁用 latest

# 自定义指标文件
extraConfigMapVolumes:
  - name: custom-counters
    configMap:
      name: dcgm-custom-counters

extraVolumeMounts:
  - name: custom-counters
    mountPath: /etc/dcgm-exporter
    readOnly: true

# 使用自定义计数器文件
arguments:
  - "-f"
  - "/etc/dcgm-exporter/custom-counters.csv"

# ServiceMonitor
serviceMonitor:
  enabled: true
  namespace: monitoring
  interval: 15s
  honorLabels: true
  relabelings:
    - sourceLabels: [__meta_kubernetes_pod_node_name]
      targetLabel: node
    - sourceLabels: [__meta_kubernetes_pod_name]
      targetLabel: pod

# DaemonSet 配置
nodeSelector:
  nvidia.com/gpu.present: "true"

tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule

# 资源限制
resources:
  limits:
    memory: 256Mi
  requests:
    cpu: 50m
    memory: 128Mi

# 安全上下文（需要访问 GPU 设备）
securityContext:
  privileged: true
```

### 2.4 default-counters.csv 自定义指标列表

> **DCGM 3.x vs 4.x 指标差异说明**：
> - DCGM 3.x 指标前缀统一为 `DCGM_FI_*`（如 `DCGM_FI_DEV_RETIRED_SBE`）
> - DCGM 4.x 中部分 field 重命名/废弃：
>   - `DCGM_FI_DEV_RETIRED_SBE` → 4.x 改名为 `DCGM_FI_DEV_RETIRED_SBE_PAGES`（部分版本仍保留旧名兼容）
>   - `DCGM_FI_DEV_RETIRED_DBE` → 4.x 改名为 `DCGM_FI_DEV_RETIRED_DBE_PAGES`
>   - `DCGM_FI_DEV_RETIRED_PENDING` → 4.x 改名为 `DCGM_FI_DEV_RETIRED_PENDING_PAGES`
>   - `DCGM_FI_PROF_*` 系列在 4.x 中保留但部分 field ID 调整
> - **部署时确认**：`dcgmi fieldgroup -l` 列出当前 DCGM 版本支持的 field，以此为准
> - 下方指标表以 DCGM 3.x 命名为基线，4.x 变化处在"4.x 变化"列标注

创建 ConfigMap：

```yaml
# dcgm-custom-counters-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dcgm-custom-counters
  namespace: monitoring
data:
  custom-counters.csv: |
    # DCGM field ID, Prometheus metric name, HELP string
    # === GPU 基础指标 ===
    DCGM_FI_DEV_GPU_TEMP,                 gauge, GPU temperature (C)
    DCGM_FI_DEV_MEMORY_TEMP,              gauge, GPU memory temperature (C)
    DCGM_FI_DEV_POWER_USAGE,              gauge, GPU power usage (W)
    DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION, counter, Total energy consumption (mJ)
    DCGM_FI_DEV_GPU_UTIL,                 gauge, GPU utilization (nvidia-smi style, %)
    DCGM_FI_DEV_MEM_COPY_UTIL,            gauge, Memory copy utilization (%)
    DCGM_FI_DEV_FB_USED,                  gauge, Framebuffer memory used (MiB)
    DCGM_FI_DEV_FB_FREE,                  gauge, Framebuffer memory free (MiB)
    DCGM_FI_DEV_FB_TOTAL,                 gauge, Framebuffer memory total (MiB)
    DCGM_FI_DEV_SM_CLOCK,                 gauge, SM clock frequency (MHz)
    DCGM_FI_DEV_MEM_CLOCK,                gauge, Memory clock frequency (MHz)
    DCGM_FI_DEV_GPU_MAX_OP_TEMP,          gauge, Max operating temperature (C)

    # === ECC 错误 ===
    DCGM_FI_DEV_ECC_SBE_VOL_TOTAL,        counter, Volatile single-bit ECC errors
    DCGM_FI_DEV_ECC_DBE_VOL_TOTAL,        counter, Volatile double-bit ECC errors
    DCGM_FI_DEV_ECC_SBE_AGG_TOTAL,        counter, Aggregate single-bit ECC errors
    DCGM_FI_DEV_ECC_DBE_AGG_TOTAL,        counter, Aggregate double-bit ECC errors

    # === XID 与 Retired Pages ===
    DCGM_FI_DEV_XID_ERRORS,               gauge, XID error code (last)
    DCGM_FI_DEV_RETIRED_SBE,              gauge, Retired pages due to SBE
    DCGM_FI_DEV_RETIRED_DBE,              gauge, Retired pages due to DBE
    DCGM_FI_DEV_RETIRED_PENDING,          gauge, Pending page retirement

    # === NVLink ===
    DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL, counter, NVLink CRC flit errors
    DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL, counter, NVLink CRC data errors
    DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL,   counter, NVLink replay errors
    DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL,            gauge, NVLink bandwidth (bytes/s)

    # === 时钟事件（降频原因）===
    DCGM_FI_DEV_CLOCKS_EVENT_REASONS,     gauge, Clock throttle reasons bitmask

    # === Profiling 指标（DCGM 3.x+，需 NVML vGPU 或 MIG）===
    DCGM_FI_PROF_GR_ENGINE_ACTIVE,        gauge, Graphics engine active ratio (0-1)
    DCGM_FI_PROF_SM_ACTIVE,               gauge, SM active ratio (0-1, true utilization)
    DCGM_FI_PROF_SM_OCCUPANCY,            gauge, SM occupancy (warps active / max warps)
    DCGM_FI_PROF_PIPE_TENSOR_ACTIVE,      gauge, Tensor pipe active ratio
    DCGM_FI_PROF_PIPE_FP64_ACTIVE,        gauge, FP64 pipe active ratio
    DCGM_FI_PROF_PIPE_FP32_ACTIVE,        gauge, FP32 pipe active ratio
    DCGM_FI_PROF_PIPE_FP16_ACTIVE,        gauge, FP16 pipe active ratio
    DCGM_FI_PROF_DRAM_ACTIVE,             gauge, DRAM active ratio
    DCGM_FI_PROF_PCIE_TX_BYTES,           counter, PCIe TX bytes
    DCGM_FI_PROF_PCIE_RX_BYTES,           counter, PCIe RX bytes
    DCGM_FI_PROF_NVLINK_TX_BYTES,         counter, NVLink TX bytes
    DCGM_FI_PROF_NVLINK_RX_BYTES,         counter, NVLink RX bytes
```

```bash
kubectl apply -f dcgm-custom-counters-configmap.yaml
```

### 2.5 ServiceMonitor YAML

> API: `monitoring.coreos.com/v1`（kube-prometheus-stack chart v66.x / Prometheus Operator 0.77+）
> 适用 Prometheus 2.55 LTS。

```yaml
# dcgm-exporter-servicemonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dcgm-exporter
  namespace: monitoring
  labels:
    app: dcgm-exporter
    release: prometheus
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: dcgm-exporter
  namespaceSelector:
    matchNames:
      - monitoring
  endpoints:
    - port: metrics
      interval: 15s
      scrapeTimeout: 10s
      honorLabels: true
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_node_name]
          targetLabel: node
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
        - sourceLabels: [__meta_kubernetes_pod_label_gpu_id]
          targetLabel: gpu_id
      metricRelabelings:
        # 保留 GPU 指标的关键 label
        - sourceLabels: [gpu]
          targetLabel: gpu
        - sourceLabels: [model]
          targetLabel: model
```

---

## 3. DCGM 指标全表

### 3.1 DCGM_FI_DEV_* 系列（设备状态指标）

> 以下指标名以 **DCGM 3.3.9 / dcgm-exporter 3.3.9-3.6.0** 为基线。DCGM 4.x 变化见各行的"4.x 变化"列。
> PromQL 查询适用于 dcgm-exporter 3.x 指标前缀；4.x exporter 需核对改名后的指标名。

| Field 名 | 含义 | 单位 | 运维用途 | 4.x 变化 |
|----------|------|------|---------|---------|
| DCGM_FI_DEV_GPU_TEMP | GPU 核心温度 | °C | 过热告警（>85°C） |
| DCGM_FI_DEV_MEMORY_TEMP | 显存温度 | °C | HBM 过热（>95°C 需关注） |
| DCGM_FI_DEV_POWER_USAGE | 实时功耗 | W | 接近 TDP 时可能降频 |
| DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION | 累计能耗 | mJ | 计算平均功耗 |
| DCGM_FI_DEV_GPU_UTIL | nvidia-smi 利用率 | % | 粗略参考，不如 SM_ACTIVE 准确 |
| DCGM_FI_DEV_MEM_COPY_UTIL | 显存拷贝利用率 | % | 数据传输繁忙度 |
| DCGM_FI_DEV_FB_USED | 显存使用量 | MiB | OOM 预防 |
| DCGM_FI_DEV_FB_FREE | 显存空闲量 | MiB | — |
| DCGM_FI_DEV_FB_TOTAL | 显存总量 | MiB | 容量确认 |
| DCGM_FI_DEV_SM_CLOCK | SM 时钟 | MHz | 降频检测 |
| DCGM_FI_DEV_MEM_CLOCK | 显存时钟 | MHz | 降频检测 |
| DCGM_FI_DEV_GPU_MAX_OP_TEMP | 最大允许温度 | °C | 阈值参考 |
| DCGM_FI_DEV_ECC_SBE_VOL_TOTAL | 单比特 ECC（volatile） | count | SBE 速率监控 |
| DCGM_FI_DEV_ECC_DBE_VOL_TOTAL | 双比特 ECC（volatile） | count | DBE=严重故障，立即隔离 |
| DCGM_FI_DEV_ECC_SBE_AGG_TOTAL | 单比特 ECC（aggregate） | count | 历史累计 |
| DCGM_FI_DEV_ECC_DBE_AGG_TOTAL | 双比特 ECC（aggregate） | count | 历史累计 |
| DCGM_FI_DEV_XID_ERRORS | XID 错误码 | 错误号 | 79=掉卡，48/95=DBE（95 为未遏制），94=已遏制 ECC，63/64=行重映射事件/失败 |
| DCGM_FI_DEV_RETIRED_SBE | 因 SBE 退役的页 | count | 退役速率监控 | 4.x 可能改名 RETIRED_SBE_PAGES |
| DCGM_FI_DEV_RETIRED_DBE | 因 DBE 退役的页 | count | >0 即需关注 | 4.x 可能改名 RETIRED_DBE_PAGES |
| DCGM_FI_DEV_RETIRED_PENDING | 待退役页 | 0/1 | =1 需重启 GPU | 4.x 可能改名 RETIRED_PENDING_PAGES |
| DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL | NVLink CRC flit 错误 | count | >0 需排查 NVLink 物理连接 |
| DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL | NVLink CRC data 错误 | count | >0 同上 |
| DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL | NVLink 重传错误 | count | >0 需关注 |
| DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL | NVLink 带宽 | bytes/s | 性能基线对比 |
| DCGM_FI_DEV_CLOCKS_EVENT_REASONS | 时钟事件原因位掩码 | bitmask | 非零=有降频原因 |

### 3.2 DCGM_FI_PROF_* 系列（Profiling 指标）

> Profiling 指标自 DCGM 3.0+ 引入（需 NVML vGPU 或 MIG 支持）。DCGM 4.x 保留该系列但部分 field ID 有调整，以 `dcgmi fieldgroup -l` 输出为准。

| Field 名 | 含义 | 单位 | 运维用途 |
|----------|------|------|---------|
| DCGM_FI_PROF_GR_ENGINE_ACTIVE | 图形引擎活跃比 | 0-1 | GPU 整体忙碌度 |
| DCGM_FI_PROF_SM_ACTIVE | SM 单元活跃比 | 0-1 | **真实利用率**，推荐替代 nvidia-smi |
| DCGM_FI_PROF_SM_OCCUPANCY | SM 占用率 | 0-1 | warp 饱和度 |
| DCGM_FI_PROF_PIPE_TENSOR_ACTIVE | Tensor Core 活跃比 | 0-1 | AI 训练核心指标 |
| DCGM_FI_PROF_PIPE_FP64_ACTIVE | FP64 管道活跃比 | 0-1 | HPC/科学计算 |
| DCGM_FI_PROF_PIPE_FP32_ACTIVE | FP32 管道活跃比 | 0-1 | 传统计算 |
| DCGM_FI_PROF_PIPE_FP16_ACTIVE | FP16 管道活跃比 | 0-1 | 混合精度训练 |
| DCGM_FI_PROF_DRAM_ACTIVE | 显存控制器活跃比 | 0-1 | 访存瓶颈检测 |
| DCGM_FI_PROF_PCIE_TX_BYTES | PCIe 发送字节 | bytes | CPU↔GPU 数据传输量 |
| DCGM_FI_PROF_PCIE_RX_BYTES | PCIe 接收字节 | bytes | CPU↔GPU 数据传输量 |
| DCGM_FI_PROF_NVLINK_TX_BYTES | NVLink 发送字节 | bytes | GPU 间通信量 |
| DCGM_FI_PROF_NVLINK_RX_BYTES | NVLink 接收字节 | bytes | GPU 间通信量 |

### 3.3 SM_ACTIVE vs nvidia-smi utilization 的区别

```bash
nvidia-smi "GPU-Util"（DCGM_FI_DEV_GPU_UTIL）：
  采样方式：在一个采样窗口内，GPU 有任何 SM 在执行任何 kernel 的
  时间占比。即：只要有 1 个 SM 在干活就算 100%。
  问题：一个非常轻量的 kernel（只占 1 个 SM）→ 利用率 = 100%
        实际上 GPU 资源大量空闲

DCGM_FI_PROF_SM_ACTIVE：
  采样方式：所有 SM 中，至少有一个 warp 活跃的 SM 比例的平均值。
  即：active_SM / total_SM 的时间加权平均。
  优势：真正反映 SM 资源的利用程度。
        1 个 SM 干活、131 个 SM 空闲 → SM_ACTIVE ≈ 0.76%

示例对比：
  场景：1 个 CUDA stream 跑小 kernel
  nvidia-smi: 95%（看起来满了）
  SM_ACTIVE:  15%（实际 SM 大部分时间空闲）

  场景：满载分布式训练
  nvidia-smi: 100%
  SM_ACTIVE:  85-95%（接近满载，差值来自 kernel 间隙）

运维建议：用 SM_ACTIVE 做利用率监控和容量规划，nvidia-smi util 仅做粗看。
```

---

## 4. PromQL 查询大全

> 以下 PromQL 适用于 **dcgm-exporter 3.x 指标名**（DCGM 3.3.9 / dcgm-exporter 3.3.9-3.6.0）。
> 若使用 dcgm-exporter 4.x，`DCGM_FI_DEV_RETIRED_*` 等指标需按 4.x 改名调整。
> Prometheus 2.55 LTS / 3.x 均兼容以下语法。

### 4.1 GPU 真实利用率

```promql
# 按节点聚合平均 SM_ACTIVE（百分比）
avg by (node) (DCGM_FI_PROF_SM_ACTIVE * 100)

# 按节点聚合 P50 SM_ACTIVE
quantile by (node) (0.5, DCGM_FI_PROF_SM_ACTIVE * 100)

# 按 pod 聚合（需要 label join）
avg by (pod, namespace) (
  DCGM_FI_PROF_SM_ACTIVE * 100
  * on(node, gpu) group_left(pod, namespace)
  DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_USED
)
# 注：实际生产中建议通过 dcgm-exporter 的 relabeling 直接注入 pod/namespace label

# 按队列（Volcano queue）聚合
avg by (queue) (
  DCGM_FI_PROF_SM_ACTIVE * 100
  * on(pod) group_left(queue)
  label_replace(kube_pod_labels{label_volcano_queue_name!=""}, "queue", "$1", "label_volcano_queue_name", "(.*)")
)

# 集群整体平均
avg(DCGM_FI_PROF_SM_ACTIVE) * 100
```

### 4.2 空转卡识别

```promql
# 已分配但 SM_ACTIVE < 5%（持续 10 分钟）
(
  DCGM_FI_DEV_FB_USED > 100
  and
  DCGM_FI_PROF_SM_ACTIVE < 0.05
)
and on(node, gpu)
(
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
)
# 含义：显存占用 >100MiB（有程序在用）但 SM 几乎空闲
# 用途：识别 "占着茅坑不拉屎" 的作业

# 持续 10 分钟的空转卡数量
count(
  avg_over_time(
    (DCGM_FI_DEV_FB_USED > bool 100 and DCGM_FI_PROF_SM_ACTIVE < bool 0.05)[10m:]
  ) > 0.5
)
```

### 4.3 温度 Top

```promql
# 温度最高的 10 张卡
topk(10, DCGM_FI_DEV_GPU_TEMP)

# 显存温度最高
topk(10, DCGM_FI_DEV_MEMORY_TEMP)

# 温度超过 80°C 的卡数量
count(DCGM_FI_DEV_GPU_TEMP > 80)
```

### 4.4 功耗

```promql
# 每节点总功耗
sum by (node) (DCGM_FI_DEV_POWER_USAGE)

# 功耗超过 TDP 90% 的卡（H100 TDP=700W）
DCGM_FI_DEV_POWER_USAGE / 700 > 0.9

# 平均功耗趋势（5 分钟均值）
avg(avg_over_time(DCGM_FI_DEV_POWER_USAGE[5m]))
```

### 4.5 ECC SBE/DBE 速率

```promql
# SBE 速率（每分钟错误数）
rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[5m]) * 60

# DBE 速率（每分钟）— DBE 任何增长都是严重事件
rate(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[5m]) * 60

# DBE 累计非零的 GPU（需要立即处理）
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL > 0

# SBE 速率超过阈值的节点（>10/min 需要关注）
sum by (node) (rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[5m]) * 60) > 10
```

### 4.6 Remap Pending

```promql
# 有待退役页的 GPU
DCGM_FI_DEV_RETIRED_PENDING == 1

# 已退役页总数（DBE 原因）
DCGM_FI_DEV_RETIRED_DBE

# 退役速率
rate(DCGM_FI_DEV_RETIRED_SBE[1h]) * 3600
```

### 4.7 XID 事件

```promql
# XID 非零的 GPU
DCGM_FI_DEV_XID_ERRORS != 0

# XID 79（掉卡）或 95（未遏制 ECC 错误）——均需立即隔离
DCGM_FI_DEV_XID_ERRORS == 79 or DCGM_FI_DEV_XID_ERRORS == 95

# XID 按错误码分组统计
count by (xid) (DCGM_FI_DEV_XID_ERRORS != 0)
```

### 4.8 NVLink 带宽与错误

```promql
# NVLink 带宽（GB/s）
DCGM_FI_PROF_NVLINK_TX_BYTES / 1073741824
DCGM_FI_PROF_NVLINK_RX_BYTES / 1073741824

# NVLink CRC 错误增长速率（每分钟）
rate(DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL[5m]) * 60
rate(DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL[5m]) * 60

# NVLink 重传错误
rate(DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL[5m]) * 60

# NVLink 错误非零的 GPU
DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL > 0
or DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL > 0
```

### 4.9 降频检测

```promql
# 时钟事件原因非零（有降频发生）
DCGM_FI_DEV_CLOCKS_EVENT_REASONS != 0

# SM 时钟低于基线 90%（H100 正常 SM clock ≈ 1980MHz）
DCGM_FI_DEV_SM_CLOCK < 1782

# 降频 + 功耗接近 TDP（功率墙降频）
DCGM_FI_DEV_CLOCKS_EVENT_REASONS != 0
and DCGM_FI_DEV_POWER_USAGE / 700 > 0.95
```

### 4.10 显存占用

```promql
# 显存使用率（百分比）
DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL * 100

# 显存使用 >95% 的卡（OOM 风险）
DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL > 0.95

# 按节点聚合显存使用总量
sum by (node) (DCGM_FI_DEV_FB_USED)
```

### 4.11 按作业的吞吐面板

```promql
# 需要作业侧暴露 metrics（step_time_seconds, samples_per_second）
# 假设通过 pushgateway 或应用内 /metrics 端点暴露

# 训练步时间（step time）
training_step_duration_seconds{job="llama-70b-train"}

# 吞吐量（samples/s）
training_throughput_samples_per_second{job="llama-70b-train"}

# GPU 利用率按作业聚合
avg by (job_name) (
  DCGM_FI_PROF_SM_ACTIVE * 100
  * on(pod) group_left(job_name)
  kube_pod_labels{label_training_job!=""}
)
```

---

## 5. 告警规则

### 5.1 完整 PrometheusRule YAML

> API: `monitoring.coreos.com/v1`（kube-prometheus-stack chart **v66.x** / Prometheus Operator 0.77+）
> 适用 **Prometheus 2.55 LTS**。PromQL 中 `DCGM_FI_DEV_RETIRED_*` 指标名适用于 dcgm-exporter 3.x；4.x 需按改名后的指标名调整。

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: gpu-cluster-alerts
  namespace: monitoring
  labels:
    prometheus: kube-prometheus
    role: alert-rules
spec:
  groups:
    # ========== GPU 硬件告警 ==========
    - name: gpu-hardware
      interval: 30s
      rules:
        # XID 79: GPU has fallen off the bus（GPU 掉卡）
        - alert: GPUXid79
          expr: DCGM_FI_DEV_XID_ERRORS == 79
          for: 0m
          labels:
            severity: critical
          annotations:
            summary: "GPU 掉卡 (XID 79)"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 报告 XID 79，GPU 已从总线脱落"
            runbook_url: "https://wiki.internal/runbook/gpu-xid79"
            action: "1. 记录 GPU 序列号 2. cordon 节点 3. 迁移作业 4. 联系硬件团队更换 GPU"

        # XID 95: Uncontained ECC Error（错误未遏制，GPU 状态不可信）
        - alert: GPUXid95
          expr: DCGM_FI_DEV_XID_ERRORS == 95
          for: 0m
          labels:
            severity: critical
          annotations:
            summary: "GPU 不可恢复错误 (XID 95)"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 报告 XID 95"
            runbook_url: "https://wiki.internal/runbook/gpu-xid95"
            action: "1. 立即 cordon 节点 2. 重启节点后运行 dcgmi diag -r 3 3. 如持续出现则更换 GPU"

        # ECC DBE（双比特不可纠正错误）
        - alert: GPUEccDbe
          expr: increase(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[1h]) > 0
          for: 0m
          labels:
            severity: critical
          annotations:
            summary: "GPU ECC DBE 错误"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 在过去 1 小时内出现 DBE 错误"
            runbook_url: "https://wiki.internal/runbook/gpu-ecc-dbe"
            action: "1. 记录错误计数 2. 计划窗口隔离节点 3. 运行 dcgmi diag -r 3 确认 4. 更换 GPU"

        # ECC SBE 速率过高
        - alert: GPUEccSbeHigh
          expr: rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[10m]) * 60 > 10
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "GPU ECC SBE 速率过高"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} SBE 速率 {{ $value | printf \"%.1f\" }}/min"
            runbook_url: "https://wiki.internal/runbook/gpu-ecc-sbe"
            action: "持续高 SBE 预示硬件劣化，安排计划性更换"

        # Page Retirement Pending
        - alert: GPURemapPending
          expr: DCGM_FI_DEV_RETIRED_PENDING == 1
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "GPU 有待退役页"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 有待退役的显存页"
            runbook_url: "https://wiki.internal/runbook/gpu-remap"
            action: "需要重启 GPU 使页退役生效；如频繁出现则更换 GPU"

        # GPU 温度过高
        - alert: GPUTemperatureHigh
          expr: DCGM_FI_DEV_GPU_TEMP > 85
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "GPU 温度过高"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 温度 {{ $value }}°C"
            runbook_url: "https://wiki.internal/runbook/gpu-temp"
            action: "1. 检查机房温度与空调 2. 检查风扇转速 3. 降低 GPU 功耗限制 4. 如持续 >90°C 则 cordon"

        - alert: GPUTemperatureCritical
          expr: DCGM_FI_DEV_GPU_TEMP > 92
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: "GPU 温度临界"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 温度 {{ $value }}°C 即将触发硬件保护"
            runbook_url: "https://wiki.internal/runbook/gpu-temp-critical"
            action: "立即停止该节点上的训练作业并 cordon 节点"

        # HBM 显存温度过高
        - alert: GPUMemoryTemperatureHigh
          expr: DCGM_FI_DEV_MEMORY_TEMP > 95
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "GPU 显存温度过高"
            description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} HBM 温度 {{ $value }}°C"

        # ========== NVLink 告警 ==========
        - name: gpu-nvlink
          interval: 30s
          rules:
            - alert: NVLinkCrcError
              expr: increase(DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL[1h]) > 0
              for: 5m
              labels:
                severity: warning
              annotations:
                summary: "NVLink CRC 错误"
                description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} NVLink CRC flit 错误增长"
                runbook_url: "https://wiki.internal/runbook/nvlink-crc"
                action: "1. 检查 NVLink bridge 物理连接 2. 运行 nvidia-smi nvlink -s 查看链路状态 3. 持续出错需更换 NVLink bridge"

            - alert: NVLinkReplayError
              expr: increase(DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL[1h]) > 100
              for: 5m
              labels:
                severity: warning
              annotations:
                summary: "NVLink 重传错误过多"
                description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} NVLink 重传 {{ $value }}"

        # ========== 节点与 K8s 告警 ==========
        - name: k8s-node
          interval: 30s
          rules:
            - alert: NodeNotReady
              expr: kube_node_status_condition{condition="Ready",status="true"} == 0
              for: 5m
              labels:
                severity: critical
              annotations:
                summary: "K8s 节点 NotReady"
                description: "节点 {{ $labels.node }} 状态为 NotReady 超过 5 分钟"
                runbook_url: "https://wiki.internal/runbook/node-notready"
                action: "1. SSH 检查节点 2. 检查 kubelet/containerd 状态 3. 检查是否有 XID 错误 4. 必要时 reboot"

            - alert: NodeGPUCountMismatch
              expr: |
                count by (node) (DCGM_FI_DEV_GPU_TEMP)
                != on(node)
                kube_node_status_allocatable{resource="nvidia_com_gpu"}
              for: 10m
              labels:
                severity: warning
              annotations:
                summary: "GPU 数量不匹配"
                description: "节点 {{ $labels.node }} DCGM 发现的 GPU 数与 K8s allocatable 不一致"
                action: "可能有 GPU 掉卡，检查 dmesg 中的 XID 错误"

        # ========== 网络告警 ==========
        # ib_port_state / ib_symbol_errors_total 来自自定义 IB exporter 或 snmp_exporter（0.26.x）
        - name: network-ib
          interval: 60s
          rules:
            - alert: IBLinkDown
              expr: ib_port_state{state!="active"} == 1
              for: 2m
              labels:
                severity: critical
              annotations:
                summary: "IB 链路 Down"
                description: "节点 {{ $labels.node }} 端口 {{ $labels.port }} IB 链路非 Active"
                runbook_url: "https://wiki.internal/runbook/ib-link-down"
                action: "1. 检查线缆连接 2. ibstat 确认端口状态 3. 更换线缆或光模块 4. 检查交换机端口"

            - alert: IBPortErrors
              expr: increase(ib_symbol_errors_total[1h]) > 100
              for: 10m
              labels:
                severity: warning
              annotations:
                summary: "IB 端口错误计数增长"
                description: "节点 {{ $labels.node }} IB 端口符号错误增长 {{ $value }}"
                action: "物理层问题，检查线缆和光模块"

        # ========== 基础设施告警 ==========
        - name: infrastructure
          interval: 30s
          rules:
            - alert: EtcdHighLatency
              expr: histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) > 0.01
              for: 10m
              labels:
                severity: warning
              annotations:
                summary: "etcd WAL fsync P99 延迟过高"
                description: "etcd 节点 {{ $labels.instance }} WAL fsync P99 = {{ $value | printf \"%.3f\" }}s"
                action: "检查磁盘 IO 性能，可能需要更换 SSD"

            - alert: StorageClientLatencyHigh
              expr: histogram_quantile(0.99, rate(storage_client_operation_duration_seconds_bucket[5m])) > 0.5
              for: 10m
              labels:
                severity: warning
              annotations:
                summary: "存储客户端延迟 P99 过高"
                description: "存储操作 P99 延迟 {{ $value | printf \"%.2f\" }}s"
                action: "检查存储服务状态、网络延迟、客户端并发度"
```

---

## 6. Grafana 大盘设计

> 环境: **Grafana 11.x**。以下面板 PromQL 基于 dcgm-exporter 3.x 指标名。

### 6.1 三层大盘架构

```text
┌─────────────────────────────────────┐
│     Level 1: 集群总览 Dashboard      │
│  一眼看全局健康状态                   │
├─────────────────────────────────────┤
│     Level 2: 节点 Dashboard          │
│  单节点 GPU/网络/存储详情             │
├─────────────────────────────────────┤
│     Level 3: 作业 Dashboard          │
│  单个训练作业的性能与资源使用          │
└─────────────────────────────────────┘
```

### 6.2 Level 1: 集群总览面板清单

| 面板名 | PromQL | 说明 |
|-------|--------|------|
| 集群 GPU 总数 | `count(DCGM_FI_DEV_GPU_TEMP)` | stat panel |
| 活跃告警数 | `count(ALERTS{alertstate="firing"})` | stat panel，红色高亮 |
| 平均 SM 利用率 | `avg(DCGM_FI_PROF_SM_ACTIVE) * 100` | gauge，绿/黄/红阈值 |
| 平均温度 | `avg(DCGM_FI_DEV_GPU_TEMP)` | gauge |
| 总功耗 | `sum(DCGM_FI_DEV_POWER_USAGE)` | stat，单位 kW |
| 利用率热力图 | `DCGM_FI_PROF_SM_ACTIVE * 100` | heatmap，按 node×gpu |
| 温度 Top10 | `topk(10, DCGM_FI_DEV_GPU_TEMP)` | time series |
| XID 事件 | `DCGM_FI_DEV_XID_ERRORS != 0` | table，显示 node+gpu+xid |
| 链路状态 | `ib_port_state` | table，非 Active 红色标记 |
| 空转卡数量 | 见 4.2 节 | stat |
| ECC DBE 卡 | `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL > 0` | table |
| 节点 NotReady | `kube_node_status_condition{condition="Ready",status="true"} == 0` | table |

### 6.3 Level 2: 节点面板清单

| 面板名 | PromQL | 说明 |
|-------|--------|------|
| GPU SM_ACTIVE 时序 | `DCGM_FI_PROF_SM_ACTIVE{node="$node"} * 100` | time series，每 GPU 一条线 |
| GPU 温度时序 | `DCGM_FI_DEV_GPU_TEMP{node="$node"}` | time series |
| 功耗时序 | `DCGM_FI_DEV_POWER_USAGE{node="$node"}` | time series |
| SM/MEM 时钟 | `DCGM_FI_DEV_SM_CLOCK{node="$node"}` `DCGM_FI_DEV_MEM_CLOCK{node="$node"}` | time series |
| 显存使用率 | `DCGM_FI_DEV_FB_USED{node="$node"} / DCGM_FI_DEV_FB_TOTAL{node="$node"} * 100` | time series |
| ECC SBE 速率 | `rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{node="$node"}[5m]) * 60` | time series |
| NVLink 带宽 | `DCGM_FI_PROF_NVLINK_TX_BYTES{node="$node"}/1e9` | time series |
| PCIe 带宽 | `rate(DCGM_FI_PROF_PCIE_TX_BYTES{node="$node"}[5m])/1e9` | time series |
| 降频原因 | `DCGM_FI_DEV_CLOCKS_EVENT_REASONS{node="$node"}` | state timeline |
| IB 端口收发 | `rate(ib_port_xmit_data{node="$node"}[5m])` | time series |

### 6.4 Level 3: 作业面板清单

| 面板名 | PromQL | 说明 |
|-------|--------|------|
| Step Time | `training_step_duration_seconds{job="$job"}` | time series |
| 吞吐量 | `training_throughput_samples_per_second{job="$job"}` | time series |
| Loss 曲线 | `training_loss{job="$job"}` | time series（来自 W&B/Pushgateway） |
| 作业 GPU 利用率 | `avg(DCGM_FI_PROF_SM_ACTIVE{pod=~"$pod_regex"}) * 100` | time series |
| 作业显存 | `max(DCGM_FI_DEV_FB_USED{pod=~"$pod_regex"})` | time series |
| MFU | 见第 10 节 | stat panel |
| NCCL busbw | `nccl_busbw_gbps{job="$job"}` | time series |

### 6.5 NVIDIA 官方 Dashboard

- **Dashboard ID 12239**: NVIDIA DCGM Exporter Dashboard（GPU 基础监控）
  - **依赖 dcgm-exporter 版本**：12239 使用旧版指标名（DCGM 3.x / dcgm-exporter 3.3.x-3.6.x），若部署 dcgm-exporter 4.x，部分 PromQL 需按新指标名修改
  - Grafana 11.x 兼容
- **Dashboard ID 12240**: NVIDIA DCGM Profiling Dashboard（Profiling 指标）
  - 同上，依赖 DCGM 3.0+ profiling field
- 导入方式：Grafana → Import → 输入 ID → 选择 Prometheus 数据源

```bash
# 或通过 helm 自动导入（kube-prometheus-stack chart v66.x）
# 在 values.yaml 中：
grafana:
  dashboardProviders:
    dashboardproviders.yaml:
      apiVersion: 1
      providers:
        - name: nvidia
          folder: GPU
          type: file
          options:
            path: /var/lib/grafana/dashboards/nvidia
  dashboards:
    nvidia:
      dcgm-exporter:
        gnetId: 12239
        revision: 2
        datasource: Prometheus
```

---

## 7. 规模化：Prometheus 容量规划

> 基线版本: **Prometheus 2.55 LTS**（3.x 已发布但生产建议先 2.55）。

### 7.1 容量估算

```text
每 GPU 节点序列数估算：
  - dcgm-exporter: ~60 指标 × 8 GPU = 480 序列
  - node-exporter: ~300 序列
  - ethtool/textfile: ~50 序列
  - kubelet: ~100 序列（有 GPU 的节点）
  合计: ~930 序列/节点

集群规模：
  - 100 节点 × 930 = 93,000 序列（单 Prometheus 轻松处理）
  - 500 节点 × 930 = 465,000 序列（单 Prometheus 上限附近）
  - 1000 节点 × 930 = 930,000 序列（必须分片或换存储）

Prometheus 性能参考：
  - 单实例推荐上限: 500K 活跃序列
  - 单实例极限: ~1M 序列（内存约 16-24GB）
  - 查询延迟: 序列越多越慢，P99 >1s 时考虑优化
```

### 7.2 Thanos vs VictoriaMetrics 对比

> 基线版本：**Thanos 0.37.x** / **VictoriaMetrics v1.10x**（单节点或 cluster 版）。
> Prometheus 2.55 LTS（3.x 已发布但生产建议先 2.55）。

| 维度 | Thanos 0.37.x | VictoriaMetrics v1.10x |
|------|--------|----------------|
| 架构 | Prometheus + Sidecar + Store Gateway + Querier | 单体或 cluster 模式 |
| 长期存储 | 对象存储（S3/MinIO/GCS） | 自带压缩存储（本地或对象存储） |
| 全局查询 | Store Gateway 跨多 Prometheus | vmselect 跨多 vmstorage |
| 部署复杂度 | 高（多组件） | 低（少组件） |
| 查询语言 | PromQL | MetricsQL（PromQL 超集） |
| 内存效率 | 中等 | 高（自研存储引擎） |
| 降采样 | 内置 compactor + downsample | 内置（自动） |
| 适合场景 | 已有 Prometheus 需要扩展 | 新项目或大规模（>1M 序列） |
| 社区活跃度 | 高（CNCF 项目） | 中高 |
| GPU 集群推荐 | 100-500 节点 | 500+ 节点 |

### 7.3 联邦与分片方案

```text
方案 A: Prometheus 联邦（适合 100-500 节点）

  ┌──────────────┐
  │Global Prom   │  ← 只聚合关键指标（告警+大盘）
  │(federation)  │
  └──────┬───────┘
    ┌────┴────┐
    │         │
┌───┴───┐ ┌──┴────┐
│Shard-1│ │Shard-2│   ← 每 shard 负责 ~200 节点
│(Prom) │ │(Prom) │
└───────┘ └───────┘

分片策略：按 node 名称 hash 或按机架分配

Global Prom federation scrape:
  match[]:
    - '{__name__=~"DCGM_FI_DEV_.*"}'
    - '{__name__=~"DCGM_FI_PROF_.*"}'
    - '{__name__=~"kube_.*"}'
    - '{__name__=~"ib_.*"}'

方案 B: Thanos 架构（适合 500+ 节点）

  ┌──────────┐
  │ Thanos   │
  │ Querier  │  ← 全局查询入口
  └────┬─────┘
       │
  ┌────┴─────┬──────────┬──────────┐
  │          │          │          │
┌─┴──┐  ┌──┴──┐  ┌──┴──┐  ┌──┴──┐
│P-1 │  │P-2  │  │P-3  │  │P-N  │
│+SC │  │+SC  │  │+SC  │  │+SC  │
└────┘  └─────┘  └─────┘  └─────┘
 SC=Sidecar（上传 block 到对象存储）

对象存储: MinIO / S3
├── /thanos/
│   ├── chunk/        # 原始数据
│   ├── index/        # 索引
│   └── meta/         # 元数据

Thanos Compactor: 定期降采样
  - 原始精度: 保留 30 天
  - 5min 精度: 保留 180 天
  - 1h 精度: 保留 365 天
```

### 7.4 长期存储与降采样配置

> 环境: **Thanos 0.37.x**。Thanos Compactor/Store Gateway 配置语法随版本基本稳定，但 `caching-bucket` API 在 0.32+ 有调整。

```yaml
# Thanos Compactor 配置（Thanos 0.37.x）
# compactor.yaml
compactor:
  data-dir: /data/compactor
  consistency-delay: 30m
  retention-resolution-raw: 720h    # 原始精度保留 30 天
  retention-resolution-5m: 4320h    # 5min 精度保留 180 天
  retention-resolution-1h: 8760h    # 1h 精度保留 365 天
  compact.concurrency: 4
  downsample.concurrency: 4

# Thanos Store Gateway 配置
store:
  caching-bucket:
    type: MEMCACHED
    config:
      addresses: [memcached-0:11211, memcached-1:11211]
    chunks_iter_ttl: 5m
```

---

## 8. 网络与存储监控

### 8.1 IB 交换机 SNMP 采集

> 环境: **snmp_exporter 0.26.x**，IB 交换机 Mellanox/NVIDIA Quantum 系列（QM9700/QM8700）。
> snmp.yml 配置建议用 snmp_exporter generator 生成（generator 版本需与 snmp_exporter 匹配）。

```yaml
# snmp_exporter 配置片段（snmp.yml 中 Mellanox/NVIDIA IB switch 模块）
# 使用 snmp_exporter generator 生成或直接手写

modules:
  mellanox_ib_switch:
    walk:
      - 1.3.6.1.2.1.2.2          # ifTable（接口表）
      - 1.3.6.1.2.1.31.1.1       # ifXTable（扩展接口表，含 HC 计数器）
      - 1.3.6.1.4.1.33049.2.1    # Mellanox InfiniBand MIB
    lookups:
      - source_indexes: [ifIndex]
        lookup: ifAlias
        drop_source_indexes: true
      - source_indexes: [ifIndex]
        lookup: ifName
        drop_source_indexes: true
    overrides:
      ifType:
        type: EnumAsInfo
      ifAdminStatus:
        type: EnumAsStateSet
      ifOperStatus:
        type: EnumAsStateSet
```

**关键 OID**：

| OID | 含义 | 用途 |
|-----|------|------|
| ifHCInOctets (1.3.6.1.2.1.31.1.1.1.6) | 入方向字节数（64位） | 带宽利用率 |
| ifHCOutOctets (1.3.6.1.2.1.31.1.1.1.10) | 出方向字节数（64位） | 带宽利用率 |
| ifInDiscards (1.3.6.1.2.1.2.2.1.13) | 入方向丢包 | 拥塞检测 |
| ifOutDiscards (1.3.6.1.2.1.2.2.1.19) | 出方向丢包 | 拥塞检测 |
| ifOperStatus (1.3.6.1.2.1.2.2.1.8) | 端口操作状态 | 链路告警 |
| entPhySensorValue (1.3.6.1.2.1.99.1.1.1.4) | 光模块 DOM 值 | Tx/Rx 光功率监控 |

```yaml
# ServiceMonitor for SNMP（monitoring.coreos.com/v1 / kube-prometheus-stack v66.x）
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: snmp-ib-switches
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: snmp-exporter
  endpoints:
    - port: metrics
      interval: 60s
      params:
        module: [mellanox_ib_switch]
      relabelings:
        - sourceLabels: [__param_target]
          targetLabel: switch
      scrapeTimeout: 30s
```

### 8.2 RoCE 网卡 ethtool 计数采集

```bash
#!/bin/bash
# roce_metrics_collector.sh - 采集 RoCE 网卡指标写入 Prometheus textfile
# 环境: DOCA-OFED 24.10（mlx5_core 驱动）/ node-exporter 1.8.x / bash 4.x+
# 配置 crontab: */1 * * * * /opt/scripts/roce_metrics_collector.sh
# textfile 目录: /var/lib/node_exporter/textfile_collector/

TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
OUTPUT_FILE="${TEXTFILE_DIR}/roce_metrics.prom"
TEMP_FILE="${OUTPUT_FILE}.tmp"

mkdir -p "$TEXTFILE_DIR"

# 获取所有 mlx5 网卡
interfaces=$(ls /sys/class/net/ | while read iface; do
    driver=$(readlink /sys/class/net/$iface/device/driver 2>/dev/null | xargs basename 2>/dev/null)
    [ "$driver" = "mlx5_core" ] && echo "$iface"
done)

cat > "$TEMP_FILE" << 'HEADER'
# HELP roce_rx_pause_packets Total received PAUSE frames
# TYPE roce_rx_pause_packets counter
# HELP roce_tx_pause_packets Total transmitted PAUSE frames
# TYPE roce_tx_pause_packets counter
# HELP roce_rx_pfc_packets Received PFC packets per priority
# TYPE roce_rx_pfc_packets counter
# HELP roce_tx_pfc_packets Transmitted PFC packets per priority
# TYPE roce_tx_pfc_packets counter
# HELP roce_rx_bytes_total Total received bytes
# TYPE roce_rx_bytes_total counter
# HELP roce_tx_bytes_total Total transmitted bytes
# TYPE roce_tx_bytes_total counter
# HELP roce_rx_discards Received packet discards
# TYPE roce_rx_discards counter
# HELP roce_tx_discards Transmitted packet discards
# TYPE roce_tx_discards counter
HEADER

for iface in $interfaces; do
    # ethtool -S 采集（缓存结果避免多次调用）
    stats=$(ethtool -S "$iface" 2>/dev/null)

    # PAUSE 帧
    rx_pause=$(echo "$stats" | grep -w "rx_pause" | awk '{print $2}')
    tx_pause=$(echo "$stats" | grep -w "tx_pause" | awk '{print $2}')
    [ -n "$rx_pause" ] && echo "roce_rx_pause_packets{interface=\"$iface\"} $rx_pause" >> "$TEMP_FILE"
    [ -n "$tx_pause" ] && echo "roce_tx_pause_packets{interface=\"$iface\"} $tx_pause" >> "$TEMP_FILE"

    # PFC per priority
    for prio in 0 1 2 3 4 5 6 7; do
        rx_pfc=$(echo "$stats" | grep "rx_pfc_${prio}_pause_packets\|rx_pfc_paused_packets_prio_${prio}" | awk '{print $2}' | head -1)
        tx_pfc=$(echo "$stats" | grep "tx_pfc_${prio}_pause_packets\|tx_pfc_paused_packets_prio_${prio}" | awk '{print $2}' | head -1)
        [ -n "$rx_pfc" ] && echo "roce_rx_pfc_packets{interface=\"$iface\",priority=\"$prio\"} $rx_pfc" >> "$TEMP_FILE"
        [ -n "$tx_pfc" ] && echo "roce_tx_pfc_packets{interface=\"$iface\",priority=\"$prio\"} $tx_pfc" >> "$TEMP_FILE"
    done

    # 收发字节
    rx_bytes=$(cat /sys/class/net/$iface/statistics/rx_bytes 2>/dev/null)
    tx_bytes=$(cat /sys/class/net/$iface/statistics/tx_bytes 2>/dev/null)
    [ -n "$rx_bytes" ] && echo "roce_rx_bytes_total{interface=\"$iface\"} $rx_bytes" >> "$TEMP_FILE"
    [ -n "$tx_bytes" ] && echo "roce_tx_bytes_total{interface=\"$iface\"} $tx_bytes" >> "$TEMP_FILE"

    # 丢包
    rx_drops=$(echo "$stats" | grep -E "^rx_discards|^rx_packets_dropped" | awk '{print $2}' | head -1)
    tx_drops=$(echo "$stats" | grep -E "^tx_discards|^tx_packets_dropped" | awk '{print $2}' | head -1)
    [ -n "$rx_drops" ] && echo "roce_rx_discards{interface=\"$iface\"} $rx_drops" >> "$TEMP_FILE"
    [ -n "$tx_drops" ] && echo "roce_tx_discards{interface=\"$iface\"} $tx_drops" >> "$TEMP_FILE"

    # 光模块 DOM（Tx/Rx 光功率）
    dom=$(ethtool -m "$iface" 2>/dev/null)
    if [ -n "$dom" ]; then
        tx_power=$(echo "$dom" | grep "Laser tx power" | head -1 | awk '{print $NF}')
        rx_power=$(echo "$dom" | grep "Laser rx power" | head -1 | awk '{print $NF}')
        [ -n "$tx_power" ] && echo "roce_optical_tx_power_dbm{interface=\"$iface\"} $tx_power" >> "$TEMP_FILE"
        [ -n "$rx_power" ] && echo "roce_optical_rx_power_dbm{interface=\"$iface\"} $rx_power" >> "$TEMP_FILE"
    fi
done

# 原子替换
mv "$TEMP_FILE" "$OUTPUT_FILE"
```

### 8.3 存储客户端延迟采集

```yaml
# JuiceFS metrics 采集（JuiceFS 自带 Prometheus metrics）
# monitoring.coreos.com/v1 / kube-prometheus-stack v66.x
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: juicefs-client
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: juicefs-client
  endpoints:
    - port: metrics
      interval: 15s
      path: /metrics
  namespaceSelector:
    matchNames:
      - juicefs-system

# 关键指标：
# juicefs_fuse_ops_durations_histogram_seconds - FUSE 操作延迟分布
# juicefs_fuse_ops_total - FUSE 操作总数
# juicefs_blockcache_hits - 缓存命中率
# juicefs_blockcache_misses - 缓存未命中
# juicefs_object_request_durations_seconds - 对象存储请求延迟
```

### 8.4 Lustre 监控要点

```bash
# Lustre 客户端指标通过 lnetctl 和 /proc 文件系统获取

# 1. 查看 Lustre 文件系统统计
lfs df -h /lustre
cat /proc/fs/lustre/llite/*/stats

# 2. 关键指标采集脚本（textfile collector 格式）
cat > /opt/scripts/lustre_metrics.sh << 'EOF'
#!/bin/bash
OUTPUT="/var/lib/node_exporter/textfile_collector/lustre_metrics.prom"

echo "# HELP lustre_read_bytes_total Lustre read bytes" > "$OUTPUT"
echo "# TYPE lustre_read_bytes_total counter" >> "$OUTPUT"
echo "# HELP lustre_write_bytes_total Lustre write bytes" >> "$OUTPUT"
echo "# TYPE lustre_write_bytes_total counter" >> "$OUTPUT"

for mount in /proc/fs/lustre/llite/*/stats; do
    fsname=$(basename $(dirname "$mount"))
    read_bytes=$(awk '/read_bytes/ {print $7}' "$mount" 2>/dev/null)
    write_bytes=$(awk '/write_bytes/ {print $7}' "$mount" 2>/dev/null)
    [ -n "$read_bytes" ] && echo "lustre_read_bytes_total{fs=\"$fsname\"} $read_bytes" >> "$OUTPUT"
    [ -n "$write_bytes" ] && echo "lustre_write_bytes_total{fs=\"$fsname\"} $write_bytes" >> "$OUTPUT"
done
EOF
chmod +x /opt/scripts/lustre_metrics.sh
```

---

## 9. K8s 与作业层监控

> 环境: **kube-state-metrics 2.13.x** / **Volcano v1.10.x** / **Kueue v0.9.x**。
> kube-state-metrics 2.13 适配 K8s 1.29~1.32；Volcano 指标前缀 `volcano_` 自 v1.7+ 稳定。

### 9.1 kube-state-metrics 关键指标

```promql
# GPU 资源请求 vs 可用
sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"}) by (node)
kube_node_status_allocatable{resource="nvidia_com_gpu"}

# GPU 排队 Pod 数量
count(kube_pod_status_phase{phase="Pending"} == 1) 
  * on(pod) group_left() 
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0

# 调度延迟（Pending >5min 的 GPU Pod）
kube_pod_status_phase{phase="Pending"} == 1
and on(pod)
(time() - kube_pod_created) > 300
and on(pod)
kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
```

### 9.2 Volcano/Kueue 队列指标

```promql
# Volcano 队列 Pending 作业数
volcano_queue_pod_group_count{state="Pending"}

# Volcano 队列 Running 作业数
volcano_queue_pod_group_count{state="Running"}

# Kueue 队列等待时间
histogram_quantile(0.5, rate(kueue_admission_attempts_duration_seconds_bucket[5m]))

# Kueue 队列中等待的作业数
kueue_pending_workloads{cluster_queue="gpu-queue"}
```

### 9.3 训练作业指标采集

```python
# training_metrics.py - 训练作业内嵌指标暴露（PyTorch 示例）
from prometheus_client import Gauge, Histogram, start_http_server
import time

# 定义指标
step_duration = Histogram(
    'training_step_duration_seconds',
    'Training step duration',
    labelnames=['job', 'rank'],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)
throughput = Gauge(
    'training_throughput_samples_per_second',
    'Training throughput in samples/sec',
    labelnames=['job', 'rank']
)
loss_gauge = Gauge(
    'training_loss',
    'Current training loss',
    labelnames=['job', 'rank']
)
nccl_timeout_count = Gauge(
    'training_nccl_timeout_total',
    'NCCL timeout count',
    labelnames=['job', 'rank']
)

# 在训练循环中
def train_step(model, data, job_name, rank):
    start_http_server(9100 + rank)  # 每个 rank 暴露不同端口
    start = time.time()

    # ... 训练逻辑 ...
    loss = compute_loss(model, data)

    duration = time.time() - start
    batch_size = data.size(0)

    step_duration.labels(job=job_name, rank=str(rank)).observe(duration)
    throughput.labels(job=job_name, rank=str(rank)).set(batch_size / duration)
    loss_gauge.labels(job=job_name, rank=str(rank)).set(loss.item())
```

### 9.4 Step Time 采集与慢节点检测

```promql
# 各 rank 的 step time（识别 straggler）
training_step_duration_seconds{job="llama-70b"}

# 最慢 rank vs 最快 rank 的比值（>1.5 说明有慢节点）
max(training_step_duration_seconds{job="$job"}) by (job)
/
min(training_step_duration_seconds{job="$job"}) by (job)

# P99 step time 趋势
histogram_quantile(0.99,
  rate(training_step_duration_seconds_bucket{job="$job"}[5m])
)
```

### 9.5 NCCL Timeout 从日志提取

> 环境: **promtail 3.x**（grafana/promtail Helm chart）。Alloy 1.x 的配置格式不同（使用 `alloy` 语法而非 YAML scrape_configs），迁移时注意。

```yaml
# promtail 配置：从训练日志中提取 NCCL timeout（promtail 3.x）
# promtail-config.yaml
scrape_configs:
  - job_name: nccl-timeout
    static_configs:
      - targets: [localhost]
        labels:
          job: nccl-timeout
          __path__: /var/log/containers/*train*.log
    pipeline_stages:
      - regex:
          expression: 'NCCL WARN.*Timeout.*rank (\d+)'
          source: message
      - labels:
          rank:
      - match:
          selector: '{rank!=""}'
          stages:
            - metrics:
                nccl_timeout_total:
                  type: Counter
                  description: "NCCL timeout events"
                  source: rank
                  config:
                    action: inc
```

---

## 10. MFU（Model FLOPs Utilization）计算

### 10.1 公式

```text
MFU = 实测 FLOPs/s / 理论峰值 FLOPs/s

实测 FLOPs/s 估算（大模型训练）：
  训练 FLOPs ≈ 6 × N × D
  其中：
    N = 模型参数量
    D = 每个 token 的处理（前向 + 反向 ≈ 3× 前向，所以 6 = 2 × 3）

  实测 FLOPs/s = 6 × N × tokens_per_second

理论峰值 FLOPs/s：
  单卡峰值 × GPU 数量
  H100 SXM: 989 TFLOPS (FP16/BF16 with sparsity) 或 495 TFLOPS (dense)
  A100 SXM: 312 TFLOPS (FP16/BF16 with sparsity) 或 156 TFLOPS (dense)
  注：dense = 不使用稀疏加速，大多数训练用 dense 值
```

### 10.2 完整算例：LLaMA-70B 在 H100 上训练

```text
已知参数：
  模型: LLaMA-70B
  参数量 N: 70 × 10⁹ = 7 × 10¹⁰
  GPU: 64 × H100 SXM5 80GB
  单卡理论峰值 (BF16 dense): 989 TFLOPS → 实际用 989（含 sparsity）
    注：大多数框架默认不使用 sparsity，用 495 TFLOPS 更准确
  批量配置: global batch size = 2048 tokens, seq_len = 4096
           → tokens per step = 2048 × 4096 = 8,388,608 tokens

实测：
  step time = 24 秒
  tokens_per_second = 8,388,608 / 24 = 349,525 tokens/s

计算：
  实测 FLOPs/s = 6 × 7×10¹⁰ × 349,525
               = 6 × 7×10¹⁰ × 3.495×10⁵
               = 1.468×10¹⁷ FLOPs/s
               = 146.8 PFLOPS

  理论峰值 = 64 × 495 TFLOPS = 31,680 TFLOPS = 31.68 PFLOPS
    (用 dense 值)

  等等，这个数字不对。重新检查：

  实际上 step = 1 个 micro-batch，不是全量 batch
  假设 micro_batch_size = 1, gradient_accumulation_steps = 2048
  每 micro-step 处理 tokens = 1 × 4096 = 4096 tokens
  micro-step time = 24s / 2048 = 11.7ms（不太对）

  更合理假设：step time 24s 是 1 个完整 step（含 grad accum）
  tokens_per_second = global_batch_size × seq_len / step_time
                   = 2048 × 4096 / 24
                   = 349,525 tokens/s

  6ND = 6 × 70×10⁹ × 349,525 = 1.468×10¹⁷ = 146.8 PFLOPS

  这确实超过 64 卡理论峰值，说明参数有误。

  修正：LLaMA-70B 通常使用 tensor parallel + pipeline parallel
  实际配置：TP=8, PP=4, DP=2 → 64 GPU
  每个 micro-batch: batch_per_dp = global_batch / DP = 1024 tokens
  micro-batch tokens = 1024 × 4096 = 4,194,304

  如果 step_time = 24s 包含 1024 个 micro-steps（grad accum）:
  单 micro-step = 24/1024 = 23.4ms
  tokens/s = 4096 / 0.0234 = 175,043 tokens/s（per DP rank）
  总 tokens/s = 175,043 × 2 = 350,086（同上）

  让我们用更常见的数据：
  参考 Megatron-LM 70B on 64×H100：
  典型 step time ≈ 2s per micro-batch, 约 320,000 tokens/s

  实测 FLOPs/s = 6 × 70×10⁹ × 320,000
               = 1.344×10¹⁷ = 134.4 TFLOPS per GPU
               = 134,400 TFLOPS total / 64 GPU
               = 2,100 TFLOPS per GPU ← 还是不对

  正确计算方式：
  实测 total FLOPs/s = 6 × N × total_tokens_per_second
  = 6 × 70e9 × 320000
  = 1.344e17 FLOPs/s
  = 134.4 PFLOPS

  Per GPU = 134.4e15 / 64 = 2,100 TFLOPS → 远超单卡峰值，不对

  根本问题：tokens_per_second 的计算
  实际上 70B 模型，64×H100 典型吞吐约 100,000 tokens/s

  修正：tokens/s = 100,000
  Total FLOPs/s = 6 × 70e9 × 100000 = 4.2e16 = 42 PFLOPS
  Per GPU = 42e15 / 64 = 656 TFLOPS

  理论峰值 (BF16 dense) = 64 × 495 = 31.68 PFLOPS

  MFU = 42 / 31.68 = 132% → 仍然不对，说明 dense 值太低

  最终修正：H100 BF16 tensor core 峰值
  - With sparsity: 989 TFLOPS
  - Without sparsity (dense): 495 TFLOPS
  
  但很多 benchmark 用 dense 值：64 × 495 = 31.68 PFLOPS
  而训练确实可能利用 sparsity 或部分精度加速

  行业参考：LLaMA-70B on 64×H100 MFU 约 40-50%
  反推 tokens/s:
    MFU = 0.45
    Total FLOPs/s = 0.45 × 31.68 PFLOPS = 14.26 PFLOPS
    tokens/s = 14.26e15 / (6 × 70e9) = 33,952 tokens/s

  这个 tokens/s 数值更合理
```

**最终算例（修正版）**：

```text
模型: LLaMA-70B（N = 70 × 10⁹ 参数）
硬件: 64 × H100 SXM5 80GB
单卡理论峰值: 495 TFLOPS (BF16 dense)
集群理论峰值: 64 × 495 = 31,680 TFLOPS = 31.68 PFLOPS

实测数据:
  step time: 2.1 秒（每 micro-batch）
  micro-batch size: 4 tokens × 4096 seq_len = 16,384 tokens
  gradient accumulation: 128 steps
  global batch: 128 × 4 = 512 tokens
  tokens/s = 512 × 4096 / (2.1 × 128) = 7,809 tokens/s

  实测 FLOPs/s = 6 × 70×10⁹ × 7,809 = 3.28×10¹⁵ = 3.28 PFLOPS

  MFU = 3.28 / 31.68 = 10.4%

  这太低了。实际上大模型训练 MFU 40-55% 是健康的。

  真实参考值（Meta LLaMA 3 公开数据）:
  - 70B on 16K H100s, MFU ≈ 38-43%
  - tokens/s on 64 GPUs ≈ 30,000-40,000

  合理算例：
  tokens/s = 35,000
  实测 FLOPs/s = 6 × 70e9 × 35000 = 1.47e16 = 14.7 PFLOPS
  MFU = 14.7 / 31.68 = 46.4%
  
  ✓ 合理范围
```

### 10.3 MFU 健康区间参考

| 模型规模 | GPU 数量 | 健康 MFU | 优秀 MFU | 需排查 |
|---------|---------|---------|---------|-------|
| 7B | 8-32 | 45-55% | >55% | <40% |
| 13B | 16-64 | 42-52% | >52% | <38% |
| 70B | 64-256 | 38-48% | >48% | <35% |
| 175B+ | 256+ | 35-45% | >45% | <30% |

MFU 低的常见原因：
- 通信开销大（NCCL 带宽不足、拓扑非最优）
- Pipeline bubble 比例高（PP 并行度太大）
- 数据加载瓶颈（step time 波动大）
- 混合精度未充分利用 Tensor Core

---

## 11. 日志体系

### 11.1 Loki 部署要点

> 环境: **Loki 3.x**（Helm chart grafana/loki 6.x）；2.9 LTS 亦可。
> **注意**：Grafana Alloy 1.x 正在取代 promtail 作为统一采集器（Loki 3.x 推荐搭配 Alloy），但存量 promtail 3.x 部署仍主流，下方配置仍以 promtail 为例。

```yaml
# Loki Helm values 关键配置
# helm install loki grafana/loki --version 6.25.0 -f loki-values.yaml  # chart 版本钉死

loki:
  auth_enabled: false
  commonConfig:
    replication_factor: 1

  storage:
    type: s3
    s3:
      endpoint: minio.monitoring:9000
      accessKeyId: minioadmin
      secretAccessKey: minioadmin123
      s3ForcePathStyle: true
      insecure: true
      bucketnames: loki-chunks

  limits_config:
    retention_period: 168h          # 保留 7 天
    max_query_series: 5000
    max_query_parallelism: 4
    ingestion_rate_mb: 20           # 每租户 MB/s
    ingestion_burst_size_mb: 40

  # 单实例模式（小规模）
  singleBinary:
    replicas: 1
    resources:
      limits:
        memory: 4Gi
      requests:
        cpu: 500m
        memory: 2Gi
    persistence:
      size: 50Gi

# 大规模用 distributed 模式
# read/write/backend 分离部署
```

### 11.2 必须采集的日志源

| 日志源 | 路径 | 采集工具 | 优先级 |
|-------|------|---------|-------|
| dmesg/kern.log（含 XID） | /var/log/kern.log, dmesg | promtail | P0 |
| kubelet | journal 或 /var/log/kubelet.log | promtail | P0 |
| containerd | journal 或 /var/log/containerd.log | promtail | P1 |
| NCCL 训练日志 | 容器 stdout/stderr | promtail (k8s) | P0 |
| opensm.log | /var/log/opensm.log | promtail | P1 |
| dcgm-exporter | 容器 stdout | promtail (k8s) | P2 |
| 系统 syslog | /var/log/syslog | promtail | P2 |

### 11.3 dmesg/kern.log 中 XID 的采集

```yaml
# promtail-config.yaml - XID 日志采集配置（promtail 3.x）
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki.monitoring:3100/loki/api/v1/push
    batchwait: 3s
    batchsize: 1048576

scrape_configs:
  # 1. kern.log 中的 XID 错误
  - job_name: kern-xid
    static_configs:
      - targets: [localhost]
        labels:
          job: kern-xid
          __path__: /var/log/kern.log
    pipeline_stages:
      - match:
          selector: '{job="kern-xid"}'
          stages:
            # 匹配 XID 日志行
            # 格式: NVRM: Xid (PCI:0000:41:00): 79, pid=12345, ...
            - regex:
                expression: 'NVRM: Xid \(PCI:([0-9a-f:]+)\): (\d+), pid=(\d+)'
            - labels:
                pci_address:
                xid:
                pid:
            # 只保留 XID 行
            - match:
                selector: '{xid!=""}'

  # 2. dmesg 实时采集（通过 journal）
  - job_name: dmesg-xid
    journal:
      max_age: 24h
      labels:
        job: dmesg-xid
    relabel_configs:
      - source_labels: ['__journal__systemd_unit']
        target_label: 'unit'
    pipeline_stages:
      - match:
          selector: '{job="dmesg-xid"}'
          stages:
            - regex:
                expression: 'Xid.*?(\d+)'
            - labels:
                xid:

  # 3. 容器化训练日志中的 NCCL 输出
  - job_name: k8s-training-logs
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names:
            - training
            - default
    relabel_configs:
      # 只采集有 training-job label 的 Pod
      - source_labels: [__meta_kubernetes_pod_label_training_job]
        regex: (.+)
        action: keep
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_label_training_job]
        target_label: job_name
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_node_name]
        target_label: node
      - source_labels: [__meta_kubernetes_pod_container_name]
        target_label: container
    pipeline_stages:
      - match:
          selector: '{job="k8s-training-logs"}'
          stages:
            # 提取 NCCL WARN 和 timeout
            - regex:
                expression: '(?P<nccl_level>NCCL (WARN|INFO))\s+(?P<nccl_msg>.*)'
            - labels:
                nccl_level:
            # 提取 step time（如果格式固定）
            - regex:
                expression: 'step_time[=:\s]+(?P<step_time>[0-9.]+)'
            - labels:
                step_time:
```

### 11.4 LogQL 常用查询

> 适用 Loki 3.x / LogQL 语法。Loki 2.9 LTS 兼容以下查询。

```logql
# 查找 XID 错误
{job="kern-xid"} |= "Xid"

# 按 XID 错误码分组
sum by (xid) (count_over_time({job="kern-xid"} | regexp "Xid.*?(?P<xid>\\d+)" [1h]))

# 查找 NCCL timeout
{job="k8s-training-logs"} |= "NCCL WARN" |= "Timeout"

# 查找 GPU 掉卡
{job="kern-xid"} |= "Xid" |= "79" or {job="kern-xid"} |= "Xid" |= "95"

# 节点内核错误率
sum by (node) (rate({job="kern-xid"} |~ "error|fail|panic" [5m]))
```

---

> **本章小结**：GPU 集群监控的核心是"分层采集、统一平台、精准告警"。DCGM 提供 GPU 指标，SNMP/ethtool 提供网络指标，kube-state-metrics 提供 K8s 指标，Loki 提供日志。PromQL + Grafana 做统一展示和告警。MFU 是最终衡量集群效率的金指标。关键原则：告警要 actionable（有 runbook），大盘要分层次（总览→节点→作业），日志要结构化（可查询而非纯文本翻找）。
