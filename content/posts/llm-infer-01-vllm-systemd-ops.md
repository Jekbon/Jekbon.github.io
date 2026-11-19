---
title: "vLLM 推理服务的 systemd 化：一个 GLM5.1 实例的运维档案"
date: 2026-11-18T09:00:00+08:00
draft: false
description: "把 vLLM 推理服务交给 systemd 管：单元属性、启动参数、环境变量、API 端点与运维命令，以及五条踩出来的注意事项。"
tags: ["vLLM", "systemd", "运维", "LLM 推理"]
series: "大模型推理服务部署"
---

Docker 不是唯一答案。单机、固定模型、要求开机自启与崩溃自愈时，
systemd 比容器更轻、排障更直接。本文是一个真实在生产跑的
GLM5.1-FP8 服务的完整档案：配置、参数、命令和注意事项。

同系列的 [GLM-5.3-Flash Runbook](/posts/llm-infer-02-glm53flash-docker-runbook/)
走的是 Docker 路线，两种形态可以对照着选。

## 服务形态

| 项目 | 值 |
|---|---|
| systemd 服务名 | `glm51-vllm.service` |
| 服务描述 | vLLM GLM5.1-FP8 Service |
| 单元文件 | `/etc/systemd/system/glm51-vllm.service` |
| 启动脚本 | `/opt/glm5/run_vllm.sh` |
| 模型路径 | `/data/model/GLM5.1` |
| 运行用户 | `gpu`（组 `gpu`） |
| 工作目录 | `/opt/glm5` |
| 开机自启 | enabled |
| 崩溃重启 | `on-failure`，间隔 30s |
| 日志 | `/opt/glm5/logs/`，按时间戳滚动（如 `vllm_20260921_143149.log`） |

按上述属性组装的单元示意：

```ini
[Unit]
Description=vLLM GLM5.1-FP8 Service
After=network.target

[Service]
Type=simple
User=gpu
Group=gpu
WorkingDirectory=/opt/glm5
ExecStart=/opt/glm5/run_vllm.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

硬件为 8 × NVIDIA H20-3e（单卡 ~141 GB），TP=8
（`CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7`）。

## 启动参数

| 参数 | 值 | 说明 |
|---|---|---|
| `--host` / `--port` | `0.0.0.0:8000` | 监听所有网卡 |
| `--served-model-name` | `glm5.1-fp8` | 对外模型名 |
| `--tensor-parallel-size` | 8 | 8 卡张量并行 |
| `--quantization` | fp8 | FP8 量化 |
| `--gpu-memory-utilization` | 0.90 | 显存利用率上限 |
| `--max-model-len` | 200000 | 最大上下文 200K |
| `--max-num-batched-tokens` | 4096 | 单批 prefill token 数 |
| `--max-num-seqs` | 8 | 最大并发序列数 |
| `--distributed-executor-backend` | mp | 多进程后端 |
| `--enable-auto-tool-choice` + `--tool-call-parser glm47` | — | 工具调用 |
| `--reasoning-parser glm45` | — | 思维链解析 |
| `--chat-template-content-format` | string | 聊天模板内容格式 |
| `--enable-prefix-caching` | — | 前缀缓存 |
| `--enable-chunked-prefill` | — | 分块 prefill |
| `--disable-custom-all-reduce` | — | 禁用自定义 all-reduce |
| `--api-key` | `sk-REPLACE_WITH_YOUR_KEY` | API 鉴权密钥 |
| `--enable-log-requests` / `--enable-log-outputs` | — | 请求/输出日志 |
| `--enable-request-id-headers` | — | 响应带 request-id 头 |
| `--disable-fastapi-docs` | — | 关闭 API 文档页 |
| `--trust-remote-code` | — | 信任模型自定义代码 |

## 关键环境变量

| 变量 | 值 | 说明 |
|---|---|---|
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` | PyTorch 显存分配优化 |
| `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS` | 0 | 关闭 CUDA graph 显存预估（见注意事项 2） |
| `CUDA_HOME` / `LD_LIBRARY_PATH` | `/usr/local/cuda` | CUDA 环境 |
| `NCCL_ALGO` | Ring | NCCL 通信算法 |
| `NCCL_IB_DISABLE` | 0 | 启用 IB 网络 |
| `NCCL_NET_GDR_LEVEL` | 2 | GPUDirect RDMA 级别 |
| `NCCL_P2P_LEVEL` | NVL | NVLink P2P |
| `VLLM_WORKER_MULTIPROC_METHOD` | spawn | worker 多进程方式 |
| `VLLM_USE_V1` | unset | 不强制切换引擎（见注意事项 5） |
| `VLLM_LOGGING_LEVEL` | INFO | 日志级别 |

## 运行时信息

| 项目 | 值 |
|---|---|
| vLLM 版本 | 0.22.0 |
| 引擎 | V1 LLM engine |
| dtype | bfloat16（权重 FP8 量化） |
| max_seq_len | 200000 |
| KV cache 可用显存 | 34.11 GiB / 卡 |
| Python 虚拟环境 | `/opt/glm5/.venv` |

## API 端点

Base URL `http://<服务器IP>:8000`，鉴权 `Authorization: Bearer <你的key>`。

- `POST /v1/chat/completions` — 对话补全（OpenAI 兼容）
- `POST /v1/completions` — 文本补全
- `POST /v1/messages` — Anthropic 兼容接口
- `POST /v1/responses` — Responses API
- `GET /v1/models` — 模型列表
- `GET /health`、`GET /metrics` — 健康检查与指标
- `POST /tokenize` / `POST /detokenize` — 分词

## 运维命令

```bash
# 状态 / 重启 / 停止 / 启动
systemctl status glm51-vllm.service
systemctl restart glm51-vllm.service
systemctl stop glm51-vllm.service
systemctl start glm51-vllm.service

# 日志：journalctl 与文件（最新一份）
journalctl -u glm51-vllm.service -f
ls -t /opt/glm5/logs/ | head -1
tail -f /opt/glm5/logs/vllm_20260921_143149.log

# 健康检查与快速验证（需带 key）
curl http://localhost:8000/health
curl http://localhost:8000/v1/models -H "Authorization: Bearer $VLLM_API_KEY"
```

## 五条注意事项

1. **互斥部署**：本机多个 vLLM 服务共用 8000 端口和全部 8 张 GPU，
   同一时间只能运行一个。切换模型必须先 `systemctl stop` 当前服务再启动目标服务
2. **CUDA graph 显存预估已关闭**（`VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`）：
   KV cache 分配时未计入 CUDA graph 显存，存在 OOM 风险。
   官方建议恢复默认，并可将 `--gpu-memory-utilization` 提到 0.9016
3. **默认采样参数被模型覆盖**：`generation_config.json` 会把默认参数覆盖为
   `temperature=1.0, top_p=0.95`。如非预期，用 `--generation-config vllm` 启动
4. **公网暴露风险**：日志显示大量来自公网 IP 的扫描请求（`GET /`、`/mcp`、`/sse` 等 404）
   及无效 HTTP 请求。服务监听 `0.0.0.0` 且有 API key，但仍建议用防火墙把 8000 端口
   限制为内网或白名单来源
5. **`unset VLLM_USE_V1` 与实际跑 V1 引擎不矛盾**：vLLM 0.22.0 已默认使用 V1 引擎，
   unset 只是确保不强制切换到 V0
