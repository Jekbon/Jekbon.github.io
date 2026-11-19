---
title: "GPU 集群运维（07）：日常运维与节点管理"
date: 2026-11-13T09:00:00+08:00
draft: false
description: "每日巡检体系、节点健康池管理、慢节点（Straggler）检测、变更管理、RMA 与备件、大任务护航 SOP、值班体系，附 GPU XID 79 掉卡 Runbook。"
tags: ["运维", "巡检", "GPU"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下为本书全部命令/脚本的版本上下文，详见 [00-版本基线](./00-版本基线.md)。

| 维度 | 基线版本 |
|---|---|
| OS | Ubuntu 22.04.5 LTS (kernel 5.15) 或 Rocky Linux 9.5 (kernel 5.14) |
| NVIDIA Driver | R570 570.124.04（示例）/ R550 550.127.08（示例，Ampere 存量） |
| CUDA Toolkit | 12.8（Hopper/Blackwell）/ 12.4（Ampere 存量） |
| DCGM | 3.3.9 / 4.2.x（dcgmi 命令行为随版本变化，见标注） |
| Kubernetes | 1.31.x（kubectl 与集群版本偏差 ≤1 minor） |
| containerd | 1.7.x LTS |
| etcd | 3.5.15+（etcdctl 3.5.x，3.5 默认 v3 API；3.4 及以前需 `ETCDCTL_API=3`） |
| DOCA-OFED | 24.10（MLNX_OFED 24.04-0.7.0.0 为末代旧名） |
| Fabric Manager | 与驱动完全同版本 |
| GSP 固件 | 随驱动打包，R515+ 默认启用 |
| nvidia-container-toolkit | v1.17.4 |
| chrony | 4.2（Ubuntu 22.04 apt） |

---

## 目录

- [1. 每日巡检体系](#1-每日巡检体系)
- [2. 节点健康池管理](#2-节点健康池管理)
- [3. 慢节点（Straggler）检测](#3-慢节点straggler检测)
- [4. 变更管理](#4-变更管理)
- [5. RMA 与备件](#5-rma-与备件)
- [6. 大任务护航 SOP](#6-大任务护航-sop)
- [7. 值班体系](#7-值班体系)
- [8. 备份策略](#8-备份策略)

---

## 1. 每日巡检体系

### 1.1 巡检项清单

| 巡检项 | 命令/方法 | 健康标准 | 频率 |
|---|---|---|---|
| nvidia-smi 在位 | `nvidia-smi` 返回码 = 0，卡数与预期一致 | 所有 GPU 可见，无掉卡 | 每次巡检 |
| ECC 错误计数 | `nvidia-smi -q -d ECC` | volatile SBE = 0，DBE = 0；aggregate DBE > 0 立即隔离 | 每次巡检 |
| XID 错误 | `dmesg -T \| grep 'NVRM.*Xid'` 近 24h | 无 XID 79/95/48/63/64 等硬件类错误 | 每次巡检 |
| GPU 温度 | `nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader` | ≤ 83°C（A100），≤ 87°C（H100） | 每次巡检 |
| GPU 功耗 | `nvidia-smi --query-gpu=power.draw,power.limit --format=csv` | 实际功耗在 limit 的 95% 以内（非降频状态） | 每次巡检 |
| DCGM Level-1 诊断 | `dcgmi diag -r 1` | 全部 PASS | 每日一次 |
| IB 端口状态 | `ibstat \| grep -E 'Port|State'` | State = Active，Physical = LinkUp | 每次巡检 |
| 内核日志关键字 | `dmesg -T \| grep -iE 'error\|fail\|mce\|edac\|hardware'` | 无新增 GPU/PCIe/Memory 错误 | 每次巡检 |
| 磁盘空间 | `df -h` | 使用率 < 85% | 每次巡检 |
| inode 使用率 | `df -i` | 使用率 < 80% | 每次巡检 |
| 时钟偏移 | `chronyc tracking \| grep 'System time'` | 偏移 < 10ms | 每次巡检 |
| 风扇转速 | `nvidia-smi --query-gpu=fan.speed --format=csv,noheader` | 30%~100%，无 0%（风扇停转） | 每次巡检 |
| NVLink 状态 | `nvidia-smi nvlink --status` | 所有 link = Active | 每日一次 |
| PCIe 带宽 | `nvidia-smi -q -d PCIE \| grep 'Tx Throughput'` | 接近满带宽（x16 Gen4 ≈ 25 GB/s） | 每日一次 |

> **版本说明**：XID 92/94/95 自较新驱动分支（R418/R465+）引入；XID 119/120 为 GSP 相关，自 R515+ 引入。详见 [08-故障排查与案例库 §3.1](./08-故障排查与案例库.md)。`nvidia-smi --query-gpu=fan.speed` 在 PCIe 卡（如 A30/A10）返回 N/A，仅 SXM 模组有风扇读数。

### 1.2 完整节点巡检脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# gpu_node_inspect.sh — GPU 节点每日巡检脚本
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / DCGM 3.3+ / K8s 1.31 (kubectl)
# 参考硬件：A100 80GB SXM 或 H100 80GB SXM
# 用法：sudo bash gpu_node_inspect.sh [--output /path/to/report]
# 输出：结构化文本报告，同时写入 syslog
# 版本说明：
#   - nvidia-smi --query-gpu 的 ecc.errors.* 字段自 R4xx+ 稳定；R550+ 输出格式含 SRAM/DRAM 分项
#   - dcgmi diag -r 1 自 DCGM 3.3+ 支持；4.x 增加了 GPU 子测试项
#   - chronyc tracking 的 'System time' 字段格式 chrony 4.x 稳定
#===============================================================================

set -euo pipefail

# ---------- 可配置参数 ----------
HOSTNAME=$(hostname)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
REPORT_DIR="${1:-/var/log/gpu_inspect}"
REPORT_FILE="${REPORT_DIR}/${HOSTNAME}_$(date '+%Y%m%d_%H%M%S').txt"
EXPECTED_GPU_COUNT=8          # 预期 GPU 数量
MAX_TEMP_A100=83              # A100 温度阈值 °C
MAX_TEMP_H100=87              # H100 温度阈值 °C
MAX_TEMP=${MAX_TEMP:-$MAX_TEMP_A100}
MAX_ECC_DBE=0                 # DBE 容忍值（0 = 零容忍）
MAX_CLOCK_OFFSET_MS=10        # chrony 时钟偏移阈值 ms
DISK_USAGE_WARN=85            # 磁盘使用率告警 %
INODE_USAGE_WARN=80           # inode 使用率告警 %
XID_LOOKBACK_HOURS=24         # XID 回溯小时数

# ---------- 颜色定义 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ---------- 计数器 ----------
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

mkdir -p "$REPORT_DIR"

# ---------- 工具函数 ----------
log_result() {
    local item="$1" status="$2" detail="$3"
    case "$status" in
        PASS) ((PASS_COUNT++)); echo -e "[${GREEN}PASS${NC}] $item: $detail" ;;
        WARN) ((WARN_COUNT++)); echo -e "[${YELLOW}WARN${NC}] $item: $detail" ;;
        FAIL) ((FAIL_COUNT++)); echo -e "[${RED}FAIL${NC}] $item: $detail" ;;
    esac
    echo "$(date '+%Y-%m-%dT%H:%M:%S') [$status] $item: $detail" >> "$REPORT_FILE"
}

separator() {
    echo "================================================================"
    echo "$1" | tee -a "$REPORT_FILE"
    echo "================================================================"
}

# ---------- 报告头 ----------
separator "GPU 节点巡检报告"
echo "主机名:  $HOSTNAME"  | tee -a "$REPORT_FILE"
echo "时间:    $TIMESTAMP" | tee -a "$REPORT_FILE"
echo "巡检人:  ${SUDO_USER:-root}" | tee -a "$REPORT_FILE"
echo ""

# ========== 1. nvidia-smi 在位检查 ==========
separator "1. nvidia-smi 在位检查"
if ! command -v nvidia-smi &>/dev/null; then
    log_result "nvidia-smi" "FAIL" "命令不存在，驱动可能未安装"
else
    GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader 2>/dev/null | head -1 || echo "0")
    if [[ "$GPU_COUNT" -eq "$EXPECTED_GPU_COUNT" ]]; then
        log_result "GPU数量" "PASS" "检测到 ${GPU_COUNT}/${EXPECTED_GPU_COUNT} 张 GPU"
    else
        log_result "GPU数量" "FAIL" "检测到 ${GPU_COUNT} 张，预期 ${EXPECTED_GPU_COUNT}"
    fi

    # 列出每张卡的 UUID 和型号
    nvidia-smi --query-gpu=index,name,uuid,driver_version --format=csv,noheader | while read -r line; do
        echo "  $line" | tee -a "$REPORT_FILE"
    done
fi
echo ""

# ========== 2. ECC 错误检查 ==========
# 注意：nvidia-smi --query-gpu=ecc.errors.* 字段在 R550+ 驱动分支稳定输出；
# R470 等旧分支可能需要改用 nvidia-smi -q -d ECC 解析文本输出
separator "2. ECC 错误检查"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total,ecc.errors.corrected.aggregate.total,ecc.errors.uncorrected.aggregate.total --format=csv,noheader | while IFS=',' read -r idx sbe_v dbe_v sbe_a dbe_a; do
        idx=$(echo "$idx" | xargs)
        sbe_v=$(echo "$sbe_v" | xargs)
        dbe_v=$(echo "$dbe_v" | xargs)
        sbe_a=$(echo "$sbe_a" | xargs)
        dbe_a=$(echo "$dbe_a" | xargs)

        if [[ "$dbe_v" -gt "$MAX_ECC_DBE" ]] 2>/dev/null; then
            log_result "GPU${idx} volatile DBE" "FAIL" "volatile DBE=${dbe_v}，需立即隔离"
        elif [[ "$sbe_v" -gt 100 ]] 2>/dev/null; then
            log_result "GPU${idx} volatile SBE" "WARN" "volatile SBE=${sbe_v}，持续增长需关注"
        else
            log_result "GPU${idx} ECC" "PASS" "volatile SBE=${sbe_v}, DBE=${dbe_v}; aggregate SBE=${sbe_a}, DBE=${dbe_a}"
        fi
    done
fi
echo ""

# ========== 3. XID 错误检查（近 ${XID_LOOKBACK_HOURS}h）==========
# dmesg -T 格式依赖 systemd 时间戳（kernel 5.x+），XID 日志格式随驱动分支略有差异
# XID 92/94/95 自 R418/R465+ 引入；XID 119/120 为 GSP 相关，自 R515+ 引入
separator "3. XID 错误检查（近 ${XID_LOOKBACK_HOURS}h）"
SINCE_TIME=$(date -d "-${XID_LOOKBACK_HOURS} hours" '+%b %d %H:%M' 2>/dev/null || date -v-${XID_LOOKBACK_HOURS}H '+%b %d %H:%M')
XID_LINES=$(dmesg -T 2>/dev/null | grep 'NVRM.*Xid' | tail -50 || echo "")
if [[ -z "$XID_LINES" ]]; then
    log_result "XID检查" "PASS" "近 ${XID_LOOKBACK_HOURS}h 无 XID 错误"
else
    # 统计各 XID 出现次数
    echo "$XID_LINES" | grep -oP 'Xid \K[0-9]+' | sort | uniq -c | sort -rn | while read -r count xid; do
        case "$xid" in
            79|95|48|63|64|92|94)
                log_result "XID ${xid}" "FAIL" "硬件类错误，出现 ${count} 次，需立即处理"
                ;;
            31|32|43|45|68|69|74)
                log_result "XID ${xid}" "WARN" "性能/可靠性类，出现 ${count} 次"
                ;;
            *)
                log_result "XID ${xid}" "WARN" "出现 ${count} 次，请查阅 XID 手册"
                ;;
        esac
    done
fi
echo ""

# ========== 4. GPU 温度 ==========
separator "4. GPU 温度检查"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader | while IFS=',' read -r idx temp; do
        idx=$(echo "$idx" | xargs)
        temp=$(echo "$temp" | xargs)
        if [[ "$temp" -ge "$MAX_TEMP" ]] 2>/dev/null; then
            log_result "GPU${idx} 温度" "FAIL" "${temp}°C 超过阈值 ${MAX_TEMP}°C"
        elif [[ "$temp" -ge $((MAX_TEMP - 5)) ]] 2>/dev/null; then
            log_result "GPU${idx} 温度" "WARN" "${temp}°C，接近阈值 ${MAX_TEMP}°C"
        else
            log_result "GPU${idx} 温度" "PASS" "${temp}°C"
        fi
    done
fi
echo ""

# ========== 5. GPU 功耗 ==========
separator "5. GPU 功耗检查"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,power.draw,power.limit --format=csv,noheader | while IFS=',' read -r idx draw limit; do
        idx=$(echo "$idx" | xargs)
        draw=$(echo "$draw" | xargs | sed 's/ W//')
        limit=$(echo "$limit" | xargs | sed 's/ W//')
        if [[ -n "$draw" && -n "$limit" ]]; then
            pct=$(awk "BEGIN {printf \"%.0f\", ($draw/$limit)*100}")
            log_result "GPU${idx} 功耗" "PASS" "${draw}W / ${limit}W (${pct}%)"
        fi
    done
fi
echo ""

# ========== 6. DCGM Level-1 诊断 ==========
# dcgmi diag -r 1: DCGM 3.3+ 支持 Level-1 快速诊断；DCGM 4.x 增加了更多子测试
# dcgmi 需 dcgm.service 运行中（systemctl status dcgm）
separator "6. DCGM Level-1 诊断"
if command -v dcgmi &>/dev/null; then
    DCGM_OUTPUT=$(dcgmi diag -r 1 2>&1)
    if echo "$DCGM_OUTPUT" | grep -qi "fail\|error"; then
        log_result "dcgmi diag -r 1" "FAIL" "存在失败项，详见输出"
        echo "$DCGM_OUTPUT" | tail -30 | tee -a "$REPORT_FILE"
    else
        log_result "dcgmi diag -r 1" "PASS" "全部通过"
    fi
else
    log_result "dcgmi" "WARN" "dcgmi 未安装，跳过 DCGM 诊断"
fi
echo ""

# ========== 7. IB 端口状态 ==========
separator "7. InfiniBand 端口状态"
if command -v ibstat &>/dev/null; then
    ibstat 2>/dev/null | grep -E "CA '|Port " | while read -r line; do
        echo "  $line" | tee -a "$REPORT_FILE"
    done
    # 检查是否有非 Active 端口
    INACTIVE=$(ibstat 2>/dev/null | grep "State:" | grep -v "Active" || echo "")
    if [[ -z "$INACTIVE" ]]; then
        log_result "IB端口状态" "PASS" "所有端口 Active"
    else
        log_result "IB端口状态" "FAIL" "存在非 Active 端口: $INACTIVE"
    fi
else
    log_result "ibstat" "WARN" "ibstat 未安装，跳过 IB 检查"
fi
echo ""

# ========== 8. 内核日志关键字 ==========
separator "8. 内核日志关键字检查"
DMESG_ERRORS=$(dmesg -T 2>/dev/null | tail -5000 | grep -iE 'error|fail|mce|edac|hardware error' | grep -iv 'nvidia\|nfs\|drm' | tail -20 || echo "")
if [[ -z "$DMESG_ERRORS" ]]; then
    log_result "内核日志" "PASS" "无异常关键字"
else
    log_result "内核日志" "WARN" "发现异常关键字，请人工确认"
    echo "$DMESG_ERRORS" | tee -a "$REPORT_FILE"
fi
echo ""

# ========== 9. 磁盘空间 ==========
separator "9. 磁盘空间检查"
df -h --output=target,pcent,avail 2>/dev/null | tail -n +2 | while read -r mount pct avail; do
    pct_num=${pct%%%}
    if [[ "$pct_num" -ge "$DISK_USAGE_WARN" ]] 2>/dev/null; then
        log_result "磁盘 $mount" "WARN" "使用率 ${pct}，剩余 ${avail}"
    else
        log_result "磁盘 $mount" "PASS" "使用率 ${pct}，剩余 ${avail}"
    fi
done
echo ""

# ========== 10. inode 使用率 ==========
separator "10. inode 使用率检查"
df -i --output=target,ipcent 2>/dev/null | tail -n +2 | while read -r mount pct; do
    pct_num=${pct%%%}
    if [[ "$pct_num" -ge "$INODE_USAGE_WARN" ]] 2>/dev/null; then
        log_result "inode $mount" "WARN" "inode 使用率 ${pct}"
    else
        log_result "inode $mount" "PASS" "inode 使用率 ${pct}"
    fi
done
echo ""

# ========== 11. 时钟偏移 ==========
separator "11. 时钟偏移检查"
if command -v chronyc &>/dev/null; then
    CHRONY_OUT=$(chronyc tracking 2>/dev/null | grep 'System time' || echo "")
    if [[ -n "$CHRONY_OUT" ]]; then
        OFFSET_NS=$(echo "$CHRONY_OUT" | awk '{print $4}')
        OFFSET_MS=$(awk "BEGIN {printf \"%.3f\", $OFFSET_NS * 1000}" 2>/dev/null || echo "unknown")
        OFFSET_ABS=$(awk "BEGIN {v=$OFFSET_NS*1000; if(v<0)v=-v; printf \"%.3f\",v}" 2>/dev/null || echo "999")
        if awk "BEGIN {exit !($OFFSET_ABS > $MAX_CLOCK_OFFSET_MS)}"; then
            log_result "时钟偏移" "FAIL" "偏移 ${OFFSET_MS}ms，超过阈值 ${MAX_CLOCK_OFFSET_MS}ms"
        else
            log_result "时钟偏移" "PASS" "偏移 ${OFFSET_MS}ms"
        fi
    fi
else
    log_result "chronyc" "WARN" "chronyc 未安装，跳过时钟检查"
fi
echo ""

# ========== 12. NVLink 状态 ==========
separator "12. NVLink 状态"
if command -v nvidia-smi &>/dev/null; then
    NVLINK_OUT=$(nvidia-smi nvlink --status 2>/dev/null || echo "")
    if [[ -n "$NVLINK_OUT" ]]; then
        INACTIVE_LINKS=$(echo "$NVLINK_OUT" | grep -c "inactive" || echo "0")
        if [[ "$INACTIVE_LINKS" -gt 0 ]]; then
            log_result "NVLink" "FAIL" "${INACTIVE_LINKS} 条链路 inactive"
        else
            log_result "NVLink" "PASS" "所有链路 Active"
        fi
    else
        log_result "NVLink" "WARN" "无法读取 NVLink 状态"
    fi
fi
echo ""

# ========== 汇总 ==========
separator "巡检汇总"
echo -e "PASS: ${GREEN}${PASS_COUNT}${NC}  WARN: ${YELLOW}${WARN_COUNT}${NC}  FAIL: ${RED}${FAIL_COUNT}${NC}" | tee -a "$REPORT_FILE"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "" | tee -a "$REPORT_FILE"
    echo "⚠️  存在 FAIL 项，请立即处理！" | tee -a "$REPORT_FILE"
    EXIT_CODE=2
elif [[ "$WARN_COUNT" -gt 0 ]]; then
    EXIT_CODE=1
else
    EXIT_CODE=0
fi

echo "报告已保存至: $REPORT_FILE" | tee -a "$REPORT_FILE"
exit ${EXIT_CODE}
```

### 1.3 巡检结果自动上报思路

```bash
# 方案一：cron + webhook（推荐，最简）
# /etc/cron.d/gpu-inspect
0 8 * * * root /opt/scripts/gpu_node_inspect.sh /var/log/gpu_inspect && \
    curl -sf -X POST "https://alertmanager.example.com/api/v1/alerts" \
      -H 'Content-Type: application/json' \
      -d "{\"labels\":{\"alertname\":\"GPUInspectDone\",\"host\":\"$(hostname)\",\"status\":\"ok\"}}" || \
    curl -sf -X POST "https://alertmanager.example.com/api/v1/alerts" \
      -d "{\"labels\":{\"alertname\":\"GPUInspectFail\",\"host\":\"$(hostname)\",\"status\":\"fail\"}}"

# 方案二：Prometheus node_exporter textfile collector
# 脚本末尾追加：
cat > /var/lib/node_exporter/gpu_inspect.prom <<EOF
gpu_inspect_pass_count{host="$HOSTNAME"} $PASS_COUNT
gpu_inspect_warn_count{host="$HOSTNAME"} $WARN_COUNT
gpu_inspect_fail_count{host="$HOSTNAME"} $FAIL_COUNT
gpu_inspect_timestamp{host="$HOSTNAME"} $(date +%s)
EOF

# 方案三：结果写入 Kafka/ES，Grafana 做看板
# 脚本末尾 JSON 化输出，由 filebeat/fluentd 采集
```

---

## 2. 节点健康池管理

### 2.1 三池设计

![节点健康池模型：healthy、suspect、maintenance 三池及流转条件](/images/gpu-07-health-pools.svg)

### 2.2 K8s Label + Taint 方案

**Label 键值设计：**

| Label Key | 值域 | 说明 |
|---|---|---|
| `gpu-cluster.io/health-status` | `healthy` / `suspect` / `maintenance` | 健康池状态 |
| `gpu-cluster.io/last-inspect` | `2024-01-15T08:00:00Z` | 最后巡检时间 |
| `gpu-cluster.io/isolate-reason` | `xid79` / `ecc_dbe` / `diag_fail` / `nccl_slow` | 隔离原因 |
| `gpu-cluster.io/gpu-model` | `A100-80G-SXM` / `H100-80G-SXM` | GPU 型号 |
| `gpu-cluster.io/node-tier` | `tier1` / `tier2` | 节点分级（按网络拓扑） |

**Taint 设计：**

```yaml
# suspect 池：不允许普通训练任务调度，但允许诊断任务
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-042
  labels:
    gpu-cluster.io/health-status: suspect
    gpu-cluster.io/isolate-reason: xid79
spec:
  taints:
    - key: gpu-cluster.io/suspect
      value: "true"
      effect: NoSchedule

---
# maintenance 池：完全隔离
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-042
  labels:
    gpu-cluster.io/health-status: maintenance
    gpu-cluster.io/isolate-reason: ecc_dbe
spec:
  taints:
    - key: gpu-cluster.io/maintenance
      value: "true"
      effect: NoExecute    # 驱逐存量 Pod
```

**训练任务的 toleration 设计（仅 healthy 节点可调度）：**

```yaml
# 训练 Pod 配置：只调度到 healthy 节点
spec:
  nodeSelector:
    gpu-cluster.io/health-status: healthy
  tolerations: []   # 不容忍任何 taint，自然被 suspect/maintenance 节点排斥
```

### 2.3 自动隔离触发条件表

| 触发条件 | 判定方法 | 目标池 | 动作 |
|---|---|---|---|
| XID 79（GPU 掉卡） | `dmesg -T \| grep 'Xid 79'` 单次触发 | maintenance | 立即 cordon + drain |
| XID 95（ECC DBE） | `dmesg -T \| grep 'Xid 95'` 单次触发 | maintenance | 立即 cordon + drain |
| ECC volatile DBE > 0 | `nvidia-smi -q -d ECC` DBE > 0 | maintenance | 立即 cordon + drain |
| ECC aggregate DBE 持续增长 | 两次巡检 aggregate DBE 增量 > 0 | suspect | 标记观察，24h 复测 |
| dcgmi diag -r 1 失败 | `dcgmi diag -r 1` 非全 PASS | suspect | 标记观察，重跑一次确认 |
| NCCL 带宽低于基线 10% | 定期 nccl-tests 与基线对比 | suspect | 标记观察，复测确认 |
| XID 74/68/69（NVLink 错误） | `dmesg` 中近 1h 出现 ≥ 2 次 | suspect | 标记观察 |
| 温度持续 > 85°C 10 分钟 | Prometheus 告警 | suspect | 检查散热，标记观察 |
| ROW_REMAPPER pending=yes | `nvidia-smi -q -d ROW_REMAPPER` | suspect | 计划重启后复测 |

> **版本说明**：`nvidia-smi -q -d ROW_REMAPPER` 字段自 R515+ 驱动分支可用（Hopper/Blackwell），旧驱动（R470 及以前）对应字段为 `Retired_Pages`（Retired Pages SBE/DBE）。`dcgmi diag -r 1` 需 DCGM 3.3+，4.x 扩展了子测试项。

### 2.4 隔离与回归自动化流程

```bash
#!/usr/bin/env bash
#===============================================================================
# node_isolate.sh — 节点隔离流程
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / DCGM 3.3+ / K8s 1.31 (kubectl)
# 用法：sudo bash node_isolate.sh <node-name> <reason> [suspect|maintenance]
# 版本说明：
#   - kubectl drain --delete-emptydir-data: K8s 1.20+ 替代旧 --delete-local-data
#   - kubectl drain --force: 允许驱逐非 DaemonSet 管理的 Pod，K8s 1.31 行为稳定
#   - Alertmanager v2 silences API: 需 Alertmanager 0.27+（本基线版本）
#===============================================================================

set -euo pipefail

NODE_NAME="${1:?用法: node_isolate.sh <node-name> <reason> [suspect|maintenance]}"
REASON="${2:?请指定隔离原因，如 xid79, ecc_dbe, diag_fail}"
TARGET_POOL="${3:-maintenance}"

echo "=== 开始隔离节点: $NODE_NAME ==="
echo "原因: $REASON | 目标池: $TARGET_POOL"

# 1. Cordon：阻止新调度
echo "[1/4] Cordon 节点..."
kubectl cordon "$NODE_NAME"

# 2. Drain：驱逐存量 Pod
echo "[2/4] Drain 节点（等待 300s，之后强制删除）..."
kubectl drain "$NODE_NAME" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=60 \
    --timeout=300s \
    --force || {
    echo "Drain 超时，尝试强制驱逐残留 Pod..."
    kubectl get pods -A -o wide --field-selector spec.nodeName="$NODE_NAME" | \
        awk 'NR>1 {print $1, $2}' | while read -r ns pod; do
        echo "  强制删除 $ns/$pod"
        kubectl delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
    done
}

# 3. 打 Label + Taint
echo "[3/4] 设置标签和污点..."
kubectl label node "$NODE_NAME" \
    "gpu-cluster.io/health-status=$TARGET_POOL" \
    "gpu-cluster.io/isolate-reason=$REASON" \
    "gpu-cluster.io/last-inspect=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --overwrite

if [[ "$TARGET_POOL" == "maintenance" ]]; then
    kubectl taint nodes "$NODE_NAME" \
        "gpu-cluster.io/maintenance=true:NoExecute" --overwrite
elif [[ "$TARGET_POOL" == "suspect" ]]; then
    kubectl taint nodes "$NODE_NAME" \
        "gpu-cluster.io/suspect=true:NoSchedule" --overwrite
fi

# 4. 静默告警（避免隔离期间告警风暴）
echo "[4/4] 静 Prometheus 告警..."
curl -sf -X POST "http://alertmanager:9093/api/v2/silences" \
  -H 'Content-Type: application/json' \
  -d "{
    \"matchers\": [{\"name\":\"instance\",\"value\":\"$NODE_NAME\",\"isRegex\":false}],
    \"startsAt\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
    \"endsAt\": \"$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+24H +%Y-%m-%dT%H:%M:%SZ)\",
    \"createdBy\": \"node_isolate_script\",
    \"comment\": \"Node isolated: $REASON\"
  }" || echo "告警静默请求失败，请手动操作"

echo "=== 节点 $NODE_NAME 已隔离至 $TARGET_POOL 池 ==="
```

```bash
#!/usr/bin/env bash
#===============================================================================
# node_return.sh — 节点回归流程
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / DCGM 3.3+ / K8s 1.31 (kubectl)
# 用法：sudo bash node_return.sh <node-name>
# 版本说明：
#   - dcgmi diag -r 2: Level-2 含 GPU memory/performance 子测试，DCGM 3.3+ 支持
#   - nccl-tests: 2.26.x（CUDA 12.8）/ 2.21.x（CUDA 12.4），编译需与 NCCL 版本匹配
#   - kubectl taint 末尾 "-" 表示移除 taint，K8s 1.31 语法稳定
#===============================================================================

set -euo pipefail

NODE_NAME="${1:?用法: node_return.sh <node-name>}"

echo "=== 开始节点回归流程: $NODE_NAME ==="

# 1. 运行完整诊断
echo "[1/5] 运行 dcgmi diag -r 2..."
if ! dcgmi diag -r 2; then
    echo "FAIL: dcgmi 诊断未通过，回归中止"
    exit 1
fi

# 2. 运行 NCCL 基准测试（单节点内）
echo "[2/5] 运行节点内 NCCL all_reduce 基准..."
NCCL_RESULT=$(timeout 120 /opt/nccl-tests/build/all_reduce_perf -b 1G -e 8G -f 2 -g 8 2>&1)
NCCL_BW=$(echo "$NCCL_RESULT" | grep 'Avg' | awk '{print $NF}')
NCCL_BASELINE=280.0  # GB/s，以 A100 NVLink 基线为参考
echo "  当前带宽: ${NCCL_BW} GB/s，基线: ${NCCL_BASELINE} GB/s"
if awk "BEGIN {exit !($NCCL_BW < $NCCL_BASELINE * 0.95)}"; then
    echo "FAIL: NCCL 带宽低于基线 95%，回归中止"
    exit 1
fi

# 3. ECC 计数确认
echo "[3/5] ECC 计数确认..."
DBE_NOW=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits | head -1)
if [[ "$DBE_NOW" -gt 0 ]] 2>/dev/null; then
    echo "FAIL: volatile DBE=${DBE_NOW}，不可回归"
    exit 1
