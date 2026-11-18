---
title: "GPU 集群运维（04）：存储"
date: 2026-11-10T09:00:00+08:00
draft: false
description: "训练场景 IO 特征、存储方案对比、Lustre 客户端运维、JuiceFS 与 Fluid 完整实操、镜像与数据集分发、Checkpoint 优化、fio 测试、存储监控与容量治理。"
tags: ["存储", "Lustre", "JuiceFS", "GPU"]
series: "GPU 集群运维"
---

本分册聚焦 GPU 集群训练场景下的存储选型、部署运维、IO 优化与数据治理。

## 本册环境基线

| 项目 | 基线版本 | 来源 |
|------|---------|------|
| OS | Ubuntu 22.04.5 LTS / Rocky Linux 9.5 | 00-版本基线 §1 |
| 内核 | 5.15.0-xxx-generic (Ubuntu GA) / 5.14.0-503 (Rocky) | 00-版本基线 §1 |
| Kubernetes | **1.31.x**（支持范围 1.29~1.32） | 00-版本基线 §3 |
| Lustre 服务端/客户端 | **2.15.x LTS**（DDN 客户端按 EX 版本） | 00-版本基线 §5 |
| JuiceFS 社区版 | **1.2.x** | 00-版本基线 §5 |
| JuiceFS CSI Driver | **0.24.x** | 00-版本基线 §5 |
| Fluid | **v1.0.x** | 00-版本基线 §5 |
| Dragonfly | **v2.1x**（dfget/dfdaemon） | 00-版本基线 §5 |
| fio | 源码编译 **3.36+**（发行版自带 3.26~3.28 偏旧） | 00-版本基线 §5 |
| containerd | 1.7.x LTS | 00-版本基线 §3 |
| etcd | 3.5.15+ | 00-版本基线 §3 |

> 基线快照时间：2026-01。所有命令/YAML/镜像 tag 均以此为准。
> Lustre 客户端内核模块需与本机内核（5.15.0-xxx）匹配编译。

---

## 目录

