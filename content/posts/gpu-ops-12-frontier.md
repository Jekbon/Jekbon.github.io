---
title: "GPU 集群运维（12）：前沿方向与异构算力"
date: 2026-11-18T09:00:00+08:00
draft: false
description: "超节点时代、液冷运维、网络演进、国产算力运维、DRA 与调度前沿、大规模训练容错、推理运维前沿、算力运营前沿，以及学习资源与信息渠道清单。"
tags: ["GPU", "前沿", "异构算力"]
series: "GPU 集群运维"
---

## 本册环境基线

| 组件 | 基线版本 | 说明 |
|------|---------|------|
| Kubernetes | 1.31.x | DRA resource.k8s.io/v1beta1（1.31 Beta；1.32 v1beta2，以 `kubectl api-versions \| grep resource` 为准） |
| NVIDIA Driver | R570 生产分支（570.124.04 示例） | GB200 NVL72 必须 R570+ |
| CUDA | 12.8 | GB200/Blackwell 必须 12.8+ |
| NVSwitch 固件 | 随 Fabric Manager 配套（版本与驱动严格一致） | NVL72 Switch Tray 固件管理 |
| 昇腾 CANN | 8.0.x + 配套固件 | npu-smi 工具链独立，固件须与 CANN 版本配套 |
| HAMi | v2.5.x | 异构统一调度中间件 |
| vLLM | 0.7.x | 推理框架参考版本 |
| Volcano | v1.10.x | scheduling.volcano.sh/v1beta1 |
| MegaScale 论文 | NSDI 2024 | 字节万卡训练基础设施 |
| HPN 论文 | SIGCOMM 2023 | GPU 集群高性能网络设计 |
| OS 基线 | Ubuntu 22.04 LTS / Rocky Linux 9.x | 内核 5.15/5.14 GA |

---



GPU 集群运维正在经历从"管卡"到"管超节点"、从"同构"到"异构"、从"手动"到"智能化"的范式跃迁。本分册覆盖超节点架构、液冷、网络演进、国产算力、DRA 调度、大规模容错、推理运维及算力运营等前沿方向。

---

## 1. 超节点时代

### 1.1 GB200/GB300 NVL72 架构详解

**NVL72 物理架构（以 GB200 NVL72 为参考）：**

| 组件 | 规格 | 说明 |
|------|------|------|
| Compute Tray | 每 tray 含 2 颗 Grace CPU + 4 颗 B200 GPU | NVL72 = 18 个 compute tray |
| Switch Tray | 每 tray 含 NVLink Switch 芯片 | NVL72 = 9 个 switch tray（每个 switch tray 含 2 颗 NVSwitch5） |
| NVLink Domain | 72 颗 GPU 全互联 | 900 GB/s 双向带宽（NVLink5，每 GPU 18 条 link） |
| 单机柜功耗 | 120kW+ | 液冷 mandatory（风冷上限约 40kW/柜） |
| HBM 容量 | 72 × 192GB = 13.8TB HBM3e | 单 domain 内统一显存寻址 |
| FP8 算力 | 约 720 PFLOPS | 72 × 10 PFLOPS（B200 单卡） |

**Compute Tray 结构：**

```text
┌──────────────────────────────────────┐
│  Compute Tray (× 18 per NVL72 rack) │
│                                      │
│  ┌─────────┐    ┌─────────┐         │
│  │ Grace   │    │ Grace   │         │
│  │ CPU     │    │ CPU     │         │
│  └────┬────┘    └────┬────┘         │
│       │              │               │
│  ┌────┴────┐   ┌────┴────┐         │
│  │ B200    │   │ B200    │         │
│  │ GPU     │   │ GPU     │         │
│  └────┬────┘   └────┬────┘         │
│       │              │               │
│  ┌────┴────┐   ┌────┴────┐         │
│  │ B200    │   │ B200    │         │
│  │ GPU     │   │ GPU     │         │
│  └─────────┘   └─────────┘         │
└──────────────────────────────────────┘
```

**NVLink Switch Tray 结构：**

```text
┌──────────────────────────────────────┐
│  Switch Tray (× 9 per NVL72 rack)   │
│                                      │
│  ┌─────────────┐ ┌─────────────┐    │
│  │ NVSwitch5   │ │ NVSwitch5   │    │
│  │ 144 ports   │ │ 144 ports   │    │
│  │ 1.8 TB/s    │ │ 1.8 TB/s    │    │
│  └─────────────┘ └─────────────┘    │
│  (每 switch 连接所有 72 GPU)         │
└──────────────────────────────────────┘
```

### 1.2 运维范式变化

| 维度 | 传统节点时代 | 超节点时代 |
|------|------------|-----------|
| 故障域 | 单节点（1~8 卡） | 单机柜（72 卡 domain） |
| 最小可更换单元 | GPU 卡 | Compute Tray / Switch Tray |
| 散热方式 | 风冷可选 | 液冷 mandatory |
| 调度粒度 | 节点/卡 | NVLink domain / tray |
| 故障影响 | 单训练任务 | 整个 domain 内任务 |
| 部署方式 | 上架 + 网线 | 预组装机柜 + 液冷管路 |
| 监控粒度 | 卡级指标 | tray 级 + link 级 + 温度矩阵 |

**NVLink Switch Tray 运维要点：**

> **软件前提（基线 2026-01）：** GB200 NVL72 运维要求 NVIDIA Driver **≥R570**（Production Branch，570.124.04 示例）、CUDA **≥12.8**、配套 NVSwitch5 固件（随 Fabric Manager 与驱动**完全同版本**，见 00-基线 §2.1）。Blackwell 架构不支持 R550 及以下驱动分支。Fabric Manager 版本不匹配 = NVLink domain 不可用。

- Switch Tray 故障 = 整个 NVL72 domain 通信降级或中断
- Switch Tray 更换需要停整个机柜（非热插拔）
- 监控 NVSwitch 温度/带宽利用率/错误计数（通过 NVSwitch Management）
- NVLink 误码率监控：`nvidia-smi nvlink -e` 关注 Replay/Error 计数器

**液冷 mandatory 的运维影响：**

- 机柜交付周期从 1~2 周延长到 4~8 周（液冷管路施工）
- 需要冷却液运维团队（防冻液管理、水质检测、漏液应急预案）
- 机房楼板承重评估（单柜 1500kg+）
- 电力配套（单柜 120kW 需要独立 PDU + UPS 分支）

### 1.3 NVL36/576 演进路线

