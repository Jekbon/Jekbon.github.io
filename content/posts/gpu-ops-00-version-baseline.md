---
title: "GPU 集群运维（00）：全系列版本基线"
date: 2026-11-06T09:00:00+08:00
draft: false
description: "全部 12 个分册的版本引用源：OS 与内核、NVIDIA 驱动与 CUDA、Kubernetes、网络、存储、监控、自动化工具的基线版本，硬件代际对应关系，版本钉死方法，以及接手任何机器先跑的现场核查清单。"
tags: ["GPU", "版本管理", "基线"]
series: "GPU 集群运维"
---

---

## 0. 基线总原则

1. **双 OS 基线**：全系列以 Ubuntu 22.04 LTS 为主基线，Rocky Linux 9.x 为并列基线（命令差异处两者都给）
2. **生产分支优先**：NVIDIA 驱动只用 Production Branch，不用 New Feature Branch
3. **版本三要素**：每条关键命令/配置标注 `软件版本 + OS/内核 + 硬件代际` 三要素
4. **示例版本号规则**：标 `（示例）` 的 build 号是真实存在过的版本，用于说明格式；实际部署以官方兼容矩阵当期推荐为准
5. **查版本命令**：任何机器上接手先跑第 10 节的版本核查清单，别信文档信现场

---

## 1. 操作系统与内核基线

| 项目 | Ubuntu 基线 | Rocky/RHEL 基线 |
|---|---|---|
| OS 版本 | Ubuntu Server 22.04.5 LTS (Jammy) | Rocky Linux 9.5 |
| 内核（GA） | 5.15.0-xxx-generic | 5.14.0-503.xxx.el9_5 |
| 内核（HWE，可选） | 6.8.0-xxx-generic (linux-generic-hwe-22.04) | 不支持（RHEL 系内核锁定） |
| glibc | 2.35 | 2.34 |
| gcc | 11.4（可装 12/13） | 11.5 |
| systemd | 249 | 252 |
| 容器运行时 | containerd 1.7.x（apt 源） | containerd 1.7.x（dnf 源） |
| Python（系统） | 3.10 | 3.9 |
| 备选新 OS | Ubuntu 24.04.1 LTS（内核 6.8） | Rocky 9.x 系 |

**版本敏感点：**
- 内核 ≥6.x 时，NVIDIA 驱动必须用支持该内核的分支（R550+ 对 6.x 支持较好；R535 编译 6.8 内核可能失败）
- DKMS 依赖：Ubuntu 装 `linux-headers-$(uname -r)`，Rocky 装 `kernel-devel-$(uname -r) kernel-headers-$(uname -r)`，**版本必须与运行内核完全一致**
- Ubuntu 22.04 apt 源里 fio=3.28、chrony=4.2；Rocky 9 里 fio=3.26；要新特性需源码编译（分册中给编译方法）
- SELinux：Rocky 默认 enforcing，GPU 容器需要额外策略或用 permissive 过渡；Ubuntu 用 AppArmor

---

## 2. GPU 软件栈基线（NVIDIA）

### 2.1 驱动与 CUDA

| 组件 | 主基线（Hopper/Blackwell） | 兼容基线（Ampere 存量集群） | 说明 |
|---|---|---|---|
| NVIDIA Driver | **R570 生产分支，570.124.04（示例）** | **R550 生产分支，550.127.08（示例）** | 数据中心卡用 Data Center/Tesla 驱动 |
| CUDA Toolkit | 12.8 | 12.4 | 容器内自带，宿主机只出驱动 |
| Fabric Manager | **与驱动完全同版本**（570.124.04 / 550.127.08） | 同左 | HGX 机型必须；版本不匹配 = GPU 不可用 |
| nvidia-persistenced | 随驱动同版本 | 同左 | |
| GSP 固件 | 随驱动打包 | 同左 | R515+ 默认启用 GSP；`NVreg_EnableGpuFirmware=0` 可禁用 |

**驱动 ↔ CUDA 兼容矩阵（Linux x86_64，最低驱动要求）：**

| CUDA Toolkit | 最低驱动版本 |
|---|---|
| 12.8 | ≥570.26 |
| 12.6 | ≥560.28.03 |
| 12.4 | ≥550.54.14 |
| 12.2 | ≥535.86.05 |
| 11.8 | ≥520.61.05（或 450.80.02 + forward compat） |

规则：**驱动向下兼容旧 CUDA**（新驱动可跑旧 CUDA 镜像），反向不行。集群内驱动版本必须全一致。

### 2.2 CUDA 生态库

