---
title: "GPU 集群运维（03）：Kubernetes 平台层"
date: 2026-11-09T09:00:00+08:00
draft: false
description: "GPU 组件栈全景、Device Plugin 深入、MIG 完整实操、GPU 共享方案对比、批量调度器、NUMA 拓扑、训练作业编排、DRA 动态资源分配，以及集群自身运维与故障速查表。"
tags: ["Kubernetes", "GPU", "调度"]
series: "GPU 集群运维"
---

本分册聚焦 GPU 集群中 Kubernetes 平台层的核心组件、调度策略、作业编排与集群自身运维。

## 本册环境基线

| 项目 | 基线版本 | 来源 |
|------|---------|------|
| OS | Ubuntu 22.04.5 LTS / Rocky Linux 9.5 | 00-版本基线 §1 |
| 内核 | 5.15.0-xxx-generic (Ubuntu GA) / 5.14.0-503 (Rocky) | 00-版本基线 §1 |
| Kubernetes | **1.31.x**（支持范围 1.29~1.32） | 00-版本基线 §3 |
| containerd | 1.7.x LTS | 00-版本基线 §3 |
| etcd | 3.5.15+ | 00-版本基线 §3 |
| NVIDIA GPU Operator | Helm chart **v24.9.2** | 00-版本基线 §2.3 |
| k8s-device-plugin | **v0.17.0** | 00-版本基线 §2.3 |
| gpu-feature-discovery | v0.17.0 | 00-版本基线 §2.3 |
| mig-manager | v0.8.0 | 00-版本基线 §2.3 |
| node-feature-discovery | v0.16.x | 00-版本基线 §2.3 |
| HAMi | v2.5.x | 00-版本基线 §2.3 |
| Volcano | **v1.10.x** | 00-版本基线 §3 |
| Kueue | **v0.9.x** | 00-版本基线 §3 |
| Kubeflow Training Operator | **v1.8.x** | 00-版本基线 §3 |
| multus-cni | v4.0.2 | 00-版本基线 §3 |
| sriov-network-operator | v4.7.x | 00-版本基线 §3 |
| nvidia-container-toolkit | v1.17.4 | 00-版本基线 §2.3 |

> 基线快照时间：2026-01。所有命令/YAML/镜像 tag 均以此为准。

---

## 目录

