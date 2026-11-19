---
title: "8×H20 部署 GLM-5.3-Flash：Docker、1M 上下文与多模态 Runbook"
date: 2026-11-19T09:00:00+08:00
draft: false
description: "在国内无代理网络下，用 Docker 专用镜像把 320B-A18B 的 GLM-5.3-Flash 跑上 8 卡 H20：1M 上下文、多模态、工具调用、MTP 投机解码，以及一路踩过的坑。"
tags: ["vLLM", "Docker", "GPU", "LLM 推理"]
series: "大模型推理服务部署"
---

## 环境与目标

| 项 | 配置 |
|---|---|
| GPU | 8 × NVIDIA H20-3e（单卡 ~140 GB 显存，合计 ~1.1 TB） |
| 驱动 / CUDA | 595.45.04 / 13.2 |
| CPU | 2 × Xeon Platinum 8558P（96 物理核 / 192 线程，2 NUMA） |
| 内存 | 2 TiB |
| 数据盘 | 7 TB NVMe（挂载 `/data`） |
| OS / 内核 | Ubuntu 22.04 / 6.8.0-136-generic |

模型：**GLM-5.3-Flash**，320B-A18B MoE，原生 FP8（磁盘约 306 GB、62 个分片），
`glm5_next` 架构，原生多模态。权重从 ModelScope 下载，放在 `/data/model/GLM-5.3-Flash`
（注意是单数 `model`）。

目标形态：

- TP=8 张量并行，**1M 上下文 + 多模态 + 工具调用**
- Docker 隔离，专用镜像，国内无代理全程国内源
- 生产化：崩溃自愈、日志轮转、启动超时保护、权重只读挂载、chunked-prefill

## 为什么不用 pip

三条硬卡点，任何一条都走不通：

1. **架构太新**：`glm5_next` 的核心支持 PR 于 2026-09-03 才合入主线，0.29.0 稳定版
   （8/26 cut）没赶上；本机现有 venv（0.17.1 / 0.22.0）都只认 GLM-4 架构，
   启动直接报 `Architecture glm5_next is not supported`
2. **稳定版有 checkpoint bug**：0.29.0 存在 `k_conv1d` vs `conv1d.weight` 的加载问题
3. **nightly 源连不上**：pip 的 nightly wheels 源在纯国内网络不可达

结论：只能走 Docker 专用镜像 `vllm/vllm-openai:glm53-flash`。
镜像内 vLLM 为 dev build `0.1.dev20051+g487ecf187`，预检确认支持
`Glm5NextForCausalLM / Glm5NextForConditionalGeneration / Glm5NextMTPModel`，
与权重 `config.json` 的 `Glm5NextForConditionalGeneration` 对得上。

## 1. 下载权重（306 GB，务必 tmux 里跑）

堡垒机或跳板会话随时可能断，下载必须放进 tmux：

```bash
tmux new -s glm-dl
```

```bash
pip install -U modelscope
```

```bash
# 国内源，支持断点续传；中断后重跑同一条即可续传
modelscope download --model ZhipuAI/GLM-5.3-Flash --local_dir /data/model/GLM-5.3-Flash
```

```bash
# 校验：应看到 safetensors 分片 + config.json，总量 ~306GB
ls -lh /data/model/GLM-5.3-Flash | head
du -sh /data/model/GLM-5.3-Flash
```

tmux 脱离：`Ctrl+b` 然后 `d`；重连：`tmux attach -t glm-dl`。

## 2. Docker 环境（纯国内网）

装包走阿里云源，不碰 `get.docker.com` 的国际脚本：

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker && docker --version
```

数据目录放 NVMe，不占系统盘；镜像加速器一次写进 `daemon.json`：

```bash
sudo systemctl stop docker docker.socket
sudo mkdir -p /data/docker
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "data-root": "/data/docker",
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1panel.live",
    "https://hub.rat.dev"
  ]
}
EOF
sudo systemctl start docker
docker info | grep -E "Docker Root Dir|Registry Mirrors" -A3
```

nvidia-container-toolkit 与运行时配置：

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

```bash
# 验证容器能看到 8 张卡
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## 3. 拉镜像与启动前预检

镜像约 20 GB，同样放 tmux。报 not found 就改 `:nightly`：

```bash
# 方式一：走 daemon.json 里配的公共加速器
docker pull vllm/vllm-openai:glm53-flash
```

```bash
# 方式二：公共加速器失效时，直接拉华为云 SWR 的同步镜像，最稳
docker pull swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/vllm/vllm-openai:glm53-flash
# 后文命令都用短名，先打个 tag
docker tag swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/vllm/vllm-openai:glm53-flash \
  vllm/vllm-openai:glm53-flash
```