| 组件 | 基线版本 | 配套关系 |
|---|---|---|
| cuDNN | 9.8.x（for CUDA 12） | 与 PyTorch 绑定，容器内自带 |
| NCCL | 2.26.x（CUDA 12.8）/ 2.21.x（CUDA 12.4） | 跨机通信；同作业各 rank 版本必须一致 |
| TensorRT | 10.8.x | 推理节点 |
| Triton Inference Server | NGC 24.12（3.53.0） | 推理服务 |
| CUTLASS / nvbandwidth | git master（按 commit 钉） | 测试工具 |

### 2.3 容器与 K8s GPU 组件

| 组件 | 基线版本（示例 tag） | 备注 |
|---|---|---|
| nvidia-container-toolkit | v1.17.4 | apt/dnf 仓库 `nvidia-container-toolkit` |
| NVIDIA GPU Operator | Helm chart **v24.9.2** | 支持 K8s 1.29~1.32；内含以下组件 |
| ├─ driver container | 570.124.04-ubuntu22.04（示例） | values 里 `driver.version` 钉死 |
| ├─ k8s-device-plugin | v0.17.0 | |
| ├─ dcgm-exporter | 4.1.0-4.2.0-ubuntu22.04（示例 tag） | |
| ├─ gpu-feature-discovery | v0.17.0 | |
| ├─ mig-manager | v0.8.0 | |
| └─ node-feature-discovery | v0.16.x | |
| DCGM（宿主机版） | 3.3.9 / 4.2.x | `datacenter-gpu-manager` 包 |
| HAMi（异构共享） | v2.5.x | 国内多租户场景 |

### 2.4 AI 框架参考版本（运维对接算法团队时的口径）

| 组件 | 基线版本 | 备注 |
|---|---|---|
| PyTorch | 2.6.0（cu128 轮子）或 NGC `pytorch:25.01-py3` | NGC 容器 tag 格式 = YY.MM |
| NGC PyTorch 容器 | `nvcr.io/nvidia/pytorch:24.12-py3`（示例） | 内含 CUDA 12.6 + PyTorch 2.5 + NCCL 2.23 |
| DeepSpeed | 0.16.x | |
| Megatron-LM | 按 commit 钉（core_r0.9.0 等分支） | 无正式 release |
| vLLM | 0.7.x | 推理 |
| torchrun | 随 PyTorch | elastic 参数 2.x 后稳定 |

---

## 3. Kubernetes 栈基线

| 组件 | 基线版本 | 版本敏感点 |
|---|---|---|
| Kubernetes | **1.31.x**（支持范围 1.29~1.32） | API 废弃：1.32 移除 flowcontrol v1beta2 等 |
| kubelet/kubeadm | 与 K8s 同版本 | 版本偏差 ≤1 minor |
| containerd | 1.7.x LTS（或 2.0.x） | 2.0 配置结构与 1.7 有差异；升级会覆盖 runtime 配置（经典坑） |
| runc | 1.1.x / 1.2.x | |
| etcd | 3.5.15+ | K8s 1.31 配套；必须 NVMe 独立盘 |
| Helm | 3.16.x | |
| CNI（Calico） | v3.28.x | 或 Cilium 1.16.x |
| multus-cni | v4.0.2 | |
| sriov-network-operator | v4.7.x | IB 网卡接入训练 Pod |
| Volcano | v1.10.x | gang/queue；PyTorchJob 支持自 v1.4+ 稳定 |
| Kueue | v0.9.x | ClusterQueue/LocalQueue API v1beta1 |
| Kubeflow Training Operator | v1.8.x | PyTorchJob v1；JobSet 集成 |
| Kube-prometheus-stack chart | v66.x（示例） | 内含 Prometheus 2.55 LTS |
| ArgoCD | v2.13.x | GitOps |
| Kyverno | v1.13.x | 准入策略（分册 11 用） |
| Gatekeeper | v3.17.x | 备选准入方案 |
| Cert-manager | v1.16.x | |

---

## 4. 网络栈基线（IB/RoCE）