| 配置 | GPU 数量 | NVLink Domain | 功耗 | 适用场景 |
|------|---------|---------------|------|---------|
| NVL36 | 36 GPU | 36 卡 domain | ~60kW | 中等训练、推理 |
| NVL72 | 72 GPU | 72 卡 domain | ~120kW | 大模型训练 |
| NVL576 | 576 GPU | 跨 8 机柜 | ~1MW | 超大模型（万亿参数） |

**NVL576 的关键运维挑战：**

- 跨机柜 NVLink 互联（铜缆距离限制 ~2m，需 IB/Spectrum-X 补充跨柜通信）
- 8 个 NVL72 机柜组成的"超级 pod"，故障域扩展到 pod 级别
- 冷却液分配从单机柜到 manifold 级别（CDU 容量 ≥ 1MW）

### 1.4 对调度的影响（NVLink domain 感知调度）

**调度需求：训练任务必须调度到同一 NVLink domain 内的 GPU 集合。**

```yaml
# K8s 1.31 — v1 Node labels（domain 感知调度，标签由 GPU Operator 或运维手工注入）
# 使用 NodeLabel + NodeAffinity 实现 domain 感知调度
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-01
  labels:
    nvidia.com/nvlink-domain: "nvld-01"
    nvidia.com/compute-tray: "tray-01"
    nvidia.com/rack: "rack-A01"
---
# batch/v1 Job（K8s 1.31 长期稳定）— 训练 Job 的 domain 亲和性
apiVersion: batch/v1
kind: Job
metadata:
  name: llm-pretrain-70b
  namespace: tenant-team-a
spec:
  template:
    spec:
      nodeSelector:
        nvidia.com/nvlink-domain: "nvld-01"
      containers:
        - name: trainer
          resources:
            limits:
              nvidia.com/gpu: 72  # 整个 domain
      tolerations:
        - key: "nvidia.com/gpu"
          operator: "Exists"
          effect: "NoSchedule"
```

**更精细的拓扑感知调度（Volcano v1.10.x，scheduling.volcano.sh/v1beta1 + topology-aware plugin）：**

```yaml
# Volcano v1.10.x — scheduling.volcano.sh/v1beta1
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: training-queue
spec:
  weight: 10
  reclaimable: true
  capability:
    nvidia.com/gpu: 72
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: llm-pretrain-pg
spec:
  minMember: 1
  queue: training-queue
  # 拓扑约束：所有 Pod 必须在同一 NVLink domain
  topologyPolicy: "single-domain"  # 概念性配置，实际实现取决于 scheduler 版本
```

---

## 2. 液冷运维

### 2.1 冷板式 vs 浸没式对比

| 特性 | 冷板式（Direct Liquid Cooling） | 浸没式（Immersion Cooling） |
|------|-------------------------------|---------------------------|
| 原理 | 冷板贴合芯片，冷却液流过冷板 | 整个服务器浸泡在介电液体中 |
| 冷却液 | 水/乙二醇混合液 | 氟化液（3M Novec / Solvay） |
| GPU 兼容性 | NVIDIA NVL 系列标配 | 需要定制 server 机箱 |
| PUE 收益 | 1.1~1.2 | 1.02~1.05 |
| 维护复杂度 | 中等（管路/漏液/水质） | 高（液体更换/蒸发损耗） |
| 成本（CAPEX） | 中等 | 高（液体贵，约 $50-100/L） |
| 成本（OPEX） | 低 | 中等（液体补充/过滤） |
| 主流程度 | 当前主流（NVL72 标配） | 特定场景（高密度矿机/研究） |
| 对运维的影响 | 需要液冷运维技能 | 全新运维范式 |

### 2.2 冷板式液冷运维要点

**CDU（Coolant Distribution Unit）运维：**

| 检查项 | 频率 | 正常范围 | 异常处理 |
|--------|------|---------|---------|
| CDU 供水温度 | 实时 | 18~25°C | 检查一次侧冷却塔/冷水机组 |
| CDU 回水温度 | 实时 | 35~45°C | 检查流量/冷板接触 |
| 二次侧流量 | 每小时 | 设计值的 90~110% | 检查泵/管路堵塞 |
| 二次侧压力 | 每小时 | 设计值 ±10% | 检查漏液/气堵 |
| CDU 泵运行状态 | 实时 | 双泵主备轮换 | 备用泵故障需立即修复 |
| CDU 过滤器压差 | 每周 | < 设计值的 50% | 压差过高更换滤芯 |

**二次侧水质管理：**

```bash
# 水质检测指标与标准（以去离子水 + 乙二醇为例）
# 检测频率：每月
# 记录到 CMDB / 运维平台

# pH 值：7.0 ~ 8.5
# 电导率：< 50 μS/cm（过高说明离子污染）
# 浊度：< 1 NTU
# 细菌总数：< 1000 CFU/mL
# 乙二醇浓度：25~35%（防冻 + 防腐）
# 缓蚀剂浓度：按厂商推荐

# 水质超标处理 SOP：
# 1. 电导率超标 → 部分换水 + 补充去离子水
# 2. 细菌超标 → 添加杀菌剂（需兼容乙二醇体系）
# 3. pH 偏低 → 检查是否有金属腐蚀，添加缓冲液
```

**漏液检测绳部署：**

| 位置 | 检测方式 | 告警级别 |
|------|---------|---------|
| 机柜底部 | 水浸检测绳（沿管路铺设） | P1（立即响应） |
| CDU 下方 | 点式水浸传感器 | P1 |
| 快接头附近 | 水浸检测贴片 | P2（1h 内排查） |
| 架空地板下（如有） | 区域水浸传感器 | P2 |

**快接头运维：**

- 类型：干式断开接头（dry-break）/ 湿式断开接头
- NVL72 推荐干式断开（断开时不漏液）
- 插拔次数限制：按厂商规格（通常 5000 次以上）
- 定期检查 O-ring 老化情况（每 6 个月）
- 断开前确保该 tray 已关机/降载

### 2.3 液冷机房改造要点

| 改造项 | 要求 | 参考标准 |
|--------|------|---------|
| 楼板承重 | ≥ 1500 kg/m²（NVL72 单柜约 1500kg） | 结构工程师评估 |
| 层高 | ≥ 4.5m（架空管路 + 机柜 + 维护空间） | - |
| 冷却液管路 | 304/316L 不锈钢；焊接连接优先 | ASHRAE TC9.9 |
| 一次侧供水 | 冷水机组 + 冷却塔冗余（N+1） | - |
| 消防 | 液冷不影响现有消防；浸没式需特殊灭火方案 | NFPA 75/76 |
| 电力 | 单柜独立 PDU（A+B 路）；120kW/柜需 3 相供电 | - |
| 环境监控 | 温湿度矩阵 + 漏液全局监控 | ASHRAE A1~A4 |

