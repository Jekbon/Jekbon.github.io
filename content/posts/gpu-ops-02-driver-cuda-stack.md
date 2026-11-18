---
title: "GPU 集群运维（02）：驱动、固件与 CUDA 软件栈"
date: 2026-11-08T09:00:00+08:00
draft: false
description: "NVIDIA 驱动三种安装方式、Fabric Manager、nvidia-persistenced、内核模块体系、驱动升级完整 SOP、固件管理、nvidia-container-toolkit，以及 nvidia-smi 命令大全与安装故障排查表。"
tags: ["GPU", "NVIDIA", "CUDA", "驱动"]
series: "GPU 集群运维"
---

---

## 本册环境基线

> 以下为本册命令/配置默认适用的版本环境，完整基线定义见 [00-版本基线](./00-版本基线.md)。

**操作系统与内核**

| 项目 | Ubuntu 基线 | Rocky/RHEL 基线 |
|---|---|---|
| OS 版本 | Ubuntu Server 22.04.5 LTS (Jammy) | Rocky Linux 9.5 |
| 内核（GA） | 5.15.0-xxx-generic | 5.14.0-503.xxx.el9_5 |
| 内核（HWE，可选） | 6.8.0-xxx-generic（linux-generic-hwe-22.04，需 R550+ 驱动） | 不支持（RHEL 系内核锁定） |
| gcc | 11.4（可装 12/13） | 11.5 |
| DKMS 依赖包 | `linux-headers-$(uname -r)` | `kernel-devel-$(uname -r)` + `kernel-headers-$(uname -r)`，版本必须与运行内核完全一致 |

**本册涉及的主要软件版本**

| 组件 | 主基线（Hopper/Blackwell） | 兼容基线（Ampere 存量） |
|---|---|---|
| NVIDIA Driver | R570 生产分支，570.124.04（示例） | R550 生产分支，550.127.08（示例） |
| CUDA Toolkit | 12.8 | 12.4 |
| Fabric Manager | 与驱动完全同版本（570.124.04 / 550.127.08） | 同左 |
| GSP 固件 | 随驱动打包，R515+ 默认启用 | 同左 |
| nvidia-persistenced | 随驱动同版本 | 同左 |
| nvidia-container-toolkit | v1.17.4 | 同左 |
| NVIDIA GPU Operator | Helm chart v24.9.2 | 同左 |
| ├─ k8s-device-plugin | v0.17.0 | 同左 |
| ├─ dcgm-exporter | 4.1.0-4.2.0-ubuntu22.04（示例 tag） | 同左 |
| ├─ gpu-feature-discovery | v0.17.0 | 同左 |
| └─ mig-manager | v0.8.0 | 同左 |
| DOCA-OFED | 24.10（MLNX_OFED 24.04-0.7.0.0 为末代旧名） | 同左 |
| nvidia-peermem | 驱动 R465+ 内置，替代旧 nv_peer_mem | 同左 |

**驱动↔CUDA 兼容速查（摘自 00-基线 §2.1，Linux x86_64 最低驱动要求）**：

| CUDA Toolkit | 最低驱动版本 |
|---|---|
| 12.8 | ≥570.26 |
| 12.6 | ≥560.28.03 |
| 12.4 | ≥550.54.14 |
| 12.2 | ≥535.86.05 |

**版本钉死规则**：`apt-get install nvidia-driver-570-server=570.124.04-0ubuntu1`（版本号以 `apt-cache madison` 查询为准）；dnf 用 `dnf --showduplicates list` 查询；helm chart 用 `--version` 钉死；容器镜像 tag 禁用 `latest`；标"（示例）"的 build 号以 NVIDIA 官方兼容矩阵当期推荐为准。

---

## 1. 版本体系

### 1.1 驱动分支说明

| 分支类型 | 英文命名 | 发布节奏 | 支持周期 | 适用场景 |
|---------|---------|---------|---------|---------|
| Production Branch | PB | 每季度 | 12 个月（以版本号标注） | **生产环境推荐**，经过充分测试，稳定性优先 |
| New Feature Branch | NFB | 每月 | 3 个月 | 开发测试，使用最新 CUDA 特性 |

> **选型建议**：GPU 集群生产环境始终使用 Production Branch；只有需要最新 CUDA 特性（如 FP8、新版 Transformer Engine）时才用 NFB，并计划在下一个 PB 发布后迁移。

### 1.2 驱动版本命名规则

```text
主版本号.次版本号（如 535.183.01）
    │       │
    │       └── 补丁版本（偶数=PB，奇数=NFB）
    └────────── 主版本（535 = R535 系列，对应 CUDA 12.2）
```

| 驱动主版本 | 对应 CUDA 最高版本 | CUDA 工具包 |
|-----------|-----------------|-----------|
| 525.x | CUDA 12.0 | cuda-12-0 |
| 535.x | CUDA 12.2 | cuda-12-2 |
| 545.x | CUDA 12.3 | cuda-12-3 |
| 550.x | CUDA 12.4 | cuda-12-4 |
| 560.x | CUDA 12.6 | cuda-12-6 |
| 570.x | CUDA 12.8 | cuda-12-8 |

> **注**：本系列基线以 R570（CUDA 12.8）为主基线、R550（CUDA 12.4）为兼容基线；驱动向下兼容旧 CUDA，反向不行。

### 1.3 Driver-CUDA-cuDNN-NCCL-PyTorch 兼容关系

| PyTorch 版本 | CUDA Toolkit | cuDNN | NCCL | 最低驱动版本（Linux） |
|-------------|-------------|-------|------|------------------|
| 2.6.x | 12.8 | 9.8.x | 2.26.x | ≥570.26 |
| 2.5.x | 12.4 / 12.6 | 9.3.x | 2.23.x | ≥550.54.14 |
| 2.4.x | 12.4 | 9.1.x | 2.22.x | ≥550.54.14 |
| 2.3.x | 12.1 / 12.4 | 8.9.x / 9.1.x | 2.21.x | ≥535.104.05 |
| 2.2.x | 12.1 / 12.4 | 8.9.x / 9.1.x | 2.20.x | ≥535.86.10 |
| 2.1.x | 12.1 | 8.9.x | 2.18.x | ≥530.30.02 |

> **注**：以上为历史兼容参考；本系列基线以 PyTorch 2.6.0（cu128 轮子）或 NGC `pytorch:24.12-py3` 为准，具体兼容关系以 NVIDIA NGC 容器 release notes 当期为准。

**查询最新兼容关系的方法**：
```bash
# 方法 1：查看 NVIDIA 官方矩阵（离线方式）
# 下载 CUDA Toolkit Release Notes PDF，查阅 "Driver Version" 章节

# 方法 2：PyTorch 官网兼容性矩阵
# https://github.com/pytorch/pytorch/blob/main/RELEASE.md

# 方法 3：容器镜像标签（最权威，NVIDIA NGC 已帮你验证过）
# nvcr.io/nvidia/pytorch:24.07-py3  → CUDA 12.4 + PyTorch 2.4 + cuDNN 9.1 + NCCL 2.22

# 方法 4：通过已安装驱动查支持的 CUDA 版本
nvidia-smi  # 右上角 "CUDA Version: 12.4" 表示驱动支持的最高 CUDA 版本
```

### 1.4 容器内 CUDA 与宿主驱动的关系

