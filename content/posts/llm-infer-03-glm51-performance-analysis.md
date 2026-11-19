---
title: "GLM-5.1 推理服务深度分析：LMCache 93% 命中率背后的五个瓶颈"
date: 2026-11-19T09:30:00+08:00
draft: false
description: "基于生产环境三天实测数据：请求流七节点全链路解析、LMCache 93.1% 命中率的代价与收益、decode 显存带宽瓶颈的量化推导，以及 Docker 与裸机部署的十五维对比。"
tags: ["vLLM", "LLM 推理", "性能调优", "LMCache"]
series: "大模型推理服务部署"
---

> 分析基于从生产服务器实时采集的 Prometheus metrics、LMCache status 与 nvidia-smi 数据，
> 采样窗口为连续三天。模型为 GLM-5.1-AWQ-4bit，8 卡张量并行。

## 请求流全链路

一个 Chat 请求从客户端到响应，经过七个节点：

![GLM-5.1 请求流全链路：客户端三条路径汇入 vLLM 七节点，节点④与 LMCache Server 经 tcp://127.0.0.1:6555 双向交互](/images/glm51-request-flow.svg)

开销可忽略的节点不展开：API Server（uvicorn，<1ms）、Tokenizer（<5ms）。
值得细看的是 ③④⑤⑥。

### 节点③ Scheduler

vLLM 的调度大脑，决定哪些请求同批执行。决策依据是空闲 GPU Block 数、
`max_num_seqs` 与 `max_num_batched_tokens`。

当前 KV Cache 容量：每卡 5,110 个 Block，每 Block 16 tokens
（vLLM `block_size=16`，LMCache 侧用 64），总容量
5,110 × 16 × 8 卡 = **654,080 tokens**，远超 `max_model_len=200K`。
显存分配 92% 给权重 + KV Cache（`gpu-memory-utilization=0.92`）。

### 节点④ LMCache：本架构最大的亮点

三天实测：

| 指标 | 数值 | 说明 |
|---|---|---|
| 总 prompt tokens | 75,095,705 | 约 7500 万 |
| LMCache 命中 | 69,895,168 | **93.1%** |
| 实际计算 | 5,200,537 | 仅 6.9% |
| 本地 Prefix Cache 命中 | 0 | 已禁用（`--no-enable-prefix-caching`） |

**93% 的 prompt 不需要重做 Prefill**，直接从 LMCache 的 64 GB 主机内存加载 KV Cache。
对长系统提示 + 多轮对话场景，这是数量级的收益。

LMCache 内部状态：

```text
L1 内存:  47.07 GB / 64 GB  (68.5% 使用率)
缓存对象: 459 个 chunk
累计写入: 4,308 次
累计读取: 546,056 次   ← 平均每个 chunk 被读 126 次，复用率极高
累计驱逐: 3,849 次     (LRU, 80% 水位触发)
```

### 节点⑤⑥ Prefill 与 Decode 的性格差异

Prefill 是**计算密集型**：156 层 Transformer、MLA 注意力
（KV Cache 压缩到 [132, 576]，每 token 仅 ~98 KB），TP=8 每层一次 AllReduce。

Decode 是**显存带宽密集型**：每个 step 都要读全部模型权重
（8 卡分摊后每卡 ~50 GB），H20 的 HBM3 约 4 TB/s/卡。

实测平均每请求生成 ~265 tokens（698,654 / 2,633 请求）。

## 五个瓶颈

```text
                        Prefill 阶段              Decode 阶段
                     (仅 6.9% 的 tokens)        (所有生成 tokens)

  计算 (FLOPs)       ██████████ 高              ████░░░░░░ 低
  显存带宽           ████░░░░░░ 中              █████████░ 高 ← 主瓶颈
  GPU 间通信         ███░░░░░░░ 中低            ███░░░░░░░ 中低
  LMCache 传输       ██░░░░░░░░ 低              ░░░░░░░░░░ 无
  调度排队           █░░░░░░░░░ 低              █░░░░░░░░░ 低
```

**瓶颈 1：Decode 的显存带宽（主瓶颈）。** 每 step 每卡读 ~50 GB 权重，
受限于 HBM3 带宽：理论最快 50GB / 4TB/s ≈ 12.5ms/step，加上 AllReduce 与
Attention 开销实际 20~40ms/step。这是 LLM 推理的固有瓶颈，TP=8 已经把
单卡压力摊到 8 份。优化方向是提高 batch size，用计算换带宽利用率。

**瓶颈 2：Prefill 计算量（已被 LMCache 大幅缓解）。** 只有 6.9% 的 token 真算。
但新对话（无缓存）的 TTFT 仍受 Prefill 支配：10K token 全 miss 时
10000 / 4096 ≈ 3 个 chunk。提高 `max-num-batched-tokens` 可加速，
代价是 Decode 阶段调度灵活性下降。

**瓶颈 3：GPU 间 AllReduce。** NV18 全互联（18 条 NVLink、单链路 26.562 GB/s、
单卡双向约 478 GB/s），通信不是主要瓶颈。但 GPU 0-3 在 NUMA node 0、
4-7 在 node 1，**跨 NUMA 的 CPU 侧数据搬运（如 LMCache）会走 UPI 总线**，
这是容易被忽略的隐性成本。

**瓶颈 4：LMCache 的 Host Memory 带宽。** KV Cache 存在主机 DDR5 里，
读回 GPU 要走 PCIe Gen5（~64 GB/s），比 GPU 本地 HBM3（~4 TB/s）慢约 60 倍。
但比起重做 Prefill（可能数秒），PCIe 传输（毫秒级）仍然快得多——
这是一笔划算的交易。

**瓶颈 5：并发限制。** `max-num-seqs=16` + `max-num-batched-tokens=4096`，
200K 长上下文下一个请求就可能占满整个 batch。当前配置偏向低延迟低并发，
不适合高吞吐场景。

## 优化建议（按优先级）

| 优先级 | 优化项 | 预期收益 | 难度 |
|---|---|---|---|
| P0 | 调高 `max-num-seqs` | 吞吐提升 2~4x | 低 |
| P0 | 开启 Prefix Caching 配合 LMCache | 双重缓存，miss 时有兜底 | 低 |
| P1 | 调高 `max-num-batched-tokens` | Prefill 加速 | 低 |
| P1 | LMCache L1 扩容 | 提高命中率、减少驱逐 | 低 |
| P2 | KV Cache 用 FP8 | 显存省 50%，支撑更长上下文 | 中 |
| P2 | Speculative Decoding | Decode 加速 2~3x | 高 |

几个关键判断依据：

- `max-num-seqs` 该不该调，看 `vllm:num_requests_waiting`——经常 > 0 说明被限流了。
  请求平均只有 ~265 tokens 时，32~64 是安全区间
- Prefix Caching 当前被 `--no-enable-prefix-caching` 完全关掉，全押 LMCache。
  建议同时开启作二级兜底：LMCache miss 后 vLLM 内部缓存还能接住，
  速度远快于 LMCache 的 PCIe 传输
- LMCache L1 使用率已 68.5% 并触发 3,849 次驱逐，而主机有 2 TB 内存，
  扩到 128 GB 几乎没有成本

## Docker 部署 vs 裸机：十五维对比

两种形态的结构差异一句话概括：Docker 把 CUDA/Python/依赖全部封进容器、
靠 `--net=host` + `--runtime=nvidia` 拿到裸机性能；裸机直接用系统 CUDA 与 NCCL。
逐项差异见下表。


| 维度 | Docker（`--net=host --runtime=nvidia`） | 裸机（pip） |
|---|---|---|
| GPU 计算性能 | 无损耗，GPU 直通零虚拟化开销 | 原生 |
| 网络性能 | `--net=host` 无损耗 | 原生 |
| CPU 开销 | cgroup + namespace < 1% | 无 |
| 共享内存 | **必须 `--shm-size=64g`，默认 64MB 会直接崩** | 系统 /dev/shm，无需配置 |
| NCCL 通信 | `--net=host` 正常；bridge 网络会失败 | 无配置 |
| 环境隔离 | 优秀，CUDA/Python/依赖全在容器内 | 差，多模型多版本易冲突 |
| 多模型管理 | 一容器一模型，互不影响 | 每模型一个 venv |
| 镜像/环境更新 | `docker pull` + 改 tag | pip 升级可能引入兼容问题 |
| 回滚 | `docker tag` 秒级 | pip 降级痛苦 |
| 调试 | 需 `docker exec` 进入 | 终端直接操作 |
| 资源限制 | `--gpus/--cpus/--memory` 精确控制 | 手动配 cgroups |
| 生产适用性 | **强烈推荐** | 适合开发实验 |

三个容易踩的具体点：

1. **`--shm-size` 默认 64MB 会让 vLLM 多进程直接崩溃**，必须显式给大值。
   实测分配 64g 后实际只用 31 MB——mp 后端并不重度使用 shm，这个值是安全冗余
2. **`--net=host` 的副作用**：网络零开销、NCCL 直通，但容器能监听宿主机任意端口，
   隔离性下降。若 API 端口直接暴露公网，防火墙是唯一防线
3. **容器内 CUDA 版本可以新于宿主机驱动支持的 runtime**：NVIDIA 驱动向下兼容
   CUDA runtime（forward compatibility），CUDA 13 容器跑在旧驱动上可行，
   但某些新特性可能不可用

结论：多模型、多版本、要回滚和生产一致性的场景，Docker 明确胜出。
裸机唯一的优势是调试方便，而 `docker exec` 基本抹平了这个差距。

## 架构总评

做得好的：

- **LMCache 集成**：7500 万 prompt tokens 只有 520 万真算，重复 system prompt
  与多轮对话场景的巨大优化
- **NVLink 全互联**：AllReduce 不走 PCIe，通信不是瓶颈
- **Chunked Prefill**：避免长文本 Prefill 一次性占满 GPU，保护 Decode 延迟
- **MLA 架构**：KV Cache 每 token ~98 KB（标准 MHA 要 500 KB+），同显存缓存更多上下文

要改的：

- `max-num-seqs=16` 偏低，按实际 QPS 调到 32~64
- Prefix Caching 被关闭，缺 GPU 本地二级兜底
- LMCache L1 使用率偏高，主机内存有余量就该扩
- **API 端口公网裸露时，API key 是唯一防线——弱 key 等于没有防线**，
  应启用防火墙限制来源 IP，并把 key 换成高强度随机值
- 未启用 FP8 KV Cache，H20 支持但当前用 auto，启用可省约 50% 显存