### 2.4 PUE 收益数据参考

| 部署方式 | PUE 典型值 | 说明 |
|---------|-----------|------|
| 纯风冷（传统） | 1.4~1.6 | 精密空调 + 冷热通道 |
| 冷板液冷 + 风冷辅助 | 1.1~1.2 | GPU 液冷，其他风冷 |
| 全浸没式 | 1.02~1.05 | 几乎无风扇能耗 |
| 液冷 + 自然冷却（冬季） | 1.05~1.10 | 寒冷地区优势显著 |

---

## 3. 网络演进

### 3.1 800G 网络

| 技术 | 标准 | 速率 | 延迟 | GPU 集群适用场景 |
|------|------|------|------|----------------|
| IB NDR | HDR→NDR | 400Gbps | ~1μs | 当前主流训练网络 |
| IB XDR | XDR（规划中） | 800Gbps | ~1μs | 下一代 NVL576 跨柜互联 |
| 以太 800G | IEEE 802.3df | 800Gbps | ~3-5μs | RoCE v2 训练；推理网络 |
| 以太 1.6T | IEEE 802.3dj（规划中） | 1.6Tbps | ~3-5μs | 超大规模推理/训练 |

**800G 以太 vs 800G IB 选择要点：**

| 维度 | 800G IB (XDR) | 800G 以太 (RoCE v2) |
|------|---------------|---------------------|
| 延迟 | ~1μs（亚微秒） | ~3-5μs（含 DSCP 调优后） |
| 拥塞控制 | 信用流控（无损） | ECN/PFC（调优复杂） |
| 生态成熟度 | NVIDIA 全栈（ConnectX-8） | 多厂商（Broadcom/Mellanox/Arista） |
| 成本 | 高（IB 交换机/线缆贵 30-50%） | 中等 |
| 运维复杂度 | 低（IB 自管理） | 高（RoCE 调优） |
| 趋势 | NVIDIA 主推（绑定 GPU 生态） | UEC 联盟推动（开放生态） |

### 3.2 224G SerDes

- SerDes（Serializer/Deserializer）是高速信号传输的核心技术
- 224G SerDes = 每通道 224Gbps（PAM4 编码），支撑 800G/1.6T 以太网和 PCIe 6.0
- 对运维的影响：信号完整性要求更高，铜缆距离更短（< 2m for 224G），光互联成为必须
- 机房布线需要更严格的弯曲半径管理和连接器清洁度

### 3.3 CPO（Co-Packaged Optics，共封装光学）

**原理：** 将光模块（光引擎）直接封装到交换机芯片旁边，而非传统可插拔光模块形式。

| 特性 | 传统可插拔光模块 | CPO 共封装光学 |
|------|----------------|---------------|
| 位置 | 交换机面板上可插拔 | 封装在交换机芯片基板上 |
| 功耗 | 每端口 15~20W（800G） | 每端口 5~10W（节省 50%+） |
| 信号完整性 | PCB 走线损耗大 | 短距电信号 + 光信号出板 |
| 可更换性 | 热插拔，故障即换 | 不可单独更换（需返厂） |
| 对运维的影响 | 光模块备件管理 | 交换机整体更换 |
| 成熟时间 | 当前成熟 | 2025~2027 商用 |

**CPO 对运维的影响：**

- 光模块故障不再是"换模块"，而是"换交换机"
- 需要备件策略从"备光模块"转向"备整机"
- 光纤连接器清洁度要求更高（CPO 输出光功率更低）
- 光链路监控方式变化（需要芯片级遥测）

### 3.4 UEC（Ultra Ethernet Consortium，超以太网联盟）

**目标：** 在以太网基础上构建面向 AI/HPC 的无损传输协议，对标 InfiniBand。

| 特性 | InfiniBand | UEC（目标） |
|------|-----------|-------------|
| 拥塞控制 | 信用流控 | 端到端拥塞管理（选择性重传） |
| 多路径 | LAG（静态） | 自适应多路径（packet spray） |
| 故障恢复 | SM 重路由（秒级） | 快速重路由（毫秒级） |
| 生态 | NVIDIA 单一主导 | 开放多厂商 |
| 成本 | 高 | 中（以太网生态规模效应） |

**UEC 与 IB 竞争格局（截至 2026 年参考）：**

- NVIDIA 坚持 IB 路线（NDR/XDR），同时 ConnectX 也支持以太 RoCE
- AMD（收购 Pensando）、Broadcom、Intel 推动 UEC
- 国内互联网厂商（字节、腾讯、阿里）积极评估 UEC 替代 IB
- 实际落地时间线：2025 年规范发布 → 2026~2027 年商用芯片 → 2027~2028 年规模部署

### 3.5 Scale-up vs Scale-out 网络分层趋势

| 层级 | 范围 | 当前技术 | 演进方向 |
|------|------|---------|---------|
| Scale-up（节点内） | GPU ↔ GPU | NVLink（900GB/s per GPU） | NVLink6（1.8TB/s+） |
| Scale-up（机柜内） | NVLink Domain | NVSwitch5（全互联） | 更大 domain（576 GPU） |
| Scale-out（跨柜） | 机柜 ↔ 机柜 | IB NDR 400G / RoCE 400G | IB XDR 800G / 以太 800G |
| Scale-out（跨集群） | 数据中心间 | IB fat-tree / Dragonfly | 光交换（OCS）+ 路由优化 |

---

## 4. 国产算力运维

### 4.1 昇腾 910B 体系

**CANN 软件栈与 CUDA 对照：**

| 层级 | NVIDIA (CUDA) | 华为昇腾 (CANN) | 说明 |
|------|--------------|----------------|------|
| 驱动 | NVIDIA Driver | Ascend Driver | 内核态驱动 |
| 运行时 | CUDA Runtime | ACL (Ascend Computing Language) | 设备管理/内存管理 |
| 数学库 | cuBLAS/cuDNN | HiBLAS/HiDNN | BLAS/DNN 加速 |
| 通信库 | NCCL | HCCL | 集合通信（AllReduce 等） |
| 算子编译 | CUDA JIT / cuBIN | TBE/TIK + AI CPU | 自定义算子开发 |
| 框架适配 | PyTorch (torch.cuda) | PyTorch (torch_npu) | 华为维护 NPU 适配分支 |
| 推理框架 | TensorRT | MindSpore Lite / ATC | 模型优化 + 推理 |
| 性能分析 | Nsight Systems/Compute | msprof | 性能分析工具 |

