---
title: "GPU 集群运维（11）：安全与合规"
date: 2026-11-17T09:00:00+08:00
draft: false
description: "GPU 集群攻击面总览、驱动与供应链安全、容器与 GPU 设备安全、Kubernetes 安全、网络安全、数据安全与审计合规。"
tags: ["安全", "合规"]
series: "GPU 集群运维"
---

## 本册环境基线

| 组件 | 基线版本 | 说明 |
|------|---------|------|
| Kubernetes | 1.31.x | PSA 自 1.25 GA；audit.k8s.io/v1；EncryptionConfiguration apiserver.config.k8s.io/v1 |
| Kyverno | v1.13.x | ClusterPolicy kyverno.io/v1 |
| Gatekeeper | v3.17.x | ConstraintTemplate templates.gatekeeper.sh/v1；Constraint constraints.gatekeeper.sh/v1beta1 |
| Trivy | 0.58.x | 镜像/配置/secret 扫描 |
| cosign | 2.4.x | 镜像签名与 attestation |
| etcd | 3.5.x | EncryptionConfiguration at-rest 加密 |
| nvidia-container-toolkit | v1.17.4 | CDI 模式自 1.13+；1.17 稳定 |
| NetworkPolicy API | networking.k8s.io/v1 | 长期稳定（自 K8s 1.7 GA） |
| RBAC API | rbac.authorization.k8s.io/v1 | 长期稳定（自 K8s 1.8 GA） |
| 审计策略 API | audit.k8s.io/v1 | K8s 1.31 |
| OS 基线 | Ubuntu 22.04 LTS / Rocky Linux 9.x | 内核 5.15/5.14 GA |

---



GPU 集群因承载高价值模型权重、敏感训练数据与昂贵硬件资源，已成为安全攻防的高价值目标。本分册从攻击面分析出发，逐层覆盖驱动/固件、容器、Kubernetes、网络、数据各层的安全加固与合规实践。

---

## 1. GPU 集群攻击面总览

| 层级 | 威胁类型 | 典型攻击向量 | 影响 | 缓解措施 |
|------|---------|-------------|------|---------|
| 驱动/固件层 | 驱动漏洞利用 | NVIDIA driver CVE（如 CVE-2024-0126 越界写入） | 宿主机 RCE / GPU 劫持 | 驱动版本跟踪 + 及时补丁；INFO driver 版本锁定 |
| 驱动/固件层 | 固件篡改 | 恶意刷写 GPU VBIOS / InfoROM | 持久化后门 | 固件签名校验；物理接触管控 |
| 容器层 | 特权逃逸 | 挂载 `/dev/nvidia*` 后利用 ioctl 攻击内核 | 容器逃逸到宿主机 | 最小设备挂载 + Pod Security Standards |
| 容器层 | 恶意镜像 | 训练镜像捆绑挖矿/反弹 shell | 集群资源滥用 | Trivy 扫描 + 准入策略拦截 |
| K8s 层 | RBAC 过度授权 | `cluster-admin` 泛滥；SA token 泄露 | 全集群接管 | 最小权限 RBAC + SA 治理 |
| K8s 层 | etcd 数据泄露 | etcd 未加密 / 证书不当 | Secret 明文泄露 | etcd 加密 + 访问控制 |
| K8s 层 | apiserver 未授权访问 | 匿名访问开启 / 审计缺失 | 操作无追溯 | 禁用匿名 + 审计策略 |
| 网络层 | 管理面暴露 | IPMI/BMC 公网可达 | 硬件层完全控制 | 独立管理 VLAN + ACL 禁公网 |
| 网络层 | 东西向横移 | 租户间无网络隔离 | 跨租户攻击 | NetworkPolicy 严格隔离 |
| 网络层 | InfiniBand 嗅探 | IB 网络无分区 | 训练数据/梯度截获 | PKey 分区 + SM 认证 |
| 数据层 | 模型权重泄露 | checkpoint 文件未加密外传 | 核心资产损失 | 加密存储 + DLP + 审计 |
| 数据层 | 训练数据投毒 | 共享数据集被篡改 | 模型质量破坏 | 数据签名 + 完整性校验 |
| 数据层 | 数据出境违规 | 跨境传输敏感数据 | 合规处罚 | 数据分级 + 出境审计 |

---

## 2. 驱动与供应链安全

### 2.1 NVIDIA 安全公告跟踪

**CVE 订阅渠道：**

| 渠道 | 地址 / 方法 | 说明 |
|------|------------|------|
| NVIDIA Security Bulletin | https://www.nvidia.com/en-us/security/ | 月度更新，含 CVSS 评分 |
| NVIDIA Security Mailing | security-alert@nvidia.com | 邮件订阅 |
| NVD (NIST) | https://nvd.nist.gov/ — 搜索 `nvidia` | 独立评分，有时晚于官方 |
| GitHub Advisory | https://github.com/advisories — 搜索 `nvidia` | 容器生态相关 CVE |
| 运维监控 | 自建 RSS 拉取 NVIDIA bulletin + NVD feed | Prometheus alertmanager 集成 |

**驱动漏洞风险评估 SOP：**

1. 收到 CVE 通知后，在 1 小时内确认影响范围：`nvidia-smi -q | grep "Driver Version"` 批量采集当前版本
2. 对照 NVIDIA bulletin 确认受影响驱动版本与 CVSS 评分
3. CVSS ≥ 7.0（High/Critical）：72 小时内完成补丁验证与灰度推送
4. CVSS 4.0–6.9（Medium）：下个维护窗口修复
5. CVSS < 4.0（Low）：记录跟踪，随下次大版本升级
6. 补丁验证流程：单节点 → 单租户 → 全集群

### 2.2 镜像安全

**Trivy 镜像扫描命令示例（Trivy 0.58.x，`--security-checks` 参数在 0.58.x 中已更名为 `--scanners`，旧参数仍兼容，以 `trivy --help` 为准）：**

```bash
# Trivy 0.58.x — 扫描容器镜像中的漏洞
trivy image --severity HIGH,CRITICAL \
  --format table \
  --exit-code 1 \
  --ignore-unfixed \
  registry.example.com/training/pytorch:2.3.0-cuda12.4

# 扫描镜像中的错误配置（misconfiguration）
trivy image --security-checks config \
  --format table \
  registry.example.com/training/pytorch:2.3.0-cuda12.4

# 扫描镜像中的硬编码密钥
trivy image --security-checks secret \
  --format json \
  -o trivy-secrets.json \
  registry.example.com/training/pytorch:2.3.0-cuda12.4

# 扫描本地镜像的 SBOM 生成
trivy image --format cyclonedx \
  -o pytorch-bom.json \
  registry.example.com/training/pytorch:2.3.0-cuda12.4
```