**核心原理**：NVIDIA 驱动分两部分——
1. **内核模块**（`nvidia.ko`、`nvidia-uvm.ko` 等）：运行在宿主机内核中，必须安装在宿主机
2. **用户态库**（`libcuda.so`、`libnvidia-ml.so` 等）：通过 `nvidia-container-toolkit` 挂载到容器内

**为什么容器不带驱动**：
- 内核模块与宿主机内核绑定，不能在容器内加载不同版本的 `nvidia.ko`
- 容器镜像只打包 CUDA Toolkit（`libcudart.so`、`nvcc` 等用户态工具），不含驱动
- `nvidia-container-toolkit` 在容器启动时将宿主的驱动用户态库 bind-mount 进容器
- 因此容器内 CUDA 版本可以低于或等于宿主驱动支持的 CUDA 版本，但不能高于

```text
┌────────────────────────────────────────────────────────┐
│ 宿主机（Host OS）                                      │
│  内核模块：nvidia.ko / nvidia-uvm.ko（驱动 570.124.04）│
│  用户态库：libcuda.so.570.124.04                       │
│                                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 容器（Container）                                │  │
│  │  CUDA Toolkit：12.8（镜像自带，用户态）          │  │
│  │  nvidia-container-toolkit v1.17.4 注入：          │  │
│  │    /usr/lib/x86_64-linux-gnu/libcuda.so.570.124.04│ │
│  │    /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*  │  │
│  │    /dev/nvidia*（设备节点）                      │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

---

## 2. 驱动安装三种方式

### 2.1 方式 A：runfile 安装

#### 禁用 nouveau

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5（nouveau 禁用方法两系统相同，initramfs 重建命令不同）
# 创建 blacklist 文件
blacklist nouveau
options nouveau modeset=0
blacklist lbm-nouveau
alias nouveau off
alias lbm-nouveau off
EOF

# 重建 initramfs（Ubuntu/Debian）
update-initramfs -u

# 或重建 initramfs（RHEL/Rocky）
dracut --force

# 验证 nouveau 已禁用
lsmod | grep nouveau  # 应无输出

# 如仍有 nouveau 加载，强制卸载
rmmod nouveau
```

#### 下载与安装

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15, gcc 11.4) / Rocky 9.5 (kernel 5.14, gcc 11.5)
# 以 R570 生产分支 570.124.04（示例）为例；适用内核范围：5.15.x（GA）/ 6.8.x（HWE，需 R550+）
# runfile 安装方式适合无包管理器的离线环境；DKMS 要求 linux-headers/kernel-devel 版本与运行内核完全一致
# 下载 runfile
cd /tmp
wget https://us.download.nvidia.com/XFree86/Linux-x86_64/570.124.04/NVIDIA-Linux-x86_64-570.124.04.run

# 安装（关键参数说明）
chmod +x NVIDIA-Linux-x86_64-570.124.04.run
./NVIDIA-Linux-x86_64-570.124.04.run \
    --silent \
    --dkms \
    --no-cc-version-check \
    --no-x-check \
    --disable-nouveau \
    --kernel-source-path=/usr/src/linux-headers-$(uname -r)  # Ubuntu 路径；Rocky 为 /usr/src/kernels/$(uname -r)

# 参数说明
# --silent          静默安装，无交互
# --dkms            启用 DKMS，内核升级后自动重编译驱动模块
# --no-cc-version-check  跳过 gcc 版本检查（内核与 gcc 版本不完全匹配时有用）
# --disable-nouveau      自动添加 nouveau blacklist
# --kernel-source-path   指定内核头文件路径

# 加载驱动模块
modprobe nvidia
modprobe nvidia-uvm
modprobe nvidia-modeset

# 验证
nvidia-smi
# 期望输出：NVIDIA-SMI 570.124.04  Driver Version: 570.124.04  CUDA Version: 12.8
```

#### DKMS 机制说明

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / Rocky 9.5 (kernel 5.14)
# DKMS 在内核升级时自动重编译 nvidia.ko；kernel-devel 版本必须与新内核完全一致
# 查看 DKMS 状态
dkms status
# 期望输出：nvidia/570.124.04, 5.15.0-xxx-generic, x86_64: installed

# 手动触发 DKMS 重编译
dkms build nvidia/570.124.04 -k $(uname -r)
dkms install nvidia/570.124.04 -k $(uname -r)

# 安装新内核后，DKMS 自动为新内核编译模块（前提：新内核头文件已安装，版本必须与运行内核一致）
apt-get install -y linux-headers-$(uname -r)  # Ubuntu
dnf install -y kernel-devel-$(uname -r)        # Rocky（包名不同，注意！）
```

### 2.2 方式 B：apt/yum 仓库安装

#### Ubuntu/Debian

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15, systemd 249)
# 添加 NVIDIA CUDA 仓库
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
dpkg -i cuda-keyring_1.1-1_all.deb
apt-get update

# 安装驱动（指定版本，版本号以 apt-cache madison nvidia-driver-570-server 查询为准）
apt-get install -y nvidia-driver-570-server=570.124.04-0ubuntu1  # R570 主基线
apt-mark hold nvidia-driver-570-server  # 锁定版本，防止意外升级
# 或安装完整 CUDA toolkit（含驱动，注意会与单独装驱动冲突，二选一）
apt-get install -y cuda-toolkit-12-8

# 安装 DKMS 模块（apt 方式通常自动安装）
apt-get install -y nvidia-dkms-570-server

# 验证
dpkg -l | grep nvidia
nvidia-smi
```

#### RHEL/Rocky Linux

```bash
# 环境: Rocky Linux 9.5 (kernel 5.14, systemd 252, SELinux enforcing)
# 注意：Rocky 的包名与 Ubuntu 不同（nvidia-driver vs nvidia-driver-570-server），且 SELinux 可能影响模块加载
# 添加 NVIDIA CUDA 仓库
dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/rhel9/x86_64/cuda-rhel9.repo

# 安装驱动（指定版本，版本号以 dnf --showduplicates list nvidia-driver 查询为准）
dnf install -y nvidia-driver-570.124.04 nvidia-driver-dkms-570.124.04  # R570 主基线
dnf versionlock add nvidia-driver  # 锁定版本

# 安装完整 CUDA toolkit
dnf install -y cuda-toolkit-12-8

# 验证
rpm -qa | grep nvidia
nvidia-smi
```

#### 内核升级后行为（DKMS 自动触发）

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / driver R570 570.124.04
# apt/yum 方式安装时，DKMS 会自动注册
# 内核升级后重启，DKMS 自动为新内核编译 nvidia.ko

# 如果 DKMS 自动编译失败（常见：新内核头文件未安装，版本必须与运行内核一致）
# 手动修复步骤：
apt-get install -y linux-headers-$(uname -r)  # Ubuntu（Rocky 用 dnf install kernel-devel-$(uname -r)）
dkms remove nvidia/570.124.04 --all            # 清理旧状态
dkms add nvidia/570.124.04                     # 重新注册
dkms build nvidia/570.124.04 -k $(uname -r)    # 编译
dkms install nvidia/570.124.04 -k $(uname -r)  # 安装
modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia  # 卸载旧模块
modprobe nvidia                                      # 加载新模块
nvidia-smi                                           # 验证
```

### 2.3 方式 C：GPU Operator（K8s 环境）

```bash
# 环境: Ubuntu 22.04.5 / K8s 1.31 / Helm 3.16.x
# 添加 NVIDIA Helm 仓库
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# 查看可用版本（基线 chart 版本 v24.9.2，支持 K8s 1.29~1.32）
helm search repo nvidia/gpu-operator --versions | head -20
```