**npu-smi info 常用命令与 nvidia-smi 对照（CANN 8.0.x，npu-smi 工具版本须与 CANN/驱动固件配套，升级时整体更新）：**

```bash
# CANN 8.0.x — npu-smi 命令参考
# 查看所有 NPU 状态（对应 nvidia-smi）
npu-smi info

# 查看指定 NPU 详细信息（对应 nvidia-smi -q -i 0）
npu-smi info -t board -i 0

# 查看 NPU 温度（对应 nvidia-smi -q -d TEMPERATURE）
npu-smi info -t usages -i 0

# 查看 NPU 内存使用（对应 nvidia-smi -q -d MEMORY）
npu-smi info -t usages -i 0
# 输出中关注：HBM Usage / HBM Temperature

# 查看 NPU 功耗（对应 nvidia-smi -q -d POWER）
npu-smi info -t board -i 0
# 输出中关注：NPU Power

# 查看进程使用（对应 nvidia-smi pmon）
npu-smi info -t process

# 设置功耗上限（对应 nvidia-smi -pl）
npu-smi set -t dpm -i 0 -c 1  # 动态功耗管理

# 重置 NPU（对应 nvidia-smi -r）
npu-smi set -t reset -i 0
```

**MindCluster/HCCL 与 NCCL 对照：**

| 功能 | NCCL (NVIDIA) | HCCL (华为) |
|------|--------------|-------------|
| AllReduce | `ncclAllReduce` | `HcclAllReduce` |
| AllGather | `ncclAllGather` | `HcclAllGather` |
| 初始化 | `ncclCommInitRank` | `HcclCommInitRootInfo` |
| 拓扑发现 | 自动（NVLink/PCIe/IB） | 自动（HCCS/RDMA） |
| 环境变量 | `NCCL_DEBUG=INFO` | `HCCL_EXEC_TIMEOUT=600` |
| 故障恢复 | NCCL 2.20+ 支持 | HCCL 支持 in-job restart |

**HCCS（Huawei Cache Coherent System）互联：**

- 类比 NVLink，HCCS 是昇腾 NPU 间的高速互联
- 910B 的 HCCS 带宽：每 link 约 28GB/s（vs NVLink4 的 50GB/s per link）
- 同一台服务器内 NPU 通过 HCCS 全互联
- 跨服务器通过 RoCE（RDMA over Converged Ethernet）

### 4.2 海光 DCU（ROCm 体系）

```bash
# rocm-smi 常用命令
rocm-smi                    # 基础状态（对应 nvidia-smi 简略输出）
rocm-smi --showtemp         # 温度详情
rocm-smi --showmeminfo vram # 显存使用详情
rocm-smi --showpower        # 功耗信息
rocm-smi --showpids         # 进程使用信息
rocm-smi --showproductname  # 设备型号
rocm-smi --setperflevel high # 设置性能模式

# rocminfo 查看设备拓扑
rocminfo | grep -A 20 "Agent"

# rocm_bandwidth_test 测试 GPU 间带宽
rocm_bandwidth_test -t     # 拓扑测试
rocm_bandwidth_test -s 0 -d 1  # 指定源和目标
```

### 4.3 寒武纪 MLU

```bash
# cnmon（Cambricon Neuware Monitor）常用命令
cnmon                       # 基础状态（类似 nvidia-smi）
cnmon -t                    # 温度信息
cnmon -p                    # 功耗信息
cnmon --list                # 列出所有设备
cnmon info -t memory -i 0   # 指定设备内存信息

# cntopo 查看设备拓扑（类似 nvidia-smi topo -m）
cntopo
```

### 4.4 异构统一调度（HAMi 架构）

**HAMi（Heterogeneous AI Computing Middleware）架构：**

HAMi 是一个 K8s 中间件，支持多厂商 GPU/NPU 统一调度，提供：
- 虚拟 GPU 切分（类似 MIG/time-slicing，但跨厂商）
- 多厂商 device plugin 统一管理
- 异构资源配额

**HAMi 多厂商卡接入配置示例（HAMi v2.5.x，镜像 tag 以 quay.io/hami/hami 官方发布为准）：**

```yaml
# HAMi v2.5.x — scheduler DaemonSet 部署示例
# 镜像 tag 请以 HAMi GitHub Release 对应版本为准（v2.5.x）
# 1. 部署 HAMi scheduler
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: hami-scheduler
  namespace: hami-system
spec:
  selector:
    matchLabels:
      app: hami-scheduler
  template:
    metadata:
      labels:
        app: hami-scheduler
    spec:
      tolerations:
        - key: "gpu"
          operator: "Exists"
          effect: "NoSchedule"
      containers:
        - name: vgpu-monitor
          image: registry.example.com/hami/scheduler:v2.5.x  # HAMi v2.5.x，tag 以官方 Release 为准
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          volumeMounts:
            - name: device-plugin
              mountPath: /var/lib/kubelet/device-plugins
            - name: lib
              mountPath: /usr/local/vgpu
      volumes:
        - name: device-plugin
          hostPath:
            path: /var/lib/kubelet/device-plugins
        - name: lib
          hostPath:
            path: /usr/local/vgpu
---
# 2. HAMi v2.5.x — v1 ConfigMap 配置多厂商设备
apiVersion: v1
kind: ConfigMap
metadata:
  name: hami-device-config
  namespace: hami-system
data:
  config.json: |
    {
      "nvidia": {
        "enabled": true,
        "resourcePrefix": "nvidia.com",
        "deviceSplitCount": 10,
        "memoryScale": 1
      },
      "ascend": {
        "enabled": true,
        "resourcePrefix": "huawei.com",
        "deviceSplitCount": 8,
        "chipConfig": {
          "910B": {
            "memory": 65536,
            "cores": 32
          }
        }
      },
      "dcu": {
        "enabled": true,
        "resourcePrefix": "hygon.com",
        "deviceSplitCount": 8
      },
      "cambricon": {
        "enabled": true,
        "resourcePrefix": "cambricon.com",
        "deviceSplitCount": 8
      }
    }
---
# 3. HAMi v2.5.x — v1 Pod 租户使用异构资源
apiVersion: v1
kind: Pod
metadata:
  name: multi-vendor-training
  namespace: tenant-team-a
spec:
  containers:
    - name: trainer
      image: registry.example.com/training/pytorch:2.6.0-multi-backend  # 对齐基线 PyTorch 2.6.0
      resources:
        limits:
          # NVIDIA GPU（切分使用）
          nvidia.com/gpu: 1
          nvidia.com/gpumem: 16384  # 16GB 显存
          # 或者昇腾 NPU
          # huawei.com/Ascend910: 1
          # huawei.com/Ascend910-memory: 16384
      env:
        - name: HAMi_VGPU
          value: "1"
```