**准入拦截 — Kyverno ClusterPolicy（拒绝高危镜像，Kyverno v1.13.x，kyverno.io/v1）：**

```yaml
# Kyverno v1.13.x — kyverno.io/v1
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: block-vulnerable-images
  annotations:
    policies.kyverno.io/title: 拒绝存在高危漏洞的镜像
    policies.kyverno.io/severity: high
spec:
  validationFailureAction: Enforce
  background: false
  webhookTimeouts:
    mutatingWebhookTimeout: 10s
    validatingWebhookTimeout: 10s
  rules:
    - name: check-image-vulnerabilities
      match:
        any:
          - resources:
              kinds:
                - Pod
      preconditions:
        all:
          - key: "{{request.namespace}}"
            operator: NotEquals
            value: "kube-system"
      validate:
        message: >-
          镜像存在 CRITICAL 漏洞，禁止部署。
          请联系安全团队评估后重新构建。
        attestors:
          - count: 1
            entries:
              - attestor:
                  keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
                      -----END PUBLIC KEY-----
                annotations:
                  trivy-scan: "pass"
    - name: require-trivy-annotation
      match:
        any:
          - resources:
              kinds:
                - Pod
      preconditions:
        all:
          - key: "{{request.namespace}}"
            operator: NotEquals
            value: "kube-system"
      validate:
        message: "镜像缺少 Trivy 扫描注解，CI/CD 流水线应先扫描再推送"
        pattern:
          metadata:
            annotations:
              security.example.com/trivy-scanned: "true"
              security.example.com/trivy-critical-count: "0"
```

**准入拦截 — OPA Gatekeeper ConstraintTemplate + Constraint（Gatekeeper v3.17.x）：**

```yaml
# Gatekeeper v3.17.x — templates.gatekeeper.sh/v1
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata:
  name: k8simagetrivycheck
spec:
  crd:
    spec:
      names:
        kind: K8sImageTrivyCheck
      validation:
        openAPIV3Schema:
          type: object
          properties:
            maxCritical:
              type: integer
              description: "允许的最大 CRITICAL 漏洞数"
            maxHigh:
              type: integer
              description: "允许的最大 HIGH 漏洞数"
  targets:
    - target: admission.k8s.gatekeeper.sh
      rego: |
        package k8simagetrivycheck

        violation[{"msg": msg}] {
          container := input.review.object.spec.containers[_]
          annotation := input.review.object.metadata.annotations
          critical := to_number(annotation["security.example.com/trivy-critical-count"])
          high := to_number(annotation["security.example.com/trivy-high-count"])
          max_crit := object.get(input.parameters, "maxCritical", 0)
          max_high := object.get(input.parameters, "maxHigh", 5)
          (critical > max_crit)
          msg := sprintf(
            "容器 %v 镜像 CRITICAL 漏洞数 %v 超过阈值 %v",
            [container.name, critical, max_crit]
          )
        }

        violation[{"msg": msg}] {
          container := input.review.object.spec.containers[_]
          annotation := input.review.object.metadata.annotations
          high := to_number(annotation["security.example.com/trivy-high-count"])
          max_high := object.get(input.parameters, "maxHigh", 5)
          (high > max_high)
          msg := sprintf(
            "容器 %v 镜像 HIGH 漏洞数 %v 超过阈值 %v",
            [container.name, high, max_high]
          )
        }
---
# Gatekeeper v3.17.x — constraints.gatekeeper.sh/v1beta1
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sImageTrivyCheck
metadata:
  name: image-trivy-policy
spec:
  match:
    kinds:
      - apiGroups: [""]
        kinds: ["Pod"]
    excludedNamespaces:
      - kube-system
      - kyverno
  parameters:
    maxCritical: 0
    maxHigh: 5
```

**镜像签名 cosign 要点（cosign 2.4.x）：**

```bash
# cosign 2.4.x — 生成密钥对（CI 中一次性生成，私钥存 Vault/KMS）
cosign generate-key-pair

# 签名镜像（CI 流水线末尾）
cosign sign --key cosign.key \
  -a "ci-run=build-4521" \
  -a "git-commit=abc123def" \
  registry.example.com/training/pytorch:2.3.0-cuda12.4

# 验证签名（部署前或准入策略中调用）
cosign verify --key cosign.pub \
  registry.example.com/training/pytorch:2.3.0-cuda12.4

# 附加 Trivy 扫描报告为 attestation
cosign attest --predicate trivy-report.json \
  --type vuln \
  --key cosign.key \
  registry.example.com/training/pytorch:2.3.0-cuda12.4
```

### 2.3 基础镜像版本管理策略

| 策略 | 做法 | 适用场景 |
|------|------|---------|
| 固定版本号 | `nvcr.io/nvidia/pytorch:24.04-py3` | 生产训练，禁止 `latest` |
| 月度重建 | 每月基于 NVIDIA NGC 最新 tag 重建 + 全量扫描 | 平衡安全与功能 |
| 版本锁定 | 维护 `approved-images.yaml` 配置清单 | 合规审计要求 |
| 回滚保留 | registry 中保留最近 6 个月的所有已发布 tag | 故障回滚需要 |

```yaml
# v1 ConfigMap（长期稳定 API）— approved-images.yaml
# 镜像 tag 须与 00-版本基线对齐（PyTorch 2.6.0 / vLLM 0.7.x，本基线快照 2026-01）
apiVersion: v1
kind: ConfigMap
metadata:
  name: approved-images
  namespace: platform-system
data:
  policy: |
    allowed_registries:
      - registry.example.com
      - nvcr.io/nvidia
    pinned_images:
      training-base: registry.example.com/training/pytorch:2.6.0-cuda12.8-r1   # 对齐基线 PyTorch 2.6.0 + CUDA 12.8
      inference-base: registry.example.com/inference/vllm:0.7.x-cuda12.8-r1    # 对齐基线 vLLM 0.7.x
      jupyter-base: registry.example.com/jupyter/pytorch:2.6.0-r1              # 对齐基线 PyTorch 2.6.0
    forbidden_tags:
      - latest
      - dev
      - nightly
```

