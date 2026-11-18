---
title: "GPU 集群运维（08）：故障排查与案例库"
date: 2026-11-14T09:00:00+08:00
draft: false
description: "排查方法论、日志源大全表、XID 完整手册、ECC 与显存健康判读、GPU 掉卡（XID 79）专题、性能类排查、K8s 层 GPU 故障案例、经典案例库与应急工具箱。"
tags: ["排障", "XID", "案例"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下为本书全部命令/脚本的版本上下文，详见 [00-版本基线](./00-版本基线.md)。

| 维度 | 基线版本 |
|---|---|
| OS | Ubuntu 22.04.5 LTS (kernel 5.15) 或 Rocky Linux 9.5 (kernel 5.14) |
| NVIDIA Driver | R570 570.124.04（示例）/ R550 550.127.08（示例，Ampere 存量） |
| CUDA Toolkit | 12.8（Hopper/Blackwell）/ 12.4（Ampere 存量） |
| DCGM | 3.3.9 / 4.2.x（dcgmi 命令行为随版本变化） |
| Kubernetes | 1.31.x（kubectl 与集群版本偏差 ≤1 minor） |
| containerd | 1.7.x LTS |
| etcd | 3.5.15+（etcdctl 3.5.x，3.5 默认 v3 API） |
| DOCA-OFED | 24.10 |
| GSP 固件 | 随驱动打包，R515+ 默认启用（XID 119/120 为 GSP 相关，自 R515+ 引入） |
| nvidia-container-toolkit | v1.17.4 |
| nvidia-bug-report.sh | 随驱动安装（R550+ 输出含 GSP 固件与 NVLink 4/5 详情） |

---

## 目录

- [1. 排查方法论](#1-排查方法论)
- [2. 日志源大全表](#2-日志源大全表)
- [3. XID 完整手册](#3-xid-完整手册)
- [4. ECC 与显存健康判读](#4-ecc-与显存健康判读)
- [5. GPU 掉卡（XID 79）专题](#5-gpu-掉卡xid-79专题)
- [6. 性能类排查](#6-性能类排查)
- [7. K8s 层 GPU 故障案例](#7-k8s-层-gpu-故障案例)
- [8. 经典案例库](#8-经典案例库)
- [9. 应急工具箱](#9-应急工具箱)

---

## 1. 排查方法论

### 1.1 分层定位模型

```text
┌─────────────────────────────────────────────────────────────┐
│  第 6 层：作业层    训练代码 / 框架 / 超参数                    │
├─────────────────────────────────────────────────────────────┤
│  第 5 层：容器/K8s  Pod / Device Plugin / 资源配额             │
├─────────────────────────────────────────────────────────────┤
│  第 4 层：驱动层    NVIDIA Driver / CUDA Runtime / NCCL        │
├─────────────────────────────────────────────────────────────┤
│  第 3 层：硬件层    GPU 本体 / NVLink / PCIe / 供电 / 散热     │
├─────────────────────────────────────────────────────────────┤
│  第 2 层：网络层    IB/RoCE / 交换机 / 光模块 / MTU / PFC      │
├─────────────────────────────────────────────────────────────┤
│  第 1 层：存储层    并行文件系统 / NVMe / 网络存储               │
└─────────────────────────────────────────────────────────────┘
```

**每层入口命令与日志源：**

| 层级 | 入口命令 | 日志源 |
|---|---|---|
| 作业层 | `nvidia-smi`（看 util/显存）；`py-spy dump pid <PID>`（看 Python 调用栈） | 训练日志（stdout/stderr）、wandb/tensorboard |
| 容器/K8s | `kubectl describe pod`；`kubectl logs`；`kubectl get events` | kubelet 日志、containerd 日志 |
| 驱动层 | `nvidia-smi -q`；`dmesg -T \| grep NVRM` | nvidia-bug-report.sh 输出 |
| 硬件层 | `nvidia-smi -q -d ECC,TEMPERATURE,PERFORMANCE`；`ipmitool sel list` | BMC/IPMI 日志、dmesg AER |

> **版本说明**：`nvidia-smi -q` 的输出字段随驱动分支变化——R550+ 增加了 GSP 固件版本、SRAM/DRAM 分项 ECC 等字段。`nvidia-bug-report.sh` 输出内容随驱动版本扩展（R550+ 含 NVLink 4/5 详情与 GSP 日志）。`ipmitool sel elist`（扩展列表含时间戳）需 ipmitool 1.8.18+。`dmesg -T` 人类可读时间戳依赖 kernel 5.x+ systemd。
| 网络层 | `ibstat`；`ibdiagnet`；`perfquery` | 交换机管理口日志、OpenSM 日志 |
| 存储层 | `iostat -x 1`；`df -h`；`mount \| grep lustre/gpfs` | 存储集群监控（Ceph/Lustre/GPFS） |

### 1.2 核心排查原则

**原则一：先问变了什么**

故障发生后第一句话问："最近 72 小时做了什么变更？"

```bash
# 快速检查最近变更
kubectl get events --sort-by='.lastTimestamp' --all-namespaces | tail -20
helm history <release> -n <namespace>
git log --oneline --since="72 hours ago"  # GitOps 仓库
journalctl --since="72 hours ago" -u kubelet -u containerd | tail -50
```

**原则二：对比法**

```text
正常节点 vs 异常节点 → 差异即是线索

对比维度：
- 驱动版本：nvidia-smi | grep Driver
- CUDA 版本：nvcc --version
- NCCL 版本：dpkg -l | grep nccl / rpm -qa | grep nccl
- 温度/功耗：nvidia-smi --query-gpu=temperature.gpu,power.draw
- PCIe 带宽：nvidia-smi -q -d PCIE | grep "Tx Throughput"
- IB 速率：ibstat | grep Rate
```

**原则三：保留现场**

重启前必须抓取以下信息（一旦重启，现场丢失）：

```bash
# 重启前必抓清单
# nvidia-bug-report.sh 输出内容随驱动版本变化（R550+ 含 GSP 固件日志与 NVLink 4/5 详情）
dmesg -T > /tmp/dmesg_before_reboot.txt
nvidia-smi -q > /tmp/nvidia-smi_full.txt
nvidia-smi --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total --format=csv > /tmp/gpu_status.csv
ipmitool sel list > /tmp/ipmi_sel.txt
cat /proc/interrupts > /tmp/interrupts.txt
journalctl -b > /tmp/journalctl_boot.txt
```

**原则四：量化验证**

```text
"变慢了" → 具体多少？基线是多少？
"温度高" → 多少度？正常节点多少度？
"带宽差" → 实测值 vs 标称值 vs 基线值
"丢包" → 丢包率多少？哪个计数器在涨？
```

---

## 2. 日志源大全表

| 日志源 | 位置 | 内容 | 采集命令 |
|---|---|---|---|
| dmesg | 内核环形缓冲区 | 内核消息（含 XID、PCIe AER、MCE） | `dmesg -T` / `dmesg -T \| grep -iE 'xid\|nvrm\|aer\|mce\|error'` |
| kern.log | `/var/log/kern.log` | 内核日志持久化 | `grep -i 'nvidia\|gpu\|xid\|aer' /var/log/kern.log \| tail -50` |
| nvidia-bug-report | 生成文件 | NVIDIA 全量诊断包 | `nvidia-bug-report.sh`（输出 `/var/log/nvidia-bug-report.log.gz`） |
| IPMI SEL | BMC 固件 | 硬件事件（供电/温度/风扇/内存） | `ipmitool sel list` / `ipmitool sel elist` |
| kubelet 日志 | journald | K8s 节点代理日志 | `journalctl -u kubelet --since="1 hour ago"` |
| containerd 日志 | journald | 容器运行时日志 | `journalctl -u containerd --since="1 hour ago"` |
| OpenSM 日志 | `/var/log/opensm.log*` | IB 子网管理器事件 | `journalctl -u opensm` / `tail -100 /var/log/opensm.log` |
| Pod Events | K8s API | Pod 调度/生命周期事件 | `kubectl get events -n <ns> --field-selector involvedObject.name=<pod>` |
| NCCL 日志 | stdout / `NCCL_DEBUG=INFO` | NCCL 初始化和通信日志 | 设置 `NCCL_DEBUG=INFO NCCL_DEBUG_FILE=/tmp/nccl_%h.log` |
| 交换机日志 | 交换机管理口 | 端口状态/错误/配置变更 | SSH 登录交换机 `show logging` / `display logbuffer` |
| syslog | `/var/log/syslog` | 系统全量日志 | `grep -i error /var/log/syslog \| tail -50` |
| Xorg/显示 | `/var/log/Xorg.0.log` | 显卡显示相关（通常无关） | 一般不需要 |
| GPU 计数器 | nvidia-smi / dcgmi | ECC/温度/功耗/NVLink 错误 | `dcgmi introspect` / `nvidia-smi -q -d ECC` |

---

## 3. XID 完整手册

### 3.1 XID 全表

> **XID 版本敏感性说明**：不同驱动分支引入的 XID 编号不同。下表中 XID 92/94/95 自较新驱动分支（R418/R465+）引入；XID 119/120 为 GSP 相关，自 R515+ 引入。XID 79 的 AER 伴随日志在较新内核（5.x+）和驱动分支（R535+）中信息更完整。精确的引入版本以 [NVIDIA XID Errors 官方文档](https://docs.nvidia.com/deploy/xid-errors/index.html) 为准。

| XID | 官方含义 | 引入/可见版本 | 常见触发原因 | 是否硬件故障 | 处置步骤 |
|---|---|---|---|---|
| **7** | Invalid or corrupted push buffer stream | 全版本 | CUDA kernel 推送了无效的 command buffer；驱动 bug | 否（通常 SW） | 1. 升级驱动 2. 检查 CUDA 代码是否有越界 3. 复现则报 NVIDIA bug |
| **13** | Graphics Engine exception | 全版本 | GPU 图形引擎异常，通常是 kernel 执行超时 | 否（通常 SW） | 1. 检查是否有 GPU kernel 死循环 2. 增加 timeout 3. 升级驱动 |
| **31** | GPU memory page fault | 全版本 | GPU 访问了未映射的虚拟地址 | 否（通常 SW） | 1. 检查代码是否有野指针/OOB 2. 增大显存分配 3. 检查 MIG 配置 |
| **32** | Invalid or corrupted push buffer stream (variant) | 全版本 | 同 XID 7，push buffer 损坏 | 否 | 同 XID 7 |
| **38** | Driver firmware error | R418+（GSP/SEC2 相关） | NVIDIA 固件（GSP/SEC2）加载或执行失败 | 可能 | 1. 重装驱动 2. 检查 GSP 固件版本 3. 禁用 GSP 测试（`NVreg_EnableGpuFirmware=0`） |
| **43** | GPU has fallen off the bus (variant) | 全版本 | 同 XID 79 的变体 | 是 | 同 XID 79 |
| **45** | Preemptive cleanup, due to pending timeout | 全版本 | 内核抢占清理超时 | 否 | 1. 检查是否有长时间运行的 kernel 2. 增加 `GPU_TIMEOUT` 3. 检查温度 |
| **48** | Double Bit ECC Error | 全版本（Ampere+ 常见） | SRAM/DRAM 双位 ECC 不可纠正错误 | **是** | 1. 立即隔离节点 2. 采集 ECC 详情 3. 重启后复测 4. 复现则 RMA |
| **56** | Display engine error | 全版本 | 显示引擎错误（无头服务器可忽略） | 否 | 1. 无头服务器可忽略 2. 有显示器则检查线缆 |
| **57** | Error programming video memory interface | 全版本 | 显存接口编程错误 | 可能 | 1. 重启节点 2. 升级驱动 3. 复现则 RMA |
| **61** | Internal micro-controller error | R418+（PMU/GSP） | GPU 内部微控制器（PMU/GSP）错误 | 可能 | 1. 重启节点 2. 升级驱动/固件 3. 复现则 RMA |
| **62** | Internal micro-controller breakpoint/warning | R418+ | 微控制器断点（info 级） | 否 | 通常可忽略，检查是否有其他 XID 伴随 |
| **63** | ECC page retirement recording | 全版本（语义随驱动分支变化） | ECC 触发行退役（row retirement） | 是（老化信号） | 1. 检查 retirement 计数 2. 如 pending=yes 需重启 3. 持续增长则计划换卡 |
| **64** | ECC page retirement / row remapping recording event **failure** | R460+（row remapping 语义） | 页退役/行重映射**记录失败**（如备用行资源耗尽），比 63 严重得多 | **是** | 1. `nvidia-smi -q -d ROW_REMAPPER` 确认 Remapping Failure / 剩余资源 2. 隔离节点 3. 基本判换卡（RMA） |
| **68** | Video processor exception | 全版本 | 视频处理器异常（计算集群少见，多与解码负载相关） | 可能 | 1. 重启节点观察 2. 复现则升级驱动/RMA |
| **69** | Graphics engine exception | 全版本 | 图形引擎异常（headless 计算卡少见） | 可能 | 1. 重启节点观察 2. 复现则升级驱动/RMA |
| **73** | NVLink error (data) | R418+（NVLink 2.0+） | NVLink 数据传输错误 | 是 | 同 XID 68，优先检查 NVSwitch |
| **74** | NVLink error (hardware) | R418+（NVLink 2.0+） | NVLink 硬件级错误 | 是 | 1. 检查 NVSwitch 状态 2. 检查 NVLink 物理连接 3. 可能需要换 NVSwitch/基板 |
| **79** | GPU has fallen off the bus | 全版本（AER 伴随日志 R535+ 更完整） | GPU 从 PCIe 总线消失 | **是** | 详见第 5 节专题 |
| **92** | High single-bit ECC error rate | **R418+ 引入** | SBE 错误率超过阈值 | 是（老化信号） | 1. 监控 SBE 增长趋势 2. 如果 24h 增量 > 100，计划换卡 3. 运行 memory stress test |
| **94** | Contained ECC error | **R465+ 引入** | 错误已被**遏制**：发生 ECC 错误（通常为 DBE），但影响被限制在出错进程内（该进程被杀），GPU 本身仍可继续服务其他进程 | 是（硬件错误） | 1. 重启受影响作业 2. 检查 ECC/ROW_REMAPPER 计数 3. 频发则隔离节点检修 |
| **95** | Uncontained ECC error | **R465+ 引入** | 错误**未被遏制**：其他进程与 GPU 状态可能已损坏，不可信 | **是** | 1. **立即隔离**（cordon+drain） 2. 采集证据（nvidia-bug-report） 3. GPU reset / 重启节点验证 4. 复发走 RMA |
| **119** | GSP RPC Timeout | **R515+（GSP 引入后）** | GSP（GPU System Processor）RPC 超时，GPU 无响应 | 可能 | 1. 升级驱动（GSP 相关 bug 多在新分支修复） 2. 尝试禁用 GSP（`modprobe nvidia NVreg_EnableGpuFirmware=0`） |
| **120** | GSP Error | **R515+（GSP 引入后）** | GSP 固件错误 | 可能 | 同 XID 119 |

### 3.2 dmesg 中 XID 日志格式示例与字段解读

> **驱动分支差异说明**：以下示例基于 R535+/R550+ 驱动分支（Hopper/Blackwell 生产环境）。较旧分支（R470 及以前）的 XID 日志字段较简略（可能缺少 `pid=`/`name=` 字段）；R515+ GSP 启用后，部分 XID（119/120）的日志路径经过 GSP 固件，格式与旧分支略有不同。`dmesg -T` 的人类可读时间戳格式依赖 kernel 5.x+ systemd 时间基准。

```text
# 示例 1：标准 XID 日志
[2024-01-15 14:32:07] NVRM: Xid (PCI:0000:41:00): 79, pid=12345, name=python3, Channel 0x1a, GPU has fallen off the bus.

字段解读：
- 时间戳：2024-01-15 14:32:07（使用 dmesg -T 的人类可读格式）
- 设备：PCI:0000:41:00（PCIe BDF 地址，对应 GPU 0~7 中的某一张）
- XID：79（错误代码）
- pid=12345：触发错误的进程 PID
- name=python3：进程名
- Channel 0x1a：GPU 内部通道编号（对排查意义有限）
- 描述：GPU has fallen off the bus（错误描述）

# 示例 2：ECC 相关 XID
[2024-01-15 15:01:23] NVRM: Xid (PCI:0000:c3:00): 48, pid=67890, name=python3, Ch 0x00000005, ECC error, memory target: L2 cache, address: 0x0000000000abcdef

字段解读：
- 设备：PCI:0000:c3:00
- XID：48（Double Bit ECC Error）
- Ch：通道号
- memory target：错误发生在 L2 cache（也可能是 DRAM、SRAM）
- address：错误地址（用于定位具体 bank/row）

# 示例 3：多 XID 并发（找首因）
[14:32:07] Xid 79, PCI:0000:41:00   ← 首因！时间最早
[14:32:07] Xid 62, PCI:0000:41:00   ← 伴随
[14:32:08] Xid 13, PCI:0000:41:00   ← 后续
[14:32:10] Xid 45, PCI:0000:41:00   ← 后续
```

### 3.3 多 XID 并发时找首因

**原则：时间序最早的那条是首因，后续 XID 通常是级联反应。**

```bash
# 快速提取 XID 时间序
dmesg -T | grep 'NVRM.*Xid' | sort -t'[' -k2 | head -20

# 关注首因 XID 的 PCI BDF，确定是哪张卡
# 然后用 nvidia-smi 的 pci.bus_id 对应到 GPU index
nvidia-smi --query-gpu=index,pci.bus_id --format=csv

# 常见级联模式：
# XID 79 → 62 → 13 → 45  （GPU 掉卡 → 微控制器告警 → 引擎异常 → 超时清理）
# XID 48 → 95              （ECC SBE 积累 → DBE 不可纠正）
# XID 68 → 69 → 73 → 74   （NVLink CRC 错误 → 硬件错误升级）
```

---

## 4. ECC 与显存健康判读

### 4.1 nvidia-smi -q -d ECC 输出解读

> **版本说明**：以下输出格式基于 driver R550+ / nvidia-smi 550.127.08。R550+ 分支将 ECC 计数分为 SRAM（GPU 内部缓存）和 DRAM（HBM 显存）两个维度；较旧驱动分支（R470 及以前）可能只有 `Single Bit`/`Double Bit` 汇总字段，不区分 SRAM/DRAM。

```text
==============NVSMI LOG==============

GPU 00000000:41:00.0
    ECC Mode                          : Enabled
    ECC Errors
        Volatile
            SRAM Correctable          : 0
            SRAM Uncorrectable        : 0
            DRAM Correctable          : 12          ← SBE，可纠正，单次不影响数据
            DRAM Uncorrectable        : 0           ← DBE，不可纠正，必须为零！
        Aggregate
            SRAM Correctable          : 156         ← 累计 SBE
            SRAM Uncorrectable        : 0           ← 累计 DBE
            DRAM Correctable          : 2341
            DRAM Uncorrectable        : 0

解读要点：
1. Volatile（易失）= 上次重置/重启后的计数，重启清零
2. Aggregate（聚合）= 卡的全生命周期累计，重启不清零
3. SRAM = GPU 内部缓存（L1/L2/寄存器文件），错误影响更直接
4. DRAM = 显存（HBM），错误更常见，SBE 可纠正但持续增长说明老化
5. 关键红线：
   - Volatile DBE > 0 → 立即隔离换卡
   - Aggregate DBE > 0 → 如果历史上出过 DBE，卡已不可信
   - Volatile SBE 单次 > 100 → 异常增长，需关注
   - Aggregate SBE > 10000 → 卡老化明显，计划更换
```

### 4.2 ROW_REMAPPER 输出解读

> **版本说明**：`nvidia-smi -q -d ROW_REMAPPER` 输出格式基于 driver R550+ / nvidia-smi 550.127.08。R515+ 驱动分支引入 Row Remapper 概念（Remapped Due to SBE/DBE + Pending + Remapping Failure）；R470 及以前使用旧字段 `Retired_Pages`（Retired Pages SBE/DBE）表示类似语义，字段名和语义有差异——`Retired_Pages` 按页计数，`Remapped Rows` 按行计数，一行包含多页。若从旧驱动升级，需重新建立基线阈值。

```text
    Row Remapper
        Remapping Failure Occurred    : No          ← 如果有 Yes，换卡
        Remapped Due to SBE           : 3           ← 因 SBE 触发的行退役数
        Remapped Due to DBE           : 0           ← 因 DBE 触发的行退役数（应为 0）
        Remapped Banks
            Bank 0                    : 0x00000000
            Bank 1                    : 0x00000000
            ...
        Pending                       : No          ← Yes 表示有退役等待生效，需重启
        Remapping Failure Occurred    : No          ← Yes 表示退役资源耗尽，必须换卡

解读要点：
1. 现代 GPU 有 spare rows（备用行），ECC 触发后自动将坏行退役到备用行
2. Pending=Yes → 有退役操作等待 GPU 重置后生效 → 需要重启
3. Remapping Failure=Yes → 备用行耗尽，无法继续退役 → **必须换卡**
4. Remapped Due to DBE > 0 → 曾有不可纠正错误触发的退役 → 卡已不可信
```

### 4.3 换卡判定流程图（文字决策树）

```text
ECC 异常发现
├── Volatile DBE > 0？
│   └── Yes → 【立即换卡】运行 rma_evidence.sh，执行隔离
├── Aggregate DBE > 0？
│   └── Yes → 卡历史上出过 DBE → 【计划换卡】，当前可观察运行
├── Remapping Failure = Yes？
│   └── Yes → 【立即换卡】备用行耗尽
├── Pending = Yes？
│   └── Yes → 安排最近维护窗口重启 → 重启后复测
│       └── 重启后 Pending 仍为 Yes → 【立即换卡】
├── Volatile SBE 增速 > 100/24h？
│   └── Yes → 标记 suspect，持续监控
│       └── 增速持续加快 → 【计划换卡】
├── Aggregate SBE > 10000？
│   └── Yes → 卡老化 → 【计划换卡】（下一个维护窗口）
└── 以上均否 → 正常，持续监控
```

### 4.4 XID 48/63/64/92/94/95 与 ECC 计数的关联分析

> **版本说明**：XID 92/94/95 自较新驱动分支（R418/R465+）引入，旧驱动环境下这些 XID 不会出现。XID 63/64 的语义随驱动分支演变——R515+ 与 Row Remapper 关联，旧分支与 Page Retirement 关联。以下关联模式基于 R550+ / A100/H100 环境。

| XID | ECC 计数器变化 | 关联解读 |
|---|---|---|
| 48 | volatile DBE +1，aggregate DBE +1 | DBE 实际发生，硬件不可纠正错误确认 |
| 63 | ROW_REMAPPER 计数 +1，可能 Pending=Yes | 行重映射已记录，重启后生效 |
| 64 | Remapping Failure=Yes 或备用行耗尽 | 重映射**失败**，严重故障 | 基本判换卡 |
| 92 | volatile SBE 快速增长 | SBE 率超限，老化预警 |
| 94 | volatile DBE +1（通常） | 错误遏制在出错进程内，GPU 仍可用 | 重启作业、观察频率 |
| 95 | volatile DBE +1（通常） | 错误未遏制，GPU 状态不可信 | 隔离 + reset/重启，复发 RMA |

**关联模式：**

```text
正常老化趋势：Aggregate SBE 缓慢增长 → XID 92（SBE 率超限）→ 计划换卡
恶化趋势：XID 94 偶发（contained DBE）→ 触发行重映射 XID 63 → Pending=Yes（重启生效）→ 备用行耗尽/重映射失败 XID 64 → 必须换卡
突发故障：无预警直接 XID 48/95（uncontained DBE）→ 立即隔离，reset/重启后复发则 RMA
```

---

## 5. GPU 掉卡（XID 79）专题

### 5.1 完整排查树

```text
XID 79 发现
│
├── 1. 检查 dmesg PCIe AER 错误
│   ├── dmesg -T | grep -i 'aer\|corrected\|uncorrected'
│   ├── 有 Fatal/Non-fatal AER → PCIe 链路问题 → 检查物理连接/重插卡
│   └── 无 AER → 继续下一步
│
├── 2. 检查供电
│   ├── ipmitool sel list | grep -i 'power\|voltage\|current'
│   ├── 有供电异常 → PSU 或 PDU 问题 → 检查电源
│   └── 正常 → 继续
│
├── 3. 检查温度历史
│   ├── 查看 Prometheus 历史：DCGM_FI_DEV_GPU_TEMP 近 1h
│   ├── 温度 > 95°C → 过热保护掉卡 → 检查散热
│   └── 温度正常 → 继续
│
├── 4. 检查 BMC 日志
│   ├── ipmitool sel elist
│   ├── 有硬件告警 → 按 BMC 提示处理
│   └── 正常 → 继续
│
├── 5. 尝试单卡重置
│   ├── nvidia-smi -r -i <gpu_index>
│   ├── 成功 → 重新加载驱动 → 验证
│   └── 失败（GPU 不可达）→ 继续
│
├── 6. 驱动重载
│   ├── rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia
│   ├── modprobe nvidia
│   ├── nvidia-smi
│   ├── 成功 → 观察 1h 是否复发
│   └── 失败 → 继续
│
├── 7. 重启节点
│   ├── reboot
│   ├── 重启后 nvidia-smi 检查
│   ├── 成功且 72h 不复发 → 偶发故障，加入观察清单
│   └── 重启后仍掉卡 或 短期内复发 → RMA
│
└── 8. 触发 RMA
    ├── 运行 rma_evidence.sh
    ├── 执行 node_isolate.sh
    └── 提交 RMA 工单
```

### 5.2 PCIe AER 错误解读示例

```text
# dmesg 中的 AER 错误示例：

# Corrected Error（可纠正，通常无害但频繁出现说明链路劣化）
[14:32:05] pcieport 0000:40:03.1: AER: Corrected error received: 0000:41:00.0
[14:32:05] pcieport 0000:40:03.1: AER: PCIe Bus Error: severity=Corrected, id=0000:41:00.0
[14:32:05]   device [10de:20b2] error status/mask=00001001/00002000
[14:32:05]    [ 0] Receiver Error         (First)     ← 接收端错误，通常是信号完整性
[14:32:05]    [12] Poisoned TLP                       ← 有毒的 TLP 包

# Non-Fatal Error（不可纠正但不致命，通常是 XID 79 的前兆）
[14:32:06] pcieport 0000:40:03.1: AER: Uncorrected (Non-Fatal) error received: 0000:41:00.0
[14:32:06]   [20] Unsupported Request Error (URE)     ← 不支持的请求
[14:32:06]   [16] ACS Violation                       ← ACS 违规

# Fatal Error（致命错误，通常伴随 XID 79）
[14:32:07] pcieport 0000:40:03.1: AER: Uncorrected (Fatal) error received: 0000:41:00.0
[14:32:07]   [ 4] Data Link Protocol Error            ← 数据链路层协议错误
[14:32:07] NVRM: Xid (PCI:0000:41:00): 79, GPU has fallen off the bus.

关键字段：
- severity：Corrected / Non-Fatal / Fatal
- [0] Receiver Error：物理层信号问题
- [4] Data Link Protocol Error：链路层问题，通常导致掉卡
- [12] Poisoned TLP：收到了标记为"有毒"的数据包
- [20] Unsupported Request Error：目标设备无法处理请求
```

---

## 6. 性能类排查

### 6.1 GPU 降频五种原因

**Clocks Event Reasons 字段解读（`nvidia-smi -q -d PERFORMANCE`）：**

> **版本说明**：`Clocks Event Reasons` 字段在 R460+ 驱动分支稳定；R550+ 分支增加了更多细节（如区分 SW/HW Thermal）。`dcgmi export -e DCGM_FI_PROF_SM_ACTIVE` 需 DCGM 3.3+；DCGM 4.x 部分 profiling 指标重命名，需核对指标名。

```text
    Clocks
        Graphics                      : 1410 MHz     ← 当前频率
        SM                            : 1410 MHz
        Memory                        : 1593 MHz
        Video                         : 1260 MHz

    Clocks Event Reasons
        GPU Idle                      : Not Active   ← 空闲降频（正常）
        Applications Clocks Setting   : Not Active   ← 应用时钟限制（正常）
        SW Power Cap                  : Not Active   ← 软件功耗限制
        HW Slowdown                   : Active       ← ⚠️ 硬件降频！
            HW Thermal Slowdown       : Active       ← ⚠️ 温度导致！
            HW Power Brake Slowdown   : Not Active   ← 电源制动降频
        Sync Boost                    : Not Active   ← 同步提升
        SW Thermal Slowdown           : Not Active   ← 软件温度限制
```

| 降频原因 | Clocks Event 字段 | 检查方法 | 处置 |
|---|---|---|---|
| 温度降频 | HW Thermal Slowdown = Active | `nvidia-smi --query-gpu=temperature.gpu` > 83°C | 检查散热/风扇/硅脂 |
| 功耗降频 | HW Power Brake Slowdown = Active | `nvidia-smi --query-gpu=power.draw` 接近 limit | 检查 PSU 或降低 power limit |
| 可靠性降频 | HW Slowdown = Active（非温度/功耗原因） | 通常是电压不稳或老化 | 升级驱动/固件，复现则 RMA |
| 空闲降频 | GPU Idle = Active | 无计算任务时正常 | 无需处理 |
| 软件限制 | SW Power Cap / SW Thermal = Active | `nvidia-smi -q -d POWER` 查看 power limit 设置 | 调整 `nvidia-smi -pl <watts>` |

### 6.2 数据加载瓶颈判断

**症状：GPU util 100% 但 SM_ACTIVE 很低**

```bash
# 1. 对比 GPU util 和 SM_ACTIVE
nvidia-smi --query-gpu=utilization.gpu,utilization.memory --format=csv -l 1
dcgmi export -f 3 -e DCGM_FI_PROF_SM_ACTIVE,DCGM_FI_PROF_SM_OCCUPANCY -c 1

# 如果 util.gpu=100% 但 SM_ACTIVE < 50%，说明 GPU 在等数据

# 2. 检查磁盘 I/O
iostat -x 1 5
# 关注：await（I/O 延迟）、%util（磁盘利用率）
# 如果 await > 10ms 或 %util ≈ 100% → 存储瓶颈

# 3. 检查 dataloader worker
ps aux | grep -c 'data.*loader\|DataLoader'
# 检查 worker 数量是否足够（建议 = CPU 核数 / GPU 数）

# 4. 检查 Python GIL / 预处理瓶颈
py-spy dump --pid $(pgrep -f 'train.py' | head -1)
# 查看主线程在做什么，如果卡在 data loading → 瓶颈确认

# 5. 快速验证：用 dummy data 跑一下看吞吐是否恢复
# 如果 dummy data 吞吐正常 → 确认是数据加载瓶颈
```

### 6.3 Nsight Systems 基本使用

```bash
# 采集 profile（推荐在 1~2 个 step 上采集，不要全程）
nsys profile \
    --trace=cuda,nvtx,osrt,cudnn,cublas,nccl \
    --output=/tmp/profile_report \
    --force-overwrite=true \
    --duration=60 \
    python train.py --steps 10

# 生成统计报告（不需要 GUI）
nsys stats /tmp/profile_report.nsys-rep --report cuda_gpu_kern_sum
nsys stats /tmp/profile_report.nsys-rep --report nccl_sum

# 关键看什么：
# 1. GPU kernel 时间分布 → 是否有小 kernel 频繁启动（overhead）
# 2. NCCL 通信时间占比 → 超过 20% 说明通信是瓶颈
# 3. CPU→GPU memcpy → 是否有大量 H2D 传输（应该在初始化时完成）
# 4. GPU idle gap → kernel 之间的空白 = 等数据时间
```

### 6.4 torch profiler 要点

```python
# 在训练代码中插入 profiler
from torch.profiler import profile, record_function, ProfilerActivity

with profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    schedule=torch.profiler.schedule(wait=1, warmup=1, active=3, repeat=1),
    on_trace_ready=torch.profiler.tensorboard_trace_handler('/tmp/profiler_logs'),
    record_shapes=True,
    profile_memory=True,
    with_stack=True
) as prof:
    for step in range(10):
        with record_function("training_step"):
            loss = train_one_step()
        prof.step()

# 查看结果
print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=20))
```

### 6.5 通信瓶颈判断

```bash
# NCCL 时间占比判断
# 方法 1：从 nsys stats 看
nsys stats profile.nsys-rep --report nccl_sum
# 如果 NCCL kernel 时间占总 GPU 时间 > 30% → 通信瓶颈

# 方法 2：对比单卡和分布式吞吐
# 单卡 step_time = 1.0s
# 8卡 step_time = 1.2s → 通信 overhead = 0.2s/step (20%)
# 64卡 step_time = 2.0s → 通信 overhead = 1.0s/step (50%) → 瓶颈

# 方法 3：NCCL 环境变量调试
export NCCL_DEBUG=INFO
export NCCL_DEBUG_FILE=/tmp/nccl_%h.log
# 运行后检查：
grep -i 'busbw\|bandwidth\|latency' /tmp/nccl_*.log
# 对比实测 busbw 与理论值（A100 NVLink 应 > 250 GB/s allreduce busbw）
```

---

## 7. K8s 层 GPU 故障案例

> **版本上下文**：本节案例基于 K8s 1.31 + NVIDIA GPU Operator v24.9.2（含 k8s-device-plugin v0.17.0、gpu-feature-discovery v0.17.0）+ nvidia-container-toolkit v1.17.4 + containerd 1.7.x。不同版本的 Device Plugin 和 Container Toolkit 行为可能有差异，请以实际部署版本为准。

### 7.1 容器内看不到卡

```bash
# 症状：Pod 内 nvidia-smi 报错 "No devices found"

# 排查步骤：
# 1. 检查 Pod 的 resource request
kubectl get pod <pod-name> -n <ns> -o yaml | grep -A5 'resources'
# 确认有 nvidia.com/gpu: N

# 2. 检查 Device Plugin 状态
kubectl get pods -n kube-system -l name=nvidia-device-plugin
kubectl describe pod -n kube-system -l name=nvidia-device-plugin | grep -A10 'Events'

# 3. 检查节点 allocatable
kubectl describe node <node> | grep -A5 'Allocatable'
# 确认 nvidia.com/gpu: 8

# 4. 检查 Device Plugin 日志
kubectl logs -n kube-system <device-plugin-pod> | tail -30

# 5. 常见根因
# - Device Plugin Pod CrashLoopBackOff → 检查驱动是否安装
# - nvidia.com/gpu 不在 allocatable 中 → 重启 Device Plugin
# - 节点 label 不匹配 → 检查 nodeSelector
```

### 7.2 CUDA unknown error

```bash
# 症状：torch.cuda 报 "CUDA error: unknown error" 或 "cudaErrorUnknown (999)"

# 排查步骤：
# 1. 检查 nvidia-smi 是否正常
nvidia-smi
# 如果报错 → 驱动问题，参考 XID 79 排查

# 2. 检查 CUDA 运行时
python -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.device_count())"

# 3. 检查 NVIDIA UVM 模块
lsmod | grep nvidia
# 如果 nvidia_uvm 未加载：
sudo modprobe nvidia_uvm

# 4. 检查容器是否有正确的设备映射
ls -la /dev/nvidia*
# 应该看到 /dev/nvidia0, /dev/nvidia1, ..., /dev/nvidiactl, /dev/nvidia-uvm

# 5. 常见根因
# - 驱动被重载但容器未重启 → 重启 Pod
# - nvidia_uvm 模块丢失 → modprobe nvidia_uvm
# - 容器运行时（containerd）升级后设备映射异常 → 重启 containerd + Pod
```

### 7.3 Failed to initialize NVML

```bash
# 症状：nvidia-smi 报 "Failed to initialize NVML: Unknown Error"

# 排查步骤：
# 1. 检查驱动模块
lsmod | grep nvidia
cat /proc/driver/nvidia/version

# 2. 检查 nvidia 设备节点
ls -la /dev/nvidia*

# 3. 检查 dmesg
dmesg -T | grep -i 'nvidia\|nvrm' | tail -20

# 4. 常见根因与处置
# a) 驱动模块被意外卸载 → modprobe nvidia
# b) 驱动版本与内核不兼容 → 检查内核版本和驱动版本匹配
# c) NVIDIA persistenced 未运行 → systemctl start nvidia-persistenced
# d) /dev/nvidia* 权限问题 → ls -la /dev/nvidia* 检查属主和权限

# 5. 快速恢复
sudo systemctl restart nvidia-persistenced
sudo nvidia-smi   # 触发设备初始化
```

### 7.4 Device Plugin 计数错

```bash
# 症状：kubectl describe node 显示 nvidia.com/gpu: 6（实际 8 卡）

# 排查步骤：
# 1. 确认物理卡数
nvidia-smi --query-gpu=count --format=csv,noheader

# 2. 检查 Device Plugin 发现的设备数
kubectl logs -n kube-system <device-plugin-pod> | grep -i 'device\|discover'

# 3. 检查是否有卡掉线
nvidia-smi   # 看是否有 GPU 显示 "ERR!" 或缺失

# 4. 常见根因
# - 某张卡 XID 79 掉卡 → Device Plugin 只发现了剩余可用卡
# - MIG 配置残留 → 检查并清除 MIG 配置
# - Device Plugin bug → 升级 Device Plugin 版本

# 5. 恢复
# a) 如果卡掉了 → 修复掉卡问题后重启 Device Plugin
# b) 如果计数错但卡都在 → 重启 Device Plugin Pod
kubectl delete pod -n kube-system <device-plugin-pod>
# DaemonSet 会自动重建
```

### 7.5 MIG 请求 Pending

```bash
# 症状：Pod 请求 MIG 设备但一直 Pending

# 排查步骤：
# 1. 检查节点 MIG 配置
nvidia-smi mig -lgi   # 列出 MIG 实例
nvidia-smi mig -lgi -i 0   # 指定 GPU

# 2. 检查 Pod 请求的 MIG profile
kubectl get pod <pod> -o yaml | grep 'nvidia.com/mig'
# 例如：nvidia.com/mig-1g.5gb: 1

# 3. 检查节点 allocatable MIG 资源
kubectl describe node <node> | grep 'nvidia.com/mig'

# 4. 常见根因
# - MIG 未配置或配置不匹配 → 重新配置 MIG
# - GPU Feature Discovery 未更新 → 重启 GFD Pod
# - 请求的 profile 不存在 → 调整 MIG 分割方案

# 5. 重新配置 MIG
sudo nvidia-smi mig -cgi 9,9,9,9,9,9,9,9 -i 0  # 创建 8 个 1g.5gb 实例
sudo nvidia-smi mig -cgi 3,3,3 -i 1              # 创建 3 个 3g.20gb 实例
# 然后重启 GPU Feature Discovery 和 Device Plugin
```

---

## 8. 经典案例库

### 案例 1：规律性变慢（温度降频）

**症状：** LLM 训练任务每天下午 14:00~17:00 step time 增加约 20%，其余时段正常。

**环境上下文：** driver 535.x（R535 分支）+ A100 80GB SXM + 机房精密空调，HW Thermal Slowdown 阈值由 GPU 固件控制（A100 ≈ 83°C，H100 ≈ 87°C）。

**排查过程：**
```bash
# 1. 查看 Prometheus 中 GPU 温度趋势
# Grafana 面板显示 14:00 起 GPU 温度从 75°C 升至 86°C

# 2. 检查 Clocks Event Reasons
nvidia-smi -q -d PERFORMANCE -i 0
# HW Thermal Slowdown: Active

# 3. 检查机房温控
# 发现该时段机房空调执行节能模式，送风温度从 22°C 升至 28°C

# 4. 确认风扇转速
nvidia-smi --query-gpu=fan.speed --format=csv
# 所有 GPU 风扇 100%（已满速）
```

**根因：** 机房空调节能策略导致下午时段进风温度升高，GPU 散热能力不足，触发 HW Thermal Slowdown 降频。

**解决：** 修改空调策略为恒温模式，送风温度恒定 22°C。

**预防：** 添加 GPU 温度告警（>80°C WARN，>85°C P2），添加 Clocks Event Reasons 中 HW Thermal Slowdown 的 Prometheus 告警。

---

### 案例 2：NCCL 随机 timeout（光纤弯折）

**症状：** 128 卡训练任务随机出现 NCCL timeout（`watchdog caught collective operation timeout`），频率约每 3~5 天一次，涉及不同节点组合。

**环境上下文：** driver 535.x + CUDA 12.2 + NCCL 2.18 + ConnectX-6 Dx HDR 200G + Quantum-2 QM8700 交换机，双机 HDR 互联。NCCL 默认 timeout 30s（`NCCL_TIMEOUT` 环境变量）。

**排查过程：**
```bash
# 1. NCCL 日志分析
grep -i 'timeout\|error\|abort' /tmp/nccl_*.log
# 发现 timeout 总是涉及 node-037 和 node-088 之间的通信

# 2. 检查 IB 计数器
perfquery -x   # 扩展计数器
# node-037 的 mlx5_0 端口 symbol_error 持续增长

# 3. 检查物理链路
# 发现 node-037 → TOR-03 → spine-01 链路中，TOR-03 端口 15 的光纤在走线架弯折角度过大

# 4. 检查 DDM
ibdiagnet -pc
# node-037 侧光模块 Rx Power: -11.2 dBm（偏低，正常应 > -8 dBm）
```

**根因：** 光纤在走线架处弯折角度过大（< 30mm 弯曲半径），导致光信号衰减增大，间歇性丢包触发 NCCL timeout。

**解决：** 重新布线，保证光纤弯曲半径 > 40mm，更换受损光纤跳线。

**预防：** IB 布线规范培训，添加光模块 DDM 监控告警（Rx Power < -10 dBm WARN）。

---

### 案例 3：Pending 碎片化

**症状：** 集群有 128 节点，空闲 30 个，但请求 16 卡的训练 Job 一直 Pending，`kubectl describe pod` 显示 `0/128 nodes are available: 16 Insufficient nvidia.com/gpu`。

**环境上下文：** K8s 1.31 + NVIDIA Device Plugin v0.17.0 + Volcano v1.10.x（Gang Scheduling），A100 8 卡/节点。

**排查过程：**
```bash
# 1. 检查各节点 allocatable GPU
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu | sort -k2 -rn
# 发现空闲节点都是 2~6 卡空闲（碎片化），没有任何节点有 ≥ 8 卡连续空闲

# 2. 检查是否有低优先级的 Pod 占着 GPU
kubectl get pods -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,NODE:.spec.nodeName,GPU:.spec.containers[0].resources.requests.nvidia\.com/gpu | grep -v "^NS" | awk '$4 > 0' | sort -k3

# 3. 发现 12 个 1~2 卡的小任务散布在各节点上
```

**根因：** 大量小任务碎片化占用了 GPU，导致没有连续 16 卡的节点可用。

**解决：** 引入 K8s priority class，大任务设置高优先级可抢占小任务；配置 Gang Scheduling（如 volcano.sh）确保 16 卡一起调度。

**预防：** 部署 cluster autoscaler 或自定义调度器，实施 bin-packing 策略减少碎片。

---

### 案例 4：镜像拉取风暴

**症状：** 同时启动 64 个节点的分布式训练，所有 Pod 都在 `Pulling image` 状态卡住超过 10 分钟。

**环境上下文：** K8s 1.31 + containerd 1.7.x + Harbor 镜像仓库（10GbE 接入），NGC PyTorch 镜像 28GB（`nvcr.io/nvidia/pytorch:24.12-py3`）。

**排查过程：**
```bash
# 1. 检查 containerd 日志
journalctl -u containerd --since "10 min ago" | grep -i 'pull\|error\|timeout'
# 大量 "context deadline exceeded" 和 "connection refused"

# 2. 检查镜像仓库负载
curl -sf http://harbor.internal/api/v2.0/health
# 响应时间 > 30s

# 3. 检查镜像大小
crictl inspecti <image> | grep -i size
# 训练镜像 28GB（含 CUDA + PyTorch + 模型权重）

# 4. 检查节点镜像拉取并发
# 64 节点同时拉取 28GB 镜像 = 瞬时带宽需求 > 100Gbps
```

**根因：** 64 节点同时拉取 28GB 大镜像，镜像仓库带宽和 IOPS 被打满，形成拉取风暴。

**解决：**
1. 提前在节点预热镜像（`crictl pull <image>` 脚本 cron 定期执行）
2. 使用 P2P 镜像分发（如 Dragonfly/DADI），节点间共享 layer
3. 训练 Job 使用 `imagePullPolicy: IfNotPresent`

**预防：** 镜像预热流水线（CI/CD push 后自动触发全节点预热），监控镜像拉取延迟。

---

### 案例 5：etcd 抖动

**症状：** 训练任务偶发性中断，`kubectl get events` 显示大量 `NodeLost` → `NodeReady` 闪烁，kubelet 报 `etcd client: etcdserver: request timed out`。

**环境上下文：** etcd 3.5.15（K8s 1.31 配套）+ NVMe SSD（Intel P5510），etcd 节点与应用混部。etcdctl 3.5.x（默认 v3 API）。

**排查过程：**
```bash
# 1. 检查 etcd 性能
etcdctl endpoint health --cluster
# 某个 etcd 节点 health check 耗时 > 5s

# 2. 检查 etcd 磁盘延迟
# Prometheus: etcd_disk_wal_fsync_duration_seconds
# P99 延迟 > 500ms（应该 < 10ms）

# 3. 检查 etcd 节点的磁盘
iostat -x 1 5
# 发现 etcd 所在节点的 NVMe SSD %util 持续 100%

# 4. 检查谁在抢 I/O
iotop -ao
# 发现日志采集 agent（filebeat）在大量读取 etcd 节点上的日志文件
```

**根因：** etcd 所在节点同时运行了高 I/O 的日志采集 agent，导致 SSD 饱和，etcd WAL fsync 超时，K8s 控制面抖动。

**解决：** 将 etcd 部署在专用节点，不运行其他工作负载；将 filebeat 的 nice/ionice 降低优先级。

**预防：** etcd 节点专用化，添加 etcd disk latency 告警（P99 > 50ms WARN），etcd 节点不打可调度标签。

---

### 案例 6：驱动升级 MIG 丢失

**症状：** 驱动从 535.104.05 升级到 535.154.05 后，所有 MIG 配置消失，Pod 请求 MIG 设备 Pending。

**环境上下文：** A100 80GB SXM + R535 驱动分支 + Fabric Manager 535.x + NVIDIA GPU Operator v24.9.2，MIG 3g.20gb profile。MIG 配置不持久化是 NVIDIA 驱动的已知行为（所有驱动分支均如此）。

**排查过程：**
```bash
# 1. 检查 MIG 配置
nvidia-smi mig -lgi
# 输出 "No MIG instances found"

# 2. 检查驱动版本
nvidia-smi | grep Driver
# 535.154.05 ✓

# 3. 检查 nvidia-smi 日志
dmesg -T | grep -i 'mig\|nvrm' | tail -10
# "NVRM: MIG configuration is not persisted across driver reload"
```

**根因：** NVIDIA 驱动升级（卸载→重装）后 MIG 配置不保留，需要重新创建。

**解决：** 重新配置 MIG 实例：
```bash
nvidia-smi mig -cgi 3,3,3 -i 0  # 重新创建 MIG 分割
nvidia-smi mig -cgi 3,3,3 -i 1
# ... 对所有 GPU
systemctl restart nvidia-fabricmanager
kubectl delete pod -n kube-system -l name=gpu-feature-discovery  # 刷新 GFD
```

**预防：** 驱动升级脚本中包含 MIG 配置恢复步骤；将 MIG 配置固化为脚本（Ansible playbook），升级后自动执行。

---

### 案例 7：新机 nccl 带宽差（GPU-NIC 跨 NUMA 插错槽）

**症状：** 新到货的 8 卡 A100 节点，nccl-tests all_reduce 带宽只有 150 GB/s（正常应 > 250 GB/s），延迟偏高。

**环境上下文：** A100 80GB SXM（HGX A100 基板）+ driver 535.x + CUDA 12.2 + NCCL 2.18 + ConnectX-6 Dx HDR 200G，双路 CPU（Intel Xeon 或 AMD EPYC）NUMA 架构。

**排查过程：**
```bash
# 1. 对比正常节点
# 正常节点 all_reduce: 275 GB/s，新机: 152 GB/s

# 2. 检查 NUMA 拓扑
numactl --hardware
# Node 0: GPU 0-3, NIC mlx5_0
# Node 1: GPU 4-7, NIC mlx5_1

# 3. 检查 GPU-NIC NUMA 亲和性
nvidia-smi topo -m
# GPU 0~3 的 NUMA node = 0
# 但 mlx5_0 的 NUMA node = 1  ← 跨 NUMA！

# 4. 物理检查
# 打开机箱发现：mlx5_0 网卡插在了 CPU1 的 PCIe 槽上，但 GPU 0~3 在 CPU0 下
```

**根因：** 网卡物理插槽错误，GPU 和 NIC 不在同一 NUMA node，跨 NUMA 通信增加了延迟、降低了带宽。

**解决：** 将网卡移到正确 NUMA 的 PCIe 槽位（与对应 GPU 同 CPU socket 下）。

**预防：** 新机验收 SOP 包含 `nvidia-smi topo -m` 检查 GPU-NIC NUMA 亲和性，不通过则退回集成商。

---

### 案例 8：containerd 升级后 CUDA unknown error

**症状：** containerd 从 1.6.x 升级到 1.7.x 后，新启动的 Pod 内 `torch.cuda.is_available()` 返回 False，报 `CUDA error: unknown error`。

**环境上下文：** containerd 1.6.x → 1.7.x（K8s 1.31 基线版本）+ nvidia-container-toolkit 1.13.x（需升级至 1.14+ 解决兼容性问题）+ driver 535.x + PyTorch 2.x（CUDA 12.x）。`privileged_without_host_devices` 参数在 1.7.x 中行为变化。

**排查过程：**
```bash
# 1. 容器内检查
nvidia-smi
# "Failed to initialize NVML: Unknown Error"

# 2. 检查 /dev 挂载
ls -la /dev/nvidia*
# 只有 /dev/nvidiactl，缺少 /dev/nvidia0 ~ /dev/nvidia7

# 3. 检查 containerd 配置
cat /etc/containerd/config.toml | grep -A10 'nvidia'
# [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
#   runtime_type = "io.containerd.runc.v2"
#   privileged_without_host_devices = true  ← 这个参数在新版本行为变了

# 4. 检查 NVIDIA Container Toolkit 版本
dpkg -l | grep nvidia-container
# nvidia-container-toolkit 1.13 → 与 containerd 1.7.x 兼容性有变化
```

**根因：** containerd 1.7.x 对 `privileged_without_host_devices` 的处理逻辑变化，导致 NVIDIA 设备未正确映射到容器内。

**解决：** 升级 nvidia-container-toolkit 到 1.14.x（修复了兼容性），或删除 `privileged_without_host_devices` 配置项。

**预防：** 容器运行时升级必须在金丝雀节点验证 CUDA 容器功能，升级 checklist 包含 `torch.cuda.is_available()` 验证。

---

### 案例 9：定时任务抢存储带宽

**症状：** 训练任务每隔 6 小时出现一次 step time spike（从 2.1s 跳到 8.5s），持续约 10 分钟后恢复。

**环境上下文：** Lustre 2.15.x 并行文件系统（客户端 kernel module 与本机 kernel 5.15 匹配编译）+ rsync 3.6.x，训练数据与备份共用同一存储路径。

**排查过程：**
```bash
# 1. 对齐时间线
# spike 发生在 00:00, 06:00, 12:00, 18:00

# 2. 检查存储 I/O
# Prometheus: node_disk_read_bytes_total 在 spike 期间从 2GB/s 飙到 15GB/s

# 3. 检查定时任务
crontab -l
# 0 */6 * * * /opt/scripts/data_backup.sh  ← 每 6 小时的数据备份任务

# 4. 确认备份脚本行为
cat /opt/scripts/data_backup.sh
# rsync -av /data/checkpoints/ /backup/  ← 全量 rsync，走同一存储路径
```

**根因：** 数据备份定时任务每 6 小时执行全量 rsync，抢占了训练任务的存储带宽。

**解决：** 调整备份任务到训练任务空闲时段（如凌晨 3 点）；对 rsync 添加 `--bwlimit=500000`（限制 500MB/s）；备份走独立存储网络。

**预防：** 存储带宽 QoS（Lustre/GPFS 的 QOS 功能），训练路径和备份路径分离。

---

### 案例 10：util 100% 但吞吐低（dataloader 瓶颈）

**症状：** GPU utilization 显示 100%，但训练吞吐只有预期的 60%，step time 比基线慢 40%。

**环境上下文：** A100 80GB SXM + driver 535.x + DCGM 3.3.x（`dcgmi export` 命令）+ PyTorch 2.x + Python 3.10（Ubuntu 22.04），数据存于 Lustre 2.15.x。

**排查过程：**
```bash
# 1. 对比 util 和 SM_ACTIVE
nvidia-smi --query-gpu=utilization.gpu --format=csv -l 1   # 100%
dcgmi export -e DCGM_FI_PROF_SM_ACTIVE -c 1               # 仅 45%

# 2. SM_ACTIVE 远低于 util → GPU 在等数据

# 3. 检查 dataloader worker 数
grep 'num_workers' train.py
# num_workers=2（8 卡只配 2 个 worker）

# 4. 检查 CPU 利用率
top -bn1 | head -20
# CPU 利用率仅 15%（大量 CPU 空闲，说明 worker 太少）

# 5. 检查数据预处理
py-spy dump --pid $(pgrep -f train.py | head -1)
# 主线程卡在 dataloader __next__()
```

**根因：** dataloader num_workers=2 太少，数据预处理速度跟不上 GPU 消费速度，GPU 在等数据（util 100% 是因为在等 kernel 也算 util）。

**解决：** 增加 `num_workers=8`（每 GPU 至少 4~8 个 worker），添加 `pin_memory=True` 和 `prefetch_factor=4`。

**预防：** 训练启动前做 dataloader 基准测试，对比 GPU 消费速率和数据加载速率，确保 data pipeline 不成为瓶颈。

---

### 案例 11：PFC 风暴全网瘫痪

**症状：** 整个集群所有训练任务吞吐骤降 80%，IB 网络延迟飙升 10 倍，所有节点同时受影响。

**环境上下文：** ConnectX-6 Dx/7 HDR/NDR + DOCA-OFED 24.10 + RoCE v2（华为 CE8850/CE6857 交换机，V200R022），PFC 和 DCQCN 拥塞控制已启用。`mlnx_qos` 来自 OFED 工具包。

**排查过程：**
```bash
# 1. 检查 IB 端口状态
ibstat
# 所有端口 Active，无物理故障

# 2. 检查 IB 计数器
perfquery -x | grep -i 'pfc\|pause\|congestion'
# port_xmit_wait: 暴增 1000x
# port_rcv_pfc_packets: 暴增

# 3. 检查 PFC 配置
cma_roce_mode   # 检查 RoCE 模式
mlnx_qos -i mlx5_0   # 检查 DCQCN/PFC 配置
# PFC 在所有 8 个 TC 上都启用

# 4. 定位源头
# 发现 node-092 的一个 Pod 在做大量 all-to-all 通信
# 导致交换机 buffer 溢出，触发 PFC pause frame 广播
# PFC 广播引发邻居节点暂停发送 → 级联 → 全网 PFC 风暴
```

**根因：** 某节点的异常通信模式触发交换机 buffer 溢出，PFC pause frame 级联传播形成风暴，导致全网拥塞。

**解决：** 立即 kill 异常 Pod；在交换机上配置 PFC deadlock detection 和 auto-recovery；限制 PFC 只在需要的 TC 上启用。

**预防：** PFC storm detection 告警（`port_rcv_pfc_packets` 增速监控），交换机配置 PFC watchdog，训练任务通信模式审计。

---

### 案例 12：MTU 不一致 NCCL 超时

**症状：** 新部署的 16 节点集群，NCCL all_reduce 在大消息（> 4MB）时超时，小消息正常。

**环境上下文：** NCCL 2.18+（CUDA 12.2）+ ConnectX-6 Dx HDR + leaf-spine 交换机拓扑，节点 OS Ubuntu 22.04.5（kernel 5.15），jumbo frame MTU 9000 为 IB/RoCE 集群标准配置。

**排查过程：**
```bash
# 1. NCCL 测试
nccl-tests/build/all_reduce_perf -b 1M -e 16M -f 2 -g 8
# 1M~4M 正常，8M 开始超时

# 2. 检查各节点 MTU
ip link show | grep mtu
# node-001~012: mtu 9000 (jumbo frame)
# node-013~016: mtu 1500 (default)  ← 新加的 4 台节点 MTU 不一致！

# 3. 检查交换机 MTU
# leaf-03（连接 node-013~016）: system mtu 1500
# 其他 leaf: system mtu 9216

# 4. NCCL 大消息需要大 MTU 分片，MTU 不匹配导致分片/重组异常
```

**根因：** 新增 4 台节点所在的 leaf 交换机 MTU 配置为 1500（默认），与集群其他节点的 9000 不一致，大消息通信时出现分片问题导致超时。

**解决：** 统一配置 leaf-03 的 system MTU 为 9216，节点网卡 MTU 统一为 9000。

**预防：** 新机上线 SOP 包含 MTU 一致性检查，自动化配置管理（Ansible）统一全网 MTU。

---

### 案例 13：ECC SBE 持续增长的老化卡

**症状：** gpu-node-056 的 GPU 3 的 aggregate SBE 在 30 天内从 500 增长到 8500，其他卡增速 < 50/月。

**环境上下文：** A100 80GB SXM + driver R550 550.127.08（示例）+ DCGM 3.3.x（指标 `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`），已运行 18 个月。ROW_REMAPPER 字段自 R515+ 可用。

**排查过程：**
```bash
# 1. 趋势分析
# Prometheus 查询 30 天 DCGM_FI_DEV_ECC_SBE_VOL_TOTAL
# GPU 3: 斜率 ≈ 267/day，其他卡 ≈ 1.2/day

# 2. 检查 ROW_REMAPPER
nvidia-smi -q -d ROW_REMAPPER -i 3
# Remapped Due to SBE: 15
# Pending: No

# 3. 运行内存压力测试
dcgmi diag -r 3 -i 3   # Level-3 诊断含 memory stress
# 测试过程中 SBE 新增 340

# 4. 检查温度
# GPU 3 温度与其他卡一致，排除温度因素
```

**根因：** GPU 3 的 HBM 显存存在老化缺陷，SBE 增速远超正常水平，虽然目前可纠正但趋势表明即将出现 DBE。

**解决：** 将该节点标记为 suspect，安排下一次维护窗口换卡（非紧急但需要计划）。

**预防：** 建立 ECC SBE 增速告警（日增速 > 50 WARN，> 200 P3），定期对比全集群 ECC 趋势，提前识别老化卡。

---

### 案例 14：OpenSM 主备切换引发训练中断

**症状：** 训练任务突然 NCCL timeout，所有节点同时通信中断约 30 秒后恢复。

**环境上下文：** Quantum-2 QM9700 NDR 交换机（固件 3.10.xxxx）+ OpenSM 主备部署（随 DOCA-OFED 24.10），双电源 PDU 配置，NCCL 2.18+（默认 timeout 30s，`NCCL_TIMEOUT` 环境变量可调）。

**排查过程：**
```bash
# 1. 检查 IB 网络事件
journalctl -u opensm --since "1 hour ago"
# "Subnet manager on switch-ib-01 port 0 lost"
# "Standby SM on switch-ib-02 taking over as master"
# "Subnet reconfiguration completed"

# 2. 检查 IPMI SEL
ipmitool -I lanplus -H switch-ib-01-bmc -U admin -P xxx sel list | tail -10
# "Power supply AC lost"
# 交换机 switch-ib-01 电源中断

# 3. 时间对齐
# OpenSM 主备切换时间 = NCCL timeout 时间 = 14:32:15
# 切换耗时约 28 秒（IB 子网重新配置时间）
```

**根因：** IB spine 交换机 switch-ib-01 电源故障，OpenSM master SM 切换至 switch-ib-02，切换期间 IB 子网重新配置导致约 30 秒通信中断。

**解决：** 确认 switch-ib-01 电源恢复；NCCL 设置 `NCCL_TIMEOUT=120`（默认 30s 太短）；训练任务设置 checkpoint 间隔 < 10 min 以减少重计算损失。

**预防：** 双电源供电（两路独立 PDU），OpenSM 配置快速 failover（减少切换时间），IB 交换机健康监控（PSU/DDM/温度）。

---

### 案例 15：checkpoint 写慢拖垮 GPU 利用率

**症状：** 训练任务每 2000 step 写一次 checkpoint（约 45GB），写入期间 GPU 利用率降至 0%，持续约 120 秒。

**环境上下文：** PyTorch 2.x + CUDA 12.x + Lustre 2.15.x 并行文件系统（写入带宽 380MB/s），`torch.save()` 同步写入。本地 NVMe（ext4/xfs）可用做异步中转。

**排查过程：**
```bash
# 1. 定位卡顿时间
# 训练日志显示 step 2000, 4000, 6000 时出现 120s 停顿

# 2. 检查 I/O
iostat -x 1 | grep -A2 'md0'   # 存储挂载点
# 写入期间：await=850ms, wMB/s=380MB/s
# 正常时：await=2ms, wMB/s=0

# 3. 检查 checkpoint 写入方式
grep -A10 'save_checkpoint' train.py
# torch.save(model.state_dict(), path)  ← 同步写入，阻塞训练

# 4. 存储带宽
# 45GB / 380MB/s ≈ 118s → 与观测到的 120s 吻合
```

**根因：** checkpoint 同步写入 45GB 到并行文件系统（写入带宽 380MB/s），耗时 120 秒，期间 GPU 完全空闲。

**解决：**
1. 改为异步写入：先写本地 NVMe（2 秒完成），再后台拷贝到并行文件系统
2. 使用 `torch.save` 的 `fsspec` 异步后端
3. 或采用 ZeRO-Offload 风格的 checkpoint 分片写入

**预防：** checkpoint 写入延迟监控，训练代码模板中默认使用异步 checkpoint 方案。

---

## 9. 应急工具箱

### 9.1 一键现场采集脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# emergency_collect.sh — 故障现场一键采集
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / DCGM 3.3+ / K8s 1.31
# 用法：sudo bash emergency_collect.sh [description]
# 输出：/tmp/emergency_<hostname>_<timestamp>.tar.gz
# 原则：在重启/重置之前运行此脚本保留现场
# 工具版本依赖：
#   - nvidia-bug-report.sh: 随驱动安装，R550+ 输出含 GSP 固件版本与 NVLink 4/5 状态
#   - ipmitool: 1.8.18+（Ubuntu 22.04）/ 1.8.19（Rocky 9）
#   - perfquery/ibstat/iblinkinfo: DOCA-OFED 24.10 或 infiniband-diags
#   - kubectl: K8s 1.31，--field-selector 语法稳定
#   - nvidia-smi -q -d ROW_REMAPPER: R515+ 才有此字段
#===============================================================================

set -euo pipefail

DESC="${1:-undocumented}"
HOSTNAME=$(hostname)
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
COLLECT_DIR="/tmp/emergency_${HOSTNAME}_${TIMESTAMP}"
COLLECT_TAR="/tmp/emergency_${HOSTNAME}_${TIMESTAMP}.tar.gz"

mkdir -p "$COLLECT_DIR"

echo "=== 应急现场采集 ==="
echo "描述: $DESC"
echo "时间: $TIMESTAMP"
echo "目录: $COLLECT_DIR"

# 1. dmesg（最重要的现场证据）
echo "[1/15] dmesg..."
dmesg -T > "$COLLECT_DIR/dmesg.txt" 2>&1
dmesg -T | grep -iE 'xid|nvrm|error|fail|aer|mce|gpu|panic|oops' > "$COLLECT_DIR/dmesg_errors.txt" 2>&1 || true

# 2. nvidia-smi 全量
echo "[2/15] nvidia-smi..."
nvidia-smi > "$COLLECT_DIR/nvidia-smi.txt" 2>&1
nvidia-smi -q > "$COLLECT_DIR/nvidia-smi_full.txt" 2>&1
nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,driver_version,temperature.gpu,power.draw,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv > "$COLLECT_DIR/gpu_summary.csv" 2>&1

# 3. nvidia-bug-report
echo "[3/15] nvidia-bug-report..."
nvidia-bug-report.sh 2>/dev/null
cp /var/log/nvidia-bug-report.log.gz "$COLLECT_DIR/" 2>/dev/null || true

# 4. IPMI SEL
echo "[4/15] IPMI SEL..."
ipmitool sel list > "$COLLECT_DIR/ipmi_sel.txt" 2>&1 || true
ipmitool sel elist > "$COLLECT_DIR/ipmi_sel_extended.txt" 2>&1 || true
ipmitool sensor list > "$COLLECT_DIR/ipmi_sensors.txt" 2>&1 || true

# 5. 系统日志
echo "[5/15] 系统日志..."
journalctl -b --no-pager > "$COLLECT_DIR/journalctl_boot.txt" 2>&1
journalctl -u kubelet --since "2 hours ago" --no-pager > "$COLLECT_DIR/kubelet.log" 2>&1 || true
journalctl -u containerd --since "2 hours ago" --no-pager > "$COLLECT_DIR/containerd.log" 2>&1 || true

# 6. K8s 状态
echo "[6/15] K8s 状态..."
kubectl get pods -A -o wide --field-selector spec.nodeName="$HOSTNAME" > "$COLLECT_DIR/k8s_pods.txt" 2>&1 || true
kubectl get events --sort-by='.lastTimestamp' -A | tail -50 > "$COLLECT_DIR/k8s_events.txt" 2>&1 || true
kubectl describe node "$HOSTNAME" > "$COLLECT_DIR/k8s_node.txt" 2>&1 || true

# 7. IB 网络状态
echo "[7/15] IB 状态..."
ibstat > "$COLLECT_DIR/ibstat.txt" 2>&1 || true
iblinkinfo > "$COLLECT_DIR/iblinkinfo.txt" 2>&1 || true
perfquery -x > "$COLLECT_DIR/perfquery.txt" 2>&1 || true

# 8. NVLink
echo "[8/15] NVLink..."
nvidia-smi nvlink --status > "$COLLECT_DIR/nvlink_status.txt" 2>&1 || true
nvidia-smi nvlink --errors > "$COLLECT_DIR/nvlink_errors.txt" 2>&1 || true

# 9. PCIe 信息
echo "[9/15] PCIe..."
lspci -vvv > "$COLLECT_DIR/lspci.txt" 2>&1 || true
nvidia-smi -q -d PCIE > "$COLLECT_DIR/pcie_gpu.txt" 2>&1 || true

# 10. 进程信息
echo "[10/15] 进程..."
ps auxf > "$COLLECT_DIR/ps.txt" 2>&1
cat /proc/interrupts > "$COLLECT_DIR/interrupts.txt" 2>&1
lsmod > "$COLLECT_DIR/lsmod.txt" 2>&1

# 11. 内核信息
echo "[11/15] 内核..."
cat /proc/version > "$COLLECT_DIR/kernel_version.txt" 2>&1
uname -a > "$COLLECT_DIR/uname.txt" 2>&1

# 12. 磁盘状态
echo "[12/15] 磁盘..."
df -h > "$COLLECT_DIR/df.txt" 2>&1
df -i > "$COLLECT_DIR/df_inode.txt" 2>&1
mount > "$COLLECT_DIR/mount.txt" 2>&1

# 13. 网络状态
echo "[13/15] 网络..."
ip addr > "$COLLECT_DIR/ip_addr.txt" 2>&1
ip route > "$COLLECT_DIR/ip_route.txt" 2>&1
ss -s > "$COLLECT_DIR/ss_summary.txt" 2>&1

# 14. ECC 详细
echo "[14/15] ECC..."
nvidia-smi -q -d ECC > "$COLLECT_DIR/ecc_detail.txt" 2>&1 || true
nvidia-smi -q -d ROW_REMAPPER > "$COLLECT_DIR/row_remapper.txt" 2>&1 || true

# 15. 驱动信息
echo "[15/15] 驱动..."
cat /proc/driver/nvidia/version > "$COLLECT_DIR/nvidia_driver_version.txt" 2>&1 || true
nvidia-smi --query-gpu=driver_version,vbios_version --format=csv > "$COLLECT_DIR/gpu_versions.csv" 2>&1 || true

# 打包
echo "打包..."
tar czf "$COLLECT_TAR" -C /tmp "$(basename "$COLLECT_DIR")"
rm -rf "$COLLECT_DIR"

echo "=== 采集完成 ==="
echo "文件: $COLLECT_TAR"
echo "大小: $(du -h "$COLLECT_TAR" | awk '{print $1}')"
echo "描述: $DESC"
echo ""
echo "请将此文件保存，作为故障分析的依据"
```

### 9.2 GPU 软重置序列脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# gpu_soft_reset.sh — GPU 软重置序列
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / K8s 1.31
# 用法：sudo bash gpu_soft_reset.sh <gpu_index>
# 场景：XID 错误后尝试不重启节点恢复 GPU
# 注意：如果 GPU 完全不可达（XID 79），此脚本无效，需要重启
# 版本说明：
#   - nvidia-smi -r (GPU reset): 仅对非 XID 79 的 GPU 有效，R550+ 支持逐卡重置
#   - rmmod nvidia_uvm: 需要该模块未被其他进程占用，否则需先杀进程
#===============================================================================

set -euo pipefail

GPU_IDX="${1:?用法: gpu_soft_reset.sh <gpu_index>}"
MAX_WAIT=30

echo "=== GPU 软重置序列 ==="
echo "目标: GPU $GPU_IDX"

# 1. 检查 GPU 是否可达
echo "[1/6] 检查 GPU 可达性..."
if ! nvidia-smi -i "$GPU_IDX" &>/dev/null; then
    echo "FAIL: GPU $GPU_IDX 不可达，软重置无法执行，需要重启节点"
    exit 1
fi

# 2. 停止所有使用该 GPU 的进程
echo "[2/6] 停止 GPU 相关进程..."
GPU_PIDS=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader -i "$GPU_IDX" 2>/dev/null || echo "")
if [[ -n "$GPU_PIDS" ]]; then
    echo "  发现进程: $GPU_PIDS"
    for pid in $GPU_PIDS; do
        echo "  发送 SIGTERM 到 PID $pid"
        kill -15 "$pid" 2>/dev/null || true
    done
    sleep 5
    
    # 检查是否还有残留
    REMAINING=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader -i "$GPU_IDX" 2>/dev/null || echo "")
    if [[ -n "$REMAINING" ]]; then
        echo "  残留进程，发送 SIGKILL..."
        for pid in $REMAINING; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 2
    fi
else
    echo "  无运行中的 GPU 进程"
fi

# 3. GPU 重置
echo "[3/6] 执行 GPU 重置..."
nvidia-smi -r -i "$GPU_IDX" || {
    echo "FAIL: GPU 重置失败"
    exit 1
}
sleep 3

# 4. 重载驱动模块（仅该 GPU 相关的部分）
echo "[4/6] 重载 UVM 模块..."
rmmod nvidia_uvm 2>/dev/null || true
modprobe nvidia_uvm
sleep 2

# 5. 验证 GPU 状态
echo "[5/6] 验证 GPU..."
if nvidia-smi -i "$GPU_IDX"; then
    echo "  nvidia-smi 成功"
else
    echo "FAIL: nvidia-smi 仍然失败"
    exit 1
fi

# 检查 ECC
DBE=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits -i "$GPU_IDX" 2>/dev/null | xargs)
if [[ "$DBE" -gt 0 ]] 2>/dev/null; then
    echo "WARN: volatile DBE = $DBE，GPU 可能存在硬件问题"
fi

# 6. 简单计算验证
echo "[6/6] 简单计算验证..."
python3 -c "
import subprocess
result = subprocess.run(['nvidia-smi', '-i', '$GPU_IDX', '--query-gpu=clocks.current.sm', '--format=csv,noheader,nounits'], capture_output=True, text=True)
clock = int(result.stdout.strip())
print(f'  SM Clock: {clock} MHz')
if clock < 300:
    print('  WARN: SM 时钟过低')
else:
    print('  OK: SM 时钟正常')
" 2>/dev/null || echo "  跳过计算验证（无 python3）"

echo "=== 软重置完成 ==="
echo "GPU $GPU_IDX 已恢复，但请持续观察是否复发"
echo "如果 1h 内再次出现 XID 错误，建议重启节点或触发 RMA"
```

### 9.3 节点快速隔离脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# quick_isolate.sh — 节点快速隔离（一键执行，适用于紧急场景）
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / K8s 1.31 (kubectl)
# 用法：bash quick_isolate.sh <node-name> <reason>
# 特点：快速、不交互、适合 P1 告警响应
# 版本说明：
#   - kubectl drain --delete-emptydir-data: K8s 1.20+（替代旧 --delete-local-data）
#   - kubectl drain --force: 允许驱逐非 DaemonSet 管理的 Pod
#===============================================================================

set -euo pipefail

NODE="${1:?用法: quick_isolate.sh <node-name> <reason>}"
REASON="${2:?请指定原因}"

echo "=== 快速隔离 $NODE ==="
echo "原因: $REASON"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"

# 1. 立即 Cordon（阻止新调度）
echo "[1/3] Cordon..."
kubectl cordon "$NODE" 2>/dev/null && echo "  ✅ Cordon 成功" || echo "  ⚠️ Cordon 失败（可能已 cordon）"

# 2. Drain（300s 超时后强制）
echo "[2/3] Drain..."
kubectl drain "$NODE" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=30 \
    --timeout=300s \
    --force 2>/dev/null && echo "  ✅ Drain 成功" || {
    echo "  ⚠️ Drain 超时或失败，强制清理..."
    kubectl get pods -A -o json --field-selector spec.nodeName="$NODE" 2>/dev/null | \
        jq -r '.items[] | "\(.metadata.namespace) \(.metadata.name)"' | \
        while read -r ns pod; do
            kubectl delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
        done
}

# 3. 打标签
echo "[3/3] 标记..."
kubectl label node "$NODE" \
    gpu-cluster.io/health-status=maintenance \
    "gpu-cluster.io/isolate-reason=$REASON" \
    "gpu-cluster.io/isolate-time=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --overwrite

kubectl taint nodes "$NODE" \
    "gpu-cluster.io/maintenance=true:NoExecute" --overwrite 2>/dev/null || true

echo "=== 隔离完成 ==="
echo "节点 $NODE 已隔离（maintenance），存量 Pod 已驱逐"
echo ""
echo "后续步骤："
echo "  1. 通知相关训练任务 owner"
echo "  2. SSH 到节点诊断: ssh $NODE"
echo "  3. 现场采集: sudo bash /opt/scripts/emergency_collect.sh '$REASON'"
echo "  4. 如需 RMA: sudo bash /opt/scripts/rma_evidence.sh"
```

---

> **上一分册：[07-日常运维与节点管理](./07-日常运维与节点管理.md)**