### 4.5 国产卡运维差异总结

| 维度 | NVIDIA (A100/H100/B200) | 昇腾 910B | 海光 DCU (Z100) | 寒武纪 MLU370/590 |
|------|------------------------|-----------|-----------------|-------------------|
| 监控工具 | nvidia-smi, DCGM | npu-smi | rocm-smi | cnmon |
| 集群管理 | Base Command / DCGM | MindCluster | ROCm SMI | Neuware Manager |
| 驱动更新 | 在线 yum/apt + dkms | 离线 rpm 包为主 | 离线 rpm 包 | 离线 rpm 包 |
| 容器运行时 | nvidia-container-runtime | ascend-container-runtime | ROCm docker | cntech-container |
| 故障率参考 | ~1-2%/年/卡 | ~3-5%/年/卡（参考） | ~3-5%/年/卡（参考） | ~3-5%/年/卡（参考） |
| 生态成熟度 | 最成熟（CUDA 15 年） | 快速追赶（PyTorch 适配好） | ROCm 生态（AMD 主线） | 较弱（自有框架为主） |
| 文档质量 | 极好 | 中等（中文为主） | 中等（英文 ROCm 文档） | 较弱 |
| 社区支持 | 全球社区 | 国内社区为主 | AMD ROCm GitHub | 厂商支持为主 |
| NCCL 替代 | NCCL | HCCL | RCCL (ROCm) | CNCL |
| 算子开发 | CUDA C/C++ | TBE/TIK (C++) | HIP (C++) | BANG C/C++ |
| 主要风险 | 供应受限（出口管制） | 软件兼容性仍在追赶 | 性能略低于同代 NVIDIA | 生态锁定 |

---

## 5. DRA 与调度前沿

### 5.1 DRA 架构

**DRA（Dynamic Resource Allocation）核心概念：**

> **API 版本说明：** DRA `resource.k8s.io` API 随 K8s 版本演进：1.31 为 `v1beta1`，1.32 为 `v1beta2`。查询当前集群可用版本：`kubectl api-versions | grep resource`。本文示例使用 `resource.k8s.io/v1beta1`（K8s 1.31）。

| 概念 | 说明 | 类比 Device Plugin |
|------|------|-------------------|
| ResourceClaim | 工作负载对资源的"申领"（类似 PVC） | 隐式（resource request） |
| ResourceSlice | 节点上可用资源的"切片"描述 | device plugin 注册 |
| DeviceClass | 资源的"分类模板"（类似 StorageClass） | 无对应 |
| DRA Driver | 资源分配的控制器 + 节点代理 | device plugin |

**DRA 资源申领流程：**

```text
用户创建 Pod（引用 ResourceClaim）
  → Scheduler 选择满足 Claim 的节点
  → DRA Driver 在目标节点分配具体设备
  → Kubelet 将设备注入容器
  → Pod 运行
```

### 5.2 DRA vs Device Plugin 对比

| 维度 | Device Plugin | DRA |
|------|--------------|-----|
| K8s 版本 | v1.8+ (GA) | v1.26 Alpha → v1.30+ Beta |
| 资源声明 | `resources.limits` | `ResourceClaim` 对象 |
| 拓扑感知 | 无（需额外实现） | 内置（Structured Parameters） |
| 动态切分 | 不支持（静态注册） | 支持（运行时切分 MIG） |
| 资源组合 | 单类型 | 多类型组合申领 |
| 延迟分配 | 不支持 | 支持（claim 可延迟到调度时） |
| 网络资源 | 不支持 | 可扩展支持 |
| 复杂度 | 低 | 高 |

### 5.3 NVIDIA DRA Driver 能力

**NVIDIA DRA Driver 支持的能力：**

| 能力 | 说明 | K8s 版本要求 |
|------|------|-------------|
| GPU 整卡申领 | 申领完整 GPU | v1.27+ |
| MIG 动态切分 | 运行时创建/销毁 MIG 实例 | v1.28+ |
| 拓扑感知申领 | 感知 NVLink/NVSwitch 拓扑 | v1.30+ |
| 多 GPU 组合申领 | 申领特定拓扑关系的多 GPU | v1.30+ |

**DRA ResourceClaim 示例（申领 MIG 实例，resource.k8s.io/v1beta1，K8s 1.31）：**

```yaml
# resource.k8s.io/v1beta1（K8s 1.31；1.32 升级为 v1beta2）
apiVersion: resource.k8s.io/v1beta1
kind: ResourceClaim
metadata:
  name: mig-1g-10gb-claim
  namespace: tenant-team-a
spec:
  devices:
    requests:
      - name: gpu
        deviceClassName: nvidia-gpu
        selectors:
          - cel:
              expression: "device.attributes['nvidia'].mig_profile == '1g.10gb'"
        count: 2  # 需要 2 个 MIG 1g.10gb 实例
---
# DeviceClass — resource.k8s.io/v1beta1（K8s 1.31）
apiVersion: resource.k8s.io/v1beta1
kind: DeviceClass
metadata:
  name: nvidia-gpu
spec:
  selectors:
    - cel:
        expression: "device.driver == 'gpu.nvidia.com'"
---
# Pod 引用 ResourceClaim
apiVersion: v1
kind: Pod
metadata:
  name: mig-training
  namespace: tenant-team-a
spec:
  containers:
    - name: trainer
      image: registry.example.com/training/pytorch:2.3.0
      resources:
        claims:
          - name: mig-gpu
  resourceClaims:
    - name: mig-gpu
      resourceClaimName: mig-1g-10gb-claim
```

### 5.4 K8s 版本演进时间线

| K8s 版本 | DRA 状态 | apiVersion | 关键特性 |
|---------|---------|------------|---------|
| v1.26 | Alpha | resource.k8s.io/v1alpha2 | DRA 初始实现 |
| v1.27 | Alpha | resource.k8s.io/v1alpha2 | 基本 GPU 申领 |
| v1.28 | Alpha | resource.k8s.io/v1alpha2 | MIG 动态切分 |
| v1.30 | Beta | resource.k8s.io/v1beta1 | Structured Parameters（拓扑感知） |
| v1.31 | Beta | resource.k8s.io/v1beta1 | 性能优化 + AdminAccess |
| v1.32 | Beta | resource.k8s.io/v1beta2 | API 迭代（以官方 migration guide 为准） |

> **查询实际可用 API：** `kubectl api-versions | grep resource`；跨版本升级参见 K8s 官方 migration guide。