---

## 3. 容器与 GPU 设备安全

### 3.1 特权容器风险与 /dev/nvidia* 攻击面

**挂载 /dev/nvidia* 的攻击面分析：**

| 设备节点 | 功能 | 被滥用风险 |
|----------|------|-----------|
| `/dev/nvidia0` ~ `/dev/nvidiaN` | GPU 设备主设备 | ioctl 攻击驱动漏洞 → 内核态执行 |
| `/dev/nvidiactl` | 控制接口 | 驱动级配置篡改 |
| `/dev/nvidia-uvm` | 统一虚拟内存 | 内存越界访问 |
| `/dev/nvidia-uvm-tools` | UVM 工具接口 | 调试接口信息泄露 |
| `/dev/nvidia-caps/*` | MIG 设备 | 相对隔离，攻击面较小 |

**最佳实践：**

- 禁止在 Pod spec 中使用 `privileged: true`
- 不直接挂载 `/dev/nvidia*`，改用 NVIDIA device plugin 的 `nvidia.com/gpu` resource
- 如必须挂载（如 nvidia-smi 诊断 Pod），使用 `securityContext.readOnlyRootFilesystem: true` + `allowPrivilegeEscalation: false`

```yaml
# K8s 1.31 — v1 Pod（安全诊断 Pod，临时诊断用，非常驻）
apiVersion: v1
kind: Pod
metadata:
  name: gpu-diagnostics
  namespace: platform-system
spec:
  restartPolicy: Never
  containers:
    - name: nvidia-smi
      image: nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04  # 对齐 00-版本基线 CUDA 12.8
      command: ["nvidia-smi", "-q"]
      resources:
        limits:
          nvidia.com/gpu: 1
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        runAsNonRoot: false  # nvidia-smi 通常需要 root 访问设备
        capabilities:
          drop:
            - ALL
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: tmp
      emptyDir: {}
  tolerations:
    - key: "nvidia.com/gpu"
      operator: "Exists"
      effect: "NoSchedule"
```

### 3.2 Pod Security Standards 三级配置（namespace label 方式）

> **版本要求：** Pod Security Admission（PSA）namespace label 方式自 **K8s 1.25 GA**，全系列 K8s 1.31 满足。`pod-security.kubernetes.io/*` label 由内置 PodSecurity admission controller 处理，无需额外部署 admission webhook。`enforce-version: "latest"` 对应当前 K8s 版本的最新 PSS 标准。

**Privileged 级别（仅限平台组件 namespace）：**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: platform-system
  labels:
    pod-security.kubernetes.io/enforce: "privileged"
    pod-security.kubernetes.io/enforce-version: "latest"
    pod-security.kubernetes.io/audit: "privileged"
    pod-security.kubernetes.io/audit-version: "latest"
    pod-security.kubernetes.io/warn: "privileged"
    pod-security.kubernetes.io/warn-version: "latest"
```

**Baseline 级别（基础设施服务 namespace，如监控、日志）：**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: monitoring
  labels:
    pod-security.kubernetes.io/enforce: "baseline"
    pod-security.kubernetes.io/enforce-version: "latest"
    pod-security.kubernetes.io/audit: "restricted"
    pod-security.kubernetes.io/audit-version: "latest"
    pod-security.kubernetes.io/warn: "restricted"
    pod-security.kubernetes.io/warn-version: "latest"
```

**Restricted 级别（租户训练 namespace）：**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: tenant-team-a
  labels:
    pod-security.kubernetes.io/enforce: "restricted"
    pod-security.kubernetes.io/enforce-version: "latest"
    pod-security.kubernetes.io/audit: "restricted"
    pod-security.kubernetes.io/audit-version: "latest"
    pod-security.kubernetes.io/warn: "restricted"
    pod-security.kubernetes.io/warn-version: "latest"
```

Restricted 级别自动拒绝的操作：`privileged: true`、`hostNetwork`、`hostPID`、`hostIPC`、`hostPath` 挂载、`CAP_SYS_ADMIN` 等危险 capability、非安全端口容器、未设 `runAsNonRoot` 的容器（v1.25+）。

### 3.3 禁止 hostPath 滥用的 Kyverno 准入策略

```yaml
# Kyverno v1.13.x — kyverno.io/v1
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-hostpath
  annotations:
    policies.kyverno.io/title: 禁止 hostPath 挂载
    policies.kyverno.io/severity: high
    policies.kyverno.io/description: >-
      禁止任何 Pod 使用 hostPath 卷，防止容器直接访问宿主机文件系统。
      GPU 设备通过 NVIDIA device plugin 的 resource request 获取，
      不需要 hostPath 挂载 /dev 目录。
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: block-hostpath-volumes
      match:
        any:
          - resources:
              kinds:
                - Pod
      exclude:
        any:
          - resources:
              namespaces:
                - kube-system
                - platform-system
              selector:
                matchLabels:
                  platform.example.com/system-component: "true"
      validate:
        message: >-
          hostPath 卷已被禁止。GPU 设备请使用 nvidia.com/gpu resource request。
          如需宿主机文件访问请联系平台团队。
        deny:
          conditions:
            any:
              - key: "{{ request.object.spec.volumes[?hostPath] | length(@) }}"
                operator: GreaterThan
                value: 0
```

### 3.4 MIG 硬隔离 vs Time-Slicing 无隔离安全边界

| 特性 | MIG（Multi-Instance GPU） | Time-Slicing |
|------|---------------------------|--------------|
| 隔离级别 | 硬件级（独立 SM/内存/编码器） | 时间片共享（无隔离） |
| 内存隔离 | 是（ECC 独立、地址空间隔离） | 否（共享显存） |
| 故障隔离 | 单实例故障不影响其他 | 单进程 GPU hang 可影响所有切片 |
| 性能干扰 | 无（物理分区） | 有（时间片争抢） |
| 安全边界 | 可服务不同租户 | 仅限同一信任域内 |
| 适用场景 | 多租户 GPU 共享（不同部门/客户） | 同一团队内的资源超卖 |
| 可观测性 | 每个 GI（GPU Instance）独立指标 | 需 DCGM profiling 区分进程 |
| 配置复杂度 | 需要 nvidia-smi mig 配置 | 仅需 daemonset 配置 |

**安全决策规则：不同租户 → 必须 MIG；同一租户内多任务 → Time-Slicing 可接受。**

### 3.5 NVIDIA Container Toolkit 安全配置

> **版本说明：** 以下为 nvidia-container-toolkit **v1.17.4** 的配置格式。CDI（Container Device Interface）spec 生成命令 `nvidia-ctk cdi generate` 自 toolkit **≥1.13** 提供，1.17 稳定。rootless/no-cgroups 支持以实际安装版本为准，查阅 `nvidia-container-runtime --help`。

**no-cgroups 模式（适用于非 systemd 环境或 rootless）：**

```toml
# /etc/nvidia-container-runtime/config.toml
# nvidia-container-toolkit v1.17.4 配置格式
[nvidia-container-cli]
# root 用户运行 nvidia-container-cli 时使用的路径
root = "/"
path = "/usr/bin/nvidia-container-cli"
environment = []
# debug 日志（生产关闭）
#debug = "/var/log/nvidia-container-runtime.log"