fi

# 4. 移除 Taint 和隔离 Label
echo "[4/5] 移除隔离标记..."
kubectl taint nodes "$NODE_NAME" \
    "gpu-cluster.io/maintenance=true:NoExecute-" \
    "gpu-cluster.io/suspect=true:NoSchedule-" 2>/dev/null || true
kubectl label node "$NODE_NAME" \
    "gpu-cluster.io/health-status=healthy" \
    "gpu-cluster.io/isolate-reason-" \
    "gpu-cluster.io/last-inspect=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --overwrite

# 5. Uncordon
echo "[5/5] Uncordon 节点..."
kubectl uncordon "$NODE_NAME"

echo "=== 节点 $NODE_NAME 已回归 healthy 池 ==="
```

### 2.5 节点维修上下线完整 SOP

```text
SOP-001：节点维修上线
────────────────────────────────────────────────────
1. [准备] 确认维修工单号、备件到位
2. [隔离] 执行 node_isolate.sh <node> hardware_repair maintenance
3. [断电] 确认 Pod 全部驱逐后，通知机房断电
4. [维修] 硬件更换（卡/内存/盘/风扇）
5. [上电] 开机，等待 K8s node Ready
6. [验证] 
   a. nvidia-smi 确认卡数正确
   b. dcgmi diag -r 2 全 PASS
   c. 节点内 nccl-tests all_reduce 达标
   d. ibstat 确认 IB Active
   e. 持续 burn-in 运行 30min（gpu-burn 或 stress-ng）