**运维建议：** 生产环境建议 K8s v1.30+（DRA Beta），device plugin 作为回退方案。

### 5.5 Gang + DRA 结合展望

**Gang Scheduling + DRA 的结合场景：**

- 分布式训练需要所有 worker 同时启动（Gang），且每个 worker 需要特定拓扑的 GPU（DRA）
- 结合方式：Volcano/Kueue 的 PodGroup 中引用 ResourceClaimTemplate
- 调度器先满足 Gang 约束（所有成员可调度），再满足 DRA 约束（每个成员获得正确的 GPU 拓扑）

**预期演进路线：**

1. Kueue + DRA 集成（v1.31+ 可用）
2. Volcano PodGroup 原生支持 ResourceClaimTemplate
3. 拓扑感知 Gang 调度（NVLink domain 约束 + Gang 约束联合优化）

---

## 6. 大规模训练容错前沿

### 6.1 万卡集群故障率数据参考

| 规模 | 故障率参考值 | 数据来源/场景 |
|------|------------|-------------|
| 1,000 卡 | ~0.1 次/天（每千卡） | 业界公开报告（Meta, 2024） |
| 10,000 卡 | ~1-5 次/天 | Meta Llama 3 训练报告 |
| 16,000 卡 | ~2-3 次/天 | Meta 16K H100 集群报告 |
| 100,000 卡 | ~20-50 次/天（预估） | 线性外推 + 网络故障增加 |

**故障类型分布参考：**

| 故障类型 | 占比（参考） | MTTR |
|---------|------------|------|
| GPU 硬件故障（Xid/ECC） | ~40% | 30-60 min（自动替换） |
| 网络故障（NCCL timeout/IB） | ~25% | 5-30 min |
| 存储故障（checkpoint 写失败） | ~15% | 5-15 min |
| 软件故障（OOM/框架 bug） | ~15% | 2-10 min（重启） |
| 电力/散热故障 | ~5% | 60+ min |

### 6.2 大厂做法要点

**全栈可观测（以 Meta 16K H100 为参考）：**

- GPU 级：Xid 错误实时监控、ECC 错误趋势预测、温度/功耗异常检测
- 网络级：IB 链路误码率监控、NCCL 通信延迟基线对比
- 存储级：checkpoint 写入延迟/失败率监控、Lustre/JuiceFS 客户端健康
- 训练级：loss spike 检测、梯度爆炸/消失检测

**诊断加速：**

- 训练启动时运行"health check"训练 step（空跑 10 步），检测所有 GPU 正常
- 故障发生后自动运行诊断 Pod（nvidia-smi -q + dcgmi diag -r 3）
- 诊断结果缓存，避免重复诊断同一 GPU

**In-Job Restart（训练任务内重启）：**

```python
# 伪代码：PyTorch 弹性训练 + checkpoint 恢复
import torch
import torch.distributed as dist
from torch.distributed.elastic.multiprocessing.errors import record

@record
def train_fn():
    dist.init_process_group(backend="nccl")

    # 加载最新 checkpoint（如果存在）
    latest_ckpt = find_latest_checkpoint("/checkpoints/team-a/llm-70b/")
    if latest_ckpt:
        model.load_state_dict(torch.load(latest_ckpt))
        start_step = latest_ckpt.step + 1
    else:
        start_step = 0

    for step in range(start_step, total_steps):
        try:
            loss = train_step(model, data_loader)
            if step % checkpoint_interval == 0:
                save_checkpoint(model, step, "/checkpoints/team-a/llm-70b/")
        except torch.cuda.CUDAError as e:
            log_error(e)
            # torchrun elastic agent 会自动重启 worker
            raise
```

**热备节点方案：**

| 方案 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| 1% 热备 | 预留 1% 节点作为热备 | 故障恢复快（< 5min） | 资源浪费 |
| 弹性队列 | 故障时从 Kueue 申请新节点 | 资源利用率高 | 恢复慢（10-30min） |
| 影子任务 | 热备节点运行低优先级任务 | 兼顾利用率和恢复速度 | 实现复杂 |

### 6.3 Checkpoint 前沿

**内存 Checkpoint（In-Memory Checkpoint）：**

- 原理：将模型状态保持在 GPU 显存/主机内存中，不写磁盘
- 优势：写入速度快 10-100x（避免存储 IO 瓶颈）
- 风险：节点故障时内存数据丢失（需要多级备份）
- 实现：GPU 0 显存 → 主机内存 → 本地 SSD → 远端对象存储（异步流水线）

**分布式快照异步化：**

```python
# 异步 checkpoint 写入（参考 PyTorch DCP - Distributed Checkpoint）
import torch.distributed.checkpoint as DCP

# 非阻塞保存（后台线程写磁盘）
checkpoint_writer = DCP.FileSystemWriter(
    "/checkpoints/team-a/llm-70b/",
    single_file_per_rank=True,  # 每个 rank 独立文件
    sync_files=False            # 不强制 sync（后台 flush）
)

# 异步保存
async_state = DCP.save_async(
    state_dict={"model": model.state_dict(), "optimizer": optimizer.state_dict()},
    storage_writer=checkpoint_writer
)

# 继续训练，不等待 IO 完成
train_step(...)

# 在下一个 checkpoint 前确认上一个完成
async_state.result()  # 阻塞等待
```

### 6.4 故障预测

**ECC 错误趋势预警：**

| 指标 | 阈值 | 动作 |
|------|------|------|
| Single-bit ECC 错误率 | > 10 次/小时 | 标记为"观察" |
| Single-bit ECC 错误率 | > 100 次/小时 或趋势上升 | 标记为"计划替换" |
| Double-bit ECC 错误 | 任意 1 次 | 立即隔离（Xid 48） |
| 行级 ECC (Row-remap) | 已触发 remap | 标记为"计划替换"（容量降低） |

**温度趋势预警：**

```python
# Prometheus 告警规则
# 温度持续上升趋势（可能散热故障）
# alerting_rules.yaml
groups:
  - name: gpu-thermal-prediction
    rules:
      - alert: GPUTemperatureTrendUp
        expr: |
          predict_linear(DCGM_FI_DEV_GPU_TEMP[1h], 3600) > 85
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "GPU {{ $labels.gpu }} 温度趋势预测 1h 后将超过 85°C"
          description: "当前温度 {{ $value | humanize }}°C，趋势上升，可能散热故障"

      - alert: GPUTemperatureCritical
        expr: DCGM_FI_DEV_GPU_TEMP > 90
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "GPU {{ $labels.gpu }} 温度超过 90°C"
```

