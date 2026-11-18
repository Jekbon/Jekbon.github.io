---
title: "Kubernetes 排障的固定套路"
date: 2026-11-12T14:20:00+08:00
draft: false
tags: ["Kubernetes", "运维", "排障"]
summary: "Pod 起不来时，按固定顺序看几处信息，大部分问题能在两分钟内定位。"
ShowToc: true
---

排查 Pod 异常，靠的是固定顺序，而不是碰运气地敲命令。

## 第一步：看状态，不是看日志

```bash
kubectl get pod <name> -n <ns> -o wide
kubectl describe pod <name> -n <ns>
```

`describe` 底部的 **Events** 是最有价值的部分，按时间倒序读最后 10 条。容器还没起来时日志是空的，Events 才有答案。

常见状态对应的方向：

| 状态 | 优先怀疑 |
|------|---------|
| `Pending` | 资源不足 / 节点亲和性 / PVC 没绑定 |
| `ImagePullBackOff` | 镜像名或 tag 错、私有仓库凭据、节点网络 |
| `CrashLoopBackOff` | 应用自己启动失败，这时才去看日志 |
| `OOMKilled` | 内存 limit 太低，或应用有泄漏 |
| `Running` 但不通 | Service / Endpoints / 网络策略 |

## 第二步：容器起不来才看日志

```bash
kubectl logs <pod> -n <ns>                    # 当前容器
kubectl logs <pod> -n <ns> --previous         # 上一个已崩溃的容器，CrashLoop 关键
kubectl logs <pod> -n <ns> -c <container>     # 多容器 Pod 要指定
kubectl logs <pod> -n <ns> --tail=200 -f
```

`--previous` 是最容易忘的：容器反复重启时，`kubectl logs` 看到的是刚起来还没输出什么的新容器，真正的报错在上一次。

## 第三步：确认 Service 后面有没有端点

```bash
kubectl get endpoints <svc> -n <ns>
```

空列表意味着 selector 没匹配到任何 Ready 的 Pod —— 标签拼错，或 Pod 的 readinessProbe 没通过。此时 Service IP 能 ping 通也没用。

## 第四步：进到 Pod 里看

```bash
kubectl exec -it <pod> -n <ns> -- sh
kubectl debug <pod> -n <ns> -it --image=busybox --target=<container>
```

`kubectl debug` 对付 distroless / 无 shell 的镜像很有用，它挂一个临时容器共享进程命名空间。

## 一条命令看全集群异常

```bash
kubectl get pods -A --field-selector=status.phase!=Running
```

## 事件只保留一小时

默认 `--event-ttl` 是 1 小时，隔天再查现场就没了。要么及时导出，要么部署事件持久化组件：

```bash
kubectl get events -A --sort-by=.lastTimestamp > events-$(date +%F-%H%M).txt
```