#### 完整 values.yaml

```yaml
# /opt/gpu-operator/values.yaml（GPU Operator chart v24.9.2，K8s 1.31，Ubuntu 22.04.5）
# 注意：镜像 tag 必须钉死，禁用 latest；组件版本须与 chart 版本配套，不可随意混用

# 全局设置
platform:
  openshift: false

# NVIDIA Driver（容器化驱动，不需要宿主机预装）
driver:
  enabled: true
  version: "570.124.04-ubuntu22.04"  # R570 主基线，tag 格式：<version>-ubuntu22.04；Ampere 存量可用 550.127.08-ubuntu22.04
  image: nvidia-driver
  repository: nvcr.io/nvidia
  # 是否使用预编译驱动（推荐，避免每次内核升级都重编译）
  usePrecompiled: true
  # RDMA 支持（IB/RoCE 环境必须）
  rdma:
    enabled: true
    useHostMofed: false
  # 管理设置
  manager:
    image: k8s-driver-manager
    repository: nvcr.io/nvidia/cloud-native
    version: v0.7.0
  # 容忍调度
  tolerations:
    - key: "node-role.kubernetes.io/master"
      operator: "Exists"
      effect: "NoSchedule"
    - key: "nvidia.com/gpu"
      operator: "Exists"
      effect: "NoSchedule"

# NVIDIA Container Toolkit（nvidia-container-runtime）
toolkit:
  enabled: true
  image: nvidia-container-toolkit
  repository: nvcr.io/nvidia/k8s
  version: v1.17.4-ubuntu22.04  # 基线 v1.17.4；tag 格式 v<version>-ubuntu22.04

# NVIDIA Device Plugin（K8s device plugin，暴露 GPU 资源）
devicePlugin:
  enabled: true
  image: k8s-device-plugin
  repository: nvcr.io/nvidia/k8s
  version: v0.17.0-ubuntu22.04  # 基线 v0.17.0（chart v24.9.2 配套）
  config:
    name: nvidia-device-plugin-config
    default: "default"
  # 共享策略（MIG 场景使用）
  # sharingStrategy: "time-slicing"

# NVIDIA DCGM（GPU 监控）
dcgm:
  enabled: true
  image: dcgm
  repository: nvcr.io/nvidia/cloud-native
  version: 4.2.0-1-ubuntu22.04  # 基线 4.2.x；注意 4.x 部分指标名前缀与 3.x 不同

# DCGM Exporter（Prometheus metrics）
dcgmExporter:
  enabled: true
  image: dcgm-exporter
  repository: nvcr.io/nvidia/k8s
  version: 4.1.0-4.2.0-ubuntu22.04  # 基线 4.1.0-4.2.0；tag 格式 <dcgm-ver>-<exporter-ver>-ubuntu22.04
  serviceMonitor:
    enabled: true  # 需集群部署 Prometheus Operator

# GFD（GPU Feature Discovery，自动标签 GPU 型号/内存）
gfd:
  enabled: true
  image: gpu-feature-discovery
  repository: nvcr.io/nvidia/cloud-native
  version: v0.17.0-ubuntu22.04  # 基线 v0.17.0（chart v24.9.2 配套，与 device-plugin 同版本）

# MIG Manager（Multi-Instance GPU 切分）
migManager:
  enabled: false  # 根据需求开启，H100/A100 SXM 支持 MIG
  image: mig-manager
  repository: nvcr.io/nvidia/cloud-native
  version: v0.8.0-ubuntu22.04  # 基线 v0.8.0（chart v24.9.2 配套）

# Node Feature Discovery（硬件特征发现）
node-feature-discovery:
  enabled: true
  worker:
    tolerations:
      - key: "nvidia.com/gpu"
        operator: "Exists"
        effect: "NoSchedule"

# Sandbox Device Plugin（vGPU 场景，物理机不需要）
sandboxDevicePlugin:
  enabled: false

# VFIO Manager（GPU 直通虚拟化场景）
vfioManager:
  enabled: false

# Validator（安装后自动验证）
validator:
  enabled: true
  driver:
    image: cuda-sample
    repository: nvcr.io/nvidia/k8s
    version: vectoradd-cuda12.8.0  # 与驱动 R570 / CUDA 12.8 配套
  toolkit:
    image: cuda-sample
    repository: nvcr.io/nvidia/k8s
    version: vectoradd-cuda12.8.0  # 与驱动 R570 / CUDA 12.8 配套

# Node 选择器（只在有 GPU 的节点运行）
nodeSelector:
  nvidia.com/gpu.present: "true"
```

#### 安装与验证

```bash
# 环境: K8s 1.31 / Helm 3.16.x / Ubuntu 22.04.5
# chart 版本钉死到 v24.9.2（基线版本），支持 K8s 1.29~1.32
# 创建 namespace
kubectl create namespace gpu-operator

# 安装 GPU Operator（--version 钉死 chart 版本，禁止不指定版本安装）
helm upgrade --install gpu-operator nvidia/gpu-operator \
    --namespace gpu-operator \
    --version v24.9.2 \
    --values /opt/gpu-operator/values.yaml \
    --wait \
    --timeout 600s

# 验证 Pod 状态
kubectl -n gpu-operator get pods -o wide
# 期望所有 Pod 为 Running/Completed

# 验证 GPU 资源已注册
kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, gpu: .status.allocatable["nvidia.com/gpu"]}'
# 期望 gpu: "8"

# 验证 nvidia-smi 可在容器内运行
kubectl run nvidia-smi-test --rm -it --restart=Never \
    --image=nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04 \
    --requests="nvidia.com/gpu=1" \
    -- nvidia-smi

# 查看 Validator 结果
kubectl -n gpu-operator logs -l app=nvidia-operator-validator
```

---

## 3. Fabric Manager

### 3.1 作用原理

**Fabric Manager（FM）是 NVSwitch 机型（DGX/HGX A100、H100）必需的系统服务**，原因如下：

- NVSwitch 不是简单的 PCIe Switch，它需要软件初始化配置路由表
- FM 负责：①初始化 NVSwitch 互联拓扑 ②管理 NVLink 链路状态 ③为 NCCL 提供路由信息
- 没有 FM，NVSwitch 不工作，8 GPU 之间无法通过 NVLink 通信，nvidia-smi 拓扑会降级为 SYS/NODE
- FM 版本必须与驱动版本**严格一致**（如驱动 570.124.04 → FM 570.124.04；550.127.08 → FM 550.127.08）

### 3.2 安装

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / driver R570 570.124.04
# FM 版本必须与驱动严格一致（570.124.04），版本不匹配 = GPU 不可用（HGX 机型）
# Ubuntu（通过 NVIDIA CUDA 仓库，已在 2.2 节添加）
apt-get install -y nvidia-fabricmanager-570=570.124.04-1  # 版本号必须与驱动完全一致

# 或安装与驱动同版本的最新版（自动匹配，但建议显式指定版本避免意外）
apt-get install -y nvidia-fabricmanager

# Rocky 9.5（包名略有不同，注意 - 与 _ 的差异）
dnf install -y nvidia-fabric-manager-570.124.04  # Rocky 包名格式与 Ubuntu 不同

