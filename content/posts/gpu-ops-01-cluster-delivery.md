---
title: "GPU 集群运维（01）：集群交付与验收"
date: 2026-11-07T09:00:00+08:00
draft: false
description: "从机房规划、网络架构设计、BIOS 全量清单、OS 基线配置，到装机验收流程、nccl-tests、IB 网络验收、存储验收与验收报告模板的完整交付手册。"
tags: ["GPU", "交付验收", "机房规划"]
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
| gcc | 11.4 | 11.5 |
| systemd | 249 | 252 |
| Python（系统） | 3.10 | 3.9 |

**本册涉及的主要软件版本**

| 组件 | 版本 | 来源/说明 |
|---|---|---|
| NVIDIA Driver | R570 生产分支，570.124.04（示例） | Hopper/Blackwell 主基线 |
| NVIDIA Driver（兼容） | R550 生产分支，550.127.08（示例） | Ampere 存量集群兼容基线 |
| CUDA Toolkit | 12.8（R570）/ 12.4（R550） | 容器内自带，宿主机只出驱动 |
| DCGM（宿主机） | 3.3.9 / 4.2.x（`datacenter-gpu-manager` 包） | dcgmi diag 用于验收诊断 |
| dcgm-exporter | 4.1.0-4.2.0-ubuntu22.04（示例 tag） | GPU Operator v24.9.2 内含 |
| gpu-burn | git 最新 commit，编译依赖 CUDA Toolkit | 烤机工具，无正式 release |
| nccl-tests | git checkout commit（示例），编译依赖 CUDA 12.8 + NCCL 2.26.x（或 CUDA 12.4 + NCCL 2.21.x） | 无正式 release |
| fio | 源码编译 3.36+（发行版 Ubuntu 22.04=3.28，Rocky 9.5=3.26，偏旧） | 存储验收工具 |
| perftest（ib_write_bw 等） | 24.04.x（随 DOCA-OFED 24.10 发布） | IB 网络验收工具 |
| DOCA-OFED | 24.10（MLNX_OFED 24.04-0.7.0.0 为末代旧名） | ConnectX-7 固件 28.39.xxxx（示例） |
| nvidia-container-toolkit | v1.17.4 | apt/dnf 仓库 |

**版本钉死规则**：容器镜像 tag 禁用 `latest`，git 工具 checkout 到 tag/commit；标"（示例）"的 build 号以 NVIDIA 官方兼容矩阵当期推荐为准；Ubuntu 与 Rocky 命令差异处两者都给。

---

## 1. 机房规划

### 1.1 主流 GPU 服务器机型参数表

| 机型 | GPU 数量 | 单卡 TDP | 整机典型功耗 | 整机峰值功耗 | 整机重量 | 机柜功率密度要求 | 制冷方式 |
|------|----------|----------|--------------|--------------|----------|------------------|----------|
| NVIDIA DGX A100 | 8 × A100 80GB | 400W | 6.5 kW | 10.2 kW | ~77 kg | ≥15 kW/U（42U 机柜不超过 2 台） | 风冷（前后贯通） |
| NVIDIA DGX H100 | 8 × H100 80GB SXM5 | 700W | 10.2 kW | 14.3 kW | ~105 kg | ≥20 kW/U（42U 机柜不超过 1 台） | 风冷（需高风量机柜） |
| NVIDIA HGX H800（OEM） | 8 × H800 80GB SXM5 | 700W | 10.0 kW | 14.0 kW | ~95 kg（因 OEM 而异） | ≥20 kW/U | 风冷 |
| NVIDIA GB200 NVL72 | 72 × GB200（36 Grace + 72 B200） | ~1000W/GPU | ~120 kW（整 rack） | ~132 kW | ~2700 kg（满 rack） | 整 rack 设计，单机柜 120kW | 液冷（必须 DLC） |

> **注**：以上功耗数据以 NVIDIA 官方规格为参考，实际部署以 OEM 厂商（Dell、浪潮、超聚变、联想等）铭牌为准。

### 1.2 供电与 UPS 规划要点

```text
# 单机柜功率计算示例（DGX H100 × 1 台 + IB 交换机 × 2 台 + 存储节点 × 2 台）
DGX H100 峰值:     14.3 kW
IB 交换机（QM9700）: 0.8 kW × 2 = 1.6 kW
存储节点:          0.8 kW × 2 = 1.6 kW
──────────────────────────────
合计峰值:          17.5 kW
× 1.25 安全系数 =  21.9 kW

建议机柜配电: 25 kW（三相 380V / 40A 或 PDU 额定 ≥ 32A）
UPS 冗余级别: N+1（在线双变换 UPS，单台 ≥ 40 kVA）
PDU 要求: 双路供电（A/B 路），每条 PDU 独立承载 ≥ 50% 负载
```

### 1.3 承重与液冷要点

| 检查项 | 要求 | 说明 |
|--------|------|------|
| 机房楼板承重 | ≥ 1200 kg/m² | DGX H100 满装机柜 ~1200 kg，GB200 NVL72 ~2700 kg 需加强区 |
| 防静电地板承重 | ≥ 500 kg/块（600×600mm） | 液冷管路走地板下时需局部加固 |
| 液冷 CDU 位置 | 靠近机柜列头，管路 ≤ 15 m | 管路过长增加泵压、增大漏液风险 |
| 冷却液类型 | 丙二醇/去离子水混合液（PG25） | 严禁使用纯水，防冻/防腐/防生物 |
| 漏液检测 | 每 rack 底部部署漏液绳，接入 BMC 告警 | 液冷机型必须，风冷机型建议 |
| 进风温度（ASHRAE A3） | 5°C ~ 40°C，推荐 18~27°C | GPU 进风温度 >35°C 将触发降频保护 |

---

## 2. 网络架构设计

### 2.1 三网分离架构

![三网分离架构：管理网、业务网、IB 计算网各自独立交换与网段](/images/gpu-01-three-networks.svg)

| 网络 | 用途 | 带宽 | 协议 | VLAN |
|------|------|------|------|------|
| 管理网（BMC/IPMI） | 远程开关机、固件刷写、传感器监控 | 1 GbE | IPMI/Redfish/SSH | VLAN 100 |
| 业务以太网 | K8s Node 通信、Pod 流量、存储 IO、镜像拉取 | 2×25 GbE LACP | TCP/IP | VLAN 200（Node）、VLAN 201（Storage） |
| IB 计算网 | NCCL AllReduce、分布式训练梯度同步 | HDR 200Gb/s 或 NDR 400Gb/s | IB Verbs（无 IP） | 无（IB subnet） |