[nvidia-container-runtime]
# 日志级别
log-level = "info"
log-path = "/var/log/nvidia-container-runtime.log"

# 禁用 cgroups 设备权限管理（rootless 或自定义 cgroup 场景）
[nvidia-container-runtime.modes.cdi]
annotation-prefixes = ["cdi.k8s.io/"]

# 关键安全参数
no-cgroups = false  # 生产环境保持 false，由容器运行时管理设备权限
# 仅在 rootless podman 等场景设为 true，此时需在 OCI spec 中显式声明设备

[nvidia-container-runtime.modes.csv]
create-device-specs = true
```

**rootless 运行要点：**

- rootless 模式下容器以非 root 用户运行，NVIDIA 设备节点权限通过 CDI（Container Device Interface）spec 管理
- 需要 `nvidia-cdi-hook` 注入设备，而非传统的 `nvidia-container-cli` 挂载
- 生产 K8s 场景仍以 root 运行 containerd + NVIDIA runtime 为主，rootless 主要用于开发工作站

---

## 4. Kubernetes 安全

### 4.1 RBAC 最小权限完整示例

> **API 稳定性：** `rbac.authorization.k8s.io/v1` 自 K8s 1.8 GA，长期稳定（K8s 1.31 无变化）。

**租户 Role（只能操作自己 namespace 的 Pod/Job）：**

```yaml
# rbac.authorization.k8s.io/v1 — 长期稳定 API（K8s 1.31 无变化）
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tenant-trainer
  namespace: tenant-team-a
rules:
  # Pod 操作（创建训练任务）
  - apiGroups: [""]
    resources: ["pods", "pods/log", "pods/status"]
    verbs: ["get", "list", "watch", "create", "delete"]
  # Pod attach/exec 仅限开发 namespace（可选，敏感环境应禁用）
  - apiGroups: [""]
    resources: ["pods/exec", "pods/attach"]
    verbs: ["create"]
  # Job 管理
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete"]
  # ConfigMap/Secret 只读（模型配置、训练超参）
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  # Secret 仅限指定前缀（防止看到其他租户 Secret）
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["training-*"]  # 只允许访问 training- 开头的 Secret
    verbs: ["get"]
  # PVC 使用（绑定已创建的存储）
  - apiGroups: [""]
    resources: ["persistentvolumeclaims"]
    verbs: ["get", "list", "watch"]
  # Service 创建（TensorBoard/Jupyter）
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "watch", "create", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: tenant-team-a-trainer-binding
  namespace: tenant-team-a
subjects:
  - kind: User
    name: "oidc:alice@example.com"
    apiGroup: rbac.authorization.k8s.io
  - kind: User
    name: "oidc:bob@example.com"
    apiGroup: rbac.authorization.k8s.io
  - kind: Group
    name: "oidc:team-a-ml-engineers"
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: tenant-trainer
  apiGroup: rbac.authorization.k8s.io
```

**租户只读 Role（数据科学家只看不改）：**

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tenant-viewer
  namespace: tenant-team-a
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "pods/status"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["configmaps", "services"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: tenant-team-a-viewer-binding
  namespace: tenant-team-a
subjects:
  - kind: Group
    name: "oidc:team-a-data-scientists"
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: tenant-viewer
  apiGroup: rbac.authorization.k8s.io
```

### 4.2 ServiceAccount 治理

```yaml
# K8s 1.31 — v1 ServiceAccount（长期稳定 API）
# 租户训练任务专用 SA（非 default SA）
apiVersion: v1
kind: ServiceAccount
metadata:
  name: training-job-sa
  namespace: tenant-team-a
automountServiceAccountToken: false  # 默认不挂载 token
---
# K8s 1.31 — v1 Pod（长期稳定 API）
# 训练任务 Pod 显式引用 SA
apiVersion: v1
kind: Pod
metadata:
  name: bert-finetune
  namespace: tenant-team-a
spec:
  serviceAccountName: training-job-sa
  automountServiceAccountToken: false  # Pod 级别也关闭
  containers:
    - name: trainer
      image: registry.example.com/training/pytorch:2.6.0-cuda12.8-r1  # 对齐基线 PyTorch 2.6.0 + CUDA 12.8
      resources:
        limits:
          nvidia.com/gpu: 8
```

**SA 治理规则：**
- 每个租户 namespace 创建专用 SA，禁止使用 `default` SA
- `automountServiceAccountToken: false` 全局默认关闭
- 定期审计未使用的 SA：`kubectl get sa --all-namespaces -o json | jq '.items[] | select(.metadata.name != "default") | .metadata.namespace + "/" + .metadata.name'`

### 4.3 etcd 加密（EncryptionConfiguration）

> **版本说明：** `apiserver.config.k8s.io/v1` 对应 K8s 1.31 + etcd 3.5。KMS v2（`apiVersion: v2`）自 K8s 1.29 GA；KMS v1（`apiVersion: v1`）在 K8s 1.28 废弃、1.29 移除。etcd 加密需 etcdctl 3.5.x + `ETCDCTL_API=3`。