| 组件 | 基线版本 | 说明 |
|---|---|---|
| **MLNX_OFED / DOCA-OFED** | **DOCA-OFED 24.10**（MLNX_OFED 24.04-0.7.0.0 为末代旧名） | 24.10 起 NVIDIA 将 OFED 更名并入 DOCA；23.10 是长维护版 |
| 内核态驱动 | mlx5_core（随 OFED 或发行版 inbox） | inbox vs OFED 驱动功能差异见分册 05 |
| ConnectX-7 固件 | 28.39.xxxx（示例） | `mlxfwmanager --query` 核对 |
| ConnectX-6 Dx 固件 | 22.39.xxxx（示例） | |
| infiniband-diags | 随 OFED（ibstat/perfquery/iblinkinfo 等） | apt `infiniband-diags` 版本较旧，生产装 OFED 版 |
| perftest | 24.04.x（ib_write_bw 等） | |
| OpenSM | 随 OFED 发布 | `opensm -v` 查看；主备 priority 配置 |
| MFT 工具（mlxconfig/mlxfwmanager） | 4.29.x（示例） | |
| IB 交换机 | Quantum-2 QM9700（NDR 400G）/ QM8700（HDR 200G） | 固件 3.10.xxxx / 3.9.xxxx（示例），`ibswitchinfo` 查看 |
| RoCE 交换机 | 华为 CE8850/CE6857（V200R022 示例）或 NVIDIA Spectrum-4 SN5000（4.x） | PFC/ECN 配置语法按厂商版本 |
| GPUDirect RDMA | nvidia-peermem（驱动 R465+ 内置，替代旧 nv_peer_mem） | 内核 ≥5.13 也可用 dmabuf 路径 |

---

## 5. 存储栈基线

| 组件 | 基线版本 | 说明 |
|---|---|---|
| Lustre 服务端/客户端 | 2.15.x LTS（DDN 客户端按 EX 版本） | 客户端内核模块需与本机内核匹配编译 |
| JuiceFS 社区版 | 1.2.x | `juicefs version` |
| JuiceFS CSI Driver | 0.24.x | helm chart juicefs-csi-driver-0.24.x |
| Fluid | v1.0.x | Dataset/Runtime CRD v1alpha1 |
| Alluxio | 2.9.x（社区版停止大更新，企业版 Alluxio 3.x） | |
| Ceph（如用） | Reef 18.2.x / Squid 19.2.x | |
| MinIO | RELEASE.2024-xx（按 tag 钉） | |
| Dragonfly | v2.1x（dfget/dfdaemon） | P2P 分发 |
| fio | 源码编译 3.36+（发行版自带 3.26~3.28 偏旧） | |
| 本地缓存盘 | NVMe，ext4/xfs；建议 xfs（大文件） | mkfs 参数见分册 04 |

---

## 6. 监控栈基线

| 组件 | 基线版本 | 说明 |
|---|---|---|
| Prometheus | 2.55.x LTS（3.x 已发布，UTF-8/新特性；生产建议先 2.55 LTS） | PromQL 两版兼容 |
| Alertmanager | 0.27.x | |
| Grafana | 11.x | NVIDIA DCGM 官方 dashboard ID 12239（旧版指标名）需按 dcgm-exporter 版本核对 |
| dcgm-exporter | 3.3.9-3.6.0 / 4.x | **指标名前缀变化**：3.x `DCGM_FI_*`；4.x 部分重命名（见分册 06） |
| Thanos | 0.37.x | 长期存储/全局查询 |
| VictoriaMetrics | v1.10x（单节点或 cluster 版） | Thanos 替代方案 |
| Loki | 3.x（2.9 LTS 亦可） | |
| promtail | 3.x（已被 Alloy 取代中，存量仍主流） | |
| Grafana Alloy | 1.x | promtail/agent 的继任者 |
| snmp_exporter | 0.26.x | IB 交换机采集 |
| node-exporter | 1.8.x | |
| kube-state-metrics | 2.13.x | |

---

## 7. 自动化工具基线

| 组件 | 基线版本 |
|---|---|
| Ansible | ansible-core 2.17.x（ansible 10 套件） |
| pdsh / clush | pdsh 2.34 / clustershell 1.9 |
| Trivy | 0.58.x |
| cosign | 2.4.x |
| etcdctl | 3.5.x（必须 `ETCDCTL_API=3`，随 etcd 版本） |
| kubectl | 与集群版本偏差 ≤1 minor |
| git | 2.34（Ubuntu 22.04）/ 2.43（Rocky 9.5） |

---

## 8. 硬件代际与软件要求的对应关系

| GPU 代际 | 代表卡 | 最低驱动分支 | CUDA | 备注 |
|---|---|---|---|---|
| Ampere | A100/A800/A30 | R470+（建议 R535/R550） | 11.x+ | MIG 自 A100 始 |
| Hopper | H100/H800/H20 | **R535+（建议 R550/R570）** | **12.x 必须** | 需 Fabric Manager；GSP 默认启用 |
| Blackwell | B200/GB200 | **R570+ 必须** | **12.8+ 必须** | NVL72 需配套 NVLink Switch 固件 |
| 昇腾 | Atlas 910B | CANN 8.0.x + 配套固件 | - | npu-smi 工具链独立 |