### 2.2 IB 胖树拓扑详解

#### Leaf-Spine 无收敛胖树（以 64 节点 HDR 集群为例）

```text
                    ┌─────────────────────────────┐
                    │      Spine 层（2 台）        │
                    │   QM8700 40口 HDR 200Gb/s   │
                    │   spine-ib-01  spine-ib-02  │
                    └─────┬──────────────┬────────┘
                          │              │
            ┌─────────────┼──────────────┼─────────────┐
            │             │              │             │
     ┌──────┴──────┐ ┌────┴─────┐ ┌─────┴─────┐ ┌────┴──────┐
     │ leaf-ib-01  │ │leaf-ib-02│ │leaf-ib-03 │ │leaf-ib-04 │
     │ QM8700 40口 │ │ QM8700   │ │ QM8700    │ │ QM8700    │
     └──┬─┬─┬─┬───┘ └─┬─┬─┬─┬─┘ └─┬─┬─┬─┬──┘ └─┬─┬─┬─┬──┘
        │ │ │ │        │ │ │ │     │ │ │ │       │ │ │ │
        N N N N        N N N N     N N N N       N N N N
        o o o o        o o o o     o o o o       o o o o
        d d d d        d d d d     d d d d       d d d d
        e e e e        e e e e     e e e e       e e e e

每台 Leaf: 40口，20口下联节点 + 20口上联 Spine
每台 Spine: 40口，分别接 4 台 Leaf × 5口（端口聚合 LAG 或 ECMP）
```

**收敛比计算**：
- 每 Leaf 下联 20 节点（每节点 1 HCA）= 20 × 200 Gb/s = 4 Tb/s 入口
- 每 Leaf 上联 Spine = 20 × 200 Gb/s = 4 Tb/s 出口
- **收敛比 = 1:1（无收敛，完全无阻塞胖树）**

| 交换机型号 | 端口数 | 单端口速率 | 总交换容量 | 适用层 |
|-----------|--------|-----------|-----------|--------|
| Mellanox QM8700（MQM8790-HS2F） | 40 × QSFP56 | HDR 200 Gb/s | 16 Tb/s | Leaf / Spine |
| Mellanox QM9700（MQM9790-HS2F） | 64 × OSFP | NDR 400 Gb/s | 51.2 Tb/s | Leaf / Spine |
| Mellanox QM9700（MQM9790-HS2F-NS） | 32 × OSFP（半配） | NDR 400 Gb/s | 25.6 Tb/s | Leaf（小规模） |

### 2.3 Rail-Optimized 架构原理

```text
传统拓扑（每节点 1 HCA 连 1 Leaf）          Rail-Optimized（每 GPU 1 HCA，GPU0-3→LeafA，GPU4-7→LeafB）

  gpu-node-01 (8×GPU)                        gpu-node-01 (8×GPU)
  ┌─────────────────────┐                    ┌───────────────────────────────────┐
  │GPU0 GPU1 ... GPU7   │                    │GPU0 GPU1 GPU2 GPU3 GPU4 GPU5 GPU6 GPU7│
  │  │    │        │    │                    │ │    │    │    │    │    │    │    │ │
  │  └────┴───┬────┘    │                    │ └────┴────┴────┘    └────┴────┴────┘ │
  │           │         │                    │       │                  │           │
  │       1×HCA         │                    │    HCA-0               HCA-1         │
  │           │         │                    │       │                  │           │
  └───────────┼─────────┘                    └───────┼──────────────────┼───────────┘
              │                                      │                  │
         leaf-ib-01                            leaf-ib-01          leaf-ib-02
```

**Rail-Optimized 优势**：
- 8-GPU 节点配 4 或 8 张 HCA，每 2 或 1 张 GPU 共享 1 HCA
- NCCL AllReduce 时同 rail 的 GPU 同时发数据，最大化 IB 链路利用率
- 避免单机 HCA 成为瓶颈（8 GPU 共享 1 HCA 时 AllReduce 吞吐受限）
- H100 8-GPU 推荐配置：8 × ConnectX-7 NDR（每 GPU 独立 HCA），或 4 × ConnectX-7（每 2 GPU 共享）

### 2.4 线缆选型表

| 连接类型 | 距离 | 线缆/模块型号 | 接口类型 | 速率 | 备注 |
|---------|------|-------------|---------|------|------|
| DAC（直连铜缆） | ≤ 3 m | Mellanox MCP1600-C003E（3m） | QSFP56 | HDR 200Gb/s | 机柜内叶到节点首选 |
| DAC（直连铜缆） | ≤ 2 m | Mellanox MCP1600-C002E（2m） | QSFP56 | HDR 200Gb/s | 相邻机柜 |
| AOC（有源光缆） | 3~30 m | Mellanox MFA1A00-C010（10m） | QSFP56 | HDR 200Gb/s | 跨机柜 Leaf-Spine |
| 光纤 + 光模块 | 10~100 m | 模块：Mellanox MMA1B00-C100（QSFP56 SR4）<br>光纤：MPO-MPO OM4 多模 | QSFP56 | HDR 200Gb/s | 跨机房、长距 Spine 互联 |
| NDR DAC | ≤ 2 m | Mellanox MCP7531-C002E（2m） | OSFP | NDR 400Gb/s | H100/GB200 机柜内 |
| NDR AOC | 3~30 m | Mellanox MFA1A00-C015（NDR OSFP） | OSFP | NDR 400Gb/s | 跨机柜 |
| NDR 光纤 | 10~100 m | 模块：OSFP-SR4-400G（QSFP112）<br>光纤：MPO-12 OM4 | OSFP | NDR 400Gb/s | 长距互联 |
| 管理网 | ≤ 100 m | Cat6/Cat6A 铜缆 | RJ45 | 1 GbE | BMC/IPMI |
| 业务网 | ≤ 100 m | Cat6A 或 SFP28 DAC | SFP28/RJ45 | 25 GbE | 业务以太网 |

> **选型原则**：DAC 最便宜且延迟最低（~0.1 μs），但距离受限；AOC 适合中等距离；光纤适合长距但光模块成本高。IB 网络优先用 DAC/AOC，避免光模块引入额外延迟。

---

## 3. BIOS 设置全量清单

以下以 Dell PowerEdge R760xa / 浪潮 NF5688M6 / 超聚变 FusionServer G5500 V7 通用叫法为参考，各厂商 BIOS 界面命名略有差异但功能等价。