# 验证安装版本（两者输出应一致，且与 nvidia-smi 显示的驱动版本完全相同）
dpkg -l nvidia-fabric-manager  # Ubuntu
rpm -q nvidia-fabric-manager   # Rocky
```

### 3.3 systemctl 管理

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5（systemctl 单元名两系统均为 nvidia-fabricmanager，无差异）
# 启动并设置开机自启

# 查看状态
systemctl status nvidia-fabricmanager
# 期望：Active: active (running)

# 查看日志（排查 NVSwitch 初始化问题）
journalctl -u nvidia-fabricmanager -f

# 重启（驱动升级后必须重启 FM）
systemctl restart nvidia-fabricmanager

# FM 配置文件位置
cat /usr/share/nvidia/nvswitch/nvswitch.yaml  # 默认配置，一般不修改
```

### 3.4 常见故障

```text
# 故障 1：版本不匹配
# 现象：
systemctl status nvidia-fabricmanager
# 输出：
# nvidia-fabricmanager.service - NVIDIA fabric manager service
#    Loaded: loaded (/lib/systemd/system/nvidia-fabricmanager.service; enabled)
#    Active: failed (Result: exit-code) since ...
#    Process: ... fabricmanager (code=exited, status=1/FAILURE)
# journalctl -u nvidia-fabricmanager 显示：
# "Fabric Manager version 550.127.08 does not match driver version 570.124.04"

# 解决：安装与驱动完全一致版本的 FM（示例：驱动 570.124.04 → FM 570.124.04）
apt-get install -y nvidia-fabricmanager-570=570.124.04-1  # Ubuntu
dnf install -y nvidia-fabric-manager-570.124.04           # Rocky
systemctl restart nvidia-fabricmanager

# 故障 2：FM 启动后 nvidia-smi topo -m 仍无 NVLink
# 原因：FM 启动顺序不对，必须在 nvidia.ko 加载之后启动
# 解决：
systemctl restart nvidia-persistenced
systemctl restart nvidia-fabricmanager
# 等待 30 秒后重新检查
nvidia-smi topo -m

# 故障 3：FM 正常运行但 NVLink 显示为 degraded
# 检查：
nvidia-smi nvlink --status
# 如某条 NVLink 显示 Inactive，可能是 NVSwitch 硬件故障，需 OEM 报修
```

---

## 4. nvidia-persistenced 与持久化模式

### 4.1 原理

**问题**：NVIDIA 驱动默认"按需加载"模式，每次调用 `nvidia-smi` 或 CUDA 程序时，驱动需要重新初始化（约 1~3 秒），且当所有 GPU 进程退出后驱动会卸载，导致：
- 训练任务间歇性延迟（每次 CUDA 初始化慢 1~3 秒）
- 驱动卸载/加载期间出现竞态条件
- `nvidia-smi` 首次调用卡 1~3 秒

**解决**：`nvidia-persistenced` 守护进程持续保持驱动初始化状态，即使没有 GPU 进程运行。

### 4.2 启用命令

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5（systemctl 单元名两系统均为 nvidia-persistenced，无差异）
# nvidia-persistenced 版本随驱动同版本（R570 570.124.04 配套）
# 启用持久化守护进程（推荐方式，通过 systemd）

# 或使用 nvidia-smi 设置（效果等价，但不持久化到 systemd）
nvidia-smi -pm 1  # 启用持久化模式（等效于 persistenced 运行）
nvidia-smi -pm 0  # 禁用（不推荐）

# 验证
nvidia-smi
# 查看 "Persistence Mode" 行：
# Persistence Mode                : Enabled

# 或通过进程验证
ps aux | grep nvidia-persistenced
# 应有 /usr/bin/nvidia-persistenced --user nvidia-persistenced --no-persistence-mode 进程

# 查看持久化状态
nvidia-smi -q | grep -i persistence
# Persistence Mode                      : Enabled
```

### 4.3 与 K8s 的配合

```bash
# K8s GPU 节点必须在系统启动时自动启用 persistenced
# GPU Operator 会自动配置，手动安装驱动需自行设置

# 确保 nvidia-persistenced 服务开机自启
systemctl enable nvidia-persistenced

# 验证服务在重启后自动运行
reboot
# 重启后：
systemctl is-active nvidia-persistenced  # 应输出 active
nvidia-smi | grep "Persistence Mode"     # 应输出 Enabled
```

---

## 5. 内核模块体系

### 5.1 四大内核模块

| 模块名称 | 作用 | 何时需要 |
|---------|------|---------|
| `nvidia.ko` | 核心驱动，管理 GPU 硬件、显存、计算单元 | **必须**，所有场景 |
| `nvidia-uvm.ko` | Unified Virtual Memory，GPU/CPU 统一地址空间，支持 CUDA managed memory | **必须**，深度学习框架均使用 UVM |
| `nvidia-modeset.ko` | 显示模式设置（Display Mode Setting），管理显示器输出 | 服务器无显示器可不加载；有显示输出需求时加载 |
| `nvidia-peermem.ko` | GPU Direct RDMA（GDR），GPU 显存与 IB HCA 直接通信，绕过 CPU 内存 | **IB 环境必须**，NCCL GPUDirect RDMA 依赖此模块；驱动 R465+ 内置，**替代旧 `nv_peer_mem`（已废弃）** |

### 5.2 modprobe 配置

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / Rocky 9.5 (kernel 5.14) / driver R570 570.124.04
# 模块配置两系统无差异（均在 /etc/modprobe.d/ 和 /etc/modules-load.d/）
# nvidia-peermem.ko：R465+ 内置，替代旧 nv_peer_mem（已废弃），用于 GPUDirect RDMA
# 创建统一的 NVIDIA 模块配置
# NVIDIA 内核模块配置
# 启用 NVLink P2P（GPU 间直接通信）
options nvidia NVreg_EnableUserTIModules=0

# UVM 参数：禁用 UVM 页迁移（某些场景减少缺页中断）
# options nvidia-uvm NVreg_PageRetirementPolicy=0

# 禁止 nouveau 加载（双重保险）
blacklist nouveau
options nouveau modeset=0
EOF

# 创建模块加载顺序配置（顺序很重要）
cat > /etc/modules-load.d/nvidia.conf << 'EOF'
nvidia
nvidia-uvm
nvidia-modeset
nvidia-peermem
EOF

# 手动按顺序加载
modprobe nvidia
modprobe nvidia-uvm
modprobe nvidia-modeset
modprobe nvidia-peermem

# 验证所有模块已加载
lsmod | grep nvidia
# 期望输出：
# nvidia_peermem     16384  0
# nvidia_uvm       3452928  0
# nvidia_modeset   1654784  1 nvidia_peermem
# nvidia          71340032  2 nvidia_peermem,nvidia_uvm,nvidia_modeset
```

### 5.3 创建 UVM 设备节点（容器运行时需要）

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5（udev 规则两系统相同，均在 /etc/udev/rules.d/）
# 自动创建设备节点的 udev 规则（容器运行时需要，确保 /dev/nvidia* 节点在开机时创建）
KERNEL=="nvidia", RUN+="/bin/bash -c '/usr/bin/nvidia-smi -L && /usr/bin/nvidia-persistenced --user nvidia-persistenced --no-persistence-mode'"
KERNEL=="nvidia_uvm", RUN+="/bin/bash -c '/usr/bin/nvidia-modprobe -c 0 -u'"
KERNEL=="nvidia_modeset", RUN+="/bin/bash -c '/usr/bin/nvidia-modprobe -c 0 -m'"
EOF