7. [回归] 执行 node_return.sh <node>
8. [观察] 回归后 2h 内密切关注该节点监控
9. [关闭] 关闭工单，更新备件台账
```

---

## 3. 慢节点（Straggler）检测

### 3.1 为什么千卡集群 1 个慢节点拖垮全作业

**同步点原理：**

分布式训练使用 AllReduce/AllGather 等集合通信，所有 rank 必须到达同一个 barrier 才能继续。训练框架（如 Megatron-LM、DeepSpeed）在每个 step 末尾执行梯度同步，这是一个全局 barrier。

```text
Step N 耗时 = max(rank_0_step_time, rank_1_step_time, ..., rank_1023_step_time)
```

- 如果 rank_500 所在节点因温度降频慢了 2 倍，整个 1024 卡集群都要等它
- 影响被放大：1 个慢节点 → 1024 张卡全部空等 → 整体吞吐下降接近 50%
- 这就是"木桶效应"——最慢的 rank 决定全局速度

### 3.2 四种检测方法

**方法一：rank step time 打点对比**

```python
# 在训练代码中插入（以 PyTorch 为例）
import time
import torch.distributed as dist
import logging

logger = logging.getLogger("straggler_detect")

def measure_step_time(step_fn, *args, **kwargs):
    """在每个 step 前后打点，上报 rank 耗时"""
    if dist.is_initialized():
        sync_tensor = torch.cuda.Event(enable_timing=True)
        sync_tensor.record()
    
    t0 = time.perf_counter()
    result = step_fn(*args, **kwargs)
    torch.cuda.synchronize()
    t1 = time.perf_counter()
    
    step_time = t1 - t0
    rank = dist.get_rank() if dist.is_initialized() else 0
    
    # 通过 wandb / tensorboard / 文件上报
    logger.info(f"[STRAGGLER] rank={rank} step_time={step_time:.4f}s")
    
    # 在 rank 0 做聚合对比
    if rank == 0:
        all_times = [torch.tensor(0.0) for _ in range(dist.get_world_size())]
        dist.gather(torch.tensor(step_time), all_times, dst=0)
        max_time = max(t.item() for t in all_times)
        min_time = min(t.item() for t in all_times)
        if max_time > min_time * 1.5:
            slow_ranks = [i for i, t in enumerate(all_times) if t.item() > min_time * 1.2]
            logger.warning(f"[STRAGGLER] 检测到慢 rank: {slow_ranks}, max/min={max_time/min_time:.2f}")
    
    return result