| 设置项 | 推荐值 | 原因 |
|--------|--------|------|
| IOMMU（Intel VT-d / AMD-Vi） | **Disabled** | GPU 直通场景开启 IOMMU 会引入 DMA 地址转换开销，NCCL 实测降 5~10% 带宽；仅 KVM 虚拟化宿主机需开启 |
| SR-IOV | **Disabled**（物理机部署）/ Enabled（虚拟化部署） | 物理机直接用 GPU 无需 SR-IOV；开启后 BIOS 会分配额外 BAR 资源 |
| Above 4G Decoding | **Enabled** | 必须开启，否则 8×A100 80GB 的 BAR 空间（每卡 64GB）无法映射，导致 nvidia-smi 无法识别全部 GPU |
| PCIe ASPM（Active State Power Management） | **Disabled** | 省电状态切换会引入 PCIe 链路唤醒延迟，影响 GPU P2P 通信稳定性 |
| C-States（C1E/C3/C6） | **Disabled**（C1E=Off, C3=Off, C6=Off） | CPU 进入深 C-State 后响应中断延迟增大，影响 NCCL 集合通信尾延迟 |
| Turbo Boost / P-States | **Enabled**（Performance 模式） | 电源策略选 Performance，禁用 Power Saving；避免 CPU 降频影响数据预处理瓶颈 |
| 电源策略（Power Profile） | **Performance Per Watt (OS)** 或 **Performance** | 选 OS Controlled 时由 cpufreq 管理；选 Performance 锁定最高频率 |
| PCIe Gen 速率 | **Gen4**（A100）/ **Gen5**（H100/B200） | 锁定到硬件支持的最高代；Auto 有时降级到 Gen3 导致带宽减半 |
| PCIe Slot Bifurcation | **x16/x16**（按主板文档） | 确保每张 GPU 卡运行在 PCIe x16 模式，而非 x8 |
| 超线程（Hyper-Threading / SMT） | **Enabled** | GPU 训练场景超线程影响 <2%，但数据预处理、NCCL 线程可受益；纯推理低延迟场景可 Disable |
| 内存频率 | **最高支持频率（如 DDR5-4800）** | 锁定最高频率，不让 BIOS 降频节能 |
| 内存通道模式 | **Optimizer Mode**（Dell）/ **Independent Channel** | 最大化内存带宽，数据预处理不成为瓶颈 |
| NUMA 节点交织（Node Interleaving） | **Disabled** | NUMA 感知调度下训练性能更好；开启后内存访问跨 NUMA 延迟增大 |
| SVM Mode（AMD 虚拟化） | **Disabled**（物理机） | 同 IOMMU，物理机无需 |
| Secure Boot | **Disabled**（驱动安装阶段）| 开启后 NVIDIA 驱动内核模块需签名才能加载，安装阶段建议先关闭，生产环境可配置签名密钥后开启 |
| Boot Mode | **UEFI** | 必须 UEFI，Legacy BIOS 不支持大 BAR 映射 |
| iDRAC/IPMI LAN | **Enabled，独立专用网口** | 确保 BMC 带外管理独立于业务网 |

**保存并验证**：
```bash
# Dell（通过 racadm 远程设置示例）
racadm -r 10.0.1.11 -u root -p calvin set BIOS.SysProfileSettings.SysProfile Performance

# 浪潮（通过 ipmitool）
ipmitool -I lanplus -H 10.0.1.12 -U admin -P admin chassis bootdev pxe
ipmitool -I lanplus -H 10.0.1.12 -U admin -P admin power cycle
```

---

## 4. OS 基线配置

### 4.1 操作系统版本建议

| 场景 | 推荐 OS | 内核版本 |
|------|--------|---------|
| 物理机 GPU 训练 | Ubuntu 22.04 LTS（HWE 内核） | 5.15+ 或 6.5+ HWE |
| 物理机 GPU 训练 | RHEL/Rocky Linux 9.x | 5.14+ |
| K8s Worker（GPU 节点） | Ubuntu 22.04 LTS | 5.15 HWE |

### 4.2 sysctl 完整配置

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / Rocky 9.5 (kernel 5.14)
# sysctl 参数两系统通用，无 OS 差异
cat > /etc/sysctl.d/99-gpu-cluster.conf << 'EOF'
# === 网络 buffer（IB/25GbE 高带宽场景） ===
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 33554432
net.core.wmem_default = 33554432
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535

# === 文件句柄 ===
fs.file-max = 2097152
fs.nr_open = 2097152

# === VM 参数 ===
vm.swappiness = 1
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5
vm.overcommit_memory = 1
vm.max_map_count = 262144
vm.min_free_kbytes = 1048576

# === 共享内存（NCCL shm 需要） ===
kernel.shmmax = 68719476736
kernel.shmall = 4294967296

# === NUMA 相关 ===
vm.zone_reclaim_mode = 0

# === 网络转发（K8s 节点必须） ===
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF

sysctl --system
```

### 4.3 chrony.conf 完整示例

```bash
# 环境: Ubuntu 22.04.5 (chrony 4.2) / Rocky 9.5 (chrony 4.x)
# 包名均为 chrony，systemctl 单元名均为 chronyd，两系统无差异
cat > /etc/chrony/chrony.conf << 'EOF'
# GPU 集群时间同步（训练任务对时间敏感，NCCL timeout 依赖时钟一致性）
# 使用内网 NTP 服务器，延迟 <1ms

server ntp-01.cluster.local iburst prefer
server ntp-02.cluster.local iburst prefer
server ntp.aliyun.com iburst

# 允许本地子网查询
allow 10.0.0.0/8

# 时间跳变 >1s 时直接 slew（不跳变，避免训练中断）
makestep 1.0 -1

# 硬件时钟 UTC
rtcsync

# 漂移文件
driftfile /var/lib/chrony/drift

# 日志
logdir /var/log/chrony
log measurements statistics tracking

# 密钥文件
keyfile /etc/chrony/chrony.keys
EOF

systemctl enable --now chronyd
chronyc sources -v
chronyc tracking
```

### 4.4 SELinux / Swap / Firewalld 处理

```bash
# 环境: Ubuntu 22.04.5 (systemd 249, UFW) / Rocky 9.5 (systemd 252, firewalld)
# === SELinux（训练集群建议 Permissive 或 Disabled） ===
# RHEL/Rocky
sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
setenforce 0
getenforce  # 验证输出：Permissive