```yaml
# EncryptionConfiguration — K8s 1.31 + etcd 3.5
# KMS v2（apiVersion: v2）自 K8s 1.29 GA；v1 已于 1.29 移除
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  # Secret 资源使用 AES-CBC + KMS 双层加密
  - resources:
      - secrets
    providers:
      # 首选：KMS 插件（对接 HashiCorp Vault / AWS KMS）
      - kms:
          name: vault-kms-provider
          endpoint: unix:///var/run/kmsplugin/vault.sock
          cachesize: 1000
          timeout: 3s
          apiVersion: v2
      # 备选：本地 AES-CBC 密钥（密钥轮换需手动）
      - aescbc:
          keys:
            - name: key-2026-01
              secret: BASE64_ENCODED_32_BYTE_KEY_HERE==
      # 最后兜底：明文（仅用于迁移期，正常运行应删除此行）
      # - identity: {}
  # ConfigMap 也建议加密（可能含敏感配置）
  - resources:
      - configmaps
    providers:
      - aescbc:
          keys:
            - name: key-cm-2026-01
              secret: BASE64_ENCODED_32_BYTE_KEY_FOR_CM==
      - identity: {}
```

**启用加密的 apiserver 参数：**

```bash
# /etc/kubernetes/manifests/kube-apiserver.yaml 中添加
--encryption-provider-config=/etc/kubernetes/encryption/encryption-config.yaml
--encryption-provider-config-automatic-reload=true  # 支持不重启热加载配置
```

**密钥生成与轮换 SOP：**

1. 生成 32 字节随机密钥：`head -c 32 /dev/urandom | base64`
2. 将新密钥添加到 providers 列表最前面（新写入使用新密钥）
3. 执行全量 re-encryption：`kubectl get secrets --all-namespaces -o json | kubectl replace -f -`
4. 确认所有 Secret 已用新密钥加密后，删除旧密钥 provider

### 4.4 apiserver 安全参数清单

```yaml
# kube-apiserver 安全相关启动参数（/etc/kubernetes/manifests/kube-apiserver.yaml）
apiVersion: v1
kind: Pod
metadata:
  name: kube-apiserver
  namespace: kube-system
spec:
  containers:
    - command:
        - kube-apiserver
        # 禁止匿名访问
        - --anonymous-auth=false
        # 审计日志
        - --audit-log-path=/var/log/kubernetes/audit/audit.log
        - --audit-log-maxage=90
        - --audit-log-maxbackup=30
        - --audit-log-maxsize=200
        - --audit-policy-file=/etc/kubernetes/audit/audit-policy.yaml
        # 认证
        - --oidc-issuer-url=https://auth.example.com/realms/gpu-cluster
        - --oidc-client-id=kubernetes
        - --oidc-username-claim=email
        - --oidc-groups-claim=groups
        # 授权
        - --authorization-mode=Node,RBAC
        # 准入控制
        - --enable-admission-plugins=NodeRestriction,PodSecurity
        # 加密
        - --encryption-provider-config=/etc/kubernetes/encryption/encryption-config.yaml
        - --encryption-provider-config-automatic-reload=true
        # 安全端口
        - --secure-port=6443
        # 禁止不安全端口
        - --port=0
        # Token 过期
        - --service-account-max-token-expiration=24h
      volumeMounts:
        - mountPath: /etc/kubernetes/audit
          name: audit-config
          readOnly: true
        - mountPath: /var/log/kubernetes/audit
          name: audit-log
        - mountPath: /etc/kubernetes/encryption
          name: encryption-config
          readOnly: true
  volumes:
    - hostPath:
        path: /etc/kubernetes/audit
      name: audit-config
    - hostPath:
        path: /var/log/kubernetes/audit
      name: audit-log
    - hostPath:
        path: /etc/kubernetes/encryption
      name: encryption-config
```

**审计日志策略完整 YAML（`/etc/kubernetes/audit/audit-policy.yaml`，K8s 1.31，audit.k8s.io/v1）：**

```yaml
# K8s 1.31 — audit.k8s.io/v1
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # 最高级别：记录 Secret 的所有操作（不含值，只记 metadata）
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]
    omitStages:
      - RequestReceived

  # 高级别：RBAC 变更完整记录
  - level: RequestResponse
    resources:
      - group: "rbac.authorization.k8s.io"
        resources:
          - clusterroles
          - clusterrolebindings
          - roles
          - rolebindings
    omitStages:
      - RequestReceived

  # 高级别：namespace 操作
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["namespaces"]
    omitStages:
      - RequestReceived

  # 中级别：Pod/Job 的创建删除（GPU 资源审计核心）
  - level: Request
    resources:
      - group: ""
        resources: ["pods"]
      - group: "batch"
        resources: ["jobs"]
    verbs: ["create", "delete", "update", "patch"]
    omitStages:
      - RequestReceived

  # 中级别：Node 状态变更
  - level: Request
    resources:
      - group: ""
        resources: ["nodes"]
    verbs: ["patch", "update"]
    omitStages:
      - RequestReceived

  # 低级别：其他所有操作记 metadata
  - level: Metadata
    omitStages:
      - RequestReceived

  # 不记录健康检查和只读 list/watch（减少日志量）
  - level: None
    verbs: ["get", "list", "watch"]
    resources:
      - group: ""
        resources: ["pods", "nodes", "services"]
```

### 4.5 NetworkPolicy 租户隔离

> **API 稳定性：** `networking.k8s.io/v1` NetworkPolicy 自 K8s 1.7 GA，长期稳定，K8s 1.31 无变化。

```yaml
# networking.k8s.io/v1 — 长期稳定（自 K8s 1.7 GA）
# 默认拒绝所有入站（每个租户 namespace 必加）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: tenant-team-a
spec:
  podSelector: {}  # 匹配 namespace 内所有 Pod
  policyTypes:
    - Ingress
---
# 默认拒绝所有出站
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-egress
  namespace: tenant-team-a
spec:
  podSelector: {}
  policyTypes:
    - Egress
---
# 允许 namespace 内部通信（训练任务 + TensorBoard）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: tenant-team-a
spec:
  podSelector: {}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}  # 同 namespace 的 Pod
---
# 允许出站：DNS + 对象存储 + 镜像仓库 + NCCL 通信端口
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-essential-egress
  namespace: tenant-team-a
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    # DNS
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # 镜像仓库
    - to:
        - ipBlock:
            cidr: 10.10.1.0/24  # 镜像仓库网段
      ports:
        - protocol: TCP
          port: 5000
    # 对象存储（训练数据/checkpoint）
    - to:
        - ipBlock:
            cidr: 10.10.2.0/24  # 存储网段
      ports:
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 9000  # MinIO S3 兼容
    # NCCL 通信（分布式训练，同 namespace 内）
    - to:
        - podSelector: {}
      ports:
        - protocol: TCP
          port: 29500  # PyTorch DDP 默认 rendezvous 端口
---
# 允许特定 label 的 Pod 接收 Ingress（TensorBoard/Jupyter）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-web-ingress
  namespace: tenant-team-a
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: web-ui
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              platform.example.com/ingress-allowed: "true"
      ports:
        - protocol: TCP
          port: 8888  # Jupyter
        - protocol: TCP
          port: 6006  # TensorBoard
---
# 禁止访问 metadata service（云环境防 SSRF）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: block-metadata-service
  namespace: tenant-team-a
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 169.254.169.254/32  # AWS/GCP metadata
              - 100.100.100.200/32  # 阿里云 metadata
```