1. [训练场景 IO 特征](#1-训练场景-io-特征)
2. [存储方案对比表](#2-存储方案对比表)
3. [Lustre 客户端运维](#3-lustre-客户端运维)
4. [JuiceFS 完整部署](#4-juicefs-完整部署)
5. [Fluid 完整实操](#5-fluid-完整实操)
6. [镜像与数据集分发](#6-镜像与数据集分发)
7. [Checkpoint 优化](#7-checkpoint-优化)
8. [fio 测试](#8-fio-测试)
9. [存储监控](#9-存储监控)
10. [容量治理](#10-容量治理)

---

## 1. 训练场景 IO 特征

### 1.1 三类 IO 模式

| IO 类型 | 模式特征 | 典型大小 | 对存储的核心需求 |
|---------|---------|---------|-----------------|
| 数据集读取 | 海量小文件随机读（ImageNet 1.28M 张 JPEG）或大文件顺序读（parquet/tfrecord） | 小文件 50KB~2MB/个；大文件 100MB~10GB/个 | 高 IOPS（小文件）或高吞吐（大文件）；读多写少 |
| Checkpoint 写入 | 周期性大块突发写（每 N steps 保存一次模型权重） | 单次 5GB~100GB（取决于模型大小） | 高写入带宽；写入延迟敏感（阻塞训练） |
| 日志/指标 | 持续小 IO 追加写（tensorboard/wandb） | 每次 1KB~100KB | 低延迟；不影响主训练 IO |

### 1.2 量化分析

```text
典型 LLM 训练场景 IO 估算（以 LLaMA-70B 为例）：

数据集读取：
  - 预训练数据：2TB tokenized data（约 500 个 4GB parquet 文件）
  - DataLoader 每 batch 读 ~50MB，每秒 2~4 个 batch
  - 持续读取带宽需求：100~200 MB/s/节点
  - 16 节点集群总读取：1.6~3.2 GB/s

Checkpoint 写入：
  - 模型参数量 70B × 2 bytes (bf16) = 140GB 原始权重
  - 含优化器状态 (Adam)：140GB × 3 = 420GB
  - 保存频率：每 500 steps ≈ 每 15 分钟
  - 写入时间预算：< 60s（否则 GPU 空等浪费）
  - 所需写入带宽：420GB / 60s = 7 GB/s（单节点）
  - 实际通过分片降低：每节点 26GB → 需 ~500 MB/s/节点

结论：Checkpoint 写入是对存储最严苛的考验
```

### 1.3 三类需求总结

| 需求类别 | 关键指标 | 目标值 |
|---------|---------|--------|
| 数据加载 | 读 IOPS / 读吞吐 | 小文件 > 50K IOPS；大文件 > 2 GB/s/节点 |
| Checkpoint | 写吞吐 + 写延迟 | > 1 GB/s/节点；P99 延迟 < 10ms |
| 元数据操作 | 元数据 IOPS（open/stat/readdir） | > 100K ops/s（海量小文件场景） |

---

## 2. 存储方案对比表

| 方案 | 架构 | 读性能 | 写性能 | 成本 | POSIX 兼容 | K8s 集成 | 适用规模 |
|------|------|--------|--------|------|-----------|---------|---------|
| Lustre | 并行文件（MDS+OST） | 极高（聚合 100+GB/s） | 极高 | 高（专用硬件） | 完全 | CSI Driver | 大型（PB 级，100+ 节点） |
| GPFS (Spectrum Scale) | 并行文件（NSD+TOK） | 极高 | 极高 | 极高（IBM 许可） | 完全 | CSI Driver | 大型（企业级） |
| BeeGFS | 并行文件（MDS+OSS） | 高 | 高 | 中（社区版免费） | 完全 | CSI Driver | 中型（数十节点） |
| WekaFS | 全闪存并行文件 | 极高（NVMe 优化） | 极高 | 高 | 完全 | CSI Driver | 中大型（云原生） |
| CephFS | 分布式文件（MDS+OSD） | 中（元数据瓶颈） | 中 | 中（通用硬件） | 完全 | CSI Driver | 中型（百节点内） |
| JuiceFS + 对象存储 | 元数据引擎 + 对象存储 | 中~高（依赖缓存） | 中（对象存储限制） | 低（对象存储廉价） | 完全 | CSI Driver + Fluid | 中小型（弹性场景） |

### 选型决策树

```text
预算充足 + 100+ GPU 节点 + 专用机房？
  ├── 是 → Lustre / GPFS（并行文件系统，性能天花板最高）
  └── 否 →
        需要全闪存级 IO + 云环境？
          ├── 是 → WekaFS
          └── 否 →
                需要弹性伸缩 + 成本敏感？
                  ├── 是 → JuiceFS + 对象存储（S3/OSS/MinIO）
                  └── 否 →
                        已有 Ceph 集群？
                          ├── 是 → CephFS
                          └── 否 → BeeGFS（轻量级并行文件）
```

---

## 3. Lustre 客户端运维

### 3.1 挂载命令与参数

```bash
# 安装 Lustre 客户端（基线: Ubuntu 22.04 + Lustre 2.15.3 LTS；客户端内核模块需匹配运行内核 5.15.0-xxx 编译）
# Rocky Linux 9.x 替代: dnf install lustre-client lustre-client-dkms（需 kernel-devel 匹配）
# 添加 Lustre 仓库
wget -qO - https://downloads.whamcloud.com/public/lustre/lustre-2.15.3/ubuntu2204/client/DEBS/RPMS/repomd.xml.key | apt-key add -
echo "deb https://downloads.whamcloud.com/public/lustre/lustre-2.15.3/ubuntu2204/client/DEBS/server/" > /etc/apt/sources.list.d/lustre-client.list
apt-get update
apt-get install -y lustre-client-utils lustre-client-modules-$(uname -r)

# 挂载命令（Lustre 2.15.x 客户端；flock 选项需 Lustre 2.12+）
mount -t lustre \
  -o noatime,nodiratime,flock,max_rpcs_in_flight=32,rw \
  10.0.1.10@tcp:/gpfs01 \
  /mnt/lustre

# 参数说明：
# noatime,nodiratime  - 禁用 atime 更新（减少元数据操作）
# flock               - 启用文件锁（PyTorch DDP 需要）
# max_rpcs_in_flight  - 最大并发 RPC（默认 32，大文件可调到 64）

# 持久挂载（/etc/fstab）
echo "10.0.1.10@tcp:/gpfs01 /mnt/lustre lustre noatime,nodiratime,flock,max_rpcs_in_flight=32,_netdev 0 0" >> /etc/fstab
```

### 3.2 lfs 命令大全

```bash
# === 查看文件系统信息 ===
lfs df -h /mnt/lustre
# 输出：
# UUID       bytes      Used   Available Use% Mounted on
# gpfs01-MDT0000_UUID  1.0T   200G   800G   20% /mnt/lustre[MDT:0]
# gpfs01-OST0000_UUID  100T   40T    60T    40% /mnt/lustre[OST:0]
# ...

lfs df -i /mnt/lustre
# 显示 inode 使用情况

# === 文件查找 ===
# 查找大于 1GB 的文件
lfs find /mnt/lustre/datasets --size +1G --type f

# 查找特定条带配置的文件
lfs find /mnt/lustre/checkpoints --stripe-count 8 --type f

# 查找所有 .pt 文件
lfs find /mnt/lustre/models --name "*.pt" --type f

# === 条带化设置 ===
# 设置目录的默认条带化（新文件继承）
lfs setstripe \
  --stripe-count 8 \
  --stripe-size 4M \
  --stripe-index 0 \
  /mnt/lustre/checkpoints

# 参数说明：
# --stripe-count 8  : 数据分布在 8 个 OST 上（-1 = 所有 OST）
# --stripe-size 4M  : 每条带 4MB（即每写 4MB 切换到下一个 OST）
# --stripe-index 0  : 从 OST 0 开始（-1 = 自动选择）

# 为数据集目录设置高条带化（大文件顺序读优化）
lfs setstripe -c 16 -S 1M /mnt/lustre/datasets/llama-pretrain

# 为 checkpoint 目录设置最大条带化（写吞吐优化）
lfs setstripe -c -1 -S 4M /mnt/lustre/checkpoints

# === 查看条带化信息 ===
# 查看单个文件的条带化
lfs getstripe /mnt/lustre/checkpoints/model-step-1000.pt
# 输出：
# /mnt/lustre/checkpoints/model-step-1000.pt
# lmm_stripe_count:  8
# lmm_stripe_size:   4194304
# lmm_pattern:       raid0
# lmm_layout_gen:    0
# lmm_stripe_offset: 0
#   obdidx   objid   objid   group
#        0   12345   0x3039       0
#        1   12346   0x303a       0
#        ...

# 查看目录的默认条带化
lfs getstripe -d /mnt/lustre/checkpoints

# === 创建预条带化目录 ===
lfs mkdir -c 16 -S 2M /mnt/lustre/experiment-v3
# 创建新目录时即设置条带化参数

# === 文件预热（读入缓存） ===
lfs llapi_layout_warmup /mnt/lustre/datasets/train.parquet

# === 文件系统健康检查 ===
lfs check all
lfs osts /mnt/lustre        # 列出所有 OST
lfs mdts /mnt/lustre        # 列出所有 MDT
```

### 3.3 条带化原理与调优

```text
Lustre 条带化原理：

数据写入时按 stripe_size 大小轮转写入不同 OST：

stripe_count=4, stripe_size=1M:
  File: [0-1M) → OST0, [1M-2M) → OST1, [2M-3M) → OST2, [3M-4M) → OST3
        [4M-5M) → OST0, [5M-6M) → OST1, ...

stripe_count 选择指南：
┌──────────────────┬──────────────┬──────────────────────────┐
│ 场景             │ stripe_count │ 理由                      │
├──────────────────┼──────────────┼──────────────────────────┤
│ 海量小文件(<1MB) │ 1            │ 避免元数据开销             │
│ 中等文件(1~100MB)│ 4~8          │ 平衡并发与元数据           │
│ 大文件(>1GB)     │ 8~16         │ 最大化聚合带宽             │
│ Checkpoint       │ -1 (全部)    │ 需要最高写吞吐             │
│ 数据集目录默认   │ 4            │ 覆盖多数文件大小            │
└──────────────────┴──────────────┴──────────────────────────┘

stripe_size 选择指南：
┌────────────┬──────────────────────────────────────────┐
│ stripe_size│ 适用场景                                  │
├────────────┼──────────────────────────────────────────┤
│ 64K~256K   │ 小文件为主，减少条带切换开销              │
│ 1M         │ 通用默认值                                │
│ 4M         │ 大文件顺序 IO（checkpoint、大 parquet）   │
│ 16M+       │ 超大文件，但可能导致 OST 不均衡           │
└────────────┴──────────────────────────────────────────┘
```

### 3.4 常见客户端故障

```bash
# 故障 1：挂载超时
mount -t lustre 10.0.1.10@tcp:/gpfs01 /mnt/lustre
# mount.lustre: mount 10.0.1.10@tcp:/gpfs01 at /mnt/lustre failed: Connection timed out

# 排查：
ping 10.0.1.10
lctl ping 10.0.1.10@tcp
cat /proc/fs/lustre/osc/*/import
# 查看 import 状态是否为 CONNECTED

# 故障 2：IO 错误（stale file handle）
ls /mnt/lustre/
# ls: cannot access /mnt/lustre/: Stale file handle

# 排查：
cat /proc/fs/lustre/osc/*/state
# 如果显示 EVICTED，说明客户端被 MDS 驱逐
# 解决：umount + remount
umount -f /mnt/lustre
mount -t lustre -o noatime,nodiratime,flock 10.0.1.10@tcp:/gpfs01 /mnt/lustre

# 故障 3：写入速度远低于预期
# 排查条带化配置
lfs getstripe /mnt/lustre/slow-file
# 如果 stripe_count=1，重新设置
lfs setstripe -c 8 -S 4M /mnt/lustre/target-dir

# 检查 OST 均衡
lfs df -h /mnt/lustre
# 如果某个 OST 使用率远高于其他，说明条带化不均
```

---

## 4. JuiceFS 完整部署

### 4.1 架构

```text
┌─────────────────────────────────────────────────────────┐
│                    JuiceFS 架构                          │
│                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │  元数据引擎  │    │  对象存储     │    │  缓存层   │  │
│  │ (Redis/     │    │ (S3/OSS/     │    │ (本地NVMe)│  │
│  │  MySQL/     │    │  MinIO)      │    │           │  │
│  │  TiKV)      │    │              │    │           │  │
│  └──────┬──────┘    └──────┬───────┘    └─────┬─────┘  │
│         │                  │                  │         │
│         └──────────────────┴──────────────────┘         │
│                            │                            │
│                     ┌──────┴───────┐                    │
│                     │ JuiceFS 客户端│                    │
│                     │  (FUSE/CSI)  │                    │
│                     └──────┬───────┘                    │
│                            │                            │
│                     ┌──────┴───────┐                    │
│                     │  挂载点       │                    │
│                     │ /mnt/juicefs │                    │
│                     └──────────────┘                    │
└─────────────────────────────────────────────────────────┘

数据流：
  读请求 → 查缓存（NVMe命中？）→ 命中直接返回
                               → 未命中 → 从对象存储拉取 → 写入缓存 → 返回
  写请求 → 写缓存（NVMe）→ 异步刷入对象存储 → 元数据写入元数据引擎
```

### 4.2 社区版安装

```bash
# 安装 JuiceFS 客户端（基线版本 v1.2.x，示例 v1.2.1）
curl -sSL https://d.juicefs.com/install | sh -

# 验证版本
juicefs --version
# juicefs version 1.2.1（或更高 patch；以官方 release 为准）

# 安装 FUSE（如果未安装）
# Ubuntu 22.04:
apt-get install -y fuse3
# Rocky Linux 9:
# dnf install -y fuse3
```

### 4.3 format 完整命令

```bash
# 格式化 JuiceFS 文件系统（JuiceFS v1.2.x 客户端；MinIO 对象存储后端）
juicefs format \
  --storage minio \
  --bucket http://10.0.0.58:9000/training-data \
  --access-key admin \
  --secret-key REPLACE_WITH_STRONG_SECRET \
  --block-size 4096 \
  --compress lz4 \
  --encrypt-rsa-key /etc/juicefs/rsa-key.pem \
  redis://:password@10.0.1.50:6379/1 \
  training-fs

# 参数说明：
# --storage minio       : 对象存储类型（minio/s3/oss/gcs）
# --bucket              : 对象存储 bucket URL
# --access-key          : 对象存储 AK
# --secret-key          : 对象存储 SK
# --block-size 4096     : 数据块大小 4096KB（4MB，训练场景推荐）
# --compress lz4        : 压缩算法（lz4/zstd，推理数据建议不压缩）
# --encrypt-rsa-key     : 数据加密密钥（可选）
# redis://...           : 元数据引擎地址（Redis）
# training-fs           : 文件系统名称
```

### 4.4 K8s CSI Driver 部署

```bash
# 添加 JuiceFS CSI helm 仓库
helm repo add juicefs https://juicedata.github.io/charts/
helm repo update

# 安装 CSI Driver（基线版本 0.24.x，示例 0.24.0；以官方 release 为准）
helm install juicefs-csi-driver juicefs/juicefs-csi-driver \
  --namespace juicefs-system --create-namespace \
  --version 0.24.0 \
  --set image.tag=v0.24.0 \
  --set mountPod.resources.limits.cpu=2 \
  --set mountPod.resources.limits.memory=4Gi \
  --set mountPod.resources.requests.cpu=500m \
  --set mountPod.resources.requests.memory=1Gi \
  --set kubeletDir=/var/lib/kubelet
```

**StorageClass + PV + PVC 完整 YAML：**

```yaml
# 适用: K8s 1.29~1.32 / JuiceFS CSI Driver 0.24.x / apiVersion: storage.k8s.io/v1
# JuiceFS mount image: juicedata/mount:ce-v1.2.1（社区版，与 JuiceFS 1.2.x 匹配）
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: juicefs-sc
provisioner: csi.juicefs.com
parameters:
  csi.storage.k8s.io/provisioner-secret-name: juicefs-secret
  csi.storage.k8s.io/provisioner-secret-namespace: juicefs-system
  csi.storage.k8s.io/node-publish-secret-name: juicefs-secret
  csi.storage.k8s.io/node-publish-secret-namespace: juicefs-system
reclaimPolicy: Retain
mountOptions:
  - cache-size=204800       # 缓存大小 200GB（单位 MB）
  - buffer-size=1024        # 读写缓冲区 1GB
  - prefetch=1              # 启用预读
  - cache-dir=/var/jfsCache # 缓存目录
  - metrics=0.0.0.0:9567    # metrics 地址
---
apiVersion: v1
kind: Secret
metadata:
  name: juicefs-secret
  namespace: juicefs-system
type: Opaque
stringData:
  name: training-fs
  metaurl: redis://:password@10.0.1.50:6379/1
  storage: minio
  bucket: http://10.0.0.58:9000/training-data
  access-key: admin
  secret-key: REPLACE_WITH_STRONG_SECRET
  format-options: block-size=4096,compress=lz4
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: juicefs-training-pv
spec:
  capacity:
    storage: 100Ti          # JuiceFS 弹性容量，这里设置逻辑上限
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: juicefs-sc
  csi:
    driver: csi.juicefs.com
    volumeHandle: juicefs-training-pv
    nodePublishSecretRef:
      name: juicefs-secret
      namespace: juicefs-system
    volumeAttributes:
      juicefs/mount-cpu-limit: "4"
      juicefs/mount-memory-limit: 8Gi
      juicefs/mount-image: juicedata/mount:ce-v1.2.1   # JuiceFS 社区版 1.2.x 镜像（与 CSI Driver 0.24.x 匹配）
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: juicefs-training-pvc
  namespace: ml-training
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: juicefs-sc
  resources:
    requests:
      storage: 10Ti
```

### 4.5 缓存配置与性能调优

```yaml
# 适用: JuiceFS 1.2.x / JuiceFS CSI Driver 0.24.x
# JuiceFS Mount Pod 缓存配置（通过 CSI mountOptions 或 mountPod.env 设置）
# 关键参数表：

# ┌──────────────────┬──────────┬──────────────────────────────────────────┐
# │ 参数             │ 推荐值   │ 说明                                      │
# ├──────────────────┼──────────┼──────────────────────────────────────────┤
# │ cache-dir        │ NVMe 路径│ 缓存目录，务必用 NVMe（/data/jfsCache）   │
# │ cache-size       │ 204800   │ 缓存大小（MB），即 200GB；NVMe 盘的 60%   │
# │ free-space-ratio │ 0.1      │ 缓存盘最少保留 10% 空间                   │
# │ buffer-size      │ 1024     │ 内存缓冲区（MB），大文件读写调大           │
# │ prefetch         │ 1        │ 启用异步预读（数据集加载场景必须开启）     │
# │ writeback        │ 0        │ 写回缓存（0=直写对象存储，1=先写缓存）     │
# │ upload-limit     │ 0        │ 上传带宽限制（0=不限，单位 Mbps）          │
# │ download-limit   │ 0        │ 下载带宽限制（0=不限）                     │
# │ io-retries       │ 10       │ IO 重试次数                               │
# │ max-uploads      │ 20       │ 并发上传数（对象存储）                     │
# │ get-timeout      │ 60       │ GET 请求超时（秒）                         │
# │ put-timeout      │ 60       │ PUT 请求超时（秒）                         │
# └──────────────────┴──────────┴──────────────────────────────────────────┘

# 示例：针对训练场景的 mountOptions
mountOptions:
  - cache-dir=/data/jfsCache         # NVMe 盘
  - cache-size=204800                # 200GB 缓存
  - free-space-ratio=0.1
  - buffer-size=2048                 # 2GB 缓冲区
  - prefetch=1
  - prefetch=0                       # 禁用预读（Checkpoint 写场景）
  - writeback=1                      # 启用写回（Checkpoint 场景）
  - max-uploads=50                   # 提高并发上传
  - upload-limit=0
  - io-retries=10
  - get-timeout=120
  - put-timeout=120
```

---

## 5. Fluid 完整实操

### 5.1 架构

```text
Fluid 架构：

┌─────────────────────────────────────────────────────────────┐
│                     K8s Control Plane                        │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Fluid       │  │  Dataset CRD │  │  Runtime CRD      │  │
│  │  Controller  │  │              │  │ (JuiceFSRuntime/  │  │
│  │              │  │              │  │  AlluxioRuntime)   │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────┘  │
└─────────┼────────────────────────────────────────────────────┘
          │ reconcile
          ▼
┌─────────────────────────────────────────────────────────────┐
│                      Worker Node                             │
│  ┌────────────────────┐  ┌──────────────────────────────┐   │
│  │  Runtime Worker    │  │  CSI Plugin (FUSE mount)     │   │
│  │  (JuiceFS client/  │  │  /var/lib/kubelet/plugins/   │   │
│  │   Alluxio Worker)  │  │  runtime.fluid.io/           │   │
│  └────────────────────┘  └──────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  训练 Pod                                            │   │
│  │  volumeMount: /data → fluid PVC (FUSE → Runtime)     │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 JuiceFSRuntime Dataset + Runtime 完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Fluid v1.0.x / apiVersion: data.fluid.io/v1alpha1（Fluid v0.x 以来稳定）
# Dataset - 声明数据源
apiVersion: data.fluid.io/v1alpha1
kind: Dataset
metadata:
  name: llama-pretrain-data
  namespace: ml-training
spec:
  mounts:
  - mountPoint: redis://:password@10.0.1.50:6379/1
    name: training-data
    options:
      storage: minio
      bucket: http://10.0.0.58:9000/training-data
      access-key: admin
      secret-key: REPLACE_WITH_STRONG_SECRET
  accessModes:
    - ReadOnlyMany
  nodeAffinity:
    required:
      nodeSelectorTerms:
      - matchExpressions:
        - key: node-role.kubernetes.io/gpu
          operator: Exists
---
# JuiceFSRuntime - 声明运行时配置（Fluid v1.0.x；JuiceFS worker 镜像 juicedata/juicefs-csi-driver:0.24.x 或 juicedata/mount:ce-v1.2.1）
apiVersion: data.fluid.io/v1alpha1
kind: JuiceFSRuntime
metadata:
  name: llama-pretrain-runtime
  namespace: ml-training
spec:
  replicas: 2                       # Worker 副本数
  tieredstore:
    levels:
    - mediumtype: SSD               # 缓存介质类型
      path: /data/jfsCache           # NVMe 盘路径
      quota: 200Gi                  # 缓存配额
      high: "0.95"                  # 缓存高水位（开始淘汰）
      low: "0.7"                    # 缓存低水位（淘汰到此）
  worker:
    resources:
      limits:
        cpu: "4"
        memory: 8Gi
      requests:
        cpu: "2"
        memory: 4Gi
    networkMode: Host               # 使用主机网络（减少网络开销）
  fuse:
    resources:
      limits:
        cpu: "2"
        memory: 4Gi
      requests:
        cpu: "1"
        memory: 2Gi
    args:
      - cache-size=204800           # 200GB 缓存
      - buffer-size=1024            # 1GB 缓冲区
      - prefetch=1
      - metrics=0.0.0.0:9567
    cleanPolicy: OnDemand           # Pod 退出时清理缓存
---
# PVC - 训练 Pod 引用
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: llama-pretrain-data-pvc
  namespace: ml-training
spec:
  accessModes:
    - ReadOnlyMany
  resources:
    requests:
      storage: 10Ti
  selector:
    matchLabels:
      fluid.io/dataset: llama-pretrain-data
```

### 5.3 Warmup 预热任务完整 YAML

```yaml
# 适用: K8s 1.29~1.32 / Fluid v1.0.x / apiVersion: data.fluid.io/v1alpha1
apiVersion: data.fluid.io/v1alpha1
kind: DataLoad
metadata:
  name: llama-data-warmup
  namespace: ml-training
spec:
  dataset:
    name: llama-pretrain-data
    namespace: ml-training
  loadMetadata: true               # 同时加载元数据
  target:
    - path: /pretrain/tokenized/    # 要预热的目录路径
      replicas: 1                   # 数据副本数
    - path: /pretrain/validation/
      replicas: 1
  podMetadata:
    labels:
      job: data-warmup
  ttlSecondsAfterFinished: 3600    # 完成后 1 小时自动清理 Job
  schedulerName: default-scheduler
  resources:
    limits:
      cpu: "4"
      memory: 8Gi
    requests:
      cpu: "2"
      memory: 4Gi
```

### 5.4 数据亲和调度

```bash
# Fluid 会自动为 Dataset 创建 nodeAffinity 标签
# 查看 Fluid 添加的标签
kubectl get nodes --show-labels | grep fluid

# Fluid 生成的标签格式：
# fluid.io/s-ml-training-llama-pretrain-data=true  （Dataset 缓存就绪）
# fluid.io/f-ml-training-llama-pretrain-data=true  （FUSE 就绪）

# 训练 Pod 会自动被调度到有缓存的节点
# 如果需要显式指定，在 Pod 中添加 nodeAffinity：
```

```yaml
# 适用: K8s 1.29~1.32 / Fluid v1.0.x / apiVersion: v1
# 在训练 Pod spec 中添加数据亲和（Fluid 自动生成的标签格式，v0.x/v1.x 一致）
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: fluid.io/s-ml-training-llama-pretrain-data
            operator: In
            values:
            - "true"
```

### 5.5 缓存命中率查看

```bash
# 查看 Dataset 状态
kubectl get dataset llama-pretrain-data -n ml-training -o yaml

# 查看缓存使用率
kubectl get dataset llama-pretrain-data -n ml-training \
  -o jsonpath='{.status.cacheStates.cached}'

# 查看缓存命中率（通过 JuiceFS metrics）
curl -s http://<juicefs-worker-ip>:9567/metrics | grep juicefs_cache

# 关键指标：
# juicefs_cache_hits      - 缓存命中次数
# juicefs_cache_misses    - 缓存未命中次数
# juicefs_cache_bytes     - 缓存数据量
# 命中率 = cache_hits / (cache_hits + cache_misses)

# 通过 Fluid Dashboard 或 Prometheus 查询
# PromQL:
# rate(juicefs_cache_hits[5m]) / (rate(juicefs_cache_hits[5m]) + rate(juicefs_cache_misses[5m]))
```

---

## 6. 镜像与数据集分发

### 6.1 镜像拉取风暴问题量化分析

```text
场景：100 个 GPU 节点同时拉取 20GB 训练镜像

无优化时：
  每个节点从 Registry 拉取 20GB
  Registry 出口带宽：10 Gbps = 1.25 GB/s
  100 节点并发：每个节点分到 12.5 MB/s
  拉取时间：20 GB / 12.5 MB/s = 1600 秒 ≈ 27 分钟

  总网络流量：100 × 20 GB = 2 TB（全部从 Registry 出）

使用 P2P 分发后：
  只有 1 个节点从 Registry 拉取（20GB / 1.25 GB/s = 16 秒）
  其余 99 节点通过 P2P 从已拉取节点获取
  P2P 内网带宽：25 Gbps = 3.125 GB/s
  每个节点拉取时间：20 GB / 3.125 GB/s ≈ 6.4 秒

  Registry 出流量：20 GB（降低 99%）
  总拉取时间：从 27 分钟降到 < 1 分钟
```

### 6.2 Dragonfly 架构与部署要点

```text
Dragonfly 架构：

┌──────────┐    ┌──────────┐    ┌──────────┐
│ Registry │    │ Dragonfly│    │  P2P     │
│ (Harbor) │◄───│ Scheduler│◄───│ Network  │
└──────────┘    └──────────┘    └────┬─────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
         ┌────┴────┐          ┌──────┴────┐          ┌─────┴────┐
         │DFDaemon │          │DFDaemon   │          │DFDaemon  │
         │(gpu-01) │          │(gpu-02)   │          │(gpu-99)  │
         └─────────┘          └───────────┘          └──────────┘

组件说明：
- Scheduler: 调度下载任务，选择最优 peer
- DFDaemon: 节点代理，实现镜像层 P2P 下载
- containerd 配置 proxy 指向 DFDaemon
```

```bash
# Dragonfly 部署要点（基线版本 v2.1x，示例 dfget v2.1.5；helm chart 版本以官方 release 为准）
# 1. 安装 Scheduler + Manager
helm repo add dragonfly https://dragonflyoss.github.io/helm-charts/
helm repo update

helm install dragonfly dragonfly/dragonfly \
  --namespace dragonfly-system --create-namespace \
  --set scheduler.replicas=3 \
  --set manager.replicas=2 \
  --set dfdaemon.hostNetwork=true \
  --version 1.2.3   # helm chart 版本（示例，以官方 release 为准）

# 2. 配置 containerd 使用 Dragonfly 代理（containerd 1.7.x drop-in 配置目录）
# 在每个节点上修改 /etc/containerd/config.toml（注意 containerd 2.0 配置结构有差异）
cat >> /etc/containerd/config.d/dragonfly.toml << 'EOF'
version = 2
[plugins."io.containerd.grpc.v1.cri".registry]
  [plugins."io.containerd.grpc.v1.cri".registry.mirrors]
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
      endpoint = ["http://127.0.0.1:65001"]
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors."my-registry.internal:5000"]
      endpoint = ["http://127.0.0.1:65001"]
EOF

systemctl restart containerd
```

### 6.3 eStargz / Nydus 懒加载

**eStargz 原理：**
- 将镜像层转换为 seekable 的 stargz 格式
- 容器启动时只拉取必须的元数据（TOC），实际文件按需懒加载
- 启动时间从分钟级降到秒级

**Nydus 原理：**
- 镜像层使用自定义的 RAFS 文件系统格式
- FUSE 挂载镜像层，文件按需从 Registry 拉取
- 支持 chunk 级别去重，减少存储

**containerd + Nydus 配置：**

```toml
# 适用: containerd 1.7.x / nydus-snapshotter（随 Dragonfly image-service v2.2.x）
# /etc/containerd/config.d/nydus.toml
version = 2

[proxy_plugins]
  [proxy_plugins.nydus]
    type = "snapshot"
    address = "/run/containerd-nydus/containerd-nydus-grpc.sock"

[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "nydus"
  disable_snapshot_annotations = false

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  snapshotter = "nydus"
```

```bash
# 安装 Nydus Snapshotter（版本以官方 release 为准，匹配 Dragonfly image-service v2.2.x）
helm install nydus-snapshotter dragonfly/nydus-snapshotter \
  --namespace nydus-system --create-namespace \
  --set daemonset.nydusdArgs="--log-level info --api-sock /run/containerd-nydus/api.sock"

# 转换现有镜像为 Nydus 格式
# 安装 nydusify 工具（Dragonfly image-service v2.2.4，示例；以官方 release 为准）
wget https://github.com/dragonflyoss/image-service/releases/download/v2.2.4/nydusify-linux-amd64.tgz
tar xzf nydusify-linux-amd64.tgz

# 转换镜像
./nydusify convert \
  --source my-registry.internal:5000/training:v2.1 \
  --target my-registry.internal:5000/training:v2.1-nydus \
  --nydus-image ./nydus-image \
  --workdir /tmp/nydus-convert
```

### 6.4 P2P 分发数据集思路

```text
场景：将 2TB 训练数据集从中心存储分发到 100 个 GPU 节点

方案 1：Dragonfly 文件分发
  - 将数据集打包为 tar.gz 并通过 Dragonfly 的 dfget 分发
  - dfget 支持文件级别 P2P 下载
  - 适用于一次性数据集部署

方案 2：Fluid + JuiceFS 缓存预热
  - 首次访问触发缓存填充
  - 后续 Pod 命中缓存，实质是 P2P 效果
  - 适用于增量更新场景

方案 3：BitTorrent 式分发
  - 使用 aria2 + tracker 做数据集 P2P 分发
  - 适合超大数据集（10TB+）

实际部署建议：
  1. 训练数据集 → JuiceFS + Fluid 缓存（热数据自动缓存）
  2. 容器镜像 → Dragonfly P2P（镜像拉取加速）
  3. 模型权重 → 对象存储直连（Checkpoint 场景）
```

---

## 7. Checkpoint 优化

### 7.1 同步 vs 异步 Checkpoint

```text
同步 Checkpoint 时序：
┌─────────────────────────────────────────────────────────────┐
│  Step 500 完成  │←─── 保存 Checkpoint ───→│  Step 501 开始  │
│                 │  GPU 空等 ~60s           │                 │
│  计算 loss      │  写 420GB 到共享存储    │  前向传播       │
│  反向传播       │  (所有 GPU 阻塞)         │  反向传播       │
└─────────────────┴─────────────────────────┴─────────────────┘
  总耗时：计算时间 + 60s Checkpoint 保存时间

异步 Checkpoint 时序：
┌─────────────────────────────────────────────────────────────┐
│  Step 500 完成  │ Step 501 开始     │ Step 502 ...          │
│                 │                   │                       │
│  计算 loss      │  前向传播         │  继续训练             │
│  反向传播       │  反向传播         │                       │
│  快速 dump 到   │  ← 后台上传到远端 →│                       │
│  本地 NVMe      │                   │                       │
│  (~5s)          │                   │                       │
└─────────────────┴───────────────────┴───────────────────────┘
  总耗时：计算时间 + 5s 本地 dump（上传与训练并行）
```

### 7.2 torch.distributed.checkpoint 分片保存代码示例

```python
import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.state_dict import (
    get_state_dict,
    set_state_dict,
    StateDictOptions,
)
import os
import time

def save_checkpoint_distributed(model, optimizer, epoch, step, save_dir):
    """
    使用 torch.distributed.checkpoint 分片保存
    每个 rank 只保存自己的分片，无需 gather 到 rank 0
    """
    os.makedirs(save_dir, exist_ok=True)

    # 获取分布式 state_dict（FSDP/DDP 兼容）
    state_dict_options = StateDictOptions(
        full_state_dict=False,          # 分片保存（非全量）
        cpu_offload=True,               # offload 到 CPU 内存（减少 GPU 占用）
    )
    model_state_dict, optimizer_state_dict = get_state_dict(
        model, optimizer, options=state_dict_options
    )

    state_dict = {
        "model": model_state_dict,
        "optimizer": optimizer_state_dict,
        "epoch": epoch,
        "step": step,
        "timestamp": time.time(),
    }

    # 分布式保存（每个 rank 写自己的 shard）
    dcp.save(state_dict, checkpoint_id=save_dir)

    if dist.get_rank() == 0:
        print(f"Checkpoint saved to {save_dir} at step {step}")

def load_checkpoint_distributed(model, optimizer, save_dir):
    """加载分布式 Checkpoint"""
    state_dict_options = StateDictOptions(
        full_state_dict=False,
        cpu_offload=True,
    )
    model_state_dict, optimizer_state_dict = get_state_dict(
        model, optimizer, options=state_dict_options
    )

    state_dict = {
        "model": model_state_dict,
        "optimizer": optimizer_state_dict,
        "epoch": torch.tensor(0),
        "step": torch.tensor(0),
    }

    dcp.load(state_dict, checkpoint_id=save_dir)
    return state_dict["epoch"].item(), state_dict["step"].item()
```

### 7.3 异步保存方案（先写本地 NVMe 再上传）

```python
import shutil
import threading
import subprocess
from pathlib import Path

class AsyncCheckpointManager:
    def __init__(self, local_nvme_dir, remote_dir, max_pending=2):
        """
        local_nvme_dir: 本地 NVMe 路径（如 /data/nvme/checkpoints）
        remote_dir: 远端存储路径（如 /mnt/lustre/checkpoints 或 s3://...）
        max_pending: 最大并行上传数
        """
        self.local_dir = Path(local_nvme_dir)
        self.remote_dir = remote_dir
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.pending_uploads = []
        self.semaphore = threading.Semaphore(max_pending)

    def save_async(self, state_dict, step):
        """异步保存：先快速写入本地 NVMe，后台上传到远端"""
        local_path = self.local_dir / f"step-{step}"

        # 1. 快速写入本地 NVMe（~5s for 26GB per node）
        torch.save(state_dict, local_path / "checkpoint.pt")

        # 2. 启动后台上传线程
        upload_thread = threading.Thread(
            target=self._upload_to_remote,
            args=(local_path, step),
            daemon=True
        )
        upload_thread.start()
        self.pending_uploads.append(upload_thread)

        # 清理已完成的上传
        self.pending_uploads = [t for t in self.pending_uploads if t.is_alive()]

    def _upload_to_remote(self, local_path, step):
        """后台上传到远端存储"""
        with self.semaphore:
            remote_path = f"{self.remote_dir}/step-{step}"
            # 使用 rsync（Lustre/CephFS）或 aws s3 cp（对象存储）
            cmd = f"rsync -a {local_path}/ {remote_path}/"
            subprocess.run(cmd, shell=True, check=True)
            # 上传成功后删除本地副本
            shutil.rmtree(local_path)

    def wait_all(self):
        """等待所有上传完成"""
        for t in self.pending_uploads:
            t.join()
```

### 7.4 保存频率与 MTBF 权衡计算

```text
MTBF（Mean Time Between Failure）权衡计算：

假设：
  - 集群 128 GPU，单 GPU 年故障率 5%（MTBF ≈ 20 年/GPU）
  - 集群级故障率：128 × 5% = 6.4 次/年 ≈ 每 57 天一次
  - 实际含网络/软件故障：经验值每 2~8 小时一次故障事件

Checkpoint 保存频率决策：

┌────────────────┬────────────────────┬────────────────────────────────┐
│ 保存间隔       │ Checkpoint 大小    │ 权衡                           │
├────────────────┼────────────────────┼────────────────────────────────┤
│ 每 100 steps   │ 420GB × 1 = 420GB │ 浪费最少（~3min），但丢失 100 步│
│ (约 3 分钟)    │                    │                                │
├────────────────┼────────────────────┼────────────────────────────────┤
│ 每 500 steps   │ 420GB × 1 = 420GB │ 平衡选择，丢失 500 步           │
│ (约 15 分钟)   │                    │ （~5min 数据）                 │
├────────────────┼────────────────────┼────────────────────────────────┤
│ 每 2000 steps  │ 420GB × 1 = 420GB │ 节省存储 IO，但故障后重启      │
│ (约 1 小时)    │                    │ 成本高（丢失 1 小时训练）       │
└────────────────┴────────────────────┴────────────────────────────────┘

推荐公式：
  最佳保存间隔 = sqrt(2 × T_save × T_step) / failure_rate
  其中：
    T_save = 单次 Checkpoint 保存耗时（秒）
    T_step = 单步训练耗时（秒）
    failure_rate = 每秒故障概率

示例：
  T_save = 60s, T_step = 2s, failure_rate = 1/(4*3600) = 0.000069/s
  最佳间隔 = sqrt(2 × 60 × 2) / 0.000069 ≈ sqrt(240) / 0.000069 ≈ 225 steps
```

### 7.5 K8s 上 Checkpoint 存储挂载建议

```yaml
# 适用: K8s 1.29~1.32 / apiVersion: v1
# 推荐的 Checkpoint 存储挂载方案：
# - 本地 NVMe（hostPath）用于异步 Checkpoint 临时存放
# - 远端 PVC（Lustre/JuiceFS）用于持久化存储
# 镜像: 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3

apiVersion: v1
kind: Pod
metadata:
  name: training-with-checkpoint
spec:
  containers:
  - name: training
    image: my-registry/training:v2.1   # 生产推荐 nvcr.io/nvidia/pytorch:24.12-py3
    volumeMounts:
    # 本地 NVMe - 异步 Checkpoint 快速写入
    - name: local-nvme
      mountPath: /data/local-checkpoint
    # 远端共享存储 - Checkpoint 持久化
    - name: remote-checkpoint
      mountPath: /data/checkpoint
    # 共享内存（NCCL + DataLoader）
    - name: shm
      mountPath: /dev/shm
  volumes:
  # 本地 NVMe（每个节点需有 NVMe 盘）
  - name: local-nvme
    hostPath:
      path: /data/nvme/checkpoints
      type: DirectoryOrCreate
  # 远端持久化存储
  - name: remote-checkpoint
    persistentVolumeClaim:
      claimName: checkpoint-lustre-pvc
  # 共享内存
  - name: shm
    emptyDir:
      medium: Memory
      sizeLimit: 128Gi
```

---

## 8. fio 测试

### 8.1 4K/64K 随机读（模拟 DataLoader 小文件场景）

```ini
# /tmp/fio-random-read.fio
# 模拟 DataLoader 读取海量小文件（如 ImageNet JPEG）
# 适用: fio 3.36+（源码编译；Ubuntu 22.04 apt 自带 3.28 偏旧，建议源码编译获取完整特性）

[global]
ioengine=libaio
direct=1
runtime=120
time_based=1
group_reporting=1
numjobs=8
iodepth=32
rw=randread
bs=4k
size=4G
directory=/mnt/lustre/fio-test

[random-read-4k]
stonewall
bs=4k
rw=randread

[random-read-64k]
stonewall
bs=64k
rw=randread
```

```bash
# 执行测试
fio /tmp/fio-random-read.fio --output=/tmp/fio-random-read.json --output-format=json

# 解读结果：
# - IOPS: 4K 随机读 IOPS（越高越好）
#   Lustre 参考值: > 50,000 IOPS（8 OST 条带化）
#   JuiceFS 参考值: > 20,000 IOPS（缓存命中时）
# - lat (usec): P99 延迟（越低越好）
#   Lustre 参考值: P99 < 1000 us
#   JuiceFS 参考值: P99 < 2000 us（缓存命中）/ < 50000 us（缓存未命中）
# - BW: 聚合带宽
#   4K: IOPS × 4KB = 实际吞吐
#   64K: 期望 > 500 MB/s
```

### 8.2 4M 顺序写（模拟 Checkpoint 场景）

```ini
# /tmp/fio-sequential-write.fio
# 模拟 Checkpoint 大块顺序写入
# 适用: fio 3.36+（源码编译）

[global]
ioengine=libaio
direct=1
runtime=120
time_based=1
group_reporting=1
numjobs=4
iodepth=16
rw=write
bs=4M
size=16G
directory=/mnt/lustre/fio-test
fsync=1024

[sequential-write-4M]
stonewall
bs=4M
rw=write
```

```bash
# 执行测试
fio /tmp/fio-sequential-write.fio --output=/tmp/fio-seq-write.json --output-format=json

# 解读结果：
# - BW (MiB/s): 顺序写带宽（最核心指标）
#   Lustre 参考值: > 2000 MiB/s（16 OST 条带化）
#   JuiceFS 参考值: > 500 MiB/s（受对象存储上传限制）
#   本地 NVMe 参考值: > 3000 MiB/s
# - lat (msec): 写延迟
#   Lustre 参考值: P99 < 50 ms
#   JuiceFS 参考值: P99 < 200 ms（含对象存储上传）
# - IOPS: 4M 块大小下 IOPS 不高是正常的
#   2000 MiB/s / 4 MiB = 500 IOPS 即为正常
```

### 8.3 结果关键指标判断标准

| 指标 | 4K 随机读 | 64K 随机读 | 4M 顺序写 | 判断标准 |
|------|----------|-----------|----------|---------|
| IOPS | > 50K 优秀 / > 20K 合格 | > 10K 优秀 | N/A（不适用） | 低于合格值需排查条带化/缓存 |
| BW | > 200 MB/s | > 600 MB/s | > 2 GB/s 优秀 / > 500 MB/s 合格 | Checkpoint 场景 BW < 500 MB/s 会导致保存超时 |
| P99 Lat | < 1ms 优秀 / < 5ms 合格 | < 5ms | < 50ms 优秀 / < 200ms 合格 | P99 抖动大说明存储不稳定 |

---

## 9. 存储监控

### 9.1 客户端延迟/吞吐/缓存命中率采集

```bash
# === Lustre 客户端监控 ===
# 通过 llstat 采集客户端 IO 统计
cat /proc/fs/lustre/osc/*/stats
# 关键字段：read_bytes, write_bytes, read_rpc, write_rpc

# 使用 lustre_exporter 暴露 Prometheus metrics（版本以官方 release 为准，示例 v0.8.0）
# 部署 lustre_exporter
helm install lustre-exporter my-charts/lustre-exporter \
  --namespace monitoring \
  --set collector.interval=15s

# === JuiceFS 客户端监控 ===
# JuiceFS metrics 默认暴露在 :9567/metrics
curl -s http://localhost:9567/metrics | grep -E "juicefs_(fuse|cache|block)"

# 关键 Prometheus 指标：
# juicefs_fuse_ops_handled        - FUSE 操作处理次数
# juicefs_fuse_ops_latency        - FUSE 操作延迟
# juicefs_cache_hits              - 缓存命中次数
# juicefs_cache_misses            - 缓存未命中次数
# juicefs_block_read_bytes        - 块读取字节数
# juicefs_block_write_bytes       - 块写入字节数
# juicefs_object_request_latency  - 对象存储请求延迟

# PromQL 示例：
# 缓存命中率（5 分钟窗口）：
# rate(juicefs_cache_hits[5m]) / (rate(juicefs_cache_hits[5m]) + rate(juicefs_cache_misses[5m]))
#
# 读吞吐（5 分钟窗口）：
# rate(juicefs_block_read_bytes[5m]) / 1024 / 1024
```

### 9.2 Lustre 服务端监控要点

```bash
# MDS 负载监控
# 检查 MDS CPU 和内存
ssh mds-01 'cat /proc/fs/lustre/mdt/*/stats | head -20'
ssh mds-01 'top -bn1 | head -5'

# 检查 MDS RPC 队列
ssh mds-01 'lctl get_param mdt.*.mds.threads_max'
ssh mds-01 'lctl get_param mdt.*.mds.threads_started'

# OST 均衡检查
lfs df -h /mnt/lustre | sort -k5 -rn
# 如果最满的 OST 和最空的 OST 差距 > 20%，需要 rebalance

# OST rebalance（使用 lfs migrate）
lfs migrate -c 8 -S 4M /mnt/lustre/old-data/
# 注意：rebalance 是 IO 密集操作，在低峰期执行

# OST 健康检查
lctl get_param osc.*.import | grep -i state
# 期望所有 import 状态为 FULL 或 CONNECTED
```

### 9.3 告警阈值建议表

| 指标 | Warning 阈值 | Critical 阈值 | 说明 |
|------|-------------|--------------|------|
| Lustre 客户端读延迟 P99 | > 10ms | > 50ms | 正常 < 5ms，高延迟影响 DataLoader |
| Lustre 客户端写延迟 P99 | > 100ms | > 500ms | Checkpoint 保存超时风险 |
| Lustre MDS CPU | > 70% | > 90% | MDS 过载导致元数据操作变慢 |
| Lustre OST 使用率 | > 80% | > 90% | 空间不足影响写入 |
| Lustre OST 不均衡度 | 最大-最小 > 15% | > 25% | 需要 rebalance |
| JuiceFS 缓存命中率 | < 80% | < 60% | 缓存未预热或缓存空间不足 |
| JuiceFS 对象存储延迟 P99 | > 500ms | > 2s | 对象存储响应慢 |
| JuiceFS 缓存盘使用率 | > 90% | > 95% | 缓存淘汰压力 |
| etcd fsync 延迟 P99 | > 10ms | > 50ms | etcd 磁盘性能不足 |
| Checkpoint 保存耗时 | > 60s | > 120s | 需要优化异步保存或增加条带化 |
| 文件系统总使用率 | > 75% | > 85% | 容量预警 |
| 训练 GPU 空闲等待 IO | > 5% | > 15% | 存储成为训练瓶颈 |

---

## 10. 容量治理

### 10.1 配额管理

**Lustre 配额：**

```bash
# 查看用户配额
lfs quota -u alice /mnt/lustre
# 输出：
# Disk quotas for usr alice (uid 1001):
#      Filesystem    kbytes   quota   limit   grace   files   quota   limit   grace
#      /mnt/lustre   500G     800G    1000G    -      50000   100000  200000    -

# 设置用户配额
lfs setquota -u alice --block-softlimit 800G --block-hardlimit 1000G \
  --inode-softlimit 100000 --inode-hardlimit 200000 /mnt/lustre

# 设置组配额（按团队）
lfs setquota -g ml-team --block-softlimit 50T --block-hardlimit 60T \
  --inode-softlimit 5000000 --inode-hardlimit 10000000 /mnt/lustre

# 查看项目配额（按实验/项目）
lfs setquota -p project-llama --block-softlimit 20T --block-hardlimit 25T /mnt/lustre
lfs quota -p project-llama /mnt/lustre

# 查看所有用户配额汇总
lfs quota -t /mnt/lustre    # 查看 grace period
lfs quota -a -u /mnt/lustre # 查看所有用户（谨慎：输出可能很大）
```

**JuiceFS 目录配额：**

```bash
# 设置目录配额（JuiceFS v1.1+ 支持；基线 v1.2.x 全功能可用）
juicefs config redis://:password@10.0.1.50:6379/1 \
  --dir-quota /pretrain:10T

# 查看目录配额
juicefs config redis://:password@10.0.1.50:6379/1 --get-dir-quota /pretrain

# 设置用户配额
juicefs config redis://:password@10.0.1.50:6379/1 \
  --user-quota alice:500G

# 删除配额
juicefs config redis://:password@10.0.1.50:6379/1 \
  --del-dir-quota /pretrain
```

### 10.2 生命周期管理（冷数据下沉）

```bash
# === Lustre HSM（Hierarchical Storage Management） ===
# 适用: Lustre 2.15.x LTS；HSM 功能自 Lustre 2.4+ 引入
# 配置 HSM（将冷数据归档到对象存储）
lfs hsm_actions /mnt/lustre  # 查看当前 HSM 操作

# 手动归档不活跃文件
lfs hsm_archive /mnt/lustre/datasets/old-experiment-2025/

# 手动释放已归档文件的本地副本（变为 stub 文件）
lfs hsm_release /mnt/lustre/datasets/old-experiment-2025/

# 自动归档脚本
cat > /usr/local/bin/lustre-lifecycle.sh << 'EOF'
#!/bin/bash
# 将 30 天未访问的 checkpoint 归档
ARCHIVE_DIRS="/mnt/lustre/checkpoints/archive"
DAYS=30

for dir in $ARCHIVE_DIRS; do
  # 找到 30 天未修改的文件
  files=$(lfs find $dir --type f --mtime +$DAYS --size +100M 2>/dev/null)
  if [ -n "$files" ]; then
    echo "[$(date)] Archiving $(echo "$files" | wc -l) files in $dir"
    echo "$files" | xargs lfs hsm_archive
    # 归档成功后释放本地副本
    echo "$files" | xargs lfs hsm_release
  fi
done

# 清理 90 天前的 tensorboard 日志
find /mnt/lustre/logs/tensorboard -type f -mtime +90 -delete
EOF
chmod +x /usr/local/bin/lustre-lifecycle.sh
# crontab: 0 2 * * * /usr/local/bin/lustre-lifecycle.sh >> /var/log/lustre-lifecycle.log 2>&1
```

```bash
# === JuiceFS 生命周期 ===
# JuiceFS 没有内置 HSM，通过对象存储生命周期策略实现
# MinIO 生命周期策略示例：
mc alias set training-minio http://10.0.0.58:9000 admin REPLACE_WITH_STRONG_SECRET
mc ilm rule add training-minio/training-data \
  --transition-days 30 \
  --storage-class GLACIER \
  --prefix "checkpoints/archive/"

mc ilm rule add training-minio/training-data \
  --expire-days 90 \
  --prefix "logs/tensorboard/"
```

### 10.3 多租户存储隔离方案

```text
多租户存储隔离架构：

┌────────────────────────────────────────────────────────────┐
│                    存储集群                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Team A Pool  │  │ Team B Pool  │  │ Shared Pool  │     │
│  │ (独立 OST)   │  │ (独立 OST)   │  │ (公共 OST)   │     │
│  │ quota: 50TB  │  │ quota: 30TB  │  │ quota: 100TB │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
└────────────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Namespace:   │    │ Namespace:   │    │ Namespace:   │
│ ml-team-a    │    │ ml-team-b    │    │ shared-data  │
│              │    │              │    │              │
│ PVC: team-a- │    │ PVC: team-b- │    │ PVC: shared- │
│   data-pvc   │    │   data-pvc   │    │   models-pvc │
│ RWX          │    │ RWX          │    │ ROX          │
│ quota: 50TB  │    │ quota: 30TB  │    │ quota: 100TB │
└──────────────┘    └──────────────┘    └──────────────┘
```

```yaml
# 适用: K8s 1.29~1.32 / Lustre 2.15.x / Lustre OST Pool 隔离（Lustre 2.10+ 特性）
# 为不同团队分配独立的 OST Pool

# 1. 在 MDS 上创建 Pool（需 root）
ssh mds-01 'lfs pool_new gpfs01 team-a-pool'
ssh mds-01 'lfs pool_add gpfs01 team-a-pool OST0000 OST0001 OST0002 OST0003'

ssh mds-01 'lfs pool_new gpfs01 team-b-pool'
ssh mds-01 'lfs pool_add gpfs01 team-b-pool OST0004 OST0005 OST0006 OST0007'

# 2. 为团队目录设置默认 Pool
lfs setstripe --pool=team-a-pool /mnt/lustre/team-a/
lfs setstripe --pool=team-b-pool /mnt/lustre/team-b/

# 3. 验证隔离
lfs getstripe --pool /mnt/lustre/team-a/experiment-1/
# 输出: team-a-pool

# 4. 结合 K8s 的 Namespace 隔离
# team-a 的 PVC 只挂载 /mnt/lustre/team-a/ 子目录
# team-b 的 PVC 只挂载 /mnt/lustre/team-b/ 子目录

---
# K8s 层面隔离 - Team A 的 PV/PVC（适用: K8s 1.29~1.32 / apiVersion: v1）
# Lustre CSI Driver 示例（lustre.csi.example.com 为占位，实际用 lustre-csi-driver 或 hostPath/NFS CSI）
apiVersion: v1
kind: PersistentVolume
metadata:
  name: team-a-lustre-pv
spec:
  capacity:
    storage: 50Ti
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  mountOptions:
    - noatime
    - nodiratime
    - flock
  csi:
    driver: lustre.csi.example.com
    volumeHandle: team-a-data
    volumeAttributes:
      server: 10.0.1.10@tcp
      filesystem: gpfs01
      subPath: /team-a
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: team-a-data-pvc
  namespace: ml-team-a
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 50Ti
  volumeName: team-a-lustre-pv
```

```bash
# 定期容量审计脚本
cat > /usr/local/bin/storage-audit.sh << 'EOF'
#!/bin/bash
echo "=== Storage Capacity Audit $(date) ==="
echo ""

# Lustre 总体容量
echo "--- Lustre Filesystem ---"
lfs df -h /mnt/lustre

echo ""
echo "--- Per-Team Usage ---"
for team in team-a team-b shared; do
  usage=$(du -sh /mnt/lustre/$team 2>/dev/null | awk '{print $1}')
  echo "  $team: $usage"
done

echo ""
echo "--- Large Files (>100GB) ---"
lfs find /mnt/lustre --size +100G --type f | head -20

echo ""
echo "--- Orphan Checkpoints (not referenced in 60 days) ---"
lfs find /mnt/lustre/checkpoints --type f --atime +60 --name "*.pt" | wc -l
echo "orphan checkpoint files found"

echo ""
echo "--- JuiceFS Usage ---"
juicefs status redis://:password@10.0.1.50:6379/1 2>/dev/null | head -10
EOF
chmod +x /usr/local/bin/storage-audit.sh
# 每周执行: 0 3 * * 1 /usr/local/bin/storage-audit.sh >> /var/log/storage-audit.log 2>&1
```