# === Swap（K8s 节点必须关闭，GPU 训练节点强烈建议关闭） ===
swapoff -a
sed -i '/swap/s/^/#/' /etc/fstab
# 验证
free -h  # Swap 行应全为 0

# === Firewalld（K8s 节点关闭，改用网络策略） ===
systemctl stop firewalld
systemctl disable firewalld
systemctl stop iptables 2>/dev/null
systemctl disable iptables 2>/dev/null
# 验证
systemctl is-active firewalld  # 应输出 inactive

# === Ubuntu UFW ===
ufw disable
systemctl disable ufw 2>/dev/null
```

### 4.5 内核头文件与编译工具

```bash
# 环境: Ubuntu 22.04.5 (gcc 11.4) / Rocky 9.5 (gcc 11.5)
# 注意：Ubuntu 包头文件包名=linux-headers-$(uname -r)，Rocky 包名=kernel-devel-$(uname -r)
# 重要：DKMS 依赖的 kernel-devel/linux-headers 版本必须与运行内核完全一致，否则驱动编译失败
# Ubuntu/Debian
    build-essential \
    dkms \
    gcc \
    g++ \
    make \
    pkg-config \
    libpci-dev \
    libnuma-dev \
    libcap2 \
    libtool \
    autoconf \
    automake

# RHEL/Rocky Linux
dnf install -y \
    kernel-devel-$(uname -r) \
    kernel-headers-$(uname -r) \
    gcc \
    gcc-c++ \
    make \
    dkms \
    rpm-build \
    libpciaccess-devel \
    numactl-devel \
    libcap \
    libtool \
    autoconf \
    automake

# 验证内核头版本与运行内核一致
rpm -q kernel-devel  # RHEL
dpkg -l linux-headers-$(uname -r)  # Ubuntu
```

---

## 5. 装机验收完整流程

### 5.1 PCIe 链路检查

```bash
# 查看所有 NVIDIA GPU 的 PCIe 链路状态
lspci -vvv -s $(lspci | grep -i nvidia | awk '{print $1}' | head -1) | grep -E "LnkCap|LnkSta|Region"

# 完整检查脚本：逐卡验证 PCIe Gen 与 Lane 宽度
for dev in $(lspci | grep -i "3D controller\|VGA.*NVIDIA" | awk '{print $1}'); do
    echo "=== GPU: $dev ==="
    lspci -vvv -s "$dev" | grep -E "LnkCap:|LnkSta:"
    echo ""
done
```

**期望输出（A100 PCIe Gen4 x16）**：
```yaml
LnkCap: Port 0, Speed 16GT/s, Width x16, ASPM L1, Exit Latency L1 <64us
LnkSta: Speed 16GT/s (ok), Width x16 (ok)
```

**判读标准**：
- `LnkSta Speed` 必须等于 `LnkCap Speed`（Gen4 = 16GT/s，Gen5 = 32GT/s）
- `LnkSta Width` 必须等于 `LnkCap Width`（x16）
- 若 `LnkSta Width x8`：检查 Riser 卡、主板插槽、BIOS Bifurcation 设置
- 若 `LnkSta Speed 8GT/s`（Gen3）：检查 BIOS PCIe Gen 锁定、重新插拔卡

### 5.2 GPU 拓扑验证

```bash
nvidia-smi topo -m
```

**DGX A100 期望输出**：
```text
        GPU0  GPU1  GPU2  GPU3  GPU4  GPU5  GPU6  GPU7  NIC0  NIC1
GPU0     X    NV12  NV12  NV12  NV12  NV12  NV12  NV12  NODE  PIX
GPU1    NV12   X    NV12  NV12  NV12  NV12  NV12  NV12  PIX   NODE
GPU2    NV12  NV12   X    NV12  NV12  NV12  NV12  NV12  NODE  PIX
...
```

**拓扑标识判读表**：

| 标识 | 含义 | 说明 |
|------|------|------|
| NV18 | NVLink 连接，18 条链路 | A100 SXM 满配 NVLink（600 GB/s 双向） |
| NV12 | NVLink 连接，12 条链路 | A100 SXM 部分配置 |
| NV4 | NVLink 连接，4 条链路 | PCIe 版 A100 或降配 |
| PIX | 同一 PCIe 交换机（PCIe Switch） | 同基板内 GPU 互联 |
| PXB | 同一 CPU Socket 下不同 PCIe Switch | 跨 PCIe Switch 但同 CPU |
| PHB | 同 CPU Socket，跨 PCIe Root Complex | 同 CPU 跨 NUMA node |
| NODE | 同系统，跨 CPU Socket | 跨 NUMA，延迟高 |
| SYS | 不同系统（跨机） | 经 IB/以太网 |

**常见问题判读**：
- 出现 `SYS` 而非 `NV*`：NVSwitch 或基板故障，立即报修
- 两 GPU 间为 `NODE` 而非 `NV*`：NVLink 连接缺失，检查 NVSwitch 状态
- 8 GPU 两两均 NV18：正常（DGX/HGX A100 SXM 满配）

### 5.3 nvidia-smi -q 关键字段

```bash
# 逐卡检查关键字段
for i in $(seq 0 7); do
    echo "=== GPU $i ==="
    nvidia-smi -i $i -q | grep -E "Serial Number|ECC Mode|Retired|Remapped|UUID|VBIOS Version|Driver Version"
    echo ""
done

# 详细检查 ECC 与 Remap 状态
nvidia-smi -q -d ECC,RETIRED_PAGES,ROW_REMAPPER
```

**关键字段判读**：

| 字段 | 期望值 | 异常处理 |
|------|--------|---------|
| ECC Mode | Enabled | 若 Disabled 需开启：`nvidia-smi -e 1`（需重启） |
| Single Bit ECC | 0 | >0 可接受，持续增加需监控 |
| Double Bit ECC | 0 | >0 立即报修，GPU 硬件故障 |
| Remapped Rows（Uncorrectable）| 0 | >0 需关注，达到阈值（如 8 行）GPU 会退役 |
| Retired Pages（SBE/DBE） | 0 | SBE>0 监控；DBE>0 立即报修 |
| Serial Number | 与装箱单一致 | 不一致则发错货 |
| VBIOS Version | 与 OEM 交付清单一致 | 不一致需刷写到指定版本 |

### 5.4 dcgmi diag 诊断

```bash
# 环境: Ubuntu 22.04.5 / Rocky 9.5 / DCGM 3.3.9 或 4.2.x（datacenter-gpu-manager 包）
# 注意：DCGM 4.x 部分指标名前缀有变化（3.x DCGM_FI_*，4.x 部分重命名），dcgmi diag 命令两版本一致
# Level 1：快速检查（~1 分钟），验证 GPU 基本功能
dcgmi diag -r 1