---

## 5. 网络安全

### 5.1 三网分离与 ACL 设计

| 网络平面 | 用途 | 网段示例 | 带宽要求 | ACL 规则 |
|---------|------|---------|---------|---------|
| 管理网 | IPMI/BMC、交换机管理、K8s API | 10.100.0.0/16 | 1Gbps | 仅允许跳板机网段访问；禁止公网 |
| 业务网 | Pod 通信、服务暴露、对象存储 | 10.200.0.0/16 | 25/100Gbps | 租户间 NetworkPolicy 隔离 |
| 存储/IB 网 | GPU 互联、分布式存储 | 10.300.0.0/16 或 IB fabric | 200/400Gbps | PKey 分区（IB）；ACL 仅限 GPU 节点 |

**ACL 核心规则（边界防火墙/交换机 ACL）：**

```bash
# 华为交换机 ACL 示例（管理网隔离）
acl number 3001
 description management-network-acl
 rule 5 permit ip source 10.100.0.0 0.0.255.255 destination 10.100.0.0 0.0.255.255
 rule 10 permit ip source 10.200.1.0 0.0.0.255 destination 10.100.0.0 0.0.255.255  # 跳板机
 rule 15 permit tcp source 10.200.0.0 0.0.255.255 destination 10.100.1.0 0.0.0.255 destination-port eq 6443  # apiserver
 rule 100 deny ip source any destination 10.100.0.0 0.0.255.255
#
interface Vlanif100
 traffic-filter inbound acl 3001
```

### 5.2 BMC/IPMI 安全

| 风险 | 缓解措施 |
|------|---------|
| 默认口令（admin/admin） | 交付验收时强制改密；密码复杂度 ≥16 字符 |
| 管理口暴露公网 | 物理隔离管理 VLAN；边界防火墙 deny all |
| IPMI 2.0 认证弱点 | 禁用 IPMI over LAN（cipher 0）；仅允许 cipher 3+ |
| 固件漏洞 | 厂商固件更新跟踪；BMC 固件版本纳入巡检清单 |
| 未授权 KVM 访问 | KVM over IP 单独认证；操作录像 |

**IPMI 口令批量修改脚本（以 Dell iDRAC 为例）：**

```bash
#!/bin/bash
# batch-idrac-password.sh
# 用法: ./batch-idrac-password.sh <bmc-ip-list-file> <new-password>

IP_LIST="$1"
NEW_PASS="$2"
USER="root"

while IFS= read -r bmc_ip; do
  echo "[INFO] Changing password for BMC ${bmc_ip}..."
  ipmitool -I lanplus -H "${bmc_ip}" -U "${USER}" -P "${OLD_PASS}" \
    user set password 2 "${NEW_PASS}"
  if [ $? -eq 0 ]; then
    echo "[OK] ${bmc_ip} password changed"
  else
    echo "[FAIL] ${bmc_ip} password change failed"
  fi
done < "${IP_LIST}"
```

### 5.3 InfiniBand 网络安全

**IB 网络特殊攻击面：** IB 协议不基于 IP（使用 LID/GID 寻址），传统防火墙/IDS 无法直接监控 IB 流量。

| 威胁 | 缓解措施 |
|------|---------|
| 流量嗅探 | PKey 分区隔离 + 物理安全 |
| SM（Subnet Manager）篡改 | SM 认证 + 备用 SM 冗余 |
| 未授权节点接入 | Port GUID 白名单 + 物理端口管控 |
| 分区逃逸 | PKey 全成员 vs 有限成员权限控制 |

**PKey 分区隔离多租户配置示例（OpenSM 配置文件）：**

```bash
# /etc/opensm/partitions.conf
# 格式: PKey=0x<hex>, ipoib, DEFmembership=full|limited
# full = 可互相通信；limited = 只能与 full 成员通信

# 管理分区（所有节点加入，limited 权限）
Default=0x7fff, ipoib, DEFmembership=full:
  ALL=limited;
  mgmt-node-GUID=full;

# 租户 A 分区（仅租户 A 节点可通信）
TenantA=0x0001, ipoib, DEFmembership=full:
  # 租户 A 的 GPU 节点 port GUID
  0x0002c903001a2b3c=full;
  0x0002c903001a2b3d=full;
  0x0002c903001a2b3e=full;
  0x0002c903001a2b3f=full;

# 租户 B 分区（仅租户 B 节点可通信）
TenantB=0x0002, ipoib, DEFmembership=full:
  0x0002c903002c4d5e=full;
  0x0002c903002c4d5f=full;
  0x0002c903002c4d60=full;
  0x0002c903002c4d61=full;

# 存储分区（所有节点需要访问存储 target）
Storage=0x0010, ipoib, DEFmembership=full:
  storage-target-01=full;
  storage-target-02=full;
  ALL=limited;
```

**SM 安全加固（OpenSM）：**

```text
# /etc/opensm/opensm.conf 安全相关参数
subnet_manager TRUE
sm_priority 10
smp_mkey 0x1234567890abcdef  # Management Key（所有节点必须一致）
mkey_protect TRUE             # 防止未授权的 SM 修改节点配置
mkey_lease_period 0           # 永不过期（配合 protect 使用）
```

### 5.4 东西向流量管控

| 场景 | 策略 | 工具 |
|------|------|------|
| 租户间 Pod 通信 | NetworkPolicy deny all + 白名单 | K8s NetworkPolicy / Cilium |
| 同租户跨节点训练 | 允许 NCCL 端口段 | NetworkPolicy egress + IB PKey |
| 服务网格东西向 | mTLS 加密 + 授权策略 | Istio/Cilium Service Mesh |
| 异常流量检测 | 基于流日志的异常检测 | sFlow/NetFlow + 告警规则 |