---

## 7. 推理运维前沿

### 7.1 推理与训练运维差异

| 维度 | 训练 | 推理 |
|------|------|------|
| SLO 指标 | 吞吐量（tokens/s, samples/s） | 延迟（TTFT, TPOT, E2E latency） |
| 扩缩容 | 不需要（固定资源） | 弹性伸缩（基于 QPS/延迟） |
| 故障影响 | 丢失 checkpoint（可恢复） | 用户请求失败（直接影响 SLA） |
| 资源利用 | 接近 100%（长时间占用） | 波动大（需削峰填谷） |
| 部署模式 | Job/CronJob | Deployment/Service |
| 批处理 | 大数据 batch | 动态 batching（continuous batching） |
| 模型更新 | 天/周级别 | 分钟/小时级别（A/B 测试） |
| 网络需求 | IB/高速互联（NCCL） | 标准以太网即可 |

### 7.2 PD 分离架构

**Prefill/Decode 分离（PD Disaggregation）原理：**

- Prefill（预填充）阶段：计算密集，适合大 batch、高算力 GPU
- Decode（解码）阶段：访存密集，适合低延迟、高显存带宽 GPU
- 分离后各自优化硬件配比，提升整体 GPU 利用率

| 组件 | 说明 | 硬件推荐 |
|------|------|---------|
| Prefill Pool | 处理 prompt 编码 + 首 token 生成 | H100/B200（算力密集） |
| Decode Pool | 处理后续 token 生成 | L40S/A10（访存密集，成本更低） |
| KV Cache 传输 | Prefill → Decode 传输 KV 状态 | 高速网络（25G+ 以太网即可） |
| 调度器 | 请求路由到 Prefill → Decode | Mooncake / Dynamo / 自研 |

**Mooncake（月饼）调度器思路（月之暗面开源）：**

- 将 Prefill 和 Decode 作为独立服务池
- 请求先到 Prefill 节点，完成 prompt 处理后，将 KV Cache 传输到 Decode 节点
- Decode 节点从 KV Cache 继续生成后续 token
- 调度器根据各池负载动态分配

### 7.3 KV Cache 分层存储

| 层级 | 介质 | 容量 | 延迟 | 用途 |
|------|------|------|------|------|
| L0 | GPU HBM | 40~192GB/GPU | ~ns | 当前 active requests |
| L1 | 主机 DRAM | 256~2048GB/node | ~μs | 最近完成的 KV Cache（复用） |
| L2 | 本地 SSD | 数 TB/node | ~ms | 较长历史的 KV Cache |
| L3 | 远端存储 | 无限 | ~10ms | 跨节点 KV Cache 共享 |

**分层策略：**

- Active requests → 必须 HBM
- Session 内多轮对话 → DRAM（避免重复 prefill）
- 共享 prefix（系统 prompt）→ SSD 缓存
- 跨用户共享（few-shot examples）→ 远端存储

### 7.4 vLLM/SGLang/TensorRT-LLM 部署运维对比

| 维度 | vLLM | SGLang | TensorRT-LLM |
|------|------|--------|-------------|
| 语言 | Python (PyTorch) | Python (PyTorch) | C++/Python (TensorRT) |
| 调度策略 | Continuous batching | RadixAttention (prefix caching) | In-flight batching |
| 多模型 | 多 LoRA 支持好 | 多模型共享 KV Cache | 单模型优化为主 |
| 吞吐量 | 高 | 高（prefix caching 优势） | 最高（深度优化） |
| 延迟 | 中 | 中 | 低（C++ 运行时） |
| 部署复杂度 | 低（pip install） | 低（pip install） | 高（需 TensorRT 编译） |
| 模型格式 | HF transformers 直接加载 | HF transformers 直接加载 | 需 TRT-LLM 引擎构建 |
| 运维难度 | 低 | 低 | 中高 |
| 适用场景 | 通用推理 | 多轮对话/prefix 复用 | 极致性能要求 |

**vLLM 部署示例（Production 配置，vLLM 0.7.x，镜像 tag 以 vllm/vllm-openai 官方 Release 为准）：**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-llama3-70b
  namespace: inference-prod
spec:
  replicas: 4
  selector:
    matchLabels:
      app: vllm-llama3-70b
  template:
    metadata:
      labels:
        app: vllm-llama3-70b
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.7.x  # vLLM 0.7.x 基线，tag 以官方 Release 为准
          args:
            - "--model"
            - "meta-llama/Llama-3-70B-Instruct"
            - "--tensor-parallel-size"
            - "4"
            - "--max-model-len"
            - "8192"
            - "--gpu-memory-utilization"
            - "0.90"
            - "--max-num-seqs"
            - "256"
            - "--enforce-eager"  # 调试用，生产去掉以启用 CUDA graph
          ports:
            - containerPort: 8000
              name: http
          resources:
            limits:
              nvidia.com/gpu: 4
              memory: "128Gi"
              cpu: "32"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 120
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 300
            periodSeconds: 30
          env:
            - name: HUGGING_FACE_HUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hf-token
                  key: token
            - name: VLLM_NO_USAGE_STATS
              value: "1"
      tolerations:
        - key: "nvidia.com/gpu"
          operator: "Exists"
          effect: "NoSchedule"
```

### 7.5 推理网关与智能路由

```yaml
# KServe — serving.kserve.io/v1beta1（以 KServe 官方文档版本为准）
# 推理网关（以 KServe + Istio 为例）
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: llama3-70b
  namespace: inference-prod
  annotations:
    serving.kserve.io/deploymentMode: RawDeployment
spec:
  predictor:
    minReplicas: 2
    maxReplicas: 16
    scaleMetric: concurrency
    scaleTarget: 32
    model:
      modelFormat:
        name: huggingface
      args:
        - --model_name=llama3-70b
        - --model_id=meta-llama/Llama-3-70B-Instruct
      resources:
        limits:
          nvidia.com/gpu: 4
---
# Istio — networking.istio.io/v1beta1（以 Istio 官方版本为准）
# Istio VirtualService 智能路由（A/B 测试）
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: llm-router
  namespace: inference-prod
spec:
  hosts:
    - llm.example.com
  http:
    - match:
        - headers:
            x-model-version:
              exact: "v2"
      route:
        - destination:
            host: llama3-70b-v2
            port:
              number: 80
          weight: 100
    - route:
        - destination:
            host: llama3-70b-v1
            port:
              number: 80
          weight: 90
        - destination:
            host: llama3-70b-v2
            port:
              number: 80
          weight: 10