# Level 2：中等检查（~5 分钟），含内存压力测试
dcgmi diag -r 2

# Level 3：完整检查（~15 分钟），含 GPU 计算与内存完整测试
dcgmi diag -r 3

# 指定 GPU 运行
dcgmi diag -r 2 -i 0,1,2,3

# 输出示例（Level 1 通过）
# Successfully ran Diagnostics for group 0
# +---------------------------+
# |     DIAGNOSTIC RESULTS    |
# +---------------------------+
# | GPU 0: PASSED             |
# | GPU 1: PASSED             |
# ...
# | Overall Result: PASSED    |
# +---------------------------+
```

**各级别区别**：

| 级别 | 耗时 | 检查内容 | 适用场景 |
|------|------|---------|---------|
| Level 1 | ~1 min | 硬件状态、NVLink、PCIe、ECC、温度 | 每次开机后必跑 |
| Level 2 | ~5 min | Level 1 + 内存读写压力 | 新装机验收 |
| Level 3 | ~15 min | Level 2 + GPU 计算单元全量测试 | 深度验收、故障排查 |

### 5.5 gpu-burn 烤机

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / CUDA 12.8（对应 R570 570.124.04）
# gpu-burn 无正式 release，git clone 后 checkout 到最新 commit 即可
# 编译时自动检测 CUDA_HOME；容器内编译需确保 CUDA Toolkit 12.x 已挂载
# 安装依赖
apt-get install -y git build-essential

# 下载编译 gpu-burn
cd /opt
git clone https://github.com/wilicc/gpu-burn.git
cd gpu-burn
git checkout $(git rev-list -n1 HEAD)  # 钉死到具体 commit，便于复现（示例，以实际 commit hash 为准）
make  # 自动检测 CUDA 路径，如未检测到：make CUDA_HOME=/usr/local/cuda-12.8

# 运行烤机（8 GPU，600 秒，使用 doubles 精度）
./gpu_burn -d -m 100% 600

# 运行烤机（指定 GPU 0-3，使用 tensor core）
./gpu_burn -tc -m 100% 600 0 1 2 3
```

**烤机期间同时监控**：
```bash
# 终端 2：实时监控温度与频率
watch -n 1 'nvidia-smi --query-gpu=index,temperature.gpu,clocks.sm,clocks.mem,power.draw --format=csv,noheader'

# 终端 3：监控降频原因
watch -n 2 'nvidia-smi -q -d CLOCK | grep -A5 "Clocks Event Reasons"'

# 终端 4：监控 XID 错误
journalctl -k -f | grep -i "xid\|nvidia"
```

**烤机判断标准**：

| 检查项 | 通过标准 | 不通过处理 |
|--------|---------|-----------|
| GPU 温度 | 全卡 ≤85°C（A100），≤88°C（H100） | 超过阈值检查散热/风扇/液冷流量 |
| 频率保持 | SM Clock 维持在 Boost 频率 ≥90%（A100 ≥1410MHz，H100 ≥1830MHz） | 频繁降频检查供电/散热 |
| Clocks Event Reasons | HW Slowdown=No，HW Thermal=No | HW Slowdown=Yes 表示触发电流保护，报修 |
| XID 错误 | 无 XID 31/45/48/74/79 | XID 31=GPU 页面错误，XID 74=NVLink 错误，立即报修 |
| gpu-burn 退出码 | exit 0，无 "FAILURE" 输出 | 出现 FAILURE 定位具体 GPU，交换卡位复测确认是否卡故障 |

---

## 6. nccl-tests 编译与运行

### 6.1 编译

```bash
# 环境: Ubuntu 22.04.5 (kernel 5.15) / CUDA 12.8 + NCCL 2.26.x / OpenMPI 4.1.x（apt 源）
# nccl-tests 无正式 release，git checkout 到具体 commit 钉死，编译时 CUDA/NCCL 路径与版本必须对应
# 安装依赖
apt-get install -y git build-essential mpi-default-bin mpi-default-dev

# 下载 nccl-tests
cd /opt
git clone https://github.com/NVIDIA/nccl-tests.git
cd nccl-tests
git checkout <commit-hash>  # 钉死到具体 commit（示例，以当期 NGC 容器内 NCCL 版本对应的 commit 为准）

# 编译（指定 CUDA 和 NCCL 路径，容器内 CUDA 12.8 + NCCL 2.26.x 路径）
make CUDA_HOME=/usr/local/cuda-12.8 \
     NCCL_HOME=/usr \
     MPI_HOME=/usr/lib/x86_64-linux-gnu/openmpi \
     -j$(nproc)

# 如 NCCL 通过 runfile 安装到自定义路径（CUDA 12.4 + NCCL 2.21.x 兼容基线示例）
make CUDA_HOME=/usr/local/cuda-12.4 \
     NCCL_HOME=/opt/nccl \
     MPI_HOME=/opt/openmpi \
     -j$(nproc)

# 编译产物
ls build/
# all_reduce_perf  alltoall_perf  broadcast_perf  reduce_perf  ...
```

### 6.2 单机运行（8 GPU）

```bash
# all_reduce_perf 单机 8 GPU
./build/all_reduce_perf \
    -b 8 \
    -e 2G \
    -f 2 \
    -g 8 \
    -c 1 \
    -n 100 \
    -w 50

# 参数说明
# -b 8       起始消息大小 8 bytes
# -e 2G      结束消息大小 2 GB
# -f 2       倍增因子（每次乘 2）
# -g 8       使用 8 张 GPU
# -c 1       每次迭代 1 个 AllReduce
# -n 100     100 次迭代
# -w 50      前 50 次为 warmup（不计入统计）
```

