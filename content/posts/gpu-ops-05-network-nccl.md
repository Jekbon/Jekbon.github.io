---
title: "GPU 集群运维（05）：网络 —— IB/RoCE 与 NCCL"
date: 2026-11-11T09:00:00+08:00
draft: false
description: "InfiniBand 概念体系与硬件拓扑、IB 运维命令大全、OpenSM 子网管理器、端口错误计数器巡检、RoCE v2 完整配置、GPUDirect RDMA、NCCL 深入与排障决策树。"
tags: ["网络", "RDMA", "NCCL", "GPU"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下版本摘取自 `00-版本基线.md`，仅列出本册涉及的组件。完整基线以 00 分册为准。

| 组件 | 基线版本 | 说明 |
|------|---------|------|
| OS | Ubuntu 22.04.5 LTS / Rocky Linux 9.5 | 双基线并行 |
| 内核 | 5.15.0-xxx-generic (Ubuntu GA) / 5.14.0-503 (Rocky) | HWE 可选 6.8（需 R550+ 驱动） |
| MLNX_OFED / DOCA-OFED | **DOCA-OFED 24.10**（MLNX_OFED 24.04-0.7.0.0 为末代旧名）；23.10 长维护版 | 24.10 起 OFED 更名并入 DOCA |
| ConnectX-7 固件 | 28.39.xxxx（示例） | `mlxfwmanager --query` 核对 |
| ConnectX-6 Dx 固件 | 22.39.xxxx（示例） | |
| MFT 工具（mlxconfig/mlxfwmanager） | 4.29.x（示例） | |
| perftest（ib_write_bw 等） | 24.04.x | |
| OpenSM | 随 OFED 24.10 发布 | `opensm -v` 查看 |
| infiniband-diags | 随 OFED（ibstat/perfquery/iblinkinfo 等） | apt 自带版本较旧，生产装 OFED 版 |
| IB 交换机固件 | QM9700 NDR 3.10.xxxx / QM8700 HDR 3.9.xxxx（示例） | `ibswitchinfo` 查看 |
| RoCE 交换机 | 华为 CE V200R022（示例）/ NVIDIA Spectrum-4 SN5000（4.x） | PFC/ECN 语法按厂商版本 |
| GPUDirect RDMA | nvidia-peermem（驱动 R465+ 内置，替代旧 nv_peer_mem） | 内核 ≥5.13 也可用 dmabuf |
| NCCL | 2.26.x（CUDA 12.8）/ 2.21.x（CUDA 12.4） | 跨机通信；同作业各 rank 版本必须一致 |
| NVIDIA Driver | R570 570.124.04（示例，Hopper/Blackwell）/ R550 550.127.08（示例，Ampere） | 生产分支 |

---



---

## 1. InfiniBand 概念体系

### 1.1 IB 与以太网的本质区别

| 维度 | InfiniBand | 以太网 |
|------|-----------|--------|
| 流控机制 | 信用流控（Credit-based），发送端必须有足够 credit 才发，零丢包 | 尽力而为（Best-effort），依赖 PFC/PAUSE 做反压 |
| 寻址方式 | LID（Local ID，16-bit，子网内）+ GID（Global ID，128-bit，跨子网） | MAC + IP |
| 子网管理 | 必须有 Subnet Manager（SM）才能工作 | 即插即用 |
| 传输语义 | 通道（Channel）语义，面向连接的 QP | 数据报语义，无连接 |
| QoS 实现 | SL（Service Level，3-bit）→ VL（Virtual Lane）硬件隔离 | VLAN + DSCP/PCP，PFC 按优先级 |
| 延迟（同代） | ~1μs（cut-through 交换） | ~3-10μs（store-and-forward） |
| 典型 MTU | 4096（IB L2 MTU 固定） | 1500 或 9000（Jumbo） |

### 1.2 核心对象

- **QP（Queue Pair）**：通信端点，含 Send Queue（SQ）和 Receive Queue（RQ）。类型有 RC（Reliable Connection，最常用）、UC（Unreliable Connection）、UD（Unreliable Datagram）。NCCL 使用 RC QP。
- **CQ（Completion Queue）**：完成事件队列，QP 操作完成后产生 CQE。
- **MR（Memory Region）**：注册到 HCA 的内存区域，只有注册过的内存才能被 RDMA 访问。包含 lkey（本地访问）和 rkey（远端访问）。
- **LID（Local Identifier）**：16-bit，子网内唯一，由 SM 分配。0xFFFF 表示未分配。
- **GID（Global Identifier）**：128-bit，类似 IPv6，端口 GID table 有多个 index（0,1,2...），RoCE v2 的 GID 就是 IPv4-mapped 或 IPv6 地址。
- **PKey（Partition Key）**：16-bit 分区隔离，类似 VLAN ID。0xFFFF=全成员，0x7FFF=受限成员。
- **SL（Service Level）**：3-bit，映射到 VL（Virtual Lane），用于 QoS。
- **SM（Subnet Manager）**：发现拓扑、分配 LID、配置交换机转发表、管理 PKey。

### 1.3 速率代际表

| 代际 | 缩写 | 单链路速率 | 4x 链路带宽（双向） | 线缆类型 | 典型年份 |
|------|------|-----------|-------------------|---------|---------|
| SDR | Single Data Rate | 2.5 Gbps | 20 Gbps | — | 2001 |
| DDR | Double Data Rate | 5 Gbps | 40 Gbps | — | 2005 |
| QDR | Quad Data Rate | 10 Gbps | 80 Gbps | QSFP | 2009 |
| FDR | Fourteen Data Rate | 14 Gbps | 112 Gbps（实际 56Gbps/dir） | QSFP | 2012 |
| EDR | Enhanced Data Rate | 25 Gbps | 200 Gbps（100Gbps/dir） | QSFP28 / QSFP+ | 2015 |
| HDR | High Data Rate | 50 Gbps | 400 Gbps（200Gbps/dir） | QSFP56 / HDR100 | 2019 |
| NDR | Next Data Rate | 100 Gbps | 800 Gbps（400Gbps/dir） | OSFP / QSFP-DD | 2022 |
| XDR | eXtended Data Rate | 200 Gbps | 1600 Gbps（800Gbps/dir） | OSFP-XD | 2025+ |

> 注：4x 链路指 4 lane 聚合，这是最常见的线缆配置。标注带宽均为双向聚合值（full duplex）。

---

## 2. IB 硬件与拓扑

### 2.1 HCA（Host Channel Adapter）型号

| 型号 | 系列 | 速率 | 端口数 | 关键特性 |
|------|------|------|-------|---------|
| ConnectX-6 Dx | MCX653106A | HDR 200Gbps | 2× QSFP56 | 主流 HPC，GPUDirect RDMA 2.0 |
| ConnectX-6 Lx | MCX633106A | HDR 100Gbps | 2× QSFP56 | 性价比，单 lane 50G |
| ConnectX-7 | MCX755106A | NDR 400Gbps | 2× OSFP | 最新一代，PCIe 5.0 |
| BlueField-3 | MBF3H5H | NDR 400Gbps | 2× OSFP | DPU，内嵌 ARM 核心 |

### 2.2 IB 交换机

| 型号 | 端口数 | 速率 | 端口类型 | 适用场景 |
|------|-------|------|---------|---------|
| QM8700 | 40 端口 | HDR 200Gbps | QSFP56 | HDR 叶脊架构 |
| QM8790 | 20 端口 | HDR 200Gbps | QSFP56 | 小规模集群 |
| QM9700 | 64 端口 | NDR 400Gbps | OSFP | NDR 大规模集群 |
| QM9790 | 32 端口 | NDR 400Gbps | OSFP | NDR 中等规模 |

### 2.3 线缆与光模块选型表

| 类型 | 接口 | 支持速率 | 最大距离 | 典型成本 | 适用场景 |
|------|------|---------|---------|---------|---------|
| DAC（Direct Attach Copper） | QSFP28/56 | EDR/HDR | 3m（HQ）/5m | 低 | 机柜内互联（ToR 到服务器） |
| AOC（Active Optical Cable） | QSFP28/56 | EDR/HDR | 30m | 中 | 跨机柜同排 |
| 多模光纤 + SR4 光模块 | MPO-12 | EDR/HDR | 100m（OM4） | 中高 | 跨行机柜 |
| 单模光纤 + LR4 光模块 | LC duplex | EDR/HDR | 10km | 高 | 楼宇间 |
| OSFP DAC | OSFP | NDR | 3m | 中 | NDR 柜内 |
| OSFP AOC | OSFP | NDR | 30m | 中高 | NDR 跨柜 |

> 采购注意：DAC 必须确认两端 HCA/交换机品牌兼容码（Mellanox/NVIDIA 原厂码 vs 通用码）。混用可能 link 不起来。

### 2.4 胖树（Fat-tree）拓扑

胖树是 IB 网络的标准拓扑，保证无阻塞全带宽：

```json
           [Spine Switch 1]    [Spine Switch 2]
            /  |  |  \           /  |  |  \
           /   |  |   \         /   |  |   \
    [Leaf 1] [Leaf 2] [Leaf 3] [Leaf 4]  ...
       |  |     |  |     |  |     |  |
       N  N     N  N     N  N     N  N    （GPU 节点）
```

- 每个 Leaf 交换机上半部分端口连 Spine，下半部分连服务器
- N 节点规模需要 N/(2×端口数) 个 Leaf + 对应 Spine
- 以 QM8700（40 口）为例：20 口下行 + 20 口上行 → 每 Leaf 带 20 台服务器，20 台需要 1 个 Spine

### 2.5 Rail-Optimized 拓扑详解

Rail-Optimized 是针对 GPU 集群优化的拓扑，每个 GPU 绑定固定 HCA 端口（"轨道"），同轨 GPU 直连同叶交换机：

```text
8-GPU 节点内部（以 ConnectX-7 NDR 为例）：

  GPU0 ── mlx5_0 (NDR port 0) ──┐
  GPU1 ── mlx5_1 (NDR port 1) ──┤── Leaf Switch A（Rail 0-7 下联）
  GPU2 ── mlx5_2 (NDR port 0) ──┤
  GPU3 ── mlx5_3 (NDR port 1) ──┘
  GPU4 ── mlx5_4 (NDR port 0) ──┐
  GPU5 ── mlx5_5 (NDR port 1) ──┤── Leaf Switch B（Rail 0-7 下联）
  GPU6 ── mlx5_6 (NDR port 0) ──┤
  GPU7 ── mlx5_7 (NDR port 1) ──┘
```

8 轨接线说明（每节点 8 GPU × 8 HCA 端口映射到 8 个 Leaf Switch）：
- Rail 0：所有节点的 GPU0 → 全部连到 Leaf-0
- Rail 1：所有节点的 GPU1 → 全部连到 Leaf-1
- 以此类推至 Rail 7
- 每台 Leaf 交换机下行端口全部连同一 rail 的 GPU，上行端口全部连 Spine
- 好处：同一 rail 内的 GPU 通信不经过 Spine，NCCL allreduce 在 rail 内完成时延迟最低

---

## 3. IB 运维命令大全

> 所有命令基于 **DOCA-OFED 24.10**（旧名 MLNX_OFED，24.04-0.7.0.0 为末代旧名；23.10 为长维护版），示例设备 `mlx5_0`，端口 `1`。
> 发行版 inbox 驱动（如 Ubuntu 22.04 自带 mlx5_core）与 OFED 驱动在部分行为上有差异（如 SR-IOV VF 管理、RoCE QoS 细节），标注处见各命令注释。

### 3.1 基础状态查询

#### ibstat

```bash
# 环境: DOCA-OFED 24.10（infiniband-diags 随 OFED 安装）/ ConnectX-7 FW 28.39.x（示例）
# 发行版 apt 装的 infiniband-diags 版本较旧，生产环境建议从 OFED 仓库安装
$ ibstat
CA 'mlx5_0'
        CA type: MT4123
        Number of ports: 1
        Firmware version: 28.39.1002
        Hardware version: 0
        Node GUID: 0x946dae0300123456
        System image GUID: 0x946dae0300123456
        Port 1:
                State: Active
                Physical state: LinkUp
                Rate: 200        # 表示 HDR 200Gbps (4x50G)
                Base lid: 15
                LMC: 0
                SM lid: 1
                Capability mask: 0x2651e848
                Port GUID: 0x946dae0300123456
                Link layer: InfiniBand
```

**判读要点**：
- State=Active + Physical state=LinkUp = 正常
- Rate 对应表：100=EDR, 200=HDR, 400=NDR
- SM lid=0xFFFF 表示 SM 未到达
- Link layer=Ethernet 表示 RoCE 模式

#### ibstatus

```bash
$ ibstatus
Infiniband device 'mlx5_0' port 1 status:
        default gid:     fe80:0000:0000:0000:946d:ae03:0012:3456
        base lid:        0x15
        sm lid:          0x1
        state:           4: Active
        phys state:      5: LinkUp
        rate:            200 Gb/sec (4X HDR)
        link_layer:      InfiniBand
```

#### ibv_devinfo

```bash
# 环境: DOCA-OFED 24.10 / libibverbs（随 OFED 安装）/ ConnectX-7 FW 28.39.x（示例）
$ ibv_devinfo -d mlx5_0
hca_id: mlx5_0
        transport:                      InfiniBand (0)
        fw_ver:                         28.39.1002
        node_guid:                      946d:ae03:0012:3456
        sys_image_guid:                 946d:ae03:0012:3456
        vendor_id:                      0x02c9
        vendor_part_id:                 4123
        hw_ver:                         0x0
        board_id:                       MT_0000000012
        phys_port_cnt:                  1
        max_qp:                         65536
        max_qp_wr:                      32768
        max_sge:                        512
        max_cq:                         65536
        max_cqe:                        4194303
        max_mr:                         16777216
        max_pd:                         16777216
        max_pkeys:                      128
        port:   1
                state:                  PORT_ACTIVE (4)
                max_mtu:                4096 (5)
                active_mtu:             4096 (5)
                sm_lid:                 1
                port_lid:               21
                port_lmc:               0x00
                link_layer:             InfiniBand
                max_msg_sz:             2147483647
```

#### iblinkinfo

```bash
# 环境: DOCA-OFED 24.10 / infiniband-diags（随 OFED）/ QM8790 HDR（示例拓扑）
$ iblinkinfo
CA: node01 HCA-1:
      0x946dae0300123456      1    1[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>       35    1[  ] ( )
CA: node02 HCA-1:
      0x946dae0300567890      1    2[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>       35    2[  ] ( )
SW: Switch-01 (MQM8790-HS2F):
      0x946dae0300aabbcc     35    1[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>        1    1[  ] ( )
      0x946dae0300aabbcc     35    2[  ] ==( 4X          200.0 Gbps Active/  LinkUp)==>        2    1[  ] ( )
```

**判读**：每行格式 `GUID port [index] ==(速率 状态/物理)==> 对端LID 对端口port`。状态不是 Active/LinkUp 就需要排查。

#### ibnetdiscover

```bash
$ ibnetdiscover -g -l
# Topology file: generated on 2024-03-15 10:30:00
# Max of 3 hops discovered
# Initiated from node 946dae0300123456 port 0000000000000001

vendid=0x2c9
devid=0xcf08
sysimgguid=0x946dae0300aabbcc
caguid=0x946dae0300123456

Ca      1 "H-00946dae0300123456"  # "node01 HCA-1"
"CA-1"  1 "S-00946dae0300aabbcc"  [  ]
```

### 3.2 交换机与 SM 管理

#### ibswitchinfo

```bash
# 环境: DOCA-OFED 24.10 / infiniband-diags（随 OFED）/ QM9700 NDR 或 QM8700 HDR 交换机 FW 3.10.x/3.9.x（示例）
$ ibswitchinfo
Switch: 0x946dae0300aabbcc
        Switch Ports: 36
        Enhanced Port 0:
                State: Active
                LID: 35
        Enhanced Port 1:
                State: Active
                LID: 35
                LinkSpeedEnabled: 4X 50.0 Gbps
                LinkSpeedActive: 4X 50.0 Gbps
```

#### sminfo

```bash
# 环境: DOCA-OFED 24.10 / OpenSM（随 OFED 24.10）
$ sminfo
sminfo: sm lid 1 sm guid 0x946dae0300ffffff, activity count 12345678 state 3 SMINFO_ACTIVENOTSTANDBY
```

**判读**：
- state 3 = SMINFO_ACTIVENOTSTANDBY（主 SM 运行中）
- state 1 = SMINFO_STANDBY（备用 SM）
- state 0 = SMINFO_NOTACTIVE（未激活）
- activity count 持续增长说明 SM 在正常轮询

#### smpquery

```bash
$ smpquery nodeinfo -D 0,1,2
NodeInfo:
BaseVers:........................1
MgmtVers:........................1
NodeType:........................Switch
NumPorts:........................36
SysGuid:.........................0x946dae0300aabbcc
NodeGuid:........................0x946dae0300aabbcc
PortGuid:........................0x946dae0300aabbcc
PartCap:.........................128
DevId:...........................0xcf08
Revision:........................0
PortNum:.........................3
```

### 3.3 性能计数器（重点）

#### perfquery -x（扩展计数器）

```bash
# 环境: DOCA-OFED 24.10 / perftest 24.04.x（随 OFED 安装）/ ConnectX-7 FW 28.39.x（示例）
# perftest 包含 ib_write_bw/ib_read_bw/ib_send_bw 等带宽测试，以及 perfquery 计数器查询
$ perfquery -x -G 0x946dae0300123456 1
# Port extended counters: Lid 21 port 1
PortXmitData:.....................12345678901234    # 发送数据量（单位：256字节/lane，需×4）
PortRcvData:......................12345678901234    # 接收数据量（同上）
PortXmitPkts:.....................9876543210        # 发送包数
PortRcvPkts:......................9876543210        # 接收包数
PortXmitWait:.....................0                 # 发送等待次数（非零=拥塞）
PortMulticastXmitPkts:............12345
PortMulticastRcvPkts:.............12345
```

**计数器详解**：

| 计数器 | 含义 | 关注点 |
|-------|------|--------|
| PortXmitData/PortRcvData | 收发字节量（×256×4=实际字节数） | 算带宽利用率 |
| PortXmitPkts/PortRcvPkts | 收发包数 | 算包速率 |
| PortXmitWait | 因信用不足等待发送的次数 | 持续增长=拥塞，正常情况应为 0 或极低 |
| PortUnicastXmitPkts | 单播发包数 | — |
| PortMulticastXmitPkts | 组播发包数 | 异常高可能 SM 问题 |

#### perfquery（标准计数器，错误排查用）

```bash
# 环境: DOCA-OFED 24.10 / perftest 24.04.x（随 OFED）
$ perfquery -G 0x946dae0300123456 1
# Port counters: Lid 21 port 1
SymbolErrorCounter:...............0
LinkErrorRecoveryCounter:.........0
PortRcvSwitchRelayErrors:.........0
PortRcvDiscards:..................0
PortXmitDiscards:.................0
PortXmitConstraintErrors:.........0
PortRcvConstraintErrors:..........0
CounterSelect2:...................0
LocalLinkIntegrityErrors:.........0
ExcessiveBufferOverrunErrors:.....0
VL15Dropped:......................0
PortXmitData:.....................1234567890
PortRcvData:......................1234567890
PortXmitPkts:.....................987654
PortRcvPkts:......................987654
LinkDownedCounter:................0
```

### 3.4 诊断工具

#### ibdiagnet

```bash
# 环境: DOCA-OFED 24.10 / ibdiagnet（随 OFED，版本随 OFED 发布）
$ ibdiagnet --verbosity 5 --output /tmp/ibdiagnet_report.txt
# IBDIAGNET 2.12.0-1 - IB diagnostic tool
# Running: ibdiagnet --verbosity 5
-I- Scanning ...
-I- Fabric Size:
-I-     CAs: 64
-I-     Switches: 8
-I- Checking ...
-I- Link Integrity Check
-W- Port lid 21 port 1: SymbolErrorCounter=5 (threshold=0)
-I- Congestion Check
-I-     No congestion issues found
-I- Report generated: /tmp/ibdiagnet_report.txt
```

#### ibportstate

```bash
# 环境: DOCA-OFED 24.10 / infiniband-diags（随 OFED）
$ ibportstate 35 1 enable    # 启用交换机端口 35 的 1 号口
$ ibportstate 35 1 disable   # 禁用
$ ibportstate 35 1 reset     # 重置（谨慎）
$ ibportstate 21 1           # 查询端口 21 状态
ibportstate: Lid 21 port 1 ... ACTIVE
```

### 3.5 固件与配置管理

#### mlxfwmanager

```bash
# 环境: MFT 4.29.x（mlxconfig/mlxfwmanager 属于 MFT 工具包，随 DOCA-OFED 24.10 安装）
# ConnectX-6 Dx FW 26.39.xxxx（示例当前版本）→ 可升级至 28.39.xxxx（示例可用版本）
$ mlxfwmanager
Querying Mellanox devices firmware ...

Device #1:
----------

  Device Type:      ConnectX6DX
  Part Number:      MCX653106A-CDAT_Ax
  Description:      ConnectX-6 Dx VPI adapter card; HDR/200Gb/s; 2-port QSFP56; PCIe 4.0 x16
  PSID:             MT_0000000012
  PCI Device Name:  /dev/mst/mt4125_pciconf0
  Base GUID:        946dae0300123450
  Port1 MAC:        94:6d:ae:12:34:56
  Port2 MAC:        94:6d:ae:12:34:57
  Versions:         Current        Available
     FW             26.39.1002     28.39.1002
     PXE            3.7.201        3.7.301

   Status:          Update required
```

#### mlxconfig（关键配置项）

```bash
# 环境: MFT 4.29.x（随 DOCA-OFED 24.10）/ ConnectX-6 Dx（示例设备）
# 修改配置后需 mlxfwreset 或重启节点生效
$ mlxconfig -d /dev/mst/mt4125_pciconf0 query

Device #1:
----------

Device type:    ConnectX6DX
Name:           MCX653106A-CDAT_Ax
Description:    ConnectX-6 Dx VPI adapter card
Device:         /dev/mst/mt4125_pciconf0

Configurations:                                      Next Boot       New
         SRIOV_EN                                    True(1)         True(1)
         NUM_OF_VFS                                  8               8
         LINK_TYPE_P1                                IB(1)           IB(1)
         LINK_TYPE_P2                                IB(1)           IB(1)
         BOOT_OPTION_ROM_EN_P1                       True(1)         True(1)
         BOOT_VLAN_EN_P1                             False(0)        False(0)
         LEGACY_BOOT_PROTOCOL_P1                     PXE(1)          PXE(1)
         IP_VER_P1                                   IPv4(1)         IPv4(1)
         PCI_WR_ORDERING                             per_mkey(0)     per_mkey(0)
         MULTI_PORT_VHCA_EN                          False(0)        False(0)
         ADVANCED_PCI_SETTINGS                       0x00000000      0x00000000
```

**关键配置项说明**：

| 参数 | 含义 | 推荐值 | 修改场景 |
|------|------|-------|---------|
| SRIOV_EN | SR-IOV 虚拟化 | True（虚拟化环境）/ False（裸金属） | K8s + SR-IOV 时需开启 |
| LINK_TYPE_P1/P2 | 端口链路类型 | IB(1) 或 ETH(2) | IB/RoCE 切换 |
| NUM_OF_VFS | VF 数量 | 按需 | SR-IOV VF 数 |
| PCI_WR_ORDERING | PCIe 写序 | per_mkey(0) | 某些场景改 per_qp(1) 提升性能 |

---

## 4. OpenSM 子网管理器

### 4.1 安装与启动

```bash
# 环境: OpenSM（随 DOCA-OFED 24.10 发布）/ Ubuntu 22.04 或 Rocky 9
# OFED 自带的 OpenSM 版本与 OFED 同步，比发行版 apt/dnf 源的更新
# RHEL/Rocky（OFED 自带）
yum install opensm

# 启动
systemctl enable --now opensm

# 或直接前台运行（调试用）
opensm --priority 15 --log_file /var/log/opensm.log
```

### 4.2 opensm.conf 关键参数

> 以下配置随 DOCA-OFED 24.10 附带的 OpenSM 版本验证。不同 OFED 大版本间参数名基本稳定，但新增参数（如 `torus-2QoS` 增强）需查当期发布说明。

配置文件位置：`/etc/opensm/opensm.conf`

```ini
# SM 优先级（0-15），值越大优先级越高；主 SM 设 15，备 SM 设 14
sm_priority 15

# 路由引擎选择
routing_engine updn
# 可选：minhop（最短路径）、updn（上下路由，适合胖树）、ftree（fat-tree 专用）、torus-2QoS

# 轮询间隔（毫秒），SM 轮询拓扑变化的频率
sweep_interval 10000

# 最大重路由次数
max_reverse_hops 0

# 日志级别：0=emerg, 1=alert, 2=crit, 3=error, 4=warn, 5=notice, 6=info, 7=debug
log_level 0x01

# LID 分配方式
guid_routing_order_policy 1

# PKey 管理
partition_manager_file /etc/opensm/partitions.conf

# QoS 策略
qos_policy 1
qos_policy_file /etc/opensm/qos-policy.conf

# 子网前缀（用于 GID 生成）
subnet_prefix fe80::

# 最大 MKey 租期
m_key_lease_period 200
```

### 4.3 主备部署方案

```text
                 ┌──────────────┐
                 │  Spine Sw    │
                 └──────┬───────┘
                  ┌─────┴─────┐
              ┌───┴──┐   ┌───┴──┐
              │Leaf-1│   │Leaf-2│
              └──┬─┬─┘   └──┬─┬─┘
                 │ │        │ │
             ┌───┘ └──┐ ┌──┘ └───┐
             │        │ │        │
         ┌───┴──┐ ┌──┴─┴──┐ ┌──┴───┐
         │ SM主  │ │ 节点   │ │ SM备  │
         │pri=15│ │       │ │pri=14│
         └──────┘ └───────┘ └──────┘
```

**配置**：
- 主 SM 节点：`opensm.conf` 中 `sm_priority 15`
- 备 SM 节点：`opensm.conf` 中 `sm_priority 14`
- 主 SM 故障 → 备 SM 在 polling interval 内接管（通常 10s 内）
- 两个 SM 必须能互相发现（同子网、同 PKey）

### 4.4 日志解读

```text
# /var/log/opensm.log 关键行
osm_state_mgr.c:781: SM state is ACTIVE        # SM 正常运行
osm_subnet.c:1234: Received trap(64) from LID 21  # 收到端口状态变化 trap
osm_sa_path_record.c:890: PathRecord query from LID 15 to LID 21  # 路由查询
osm_state_mgr.c:500: Entering STANDBY state     # SM 降级为备
osm_state_mgr.c:450: Entering MASTER state      # SM 升级为主
osm_port.c:200: Port 21 state changed to DOWN   # 端口 down
```

### 4.5 常用管理命令

```bash
# 查看当前 SM 状态
sminfo

# 强制 SM 重新路由（sweep）
kill -HUP $(pidof opensm)

# 查看 SM 配置的 LID 表
sminfo --dump_lfts

# 查看 SM 的 partition 配置
cat /etc/opensm/partitions.conf

# 临时切换到备 SM（维护主 SM 时）
# 在主 SM 上降低优先级
sed -i 's/sm_priority 15/sm_priority 13/' /etc/opensm/opensm.conf
kill -HUP $(pidof opensm)
# 备 SM (pri=14) 自动接管
```

---

## 5. 端口错误计数器详解与巡检脚本

### 5.1 错误计数器详解表

| 计数器 | 含义 | 增长原因 | 处置动作 |
|-------|------|---------|---------|
| SymbolErrorCounter | 物理层符号错误 | 线缆损坏、接头脏污、EMI 干扰、光衰过大 | 更换线缆/清洁接头；检查光模块 DOM（`ethtool -S` 看 Tx/Rx power） |
| LinkDownedCounter | 链路 down 次数 | 线缆松动、设备重启、交换机端口故障 | 检查物理连接；`dmesg \| grep mlx5` 看驱动日志 |
| PortRcvErrors | 接收端 CRC 等错误 | 对端发送问题、线缆单向损坏 | 结合对端 PortXmit 错误定位 |
| PortXmitDiscards | 交换机丢弃的发包 | 拥塞、路由表满、PKey 不匹配 | 检查拥塞（PortXmitWait）；核查 PKey 配置 |
| LinkErrorRecoveryCounter | 链路自恢复次数 | 间歇性物理问题 | 频繁恢复=线缆/光模块劣化，计划更换 |
| ExcessiveBufferOverrunErrors | 缓冲区溢出 | 信用流控失效、SM 配置 credit 不足 | 检查 SM buffer credit 配置；可能是硬件 bug |
| PortRcvSwitchRelayErrors | 交换机无法转发的包 | LID 不在转发表中（SM 未更新） | 触发 SM sweep；检查 SM 日志 |
| LocalLinkIntegrityErrors | 链路完整性错误 | 物理层问题，比 SymbolError 更严重 | 同 SymbolError 处理 |
| VL15Dropped | VL15（管理包）丢弃 | SM 繁忙或 SM 包冲突 | 检查 SM 负载 |

### 5.2 计数器清零

```bash
# 环境: DOCA-OFED 24.10 / perftest 24.04.x（perfquery 命令）
# 清零单个端口的计数器
perfquery -G 0x946dae0300123456 1 -r

# 清零所有端口
perfquery -r -a

# 清零扩展计数器
perfquery -x -r -a
```

### 5.3 完整巡检脚本

```bash
#!/bin/bash
# ib_port_monitor.sh - IB 端口计数器增量监控脚本
# 环境: DOCA-OFED 24.10 / perftest 24.04.x（perfquery 依赖）/ bash 4.x+
# 用法: ./ib_port_monitor.sh [间隔秒数] [循环次数]
# 示例: ./ib_port_monitor.sh 60 10  （每60秒采集一次，共10次）

INTERVAL=${1:-60}
ITERATIONS=${2:-0}  # 0=无限循环
REPORT_DIR="/var/log/ib_monitor"
mkdir -p "$REPORT_DIR"

# 获取所有 CA 端口
get_all_ports() {
    ibstat -l | while read -r ca; do
        ports=$(ibstat "$ca" | grep "Port " | awk '{print $2}' | tr -d ':')
        for port in $ports; do
            state=$(ibstat "$ca" "$port" 2>/dev/null | grep "State:" | head -1 | awk '{print $2}')
            if [ "$state" = "Active" ]; then
                guid=$(ibstat "$ca" "$port" 2>/dev/null | grep "Port GUID" | awk '{print $2}')
                lid=$(ibstat "$ca" "$port" 2>/dev/null | grep "Base lid" | awk '{print $3}')
                echo "${guid}|${lid}|${ca}|${port}"
            fi
        done
    done
}

# 读取标准错误计数器
read_std_counters() {
    local guid="$1"
    local port="$2"
    perfquery -G "$guid" "$port" 2>/dev/null | grep -E "^(SymbolError|LinkError|PortRcv(Error|Discard|Switch)|PortXmit(Discard|Constraint)|LocalLink|ExcessiveBuffer|LinkDowned|VL15)" | \
    awk -F: '{gsub(/\./, "", $1); gsub(/ /, "", $2); print $1"="$2}'
}

# 读取扩展计数器
read_ext_counters() {
    local guid="$1"
    local port="$2"
    perfquery -x -G "$guid" "$port" 2>/dev/null | grep -E "^(PortXmitData|PortRcvData|PortXmitPkts|PortRcvPkts|PortXmitWait)" | \
    awk -F: '{gsub(/\./, "", $1); gsub(/ /, "", $2); print $1"="$2}'
}

# 存储到关联数组
declare -A PREV_STD PREV_EXT

echo "===== IB Port Monitor Started at $(date) ====="
echo "Interval: ${INTERVAL}s, Report dir: ${REPORT_DIR}"
echo ""

# 首次采集基线
PORTS=$(get_all_ports)
echo "Discovered ports:"
echo "$PORTS" | while IFS='|' read -r guid lid ca port; do
    echo "  GUID=$guid LID=$lid CA=$ca Port=$port"
    while IFS='=' read -r key val; do
        PREV_STD["${guid}_${port}_${key}"]="$val"
    done < <(read_std_counters "$guid" "$port")
    while IFS='=' read -r key val; do
        PREV_EXT["${guid}_${port}_${key}"]="$val"
    done < <(read_ext_counters "$guid" "$port")
done

echo ""
echo "Baseline captured. Starting monitoring loop..."
echo ""

iteration=0
while true; do
    iteration=$((iteration + 1))
    if [ "$ITERATIONS" -gt 0 ] && [ "$iteration" -gt "$ITERATIONS" ]; then
        break
    fi

    sleep "$INTERVAL"

    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    report_file="${REPORT_DIR}/report_$(date '+%Y%m%d').log"

    echo "--- Iteration $iteration @ $timestamp ---"
    echo "--- Iteration $iteration @ $timestamp ---" >> "$report_file"

    echo "$PORTS" | while IFS='|' read -r guid lid ca port; do
        echo "  [LID=$lid $ca:$port]"

        # 标准错误计数器增量
        while IFS='=' read -r key val; do
            prev_key="${guid}_${port}_${key}"
            prev_val="${PREV_STD[$prev_key]:-0}"
            delta=$((val - prev_val))
            if [ "$delta" -gt 0 ]; then
                echo "    ⚠ ${key}: +${delta} (total: ${val})"
                echo "  [LID=$lid] ${key}: +${delta} (total: ${val})" >> "$report_file"
            fi
            PREV_STD["$prev_key"]="$val"
        done < <(read_std_counters "$guid" "$port")

        # 扩展计数器增量（流量）
        while IFS='=' read -r key val; do
            prev_key="${guid}_${port}_${key}"
            prev_val="${PREV_EXT[$prev_key]:-0}"
            delta=$((val - prev_val))
            if [ "$key" = "PortXmitData" ] || [ "$key" = "PortRcvData" ]; then
                # 转换为 GB（每单位=256字节×4lane=1024字节=1KB）
                delta_gb=$(echo "scale=2; $delta * 1024 / 1073741824" | bc 2>/dev/null || echo "N/A")
                echo "    ${key}: +${delta_gb} GB in ${INTERVAL}s"
            elif [ "$key" = "PortXmitWait" ] && [ "$delta" -gt 0 ]; then
                echo "    ⚠ ${key}: +${delta} (congestion detected!)"
                echo "  [LID=$lid] ${key}: +${delta}" >> "$report_file"
            fi
            PREV_EXT["$prev_key"]="$val"
        done < <(read_ext_counters "$guid" "$port")
    done

    echo ""
done

echo "===== Monitor completed after $iteration iterations ====="
```

---

## 6. RoCE v2 完整配置

### 6.1 无损网络原理

RoCE v2 在以太网上实现 RDMA，依赖三个机制协同工作保证零丢包：

![RoCE 无损网络三要素：两端 HCA 跑 DCQCN，交换机做 ECN 标记与 PFC 反压](/images/gpu-05-roce-lossless.svg)
**三者关系**：

1. **PFC（Priority Flow Control）= 链路层反压**：接收端 buffer 即将满时发 PAUSE 帧给上游；
   按优先级（0-7）独立控制，只暂停对应优先级；作用范围逐跳（hop-by-hop）
2. **ECN（Explicit Congestion Notification）= 拥塞通知**：交换机检测队列拥塞（超阈值）
   在 IP 头标记 CE bit；接收端收到 CE 标记的包后发 CNP（Congestion Notification Packet）
   给发送端；作用范围端到端
3. **DCQCN = NVIDIA/Mellanox 的拥塞控制算法**：发送端收到 CNP 后降速，无 CNP 时逐步恢复；
   类似 TCP 的 AIMD，但专为 RDMA 设计

**时序**：

```text
T0: 流量突增 → 交换机队列深度上升
T1: 队列深度 > ECN min_threshold → 开始标记 CE
T2: 接收端收到 CE → 发送 CNP
T3: 发送端收到 CNP → 降速
T4: 降速不够快则队列继续涨 → 达到 PFC 阈值 → 发 PAUSE
T5: 上游暂停发送 → 队列回退 → PAUSE 解除
```


### 6.2 交换机侧配置

#### 华为 CE 系列（CE6800/CE8800/CE12800）

> 环境: 华为 VRP **V200R022**（示例）。不同 VRP 版本命令有差异，尤其是 QoS 模板语法（V200R019 之前用 `qos car` 写法，V200R022 用 `traffic behavior` + `qos policy` 模板化写法）；ECN 模板参数名在 V200R021+ 有调整。请以设备 `display version` 输出为准。

```text
# ========== 全局配置 ==========
system-view

# 开启 ECN 全局功能
ecn enable

# ========== PFC 配置 ==========
# 创建流策略匹配 RoCE 流量（DSCP 25 = ECN Capable, Priority 3）
traffic classifier roce_traffic operator or
 if-match dscp 25
 if-match 8021p 3
quit

traffic behavior roce_pfc
 car cir percent 100 cbs percent 100 green pass
quit

qos policy roce_policy
 classifier roce_traffic behavior roce_pfc
quit

# 在端口上应用 PFC（以 100GE 端口为例）
interface 100GE1/0/1
 qos pfc enable priority 3
 qos pfc enable priority 4
 # 启用 PFC 死锁检测
 qos pfc deadlock detect enable
 qos pfc deadlock detect timer 200
 qos pfc deadlock recover timer 300
quit

# ========== ECN 配置 ==========
# ECN 模板
ecn template roce_ecn
 # min-threshold: 开始标记 CE 的队列深度（字节）
 # max-threshold: 100% 标记概率的队列深度
 # max-drop-probability: 在 max-threshold 时的标记概率（百分比）
 min-tail-drop-threshold 10240
 max-tail-drop-threshold 51200
 drop-probability 100
quit

# 在端口队列上绑定 ECN
interface 100GE1/0/1
 qos queue 3 ecn template roce_ecn
 qos queue 4 ecn template roce_ecn
quit

# ========== WRED 配置（可选，配合 ECN）==========
wred template roce_wred
 queue 3 low-limit 40960 high-limit 102400 discard-percentage 30
 queue 4 low-limit 40960 high-limit 102400 discard-percentage 30
quit

interface 100GE1/0/1
 qos queue 3 wred template roce_wred
 qos queue 4 wred template roce_wred
quit

# ========== MTU 配置 ==========
interface 100GE1/0/1
 mtu 9216
quit

# ========== LLDP/DCBX（自动协商 PFC/ECN）==========
lldp enable
lldp dcbx enable
lldp dcbx version ieee

# ========== 验证命令 ==========
display qos pfc interface 100GE1/0/1
display qos queue statistics interface 100GE1/0/1
display ecn statistics interface 100GE1/0/1
display lldp neighbor brief
```

#### Cumulus Linux / NVIDIA Spectrum 风格

> 环境: Cumulus Linux 5.x（NVIDIA Spectrum 交换机 SONiC/Cumulus）或 Cumulus 4.x（存量）。NVIDIA 已将 Cumulus 整合为 NVUE（NVIDIA Universal Ethernet）配置体系，旧版 NCLU 命令逐步废弃。Spectrum-4 SN5000 固件 4.x。以下示例为传统 `/etc/network/interfaces` 风格，NVUE 环境请用 `nv set` 命令。

```bash
# /etc/cumulus/switchd.conf - 启用 RoCE 优化
switchd.roce.enable = true

# PFC 配置（通过 NCLU 或直接编辑）
# /etc/network/interfaces
auto swp1
iface swp1
    mtu 9216
    # PFC 优先级 3,4（DSCP 26,28 对应的 CoS）
    pfc 0 0 0 1 1 0 0 0
    # ECN 配置
    ecn 0 1 10240 51200 1 1
    # buffer 配置
    buffer-pool 0 102400

# 使用 mlnx_qos 在交换机上配置（Spectrum 交换机）
mlnx_qos -i swp1 --pfc 0,0,0,1,1,0,0,0
mlnx_qos -i swp1 --buffer_size 0,1,2,3,4,5,6,7,102400,0,0,0,0,0,0,0
```

### 6.3 网卡侧完整配置

> 以下配置基于 DOCA-OFED 24.10 + ConnectX-7 FW 28.39.x（示例）。
> 发行版 inbox mlx5_core 驱动的 sysfs ECN 路径可能与 OFED 驱动不同（OFED 有额外 tc/ecn 参数），请根据实际驱动版本调整路径。

```bash
# ========== 1. 设置链路类型为 Ethernet（RoCE 模式）==========
# 永久配置
mlxconfig -d /dev/mst/mt4125_pciconf0 set LINK_TYPE_P1=2 LINK_TYPE_P2=2
# 需要重启生效或：
mlxfwreset -d /dev/mst/mt4125_pciconf0 reset

# ========== 2. 启用 DCQCN 拥塞控制 ==========
# 检查当前 ECN 状态
cma_roce_mode -d mlx5_0 -p 1
# 设置 RoCE v2 模式
cma_roce_mode -d mlx5_0 -p 1 -m 2

# ========== 3. ECN 内核参数 ==========
# 启用 ECN（接收端和发送端都要开）
sysctl -w net.ipv4.tcp_ecn=2

# Mellanox 网卡 ECN 配置（通过 sysfs）
# 查看当前 ECN 设置
cat /sys/class/infiniband/mlx5_0/tc/1/ecn/enable
cat /sys/class/infiniband/mlx5_0/tc/1/ecn/np_cnp_prio_mode

# 设置 ECN 参数
echo 1 > /sys/class/infiniband/mlx5_0/tc/1/ecn/enable
echo 1 > /sys/class/infiniband/mlx5_0/tc/1/ecn/rp_np_cnp_prio_mode
echo 100 > /sys/class/infiniband/mlx5_0/tc/1/ecn/np_min_time_between_cnp
echo 1 > /sys/class/infiniband/mlx5_0/tc/1/ecn/rpg_threshold

# ========== 4. PFC 优先级配置（mlnx_qos）==========
# 设置 PFC 启用优先级 3（RoCE 流量通常使用 priority 3 或 4）
mlnx_qos -i ens1f0 --pfc 0,0,0,1,0,0,0,0

# 设置 DSCP 到优先级的映射
mlnx_qos -i ens1f0 --dscp2prio 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,25:3,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0

# 查看当前 QoS 配置
mlnx_qos -i ens1f0

# 输出示例：
# PFC config:
#  pfc_en 00001000
#  prio:    0  1  2  3  4  5  6  7
#  status:  0  0  0  1  0  0  0  0

# ========== 5. MTU 配置 ==========
ip link set ens1f0 mtu 9000
ip link set ens1f1 mtu 9000

# ========== 6. 查看 GID table ==========
show_gids
# 输出示例：
#     DEV     PORT INDEX GID                                     IPv4            VER DEV
#     ---     ---- ----- ---                                     ----            --- ---
#     mlx5_0  1    0     fe80::946d:ae03:12:3456                 -               v1  eth
#     mlx5_0  1    1     fe80::946d:ae03:12:3456                 -               v2  eth
#     mlx5_0  1    2     ::ffff:10.0.0.58                      10.0.0.58     v2  eth    <-- 选这个
#     mlx5_0  1    3     ::ffff:10.0.0.58                      10.0.0.58     v1  eth

# GID index 选择规则：
# - RoCE v2 + IPv4: 选择 VER=v2 且 IPv4 非空的那行，通常是 index 2
# - 通过 NCCL_IB_GID_INDEX=2 指定

# ========== 7. ethtool 验证 ==========
ethtool -i ens1f0
# driver: mlx5_core
# version: 24.10-0.7.0.0       # ← OFED 驱动版本号（此示例为 DOCA-OFED 24.10）
# firmware-version: 28.39.1002   # ← ConnectX-7 FW（示例）

ethtool -k ens1f0 | grep -E "scatter|lro|gro|gso|tso"
# scatter-gather: on
# tcp-segmentation-offload: on
# generic-segmentation-offload: on
# large-receive-offload: off[fixed]   # LRO 必须关闭
# generic-receive-offload: on
```

### 6.4 MTU 全网一致性核查脚本

```bash
#!/bin/bash
# mtu_check.sh - 检查 RoCE 全网 MTU 一致性
# 环境: DOCA-OFED 24.10（mlx5_core 驱动）/ bash 4.x+
# 预期 MTU: 9000（以太帧 9018 含 VLAN tag）

EXPECTED_MTU=9000
NODES_FILE="/etc/gpu_cluster_nodes"  # 每行一个 IP 或 hostname
REPORT=""
FAIL=0

echo "===== MTU Consistency Check $(date) ====="
echo "Expected MTU: $EXPECTED_MTU"
echo ""

# 检查本机所有 RoCE 网卡
echo "--- Local interfaces ---"
for iface in $(ls /sys/class/net/ | grep -v lo); do
    driver=$(readlink /sys/class/net/$iface/device/driver 2>/dev/null | xargs basename 2>/dev/null)
    if [ "$driver" = "mlx5_core" ]; then
        mtu=$(cat /sys/class/net/$iface/mtu)
        operstate=$(cat /sys/class/net/$iface/operstate)
        if [ "$mtu" -ne "$EXPECTED_MTU" ]; then
            echo "  ⚠ $iface: MTU=$mtu (expected $EXPECTED_MTU) [$operstate] MISMATCH!"
            FAIL=1
        else
            echo "  ✓ $iface: MTU=$mtu [$operstate]"
        fi
    fi
done

echo ""

# 远程检查集群其他节点
if [ -f "$NODES_FILE" ]; then
    echo "--- Remote nodes ---"
    while IFS= read -r node; do
        [ -z "$node" ] || [[ "$node" == \#* ]] && continue
        result=$(ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$node" \
            'for iface in $(ls /sys/class/net/ | grep -v lo); do
                driver=$(readlink /sys/class/net/$iface/device/driver 2>/dev/null | xargs basename 2>/dev/null)
                if [ "$driver" = "mlx5_core" ]; then
                    mtu=$(cat /sys/class/net/$iface/mtu)
                    echo "$iface:$mtu"
                fi
            done' 2>/dev/null)

        if [ -z "$result" ]; then
            echo "  ✗ $node: UNREACHABLE"
            FAIL=1
            continue
        fi

        echo "$result" | while IFS=: read -r iface mtu; do
            if [ "$mtu" -ne "$EXPECTED_MTU" ]; then
                echo "  ⚠ $node/$iface: MTU=$mtu MISMATCH!"
            else
                echo "  ✓ $node/$iface: MTU=$mtu"
            fi
        done
    done < "$NODES_FILE"
fi

echo ""
if [ "$FAIL" -eq 1 ]; then
    echo "RESULT: MTU MISMATCH DETECTED - fix before running NCCL workloads"
    exit 1
else
    echo "RESULT: All interfaces MTU=$EXPECTED_MTU ✓"
    exit 0
fi
```

---

## 7. RoCE 典型故障

### 7.1 PFC 风暴

**症状**：
- NCCL 带宽骤降或训练 hang 住
- `ethtool -S` 看到 PFC pause 计数暴涨
- 交换机端口 PFC 队列深度持续高位

**定位命令**：

```bash
# 1. 查看网卡 PFC 计数
ethtool -S ens1f0 | grep -i pfc
# 示例输出：
#     rx_pfc_paused_packets: 1234567     <-- 收到 PFC pause 帧
#     tx_pfc_paused_packets: 0           <-- 发送 PFC pause 帧
#     rx_pfc_pause_transition_packets: 0
#     tx_pfc_pause_transition_packets: 0
#     rx_pfc_0_pause_packets: 0
#     rx_pfc_3_pause_packets: 1234567    <-- priority 3 的 pause（RoCE 流量）
#     tx_pfc_3_pause_packets: 0

# 2. 持续监控（每5秒）
watch -n 5 'ethtool -S ens1f0 | grep -i pfc'

# 3. 交换机侧查看（华为 CE，V200R022 示例）
# display qos pfc statistics interface 100GE1/0/1

# 4. 定位发送 pause 的设备
# 在交换机上：
# display qos pfc statistics interface all
# 找到 tx_pfc_paused_packets > 0 的端口，其下游设备在发送 pause
```

**处置**：
1. 确认 ECN 阈值 < PFC 阈值（ECN 先于 PFC 触发）
2. 检查是否有非 RoCE 流量抢占了 RoCE 优先级的 buffer
3. 临时缓解：降低 RoCE 流量的 buffer 占比或增加 buffer 总量
4. 检查是否存在 PFC 死锁（见下节）

### 7.2 PFC 死锁原理与预防

**原理**：当网络中形成环形路径且多个节点同时发 PFC pause，可能出现所有节点都在等对方停止 pause 的死锁。常见于：
- 多路径路由（ECMP）下流量不对称
- 交换机 buffer 分配不当

**预防**：
```text
# 交换机侧：启用 PFC 死锁检测
# 华为 CE（V200R022 示例；V200R019 及之前版本命令语法不同，请用 ? 查看可用参数）
qos pfc deadlock detect enable
qos pfc deadlock detect timer 200      # 检测间隔 200ms
qos pfc deadlock recover timer 300     # 恢复间隔 300ms

# Cumulus 5.x / Spectrum（Cumulus 4.x 无 pfc_deadlock_detect 参数，需手动配置 watchdog）
# 在 /etc/cumulus/switchd.conf 中
switchd.pfc_deadlock_detect = true
switchd.pfc_deadlock_timer = 200
```

### 7.3 ECN 阈值配反的后果

- ECN min_threshold > PFC threshold → PFC 先触发 → PAUSE 风暴 → 性能下降
- ECN min_threshold 过低 → 过早触发拥塞通知 → 发送端频繁降速 → 带宽利用率低
- 正确比例参考：ECN min : ECN max : PFC trigger ≈ 1 : 5 : 8

### 7.4 Pause 帧计数排查

```bash
# 查看传统 PAUSE 帧（全优先级暂停，非 PFC）
ethtool -S ens1f0 | grep -i pause
#     rx_pause: 0
#     tx_pause: 0

# 如果 rx_pause > 0 但 PFC 未配置，说明对端在用全局 PAUSE
# 全局 PAUSE 会暂停所有优先级，严重影响性能

# 查看每优先级 PFC 计数
ethtool -S ens1f0 | grep "pfc_[0-9]_pause"
#     rx_pfc_0_pause_packets: 0
#     rx_pfc_1_pause_packets: 0
#     rx_pfc_2_pause_packets: 0
#     rx_pfc_3_pause_packets: 45678     <-- 只有 priority 3 被 pause
#     ...

# 对比两个时间点的增量
prev=$(ethtool -S ens1f0 | grep "rx_pfc_3_pause_packets" | awk '{print $2}')
sleep 10
curr=$(ethtool -S ens1f0 | grep "rx_pfc_3_pause_packets" | awk '{print $2}')
echo "PFC pause rate: $(( (curr - prev) / 10 )) per second"
```

---

## 8. GPUDirect RDMA

### 8.1 原理

```text
传统路径：
  GPU显存 → PCIe → CPU内存 → PCIe → HCA → 网络
           (GPU→CPU copy)   (CPU→HCA copy)

GPUDirect RDMA 路径：
  GPU显存 → PCIe Switch → HCA → 网络
           (DMA bypass CPU, zero-copy)

关键要求：GPU 和 HCA 必须在同一个 PCIe switch 下（或 PCIe root complex 支持 P2P DMA）
```

### 8.2 依赖与验证

> GPUDirect RDMA 依赖 **nvidia-peermem** 模块（NVIDIA 驱动 R465+ 内置，替代旧版 nv_peer_mem）。
> 若系统中仍有旧 `nv_peer_mem` 模块（R460 及更早），需卸载后改用 `nvidia-peermem`。
> 内核 ≥5.13 还可使用 dmabuf 路径（无需 peermem 模块），但 NCCL 2.21+ 仍优先使用 peermem。

```bash
# 1. 确认 nvidia-peermem 模块加载
lsmod | grep peermem
# nvidia_peermem         16384  0
# ib_core               344064  9 rdma_cm,ib_ipoib,nvidia_peermem,...

# 如果未加载：
modprobe nvidia-peermem
# 永久加载
echo "nvidia-peermem" > /etc/modules-load.d/nvidia-peermem.conf

# 2. 确认 GPU 和 HCA 在同一 PCIe switch
nvidia-smi topo -m
# 期望输出：GPU 和对应的 mlx5_* 之间显示 PXB 或 PHB（同 switch）
# 如果显示 SYS = 跨 NUMA node，GDR 可能不生效或性能差

lspci -tv | grep -E "mlx5|nvidia"
# 确认 GPU 和 HCA 挂在同一个 PCIe bridge 下

# 3. 验证 GDR 是否工作
# 安装 gdrcopy 测试工具
git clone https://github.com/NVIDIA/gdrcopy.git
cd gdrcopy && make && make install

# 运行 sanity test
./sanity -v
# 期望看到 "GPU memory registration: PASS" 和 "GPUDirect RDMA: PASS"

# 4. NCCL 测试对比（开/关 GDR）
# 开启 GDR
NCCL_NET_GDR_LEVEL=5 NCCL_DEBUG=INFO ./all_reduce_perf -b 8 -e 2G -f 2 -g 1

# 关闭 GDR
NCCL_NET_GDR_LEVEL=0 NCCL_DEBUG=INFO ./all_reduce_perf -b 8 -e 2G -f 2 -g 1

# 对比 busbw：开启 GDR 应比关闭高 10-30%（HDR/NDR 环境）
```

### 8.3 NCCL_NET_GDR_LEVEL 各值含义

| 值 | 含义 | 说明 |
|---|------|------|
| 0 | 禁用 GDR | 所有 RDMA 走 bounce buffer（经 CPU 内存） |
| 1 | NVLink 同 switch | 仅 GPU 与 HCA 通过 NVLink switch 连接时 |
| 2 | PCIe 同 switch | GPU 与 HCA 在同一 PCIe switch 下 |
| 3 | PCIe 同 host bridge | 同一 PCIe root complex |
| 4 | 同 NUMA node | GPU 和 HCA 在同一 NUMA node |
| 5 | 同 socket | GPU 和 HCA 在同一 CPU socket |
| 6 | 跨 socket | 允许跨 socket 使用 GDR（可能有性能损失） |

**推荐**：`NCCL_NET_GDR_LEVEL=5`（同 socket 即启用），大多数 DGX 类机器 GPU-HCA 同 socket。

### 8.4 GDR 不生效常见原因

| 原因 | 排查方法 | 解决 |
|------|---------|------|
| nvidia-peermem 未加载 | `lsmod \| grep peermem` | `modprobe nvidia-peermem` |
| GPU-HCA 不在同 PCIe switch | `nvidia-smi topo -m` | 调整 NCCL_NET_GDR_LEVEL 或换插槽 |
| IOMMU 阻止 P2P DMA | `dmesg \| grep IOMMU` | BIOS 关闭 IOMMU 或设 `iommu=pt` |
| ACS（Access Control Services）阻止 | `lspci -vvv \| grep ACS` | `setpci -s <BDF> ECAP_ACS+6=0` |
| 驱动版本不匹配 | `nvidia-smi` vs `ofed_info` | 确认 nvidia 驱动 R550+（生产分支）+ DOCA-OFED 24.10；旧版参考：≥R525 + OFED ≥5.9 |
| BAR1 空间不足 | `nvidia-smi -q \| grep BAR1` | 极少见，需 GPU 固件更新 |

---

## 9. NCCL 深入

### 9.1 集合通信算法

#### Ring AllReduce

```text
节点数 N，每个节点一个 rank（简化为 1 GPU/节点）：

Step 1: Reduce-Scatter（N-1 步）
  每个 rank 将自己的数据分成 N 份，逐步传给邻居并累加
  最终每个 rank 拥有 1/N 的完整 reduce 结果

Step 2: All-Gather（N-1 步）
  每个 rank 将自己的 1/N 结果广播给所有 rank

总通信量: 2×(N-1)/N × DataSize
带宽效率: 接近理论最优 2(N-1)/N → 当 N→∞ 时接近 2

适合: 大消息（>1MB），节点数适中
```

#### Tree AllReduce

```text
构建二叉树（或多叉树）：

Step 1: Reduce（叶子→根，log₂N 步）
  每个内部节点接收子节点数据并累加，向上传递

Step 2: Broadcast（根→叶子，log₂N 步）
  根节点将最终结果向下广播

总通信量: 2 × DataSize（与节点数无关！）
延迟: O(log N)

适合: 小消息、大节点数
缺点: 根节点是瓶颈，带宽利用率不如 Ring
```

#### NCCL 的选择策略

- NCCL 默认同时使用 Ring + Tree 的混合策略
- 大消息（>512KB）倾向 Ring
- 小消息（<64KB）倾向 Tree
- 跨节点通信倾向 Tree（减少跳数）
- 节点内通信倾向 Ring（利用 NVLink 高带宽）

### 9.2 NCCL 环境变量完整详解表

> 基线版本：**NCCL 2.26.x（CUDA 12.8）/ NCCL 2.21.x（CUDA 12.4）**。
> 关键版本变化：① NCCL_IB_ROCE_VERSION_NUM（NCCL 2.17+）可自动选择 RoCE v2 GID，减少手动指定 NCCL_IB_GID_INDEX 的需求；② TORCH_NCCL_ASYNC_ERROR_HANDLING 是 PyTorch 2.2+ 对 NCCL_ASYNC_ERROR_HANDLING（NCCL 2.14+ 引入）的改名，默认启用；③ NVLS 算法 NCCL 2.13+ 引入，NVLSTree NCCL 2.19+；④ NCCL_DEBUG_SUBSYS 的 GDRDMA 子系统 NCCL 2.19+ 新增。

| 变量名 | 作用 | 取值/默认 | 排障用法 |
|-------|------|----------|---------|
| NCCL_DEBUG | 日志级别 | VERSION/INFO/WARN/TRACE | 设 INFO 查看初始化详情 |
| NCCL_DEBUG_SUBSYS | 日志子系统过滤 | INIT,NET,GRAPH,TUNES,COLL,P2P,SHM,ALLOC,NVLS,GDRDMA,ALL | 如 `NCCL_DEBUG_SUBSYS=NET,INIT` 只看网络和初始化；GDRDMA 子系统 NCCL 2.19+ 新增 |
| NCCL_SOCKET_IFNAME | 指定用于 bootstrap 的网卡 | 网卡名如 `eth0,ens1` | 多网卡时指定正确网段；用 `=^docker,veth` 排除 |
| NCCL_IB_HCA | 指定使用的 IB HCA | `mlx5_0,mlx5_1` 或 `=mlx5_0:1`（端口） | 指定 rail 对应的 HCA |
| NCCL_IB_DISABLE | 禁用 IB 网络 | 0/1 | 设 1 强制走 socket（调试用） |
| NCCL_IB_GID_INDEX | RoCE GID table 索引 | 数字（通常 2 或 3） | RoCE v2 IPv4 通常 =2；NCCL 2.17+ 可改用 NCCL_IB_ROCE_VERSION_NUM=2 自动选择，省略手动 GID_INDEX |
| NCCL_IB_TC | IB Traffic Class | 0-255（默认 0） | RoCE 设 106（DSCP 25→TC 106） |
| NCCL_IB_SL | IB Service Level | 0-15（默认 0） | 对应交换机 PFC 优先级 |
| NCCL_IB_TIMEOUT | IB QP 超时 | 0-23（默认 22，=67秒） | 设小（如 14）快速检测链路故障 |
| NCCL_P2P_DISABLE | 禁用 GPU 间 P2P | 0/1 | 设 1 排查 NVLink 问题 |
| NCCL_SHM_DISABLE | 禁用共享内存通道 | 0/1 | 设 1 排查同机 GPU 通信问题 |
| NCCL_ALGO | 强制算法 | Ring/Tree/NVLS/NVLSTree | 调试时固定算法对比 |
| NCCL_PROTO | 强制协议 | Simple/LL/LL128 | LL128 延迟最优，Simple 带宽最优 |
| NCCL_MIN_NCHANNELS | 最少 channel 数 | 默认自动 | 增大可提升带宽但增加延迟 |
| NCCL_MAX_NCHANNELS | 最多 channel 数 | 默认自动 | 减小降低 CPU 开销 |
| NCCL_BUFFSIZE | 每 channel buffer 大小 | 默认 4194304（4MB） | 大消息增大到 8MB/16MB 提升带宽 |
| NCCL_NTHREADS | 每 channel CUDA 线程数 | 默认 256/512 | 一般不动 |
| NCCL_SOCKET_NTHREADS | socket 线程数 | 默认自动 | bootstrap 阶段调优 |
| NCCL_TIMEOUT | 操作超时（秒） | 默认 1800（30分钟） | 调试设小值如 60 |
| NCCL_CROSS_NIC | 允许跨 NIC 通信 | 0/1 | Rail-optimized 拓扑设 0 |
| NCCL_NET_GDR_LEVEL | GDR 启用阈值 | 0-6（见上节） | 设 5 启用同 socket GDR |
| NCCL_TOPO_FILE | 自定义拓扑文件 | XML 路径 | 非标准机器提供拓扑提示 |

### 9.3 NCCL_DEBUG=INFO 日志解读

> 以下日志格式基于 **NCCL 2.2x（2.21/2.26）** 输出。NCCL 2.20+ 的日志格式相比 2.1x 有调整（如 Trees 行格式、NVLS channel 输出），早期版本可能缺少 GDRDMA 和 NVLS 相关行。

```text
# 运行命令
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET,GDRDMA ./all_reduce_perf -b 8 -e 2G -f 2 -g 8

# 关键日志逐行解读：

# ---- Bootstrap 阶段 ----
NCCL INFO Bootstrap: Using ens1f0:10.0.0.58<0>
# → 使用 ens1f0 网卡做 bootstrap 通信（TCP 建连）

NCCL INFO NET/IB: Using [0]mlx5_0:1/IB/PCIE; [1]mlx5_1:1/IB/PCIE; ...
# → 发现可用的 IB HCA 列表，[0]=mlx5_0 端口 1

NCCL INFO NET/IB: [0] mlx5_0/IB  port 1 LID 21 rate 200
# → HCA mlx5_0 端口 1 的 LID=21，速率 200Gbps（HDR）

# ---- GDRDMA 检测 ----
NCCL INFO GDRDMA: Enabled on mlx5_0, GPU 0 (GPU Direct RDMA)
# → GPUDirect RDMA 在 mlx5_0 + GPU0 上启用成功

# ---- 拓扑发现 ----
NCCL INFO Channel 00/04: 0[0] -> 1[1] via P2P/unknown/unroll
NCCL INFO Channel 00/04: 1[1] -> 2[2] via P2P/unknown/unroll
# → 建立 P2P channel，0→1→2 通过 NVLink

# ---- Ring/Tree 构建 ----
NCCL INFO Ring 00: 0[0] -> 1[1] -> 2[2] -> 3[3] -> 4[4] -> 5[5] -> 6[6] -> 7[7] -> 0[0]
# → 8 GPU 的 ring 拓扑，rank 0→1→2→...→7→0

NCCL INFO Trees [0] 7/5/4/3/2/1/0->6->root|6->0/1/2/3/4/5/7 [1] 6/4/5/2/3/1/0->7->root|7->0/1/2/3/4/5/6
# → Tree 拓扑结构，根节点和分支

# ---- 算法选择 ----
NCCL INFO Using network IB
NCCL INFO Channel 00/04: 0[0] -> 1[1] via NET/IB/0
# → 跨节点通信使用 IB 网络

NCCL INFO nChannels set to 4/4 (algo Ring/Tree)
# → Ring 和 Tree 各使用 4 个 channel

# ---- 性能参数 ----
NCCL INFO Setting buffsize to 4194304
# → 每 channel buffer 4MB
```

---

## 10. NCCL 排障决策树

### 10.1 Init Hang（初始化卡住）

```text
症状：程序在 NCCL init 阶段挂住，无任何后续日志

排查步骤：
├─ 1. 确认 bootstrap 网络
│   ├─ NCCL_DEBUG=INFO 看 "Bootstrap: Using ..." 行
│   ├─ 确认该网卡 IP 可达：ping <对端IP> -I <网卡名>
│   └─ 检查防火墙：iptables -L -n | grep <NCCL端口范围>
│       └─ 修复：开放 1024-65535 TCP 或指定 NCCL_COMM_ID
│
├─ 2. 检查 IB 链路
│   ├─ ibstat（所有端口 Active？）
│   ├─ sminfo（SM 运行中？）
│   └─ ibping 测试对端：ibping -G <对端GUID>
│       └─ 不通 → 检查 LID 分配、PKey 匹配
│
├─ 3. 检查 NCCL_SOCKET_IFNAME
│   ├─ 多网卡环境必须指定正确网卡
│   └─ 验证：NCCL_SOCKET_IFNAME=eth0 NCCL_DEBUG=INFO ...
│
├─ 4. 检查端口冲突
│   ├─ K8s 环境下 hostNetwork=false 时端口映射问题
│   └─ 修复：设置 NCCL_COMM_ID=<主节点IP:端口>
│
└─ 5. 降低超时快速报错
    └─ NCCL_TIMEOUT=60 重跑，60s 后会报具体错误
```

### 10.2 中途 Timeout

```text
症状：训练运行一段时间后报 "NCCL WARN: Timeout"

排查步骤：
├─ 1. 查看是哪个 rank 报的 timeout
│   └─ 检查各 rank 的 GPU 利用率：nvidia-smi
│       ├─ 某 rank GPU util=0% → 该 rank 可能在等 IO 或 crash
│       └─ 所有 rank GPU util 正常 → 可能是通信问题
│
├─ 2. 检查 IB 链路状态变化
│   ├─ dmesg | grep -i "mlx5\|link"
│   │   └─ 看到 "Link down" → 硬件问题
│   ├─ perfquery -G <GUID> 1 | grep -E "SymbolError|LinkDowned"
│   │   └─ 错误计数增长 → 线缆/光模块问题
│   └─ ibstat → 确认所有端口仍 Active
│
├─ 3. 检查 PFC 风暴
│   ├─ ethtool -S ens1f0 | grep pfc
│   │   └─ pause 计数暴涨 → PFC 风暴
│   └─ 处置：检查交换机 ECN/PFC 配置
│
├─ 4. 检查慢节点（straggler）
│   ├─ 各节点 nvidia-smi 对比温度/频率
│   ├─ 高温降频 → 散热问题
│   └─ ECC 错误 → GPU 硬件问题
│
└─ 5. 增大 NCCL_TIMEOUT 观察
    └─ NCCL_TIMEOUT=3600 → 如果最终能完成，说明是性能问题而非故障
```

### 10.3 带宽不达标

```text
症状：nccl-tests busbw 远低于预期

排查步骤：
├─ 1. 确认链路速率
│   ├─ ibstat | grep Rate → 确认 200（HDR）/400（NDR）
│   ├─ 低于预期 → 检查线缆（是否用了低速线缆）
│   └─ 检查交换机端口配置（是否限速）
│
├─ 2. 确认 GDR 是否生效
│   ├─ NCCL_DEBUG=INFO 看 "GDRDMA: Enabled"
│   ├─ 未启用 → 排查 GDR 依赖（见第8节）
│   └─ GDR 不生效通常损失 10-30% 带宽
│
├─ 3. 确认 MTU 一致
│   ├─ 运行 mtu_check.sh（见 6.4 节）
│   └─ MTU 不一致 → 大包被丢弃/分片 → 带宽骤降
│
├─ 4. 检查 channel 数和 buffer
│   ├─ NCCL_DEBUG=INFO 看 "nChannels set to"
│   ├─ channel 过少（<4）→ 增大 NCCL_MIN_NCHANNELS
│   └─ buffer 过小 → 增大 NCCL_BUFFSIZE=8388608
│
├─ 5. 检查拓扑和算法
│   ├─ NCCL_DEBUG=INFO 看 "Ring" 和 "Trees" 行
│   ├─ 跨节点走了 socket 而非 IB → 检查 NCCL_IB_DISABLE
│   └─ 尝试 NCCL_ALGO=Ring 单独测试
│
├─ 6. 排除 CPU/NUMA 瓶颈
│   ├─ numactl --hardware → 确认 GPU 所在 NUMA node
│   ├─ 跨 NUMA 通信 → 绑核到 GPU 同 NUMA node
│   └─ numactl --cpunodebind=0 --membind=0 ...
│
└─ 7. 对比基线（见第11节基线表）
    └─ 低于基线 20% 以上 → 硬件问题，逐节点排查
```

### 10.4 单 Rank 崩溃

```text
症状：某 rank 进程被 kill 或 segfault

排查步骤：
├─ 1. 检查 XID 错误
│   ├─ dmesg | grep -i "xid"
│   ├─ XID 79/95 → GPU 掉卡（见监控分册）
│   ├─ XID 48 → ECC DBE → GPU 硬件故障
│   └─ XID 63/64 → page fault → 可能是 OOM 或驱动 bug
│
├─ 2. 检查 OOM
│   ├─ dmesg | grep -i "oom\|killed"
│   ├─ nvidia-smi → 看显存使用
│   └─ 其他 rank 正常但一个 OOM → 数据不均
│
├─ 3. 检查 core dump
│   ├─ coredumpctl list
│   ├─ gdb <程序> <core> → bt（查看调用栈）
│   └─ 崩溃在 NCCL 库内 → 收集 NCCL_DEBUG=TRACE 日志
│
├─ 4. GPU 硬件诊断
│   ├─ nvidia-smi -q -d ECC → 看 DBE 计数
│   ├─ dcgmi diag -r 3 -g <GPU_ID> → Level 3 诊断
│   └─ 硬件故障 → 隔离节点（cordon + drain）
│
└─ 5. 检查驱动一致性
    ├─ 所有节点 nvidia-smi 对比版本
    ├─ 版本不一致 → NCCL 可能 crash
    └─ 修复：统一驱动版本后重启
```

---

## 11. nccl-tests 进阶

> nccl-tests 编译依赖组合（基线）：**CUDA 12.8 + NCCL 2.26.x + OpenMPI 4.1.x**（或 CUDA 12.4 + NCCL 2.21.x）。
> 编译命令：`make MPI=1 MPI_HOME=/opt/openmpi CUDA_HOME=/usr/local/cuda NCCL_HOME=/usr/local`
> git checkout 钉 commit，避免 master 漂移。

### 11.1 全参数说明

```bash
./all_reduce_perf [options]

# 核心参数
-b, --minbytes <size>    # 最小消息大小（默认 32M）
-e, --maxbytes <size>    # 最大消息大小（默认 32M）
-f, --stepfactor <n>     # 步长因子（默认 2，即翻倍）
-g, --nGpus <n>          # 每进程 GPU 数
-n, --nWarmup <n>        # 预热轮次（默认 5）
-w, --nIters <n>         # 测量轮次（默认 20）
-t, --nThreads <n>       # CUDA 线程数（默认 512）
-c, --check <0|1>        # 结果校验（默认 1=开启）
-z, --blocking <0|1>     # 阻塞模式（默认 0=非阻塞）

# 进阶参数
-T, --timeout <sec>      # 超时（默认 0=不限）
-G, --timing <0|1|2|3>   # 计时方式（0=elapsed, 1=cuda event, 2=cuda graph, 3=auto）
-o, --op <op>            # 操作类型：sum/prod/min/max/avg/mulsum
-d, --datatype <type>    # 数据类型：int8/uint32/int32/int64/uint64/half/float/double
-r, --redop <op>         # 归约操作：sum/prod/min/max/avg
-a, --average <0|1>      # 输出平均带宽（1）还是每个 rank 的带宽（0）

# 环境变量
NCCL_DEBUG=INFO          # 查看详细初始化
NCCL_ALGO=Ring           # 固定算法
NCCL_PROTO=Simple        # 固定协议
NCCL_BUFFSIZE=8388608    # buffer 大小
```

### 11.2 busbw 计算公式推导

```text
AllReduce 通信量分析：

对于 Ring AllReduce，N 个 rank，数据量 M：

Step 1 - ReduceScatter:
  每个 rank 发送 (N-1)/N × M 数据给下一个 rank
  总发送量 = N × (N-1)/N × M = (N-1) × M

Step 2 - AllGather:
  每个 rank 发送 1/N × M 给下一个 rank，共 N-1 步
  总发送量 = N × (N-1)/N × M = (N-1) × M

总通信量 = 2 × (N-1) × M

理论最短时间 T = 总通信量 / 链路带宽 B
T = 2(N-1)M / B

busbw（总线带宽）= 有效数据量 / 实际耗时
= M / T_actual

当 busbw 接近 B × N/(2(N-1)) 时，说明通信效率接近理论最优

示例：
  N=8 GPU, M=1GB, B=200Gbps (HDR)
  理论 busbw = 200 × 8/(2×7) = 200 × 0.571 = 114.3 Gbps
  实际 busbw 约 100-110 Gbps 即为健康

  N=2 (双机), M=1GB
  理论 busbw = 200 × 2/(2×1) = 200 Gbps
  实际 busbw 约 180-195 Gbps（双机直连，效率最高）
```

### 11.3 各测试项用途

| 测试项 | 命令 | 典型用途 | 说明 |
|-------|------|---------|------|
| all_reduce | all_reduce_perf | 综合性能基线 | 最常用的 allreduce 测试 |
| all_gather | all_gather_perf | 大模型并行（tensor parallel） | 每个 rank 收集所有 rank 的数据片段 |
| reduce_scatter | reduce_scatter_perf | FSDP/ZeRO-3 通信 | reduce + 分发 |
| reduce | reduce_perf | 梯度同步前半段 | 只 reduce 不 broadcast |
| broadcast | broadcast_perf | 参数广播 | 单源广播 |
| alltoall | alltoall_perf | MoE 专家通信 | 每个 rank 给每个 rank 发不同数据 |
| send/recv | sendrecv_perf | 点对点基线 | 单对单带宽测试 |

### 11.4 基线表（参考值，实际受配置影响）

> **版本条件说明**：以下基线值为 **R550 驱动 + NCCL 2.21.x（CUDA 12.4）+ DOCA-OFED 24.10** 环境下实测参考值。
> R570 + NCCL 2.26.x（CUDA 12.8）环境可能高出 5-10%（驱动优化和 NCCL 算法改进）。
> 实际 busbw 受 MTU、PFC/ECN 调优、GDR 状态、NUMA 绑核等因素影响，以自家集群验收测试为准。

#### A100 SXM4 80GB + HDR 200Gbps

| 场景 | 测试项 | 消息大小 | 期望 busbw |
|------|-------|---------|-----------|
| 单机 8 GPU（NVLink） | all_reduce | 2GB | 250-280 GB/s |
| 单机 8 GPU（NVLink） | all_reduce | 512MB | 230-260 GB/s |
| 单机 8 GPU（NVLink） | all_reduce | 64MB | 150-200 GB/s |
| 双机（HDR 直连） | all_reduce | 2GB | 22-24 GB/s |
| 双机（HDR 直连） | all_reduce | 512MB | 22-24 GB/s |
| 双机（HDR 直连） | sendrecv | 2GB | 24-25 GB/s |

#### H100 SXM5 80GB + NDR 400Gbps

| 场景 | 测试项 | 消息大小 | 期望 busbw |
|------|-------|---------|-----------|
| 单机 8 GPU（NVLink4） | all_reduce | 2GB | 450-500 GB/s |
| 单机 8 GPU（NVLink4） | all_reduce | 512MB | 400-460 GB/s |
| 双机（NDR 直连） | all_reduce | 2GB | 44-48 GB/s |
| 双机（NDR 直连） | all_reduce | 512MB | 44-48 GB/s |
| 双机（NDR 直连） | sendrecv | 2GB | 48-50 GB/s |

### 11.5 逐对节点矩阵测试脚本思路

```bash
#!/bin/bash
# nccl_matrix_test.sh - 逐对节点 NCCL 带宽矩阵测试
# 环境: CUDA 12.8 + NCCL 2.26.x + OpenMPI 4.1.x / DOCA-OFED 24.10
# 用法: ./nccl_matrix_test.sh <nodes_file> <output_csv>
# nodes_file 每行一个 hostname/IP

NODES_FILE="$1"
OUTPUT="${2:-nccl_matrix.csv}"
NCCL_TESTS_DIR="/opt/nccl-tests/build"
MSG_SIZE="1G"
WARMUP=5
ITERS=20

mapfile -t NODES < <(grep -v '^#' "$NODES_FILE" | grep -v '^$')
N=${#NODES[@]}

echo "Testing $N nodes: ${NODES[*]}"
echo "Output: $OUTPUT"

# CSV 表头
echo -n "node_pair" > "$OUTPUT"
for j in "${NODES[@]}"; do
    echo -n ",$j" >> "$OUTPUT"
done
echo "" >> "$OUTPUT"

# 逐对测试
for ((i=0; i<N; i++)); do
    echo -n "${NODES[$i]}" >> "$OUTPUT"
    for ((j=0; j<N; j++)); do
        if [ "$i" -eq "$j" ]; then
            echo -n ",N/A" >> "$OUTPUT"
            continue
        fi

        echo -n "  Testing ${NODES[$i]} <-> ${NODES[$j]} ..."

        # 在 node_i 上启动 mpirun
        result=$(ssh "${NODES[$i]}" "
            mpirun -np 2 \
                -H ${NODES[$i]}:1,${NODES[$j]}:1 \
                -x NCCL_DEBUG=WARN \
                -x NCCL_IB_HCA=mlx5_0 \
                -x NCCL_IB_GID_INDEX=2 \
                --bind-to numa \
                ${NCCL_TESTS_DIR}/sendrecv_perf \
                -b ${MSG_SIZE} -e ${MSG_SIZE} -f 1 \
                -w ${WARMUP} -n ${ITERS} -g 1 2>&1 | \
            grep 'Avg bus bandwidth' | awk '{print \$NF}'
        " 2>/dev/null)

        if [ -z "$result" ]; then
            echo -n ",FAIL" >> "$OUTPUT"
            echo " FAIL"
        else
            echo -n ",${result}" >> "$OUTPUT"
            echo " ${result} GB/s"
        fi
    done
    echo "" >> "$OUTPUT"
done

echo ""
echo "Matrix test complete. Results in $OUTPUT"
echo ""
echo "--- Identifying outliers (< 80% of median) ---"

# 提取所有数值，计算中位数
python3 -c "
import csv, statistics
with open('$OUTPUT') as f:
    reader = csv.reader(f)
    header = next(reader)
    values = []
    pairs = []
    for row in reader:
        for k, v in enumerate(row[1:], 1):
            try:
                val = float(v)
                values.append(val)
                pairs.append((row[0], header[k], val))
            except ValueError:
                pass
    median = statistics.median(values)
    threshold = median * 0.8
    print(f'Median busbw: {median:.2f} GB/s')
    print(f'Outlier threshold (80%): {threshold:.2f} GB/s')
    print('Outlier pairs:')
    for src, dst, val in pairs:
        if val < threshold:
            print(f'  {src} -> {dst}: {val:.2f} GB/s ({val/median*100:.1f}% of median)')
" 2>/dev/null || echo "(python3 not available for outlier analysis)"
```

---

> **本章小结**：IB/RoCE/NCCL 是 GPU 集群性能的命脉。核心排查路径：`物理层（线缆/光模块）→ 链路层（PFC/ECN）→ 网络层（SM/路由）→ 传输层（NCCL 配置）→ 应用层（通信算法/数据分片）`。每层都有对应的命令和指标，逐层排除即可定位问题。