1. [GPU 组件栈全景](#1-gpu-组件栈全景)
2. [Device Plugin 深入](#2-device-plugin-深入)
3. [MIG 完整实操](#3-mig-完整实操)
4. [GPU 共享方案对比](#4-gpu-共享方案对比)
5. [批量调度器](#5-批量调度器)
6. [拓扑与 NUMA](#6-拓扑与-numa)
7. [训练作业编排](#7-训练作业编排)
8. [DRA（Dynamic Resource Allocation）](#8-dra-dynamic-resource-allocation)
9. [集群自身运维](#9-集群自身运维)
10. [故障速查表](#10-故障速查表)

---

## 1. GPU 组件栈全景

### 1.1 组件职责一览

| 组件 | 职责 | 部署方式 | 数据流向 |
|------|------|----------|----------|
| Node Feature Discovery (NFD) | 采集节点硬件特征（PCI 设备、内核模块等）上报为 Node labels | DaemonSet + Deployment | 节点硬件 → Node labels (`feature.node.kubernetes.io/pci-10de.present=true`) |
| GPU Feature Discovery (GFD) | 读取 GPU 属性（型号、显存、MIG 能力）上报为 Node labels | DaemonSet（由 GPU Operator 管理） | nvidia-smi → Node labels (`nvidia.com/gpu.product=A100-SXM4-80GB`) |
| NVIDIA Device Plugin | 实现 kubelet Device Plugin API，将 GPU 注册为可扩展资源 | DaemonSet | kubelet gRPC → 资源 `nvidia.com/gpu` |
| MIG Manager | 管理 A100/H100 的 MIG 分区配置 | DaemonSet（仅 MIG 节点） | ConfigMap → nvidia-smi mig → MIG 设备实例 |
| DCGM Exporter | 采集 GPU 运行时指标（温度/利用率/显存/ECC）暴露 Prometheus metrics | DaemonSet | DCGM API → :9400/metrics |
| GPU Operator | 生命周期管理上述所有组件的 Operator | Deployment + CRD | CR `ClusterPolicy` → 子组件 DaemonSet/ConfigMap |

### 1.2 文字架构图

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         Control Plane                               │
│  ┌──────────┐   ┌──────────────┐   ┌──────────┐   ┌────────────┐  │
│  │apiserver │   │ GPU Operator │   │ Volcano/ │   │ Training   │  │
│  │(ClusterP │◄──│ (reconcile)  │   │ Kueue    │   │ Operator   │  │
│  │ olicy CR)│   └──────────────┘   └──────────┘   │(PyTorchJob)│  │
│  └──────────┘                                      └────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
        │                  │                  │
        ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      GPU Node (Worker)                              │
│  ┌───────────┐  ┌──────────┐  ┌───────────┐  ┌────────────────┐   │
│  │    NFD    │  │   GFD    │  │  MIG Mgr  │  │ DCGM Exporter  │   │
│  │ (worker)  │  │          │  │           │  │   :9400/metrics│   │
│  └─────┬─────┘  └────┬─────┘  └─────┬─────┘  └───────┬────────┘   │
│        │              │              │                │             │
│        ▼              ▼              ▼                │             │
│  ┌─────────────────────────────────────────────┐     │             │
│  │         NVIDIA Device Plugin                │     │             │
│  │  (kubelet gRPC: /var/lib/kubelet/           │     │             │
│  │   device-plugins/kubelet.sock)              │     │             │
│  └──────────────────┬──────────────────────────┘     │             │
│                     │                                │             │
│                     ▼                                ▼             │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              NVIDIA Driver + CUDA Runtime                    │   │
│  │              /dev/nvidia*  /dev/nvidiactl                    │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 GPU Operator 安装（Helm）

```bash
# 添加 NVIDIA helm 仓库
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# 安装 GPU Operator（基线版本 v24.9.2，支持 K8s 1.29~1.32）
helm install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator --create-namespace \
  --set operator.defaultRuntime=containerd \
  --set toolkit.env[0].name=CONTAINERD_CONFIG \
  --set toolkit.env[0].value=/etc/containerd/config.toml \
  --set toolkit.env[1].name=CONTAINERD_SOCKET \
  --set toolkit.env[1].value=/run/containerd/containerd.sock \
  --version v24.9.2

# 验证所有组件就绪
kubectl -n gpu-operator get pods -l app.kubernetes.io/managed-by=gpu-operator
```

---

## 2. Device Plugin 深入

### 2.1 工作原理

kubelet Device Plugin API 基于 gRPC，Device Plugin 作为 server 注册到 kubelet：

```yaml
注册流程：
1. Device Plugin 启动，监听 /var/lib/kubelet/device-plugins/nvidia.sock
2. 向 kubelet 的 /var/lib/kubelet/device-plugins/kubelet.sock 发送 RegisterRequest
   - version: v1beta1
   - endpoint: nvidia.sock
   - resourceName: nvidia.com/gpu
3. kubelet 调用 ListAndWatch 获取设备列表及健康状态
4. Pod 调度到节点后，kubelet 调用 Allocate 请求分配设备
```

**Allocate 流程详解：**

```text
Pod 请求 nvidia.com/gpu: 1
        │
        ▼
kubelet 调用 DevicePlugin.Allocate()
        │
        ▼
Device Plugin 从可用设备池选取 GPU（如 GPU-abc123）
        │
        ▼
构造 ContainerAllocateResponse:
  - envs: NVIDIA_VISIBLE_DEVICES=GPU-abc123
  - devices: /dev/nvidia0, /dev/nvidiactl, /dev/nvidia-uvm
  - mounts: nvidia 驱动库目录
        │
        ▼
kubelet 将 env/devices/mounts 注入容器配置
        │
        ▼
容器启动，CUDA runtime 通过 NVIDIA_VISIBLE_DEVICES 识别可用 GPU
```

### 2.2 独立安装 Device Plugin（不用 GPU Operator 时）

```bash
# 使用官方 DaemonSet（基线版本 v0.17.0，匹配 GPU Operator v24.9.2 内含版本）
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml

# 验证
kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, gpu: .status.allocatable["nvidia.com/gpu"]}'
```

### 2.3 Time-Slicing 配置

Time-slicing 允许一个物理 GPU 以时间片方式共享给多个 Pod。每个 Pod 看到的是同一个 GPU，但 CUDA context 独立。

**ConfigMap：**

```yaml
# 适用: K8s 1.29~1.32 / GPU Operator v24.9.2 / k8s-device-plugin v0.17.0
# 此 ConfigMap 被 device-plugin 读取，格式为 device-plugin 自定义 schema（非 K8s API）
apiVersion: v1
kind: ConfigMap
metadata:
  name: time-slicing-config
  namespace: gpu-operator
data:
  A100-SXM4-80GB: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
        - name: nvidia.com/mig-1g.10gb
          replicas: 2
  H100-SXM5-80GB: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 8
  default: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
```

**应用 time-slicing 到 Device Plugin：**

```bash
# 通过 GPU Operator 的 ClusterPolicy 启用
kubectl patch clusterpolicy/gpu-cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec":{"devicePlugin":{"config":{"name":"time-slicing-config","default":"default"}}}}'

# 或独立部署时，直接 patch DaemonSet
kubectl patch daemonset nvidia-device-plugin-daemonset \
  -n kube-system --type merge \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"nvidia-device-plugin","env":[{"name":"CONFIG_OVERRIDE","value":"time-slicing-config"}]}]}}}}'
```

### 2.4 envvar 设备分配方式

Device Plugin 默认通过环境变量 `NVIDIA_VISIBLE_DEVICES` 控制容器可见的 GPU。支持四种值：

| 值 | 含义 | 示例 |
|----|------|------|
| GPU UUID | 指定具体 GPU | `NVIDIA_VISIBLE_DEVICES=GPU-abc123def` |
| GPU Index | 按索引指定 | `NVIDIA_VISIBLE_DEVICES=0,1` |
| `all` | 暴露所有 GPU | `NVIDIA_VISIBLE_DEVICES=all` |
| `void` / 空 | 不暴露任何 GPU | `NVIDIA_VISIBLE_DEVICES=void` |

### 2.5 常见故障排查

#### 故障 1：GPU 计数不对（allocatable 显示 0 或数量不正确）

```bash
# 检查 device plugin Pod 日志
kubectl logs -n gpu-operator -l app=nvidia-device-plugin-daemonset --tail=100

# 检查 kubelet 是否识别到 device plugin socket
ls -la /var/lib/kubelet/device-plugins/ | grep nvidia

# 检查节点标签
kubectl get node gpu-node-01 --show-labels | grep -i nvidia

# 检查 nvidia-smi 是否正常
ssh gpu-node-01 'nvidia-smi --query-gpu=index,name,memory.total --format=csv'

# 重启 device plugin DaemonSet
kubectl rollout restart daemonset/nvidia-device-plugin-daemonset -n gpu-operator

# 检查 kubelet 日志中的 device plugin 注册信息
ssh gpu-node-01 'journalctl -u kubelet | grep -i "device plugin" | tail -20'
```

#### 故障 2：容器内看不到 GPU（CUDA unknown error）

```bash
# 确认容器内环境变量
kubectl exec -it my-pod -- env | grep NVIDIA

# 检查 /dev 下设备文件
kubectl exec -it my-pod -- ls -la /dev/nvidia*

# 检查 nvidia 驱动库是否正确挂载
kubectl exec -it my-pod -- ls -la /usr/lib/x86_64-linux-gnu/libnvidia*

# 确认 containerd runtime 配置包含 nvidia
ssh gpu-node-01 'cat /etc/containerd/config.toml | grep -A5 nvidia'

# 检查 nvidia-container-toolkit 版本
ssh gpu-node-01 'nvidia-container-cli info'

# 查看容器 runtime 日志
ssh gpu-node-01 'crictl logs <container-id> 2>&1 | tail -30'
```

#### 故障 3：GPU 被标记为 Unhealthy

```bash
# 查看 device plugin 的健康检查日志
kubectl logs -n gpu-operator -l app=nvidia-device-plugin-daemonset | grep -i health

# 检查 XID 错误（Xid 48/79/119 通常表示硬件故障）
ssh gpu-node-01 'dmesg | grep -i "xid\|nvidia" | tail -20'

# 重置 GPU（非破坏性）
ssh gpu-node-01 'nvidia-smi -r -i 0'

# 隔离故障 GPU 节点
kubectl cordon gpu-node-01
kubectl drain gpu-node-01 --ignore-daemonsets --delete-emptydir-data
```

---

## 3. MIG 完整实操

### 3.1 MIG 原理

Multi-Instance GPU (MIG) 将单个 A100/H100 物理 GPU 硬件分区为最多 7 个独立实例，每个实例有独立的：

- **GI (GPU Instance)**：计算单元分区，包含独立的 SM（Streaming Multiprocessor）集合
- **CI (Compute Instance)**：GI 内的计算子分区（H100 支持一个 GI 下多个 CI）

```text
A100-80GB 物理 GPU
├── GI-0 (Profile: 1g.10gb) → CI-0 → /dev/nvidia-cap1
├── GI-1 (Profile: 1g.10gb) → CI-0 → /dev/nvidia-cap2
├── GI-2 (Profile: 2g.20gb) → CI-0 → /dev/nvidia-cap3
└── GI-3 (Profile: 4g.40gb) → CI-0 → /dev/nvidia-cap4
```

### 3.2 nvidia-smi mig 命令全集

```bash
# === 查看可用 MIG Profile ===
nvidia-smi mig -lpm
# 输出示例：
# Profile 0: 1g.10gb  (1 SM, 10GB)
# Profile 1: 1g.10gb+me (1 SM, 10GB + media engine)
# Profile 2: 1g.20gb  (1 SM, 20GB)
# Profile 3: 2g.20gb  (2 SM, 20GB)
# Profile 4: 3g.40gb  (3 SM, 40GB)
# Profile 5: 4g.40gb  (4 SM, 40GB)
# Profile 6: 7g.80gb  (7 SM, 80GB)

# === 查看 MIG 状态 ===
nvidia-smi mig -lgi          # 列出所有 GPU Instance
nvidia-smi mig -lci          # 列出所有 Compute Instance

# === 创建 GPU Instance (GI) ===
# 在 GPU 0 上创建一个 4g.40gb 的 GI
nvidia-smi mig -i 0 -cgi 5
# -i 0     : 指定物理 GPU 索引
# -cgi 5   : 创建 profile ID 为 5 的 GI（即 4g.40gb）
# 输出: Successfully created GPU Instance 0 on GPU 0 (profile ID 5)

# 在 GPU 0 上创建一个 2g.20gb 的 GI
nvidia-smi mig -i 0 -cgi 3

# 在 GPU 0 上创建两个 1g.10gb 的 GI
nvidia-smi mig -i 0 -cgi 1,1

# === 创建 Compute Instance (CI) ===
# 在 GPU 0 的 GI 0 上创建默认 CI（覆盖全部 SM）
nvidia-smi mig -i 0 -gi 0 -cci
# -cci     : 创建 Compute Instance（默认 profile 0，覆盖 GI 的全部计算单元）

# === 销毁实例 ===
# 销毁 GPU 0 上的 GI 2（同时销毁其下的 CI）
nvidia-smi mig -i 0 -dgi -gi 2
# -dgi     : destroy GPU Instance

# 销毁 GI 0 下的 CI 0
nvidia-smi mig -i 0 -gi 0 -dci -ci 0

# === 重置 MIG 配置 ===
# 清除 GPU 0 上的所有 MIG 分区
nvidia-smi mig -i 0 -cgi ''

# === 完整示例：将 A100-80GB 配置为 1x4g + 1x2g + 2x1g ===
nvidia-smi mig -i 0 -cgi 5          # 1x 4g.40gb
nvidia-smi mig -i 0 -cgi 3          # 1x 2g.20gb
nvidia-smi mig -i 0 -cgi 1,1        # 2x 1g.10gb
nvidia-smi mig -i 0 -cci            # 为所有 GI 创建 CI
```

### 3.3 A100-80GB / H100 可用 Profile 表

| Profile ID | Profile 名称 | SM 数量 | 显存 | A100-80GB 最大实例数 | H100-SXM5-80GB 最大实例数 |
|------------|-------------|---------|------|---------------------|--------------------------|
| 0 | 1g.10gb | 14 (A100) / 15 (H100) | 10 GB | 7 | 7 |
| 1 | 1g.10gb+me | 14/15 + 媒体引擎 | 10 GB | 1（含媒体引擎） | 1 |
| 2 | 1g.20gb | 14/15 | 20 GB | - (仅 H100) | 4 |
| 3 | 2g.20gb | 28/30 | 20 GB | 3 | 3 |
| 4 | 3g.40gb | 42/45 | 40 GB | 2 | 2 |
| 5 | 4g.40gb | 56/60 | 40 GB | 1 | 1 |
| 6 | 7g.80gb | 98 (A100) / 132 (H100) | 80 GB | 1 | 1 |

> 注意：H100 额外支持 1g.20gb profile（Profile ID 2），这是 A100 不具备的。

### 3.4 mig-parted ConfigMap 完整 YAML

**Single 策略（全部 7 个 1g.10gb）：**

```yaml
# 适用: K8s 1.29~1.32 / GPU Operator v24.9.2 / mig-manager v0.8.0
# mig-parted config.yaml 为 NVIDIA 自定义 schema（非 K8s API），由 mig-manager 解析
apiVersion: v1
kind: ConfigMap
metadata:
  name: mig-parted-config
  namespace: gpu-operator
data:
  config.yaml: |
    version: v1
    mig-configs:
      all-disabled:
        - devices: all
          mig-enabled: false
      all-1g.10gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "1g.10gb": 7
      all-2g.20gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "2g.20gb": 3
      all-4g.40gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "4g.40gb": 1
      all-7g.80gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "7g.80gb": 1
```

**Mixed 策略（4g + 2g + 1g 混合）：**

```yaml
# 适用: K8s 1.29~1.32 / GPU Operator v24.9.2 / mig-manager v0.8.0
# 注意: 1g.20gb profile 仅 H100 支持（A100 无此 profile）
apiVersion: v1
kind: ConfigMap
metadata:
  name: mig-parted-mixed-config
  namespace: gpu-operator
data:
  config.yaml: |
    version: v1
    mig-configs:
      mixed-a100-80gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "1g.10gb": 2
            "2g.20gb": 1
            "4g.40gb": 1
      mixed-h100-80gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "1g.10gb": 2
            "1g.20gb": 1
            "2g.20gb": 1
            "3g.40gb": 1
      selective-gpu0-4g:
        - devices: [0]
          mig-enabled: true
          mig-devices:
            "4g.40gb": 1
        - devices: [1, 2, 3, 4, 5, 6, 7]
          mig-enabled: false
```

**应用 MIG 配置：**

```bash
# 通过节点标签触发 mig-parted 应用配置
kubectl label node gpu-node-01 \
  nvidia.com/mig.config=all-1g.10gb \
  --overwrite

# 验证 MIG 状态
ssh gpu-node-01 'nvidia-smi mig -lgi'
ssh gpu-node-01 'nvidia-smi mig -lci'

# 验证节点资源
kubectl get node gpu-node-01 -o jsonpath='{.status.allocatable}' | jq .
# 应看到 nvidia.com/mig-1g.10gb: 7
```

### 3.5 MIG 设备在 K8s 的资源名

| MIG Profile | K8s 资源名 | 示例请求 |
|-------------|-----------|---------|
| 1g.10gb | `nvidia.com/mig-1g.10gb` | `resources: {limits: {"nvidia.com/mig-1g.10gb": 1}}` |
| 1g.20gb | `nvidia.com/mig-1g.20gb` | `resources: {limits: {"nvidia.com/mig-1g.20gb": 1}}` |
| 2g.20gb | `nvidia.com/mig-2g.20gb` | `resources: {limits: {"nvidia.com/mig-2g.20gb": 1}}` |
| 3g.40gb | `nvidia.com/mig-3g.40gb` | `resources: {limits: {"nvidia.com/mig-3g.40gb": 1}}` |
| 4g.40gb | `nvidia.com/mig-4g.40gb` | `resources: {limits: {"nvidia.com/mig-4g.40gb": 1}}` |
| 7g.80gb | `nvidia.com/mig-7g.80gb` | `resources: {limits: {"nvidia.com/mig-7g.80gb": 1}}` |

### 3.6 MIG Pod 示例 YAML

```yaml
# 适用: K8s 1.29~1.32 / GPU Operator v24.9.2 / apiVersion: v1
# 镜像 tag: Triton 24.05-py3（NGC，示例）；最新推荐 nvcr.io/nvidia/tritonserver:24.12-py3（Triton 3.53.0）
apiVersion: v1
kind: Pod
metadata:
  name: mig-inference-pod
  namespace: ml-serving
  labels:
    app: inference
spec:
  containers:
  - name: inference
    image: nvcr.io/nvidia/tritonserver:24.12-py3   # Triton Inference Server 3.53.0（NGC 基线）
    command: ["tritonserver", "--model-repository=/models"]
    resources:
      limits:
        nvidia.com/mig-1g.10gb: 1
        memory: 8Gi
      requests:
        cpu: "2"
        memory: 8Gi
        nvidia.com/mig-1g.10gb: 1
    volumeMounts:
    - name: model-store
      mountPath: /models
    ports:
    - containerPort: 8000
      name: http
    - containerPort: 8001
      name: grpc
  volumes:
  - name: model-store
    persistentVolumeClaim:
      claimName: triton-models-pvc
  nodeSelector:
    nvidia.com/gpu.product: A100-SXM4-80GB-MIG-1g.10gb
```

---

## 4. GPU 共享方案对比

### 4.1 方案对比表

| 方案 | 隔离级别 | 显存隔离 | 算力隔离 | 适用场景 | 限制 |
|------|---------|---------|---------|---------|------|
| 整卡独占 | 完全 | 完全 | 完全 | 大规模训练 | GPU 利用率低 |
| Time-Slicing | 无（共享 CUDA context） | 无 | 无（时间片轮转） | 推理、小模型实验 | Pod 间互相影响性能 |
| MPS (Multi-Process Service) | 进程级 | 无 | 部分（并发控制） | 推理并发场景 | 不支持 MIG；同进程组内无隔离 |
| MIG | 硬件级 | 完全（独立显存分区） | 完全（独立 SM 集合） | 推理 + 中小训练 | 仅 A100/H100 支持；分区固定 |
| HAMi | 调度级 | 支持（显存配额） | 部分（vcore 配额） | 推理集群多租户 | 需修改调度器；社区项目 |
| vGPU (NVIDIA vGPU) | 硬件级 | 完全 | 完全 | 云/VDI 多租户 | 需 vGPU license；需 vGPU driver |

### 4.2 HAMi 部署要点

```bash
# 安装 HAMi（基线版本 v2.5.x，示例用 v2.5.0；以官方 release 为准）
helm repo add hami https://project-hami.github.io/HAMi/
helm repo update

helm install hami hami/hami \
  --namespace hami-system --create-namespace \
  --version 2.5.0 \
  --set scheduler.kubeScheduler.image=registry.k8s.io/kube-scheduler:v1.31.0 \
  --set devices.nvidia.enabled=true \
  --set devices.nvidia.nodeLockExpire=3600

# 关键参数 DeviceMemoryScaling
# 通过 NodeAnnotation 设置显存超分比（默认 1.0 即不超分）
kubectl annotate node gpu-node-01 \
  hami.io/nvidia-gpu-memory-scaling=1.5
# 80GB 卡将呈现 120GB 可分配显存

# 通过 NodeAnnotation 设置 vcore 超分比
kubectl annotate node gpu-node-01 \
  hami.io/nvidia-gpu-core-scaling=2.0
# 100% 算力将呈现 200% 可分配

# 验证 HAMi 设备注册
kubectl get node gpu-node-01 -o jsonpath='{.status.allocatable}' | jq '."hami.io/gpu"'
```

**HAMi Pod 资源请求示例：**

```yaml
# 适用: K8s 1.29~1.32 / HAMi v2.5.x / apiVersion: v1（核心 Pod）
apiVersion: v1
kind: Pod
metadata:
  name: hami-shared-pod
spec:
  containers:
  - name: worker
    image: nvidia/cuda:12.4.0-devel-ubuntu22.04   # CUDA 12.4 兼容 R550+ 驱动；NGC 镜像亦可
    resources:
      limits:
        hami.io/gpu: 1          # 请求 1 个 vGPU
        hami.io/gpumem: 8192    # 请求 8GB 显存
        hami.io/gpucores: 30    # 请求 30% 算力
```

---

## 5. 批量调度器

### 5.1 Volcano

#### 5.1.1 Helm 安装

```bash
helm repo add volcano-sh https://volcano-sh.github.io/charts
helm repo update

# 安装 Volcano（基线版本 v1.10.x，示例 v1.10.0；以官方 release 为准）
helm install volcano volcano-sh/volcano \
  --namespace volcano-system --create-namespace \
  --version 1.10.0 \
  --set basic.scheduler_enable=true \
  --set basic.image_pull_policy=IfNotPresent

# 验证
kubectl -n volcano-system get pods
# 应看到 volcano-scheduler, volcano-admission, volcano-controllers
```

#### 5.1.2 PyTorchJob + Gang 调度完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Volcano v1.10.x / apiVersion: scheduling.volcano.sh/v1beta1（Volcano v1.4+ 稳定）
# 版本敏感: scheduling.volcano.sh/v1beta1 自 Volcano v1.0+ 沿用，尚无 v1 GA
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: training-queue
spec:
  weight: 10
  reclaimable: true
  capability:
    nvidia.com/gpu: 32
---
# 适用: K8s 1.29~1.32 / Kubeflow Training Operator v1.8.x / apiVersion: kubeflow.org/v1
# PyTorchJob v1 自 Training Operator v1.4+ 稳定；v1beta1 已在 v1.7 移除
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: llm-finetune-job
  namespace: ml-training
  annotations:
    scheduling.volcano.sh/queue-name: training-queue
  labels:
    volcano.sh/queue-name: training-queue
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: OnFailure
      template:
        metadata:
          annotations:
            scheduling.volcano.sh/queue-name: training-queue
            volcano.sh/task-spec: master
          labels:
            volcano.sh/queue-name: training-queue
        spec:
          schedulerName: volcano
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 示例；生产推荐 NGC: nvcr.io/nvidia/pytorch:24.12-py3（含 CUDA 12.6 + PyTorch 2.5 + NCCL 2.23）
            command:
            - torchrun
            - --nproc_per_node=8
            - --nnodes=$(WORLD_SIZE)
            - --node_rank=$(RANK)
            - --master_addr=$(MASTER_ADDR)
            - --master_port=29500
            - train.py
            - --model=llama-7b
            - --data=/data/train
            resources:
              limits:
                nvidia.com/gpu: 8
                memory: 256Gi
              requests:
                cpu: "32"
                memory: 256Gi
                nvidia.com/gpu: 8
            volumeMounts:
            - name: shm
              mountPath: /dev/shm
            - name: dataset
              mountPath: /data
          volumes:
          - name: shm
            emptyDir:
              medium: Memory
              sizeLimit: 64Gi
          - name: dataset
            persistentVolumeClaim:
              claimName: training-data-pvc
    Worker:
      replicas: 3
      restartPolicy: OnFailure
      template:
        metadata:
          annotations:
            scheduling.volcano.sh/queue-name: training-queue
            volcano.sh/task-spec: worker
          labels:
            volcano.sh/queue-name: training-queue
        spec:
          schedulerName: volcano
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 同上，生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
            command:
            - torchrun
            - --nproc_per_node=8
            - --nnodes=$(WORLD_SIZE)
            - --node_rank=$(RANK)
            - --master_addr=$(MASTER_ADDR)
            - --master_port=29500
            - train.py
            resources:
              limits:
                nvidia.com/gpu: 8
                memory: 256Gi
              requests:
                cpu: "32"
                memory: 256Gi
                nvidia.com/gpu: 8
            volumeMounts:
            - name: shm
              mountPath: /dev/shm
            - name: dataset
              mountPath: /data
          volumes:
          - name: shm
            emptyDir:
              medium: Memory
              sizeLimit: 64Gi
          - name: dataset
            persistentVolumeClaim:
              claimName: training-data-pvc
```

#### 5.1.3 Capacity Queue 完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Volcano v1.10.x / apiVersion: scheduling.volcano.sh/v1beta1
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: research-queue
spec:
  weight: 5
  reclaimable: true
  guarantee:
    resource:
      nvidia.com/gpu: 8
      cpu: "64"
      memory: 512Gi
  capability:
    nvidia.com/gpu: 24
    cpu: "192"
    memory: 1536Gi
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: production-queue
spec:
  weight: 15
  reclaimable: true
  guarantee:
    resource:
      nvidia.com/gpu: 16
      cpu: "128"
      memory: 1024Gi
  capability:
    nvidia.com/gpu: 32
    cpu: "256"
    memory: 2048Gi
```

#### 5.1.4 常用 vcctl 命令

```bash
# 查看队列状态
vcctl queue list
vcctl queue get -n default -q training-queue

# 查看作业状态
vcctl job list -n ml-training
vcctl job view -n ml-training -j llm-finetune-job

# 暂停/恢复作业
vcctl job suspend -n ml-training -j llm-finetune-job
vcctl job resume -n ml-training -j llm-finetune-job

# 查看节点资源
vcctl pod list -n ml-training
```

### 5.2 Kueue

#### 5.2.1 安装

```bash
# 安装 Kueue（基线版本 v0.9.x，示例 v0.9.1；以官方 release 为准）
kubectl apply --server-side -f \
  https://github.com/kubernetes-sigs/kueue/releases/download/v0.9.1/manifests.yaml

# 验证
kubectl -n kueue-system get pods
```

#### 5.2.2 ResourceFlavor / ClusterQueue / LocalQueue 完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Kueue v0.9.x / apiVersion: kueue.x-k8s.io/v1beta1
# 版本敏感: kueue.x-k8s.io/v1beta1 自 Kueue v0.5+ 稳定；v1alpha1 已移除
# ResourceFlavor - 定义 GPU 资源类型
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: gpu-a100
spec:
  nodeLabels:
    nvidia.com/gpu.product: A100-SXM4-80GB
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: gpu-h100
spec:
  nodeLabels:
    nvidia.com/gpu.product: H100-SXM5-80GB
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
---
# ClusterQueue - 集群级队列（支持 cohort 借还）
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: research-cluster-queue
spec:
  cohort: gpu-pool                # 同一 cohort 内的队列可互相借用
  namespaceSelector:
    matchLabels:
      team: research
  queueingStrategy: BestEffortFIFO
  resourceGroups:
  - coveredResources:
    - nvidia.com/gpu
    - cpu
    - memory
    flavors:
    - name: gpu-a100
      resources:
      - name: nvidia.com/gpu
        nominalQuota: 16
        borrowingLimit: 8         # 最多可从其他队列借用 8 张
        lendingLimit: 4           # 最多借出 4 张给其他队列
      - name: cpu
        nominalQuota: "128"
        borrowingLimit: "64"
        lendingLimit: "32"
      - name: memory
        nominalQuota: 1024Gi
        borrowingLimit: 512Gi
        lendingLimit: 256Gi
    - name: gpu-h100
      resources:
      - name: nvidia.com/gpu
        nominalQuota: 8
        borrowingLimit: 4
        lendingLimit: 2
      - name: cpu
        nominalQuota: "64"
        borrowingLimit: "32"
        lendingLimit: "16"
      - name: memory
        nominalQuota: 512Gi
        borrowingLimit: 256Gi
        lendingLimit: 128Gi
---
# LocalQueue - 命名空间级队列
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: research-local-queue
  namespace: ml-research
spec:
  clusterQueue: research-cluster-queue
```

#### 5.2.3 JobSet / PyTorchJob 挂队列

```yaml
# 适用: K8s 1.29~1.32 / Kueue v0.9.x + Training Operator v1.8.x
# apiVersion: kubeflow.org/v1（Training Operator v1.4+ 稳定）
# 挂 Kueue 队列：通过 label kueue.x-k8s.io/queue-name 引用 LocalQueue（Kueue v0.5+ 格式）
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: llm-pretrain
  namespace: ml-research
  labels:
    kueue.x-k8s.io/queue-name: research-local-queue
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: OnFailure
      template:
        spec:
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
            command: ["torchrun", "--nproc_per_node=8", "train.py"]
            resources:
              limits:
                nvidia.com/gpu: 8
              requests:
                cpu: "32"
                memory: 256Gi
                nvidia.com/gpu: 8
    Worker:
      replicas: 7
      restartPolicy: OnFailure
      template:
        spec:
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 同上，生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
            command: ["torchrun", "--nproc_per_node=8", "train.py"]
            resources:
              limits:
                nvidia.com/gpu: 8
              requests:
                cpu: "32"
                memory: 256Gi
                nvidia.com/gpu: 8
```

#### 5.2.4 kueuectl 命令

```bash
# 查看 ClusterQueue 状态
kueuectl get clusterqueue
kueuectl describe clusterqueue research-cluster-queue

# 查看 LocalQueue 状态
kueuectl get localqueue -n ml-research

# 查看 Workload
kueuectl get workload -n ml-research
kueuectl describe workload <workload-name> -n ml-research

# 暂停/恢复队列
kueuectl stop clusterqueue research-cluster-queue
kueuectl start clusterqueue research-cluster-queue

# 查看队列利用率
kubectl get clusterqueue research-cluster-queue -o jsonpath='{.status}'  | jq .
```

### 5.3 Gang Scheduling 为什么必需

**死锁场景图解：**

```yaml
假设集群有 4 个 GPU 节点，每节点 8 卡 = 32 张 GPU

作业 A：需要 4 节点 x 8 卡 = 32 卡
作业 B：需要 4 节点 x 8 卡 = 32 卡

无 Gang Scheduling 时：
  t0: 调度器将作业 A 的 2 个 worker 调度到节点 1、2（占 16 卡）
  t1: 调度器将作业 B 的 2 个 worker 调度到节点 3、4（占 16 卡）
  t2: 作业 A 剩余 2 个 worker 无法调度（节点 3、4 已被 B 占）
  t3: 作业 B 剩余 2 个 worker 无法调度（节点 1、2 已被 A 占）
  → 死锁！两个作业都跑不起来，资源白白浪费

有 Gang Scheduling 时：
  t0: 调度器检查作业 A 需要 4 节点，当前满足 → 一次性调度全部 4 个 worker
  t1: 作业 B 等待，直到作业 A 完成释放资源
  → 无死锁
```

#### binpack vs spread 配置

**Volcano binpack plugin 配置（优先填满节点，提高利用率）：**

```yaml
# 适用: K8s 1.29~1.32 / Volcano v1.10.x
# volcano-scheduler.conf 格式为 Volcano 内部 schema，非 K8s API
apiVersion: v1
kind: ConfigMap
metadata:
  name: volcano-scheduler-config
  namespace: volcano-system
data:
  volcano-scheduler.conf: |
    actions: "enqueue, allocate, backfill"
    tiers:
    - plugins:
      - name: gang
        enablePreemptable: true
      - name: priority
      - name: task-topology
    - plugins:
      - name: binpack
        arguments:
          binpack.weight: 10
          binpack.cpu: 1
          binpack.memory: 1
          binpack.resources: nvidia.com/gpu
          binpack.resources.nvidia.com/gpu: 10
      - name: proportion
      - name: nodeorder
```

**spread 配置（分散到不同节点，提高容错性）：**

```yaml
# 适用: K8s 1.29~1.32 / Volcano v1.10.x（spread plugin 自 Volcano v1.6+ 稳定）
    - plugins:
      - name: spread
        arguments:
          spread.weight: 10
          spread.node: true
      - name: proportion
      - name: nodeorder
```

---

## 6. 拓扑与 NUMA

### 6.1 Kubelet 完整配置

```yaml
# 适用: K8s 1.29~1.32 / apiVersion: kubelet.config.k8s.io/v1beta1
# /var/lib/kubelet/config.yaml（KubeletConfiguration）
# 版本敏感:
#   - TopologyManager: GA since K8s 1.28（single-numa-node policy 自 1.28 GA）
#   - CPUManager static policy: GA since K8s 1.26
#   - MemoryManager: GA since K8s 1.28
#   - full-pcpus-only option: Beta since K8s 1.25，需开启 CPUManagerPolicyOptions feature gate（1.28+ GA）
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
# TopologyManager - 确保 CPU/内存/设备在同一 NUMA 节点
topologyManagerPolicy: single-numa-node   # restricted | single-numa-node | best-effort
topologyManagerScope: pod                  # container | pod

# CPUManager - 绑核（Guaranteed QoS Pod 独占 CPU）
cpuManagerPolicy: static                   # none | static
cpuManagerPolicyOptions:
  full-pcpus-only: "true"                  # 只分配完整物理核（含超线程对）
cpuManagerReconcilePeriod: 10s

# MemoryManager - 保证内存 NUMA 亲和
memoryManagerPolicy: Static                # None | Static
reservedSystemMemory: 4Gi
reservedSystemCPUs: "0-3"                  # 为系统保留 CPU 0-3

# 其他关键配置
kubeReserved:
  cpu: "2"
  memory: 4Gi
  ephemeral-storage: 20Gi
systemReserved:
  cpu: "2"
  memory: 4Gi
evictionHard:
  memory.available: 2Gi
  nodefs.available: 10%
  imagefs.available: 15%
maxPods: 250
```

### 6.2 验证方法

```bash
# 查看节点 NUMA 拓扑
kubectl get node gpu-node-01 -o jsonpath='{.status.allocatable}' | jq .

# 安装 numactl 查看 NUMA 布局
ssh gpu-node-01 'numactl -H'
# 期望输出：
# available: 2 nodes (0-1)
# node 0 cpus: 0 1 2 3 ... 31
# node 0 size: 256 GB
# node 1 cpus: 32 33 ... 63
# node 1 size: 256 GB

# 查看 GPU 与 NUMA 节点的对应关系
ssh gpu-node-01 'nvidia-smi topo -m'
# GPU0-GPU3 在 NUMA 0，GPU4-GPU7 在 NUMA 1

# 验证 Pod 的 CPU 绑定
kubectl exec -it my-pod -- cat /proc/self/status | grep Cpus_allowed_list
# 期望看到同一 NUMA 节点的 CPU 范围，如 0-15

# 查看 TopologyManager 分配的 hint
ssh gpu-node-01 'cat /var/lib/kubelet/topology_manager_state' | jq .
```

### 6.3 Multus + SR-IOV 给训练 Pod 挂 IB 网卡

#### SriovNetworkNodePolicy

```yaml
# 适用: K8s 1.29~1.32 / sriov-network-operator v4.7.x / multus-cni v4.0.2
# apiVersion: sriovnetwork.openshift.io/v1（SR-IOV Network Operator CRD，v4.x 系列稳定）
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetworkNodePolicy
metadata:
  name: mlnx-connectx6-policy
  namespace: sriov-network-operator
spec:
  resourceName: mlnx_sriov_ib
  nodeSelector:
    feature.node.kubernetes.io/pci-15b3.present: "true"  # Mellanox PCI vendor
  priority: 99
  numVfs: 8
  nicSelector:
    vendor: "15b3"           # Mellanox
    deviceID: "101b"         # ConnectX-6 Dx
    pfNames:
    - ens1f0                 # 物理函数名
  deviceType: netdevice      # netdevice 模式（内核驱动）
  isRdma: true               # 启用 RDMA
  linkType: IB               # InfiniBand
```

#### NetworkAttachmentDefinition

```yaml
# 适用: K8s 1.29~1.32 / multus-cni v4.0.2 / apiVersion: k8s.cni.cncf.io/v1（Multus v3.x+ 标准）
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: ib-network
  namespace: ml-training
  annotations:
    k8s.v1.cni.cncf.io/resourceName: openshift.io/mlnx_sriov_ib
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "name": "ib-network",
      "type": "ib-sriov",
      "ipam": {
        "type": "whereabouts",
        "range": "192.168.100.0/24",
        "range_start": "192.168.100.10",
        "range_end": "192.168.100.200"
      },
      "rdmaIsolation": true
    }
```

#### Pod annotations 完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / multus-cni v4.0.2 + SR-IOV Network Operator v4.7.x / apiVersion: v1
apiVersion: v1
kind: Pod
metadata:
  name: training-with-ib
  namespace: ml-training
  annotations:
    k8s.v1.cni.cncf.io/networks: |
      [
        {
          "name": "ib-network",
          "namespace": "ml-training",
          "interface": "ib0",
          "mac": "00:11:22:33:44:55"
        }
      ]
    k8s.v1.cni.cncf.io/networks-status-check: "true"
spec:
  containers:
  - name: training
    image: my-registry/training:v2.1   # 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
    command: ["torchrun", "--nproc_per_node=8", "train.py"]
    resources:
      limits:
        nvidia.com/gpu: 8
        openshift.io/mlnx_sriov_ib: 1
        memory: 256Gi
        hugepages-2Mi: 32Gi
      requests:
        cpu: "32"
        memory: 256Gi
        nvidia.com/gpu: 8
        openshift.io/mlnx_sriov_ib: 1
    securityContext:
      capabilities:
        add:
        - IPC_LOCK           # IB 通信需要
        - NET_RAW
    volumeMounts:
    - name: shm
      mountPath: /dev/shm
    - name: hugepages
      mountPath: /hugepages
  volumes:
  - name: shm
    emptyDir:
      medium: Memory
      sizeLimit: 64Gi
  - name: hugepages
    emptyDir:
      medium: HugePages
```

---

## 7. 训练作业编排

### 7.1 PyTorchJob 完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Training Operator v1.8.x / Volcano v1.10.x
# apiVersion: kubeflow.org/v1（Training Operator v1.4+ 稳定）
# 镜像: 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3 或自建镜像基于 NGC base
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: llama-70b-pretrain
  namespace: ml-training
  labels:
    model: llama-70b
    experiment: pretrain-v3
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: OnFailure
      template:
        metadata:
          labels:
            model: llama-70b
            role: master
          annotations:
            scheduling.volcano.sh/queue-name: training-queue
        spec:
          schedulerName: volcano
          restartPolicy: OnFailure
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
            imagePullPolicy: IfNotPresent
            command:
            - torchrun
            - --nproc_per_node=8
            - --nnodes=16
            - --max_restarts=3
            - --rdzv_id=llama-pretrain
            - --rdzv_backend=c10d
            - --rdzv_endpoint=$(MASTER_ADDR):29500
            - train.py
            - --config=configs/llama_70b.yaml
            - --data_path=/data/pretrain
            - --output_dir=/checkpoint/llama-70b-v3
            - --save_steps=500
            env:
            - name: NCCL_DEBUG
              value: INFO
            - name: NCCL_SOCKET_IFNAME
              value: ib0
            - name: CUDA_DEVICE_MAX_CONNECTIONS
              value: "1"
            resources:
              limits:
                nvidia.com/gpu: 8
                memory: 512Gi
                openshift.io/mlnx_sriov_ib: 1
              requests:
                cpu: "64"
                memory: 512Gi
                nvidia.com/gpu: 8
                openshift.io/mlnx_sriov_ib: 1
            volumeMounts:
            - name: shm
              mountPath: /dev/shm
            - name: checkpoint
              mountPath: /checkpoint
            - name: dataset
              mountPath: /data
              readOnly: true
          volumes:
          - name: shm
            emptyDir:
              medium: Memory
              sizeLimit: 128Gi
          - name: checkpoint
            persistentVolumeClaim:
              claimName: checkpoint-pvc
          - name: dataset
            persistentVolumeClaim:
              claimName: pretrain-data-pvc
              readOnly: true
    Worker:
      replicas: 15
      restartPolicy: OnFailure
      template:
        metadata:
          labels:
            model: llama-70b
            role: worker
          annotations:
            scheduling.volcano.sh/queue-name: training-queue
        spec:
          schedulerName: volcano
          restartPolicy: OnFailure
          containers:
          - name: pytorch
            image: my-registry/training:v2.1   # 同上，生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
            imagePullPolicy: IfNotPresent
            command:
            - torchrun
            - --nproc_per_node=8
            - --nnodes=16
            - --max_restarts=3
            - --rdzv_id=llama-pretrain
            - --rdzv_backend=c10d
            - --rdzv_endpoint=$(MASTER_ADDR):29500
            - train.py
            - --config=configs/llama_70b.yaml
            - --data_path=/data/pretrain
            - --output_dir=/checkpoint/llama-70b-v3
            - --save_steps=500
            env:
            - name: NCCL_DEBUG
              value: INFO
            - name: NCCL_SOCKET_IFNAME
              value: ib0
            resources:
              limits:
                nvidia.com/gpu: 8
                memory: 512Gi
                openshift.io/mlnx_sriov_ib: 1
              requests:
                cpu: "64"
                memory: 512Gi
                nvidia.com/gpu: 8
                openshift.io/mlnx_sriov_ib: 1
            volumeMounts:
            - name: shm
              mountPath: /dev/shm
            - name: checkpoint
              mountPath: /checkpoint
            - name: dataset
              mountPath: /data
              readOnly: true
          volumes:
          - name: shm
            emptyDir:
              medium: Memory
              sizeLimit: 128Gi
          - name: checkpoint
            persistentVolumeClaim:
              claimName: checkpoint-pvc
          - name: dataset
            persistentVolumeClaim:
              claimName: pretrain-data-pvc
              readOnly: true
```

### 7.2 restartPolicy 语义表

| restartPolicy | Master 失败 | Worker 失败 | 适用场景 |
|---------------|------------|------------|---------|
| `Always` | 重启整个作业 | 重启该 Worker | 不推荐用于训练 |
| `OnFailure` | 重启整个作业（含所有 Worker） | 仅重启失败 Worker | 推荐用于训练（搭配 elastic） |
| `ExitCode` | 根据退出码决定是否重启 | 根据退出码决定是否重启 | 需要精细控制（退出码 0=成功，非 0=失败） |
| `Never` | 不重启，作业失败 | 不重启 | 仅用于调试 |

### 7.3 torchrun elastic 容错原理

```text
Elastic 训练流程（--max_restarts=3）：

t0: 16 节点启动，rdzv_backend=c10d 注册到 etcd/redis
    所有 worker 通过 MASTER_ADDR 发现彼此

t1: 节点 gpu-node-05 硬件故障，Worker-4 退出
    → rdzv 检测到 member 离开
    → 触发 rendezvous 重协商
    → 剩余 15 节点重新建立进程组

t2: Training operator 重启 Worker-4（调度到其他节点）
    → 新 Worker-4 加入 rdzv
    → 再次触发重协商
    → 16 节点恢复完整

t3: 从最近 checkpoint 恢复训练（--save_steps=500 保证 checkpoint 间隔）

关键：
- --max_restarts=3：最多允许 3 次弹性重启
- --rdzv_backend=c10d：使用 c10d（gloo）作为 rendezvous 后端
- checkpoint 必须存储在共享存储上（所有节点可访问）
```

### 7.4 作业 hang 检测思路

```bash
# 方法 1：NCCL watchdog 超时检测
# 在训练代码中启用 NCCL 超时
export NCCL_TIMEOUT=600  # 10 分钟无通信即超时

# 方法 2：心跳检测脚本（部署为 sidecar）
cat > /tmp/heartbeat-check.sh << 'EOF'
#!/bin/bash
HEARTBEAT_FILE="/checkpoint/.heartbeat"
TIMEOUT=1800  # 30 分钟

while true; do
  if [ -f "$HEARTBEAT_FILE" ]; then
    LAST_BEAT=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_BEAT))
    if [ $DIFF -gt $TIMEOUT ]; then
      echo "[$(date)] HANG DETECTED: no heartbeat for ${DIFF}s"
      # 发送告警
      curl -s -X POST "http://alertmanager:9093/api/v1/alerts" \
        -d '[{"labels":{"alertname":"TrainingHang","job":"llama-pretrain"},"annotations":{"summary":"Training job hung for 30min"}}]'
      exit 1
    fi
  fi
  sleep 60
done
EOF

# 方法 3：Prometheus + DCGM 指标检测
# 告警规则：GPU SM 利用率持续 > 5 分钟为 0 但进程存活
cat > /tmp/training-hang-rule.yaml << 'EOF'
groups:
- name: training_hang
  rules:
  - alert: TrainingJobHang
    expr: |
      DCGM_FI_DEV_GPU_UTIL == 0
      and on(pod) DCGM_FI_DEV_FB_USED > 0
      and on(pod) kube_pod_status_phase{phase="Running"} == 1
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Training job {{ $labels.pod }} appears hung (GPU util=0 but memory allocated)"
EOF
```

---

## 8. DRA（Dynamic Resource Allocation）

### 8.1 与 Device Plugin 的区别

| 特性 | Device Plugin | DRA |
|------|--------------|-----|
| API 模型 | kubelet gRPC，静态注册 | apiserver CRD，动态声明 |
| 资源发现 | ListAndWatch | ResourceSlice（节点上报） |
| 分配时机 | Pod 调度到节点后 Allocate | 调度前声明 ResourceClaim |
| 参数化 | 无（只能指定数量） | 支持设备属性过滤（如显存 >= 40GB） |
| 共享 | 不支持 | 原生支持多 Pod 共享同一 Claim |
| K8s 版本 | GA（1.8+） | Beta（1.32+）；1.31 为 Alpha v1alpha3 |
| 复杂设备 | 不适用 | 适合 FPGA、NIC、GPU 组合需求 |

### 8.2 K8s 版本演进

- **1.26**：DRA Alpha 首次引入（resource.k8s.io/v1alpha2）
- **1.27**：Alpha 改进
- **1.28**：ResourceClaim 改进
- **1.30**：Alpha 稳定
- **1.31**：v1alpha3（API 结构变化，ResourceSlice 引入）← **本系列基线 K8s 版本**
- **1.32**：Beta（默认开启 `DynamicResourceAllocation` feature gate；resource.k8s.io/v1beta1）

### 8.3 ResourceClaim 示例 YAML（前沿方向）

```yaml
# 适用: K8s 1.32+（Beta 默认开启）/ NVIDIA DRA Driver（preview）
# 版本敏感: resource.k8s.io/v1beta1 自 K8s 1.32 Beta；K8s 1.31 使用 v1alpha3（API 不兼容）
# K8s 1.26~1.30 使用 resource.k8s.io/v1alpha2（Alpha，需手动开启 feature gate）
# ResourceClaim - Pod 声明 GPU 需求
apiVersion: resource.k8s.io/v1beta1
kind: ResourceClaim
metadata:
  name: gpu-claim-for-training
  namespace: ml-training
spec:
  devices:
    requests:
    - name: gpu
      deviceClassName: nvidia-gpu
      selectors:
      - cel:
          expression: |
            device.attributes["nvidia.com"].product == "A100-SXM4-80GB"
      allocationMode: ExactCount
      count: 8
---
# ResourceSlice - 节点上报可用设备（由 DRA driver 自动创建）
apiVersion: resource.k8s.io/v1beta1
kind: ResourceSlice
metadata:
  name: gpu-node-01-gpus
spec:
  driver: gpu.resource.nvidia.com
  pool:
    name: gpu-node-01-pool
    generation: 1
    resourceSliceCount: 1
  nodeName: gpu-node-01
  devices:
  - name: gpu-0
    basic:
      attributes:
        nvidia.com:
          product: A100-SXM4-80GB
          memory: 80Gi
          migCapable: true
          index: 0
      capacity:
        nvidia.com:
          memory:
            value: 80Gi
  - name: gpu-1
    basic:
      attributes:
        nvidia.com:
          product: A100-SXM4-80GB
          memory: 80Gi
          migCapable: true
          index: 1
      capacity:
        nvidia.com:
          memory:
            value: 80Gi
---
# DeviceClass - 定义 GPU 设备类别
apiVersion: resource.k8s.io/v1beta1
kind: DeviceClass
metadata:
  name: nvidia-gpu
spec:
  selectors:
  - cel:
      expression: device.driver == "gpu.resource.nvidia.com"
  config:
  - opaque:
      driver: gpu.resource.nvidia.com
      parameters:
        cdiVersion: "0.7.0"
---
# Pod 引用 ResourceClaim
apiVersion: v1
kind: Pod
metadata:
  name: dra-training-pod
  namespace: ml-training
spec:
  containers:
  - name: training
    image: my-registry/training:v2.1   # 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
    resources:
      claims:
      - name: gpu-resources
  resourceClaims:
  - name: gpu-resources
    resourceClaimName: gpu-claim-for-training
```

---

## 9. 集群自身运维

### 9.1 etcd 备份恢复完整命令

```bash
# === 备份 ===
# 适用: etcd 3.5.15+（K8s 1.31 配套）；ETCDCTL_API=3 在 etcd v3.4+ 可省略（v3.5 默认 v3 API）
# 设置 etcd 环境变量
export ETCDCTL_API=3
export ETCD_ENDPOINTS=https://127.0.0.1:2379
export ETCD_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCD_CERT=/etc/kubernetes/pki/etcd/server.crt
export ETCD_KEY=/etc/kubernetes/pki/etcd/server.key

# 创建快照备份
etcdctl snapshot save /backup/etcd-snapshot-$(date +%Y%m%d-%H%M%S).db \
  --endpoints=${ETCD_ENDPOINTS} \
  --cacert=${ETCD_CACERT} \
  --cert=${ETCD_CERT} \
  --key=${ETCD_KEY}

# 验证快照
etcdctl snapshot status /backup/etcd-snapshot-20261104-120000.db \
  --write-out=table
# 期望输出：hash, revision, total keys, total size

# 自动化备份脚本（crontab 每小时执行）
cat > /usr/local/bin/etcd-backup.sh << 'EOF'
#!/bin/bash
export ETCDCTL_API=3
BACKUP_DIR="/backup/etcd"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
etcdctl snapshot save ${BACKUP_DIR}/etcd-${TIMESTAMP}.db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

# 保留最近 72 个备份
ls -t ${BACKUP_DIR}/etcd-*.db | tail -n +73 | xargs -r rm -f
echo "[$(date)] Backup completed: etcd-${TIMESTAMP}.db"
EOF
chmod +x /usr/local/bin/etcd-backup.sh
# crontab: 0 * * * * /usr/local/bin/etcd-backup.sh >> /var/log/etcd-backup.log 2>&1

# === 恢复 ===
# 1. 停止 kubelet 和 apiserver
systemctl stop kubelet

# 2. 移走当前 etcd 数据目录
mv /var/lib/etcd /var/lib/etcd.bak

# 3. 从快照恢复
etcdctl snapshot restore /backup/etcd-snapshot-20261104-120000.db \
  --data-dir=/var/lib/etcd \
  --name=master-01 \
  --initial-cluster=master-01=https://10.0.0.11:2380,master-02=https://10.0.0.12:2380,master-03=https://10.0.0.13:2380 \
  --initial-cluster-token=etcd-cluster-1 \
  --initial-advertise-peer-urls=https://10.0.0.11:2380

# 4. 恢复数据目录权限
chown -R etcd:etcd /var/lib/etcd
chmod 700 /var/lib/etcd

# 5. 重启 kubelet
systemctl start kubelet

# 6. 验证
kubectl get nodes
kubectl get pods -A | head -20
```

### 9.2 GPU 集群 etcd 压力来源与调优

GPU 集群 etcd 压力远大于普通集群，原因：

| 压力来源 | 说明 | 量化影响 |
|---------|------|---------|
| Node 状态更新频繁 | 每个 GPU 节点 GFD/NFD 大量 labels 变更触发 node update | 100 GPU 节点 ≈ 500 labels，每次心跳产生 ~2KB event |
| Pod 状态更新 | 训练作业多 worker × 多 container × 频繁 status 更新 | 1000 Pod × 每 10s 更新 = 100 writes/s |
| EndpointSlice 更新 | Service 关联的 endpoint 变化 | 训练服务多 replica 时尤为明显 |
| CRD 状态 | PyTorchJob/TFJob 等 CR 的 status subresource 更新 | 每个 Job 的 condition 更新都写 etcd |

**etcd 调优参数（/etc/kubernetes/manifests/etcd.yaml）：**

```yaml
# 适用: etcd 3.5.15+（K8s 1.31 配套）
# etcd 关键启动参数调优（/etc/kubernetes/manifests/etcd.yaml 静态 Pod）
# 版本敏感: --quota-backend-bytes 自 etcd 3.3+ 支持；--snapshot-count 默认 10000（3.5 未变）
- --quota-backend-bytes=8589934592       # 8GB 配额（默认 2GB 不够）
- --snapshot-count=10000                  # 快照间隔（默认 100000 太大）
- --max-request-bytes=1572864            # 1.5MB 最大请求（默认 1.5MB）
- --heartbeat-interval=100                # 心跳间隔 ms（SSD 环境可调小）
- --election-timeout=1000                 # 选举超时 ms（网络稳定可调小）
```

### 9.3 apiserver 关键参数

```yaml
# 适用: K8s 1.29~1.32 / kube-apiserver 静态 Pod manifest
# /etc/kubernetes/manifests/kube-apiserver.yaml 关键参数
# 版本敏感: --max-requests-inflight/--max-mutating-requests-inflight 自 K8s 1.20+ 稳定
# --watch-cache-sizes 格式: resource#size（K8s 1.28+ 支持 resource.group#size 细化）
spec:
  containers:
  - command:
    - kube-apiserver
    - --max-requests-inflight=800           # 非 mutating 请求并发上限（默认 400）
    - --max-mutating-requests-inflight=400  # mutating 请求并发上限（默认 200）
    - --request-timeout=60s                 # 请求超时
    - --min-request-timeout=1800            # watch 请求最小超时
    - --watch-cache-sizes=                  # watch 缓存大小
      node#500,
      pod#5000,
      endpoints#1000,
      event#1000
    - --etcd-compaction-interval=5m         # etcd 压缩间隔
    - --event-ttl=1h                        # Event 保留时间
    - --audit-log-maxage=30                 # 审计日志保留天数
    - --audit-log-maxbackup=10              # 审计日志最大备份数
    - --audit-log-maxsize=200               # 单个审计日志最大 MB
```

### 9.4 K8s 升级 SOP

```bash
# GPU 集群 K8s 升级 SOP（以 1.30 → 1.31 为例；基线目标 K8s 1.31.x）
# 注意: 升级前查阅 GPU Operator v24.9.2 兼容矩阵确认支持目标 K8s 版本

# === 前置检查 ===
# 1. 确认目标版本兼容性
kubectl get nodes -o wide   # 确保所有节点在 N-1 版本内
# 2. 备份 etcd
/usr/local/bin/etcd-backup.sh
# 3. 确认 GPU Operator 兼容目标 K8s 版本
# 查阅 https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/platform-support.html

# === 升级控制平面（逐台 master） ===
# 4. 驱逐 master 上的工作负载（通常 master 无训练 Pod）
kubectl drain master-01 --ignore-daemonsets --delete-emptydir-data

# 5. 升级 kubeadm（Ubuntu 22.04 apt 示例；Rocky 用 dnf upgrade kubeadm-1.31.0）
apt-get update
apt-get install -y kubeadm=1.31.0-*   # 查询当期版本: apt-cache madison kubeadm
kubeadm upgrade plan
kubeadm upgrade apply v1.31.0

# 6. 升级 kubelet + kubectl
apt-get install -y kubelet=1.31.0-* kubectl=1.31.0-*   # 查询: apt-cache madison kubelet
systemctl daemon-reload
systemctl restart kubelet

# 7. 解除驱逐
kubectl uncordon master-01

# 8. 重复 4-7 对其他 master 节点

# === 升级工作节点（逐台，确保训练作业不中断） ===
# 9. 选择无训练作业的节点优先升级
kubectl get pods -A -o wide --field-selector spec.nodeName=gpu-node-01 | grep ml-training

# 10. 驱逐（训练 Pod 会迁移到其他节点）
kubectl drain gpu-node-01 \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --grace-period=300 \
  --pod-selector='app!=critical-service'

# 11. SSH 到节点升级（Ubuntu 22.04 apt 示例；Rocky 用 dnf upgrade kubelet-1.31.0）
ssh gpu-node-01
apt-get update
apt-get install -y kubelet=1.31.0-* kubectl=1.31.0-*   # 查询: apt-cache madison kubelet
kubeadm upgrade node    # 注意：worker 用 upgrade node 而非 upgrade apply
systemctl daemon-reload
systemctl restart kubelet
exit

# 12. 解除驱逐并验证
kubectl uncordon gpu-node-01
kubectl get node gpu-node-01
# 确认 GPU 资源重新注册
kubectl get node gpu-node-01 -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'

# 13. 逐台重复 9-12
```

### 9.5 containerd 升级覆盖 nvidia runtime 配置的坑与预防

```bash
# === 问题描述 ===
# 升级 containerd 时，包管理器可能覆盖 /etc/containerd/config.toml
# 导致 [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia] 段丢失
# 后果：所有 GPU Pod 无法启动，报 "runtime nvidia not found"

# === 预防措施 ===

# 1. 备份 nvidia runtime 配置
cp /etc/containerd/config.toml /etc/containerd/config.toml.nvidia-bak

# 2. 使用 drop-in 配置目录（containerd 1.7+；注意 containerd 2.0 配置结构有差异）
mkdir -p /etc/containerd/config.d/

# containerd 1.7.x drop-in 配置（注意 containerd 2.0 配置结构有差异，升级需谨慎）
cat > /etc/containerd/config.d/nvidia-runtime.toml << 'EOF'
# 适用: containerd 1.7.x / nvidia-container-toolkit v1.17.4
version = 2
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
    BinaryName = "/usr/bin/nvidia-container-runtime"
    SystemdCgroup = true
EOF

# 3. 在主配置中引入 drop-in
# 确认 /etc/containerd/config.toml 包含：
# imports = ["/etc/containerd/config.d/*.toml"]

# 4. containerd 升级后验证
systemctl restart containerd
crictl info | jq '.config.containerd.runtimes | keys'
# 期望输出包含 "nvidia"

# === 修复方案（如果已被覆盖） ===
# 从备份恢复
cp /etc/containerd/config.toml.nvidia-bak /etc/containerd/config.toml
systemctl restart containerd

# 或重新生成（使用 GPU Operator 的 nvidia-container-toolkit）
nvidia-ctk runtime configure --runtime=containerd
systemctl restart containerd

# 验证
crictl info | jq '.config.containerd.runtimes | keys'
```

---

## 10. 故障速查表

| # | 症状 | 排查命令 | 根因 | 解决方案 |
|---|------|---------|------|---------|
| 1 | Pod Pending，Events 显示 `0/N nodes are available: insufficient nvidia.com/gpu` | `kubectl describe pod <pod> \| grep -A10 Events` | GPU 资源不足或已被其他 Pod 占满 | 检查节点 allocatable：`kubectl get nodes -o json \| jq '.items[].status.allocatable["nvidia.com/gpu"]'`；清理不需要的 Pod 或扩容 |
| 2 | Pod CrashLoopBackOff，容器内 `nvidia-smi` 报 `CUDA error: unknown error` | `kubectl exec <pod> -- nvidia-smi`；`kubectl logs <pod>` | nvidia runtime 未配置或驱动库未挂载 | 检查 containerd nvidia runtime：`crictl info \| jq '.config.containerd.runtimes'`；重装 nvidia-container-toolkit |
| 3 | Device Plugin Pod 报 `Failed to register with kubelet` | `kubectl logs -n gpu-operator -l app=nvidia-device-plugin` | kubelet device plugin 目录权限或 socket 不存在 | 检查：`ls -la /var/lib/kubelet/device-plugins/`；重启 kubelet：`systemctl restart kubelet` |
| 4 | GPU 数量不对（节点显示 0 GPU） | `ssh <node> 'nvidia-smi -L'`；`kubectl get node <node> -o yaml \| grep nvidia` | 驱动未加载或 device plugin 未运行 | 加载驱动：`modprobe nvidia`；检查 device plugin Pod：`kubectl get pods -n gpu-operator -o wide \| grep <node>` |
| 5 | NCCL 通信超时/失败 | `kubectl logs <pod> \| grep -i nccl`；`dmesg \| grep -i nvidia` | IB 网络不通或 GPU 间 P2P 被禁用 | 检查：`ibstat`；`nvidia-smi topo -m`；确认 `NCCL_IB_DISABLE` 未设置 |
| 6 | OOMKilled | `kubectl describe pod <pod> \| grep -i oom`；`kubectl top pod <pod>` | 内存 limit 设置过低或数据加载过多 | 增大 memory limit；检查 dataloader num_workers 和 prefetch_factor |
| 7 | GPU Xid 错误（dmesg 报 Xid 79/48/119） | `ssh <node> 'dmesg \| grep -i xid'`；`nvidia-smi -q -d ECC` | GPU 硬件故障或 ECC 不可纠正错误 | `nvidia-smi -r -i <gpu>` 重置；如频繁出现需更换 GPU |
| 8 | MIG 设备不可见 | `ssh <node> 'nvidia-smi mig -lgi'`；`kubectl get node <node> -o yaml \| grep mig` | MIG 配置未应用或 device plugin 未识别 MIG | 重新打标签触发 mig-parted：`kubectl label node <node> nvidia.com/mig.config=all-1g.10gb --overwrite` |
| 9 | 训练作业 hang（GPU 利用率 0%） | `nvidia-smi` 看 GPU 利用率；`kubectl exec <pod> -- nvidia-smi` | NCCL 通信死锁或数据加载阻塞 | 检查 NCCL watchdog；`strace -p <pid>` 看阻塞系统调用；检查数据路径 IO 延迟 |
| 10 | containerd 重启后 GPU Pod 全部失败 | `crictl info \| jq '.config.containerd.runtimes'` | containerd 升级覆盖了 nvidia runtime 配置 | 恢复备份或执行 `nvidia-ctk runtime configure --runtime=containerd`；`systemctl restart containerd` |
| 11 | etcd leader 频繁切换 | `etcdctl endpoint status --cluster -w table` | 磁盘 IO 延迟高或网络抖动 | 检查磁盘 IOPS：`fio --name=test --ioengine=libaio --rw=randwrite --bs=4k --size=1G`；确保 etcd 使用 SSD |
| 12 | PyTorchJob Worker 无法连接 Master | `kubectl logs <worker-pod> \| grep -i connect`；`kubectl get svc -n ml-training` | Master Service 未就绪或 DNS 解析失败 | 确认 headless service 存在；检查 `$(MASTER_ADDR)` 环境变量是否正确注入 |
| 13 | 节点 NotReady，kubelet 日志报 `PLEG not healthy` | `journalctl -u kubelet \| grep PLEG` | 容器 runtime 响应慢（大量 GPU 容器同时启动） | 优化镜像大小；使用镜像预拉取；增加 `--runtime-request-timeout=30s` |
| 14 | Volcano/Kueue 队列中作业一直 Admitted=False | `vcctl job list`；`kueuectl get workload -n <ns>` | 队列配额不足或 gang scheduling 资源不满足 | 检查 queue capacity：`kubectl get queue -o yaml`；减少并发作业数或增加集群资源 |
| 15 | time-slicing Pod GPU 性能严重下降 | `nvidia-smi` 对比独占 Pod 和 time-slicing Pod 的 SM clock | 同一 GPU 上 CUDA context 切换开销过大 | 减少 replicas 数量；对性能敏感的负载改用整卡或 MIG |