**完整输出示例（A100 8×80GB SXM，消息大小 ≥1GB 部分）**：
```bash
# nThread 1 nGpus 8 minBytes 8 maxBytes 2147483648 step: 2^(1/2) factors: 2 nWarmup 50 nIters 100
# Using devices
#  Rank 0 Group 0 Pid 123456 on gpu-node-01 device 0 [0x07] A100-SXM4-80GB
#  Rank 1 Group 0 Pid 123456 on gpu-node-01 device 1 [0x0F] A100-SXM4-80GB
...
#  out-of-place AllReduce
# size (B)   time (us)   algbw (GB/s)   busbw (GB/s)   error
   1048576        20.1         52.17         91.30    N/A
   2097152        21.3         98.46        172.31    N/A
   4194304        23.5        178.42        312.24    N/A
   8388608        27.1        309.60        541.80    N/A
  16777216        35.2        476.49        833.86    N/A
  33554432        53.4        628.14       1099.25    N/A
  67108864        91.7        731.85       1280.74    N/A
 134217728       168.3        797.51       1395.64    N/A
 268435456       318.5        842.91       1475.09    N/A
 536870912       620.3        865.48       1514.59    N/A
1073741824      1226.4        875.75       1532.56    N/A
2147483648      2441.2        879.68       1539.44    N/A
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 1539.44 GB/s
```

**algbw vs busbw 解释**：
- `algbw`（Algorithm Bandwidth）= 消息大小 / 时间，算法层面带宽
- `busbw`（Bus Bandwidth）= algbw × (2(N-1)/N)，考虑了 AllReduce 两阶段（reduce-scatter + all-gather），反映实际网络/GPU 间总线利用率
- **AllReduce busbw 计算公式**：`busbw = algbw × (2(N-1)/N)`，N=GPU 数量
- N=8 时：`busbw = algbw × (14/8) = algbw × 1.75`
- 验收时以 busbw 为准，algbw 仅供参考

### 6.3 mpirun 双机运行

**hostfile 示例**（`/opt/nccl-tests/hostfile`）：
```text
gpu-node-01 slots=8
gpu-node-02 slots=8
```

**完整 mpirun 命令**：
```bash
mpirun --allow-run-as-root \
    -np 16 \
    --hostfile /opt/nccl-tests/hostfile \
    --map-by ppr:8:node \
    -x LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu \
    -x NCCL_IB_HCA=mlx5_0,mlx5_1,mlx5_2,mlx5_3 \
    -x NCCL_IB_GID_INDEX=3 \
    -x NCCL_IB_TIMEOUT=23 \
    -x NCCL_IB_RETRY_CNT=13 \
    -x NCCL_SOCKET_IFNAME=bond0 \
    -x NCCL_DEBUG=WARN \
    -x CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
    -mca btl_tcp_if_include bond0 \
    -mca oob_tcp_if_include bond0 \
    /opt/nccl-tests/build/all_reduce_perf \
    -b 512M \
    -e 2G \
    -f 2 \
    -g 1 \
    -c 1 \
    -n 50 \
    -w 20
```

**关键环境变量说明**：

| 变量 | 作用 | 典型值 |
|------|------|--------|
| `NCCL_IB_HCA` | 指定使用的 IB HCA 设备 | `mlx5_0,mlx5_1,mlx5_2,mlx5_3` |
| `NCCL_IB_GID_INDEX` | RoCE 网络必须设为 3（IPv4 GID）；纯 IB 通常用 0 | `3`（RoCE）/ `0`（IB） |
| `NCCL_IB_TIMEOUT` | IB 超时重试次数（2^N 微秒） | `23`（~8.4 秒） |
| `NCCL_SOCKET_IFNAME` | 控制面通信网卡 | `bond0` 或 `eth0` |
| `NCCL_DEBUG` | 日志级别 | `WARN`（生产）/ `INFO`（调试） |

### 6.4 各机型 busbw 基线表

| 机型 | GPU 数量 | 测试规模 | algbw 期望（2GB msg） | busbw 期望（2GB msg） |
|------|---------|---------|---------------------|---------------------|
| DGX A100 SXM 80GB | 8（单机） | 单机 all_reduce | ≥850 GB/s | ≥1480 GB/s |
| DGX H100 SXM 80GB | 8（单机） | 单机 all_reduce | ≥1200 GB/s | ≥2100 GB/s |
| HGX H800 SXM 80GB | 8（单机） | 单机 all_reduce | ≥1100 GB/s | ≥1925 GB/s |
| A100 × 2 节点（IB HDR 200Gb/s） | 16（双机） | 双机 all_reduce | ≥21 GB/s | ≥36 GB/s |
| H100 × 2 节点（IB NDR 400Gb/s） | 16（双机） | 双机 all_reduce | ≥42 GB/s | ≥73 GB/s |
| A100 × 2 节点（IB NDR 400Gb/s） | 16（双机） | 双机 all_reduce | ≥42 GB/s | ≥73 GB/s |

> **说明**：单机 busbw 受 NVLink 带宽决定（A100 SXM NVLink 600 GB/s 双向，8 GPU 满配 all-reduce busbw 可达 1480 GB/s）；双机 busbw 受 IB 链路数量与速率决定。

### 6.5 低于基线的排查思路

```text
双机 busbw 低于期望值
├── 检查 IB 链路状态
│   ├── ibstat mlx5_0 → 确认 Rate = 200（HDR）/ 400（NDR）
│   ├── iblinkinfo → 确认无降级端口（LinkDowned > 0）
│   └── 更换 DAC/AOC 线缆测试
├── 检查 NCCL 路径选择
│   ├── NCCL_DEBUG=INFO 运行，查看 "Channel 00 : 0[0] -> 1[0] via NET/IB/0" 日志
│   ├── 确认使用了 IB（不是 Socket/TCP）
│   └── 确认 NCCL_IB_HCA 指定了正确的 HCA 设备
├── 检查 NUMA 对齐
│   ├── nvidia-smi topo -m → GPU 与 NIC 是否同 NUMA node
│   └── numactl --hardware → 确认 HCA 与 GPU 的 NUMA 亲和性
├── 检查 PCIe 带宽
│   ├── lspci -vvv → LnkSta 是否满速
│   └── 单独跑 PCIe bandwidth test
└── 检查系统负载
    ├── top/htop → 是否有其他进程占用 CPU
    └── dmesg | grep -i "aer\|corrected" → PCIe AER 错误
```

---

## 7. IB 网络验收

### 7.1 ibstat 检查

```bash
# 查看所有 HCA 端口状态
ibstat

# 期望输出示例（ConnectX-7 HDR）
# CA 'mlx5_0'
#   CA type: MT4129
#   Number of ports: 1
#   Firmware version: 28.39.3004
#   Hardware version: 0
#   Node GUID: 0x946d ae03 0012 3456
#   System image GUID: 0x946d ae03 0012 3456
#   Port 1:
#     State: Active
#     Physical state: LinkUp
#     Rate: 200              ← HDR = 200 Gb/s
#     Base lid: 1
#     LMC: 0
#     SM lid: 1
#     Capability mask: 0x2651e84a
#     Port GUID: 0x946d ae03 0012 3456
#     Link layer: InfiniBand

# 判读：State=Active, Physical state=LinkUp, Rate=200（HDR）或 400（NDR）
```