udevadm control --reload-rules
udevadm trigger
```

---

## 6. 驱动升级完整 SOP

### 6.1 升级脚本（可复制执行）

```bash
#!/bin/bash
# /opt/scripts/upgrade-nvidia-driver.sh
# 环境: Ubuntu 22.04.5 (kernel 5.15) / Rocky 9.5 (kernel 5.14) / K8s 1.31
# NVIDIA 驱动升级 SOP（宿主机 runfile 安装方式，非 GPU Operator）
# 用法：bash /opt/scripts/upgrade-nvidia-driver.sh 570.124.04
# 注意：升级前必须确认新驱动版本与当前内核兼容（R550+ 支持 kernel 5.15/6.8；R535 编译 6.8 可能失败）

set -euo pipefail
NEW_VERSION="${1:?Usage: $0 <driver-version>}"
DRIVER_URL="https://us.download.nvidia.com/XFree86/Linux-x86_64/${NEW_VERSION}/NVIDIA-Linux-x86_64-${NEW_VERSION}.run"
LOG_FILE="/var/log/nvidia-driver-upgrade-$(date +%Y%m%d-%H%M%S).log"

echo "[$(date)] Starting NVIDIA driver upgrade to $NEW_VERSION" | tee -a "$LOG_FILE"

# === Step 1: 检查 K8s 节点并 cordon/drain ===
echo "[$(date)] Step 1: Cordon and drain K8s node..." | tee -a "$LOG_FILE"
NODE_NAME=$(hostname)
if command -v kubectl &>/dev/null; then
    kubectl cordon "$NODE_NAME" 2>/dev/null || echo "kubectl cordon skipped (not a K8s node or no kubeconfig)" | tee -a "$LOG_FILE"
    kubectl drain "$NODE_NAME" \
        --ignore-daemonsets \
        --delete-emptydir-data \
        --force \
        --grace-period=120 \
        --timeout=300s 2>/dev/null || echo "kubectl drain skipped or completed with warnings" | tee -a "$LOG_FILE"
fi

# === Step 2: 停止 GPU 相关服务 ===
echo "[$(date)] Step 2: Stopping GPU-related services..." | tee -a "$LOG_FILE"
systemctl stop nvidia-fabricmanager 2>/dev/null || true
systemctl stop nvidia-persistenced 2>/dev/null || true
systemctl stop dcgm 2>/dev/null || true
systemctl stop dcgm-exporter 2>/dev/null || true

# === Step 3: 检查 GPU 占用 ===
echo "[$(date)] Step 3: Checking GPU processes..." | tee -a "$LOG_FILE"
if nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null | grep -q .; then
    echo "WARNING: GPU processes still running:" | tee -a "$LOG_FILE"
    nvidia-smi --query-compute-apps=pid,process_name --format=csv | tee -a "$LOG_FILE"
    echo "Attempting to kill remaining processes..." | tee -a "$LOG_FILE"
    nvidia-smi --query-compute-apps=pid --format=csv,noheader | xargs -r kill -9 2>/dev/null || true
    sleep 5
fi

# 双重检查：lsof 和 fuser
fuser -v /dev/nvidia* 2>/dev/null || echo "No processes using /dev/nvidia*" | tee -a "$LOG_FILE"
lsof /dev/nvidia* 2>/dev/null | grep -v "^COMMAND" || echo "No open files on /dev/nvidia*" | tee -a "$LOG_FILE"

# === Step 4: 卸载当前驱动 ===
echo "[$(date)] Step 4: Unloading NVIDIA kernel modules..." | tee -a "$LOG_FILE"
rmmod nvidia_uvm 2>/dev/null || true
rmmod nvidia_drm 2>/dev/null || true
rmmod nvidia_modeset 2>/dev/null || true
rmmod nvidia_peermem 2>/dev/null || true
rmmod nvidia 2>/dev/null || true

# 验证模块已卸载
if lsmod | grep -q nvidia; then
    echo "ERROR: NVIDIA modules still loaded:" | tee -a "$LOG_FILE"
    lsmod | grep nvidia | tee -a "$LOG_FILE"
    echo "Cannot proceed. Manual intervention required." | tee -a "$LOG_FILE"
    exit 1
fi

# === Step 5: 卸载旧驱动（如通过 runfile 安装） ===
echo "[$(date)] Step 5: Uninstalling old driver..." | tee -a "$LOG_FILE"
if [ -x /usr/bin/nvidia-uninstall ]; then
    /usr/bin/nvidia-uninstall --silent 2>/dev/null || true
fi

# === Step 6: 下载并安装新驱动 ===
echo "[$(date)] Step 6: Downloading and installing driver $NEW_VERSION..." | tee -a "$LOG_FILE"
cd /tmp
wget -q --show-progress "$DRIVER_URL" -O "NVIDIA-Linux-x86_64-${NEW_VERSION}.run"
chmod +x "NVIDIA-Linux-x86_64-${NEW_VERSION}.run"
./"NVIDIA-Linux-x86_64-${NEW_VERSION}.run" \
    --silent \
    --dkms \
    --no-cc-version-check \
    --disable-nouveau 2>&1 | tee -a "$LOG_FILE"

# === Step 7: 加载新驱动模块 ===
echo "[$(date)] Step 7: Loading new kernel modules..." | tee -a "$LOG_FILE"
modprobe nvidia
modprobe nvidia-uvm
modprobe nvidia-modeset
modprobe nvidia-peermem

# === Step 8: 重启服务 ===
echo "[$(date)] Step 8: Restarting services..." | tee -a "$LOG_FILE"
systemctl start nvidia-persistenced
systemctl start nvidia-fabricmanager
sleep 10  # 等待 FM 初始化 NVSwitch
systemctl start dcgm 2>/dev/null || true
systemctl start dcgm-exporter 2>/dev/null || true

# === Step 9: 验证 ===
echo "[$(date)] Step 9: Verification..." | tee -a "$LOG_FILE"
INSTALLED_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
echo "Installed driver version: $INSTALLED_VERSION" | tee -a "$LOG_FILE"

if [ "$INSTALLED_VERSION" != "$NEW_VERSION" ]; then
    echo "ERROR: Version mismatch! Expected $NEW_VERSION, got $INSTALLED_VERSION" | tee -a "$LOG_FILE"
    exit 1
fi

# GPU 数量检查
GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader | head -1)
echo "GPU count: $GPU_COUNT" | tee -a "$LOG_FILE"

# 快速 dcgmi 检查
dcgmi diag -r 1 2>&1 | tee -a "$LOG_FILE"

# === Step 10: 恢复 K8s 节点 ===
echo "[$(date)] Step 10: Uncordon K8s node..." | tee -a "$LOG_FILE"
if command -v kubectl &>/dev/null; then
    kubectl uncordon "$NODE_NAME" 2>/dev/null || echo "kubectl uncordon skipped" | tee -a "$LOG_FILE"
fi

echo "[$(date)] Driver upgrade complete. Log saved to $LOG_FILE" | tee -a "$LOG_FILE"
```

---

## 7. 固件管理

### 7.1 VBIOS 查看与刷写

```bash
# 查看 VBIOS 版本
nvidia-smi -q | grep "VBIOS Version"
# 示例输出：VBIOS Version             : 96.00.89.00.01

# 查看所有 GPU VBIOS 版本
nvidia-smi --query-gpu=index,vbios_version --format=csv
# 输出：
# index, vbios_version
# 0, 96.00.89.00.01
# 1, 96.00.89.00.01
# ...