---

## 6. 数据安全

### 6.1 训练数据分级分类

| 级别 | 定义 | 示例 | 存储要求 | 访问控制 |
|------|------|------|---------|---------|
| L1 公开 | 可公开发布 | ImageNet、Common Crawl | 无特殊要求 | 无限制 |
| L2 内部 | 公司内部使用 | 内部标注数据、业务日志 | 内网存储 | 部门级权限 |
| L3 机密 | 敏感业务数据 | 客户对话数据、金融交易数据 | 加密存储 + 审计 | 项目级权限 + 审批 |
| L4 受限 | 法规限制 | 医疗影像、个人身份信息、军事数据 | 加密 + 物理隔离 + DLP | 最小知悉 + 脱敏 + 审计 |

### 6.2 静态加密

| 存储系统 | 加密方案 | 加密层 | 性能影响 |
|---------|---------|-------|---------|
| Lustre | Lustre 客户端加密（fscrypt）或后端 LUKS | 文件系统层/块设备层 | 吞吐下降约 5-10% |
| 对象存储 (MinIO/S3) | SSE-S3（服务端）/ SSE-C（客户端密钥）| 对象层 | 几乎无感（硬件 AES-NI）|
| JuiceFS | 客户端加密（AES-256-GCM） | 文件层（写入前加密） | 读吞吐下降约 10-15% |
| NFS (NFSv4.1) | Kerberos + 传输加密（可选 pNFS） | 传输层 | 连接建立有开销 |

**JuiceFS 加密配置示例（`juicefs format` 时启用）：**

```bash
juicefs format \
  --storage minio \
  --bucket http://minio.example.com:9000/juicefs-data \
  --access-key minioadmin \
  --secret-key "${MINIO_SECRET_KEY}" \
  --encrypt-rsa-key /etc/juicefs/encrypt-key.pem \
  redis://redis.example.com:6379/1 \
  training-data

# 挂载时需提供解密私钥
juicefs mount \
  --encrypt-rsa-key /etc/juicefs/encrypt-key.pem \
  redis://redis.example.com:6379/1 \
  /mnt/training-data
```

**MinIO SSE-S3 配置（KMS 集成）：**

```bash
# MinIO 配置 KES（Key Encryption Service）+ Vault
export MINIO_KMS_KES_ENDPOINT=https://kes.example.com:7373
export MINIO_KMS_KES_CERT_FILE=/etc/minio/kes/client.crt
export MINIO_KMS_KES_KEY_FILE=/etc/minio/kes/client.key
export MINIO_KMS_KES_CAPATH=/etc/minio/kes/ca.crt
export MINIO_KMS_KES_KEY_NAME=minio-training-key

# bucket 级别强制 SSE-S3
mc admin bucket encryption set --sse-s3 myminio/training-data
```

### 6.3 传输加密

| 链路 | 加密方案 | 说明 |
|------|---------|------|
| K8s 内部通信 | mTLS（apiserver/kubelet 证书） | 默认已启用 |
| 对象存储访问 | HTTPS (TLS 1.2+) | 强制 HTTPS endpoint |
| NCCL GPU 通信 | 物理隔离为主（IB PKey）；可选 TLS | NCCL 原生不支持 TLS，依赖网络层 |
| SSH 远程访问 | SSH 密钥认证 + 禁止密码 | 跳板机统一入口 |
| 跨站点传输 | IPsec VPN / 专线 | 站点间数据同步 |

### 6.4 多租户数据不可见实现

| 方案 | 隔离粒度 | 实现复杂度 | 安全性 | 适用场景 |
|------|---------|-----------|--------|---------|
| 独立 bucket/独立 namespace | bucket 级别 | 低 | 高（物理隔离） | 强合规要求 |
| 独立 PV（每租户） | PV 级别 | 中 | 高（K8s PV 隔离） | 中等规模 |
| 子目录 + POSIX 权限 | 目录级别 | 低 | 中（依赖 UID 映射） | 开发环境 |
| 子目录 + SELinux/AppArmor | 目录 + MAC | 高 | 高 | 高安全要求 |

**独立 PV 隔离方案示例：**

```yaml
# K8s 1.31 — v1 PersistentVolume（长期稳定 API）
# 租户 A 的 PV 和 PVC
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-tenant-a-training
  labels:
    tenant: team-a
spec:
  capacity:
    storage: 50Ti
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: juicefs.csi.example.com
    volumeHandle: tenant-a-training
    volumeAttributes:
      pathPrefix: /tenants/team-a/training
      secretName: juicefs-tenant-a
      secretNamespace: platform-system
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: tenant
              operator: In
              values: ["team-a"]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: training-data
  namespace: tenant-team-a
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 50Ti
  selector:
    matchLabels:
      tenant: team-a
  storageClassName: ""
```

### 6.5 模型权重保护

**checkpoint 访问审计实现：**

```bash
# 对象存储（MinIO）bucket notification + 审计日志
mc admin config set myminio notify_webhook:training-audit \
  endpoint="http://audit-collector.example.com:8080/webhook" \
  auth_token="${AUDIT_WEBHOOK_TOKEN}"

mc event add myminio/training-checkpoints \
  arn:minio:sqs::training-audit:webhook \
  --event get,put,delete
```

**防外泄 DLP（Data Loss Prevention）要点：**

| 控制点 | 措施 | 说明 |
|--------|------|------|
| 网络出口 | 出站流量大小异常检测 | 单 Pod 出流量 > 1GB/h 告警 |
| 容器内 | 禁止 curl/wget/scp 到外部 | 镜像中移除；NetworkPolicy 兜底 |
| 对象存储 | checkpoint bucket 禁止公网访问 | 仅内网 endpoint |
| 审计 | 模型文件下载操作记录到人 | 对象存储访问日志 + 用户关联 |
| 物理 | USB/外置存储禁用 | 服务器 BIOS 禁用 USB boot |

### 6.6 数据出境合规要点