```

**方法二：DCGM SM_ACTIVE / 温度离群 PromQL**

```promql
# 找出 SM_ACTIVE 明显低于集群中位数的节点（过去 5 分钟）
# 低于集群 P25 减 2 倍 IQR 的视为慢节点
(
  avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])
  < ignoring(job) group_left
  (
    quantile(0.25, avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m]))
    - 2 * (
        quantile(0.75, avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m]))
        - quantile(0.25, avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m]))
      )
  )
)

# 温度离群检测：某节点温度显著高于其他节点
(
  DCGM_FI_DEV_GPU_TEMP
  > on() group_left()
  (
    avg(DCGM_FI_DEV_GPU_TEMP) + 2 * stddev(DCGM_FI_DEV_GPU_TEMP)
  )
)
```

**方法三：定期 nccl-tests 轮测对比基线**

```bash
#!/usr/bin/env bash
#===============================================================================
# straggler_nccl_scan.sh — 集群级 NCCL 轮测扫描
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / K8s 1.31 (kubectl)
# 在 idle 节点上轮询运行 all_reduce_perf，结果写入 Prometheus textfile
# 建议 cron: 每 2 小时执行一次
# 版本说明：
#   - kubectl debug node: K8s 1.25+ GA，1.31 行为稳定
#   - nccl-tests: 编译需匹配 NCCL 版本（2.26.x for CUDA 12.8 / 2.21.x for CUDA 12.4）
#   - DCGM_FI_PROF_SM_ACTIVE: dcgm-exporter 3.3+ / 4.x 均支持该指标
#===============================================================================

set -euo pipefail

NCCL_TEST_BIN="/opt/nccl-tests/build/all_reduce_perf"
BASELINE_BW_GBPS=280.0    # A100 NVLink all_reduce 基线 (GB/s)，8 卡 8G 消息
THRESHOLD_PCT=90          # 低于基线 90% 视为慢节点
RESULT_DIR="/var/lib/node_exporter"
RESULT_FILE="${RESULT_DIR}/nccl_benchmark.prom"

mkdir -p "$RESULT_DIR"

# 获取本机所有 GPU 节点的 hostname
NODES=$(kubectl get nodes -l gpu-cluster.io/health-status=healthy \
    -o jsonpath='{.items[*].metadata.name}')

echo "# HELP nccl_all_reduce_bw_gbps NCCL all_reduce bandwidth in GB/s" > "$RESULT_FILE"
echo "# TYPE nccl_all_reduce_bw_gbps gauge" >> "$RESULT_FILE"

for node in $NODES; do
    echo "测试节点: $node"
    # 在目标节点上通过 debug pod 运行 nccl-tests
    RESULT=$(kubectl debug node/"$node" -it --image=nccl-tests:2.26-cuda12.8 -- \
        timeout 120 "$NCCL_TEST_BIN" -b 1G -e 8G -f 2 -g 8 2>&1 || echo "FAIL")
    
    if echo "$RESULT" | grep -q "Avg"; then
        BW=$(echo "$RESULT" | grep "8589934592" | awk '{print $(NF-1)}')  # 8G 消息的带宽
        echo "nccl_all_reduce_bw_gbps{node=\"$node\"} $BW" >> "$RESULT_FILE"
        
        # 判断是否低于基线
        BELOW=$(awk "BEGIN {print ($BW < $BASELINE_BW_GBPS * $THRESHOLD_PCT / 100) ? 1 : 0}")
        if [[ "$BELOW" -eq 1 ]]; then
            echo "  ⚠️  节点 $node 带宽 ${BW} GB/s 低于阈值！"
        fi
    else
        echo "nccl_all_reduce_bw_gbps{node=\"$node\"} 0" >> "$RESULT_FILE"
        echo "  ❌ 节点 $node 测试失败"
    fi
done

echo "扫描完成，结果已写入 $RESULT_FILE"
```

**方法四：GEMM 基准测试**

```bash
#!/usr/bin/env bash
# gemm_benchmark.sh — 单卡 GEMM 基准，检测算力异常
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / CUDA 12.x
# 使用 CUDA samples 中的 matrixMul 或自定义 GEMM
# 版本说明：A100 FP16 Tensor Core 基线 312 TFLOPS（CUDA 12.x + cuBLAS 12.x）；
#   H100 基线约 990 TFLOPS FP16（需 CUDA 12.x + R535+ 驱动）

GEMM_BIN="/opt/gpu-tools/gemm_bench"
BASELINE_TFLOPS=312.0   # A100 FP16 Tensor Core 基线 TFLOPS
THRESHOLD_PCT=90

for gpu_idx in $(seq 0 7); do
    TFLOPS=$($GEMM_BIN --gpu $gpu_idx --dtype fp16 --size 8192 2>/dev/null | grep "TFLOPS" | awk '{print $2}')
    echo "GPU${gpu_idx}: ${TFLOPS} TFLOPS"
    
    BELOW=$(awk "BEGIN {print ($TFLOPS < $BASELINE_TFLOPS * $THRESHOLD_PCT / 100) ? 1 : 0}")
    if [[ "$BELOW" -eq 1 ]]; then
        echo "  ⚠️  GPU${gpu_idx} 算力低于基线 90%！可能降频或硬件异常"
        nvidia-smi --query-gpu=clocks.current.sm,clocks.current.memory,temperature.gpu,power.draw \
            --format=csv -i "$gpu_idx"
    fi
done
```

### 3.3 慢节点处置流程

```text
1. 确认慢节点 → 通过上述四种方法之一或组合定位
2. 标记 suspect → kubectl label node <node> gpu-cluster.io/health-status=suspect
3. 等待当前 step 结束 → 不立即 drain（避免浪费已完成计算）
4. 当前 step/job 完成后 → drain 并隔离
5. 诊断 → 
   a. 检查温度/功耗/时钟频率（是否降频）
   b. 运行 dcgmi diag -r 2
   c. 运行 GEMM/NCCL 基准
6. 根因处置 →
   - 温度降频：检查散热/重新涂抹硅脂
   - 硬件老化：RMA 换卡
   - PCIe/NVLink 异常：检查物理连接