# 刷写 VBIOS（需 OEM 提供固件文件，风险操作！）
# 警告：刷写失败可能导致 GPU 变砖，需 OEM 硬件恢复
nvidia-smi -i 0 --update-gpu-vbios /path/to/vbios-update.rom
# 刷写后需重启节点生效
```

> **重要**：VBIOS 刷写通常只在 OEM 发布安全补丁或兼容性修复时进行，切勿随意刷写。每次刷写前与 OEM 确认固件版本适配性。

### 7.2 Mellanox 网卡固件管理（mlxfwmanager）

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5 / DOCA-OFED 24.10（MLNX_OFED 24.04-0.7.0.0 为末代旧名，24.10 起更名并入 DOCA）
# 安装 DOCA-OFED 工具包（包含 mlxfwmanager、mlxconfig、mst 等）
# 下载 DOCA-OFED：https://network.nvidia.com/products/infiniband-drivers/linux/mlnx_ofed/
# 注意：Rocky 与 Ubuntu 的 DOCA-OFED 安装包名相同，但需选择对应 OS 版本的 ISO/DEB/RPM 包

# 查看当前固件版本
mlxfwmanager
# 输出示例：
# Querying Mellanox devices firmware ...
# Device #1:
#     Device Type:      ConnectX7
#     Part Number:      MCX75510AAS-NE
#     Description:      ConnectX-7 VPI adapter card, NDR(400Gb/s) and 400GbE, single-port QSFP112, PCIe 5.0 x16
#     PSID:             MT_0000000612
#     PCI Device Name:  /dev/mst/mt41692_pciconf0
#     Base GUID:        946dae030012abcd
#     Firmware Version: 28.39.3004
#     FW Release Date:  11May2024
#     Status:           Up to date

# 在线检查固件更新
mlxfwmanager --online-query-psid MT_0000000612

# 升级固件（需提前下载 .bin 文件）
mlxfwmanager --image /opt/mellanox/fw-ConnectX7-rel-28_40_1000-All-DEBs-x86_64.tgz -u -f

# 刷写后重启网卡（不重启服务器）
mlxfwreset -d /dev/mst/mt41692_pciconf0 reset -y

# 验证新固件版本
mlxfwmanager
```

### 7.3 BMC/IPMI 常用命令

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5（ipmitool 两系统均有包，apt/dnf 包名同为 ipmitool）
# 远程 IPMI 操作（从管理节点对 gpu-node-01 的 BMC 操作）
BMC_USER="admin"
BMC_PASS="calvin"

# 查看所有传感器数据（温度、电压、风扇）
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" sensor list
# 关注：GPU Temp（应 <85°C）、Inlet Temp（应 <35°C）、Fan（RPM > 基线 60%）

# 查看系统事件日志（SEL）
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" sel list
# 关注：ECC、Thermal Trip、Power Supply 相关事件

# 清空 SEL（归档后）
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" sel clear

# Serial Over LAN（SOL）远程查看控制台输出
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" sol activate
# Ctrl+] 退出 SOL

# 远程电源操作
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" power status
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" power on
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" power off
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" power cycle
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" power reset

# 查看 FRU（设备资产信息）
ipmitool -I lanplus -H "$BMC_IP" -U "$BMC_USER" -P "$BMC_PASS" fru print
```

### 7.4 NVSwitch 固件与 FM 配套关系

| NVSwitch 代次 | 适用机型 | FM 版本要求 | 固件升级方式 |
|-------------|---------|-----------|------------|
| NVSwitch Gen1（V100 时代） | DGX-2 | 不单独提供 FM | 随系统固件整体升级 |
| NVSwitch Gen2（A100） | DGX A100 / HGX A100 | FM 版本 = 驱动版本 | 通过 NVIDIA FM 包升级 |
| NVSwitch Gen3（H100） | DGX H100 / HGX H100 | FM 版本 = 驱动版本 | 通过 NVIDIA FM 包升级 |
| NVSwitch Gen4（B200/GB200）| GB200 NVL72 | FM 版本 = 驱动版本 | 通过 NVIDIA FM 包升级 |

**升级原则**：NVSwitch 固件随 FM 包一起升级，FM 版本必须与驱动版本一致，三者（驱动、FM、NVSwitch 固件）同步升级。

---

## 8. nvidia-container-toolkit

### 8.1 安装

```bash
# 环境: Ubuntu 22.04.5 (systemd 249) / Rocky 9.5 (systemd 252)
# 基线版本：nvidia-container-toolkit v1.17.4；apt/dnf 仓库安装，两系统仓库地址不同
# Ubuntu/Debian（使用 NVIDIA 仓库）
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y nvidia-container-toolkit  # v1.17.4（apt-cache madison 查询当期可用版本）

# Rocky/RHEL（仓库地址与 Ubuntu 不同，注意！）
dnf config-manager --add-repo https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
dnf install -y nvidia-container-toolkit  # v1.17.4

# 验证安装
nvidia-ctk --version
# 输出：NVIDIA Container Toolkit CLI version 1.17.4
```

### 8.2 containerd config.toml 配置

```bash
# 环境: containerd 1.7.x（apt/dnf 源）/ nvidia-container-toolkit v1.17.4
# 注意：containerd 2.0 配置结构与 1.7 有差异（升级会覆盖 runtime 配置，经典坑），升级前备份 config.toml
# 使用 nvidia-ctk 自动配置 containerd runtime
nvidia-ctk runtime configure --runtime=containerd

# 上述命令会修改 /etc/containerd/config.toml，添加如下内容：
# 手动配置参考（如自动配置不满足需求）：
cat >> /etc/containerd/config.toml << 'EOF'

# === NVIDIA Container Runtime ===
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  privileged_without_host_devices = false
  runtime_engine = ""
  runtime_root = ""
  runtime_type = "io.containerd.runc.v2"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true

# 可选：将 nvidia 设为默认 runtime
# [plugins."io.containerd.grpc.v1.cri".containerd]
#   default_runtime_name = "nvidia"
EOF

# 重启 containerd
systemctl restart containerd

# 验证 runtime 已注册
crictl info 2>/dev/null | jq '.config.containerd.runtimes | keys'
# 应包含 "nvidia"
```

### 8.3 Docker daemon.json 配置

# 环境: Docker/containerd + nvidia-container-toolkit v1.17.4 / CUDA 12.8（R570 驱动）
# 使用 nvidia-ctk 自动配置 Docker
nvidia-ctk runtime configure --runtime=docker

# 或手动编辑 /etc/docker/daemon.json
cat > /etc/docker/daemon.json << 'EOF'
{
  "default-runtime": "nvidia",
  "runtimes": {
    "nvidia": {
      "path": "/usr/bin/nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "exec-opts": ["native.cgroupdriver=systemd"],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
EOF

systemctl restart docker

# 验证
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
```text

### 8.4 CDI 模式简介

**CDI（Container Device Interface）** 是一种标准化的设备注入规范，替代传统的 `--gpus all` 方式，提供更精细的设备控制。

```bash
# 环境: nvidia-container-toolkit v1.17.4 / containerd 1.7.x / CDI 需要 containerd 1.7+ 或 Docker 24+
# 生成 CDI 规范文件

# 查看生成的 CDI 规范
cat /etc/cdi/nvidia.yaml
# 包含每个 GPU 的设备节点、挂载点、环境变量定义

# 使用 CDI 规范启动容器（containerd）
ctr run --rm \
    --device nvidia.com/gpu=all \
    docker.io/library/ubuntu:22.04 \
    test-cdi \
    nvidia-smi

# 指定特定 GPU
ctr run --rm \
    --device nvidia.com/gpu=0 \
    --device nvidia.com/gpu=1 \
    docker.io/library/ubuntu:22.04 \
    test-cdi-2gpu \
    nvidia-smi

# K8s 中使用 CDI（K8s 1.28+，需开启 DevicePlugins CDI 特性）
# 在 kubelet 配置中启用：
# featureGates:
#   DevicePluginCDIDevices: true
```text