### 7.2 iblinkinfo 检查

```bash
# 查看 IB 子网内所有链路（需在 SM 节点运行）
iblinkinfo

# 输出示例
# Switch: 0x946dae0300789abc Mellanox MQM8790 40-port HDR:
#      1   37[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>       2   18[  ] "gpu-node-01" ( )
#      1   38[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>       2   18[  ] "gpu-node-02" ( )
# ...
# 判读：所有链路应为 Active/LinkUp；如有 Polling/Down 需检查线缆

# 检查降级链路（速率低于预期）
iblinkinfo | grep -v "200.0 Gbps"  # HDR 集群
iblinkinfo | grep -v "400.0 Gbps"  # NDR 集群
```

### 7.3 ibdiagnet 全面诊断

```bash
# 运行完整 IB 网络诊断（耗时 5~20 分钟，取决于规模）
ibdiagnet --all --output /tmp/ibdiagnet-report

# 查看关键报告
cat /tmp/ibdiagnet-report/ibdiagnet2.pm
cat /tmp/ibdiagnet-report/ibdiagnet2.lnk
cat /tmp/ibdiagnet-report/ibdiagnet2.errors

# 关注点：
# - Link errors: SymbolError, LinkDowned, PortRcvErrors 应为 0 或极低
# - 无 "Warning" 或 "Error" 条目
```

### 7.4 ib_write_bw 性能测试

```bash
# 环境: DOCA-OFED 24.10 / perftest 24.04.x（ib_write_bw 随 DOCA-OFED 安装，无需单独编译）
# Server 端（gpu-node-02）
ib_write_bw -d mlx5_0 -p 18515 -F --report_gbits -q 4 -D 30

# Client 端（gpu-node-01）
ib_write_bw -d mlx5_0 -p 18515 -F --report_gbits -q 4 -D 30 10.20.0.2

# 参数说明
# -d mlx5_0     指定 HCA 设备
# -p 18515      端口号（避免与系统服务冲突）
# -F            使用 CPU affinity（绑核）
# --report_gbits  输出单位为 Gb/s
# -q 4          使用 4 个 QP（Queue Pair）
# -D 30         测试持续 30 秒

# 期望输出（Client 端最后几行，HDR 200Gb/s）
#  #bytes   #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
#  65536    1000             198.45            196.82               0.376123
#  131072   1000             199.12            198.56               0.189534
#  262144   1000             199.67            199.34               0.094967
#  524288   1000             199.89            199.71               0.047562
# 1048576   1000             199.94            199.85               0.023812

# 判读标准：
# HDR 200Gb/s：BW average ≥ 196 Gb/s（≥98% 线速）
# NDR 400Gb/s：BW average ≥ 392 Gb/s（≥98% 线速）
# 低于 95% 需排查线缆、光模块、交换机端口
```

---

## 8. 存储验收

### 8.1 fio job 文件：随机读（模拟数据集加载）

```ini
# /opt/fio-jobs/random-read-4k.job
[global]
ioengine=libaio
direct=1
group_reporting
bs=4k
iodepth=128
numjobs=8
runtime=120
time_based
filename=/data/test/random-read-test
size=10G
rw=randread

[random-read-4k]
description=Simulate training dataset random access pattern
```

### 8.2 fio job 文件：顺序写（模拟 checkpoint 保存）

```ini
# /opt/fio-jobs/sequential-write-1m.job
[global]
ioengine=libaio
direct=1
group_reporting
bs=1m
iodepth=32
numjobs=4
runtime=120
time_based
filename=/data/test/seq-write-test
size=50G
rw=write
fsync_on_close=1

[sequential-write-1m]
description=Simulate checkpoint sequential write pattern
```

### 8.3 运行与判断

```bash
# 环境: Ubuntu 22.04.5 (fio 3.28 apt) / Rocky 9.5 (fio 3.26 dnf)
# 注意：发行版 fio 版本偏旧（基线建议源码编译 3.36+），如需新特性/新 IO 引擎请走源码编译
# 安装 fio（发行版快速安装，验收足够用）
apt-get install -y fio  # Ubuntu 22.04: fio 3.28
dnf install -y fio      # Rocky 9.5: fio 3.26

# 运行随机读测试
fio /opt/fio-jobs/random-read-4k.job --output=/tmp/fio-random-read.json --output-format=json

# 运行顺序写测试
fio /opt/fio-jobs/sequential-write-1m.job --output=/tmp/fio-seq-write.json --output-format=json

# 提取关键指标
jq '.jobs[0].read.iops, .jobs[0].read.bw, .jobs[0].read.lat_ns.mean' /tmp/fio-random-read.json
jq '.jobs[0].write.bw, .jobs[0].write.lat_ns.mean' /tmp/fio-seq-write.json
```

**判断标准**（以 NVMe SSD 本地盘为例）：

| 测试场景 | 指标 | NVMe SSD（PCIe Gen4） | NVMe SSD（PCIe Gen5） | 网络存储（Ceph/NFS 25GbE） |
|---------|------|---------------------|---------------------|-------------------------|
| 随机读 4K | IOPS | ≥500,000 | ≥1,000,000 | ≥50,000 |
| 随机读 4K | 延迟（avg） | ≤150 μs | ≤100 μs | ≤500 μs |
| 顺序写 1M | 带宽 | ≥3000 MB/s | ≥6000 MB/s | ≥2000 MB/s |
| 顺序写 1M | 延迟（avg） | ≤500 μs | ≤300 μs | ≤2000 μs |

---

## 9. 验收报告模板