7. 回归验证 → 通过 node_return.sh 回归
```

---

## 4. 变更管理

### 4.1 变更三板斧

| 板斧 | 说明 | 具体要求 |
|---|---|---|
| **可灰度** | 变更必须分批执行 | 金丝雀 1 台 → 小批 10% → 中批 50% → 全量 |
| **可监控** | 变更全程有观测 | 关键指标大盘盯守，异常自动暂停 |
| **可回滚** | 变更必须能撤销 | 回滚方案在变更单里写清楚，回滚命令提前验证 |

### 4.2 变更单模板

```markdown
## 变更单 CHG-2024-XXXX

| 字段 | 内容 |
|---|---|
| 变更时间 | 2024-01-15 10:00 ~ 12:00（避开训练任务运行时段） |
| 变更内容 | NVIDIA 驱动从 535.129.03 升级至 535.154.05（R535 分支示例；当前基线为 R550/R570） |
| 影响面 | 全部 128 个 GPU 节点，1024 张 A100 |
| 灰度方案 | 金丝雀 2 台 → 第 1 批 12 台 → 第 2 批 64 台 → 第 3 批 50 台 |
| 回滚方案 | 回退驱动至 535.129.03（详见回滚命令） |
| 验证方法 | 每批完成后：nvidia-smi + dcgmi diag -r 1 + nccl-tests all_reduce |
| 执行人 | 张三（工号 A12345） |
| 审批人 | 李四（运维经理） |
| 通知范围 | 算法团队（邮件 + 群通知，提前 24h） |
| 前置条件 | 所有训练任务已 checkpoint 并暂停；无 P1/P2 告警 |
```

### 4.3 驱动升级分批滚动完整流程

```bash
#!/usr/bin/env bash
#===============================================================================
# driver_rolling_upgrade.sh — 驱动分批滚动升级
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / K8s 1.31 (kubectl)
# 示例版本：535.129.03 → 535.154.05（R535 分支示例；生产基线以 00-版本基线.md 为准）
# 前置条件：所有节点已 drain，无运行中的训练任务
# 版本说明：
#   - nvidia-uninstall --silent: 各驱动分支均支持
#   - NVIDIA-Linux-x86_64-*.run: 仅 x86_64 架构；ARM/aarch64 需对应架构包
#   - kubectl drain --timeout=120s: K8s 1.31 支持；超时后 --force 驱逐残留
#   - Fabric Manager 版本必须与驱动版本完全一致，升级后需同时重启
#===============================================================================

set -euo pipefail

# ---------- 配置 ----------
OLD_DRIVER="535.129.03"
NEW_DRIVER="535.154.05"
DRIVER_RUN="/opt/drivers/NVIDIA-Linux-x86_64-${NEW_DRIVER}.run"
CANARY_NODES=("gpu-node-001" "gpu-node-002")
BATCH1_NODES=()  # 第 1 批 10%，由脚本自动填充
BATCH2_NODES=()  # 第 2 批 50%
BATCH3_NODES=()  # 第 3 批 剩余
BATCH_INTERVAL=300  # 批次间观察期（秒）

