---
title: "Longhorn 把根分区吃到 80%：用磁盘逐出把 PVC 迁到独立数据盘"
date: 2026-11-20T10:00:00+08:00
draft: false
description: "部署时把 / 当数据盘的后果，以及 Longhorn 的磁盘级逐出（Scheduling Disable + Eviction Requested）如何在不中断业务的前提下把上百个副本迁到新盘：根分区 80% 降到 45%，附迁移全程的资源代价实测。"
tags: ["Longhorn", "存储", "Kubernetes", "排障"]
---

## 背景：/ 被当成了数据盘

早期部署把根分区当数据盘用，Longhorn 的默认磁盘 `/var/lib/longhorn/` 落在 `/` 上。
数据涨起来后 `/` 使用率到 80%——根分区同时承载操作系统和存储数据，
再涨下去先死的不是业务，是 kubelet 的镜像 GC、系统日志和包更新。

补救思路：加一块独立数据盘挂到 `/data`，在 Longhorn 里注册为第二块磁盘，
然后**把 `/` 上的 PVC 副本全部迁走**，让根分区回到只跑操作系统的角色。

迁移前节点上的两块磁盘（Longhorn UI → Node）：

| 磁盘 | Path | 调度状态 | 副本数 | 已用 / 容量 |
|---|---|---|---|---|
| default-disk-8031… | `/var/lib/longhorn/` | Disabled（迁移时设置） | 37 | 980 / 6125 GiB |
| disk-1 | `/data/loom/longhorn` | Schedulable | 95 | 2622 / 44701 GiB |

![Longhorn Node 页：旧盘在根分区 /var/lib/longhorn，新盘在 /data/loom/longhorn，红字标注根目录必须把数据迁出](/images/longhorn/1-node-disks.jpeg)

## 原理：磁盘级逐出，不是手动搬数据

Longhorn 没有「移动某个 PVC」的命令。迁移靠**磁盘级逐出（eviction）**：
把旧盘标记为不再接收新副本（`Scheduling: Disable`），同时请求逐出存量副本
（`Eviction Requested: True`），控制器会自动把副本逐个重建到其他健康磁盘上。

![Longhorn 磁盘逐出原理：旧盘禁用调度并请求逐出后，副本逐个重建到新盘，一个重建完成才迁下一个，全程保持卷高可用](/images/longhorn-disk-eviction.svg)

关键性质是**逐副本重建**：一个新副本在新盘上重建完成并转为 Healthy 之后，
才会处理下一个。任何时刻卷的可用副本数不下降，业务侧无感知。

## 操作：三步，保存即开始

1. Longhorn UI → **Node** 页 → 选择目标节点 → 下拉菜单 **Edit Node and Disks**
2. 选中旧磁盘，**Scheduling → Disable**：确保该盘不再接收任何新副本
3. 同一块盘，**Eviction Requested → True**：开启后 Longhorn 自动迁移存量副本

![Edit Node and Disks 弹窗：旧盘 Path 为 /var/lib/longhorn，Scheduling 选 Disable、Eviction Requested 选 True，红框标注](/images/longhorn/2-eviction-settings.jpeg)

保存后迁移自动开始，**不需要重启任何 Pod 或卷**。

## 观测迁移过程

### 副本状态：Rebuilding 在滚动

Node 页展开新盘的 Replicas 列表，能看到存量副本是 Healthy / Stopped，
而新盘上持续出现 **Rebuilding** 状态的副本（带进度条）。
本节点 132 个副本分 10 页，逐个滚动重建：

![新盘 replica 列表：多数 Healthy/Stopped，一个副本处于 Rebuilding 状态，红箭头标注](/images/longhorn/3-replicas-rebuilding.jpeg)

### 单卷视角：新旧盘副本并存

Volume 详情页的 Replicas 区会同时显示两块盘上的副本：
旧盘上是蓝色 Healthy 块，新盘上是黄色 Rebuilding 块（带进度条）。
左侧 Volume Details 的 State 保持 **Attached**、Health 保持 **Healthy**——
这就是「业务无感」的直接证据：

![Volume 详情：旧盘 /var/lib/longhorn 上的副本 Healthy，新盘 /data/loom/longhorn 上的副本 Rebuilding 带进度条，红箭头标注](/images/longhorn/4-volume-replica-pair.jpeg)

### 资源代价：迁移不是免费的

迁移本质是读旧盘 + 写新盘 + 节点内传输，代价真实存在。
Grafana 的 longhorn-system 命名空间大盘记录了整个过程：

| 指标 | 迁移前基线 | 迁移中峰值 |
|---|---|---|
| instance-manager CPU | ~1 核 | **~10 核** |
| longhorn 组件内存 | ~4 GiB | **~6 GiB**（阶跃后持平） |
| 网络带宽 | < 1 Mib/s | **8~24 Mib/s 持续** |

![Grafana 大盘：迁移期间 instance-manager CPU 峰值约 10 核、内存从 4 GiB 阶跃到 6 GiB、网络带宽持续 8 到 24 Mib/s，红箭头标注两处阶跃](/images/longhorn/5-grafana-migration-cost.jpeg)

规划迁移窗口就按这个量级预留：CPU 峰值约 10 核、内存 +2 GiB、
网络持续十几 Mib/s。**业务高峰期跑迁移会和负载抢资源**，选低峰期。

## 结果：根分区 80% → 45%

`df -h` 的时间序列（全程约 50 分钟）：

| 时点 | `/` 使用率 | `/data` 使用率 |
|---|---|---|
| 迁移前 | 80%（662G / 876G） | 5%（2.0T / 44T） |
| +10 min | 77%（639G） | - |
| +20 min | 72%（594G） | - |
| +25 min | 69%（569G） | - |
| +35 min | 60%（492G） | - |
| +40 min | 55%（456G） | 5%（2.2T） |
| +50 min | 49%（399G） | - |
| 迁移完成 | **45%（368G / 876G）** | 6%（2.3T / 44T） |

![终端 df -h 序列：根分区使用率从 80% 逐次下降到 45%，/data 从 5% 升到 6%](/images/longhorn/6-df-progress.jpeg)

根分区回到 45%，剩下的空间足够操作系统、镜像和日志周转；
存储增长的压力全部转移到 44T 的独立数据盘上。

## 注意事项

- **旧盘不要立刻删**：Eviction 完成前旧盘上仍有副本在服役。
  全部逐出后旧盘副本数归零，再考虑从节点磁盘配置里移除
- **Storage Reserved 要留**：旧盘保留了 262 GiB 不参与调度，
  防止根分区被系统组件写满后 Longhorn 又把副本调度回去
- **迁移窗口按资源峰值规划**：CPU ~10 核 / 内存 +2 GiB / 网络十几 Mib/s，
  避开业务高峰
- **逐副本重建决定总时长**：132 个副本不是并发全迁，
  总时间取决于数据量与两块盘的 IO 能力，本例 50 分钟迁完约 3.5 TiB
- **版本**：操作与截图基于 Longhorn **v1.9.2**；UI 路径在其他版本可能略有差异