> 镜像同步感谢 [docker.aityp.com](https://docker.aityp.com/image/docker.io/vllm/vllm-openai:glm53-flash)
> 提供的华为云 SWR 加速。纯国内网拉 docker.io 原版基本不可行，
> 这类持续同步的镜像仓库是实打实的基础设施，用上了就该署名。

**启动前先预检，别白加载 306 GB 权重**：

```bash
# 镜像是否认 glm5_next 架构
docker run --rm --entrypoint python3 vllm/vllm-openai:glm53-flash -c "import vllm; from vllm.model_executor.models.registry import ModelRegistry; print('vllm', vllm.__version__); print([a for a in ModelRegistry.get_supported_archs() if 'Glm5' in a or 'lm5' in a])"
grep -i architectures /data/model/GLM-5.3-Flash/config.json
```

```bash
# 镜像是否认后面要用的开关；不认的就从启动命令里删掉
docker run --rm --entrypoint vllm vllm/vllm-openai:glm53-flash serve --help 2>&1 | grep -oE -- "--(enable-prompt-tokens-details|enable-force-include-usage|enable-request-id-headers|chat-template-content-format|enable-chunked-prefill|enable-log-requests)" | sort -u
# 权重是否真支持 1M；不支持就把启动命令里的 1M 改回 524288
grep -E "max_position_embeddings|rope_scaling|sliding_window" /data/model/GLM-5.3-Flash/config.json
```

## 4. 启动

key 用环境变量 + guard 生成，空值直接拒绝启动，避免裸奔：

```bash
export VLLM_API_KEY="sk-glm53flash-$(openssl rand -hex 24)"; echo "$VLLM_API_KEY"
docker rm -f glm53flash 2>/dev/null   # 换参数必须重建容器
```

```bash
docker run -d \
  --name glm53flash \
  --gpus all \
  --network host \
  --ipc host \
  --restart unless-stopped \
  --log-driver json-file --log-opt max-size=1g --log-opt max-file=10 \
  -e VLLM_ENGINE_READY_TIMEOUT_S=3600 \
  -v /data/model/GLM-5.3-Flash:/model:ro \
  --entrypoint vllm \
  vllm/vllm-openai:glm53-flash \
  serve /model \
  --served-model-name GLM-5.3-Flash \
  --host 0.0.0.0 --port 8000 \
  --api-key "${VLLM_API_KEY:?VLLM_API_KEY is not set}" \
  --tensor-parallel-size 8 \
  --pipeline-parallel-size 1 \
  --distributed-executor-backend mp \
  --gpu-memory-utilization 0.95 \
  --max-model-len 1048576 \
  --max-num-seqs 16 \
  --max-num-batched-tokens 8192 \
  --enable-chunked-prefill \
  --enable-prefix-caching \
  --speculative-config '{"method":"mtp","num_speculative_tokens":5}' \
  --tool-call-parser glm47 \
  --reasoning-parser glm45 \
  --enable-auto-tool-choice \
  --enable-request-id-headers \
  --enable-prompt-tokens-details \
  --enable-force-include-usage \
  --enable-log-requests \
  --chat-template-content-format openai \
  --trust-remote-code \
  --no-enable-flashinfer-autotune
```

```bash
# 跟日志，等到 "Application startup complete"
docker logs -f glm53flash
```

启动过程卡在 `No available shared memory broadcast block`、Triton 编译、
`CUDA graph 0/51` 都是正常阶段，等即可。

## 5. 参数说明

| 参数 | 取值 | 为什么 |
|---|---|---|
| `--tensor-parallel-size 8` + `--pipeline-parallel-size 1` | TP=8 | 单机 8 卡张量并行，无流水并行 |
| `--distributed-executor-backend mp` | 多进程 | 单机 TP 默认，显式写出 |
| `--gpu-memory-utilization 0.95` | 显存上限 | 经生产验证的取值；OOM 则降 0.92 / 0.90 |
| `--max-model-len 1048576` | 1M | 输入+输出之和的硬上限 |
| `--max-num-seqs 16` | 并发序列 | 小团队够用，为 1M 留 KV 余量 |
| `--max-num-batched-tokens 8192` | prefill 分块 | 长 prompt 提速可提到 16384（更吃显存） |
| `--enable-chunked-prefill` | 开 | 长 prompt 分块预填，降显存峰值 |
| `--enable-prefix-caching` | 开 | 复用 system prompt 前缀，多轮提速 |
| `--speculative-config mtp` | 投机解码 | MTP，`num_speculative_tokens=5` |
| `--tool-call-parser glm47` / `--reasoning-parser glm45` | 解析器 | 工具调用 + 思维链 |
| `--chat-template-content-format openai` | 多模态必须 | 设成 `string` 会把图丢掉 |
| `--enable-request-id-headers` 等三项 | 可观测 | 请求追踪 + token 计费可见 |
| `--restart unless-stopped` | 自愈 | 崩溃/重启自动拉起，仅手动 stop 不拉起 |
| `--no-enable-flashinfer-autotune` | H20 实测更稳 | 来自 8 卡 H20 的跑通经验 |

## 6. 验证

都带 Bearer key。先确认日志已 `Application startup complete`。

```bash
# 鉴权检查：不带 key 应返回 401，说明鉴权生效
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v1/models
```

```bash
# 列模型，应看到 id=GLM-5.3-Flash
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | head -c 500
```

```bash
# 纯文本对话
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"GLM-5.3-Flash","max_tokens":256,"messages":[{"role":"user","content":"用一句话介绍你自己"}]}'
```

```bash
# 多模态实测：base64 内嵌，避开纯国内网拉外网图床
# 成功=返回图的描述；报错/崩溃=该 build 视觉路径不可用，回退 --chat-template-content-format string
B64=$(base64 -w0 /path/to/test.jpg)
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"GLM-5.3-Flash\",\"max_tokens\":512,\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"一句话描述这张图\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,$B64\"}}]}]}"
```

客户端接入三参数（1M 配置）：模型上下文长度 `1048576`、最大输出 `16384~32768`
（推理模型的思考也计入输出，别设小）、最大输入留空或 `1015808`。

## 7. 边界与兜底

- **公网暴露**：8000 端口公网可达，日志里已有境外扫描段（85.217.x / 198.235.x）的
  探测记录。`--api-key` 是底线，建议再用 ufw 把 8000 限制为内网或白名单 IP
- **多模态传图**：纯国内网下客户端发 http(s) 图片 URL 时是**服务端**去下载，
  外网图床大概率拉不到 → 让客户端发 base64 data URI 或国内可达图床
- **长上下文**：1M 的 prefill 较慢属正常；要更快把 `--max-num-batched-tokens` 提到 16384
- **OOM 兜底**：1M + 0.95 显存余量薄，若 CUDA 图捕获阶段 OOM，
  降 `--gpu-memory-utilization 0.92` 或 `--max-model-len 524288`
- **加速器失效**：公共镜像加速器不稳定时，到云控制台拿专属加速地址填进 `daemon.json`

## 8. 备选：SGLang

需要更高并发吞吐时可以试 SGLang：

```bash
docker run -d --name glm53-sglang --gpus all --ipc=host --shm-size=32g \
  -p 30000:30000 -v /data/model/GLM-5.3-Flash:/model \
  lmsysorg/sglang:latest \
  python3 -m sglang.launch_server --model-path /model \
  --tp 8 --host 0.0.0.0 --port 30000 --trust-remote-code
```

## 9. 常见坑速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Architecture glm5_next is not supported` | vLLM 太旧 | 用专用镜像；启动前先跑第 3 节预检 |
| 启动 OOM（CUDA 图捕获阶段） | 1M + 0.95 显存余量薄 | 降 `--gpu-memory-utilization 0.92`，或 `--max-model-len 524288` |
| `max-model-len exceeds max_position_embeddings` | 权重 config 不支持 1M | 按预检结果把 1M 改回 524288 |
| `unrecognized arguments: --xxx` | 镜像 build 没这个开关 | 跑 `serve --help` 的 grep 预检，删掉不认的那行 |
| 传图报错 / 视觉不可用 | 该 checkpoint/build 视觉路径不通 | `--chat-template-content-format` 回退 `string`，当纯文本用 |
| 传图超时 | 纯国内网，服务端拉外网图床失败 | 客户端改发 base64 data URI 或国内图床 |
| `/v1/models` 返回 401 | 没带 key | header 加 `Authorization: Bearer <key>`；这是预期，说明鉴权生效 |
| 8000 端口被扫 | 端口公网可达 | 已加 `--api-key`；建议再用 ufw 限内网/白名单 |
| 卡在共享内存广播块 / Triton 编译 / `CUDA graph 0/51` | 正常启动阶段 | 等，直到 `Application startup complete` |
| 下载中断 | 会话断连 | 务必 tmux；modelscope 支持断点续传，重跑即可 |