### 8.5 验证命令

```bash
# 环境: nvidia-container-toolkit v1.17.4 / Docker 或 containerd / CUDA 12.8 镜像（R570 驱动）
# 镜像 tag 格式：nvidia/cuda:<cuda-ver>-<base|devel>-ubuntu22.04；禁用 latest
# 基础验证：nvidia-smi 在容器内可用

# 验证 CUDA 计算能力
docker run --rm --gpus all nvidia/cuda:12.8.1-devel-ubuntu22.04 \
    /usr/local/cuda/extras/demo_suite/deviceQuery

# 验证所有 GPU 可见
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 \
    bash -c "nvidia-smi --query-gpu=index,name,memory.total --format=csv"

# 验证指定 GPU（只看 GPU 0 和 1）
docker run --rm --gpus '"device=0,1"' nvidia/cuda:12.8.1-base-ubuntu22.04 \
    nvidia-smi

# 验证 GPU 内存可访问（运行简单 CUDA 程序）
docker run --rm --gpus all nvidia/cuda:12.8.1-devel-ubuntu22.04 \
    bash -c 'cat > /tmp/test.cu << "CUDA_EOF"
#include <stdio.h>
__global__ void hello() { printf("Hello from GPU %d\\n", blockIdx.x); }
int main() { hello<<<8,1>>>(); cudaDeviceSynchronize(); return 0; }
CUDA_EOF
nvcc /tmp/test.cu -o /tmp/test && /tmp/test'
```text

---

## 9. nvidia-smi 命令大全

### 9.1 nvidia-smi -q（全量查询）

```bash
# 环境: driver R570 570.124.04 / nvidia-smi 随驱动版本（570.124.04），命令语法 R570 与 R550 无差异
# 完整查询（输出所有信息）
nvidia-smi -q

# 按 section 过滤查询
nvidia-smi -q -d PERFORMANCE     # 性能状态（P-State、频率）
nvidia-smi -q -d MEMORY          # 显存使用
nvidia-smi -q -d ECC             # ECC 状态与错误计数
nvidia-smi -q -d TEMPERATURE     # 温度
nvidia-smi -q -d POWER           # 功耗与功率限制
nvidia-smi -q -d CLOCK           # 频率与降频原因
nvidia-smi -q -d COMPUTE         # 计算模式
nvidia-smi -q -d RETIRED_PAGES   # 退役页面
nvidia-smi -q -d ROW_REMAPPER    # 行重映射状态
nvidia-smi -q -d PCIE            # PCIe 链路状态
nvidia-smi -q -d NVLINK          # NVLink 状态
```text

**各 section 关键判读**：

| Section | 关键字段 | 判读要点 |
|---------|---------|---------|
| PERFORMANCE | P-State（P0=最高性能，P8=空闲） | 训练时 GPU 应持续 P0 |
| MEMORY | FB Memory Usage / BAR1 Memory | FB 使用率持续 >95% 可能 OOM |
| ECC | Single Bit / Double Bit Volatile | DBE >0 立即报修 |
| TEMPERATURE | GPU Current Temp / GPU Shutdown Temp | 接近 Shutdown Temp 需检查散热 |
| POWER | Power Draw / Power Limit / Enforced Power Limit | Power Draw 持续达到 Power Limit 可能触发降频 |
| CLOCK | Graphics/SM/Video/Energy Clocks + Clocks Event Reasons | HW Slowdown=Yes 需排查供电/散热 |
| RETIRED_PAGES | SBE / DBE Retired Pages | DBE >0 报修 |
| ROW_REMAPPER | Pending / Remapping Failure Occurred | Failure=True 需关注 |
| PCIE | Link Gen / Link Width | 必须与 LnkCap 一致 |

### 9.2 nvidia-smi dmon（设备监控，持续输出）

```bash
# 每秒刷新，显示 GPU 关键指标
nvidia-smi dmon -s pucvmet -d 1
# 参数：-s 选择指标（p=power, u=utilization, c=clocks, v=voltage, m=memory, t=temperature, e=errors）
# -d 1 = 每秒刷新

# 输出示例
# #gpu   pwr  gtemp  mtemp    sm   mem   enc   dec  mclk  pclk  rx_util  tx_util
#    0   345      72      -  1830   877     0     0  1593  1830    31245    31245
#    1   342      71      -  1830   877     0     0  1593  1830    31098    31098
# ...

# 判读：
# pwr：接近 TDP（A100=400W, H100=700W）说明 GPU 计算满载
# gtemp：<85°C 正常（A100），<88°C 正常（H100）
# sm：SM Clock，训练时应维持 Boost 频率
# rx_util/tx_util：NVLink 收发带宽（MB/s），AllReduce 时应接近峰值
```text

### 9.3 nvidia-smi pmon（进程监控）

```bash
# 显示每个 GPU 上运行的进程及其资源占用
nvidia-smi pmon -d 1
# 输出示例
# # gpu     pid  type    sm   mem   enc   dec   command
#    0   12345     C    95    67     0     0   python
#    1   12345     C    94    65     0     0   python
#    2   12345     C    96    68     0     0   python
# ...

# 判读：
# type: C=Compute, G=Graphics, C+G=混合
# sm/mem：SM 和显存控制器利用率（%）
# 训练时 sm 应 >80%，如 <50% 说明 GPU 饥饿（数据供给不足）
```text

### 9.4 功率限制（-pl）

```bash
# 环境: driver R570 570.124.04；功率限制命令对所有 GPU 代际通用，但 OEM 可能锁定上限
# 查看当前功率限制

# 设置功率限制（A100 限制到 350W，降低功耗但性能约降 10%）
nvidia-smi -pl 350  # 所有 GPU
nvidia-smi -i 0 -pl 350  # 仅 GPU 0

# 恢复默认功率限制（A100 默认 400W）
nvidia-smi -pl 400

# 注意：需要 root 权限；某些 OEM 锁定了功率上限，无法超过出厂设置
```text

### 9.5 锁频（-lgc）

```bash
# 锁定 GPU SM 频率到固定值（消除频率波动，测试基准性能用）
nvidia-smi -lgc 1410  # A100 锁定到 1410 MHz（接近 Boost 频率）

# 锁定到频率范围
nvidia-smi -lgc 1200,1410  # 最小 1200 MHz，最大 1410 MHz

# 锁定显存频率
nvidia-smi -lmc 1593  # A100 显存锁定到 1593 MHz

# 解除锁频
nvidia-smi -rgc  # 解除 SM 频率锁定
nvidia-smi -rmc  # 解除显存频率锁定
```text

### 9.6 MIG 子命令

```bash
# 环境: driver R570 570.124.04；MIG 需 A100/H100 SXM 卡，R470+ 驱动支持
# A100 80GB 支持的 MIG 配置文件：1g.10gb、2g.20gb、3g.40gb、4g.40gb、7g.80gb
# 查看 MIG 状态
nvidia-smi mig -lgi  # 列出 GPU Instance

# 创建 MIG 实例（A100 80GB 支持：1g.10gb、2g.20gb、3g.40gb、4g.40gb、7g.80gb）
nvidia-smi -i 0 -mig 1  # 先启用 MIG 模式（需重启或重置 GPU）
nvidia-smi -i 0 -cgi 7g.80gb  # 创建 1 个 7g.80gb 实例（使用全部资源）
nvidia-smi -i 0 -cgi 3g.40gb,3g.40gb  # 创建 2 个 3g.40gb 实例

# 删除 MIG 实例
nvidia-smi -i 0 -dgi -gi 0  # 删除 GPU Instance 0

# 禁用 MIG 模式
nvidia-smi -i 0 -mig 0  # 需重置 GPU 或重启
nvidia-smi -i 0 -r  # 重置 GPU
```text