```markdown
# GPU 集群验收报告

## 基本信息

| 项目 | 内容 |
|------|------|
| 项目名称 | XX AI 计算中心 GPU 集群 |
| 验收日期 | 2026-XX-XX |
| 验收人 | 张三 / 李四 |
| 节点数量 | 8 台 GPU 节点 + 2 台 IB 交换机 + 1 台管理节点 |
| GPU 型号 | NVIDIA H100 80GB SXM5 |
| 单节点 GPU 数量 | 8 |
| 网络类型 | IB NDR 400Gb/s |
| 存储类型 | NVMe SSD（本地）+ Ceph（分布式） |

## 1. 硬件清单核对

| 检查项 | 期望值 | 实测值 | 状态 |
|--------|--------|--------|------|
| GPU 数量（总计） | 64 | 64 | ✅ PASS |
| GPU 序列号 | 与装箱单一致 | 一致 | ✅ PASS |
| IB HCA 数量/节点 | 4 × ConnectX-7 | 4 × ConnectX-7 | ✅ PASS |
| 内存容量/节点 | 2048 GB DDR5 | 2048 GB DDR5 | ✅ PASS |
| 本地 NVMe 容量/节点 | 2 × 3.84 TB | 2 × 3.84 TB | ✅ PASS |
| BMC 可达 | 全部 8 台 | 全部 8 台 | ✅ PASS |

## 2. PCIe 链路检查

| 节点 | GPU | LnkCap | LnkSta | 状态 |
|------|-----|--------|--------|------|
| gpu-node-01 | GPU 0~7 | Gen5 x16 | Gen5 x16 | ✅ PASS |
| gpu-node-02 | GPU 0~7 | Gen5 x16 | Gen5 x16 | ✅ PASS |
| ... | ... | ... | ... | ... |
| gpu-node-08 | GPU 0~7 | Gen5 x16 | Gen5 x16 | ✅ PASS |

## 3. GPU 拓扑验证

| 节点 | 期望拓扑 | 实测拓扑 | 状态 |
|------|---------|---------|------|
| gpu-node-01 | 8 GPU 全互联 NV18 | NV18 全互联 | ✅ PASS |
| gpu-node-02 | 8 GPU 全互联 NV18 | NV18 全互联 | ✅ PASS |
| ... | ... | ... | ... |
| gpu-node-08 | 8 GPU 全互联 NV18 | NV18 全互联 | ✅ PASS |

## 4. ECC 与页面退役

| 节点 | GPU | Single Bit ECC | Double Bit ECC | Remapped Rows | Retired Pages | 状态 |
|------|-----|---------------|---------------|---------------|---------------|------|
| gpu-node-01 | 0~7 | 0 | 0 | 0 | 0 | ✅ PASS |
| ... | ... | ... | ... | ... | ... | ... |

## 5. dcgmi diag 结果

| 节点 | Level 1 | Level 2 | Level 3 | 状态 |
|------|---------|---------|---------|------|
| gpu-node-01 | PASSED | PASSED | PASSED | ✅ PASS |
| ... | ... | ... | ... | ... |

## 6. gpu-burn 烤机（600 秒）

| 节点 | 最高温度 | 平均 SM Clock | HW Slowdown | XID 错误 | 状态 |
|------|---------|-------------|------------|---------|------|
| gpu-node-01 | 82°C | 1830 MHz | No | 无 | ✅ PASS |
| ... | ... | ... | ... | ... | ... |

## 7. nccl-tests all_reduce_perf

| 节点/规模 | algbw（GB/s） | busbw（GB/s） | 基线 busbw | 状态 |
|----------|-------------|-------------|-----------|------|
| gpu-node-01 单机 8 GPU | ≥1200 | ≥2100 | ≥2100 | ✅ PASS |
| gpu-node-02 单机 8 GPU | ≥1200 | ≥2100 | ≥2100 | ✅ PASS |
| ... | ... | ... | ... | ... |
| gpu-node-01 + gpu-node-02 双机 | ≥42 | ≥73 | ≥73 | ✅ PASS |

## 8. IB 网络验收

| 检查项 | 期望值 | 实测值 | 状态 |
|--------|--------|--------|------|
| ibstat 所有端口 | Active/LinkUp | 全部 Active | ✅ PASS |
| iblinkinfo 无降级链路 | 0 降级 | 0 | ✅ PASS |
| ib_write_bw（HDR） | ≥196 Gb/s | 198.5 Gb/s | ✅ PASS |
| ibdiagnet 错误 | 0 Error | 0 | ✅ PASS |

## 9. 存储性能

| 测试场景 | IOPS | 带宽（MB/s） | 延迟（μs） | 基线 | 状态 |
|---------|------|-------------|-----------|------|------|
| 随机读 4K（NVMe） | 620,000 | 2480 | 120 | IOPS≥500K | ✅ PASS |
| 顺序写 1M（NVMe） | N/A | 3400 | 380 | BW≥3000MB/s | ✅ PASS |

## 10. 遗留问题

| 编号 | 问题描述 | 严重程度 | 责任方 | 预计解决日期 |
|------|---------|---------|--------|------------|
| 1 | （无） | — | — | — |

## 验收结论

- [ ] **通过**：全部检查项 PASS，可以交付使用
- [ ] **有条件通过**：有遗留问题但不影响基本功能，限期整改
- [ ] **不通过**：存在关键项 FAIL，退回整改

签字：_______________  日期：_______________
```

---

## 附录：验收命令快速参考卡

```bash
# === 一键验收脚本骨架 ===
# 环境: Ubuntu 22.04.5 (kernel 5.15) / driver R570 570.124.04 / CUDA 12.8 / DOCA-OFED 24.10
# 依赖：nvidia-smi、dcgmi、ibstat、iblinkinfo、nccl-tests 均需提前安装完毕
#!/bin/bash
set -euo pipefail

NODE_NAME=$(hostname)
REPORT_DIR="/opt/acceptance-reports/${NODE_NAME}"
mkdir -p "$REPORT_DIR"

echo "[$(date)] Starting acceptance tests on $NODE_NAME"

# 1. PCIe 链路
for dev in $(lspci | grep -i "3D controller.*NVIDIA" | awk '{print $1}'); do
    lspci -vvv -s "$dev" | grep -E "LnkCap|LnkSta" >> "$REPORT_DIR/pcie-link.txt"
done

# 2. GPU 拓扑
nvidia-smi topo -m > "$REPORT_DIR/topo.txt"

# 3. GPU 详情
nvidia-smi -q > "$REPORT_DIR/smi-full.txt"

# 4. dcgmi diag Level 1
dcgmi diag -r 1 > "$REPORT_DIR/dcgmi-l1.txt" 2>&1

# 5. IB 状态
ibstat > "$REPORT_DIR/ibstat.txt" 2>&1
iblinkinfo > "$REPORT_DIR/iblinkinfo.txt" 2>&1

# 6. nccl-tests 单机
/opt/nccl-tests/build/all_reduce_perf -b 512M -e 2G -f 2 -g 8 -c 1 -n 50 -w 20 \
    > "$REPORT_DIR/nccl-single-node.txt" 2>&1

echo "[$(date)] Acceptance tests complete. Reports in $REPORT_DIR"
```