| 合规要求 | 适用范围 | GPU 集群落地措施 |
|---------|---------|----------------|
| 《数据出境安全评估办法》 | 出境重要数据 / 大量个人信息 | 训练数据不出境；checkpoint 跨境传输需评估 |
| GDPR | 涉及欧盟公民数据 | 数据本地化存储；训练前脱敏 |
| 《个人信息保护法》 | 个人信息处理 | 模型训练使用脱敏数据；保留原始数据不出境 |
| 行业规范（金融/医疗） | 特定行业数据 | 独立集群物理隔离；全链路审计 |

---

## 7. 审计与合规

### 7.1 K8s 审计日志关键审计事件清单

| 事件类型 | 审计规则 | 级别 | 用途 |
|---------|---------|------|------|
| Secret 访问 | secrets.* | Metadata | 检测异常 Secret 读取 |
| RBAC 变更 | clusterroles/roles.* binding | RequestResponse | 权限变更追溯 |
| Pod 创建/删除（含 GPU） | pods create/delete | Request | GPU 资源使用追溯 |
| Node 状态变更 | nodes patch/update | Request | 节点下线/上线审计 |
| 认证失败 | 401/403 responses | Metadata | 暴力破解检测 |
| namespace 创建删除 | namespaces.* | RequestResponse | 环境变更追溯 |
| exec/attach 操作 | pods/exec create | Request | 交互式操作审计 |
| ServiceAccount token 创建 | serviceaccounts/token create | Request | token 滥用检测 |

### 7.2 GPU 用量审计（kube-state-metrics + DCGM label 关联）

**实现思路：**

1. `kube-state-metrics` 采集 Pod 的 GPU resource request（`kube_pod_container_resource_requests{resource="nvidia_com_gpu"}`）
2. DCGM Exporter 采集 GPU 实际利用率，label 中包含 `pod` 和 `namespace`
3. 通过 Prometheus 关联两个指标，得到「谁在什么时间用了哪些卡、实际利用率多少」

**PromQL 示例：**

```promql
# 某租户 namespace 的 GPU 实际利用率（过去 1h 平均）
avg_over_time(
  DCGM_FI_DEV_GPU_UTIL{namespace="tenant-team-a"}[1h]
)

# 关联 Pod 信息与 GPU 利用率
# 用 recording rule 预计算
# recording_rules.yaml:
groups:
  - name: gpu-audit
    interval: 30s
    rules:
      - record: tenant:gpu_utilization:avg1h
        expr: |
          avg_over_time(
            DCGM_FI_DEV_GPU_UTIL[1h]
          )
        labels:
          source: dcgm
      - record: tenant:gpu_request:sum
        expr: |
          sum by (namespace, node) (
            kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
          )
```

**Grafana Dashboard 面板建议：**

| 面板 | PromQL | 说明 |
|------|--------|------|
| 租户 GPU 使用趋势 | `sum(DCGM_FI_DEV_GPU_UTIL) by (namespace)` | 按 namespace 聚合利用率 |
| GPU 利用率 vs 请求量 | `tenant:gpu_utilization:avg1h / tenant:gpu_request:sum` | 发现资源浪费 |
| 单用户 GPU 时间线 | `DCGM_FI_DEV_GPU_UTIL{pod=~".*alice.*"}` | 追溯个人使用记录 |

### 7.3 操作审计

| 审计类型 | 工具 | 采集目标 | 保留期限 |
|---------|------|---------|---------|
| 跳板机会话录像 | asciinema-server / Teleport | 所有 SSH 会话 | ≥ 180 天 |
| 命令审计 | sudo + auditd + 日志采集 | root/sudo 操作 | ≥ 180 天 |
| K8s 操作审计 | apiserver audit log | 所有 kubectl 操作 | ≥ 90 天 |
| 对象存储操作审计 | MinIO audit / S3 access log | 数据读写操作 | ≥ 90 天 |

**sudo 日志采集配置：**

```bash
# /etc/sudoers.d/audit
Defaults logfile="/var/log/sudo.log"
Defaults log_input
Defaults log_output
Defaults iolog_dir=/var/log/sudo-io

# rsyslog 转发到中心日志
# /etc/rsyslog.d/sudo-audit.conf
$InputFileMonitor /var/log/sudo.log
$InputFileTag sudo-audit:
$InputFileStateFile sudo-audit-state
$InputRunFileMonitor
*.* @@log-collector.example.com:514
```

### 7.4 日志保留期限建议

| 日志类型 | 推荐保留期 | 合规依据 | 存储位置 |
|---------|-----------|---------|---------|
| K8s 审计日志 | 180 天（热）+ 1 年（冷） | 等保三级 | 对象存储归档 |
| 系统日志（syslog） | 90 天 | 等保 | ELK/Loki |
| 应用日志 | 30 天（热）+ 90 天（冷） | 业务需求 | ELK/Loki |
| 会话录像 | 180 天 | 等保三级 | 专用存储 |
| GPU DCGM 指标 | 90 天（原始）+ 1 年（聚合） | 运维需求 | Prometheus/Thanos |
| 对象存储访问日志 | 1 年 | 数据安全 | 对象存储自身归档 |

### 7.5 等保/ISO 27001 对 GPU 集群的映射要点

**等保 2.0 三级映射（GPU 集群重点控制点）：**

| 等保控制域 | 控制要求 | GPU 集群对应措施 |
|-----------|---------|----------------|
| 物理安全 | 机房门禁、视频监控 | GPU 机柜单独区域；门禁 + 摄像头 |
| 网络安全 | 网络隔离、访问控制 | 三网分离 + NetworkPolicy + IB PKey |
| 主机安全 | 漏洞管理、基线加固 | 驱动 CVE 跟踪 + 基线扫描 |
| 应用安全 | 身份认证、权限控制 | OIDC + RBAC + 准入策略 |
| 数据安全 | 加密、备份、审计 | 静态/传输加密 + 审计日志 |
| 运维安全 | 操作审计、变更管理 | 会话录像 + GitOps + 审批流程 |

**ISO 27001 关键映射：**

| ISO 控制 | GPU 集群措施 |
|---------|-------------|
| A.8.15 日志记录 | K8s 审计 + DCGM + 会话录像 |
| A.8.16 监控活动 | Prometheus + AlertManager + Grafana |
| A.8.20 网络安全 | 三网分离 + ACL + NetworkPolicy |
| A.8.24 加密 | etcd 加密 + 存储加密 + 传输加密 |
| A.5.15 访问控制 | RBAC 最小权限 + 定期审计 |
| A.8.9 配置管理 | GitOps + 基线扫描 + drift 检测 |