### 9.7 nvlink 子命令

```bash
# 查看 NVLink 状态
nvidia-smi nvlink --status
# 输出示例（A100 SXM 满配）：
# GPU 0: A100-SXM4-80GB
#   Link 0: 50 GB/s
#   Link 1: 50 GB/s
#   ...（共 12 条 NVLink，每条 50 GB/s，总计 600 GB/s 双向）

# 查看 NVLink 错误计数
nvidia-smi nvlink -e
# 期望：所有错误计数为 0
# 非零说明 NVLink 链路有问题

# 查看 NVLink 能力
nvidia-smi nvlink --capabilities
# 确认 P2P、SYS 原子操作等能力均 Enabled

# 重置 NVLink 错误计数
nvidia-smi nvlink --error --reset
```text

### 9.8 topo（拓扑）

```bash
# 查看 GPU 互联拓扑矩阵
nvidia-smi topo -m

# 查看 GPU 与 CPU NUMA 亲和性
nvidia-smi topo -gpuname

# 查看 PCIe 路径
nvidia-smi topo -p 2  # GPU 0 和 GPU 2 之间的 PCIe 路径

# 基于拓扑推荐 CUDA 可见设备（NUMA 对齐）
nvidia-smi topo -c 0  # 与 GPU 0 同 NUMA 的 CPU 核
```text

### 9.9 GPU 重置（-r）

```bash
# 重置 GPU（用于恢复 XID 错误后的 GPU 状态）
nvidia-smi -i 0 -r

# 注意：
# - 重置前必须确保无进程在使用该 GPU
# - SXM 版 GPU 在某些 XID 错误后需要整节点重启，单卡 -r 不一定能恢复
# - 重置后需重新加载 CUDA 上下文
```text

---

## 10. 常见安装故障排查表

| 故障现象 | 原因分析 | 解决命令 |
|---------|---------|---------|
| `nvidia-smi` 报 `Failed to initialize NVML: Driver/library version mismatch` | 内核模块版本与用户态库版本不一致（常见于 apt/yum 升级后未重启） | `reboot`，或 `rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia && modprobe nvidia` |
| `nvidia-smi` 报 `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver` | 驱动模块未加载，或 nouveau 未正确禁用 | 检查 `lsmod \| grep nouveau`，确认黑名单生效后重启 |
| 安装时报 `ERROR: Unable to find the kernel source tree` | 内核头文件未安装或版本不匹配 | Ubuntu: `apt-get install -y linux-headers-$(uname -r)`；RHEL: `dnf install -y kernel-devel-$(uname -r)` |
| 安装时报 `gcc version mismatch` | 编译内核模块的 gcc 版本与编译内核的 gcc 版本不同 | `export CC=gcc-version`，或 `--no-cc-version-check` 参数（不推荐长期使用） |
| Secure Boot 下 `nvidia-smi` 无输出，`dmesg` 报 `module verification failed` | 内核模块未签名，Secure Boot 拒绝加载 | 方案 1：关闭 Secure Boot；方案 2：用 MOK 密钥签名模块（`mokutil --import`） |
| `nvidia-smi` 报 `Failed to initialize NVML: Unknown Error`，dmesg 有 `NVRM: GPU has fallen off the bus` | GPU 硬件故障或 PCIe 链路丢失 | 1. 重新插拔 GPU；2. 换插槽测试；3. 如复现则 GPU 硬件故障，报修 |
| 安装时报 `ERROR: The Nouveau kernel driver is currently in use` | nouveau 仍在运行 | `echo "blacklist nouveau" >> /etc/modprobe.d/blacklist.conf` → `update-initramfs -u` → `reboot` → `rmmod nouveau` |
| 驱动装完 `nvidia-smi` 正常，但 CUDA 程序报 `cudaErrorNoDevice` | UVM 模块未加载 | `modprobe nvidia-uvm`，添加到 `/etc/modules-load.d/nvidia.conf` |
| 容器内 `nvidia-smi` 报 `Failed to initialize NVML` | nvidia-container-toolkit 未安装或配置错误 | 重装 toolkit，运行 `nvidia-ctk runtime configure --runtime=containerd`，重启 containerd |
| GSP 固件相关：`dmesg` 报 `NVRM: GSP firmware load failed` | GSP（GPU System Processor）固件损坏或版本不匹配；**R515+ 默认启用 GSP**，旧驱动不涉及此问题 | 1. 禁用 GSP：`echo "options nvidia NVreg_EnableGpuFirmware=0" >> /etc/modprobe.d/nvidia.conf` → 重启；2. 或重新安装驱动 |
| 多 GPU 节点 `nvidia-smi` 只显示部分 GPU | Above 4G Decoding 未启用，BAR 空间不足 | 进入 BIOS 启用 Above 4G Decoding，保存重启 |
| 驱动安装后 reboot 回退到旧版本 | DKMS 未正确注册，或 apt/yum 的 post-install 脚本失败 | `dkms status` 检查，`dkms install nvidia/<version> -k $(uname -r)` 手动安装 |
| NCCL 报 `NCCL WARN NET/Socket : No interface found` | `NCCL_SOCKET_IFNAME` 未设置或网卡名错误 | `export NCCL_SOCKET_IFNAME=bond0`（或实际业务网卡名），通过 `ip link show` 确认网卡名 |
| GPU Operator Pod CrashLoopBackOff，日志报 `driver version mismatch` | 容器化驱动版本与宿主已安装驱动冲突 | 卸载宿主驱动后再装 GPU Operator，或 `helm` values 中 `driver.usePrecompiled=true` |

---

## 附录：快速故障诊断命令集

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5 / driver R570 570.124.04
# 一键诊断脚本，接手任何 GPU 节点先跑这个，确认驱动/FM/persistenced/container-toolkit 状态
#!/bin/bash
echo "=== NVIDIA Driver Status ==="
nvidia-smi 2>&1 || echo "nvidia-smi failed"

echo -e "\n=== Kernel Modules ==="
lsmod | grep nvidia

echo -e "\n=== dmesg (NVIDIA related, last 20 lines) ==="
dmesg | grep -i nvidia | tail -20

echo -e "\n=== PCIe Link Status ==="
for dev in $(lspci | grep -i nvidia | awk '{print $1}'); do
    echo "--- $dev ---"
    lspci -vvv -s "$dev" | grep -E "LnkSta:"
done

echo -e "\n=== Fabric Manager ==="
systemctl status nvidia-fabricmanager --no-pager 2>/dev/null || echo "FM not installed"

echo -e "\n=== Persistenced ==="
systemctl status nvidia-persistenced --no-pager 2>/dev/null || echo "persistenced not running"

echo -e "\n=== Container Toolkit ==="
nvidia-ctk --version 2>/dev/null || echo "nvidia-ctk not installed"

echo -e "\n=== XID Errors (last 10) ==="
dmesg | grep -i "xid" | tail -10
journalctl -k | grep -i "xid" | tail -10
```text