# 自动分批
ALL_NODES=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | sort)
TOTAL=${#ALL_NODES[@]}
BATCH1_SIZE=$((TOTAL / 10))
BATCH2_SIZE=$((TOTAL / 2))

echo "总节点数: $TOTAL | 金丝雀: ${#CANARY_NODES[@]} | 批次1: $BATCH1_SIZE | 批次2: $BATCH2_SIZE"

# ---------- 升级函数 ----------
upgrade_node() {
    local node=$1
    echo ">>> 升级节点: $node"
    
    # 1. Cordon + Drain
    kubectl cordon "$node"
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=120s --force
    
    # 2. 停止 GPU 相关进程
    ssh "$node" 'sudo systemctl stop dcgm nvidia-fabricmanager nvidia-persistenced'
    
    # 3. 卸载旧驱动
    ssh "$node" 'sudo nvidia-uninstall --silent' || true
    
    # 4. 安装新驱动
    ssh "$node" "sudo sh ${DRIVER_RUN} --silent --no-questions"
    
    # 5. 启动服务
    # 注意：Fabric Manager 版本必须与驱动完全一致（如 570.124.04 = FM 570.124.04）
    ssh "$node" 'sudo systemctl start nvidia-persistenced nvidia-fabricmanager dcgm'
    
    # 6. 验证
    ssh "$node" 'nvidia-smi' || { echo "FAIL: $node nvidia-smi 失败"; return 1; }
    ssh "$node" 'dcgmi diag -r 1' || { echo "FAIL: $node dcgmi 诊断失败"; return 1; }
    
    # 7. Uncordon
    kubectl uncordon "$node"
    
    echo "<<< 节点 $node 升级完成"
}

# ---------- 金丝雀阶段 ----------
echo "========== 金丝雀阶段 =========="
for node in "${CANARY_NODES[@]}"; do
    upgrade_node "$node" || {
        echo "金丝雀失败！中止升级，回滚 $node"
        # 回滚命令
        ssh "$node" "sudo nvidia-uninstall --silent && sudo sh /opt/drivers/NVIDIA-Linux-x86_64-${OLD_DRIVER}.run --silent"
        exit 1
    }
done

echo "金丝雀完成，观察 ${BATCH_INTERVAL}s..."
sleep "$BATCH_INTERVAL"

# 金丝雀验证：运行 nccl-tests
for node in "${CANARY_NODES[@]}"; do
    echo "验证金丝雀节点 $node NCCL 性能..."
    kubectl debug node/"$node" -it --image=nccl-tests:2.26-cuda12.8 -- \
        timeout 120 /opt/nccl-tests/build/all_reduce_perf -b 1G -e 8G -f 2 -g 8
done

echo "金丝雀验证通过，继续批量升级？[y/N]"
read -r confirm
[[ "$confirm" == "y" ]] || exit 0

# ---------- 分批执行 ----------
echo "========== 第 1 批（10%）=========="
# ... 按同样逻辑执行 BATCH1/BATCH2/BATCH3
echo "升级完成"
```

### 4.4 变更窗口与训练任务协调要点

| 要点 | 具体做法 |
|---|---|
| 提前通知 | 变更前 24h 邮件 + 群通知，标注影响范围和时间窗口 |
| Checkpoint 对齐 | 确认所有训练任务已完成最近一次 checkpoint（查 wandb/S3 最近写入时间） |
| 任务迁移 | 大任务优先迁移到未升级的节点继续跑（通过 nodeSelector 调整） |
| 窗口选择 | 选择训练任务最少的时段（通常是工作日上午或周末） |
| 灰度观察期 | 金丝雀至少观察 2h（含一个完整的训练 step 周期），确认 loss 曲线无异常 |
| 紧急回滚 | 如果灰度期间出现任何 XID 错误或 NCCL timeout，立即停止并回滚 |

### 4.5 "先查最近变更"原则

**故障排查第一条：过去 72 小时内做过什么变更？**

```bash
# 快速查看最近的 K8s 事件和变更
kubectl get events --all-namespaces --sort-by='.lastTimestamp' | tail -30

# 查看 helm release 历史
helm history <release-name> -n <namespace>

# 查看 GitOps 最近 commit（ArgoCD/Flux）
git log --oneline --since="72 hours ago" -- manifests/

# 查看节点上的驱动/内核变更
rpm -qa --last | head -20   # RHEL/CentOS
dpkg -l | grep -i nvidia    # Debian/Ubuntu
```

---

## 5. RMA 与备件

### 5.1 换卡判据表

| 判据 | 判定命令 | 证据要求 | 紧急度 |
|---|---|---|---|
| ECC DBE 复现 | `nvidia-smi -q -d ECC` volatile DBE > 0 且重启后仍出现 | 两次独立测试结果 | P1 立即换 |
| ROW_REMAPPER failure | `nvidia-smi -q -d ROW_REMAPPER` 显示 `Remapping Failure Occurred: Yes` | 单次确认 | P1 立即换 |
| XID 79 反复出现 | `dmesg -T \| grep 'Xid 79'` 24h 内 ≥ 2 次（同一 GPU） | 含 dmesg 完整日志 | P1 立即换 |
| XID 95 出现 | `dmesg -T \| grep 'Xid 95'` 任意一次 | 含 dmesg 完整日志 | P1 立即换 |
| NVLink 硬件错误 | `nvidia-smi nvlink --errors` 显示 CRC/重传错误持续增长 | 两次快照对比增量 | P2 计划换 |
| dcgmi diag -r 2 持续失败 | `dcgmi diag -r 2` 同一 GPU 连续 2 次 FAIL | dcgmi 完整输出 | P2 计划换 |
| GEMM 算力低于基线 20% | GEMM benchmark 结果 | benchmark 输出 + 温度/功耗数据 | P2 计划换 |
| PCIe 链路降级 | `nvidia-smi -q -d PCIE` 显示 x8 或更低 | lspci -vvv 输出 | P2 计划换 |

> **版本说明**：`nvidia-smi -q -d ROW_REMAPPER` 自 R515+ 可用，R470 及以前使用 `Retired_Pages` 字段判断退役状态。`dcgmi diag -r 2` Level-2 诊断需 DCGM 3.3+。`nvidia-smi nvlink --errors` 输出的 CRC/重传计数器自 R460+ 稳定。

### 5.2 证据采集一键脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# rma_evidence.sh — RMA 证据一键采集
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / DCGM 3.3+ / K8s 1.31
# 用法：sudo bash rma_evidence.sh [gpu_index]
# 输出：/tmp/rma_evidence_<hostname>_<date>.tar.gz
# 工具版本依赖：
#   - nvidia-bug-report.sh: 随驱动安装，R550+ 输出含 GSP 固件版本与 NVLink 4/5 状态
#   - nvidia-smi -q -d ROW_REMAPPER: R515+ 才有 Remapped Rows 字段；R470 及以前用 Retired_Pages
#   - ipmitool: ipmitool 1.8.19+（Ubuntu 22.04 apt: 1.8.18-7；Rocky 9: 1.8.19）
#   - dcgmi diag -r 2: DCGM 3.3+；4.x 扩展了 Level-2 子测试
#   - lspci -vvv: pciutils 3.7+（Ubuntu 22.04: 3.7.0；Rocky 9: 3.7.0）
#===============================================================================

set -euo pipefail

GPU_IDX="${1:-all}"
HOSTNAME=$(hostname)
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
EVIDENCE_DIR="/tmp/rma_evidence_${HOSTNAME}_${TIMESTAMP}"
EVIDENCE_TAR="/tmp/rma_evidence_${HOSTNAME}_${TIMESTAMP}.tar.gz"

mkdir -p "$EVIDENCE_DIR"

echo "=== 采集 RMA 证据 ==="
echo "目标 GPU: $GPU_IDX"
echo "输出目录: $EVIDENCE_DIR"

# 1. nvidia-bug-report
echo "[1/8] nvidia-bug-report.sh..."
nvidia-bug-report.sh 2>/dev/null
cp /var/log/nvidia-bug-report.log.gz "$EVIDENCE_DIR/" 2>/dev/null || true

# 2. nvidia-smi 全量信息
echo "[2/8] nvidia-smi 全量..."
nvidia-smi -q > "$EVIDENCE_DIR/nvidia-smi_full.txt" 2>&1
nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,driver_version,vbios_version \
    --format=csv > "$EVIDENCE_DIR/gpu_inventory.csv" 2>&1

# 3. ECC 详细数据
echo "[3/8] ECC 详细数据..."
nvidia-smi -q -d ECC > "$EVIDENCE_DIR/ecc_detail.txt" 2>&1
nvidia-smi -q -d ROW_REMAPPER > "$EVIDENCE_DIR/row_remapper.txt" 2>&1

# 4. dmesg 相关日志
echo "[4/8] dmesg 日志..."
dmesg -T > "$EVIDENCE_DIR/dmesg_full.txt" 2>&1
dmesg -T | grep -iE 'xid|nvrm|error|fail|aer|gpu|mce|edac' > "$EVIDENCE_DIR/dmesg_errors.txt" 2>&1 || true

# 5. ipmitool SEL 日志
echo "[5/8] IPMI SEL 日志..."
ipmitool sel list > "$EVIDENCE_DIR/ipmi_sel.txt" 2>&1 || echo "ipmitool 不可用" > "$EVIDENCE_DIR/ipmi_sel.txt"
ipmitool sel elist > "$EVIDENCE_DIR/ipmi_sel_extended.txt" 2>&1 || true

# 6. NVLink 状态与错误
echo "[6/8] NVLink 状态..."
nvidia-smi nvlink --status > "$EVIDENCE_DIR/nvlink_status.txt" 2>&1 || true
nvidia-smi nvlink --errors > "$EVIDENCE_DIR/nvlink_errors.txt" 2>&1 || true

# 7. PCIe 信息
echo "[7/8] PCIe 信息..."
lspci -vvv -s $(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader -i 0 2>/dev/null | head -1 | xargs) > "$EVIDENCE_DIR/pcie_detail.txt" 2>&1 || true
nvidia-smi -q -d PCIE > "$EVIDENCE_DIR/pcie_gpu.txt" 2>&1 || true

# 8. DCGM 计数器快照
echo "[8/8] DCGM 计数器快照..."
dcgmi discovery -l > "$EVIDENCE_DIR/dcgm_discovery.txt" 2>&1 || true
dcgmi diag -r 2 > "$EVIDENCE_DIR/dcgm_diag_r2.txt" 2>&1 || true

# 打包
echo "打包证据..."
tar czf "$EVIDENCE_TAR" -C /tmp "$(basename "$EVIDENCE_DIR")"
rm -rf "$EVIDENCE_DIR"

echo "=== 证据已打包: $EVIDENCE_TAR ==="
echo "文件大小: $(du -h "$EVIDENCE_TAR" | awk '{print $1}')"
echo "请将此文件附在 RMA 工单中"
```

### 5.3 RMA 流程 SOP

```text
SOP-002：GPU 卡 RMA 流程
────────────────────────────────────────────────────
1. [判据确认] 对照 5.1 换卡判据表，确认满足换卡条件
2. [证据采集] 运行 rma_evidence.sh，获取证据包
3. [内部审批] 填写 RMA 申请表（节点号/GPU UUID/故障现象/判据证据）
4. [提交工单] 向厂商提交 RMA 工单（附证据包）
   - NVIDIA Enterprise: https://enterprise-support.nvidia.com
   - 服务器 OEM（浪潮/超聚变/宁畅等）: 各厂商工单系统
5. [隔离节点] 执行 node_isolate.sh <node> rma_pending maintenance
6. [等待备件] 跟踪工单进度（通常 3~7 工作日）
7. [换卡执行] 
   a. 断电，物理更换 GPU 卡
   b. 上电，确认 nvidia-smi 识别到新卡
   c. 记录新卡 UUID 和序列号
8. [验证测试]
   a. dcgmi diag -r 2 全 PASS
   b. gpu-burn 30 分钟无错误
   c. nccl-tests all_reduce 达标
   d. ECC 计数全部归零（新卡）
9. [回归] 执行 node_return.sh <node>
10. [更新台账] 更新资产管理系统（新卡序列号/UUID/安装日期）
11. [关闭工单] 确认旧卡已寄回（如需），关闭 RMA 工单
```

### 5.4 备件备货建议表

| 备件类别 | 建议备货比例 | 100 节点集群参考数量 | 存放位置 | 备注 |
|---|---|---|---|---|
| GPU 卡（同型号） | 5%~8% | 40~65 张（按 1024 卡计） | 本地机房备件柜 | 优先 SXM 模组，兼容 PCIe 卡 |
| QSFP56 光模块 | 10%~15% | 50~75 个（按 500 链路计） | 本地机房 | 最易损耗件，需多备 |
| DAC/AOC 线缆 | 5%~10% | 25~50 条 | 本地机房 | 注意长度规格匹配 |
| 服务器内存条 | 3%~5% | 10~20 条 | 本地机房 | 匹配规格（DDR5-4800 ECC） |
| NVMe SSD | 3%~5% | 10~20 块 | 本地机房 | 匹配接口（U.2/E1.S） |
| 整机备机 | 2%~3% | 2~3 台 | 本地机房 | 完整配置，可整机替换 |
| 散热模组/风扇 | 5% | 若干 | 本地机房 | 与服务器型号匹配 |
| 电源线/PDU 模块 | 5% | 若干 | 本地机房 | 冗余电源场景 |

---

## 6. 大任务护航 SOP

### 6.1 任务前：全面健康检查

```bash
#!/usr/bin/env bash
#===============================================================================
# pre_job_check.sh — 大任务启动前全量健康检查
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / K8s 1.31 (kubectl)
# 用法：bash pre_job_check.sh <node_list_file>
# 输入：节点清单文件（每行一个 hostname）
# 版本说明：
#   - ibstat: 来自 infiniband-diags 或 DOCA-OFED 24.10；apt 源版本偏旧，建议 OFED 版
#   - nvidia-smi --query-gpu=ecc.errors.*: R550+ 稳定输出
#===============================================================================

set -euo pipefail

NODE_LIST="${1:?请提供节点清单文件路径}"
FAIL_COUNT=0

echo "=========================================="
echo "大任务启动前健康检查"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

while IFS= read -r node; do
    [[ -z "$node" || "$node" == \#* ]] && continue
    echo ""
    echo ">>> 检查节点: $node"
    
    # 1. GPU 在位
    GPU_COUNT=$(ssh "$node" 'nvidia-smi --query-gpu=count --format=csv,noheader 2>/dev/null | head -1' || echo "0")
    if [[ "$GPU_COUNT" -ne 8 ]]; then
        echo "  ❌ GPU 数量异常: $GPU_COUNT"
        ((FAIL_COUNT++))
    else
        echo "  ✅ GPU 数量: $GPU_COUNT"
    fi
    
    # 2. ECC 检查
    DBE=$(ssh "$node" 'nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits 2>/dev/null | head -1' || echo "-1")
    if [[ "$DBE" -gt 0 ]] 2>/dev/null; then
        echo "  ❌ volatile DBE = $DBE"
        ((FAIL_COUNT++))
    else
        echo "  ✅ ECC clean"
    fi
    
    # 3. IB 端口状态
    IB_STATE=$(ssh "$node" "ibstat 2>/dev/null | grep 'State:' | grep -c 'Active'" || echo "0")
    if [[ "$IB_STATE" -lt 1 ]]; then
        echo "  ❌ IB 端口非 Active"
        ((FAIL_COUNT++))
    else
        echo "  ✅ IB Active (ports: $IB_STATE)"
    fi
    
    # 4. IB 计数器清零与预检
    echo "  清零 IB 计数器..."
    ssh "$node" 'for p in /sys/class/infiniband/*/ports/*/counters/*; do echo 0 > "$p" 2>/dev/null; done' || true
    
    # 5. 温度检查
    MAX_TEMP=$(ssh "$node" 'nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | sort -rn | head -1' || echo "0")
    if [[ "$MAX_TEMP" -gt 80 ]]; then
        echo "  ⚠️  最高温度 ${MAX_TEMP}°C，偏高"
    else
        echo "  ✅ 最高温度: ${MAX_TEMP}°C"
    fi
    
    # 6. 磁盘空间
    DISK_PCT=$(ssh "$node" "df -h /data 2>/dev/null | tail -1 | awk '{print \$5}' | tr -d '%'" || echo "0")
    if [[ "$DISK_PCT" -gt 90 ]]; then
        echo "  ❌ 数据盘使用率 ${DISK_PCT}%，空间不足"
        ((FAIL_COUNT++))
    else
        echo "  ✅ 数据盘使用率: ${DISK_PCT}%"
    fi
    
done < "$NODE_LIST"

echo ""
echo "=========================================="
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "❌ 检查完成，发现 $FAIL_COUNT 个问题，不建议启动任务"
    exit 1
else
    echo "✅ 全部检查通过，可以启动任务"
fi
```

### 6.2 任务前清单

| 检查项 | 具体操作 | 通过标准 |
|---|---|---|
| 节点清单健康检查 | 运行 `pre_job_check.sh` | 全部 PASS |
| IB 计数器清零 | 脚本内自动执行 | 确认清零成功 |
| 存储预热 | 确认 checkpoint 目录可写，dd 测试写入速度 | 写入 > 1GB/s（并行文件系统） |
| 监控面板准备 | Grafana 打开训练专用 Dashboard | 面板加载正常，数据刷新 |
| 告警直达值班 | 确认 PagerDuty/飞书/钉钉告警通道畅通 | 发送测试告警确认收到 |
| 最近一次 checkpoint | 确认存储上有足够空间保存新 checkpoint | 剩余空间 > 2x 单次 checkpoint 大小 |

### 6.3 任务中盯守指标

| 指标 | 关注点 | 异常阈值 |
|---|---|---|
| GPU SM_ACTIVE | 应持续 > 85% | 低于 70% 持续 5min |
| GPU 温度 | 应稳定 | 突然上升 > 5°C 或超过 85°C |
| GPU 功耗 | 接近 TDP | 突然下降 > 20% |
| NCCL 通信带宽 | 稳定 | 下降 > 15% |
| 训练 loss 曲线 | 平滑下降 | 突然 spike 或 NaN |
| Step time | 稳定 | 波动 > 10% |
| IB 端口 symbol_error | 增量应为 0 | 任意增量 |
| 存储 IOPS/带宽 | 在 checkpoint 写入时 spike | checkpoint 写入超过 60s |

**吞吐下降响应流程：**

```text
1. 发现吞吐下降 → 检查 step time 是否变长
2. 检查 SM_ACTIVE → 如果降低，检查是否数据加载瓶颈
3. 检查 NCCL 通信 → 如果通信时间占比上升：
   a. 检查 IB 计数器（symbol_error/port_rcv_errors）
   b. 检查是否有节点温度降频
   c. 运行 quick nccl-test 对比基线
4. 检查个别 rank → 如果某个 rank 明显慢：
   a. 定位该 rank 所在节点
   b. 检查该节点温度/功耗/XID
   c. 必要时替换该节点（热备节点 top-up）
5. 如果确认硬件故障 → 记录故障节点，任务继续跑（降级模式），任务后处理
```

### 6.4 任务后复检

```bash
#!/usr/bin/env bash
#===============================================================================
# post_job_check.sh — 任务完成后节点复检
# 适用环境: Ubuntu 22.04.5 (kernel 5.15) 或 Rocky 9.5 / driver R550+ / K8s 1.31
#===============================================================================

set -euo pipefail

NODE_LIST="${1:?请提供节点清单文件路径}"

echo "=== 任务后节点复检 ==="

while IFS= read -r node; do
    [[ -z "$node" || "$node" == \#* ]] && continue
    echo ">>> 复检: $node"
    
    # 1. ECC 增量检查
    ssh "$node" 'nvidia-smi --query-gpu=index,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv' 2>/dev/null
    
    # 2. IB 计数器增量
    echo "  IB 错误计数器:"
    ssh "$node" 'for f in /sys/class/infiniband/*/ports/*/counters/{symbol_error,port_rcv_errors,port_xmit_discards}; do
        echo "    $(basename $(dirname $(dirname $f)))/$(basename $f) = $(cat $f 2>/dev/null)"
    done' 2>/dev/null || true
    
    # 3. XID 新错误
    echo "  XID 检查:"
    ssh "$node" 'dmesg -T 2>/dev/null | grep "NVRM.*Xid" | tail -5' || true
    
done < "$NODE_LIST"

echo "=== 复检完成 ==="
```

---

## 7. 值班体系

### 7.1 On-Call 轮换设计

```text
轮换模式：周轮换制（推荐 3~4 人轮换）

周一 09:00 ──── 下周一 09:00
│                │
│   主值班 A     │  ← 负责所有 P1/P2 告警响应
│   副值班 B     │  ← A 无法响应时接管
│                │
└────────────────┘

交接要求：
- 周一 09:00 交接会议（15min）
- 交接内容：上周遗留问题 + 本周计划变更 + 当前告警状态
- 交接方式：线上会议 + 书面清单
```

### 7.2 交接班清单模板

```markdown
## GPU 集群值班交接班清单

| 项目 | 内容 |
|---|---|
| 交接时间 | 2024-01-15 09:00 |
| 交班人 | 张三 |
| 接班人 | 李四 |
| 上周告警统计 | P1: 0, P2: 1（XID 74 × 1，已自动隔离）, P3: 3, P4: 5 |
| 当前活跃告警 | 无 |
| 隔离节点清单 | gpu-node-042（XID 79，RMA 中，预计 1/18 到件） |
| 本周计划变更 | 1/17 驱动升级 535.129 → 535.154（变更单 CHG-2024-0042） |
| 运行中大任务 | LLM-pretrain-70B（预计 1/20 完成，当前 step 45000/100000） |
| 遗留问题 | 1. gpu-node-017 温度偏高（已标记 suspect，观察中）<br>2. 存储集群 OSD-23 需更换（已提工单） |
| 备件状态 | A100 备卡 × 12，光模块 × 30，线缆 × 20 |
| 特别交代 | 1/16 下午算法团队要做 nccl-tests 全量扫描，已协调窗口 |
```

### 7.3 告警分级响应时限表

| 级别 | 定义 | 典型场景 | 响应时限 | 处置时限 | 通知方式 |
|---|---|---|---|---|---|
| **P1 - 紧急** | 影响生产训练任务，多节点故障 | 大规模 XID 79 掉卡、IB 交换机宕机、存储集群不可用 | 5 分钟 | 30 分钟 | 电话 + IM + PagerDuty |
| **P2 - 高** | 单节点故障，有冗余不影响整体 | 单节点 XID 95、单链路 IB 中断、单 OSD 故障 | 15 分钟 | 2 小时 | IM + PagerDuty |
| **P3 - 中** | 性能劣化，非功能性故障 | 温度偏高、ECC SBE 增长、NCCL 带宽低于基线 | 1 小时 | 24 小时 | IM |
| **P4 - 低** | 信息性告警，不影响业务 | 巡检 WARN 项、风扇转速异常、时钟偏移轻微 | 4 小时 | 72 小时 | 邮件/工单 |

### 7.4 Runbook 模板

```markdown
## Runbook: GPU XID 79 (GPU Fallen Off Bus)

### 告警含义
GPU 从 PCIe 总线消失，驱动无法与 GPU 通信。通常由硬件故障（供电/PCIe 链路/GPU 本体）引起。

### 判断步骤
1. 确认告警来源（Prometheus alert / dmesg grep）
2. 确认受影响节点和 GPU 编号
3. 检查 dmesg 中 XID 79 之前的日志（是否有 PCIe AER 错误）
4. 检查 IPMI SEL 是否有供电/温度异常

### 处置命令

```bash
# 1. 隔离节点
bash /opt/scripts/node_isolate.sh <node-name> xid79 maintenance

# 2. 尝试软重置（仅当非频繁复现时）
ssh <node-name> 'sudo nvidia-smi -r -i <gpu_index>'
ssh <node-name> 'sudo rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia'
ssh <node-name> 'sudo modprobe nvidia'
ssh <node-name> 'nvidia-smi'

# 3. 如软重置失败，重启节点
ssh <node-name> 'sudo reboot'

# 4. 重启后验证
ssh <node-name> 'nvidia-smi'
ssh <node-name> 'dmesg -T | grep Xid'

# 5. 如重启后仍掉卡，触发 RMA
bash /opt/scripts/rma_evidence.sh <gpu_index>
```text

### 升级路径
- L1（值班工程师）：执行上述隔离和重置操作
- L2（GPU 运维专家）：分析 AER 日志和 IPMI 数据，判断是否为批量性问题
- L3（厂商支持）：提交 RMA 工单，附证据包

### 关联 Runbook
- node_isolate.sh 使用说明
- rma_evidence.sh 使用说明
- 节点回归流程
```

### 7.5 Blameless Postmortem 模板

```markdown
## Postmortem: [事件标题]

| 字段 | 内容 |
|---|---|
| 事件编号 | INC-2024-XXXX |
| 发生时间 | 2024-01-15 14:32 ~ 15:47（持续 75 分钟） |
| 影响范围 | 32 节点 / 256 张 A100 / LLM-pretrain-70B 任务中断 |
| 严重级别 | P1 |
| 值班响应人 | 张三 |
| Postmortem 作者 | 张三 |
| 评审人 | 李四、王五 |

### 时间线

| 时间 | 事件 | 操作人 |
|---|---|---|
| 14:32 | 告警触发：IB spine-02 port 12 down | 系统自动 |
| 14:33 | 值班收到 P1 告警（电话+IM） | 张三 |
| 14:35 | 登录跳板机，确认 spine-02 port 12 物理 down | 张三 |
| 14:38 | 发现 32 个节点 IB 链路切换到备用路径 | 张三 |
| 14:40 | NCCL timeout 告警触发，训练任务 step 45321 卡住 | 系统自动 |
| 14:42 | 通知算法团队暂停任务 | 张三 |
| 14:50 | 登录 IB 交换机管理口，检查 port 12 状态 | 张三 |
| 14:55 | 确认光模块故障（DDM 读数异常） | 张三 |
| 15:00 | 更换光模块 | 机房值班 |
| 15:10 | port 12 恢复 Active | 机房值班 |
| 15:15 | IB 链路回切，确认带宽正常 | 张三 |
| 15:20 | 重启训练任务（从 checkpoint 45300 恢复） | 算法团队 |
| 15:47 | 训练 loss 曲线恢复正常 | 算法团队确认 |

### 根因分析（5 Why）

1. **Why**: 32 个节点的 IB 通信中断 → 因为 spine-02 port 12 down
2. **Why**: port 12 down → 因为光模块发送功率降至阈值以下
3. **Why**: 光模块功率下降 → 因为光模块老化（已运行 18 个月）
4. **Why**: 老化光模块未提前更换 → 因为没有光模块 DDM 监控告警
5. **Why**: 没有 DDM 监控 → 因为 IB 交换机监控只覆盖了端口状态，未覆盖 DDM 参数

### 处置得失

| 做得好的 | 可改进的 |
|---|---|
| 告警响应及时（1min 内收到） | 没有第一时间判断是光模块问题 |
| 快速通知算法团队暂停任务 | 备件光模块找了一段时间 |
| 从 checkpoint 恢复顺利 | 没有自动化检查 IB 链路切换后的带宽 |

### 改进项

| 编号 | 改进项 | 类型 | Owner | 截止日期 | 状态 |
|---|---|---|---|---|---|
| AI-001 | 部署 IB 交换机 DDM 监控告警（光功率阈值） | 监控 | 张三 | 2024-02-01 | 待开始 |
| AI-002 | 建立光模块寿命台账，按运行时长主动更换 | 流程 | 李四 | 2024-02-15 | 待开始 |
| AI-003 | 编写 IB 链路切换后自动带宽验证脚本 | 工具 | 王五 | 2024-02-01 | 待开始 |
| AI-004 | 备件柜整理，光模块分区标记 | 物料 | 张三 | 2024-01-20 | 进行中 |
```

---

## 8. 备份策略

### 8.1 etcd 定时快照完整脚本

```bash
#!/usr/bin/env bash
#===============================================================================
# etcd_backup.sh — etcd 定时快照备份
# 适用环境: etcd 3.5.x（K8s 1.31 配套）/ etcdctl 3.5.x
# ETCDCTL_API=3: etcd 3.5 默认使用 v3 API（无需显式设置）；
#   etcd 3.4 及以前版本必须 export ETCDCTL_API=3 才能使用 v3 命令
# cron: 0 */6 * * * root /opt/scripts/etcd_backup.sh
# 保留策略：最近 7 天的 6 小时级 + 最近 30 天的日级 + 最近 3 月的周级
# 版本说明：
#   - etcdctl snapshot save: etcd 3.5+ 支持 --cacert/--cert/--key 参数
#   - etcdctl snapshot status --write-out=table: etcd 3.5+ 支持 table 输出
#   - etcd 3.5.15+ 为 K8s 1.31 推荐版本，需 NVMe 独立盘
#===============================================================================

set -euo pipefail

# ---------- 配置 ----------
ETCD_ENDPOINTS="https://127.0.0.1:2379"
ETCD_CACERT="/etc/kubernetes/pki/etcd/ca.crt"
ETCD_CERT="/etc/kubernetes/pki/etcd/server.crt"
ETCD_KEY="/etc/kubernetes/pki/etcd/server.key"
BACKUP_BASE="/backup/etcd"
KEEP_HOURLY=28       # 保留 28 个 6 小时快照（7 天）
KEEP_DAILY=30        # 保留 30 个日级快照
KEEP_WEEKLY=12       # 保留 12 个周级快照

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_DIR="${BACKUP_BASE}/snapshots"
SNAPSHOT_FILE="${BACKUP_DIR}/etcd-snapshot-${TIMESTAMP}.db"
HASH_FILE="${SNAPSHOT_FILE}.sha256"

mkdir -p "$BACKUP_DIR"

echo "=== etcd 快照备份开始 ==="
echo "时间: $(date)"
echo "目标: $SNAPSHOT_FILE"

# 1. 创建快照
# etcdctl 3.5 默认 v3 API；若运行环境为 etcd 3.4 及以前，需在脚本头部加 export ETCDCTL_API=3
etcdctl snapshot save "$SNAPSHOT_FILE" \
    --endpoints="$ETCD_ENDPOINTS" \
    --cacert="$ETCD_CACERT" \
    --cert="$ETCD_CERT" \
    --key="$ETCD_KEY"

# 2. 校验快照
echo "校验快照..."
etcdctl snapshot status "$SNAPSHOT_FILE" --write-out=table

# 3. 计算 SHA256
sha256sum "$SNAPSHOT_FILE" > "$HASH_FILE"
echo "SHA256: $(cat "$HASH_FILE")"

# 4. 快照大小检查（异常小可能是空快照）
SIZE=$(stat -c%s "$SNAPSHOT_FILE" 2>/dev/null || stat -f%z "$SNAPSHOT_FILE")
if [[ "$SIZE" -lt 1048576 ]]; then  # 小于 1MB 可疑
    echo "⚠️  快照大小仅 ${SIZE} bytes，可能异常！"
fi

# 5. 轮转清理
echo "执行轮转清理..."

# 按时间分类：今日的是 hourly，每日最后一个保留为 daily，每周最后一个保留为 weekly
cd "$BACKUP_DIR"

# 清理超出保留数的快照
TOTAL=$(ls -1 etcd-snapshot-*.db 2>/dev/null | wc -l)
if [[ "$TOTAL" -gt $((KEEP_HOURLY + KEEP_DAILY + KEEP_WEEKLY)) ]]; then
    # 保留最近的 KEEP_HOURLY 个
    ls -1t etcd-snapshot-*.db | tail -n +$((KEEP_HOURLY + 1)) | while read -r old; do
        # 检查是否为日级或周级保留点
        FILE_DATE=$(echo "$old" | grep -oP '\d{8}' | head -1)
        DAY_OF_WEEK=$(date -d "$FILE_DATE" '+%u' 2>/dev/null || date -j -f '%Y%m%d' "$FILE_DATE" '+%u' 2>/dev/null || echo "1")
        
        if [[ "$DAY_OF_WEEK" == "7" ]]; then
            # 周日快照保留为 weekly
            continue
        fi
        
        rm -f "$old" "${old}.sha256"
        echo "  已清理: $old"
    done
fi

echo "=== 备份完成 ==="
echo "当前快照数: $(ls -1 etcd-snapshot-*.db 2>/dev/null | wc -l)"
echo "磁盘使用: $(du -sh "$BACKUP_DIR" | awk '{print $1}')"
```

### 8.2 K8s 配置与 Helm Values 的 GitOps 化

```text
目录结构（推荐）：
gitops-repo/
├── clusters/
│   ├── gpu-prod/
│   │   ├── kube-system/
│   │   │   ├── nvidia-device-plugin.yaml
│   │   │   ├── dcgm-exporter.yaml
│   │   │   └── gpu-feature-discovery.yaml
│   │   ├── monitoring/
│   │   │   ├── prometheus-values.yaml
│   │   │   ├── grafana-values.yaml
│   │   │   └── alertmanager-config.yaml
│   │   └── training/
│   │       └── pytorch-job-operator.yaml
│   └── gpu-staging/
│       └── ...（同 prod 结构）
├── base/
│   ├── nvidia-device-plugin/
│   │   ├── Chart.yaml
│   │   └── values.yaml
│   └── ...
└── scripts/
    ├── sync.sh
    └── diff.sh
```

**核心原则：**
- 所有 K8s 资源 YAML 和 Helm values 必须入 Git 仓库
- 通过 ArgoCD/Flux 自动同步，禁止手动 `kubectl apply`
- 变更走 PR/MR 审批，审核通过自动部署
- 紧急修复可以先手动 apply，但必须在 2h 内补 PR

### 8.3 监控数据备份

| 数据类型 | 备份方式 | 频率 | 保留期 | 存储位置 |
|---|---|---|---|---|
| Prometheus TSDB | 快照（`/api/v1/admin/tsdb/snapshot`） | 每日 | 30 天 | S3/NAS |
| Grafana Dashboard | JSON export + Git 版本控制 | 每次变更 | 永久 | Git 仓库 |
| Alertmanager 配置 | Git 版本控制 | 每次变更 | 永久 | Git 仓库 |
| 告警历史 | Alertmanager webhook → ES/Loki | 实时 | 90 天 | ES/Loki |

```bash
# Prometheus 快照脚本
PROM_URL="http://prometheus:9090"
SNAP_DIR="/backup/prometheus"

curl -sf -X POST "${PROM_URL}/api/v1/admin/tsdb/snapshot" | tee /tmp/snap_result.json
SNAP_NAME=$(jq -r '.data.name' /tmp/snap_result.json)

# 复制快照到备份目录
rsync -av "/data/prometheus/snapshots/${SNAP_NAME}" "${SNAP_DIR}/"

# 压缩 + 上传 S3
tar czf "${SNAP_DIR}/prom-snap-$(date +%Y%m%d).tar.gz" -C "${SNAP_DIR}" "$SNAP_NAME"
aws s3 cp "${SNAP_DIR}/prom-snap-$(date +%Y%m%d).tar.gz" "s3://backup-bucket/prometheus/"

# 清理 30 天前快照
find "$SNAP_DIR" -name "prom-snap-*.tar.gz" -mtime +30 -delete
```

### 8.4 配置基线备份清单表

| 配置项 | 备份方法 | 频率 | 存储位置 |
|---|---|---|---|
| K8s 集群 YAML（全量） | `kubectl get all -A -o yaml` | 每日 | Git + S3 |
| Helm releases | `helm get values -A` | 每日 | Git |
| etcd 快照 | etcdctl snapshot（见 8.1） | 每 6 小时 | 本地 + S3 |
| 节点 OS 配置 | `/etc/` 关键文件打包 | 每周 | S3 |
| NVIDIA 驱动版本 | 脚本采集所有节点驱动版本 | 每日 | Prometheus metric |
| IB 交换机配置 | `ibnetdiscover` + 管理口备份 | 每次变更后 | Git |
| DNS/DHCP 配置 | 配置文件 Git 化 | 每次变更 | Git |
| 防火墙规则 | `iptables-save` / `nft list ruleset` | 每周 | Git |
| crontab | `crontab -l` 全节点汇总 | 每周 | Git |
| SSH 配置 | `/etc/ssh/sshd_config` | 每次变更 | Git |

---

> **下一分册：[08-故障排查与案例库](./08-故障排查与案例库.md)**
