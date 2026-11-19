---
title: "配了 Docker data-root 根分区还是爆：containerd 的 root 是另一个配置"
date: 2026-11-15T09:00:00+08:00
draft: false
description: "daemon.json 的 data-root 只搬走了 Docker 自身数据，containerd 的镜像层仍写在 /var/lib/containerd。一次 7.6G 镜像层的迁移记录与升级时的复发陷阱。"
tags: ["Docker", "containerd", "排障", "存储"]
---

## 问题描述

Docker 的 `data-root` 已经配置到大盘（`/data/docker_data`），但 `df -h` 显示
根分区 `/` 占用仍然偏高（24G）。检查发现 containerd 的镜像层数据还在根分区。

## 根因

Docker 底层用 containerd 作容器运行时。`daemon.json` 里的 `data-root`
**只修改 Docker 自身的数据目录，containerd 的 root 仍是默认的
`/var/lib/containerd`**，于是镜像层（overlayfs snapshots，约 7.6G）继续写根分区。

从 overlay mount 参数可以直接确认：

```text
lowerdir=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/...
```

看到 `lowerdir` 还指着 `/var/lib/containerd`，就是这个病。
这是一个非常常见的配置盲区：以为改了 `data-root` 就全搬完了，
实际上 Docker 和 containerd 是**两个各自有 root 目录的组件**。

## 修复步骤

```bash
# 1. 停掉依赖容器（按实际容器名）
docker stop gitlab
```

```bash
# 2. 停 Docker 和 containerd
systemctl stop docker docker.socket containerd
```

```bash
# 3. 迁移 containerd 数据到大盘
mv /var/lib/containerd /data/containerd
```

编辑 `/etc/containerd/config.toml`：

```diff
- #root = "/var/lib/containerd"
+ root = "/data/containerd"
```

```bash
# 4. 按依赖顺序重启
systemctl start containerd docker
```

```bash
# 5. 拉起业务容器
docker start gitlab
```

## 修复前后对比

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 根分区 `/` 已用 | 24G (13%) | 16G (9%) |
| `/data` 已用 | 6.7G (2%) | 15G (4%) |
| containerd overlay 路径 | `/var/lib/containerd/...` | `/data/containerd/...` |

迁移后的数据分布：

| 路径 | 大小 | 说明 |
|---|---|---|
| `/data/containerd/` | 7.6G | containerd 镜像层数据 |
| `/data/docker_data/` | 3.9G | Docker daemon 数据 |
| `/data/gitlab/` | 6.6G | 业务持久化数据 |

## 注意事项

**这个配置会在升级时复发。** 重装或升级 Docker/containerd 时，
包管理器可能把 `/etc/containerd/config.toml` 覆盖回默认值
（`root = "/var/lib/containerd"`），镜像层又会开始写根分区。

两个防御动作：

1. 把 `config.toml` 纳入配置管理（Ansible / GitOps），升级后校验 `root` 路径
2. 监控上加一条根分区使用率告警——这类「配置悄悄回退」的问题，
   往往先是磁盘告警发现的，而不是功能报错

顺带一提：如果用的是 Kubernetes 节点，containerd 的 root 还影响
`crictl` 与 kubelet 的镜像_gc 行为，迁移后记得一并确认
`/etc/crictl.yaml` 与 kubelet 的 `imageGCHighThresholdPercent` 仍符合预期。