```

### 7.6 缩容到 0 与冷启动优化

| 策略 | 说明 | 冷启动时间 |
|------|------|-----------|
| 无缓存（冷启动） | 从 0 加载模型到 GPU | 3-10 min（70B 模型） |
| 显存保留 | 模型保持在 GPU 显存 | 0（不缩容到 0） |
| DRAM 缓存 | 模型卸载到主机内存 | 30-60s（CPU→GPU 传输） |
| SSD 缓存 | 模型 mmap 到本地 SSD | 60-120s |
| 远端模型缓存 | 对象存储拉取 | 2-5 min |

**KEDA + vLLM 缩容到 0 配置：**

```yaml
# KEDA — keda.sh/v1alpha1（以 KEDA 官方文档版本为准）
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: vllm-scaler
  namespace: inference-prod
spec:
  scaleTargetRef:
    name: vllm-llama3-70b
  minReplicaCount: 0  # 允许缩到 0
  maxReplicaCount: 16
  cooldownPeriod: 300  # 5 分钟无请求后缩容
  pollingInterval: 10
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus.monitoring:9090
        metricName: vllm_requests_in_flight
        query: |
          sum(vllm:num_requests_running{namespace="inference-prod",
                deployment="vllm-llama3-70b"})
        threshold: "32"
        activationThreshold: "1"  # 有 1 个请求就激活
```

---

## 8. 算力运营前沿

### 8.1 跨集群调度与算力交易

| 场景 | 方案 | 关键技术 |
|------|------|---------|
| 企业内部多集群 | Karmada / Cluster API | 统一控制面 + 跨集群 workload 分发 |
| 跨地域灾备 | 异步 checkpoint 同步 + 异地恢复 | 对象存储跨区域复制 |
| 算力交易 | 算力市场平台 | 资源抽象（算力单元）+ 计量计费 |
| 联邦调度 | 成本/延迟/可用性多目标优化 | 策略引擎 + 数据本地性 |

### 8.2 GPU 池化

**Remote GPU（远端 GPU 访问）：**

- 原理：将 GPU 从计算节点解耦，通过网络提供 GPU 算力
- 技术：NVIDIA GPU-over-Network（实验性）、virt-GPU、PCIe over fabric
- 延迟：网络增加 1-5ms（对推理可接受，对训练不可接受）
- 适用场景：开发调试、轻量推理、GPU 共享

**显存池化：**

- 原理：多节点共享显存池，突破单卡显存限制
- 技术：NVIDIA GPUDirect Storage + 统一虚拟内存（UVM）
- 应用：超大模型推理（超出单卡显存）
- 挑战：跨节点显存访问延迟高（需 RDMA）

### 8.3 Serverless GPU

| 平台类型 | 示例 | 计费模式 | 适用场景 |
|---------|------|---------|---------|
| 公有云 Serverless GPU | AWS Lambda + GPU / GCP Cloud Run GPU | 按请求/秒 | 轻量推理 |
| 自建 Serverless | K8s + Knative + GPU autoscaler | 内部核算 | 企业内部共享 |
| GPU 算力市场 | CoreWeave / Together AI | 按 GPU-hour | 临时扩容 |

**自建 Serverless GPU 架构要点：**

```yaml
# Knative — serving.knative.dev/v1（以 Knative 官方版本为准）
# Knative Service + GPU（概念性配置）
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: serverless-inference
  namespace: inference-serverless
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "0"   # 缩到 0
        autoscaling.knative.dev/maxScale: "8"
        autoscaling.knative.dev/target: "10"     # 每实例目标并发
    spec:
      containerConcurrency: 32
      containers:
        - image: registry.example.com/inference/vllm:0.7.x  # vLLM 0.7.x 基线
          resources:
            limits:
              nvidia.com/gpu: 1
          ports:
            - containerPort: 8000
```

---

## 9. 学习资源与信息渠道清单

### 官方文档

| 资源 | 地址 | 说明 |
|------|------|------|
| NVIDIA Data Center Documentation | https://docs.nvidia.com/datacenter/ | GPU/网络/软件全栈文档入口 |
| NVIDIA DCGM User Guide | https://docs.nvidia.com/datacenter/dcgm/ | GPU 监控诊断核心工具 |
| NVIDIA NCCL Documentation | https://docs.nvidia.com/deeplearning/nccl/ | 分布式训练通信库 |
| Kubernetes DRA Documentation | https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/ | DRA 官方设计文档 |
| CANN 文档 | https://www.hiascend.com/document/ | 昇腾全栈文档 |

### 论文

| 论文 | 关注点 | 一句话说明 |
|------|--------|-----------|
| MegaScale (NSDI 2024) | 万卡训练基础设施 | 字节跳动万卡集群全栈设计，覆盖网络/存储/调度/容错 |
| HPN (SIGCOMM 2023) | GPU 集群网络架构 | 字节高性能网络设计，Rail-optimized 拓扑 + 拥塞控制 |
| AlpaServe (OSDI 2023) | 大模型推理调度 | 模型并行 + 请求路由联合优化 |
| Firecracker (NSDI 2020) | 轻量虚拟化 | 微秒级 VM 启动，Serverless GPU 参考 |
| DistIR (MLSys 2024) | 分布式推理优化 | 推理系统仿真与优化框架 |
| Optimus (EuroSys 2020) | 训练调度 | 基于收敛速度预测的动态资源调度 |
| Varuna (OSDI 2022) | 弹性训练 | 训练任务弹性伸缩与容错 |

### 技术博客

| 博客 | 地址 | 说明 |
|------|------|------|
| NVIDIA Technical Blog | https://developer.nvidia.com/blog/ | GPU 技术深度文章 |
| Meta Engineering Blog | https://engineering.fb.com/ | 万卡集群实战经验 |
| 字节跳动技术博客 | https://www.volcengine.com/theme/ | MegaScale 配套文章 |
| Anyscale Blog | https://www.anyscale.com/blog | Ray 生态 + 分布式训练 |
| vLLM Blog | https://blog.vllm.ai/ | 推理引擎设计与更新 |

### 社区与会议

| 社区/会议 | 说明 |
|---------|------|
| KubeCon + CloudNativeCon | K8s 生态最新动态，DRA/GPU 调度 topic |
| NVIDIA GTC | GPU 计算年度大会，产品路线图 |
| MLPerf | 训练/推理性能基准，各厂商对标 |
| Open Compute Project (OCP) | 开放硬件标准，液冷/机柜设计 |
| Ultra Ethernet Consortium | UEC 进展跟踪 |
| CNCF TAG Runtime | K8s 运行时相关 SIG，DRA 讨论 |