**规则：卡越新，驱动/CUDA 下限越高；同集群混代际时，驱动取"满足最新卡的最低分支"并保持全集群一致。**

---

## 9. 版本钉死方法（怎么把版本固化下来）

```bash
# apt：装指定版本并锁定
apt-get install -y nvidia-driver-570-server=570.124.04-0ubuntu1  # 版本号以 apt-cache madison 查询为准
apt-mark hold nvidia-driver-570-server

# dnf：版本锁定
dnf install -y nvidia-driver-570.124.04-1
dnf versionlock add nvidia-driver

# helm：chart 版本钉死
helm install gpu-operator nvidia/gpu-operator --version v24.9.2 -f values.yaml

# 容器镜像：永远用具体 tag 或 digest，禁用 latest
# nvcr.io/nvidia/pytorch:24.12-py3
# registry.k8s.io/pause@sha256:...（digest 最稳）

# git 工具：clone 后 checkout tag/commit
git clone https://github.com/NVIDIA/nccl-tests.git && cd nccl-tests && git checkout <commit>

# pip：requirements.txt 全量 == 钉版本，配 pip freeze 审计
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu128
```

**查询可用版本的命令：**
```bash
apt-cache madison nvidia-driver-570-server   # Ubuntu 可装版本列表
dnf --showduplicates list nvidia-driver       # Rocky
helm search repo nvidia/gpu-operator --versions
```

---

## 10. 现场版本核查清单（接手任何机器先跑这个）

```bash
# ---- OS 与内核 ----
cat /etc/os-release | head -2          # OS 版本
uname -r                               # 运行内核
dpkg -l linux-image-$(uname -r) 2>/dev/null || rpm -q kernel-$(uname -r)  # 内核包一致性
rpm -q kernel-devel kernel-headers 2>/dev/null   # Rocky：DKMS 依赖是否同版本

# ---- GPU 驱动与固件 ----
nvidia-smi | head -4                   # 驱动版本 + CUDA 上限（注意：是驱动支持的最高 CUDA，不是已装 CUDA）
cat /proc/driver/nvidia/version        # 驱动编译信息（内核版本、gcc）
nvidia-smi -q | grep -iE 'VBIOS|Inforom'   # VBIOS/InfoROM 版本
nvidia-smi -q | grep -i 'GSP Firmware Version'  # GSP 固件版本（R515+）
systemctl status nvidia-fabricmanager | head -3  # FM 版本必须=驱动版本
modinfo nvidia | grep ^version         # 内核模块版本

# ---- CUDA 生态（容器内或宿主机 toolkit）----
nvcc --version                         # CUDA Toolkit 版本
dpkg -l | grep -E 'libcudnn|libnccl'   # cuDNN/NCCL 包版本
python3 -c "import torch; print(torch.__version__, torch.version.cuda, torch.backends.cudnn.version())"

# ---- 容器栈 ----
containerd --version ; runc --version
nvidia-container-toolkit --version 2>/dev/null || dpkg -l nvidia-container-toolkit
crictl info | grep -i runtime          # nvidia runtime 是否注册

# ---- K8s ----
kubectl version                        # server/client 版本
kubeadm version -o short 2>/dev/null
etcdctl version                        # ETCDCTL_API=3
helm version --short

# ---- 网络 ----
ofed_info -s                           # MLNX_OFED/DOCA-OFED 版本（关键！）
ibstat | grep -iE 'Firmware|Rate'      # HCA 固件与协商速率
mlxfwmanager --query | grep -E 'PSID|FW'  # 固件详情
ethtool -i <ifname> | grep firmware    # RoCE 网卡固件
rpm -qa | grep -E 'infiniband-diags|perftest|mft'

# ---- 存储 ----
lfs --version 2>/dev/null ; dpkg -l lustre-client-modules 2>/dev/null
juicefs version
fio --version

# ---- 监控 ----
dcgmi --version ; dcgmi discovery -l
promtool --version ; prometheus --version 2>/dev/null || kubectl -n monitoring get sts -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
```

---

## 11. 基线维护规则

1. **变更基线 = 变更管理**：升级基线里任何一项，走分册 07 的变更三板斧（灰度/观测/回滚）
2. **每季度评审**：驱动生产分支、K8s minor、OFED 大版本各评审一次
3. **EOL 跟踪**：Ubuntu 22.04 标准支持到 2027-04（ESM 到 2032）；K8s 每 minor 约 14 个月维护期；MLNX_OFED 23.10 之后旧名不再发布
4. **记录实际部署版本**：本基线是"目标版本"，实际集群现状用第 10 节脚本采集后归档，差异处标注原因

---

*基线快照：2026-01。维护人：___。上次评审：___。下次评审：___*
