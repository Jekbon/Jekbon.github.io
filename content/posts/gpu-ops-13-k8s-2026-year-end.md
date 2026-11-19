---
title: "2026 年底的 Kubernetes：1.34 EOL、cgroup v1 退场与 DRA 的理想现实"
date: 2026-11-19T14:00:00+08:00
draft: false
description: "三个同时发生的事实：1.34 停止安全修复、1.35 起强制 cgroup v2 与 containerd 2.x、DRA 已 GA 但推理生态还没跟上。附版本支持矩阵与 GPU 集群的升级决策。"
tags: ["Kubernetes", "版本管理", "GPU", "DRA"]
series: "GPU 集群运维"
---

2026 年 10 月底到 11 月，Kubernetes 生态同时发生了三件事，每一件都直接影响
正在跑生产集群的人：1.34 停止安全修复、cgroup v1 正式退场、DRA 成为 GPU 调度的
"官方答案"但推理生态还没跟上。这篇把三件事的事实、时间线和决策依据放在一起。

## 一、版本线：10 月 28 日那批补丁是 1.34 的末班车

GitHub 上 kubernetes/kubernetes 的 release 记录显示，**2026-10-28 一次性发了四个补丁**：
v1.34.13、v1.35.10、v1.36.6、v1.37.2；随后 **2026-11-06 发布 v1.38.0-beta.0**，
1.38 进入 beta，按节奏 12 月 GA。

关键日期是 **1.34 于 2026-10-27 EOL**（2026-08-27 起进入 maintenance）。
也就是说 1.34.13 之后，1.34 集群不再有任何安全修复或 bug 修复。
而 10 月底这批补丁恰好包含 CVE 修复——还在 1.34 上的集群，面对的是
"修复停止日"与"新漏洞披露期"重叠的最糟窗口。

当前支持矩阵（2026-11）：

| 版本 | 状态 | 备注 |
|---|---|---|
| 1.38 | beta（11-06 beta.0） | 预计 12 月 GA，撞 KubeCon NA（12-09/10） |
| 1.37 "Garhwal" | 支持中 | 2026-08-26 发布，当前主线 |
| 1.36 | 支持中 | 10-28 发 1.36.6 |
| 1.35 "Timbernetes" | 支持中，**12-28 进 maintenance** | EOL 2027-02-28；移除 cgroup v1 与 containerd 1.x |
| 1.34 | **2026-10-27 EOL** | 1.34.13 为最后补丁 |

升级目标的选择因此很直接：**跳到 1.36 或 1.37，不要停在 1.35**——
1.35 在 12 月底就进 maintenance，现在升上去等于半年后又要动一次。

## 二、cgroup v1 退场：节点 OS 的硬性门槛

1.35 移除了 cgroup v1 和 containerd 1.x 的支持。这不是"建议"，是** kubelet 起不来**：
云厂商的托管节点池被迫在升级 kubelet 之前先换节点 OS 镜像。

对自建集群意味着升级前必须核查两件事：

```bash
# 节点 cgroup 版本：输出 cgroup2fs 才是 v2
stat -fc %T /sys/fs/cgroup/

# containerd 主版本：1.35+ 需要 2.x
containerd --version
```

按本系列的基线（Ubuntu 22.04 / Rocky 9）两者默认都是 cgroup v2 + containerd 2.x，安全；
**风险在更老的存量节点**——Ubuntu 20.04、CentOS 7 这类默认 cgroup v1 的镜像，
升级 kubelet 到 1.35+ 会直接失败。排查思路照本系列的分层模型走：
先看节点 Ready 状态与 kubelet 日志，再回溯 OS 与运行时版本，别先怀疑控制面。

## 三、DRA：官方答案已 GA，推理生态还在路上

Dynamic Resource Allocation 的 structured parameters 模型在 **1.34 GA**，
1.35 补齐剩余子特性。它解决的是 device plugin 表达不了的问题：
按**属性**（显存、MIG 剖面、拓扑亲和）申请设备，而不是按整数计数。

生态侧的现状（2026-11）：

- NVIDIA GPU Operator 提供官方 DRA driver，另有 SIG 的参考 driver
- AKS 明确：**启用 DRA driver 时必须禁用 legacy device plugin**，两者不共存
- Kueue 已在 DRA 之上做队列化批调度
- 但 **vLLM production-stack 的 DRA 支持仍是 open feature request（#1087）**——
  推理服务这一层还没把 DRA 当一等公民
- 社区讨论的焦点是"新 GPU 集群要不要 day-one 上 DRA"；
  第一手的大规模生产复盘极少，2026 年的公开内容以厂商 blog 与 how-to 为主

所以结论分两半：**存量集群继续 device plugin + GPU Operator 是稳的**，
DRA 带来的收益（属性化申请、拓扑感知）在推理场景还被生态缺口抵消；
**新建 GPU 集群值得把 DRA 列入评估**，前提是接受"推理栈尚未原生适配"现状，
并预留禁用 legacy plugin 的切换窗口。

## 四、给 GPU 集群的操作清单

| 项 | 动作 |
|---|---|
| 版本 ≤1.34 | 立即规划升级；10-27 后无安全修复，1.34.13 是末班车 |
| 升级目标 | 1.36 或 1.37；避开 12 月底进 maintenance 的 1.35 |
| 节点核查 | cgroup v2 + containerd 2.x，老 OS 镜像先换再升 kubelet |
| GPU 调度 | 存量维持 device plugin；新集群评估 DRA，注意禁 legacy plugin |
| 推理栈 | 跟踪 vLLM production-stack #1087，DRA 适配落地前不抢跑 |
| 基线文档 | 把上述版本与运行时要求写进版本基线，标注"版本敏感" |

最后一行是本系列一贯的原则：抛开版本谈命令都是耍流氓。
这三件事的共同点是**它们都是版本问题**——EOL 是版本生命周期，
cgroup 是节点运行时版本，DRA 是 API 与生态组件版本。
把它们写进基线、标注敏感点，比任何单次升级操作都更保值。

## 数据来源

- [Kubernetes Releases](https://kubernetes.io/releases/) 与
  [kubernetes/kubernetes releases](https://github.com/kubernetes/kubernetes/releases)（补丁与 beta 日期为 API 实测）
- [Kubernetes 1.34 End of Life on 27 October 2026 - Upgrade Guide](https://safeguard.sh/resources/blog/kubernetes-1-34-end-of-life-october-2026-upgrade-guide)
- [Kubernetes 1.35 Kills cgroup v1, Nodes Fail to Boot](https://shattered.io/kubernetes-1-35-cgroup-v1-removal-2026/)
- [Kubernetes v1.37: Garhwal](https://kubernetes.io/blog/2026/08/26/kubernetes-v1-37-release/)
- [Kubernetes v1.34: DRA has graduated to GA](https://kubernetes.io/blog/2025/09/01/kubernetes-v1-34-dra-updates/)
- [Understanding dynamic resource allocation in Kubernetes (CNCF)](https://www.cncf.io/blog/2026/07/01/understanding-dynamic-resource-allocation-in-kubernetes/)
- [DRA devices and drivers on Kubernetes (AKS)](https://blog.aks.azure.com/2025/11/17/dra-devices-and-drivers-on-kubernetes)
- [Support Kubernetes DRA for GPU · vllm-project/production-stack#1087](https://github.com/vllm-project/production-stack/issues/1087)
- [DRA Driver for NVIDIA GPUs - NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/dra-intro-install.html)
