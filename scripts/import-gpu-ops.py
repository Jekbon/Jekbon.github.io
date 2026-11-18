#!/usr/bin/env python3
"""
把《GPU 集群高级运维知识体系》13 个分册整理成博客文章。

做四件事：
  1. 脱敏 —— 源文档里混进了真实凭据与内网地址，public 站点必须替换
  2. 去掉指向本地相对路径的系列引用块（../xxx.md 在博客里是死链）
  3. 去掉带编号的一级标题（front matter 的 title 会渲染成 h1，避免双 h1）
  4. 补 front matter（title / date / description / tags / series）

源文件不动，只生成 content/posts/ 下的副本。
用法: python3 scripts/import-gpu-ops.py [源目录]
"""
import datetime
import pathlib
import re
import sys

SRC = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
    pathlib.Path('/Users/wangbin/devops/k8s考试题/GPU集群运维')
DST = pathlib.Path(__file__).resolve().parent.parent / 'content' / 'posts'

SERIES = 'GPU 集群运维'

# 源文件前缀 -> (slug, title, tags, description)
META = {
    '00': ('gpu-ops-00-version-baseline', 'GPU 集群运维（00）：全系列版本基线',
           ['GPU', '版本管理', '基线'],
           '全部 12 个分册的版本引用源：OS 与内核、NVIDIA 驱动与 CUDA、Kubernetes、网络、存储、'
           '监控、自动化工具的基线版本，硬件代际对应关系，版本钉死方法，以及接手任何机器先跑的现场核查清单。'),
    '01': ('gpu-ops-01-cluster-delivery', 'GPU 集群运维（01）：集群交付与验收',
           ['GPU', '交付验收', '机房规划'],
           '从机房规划、网络架构设计、BIOS 全量清单、OS 基线配置，到装机验收流程、nccl-tests、'
           'IB 网络验收、存储验收与验收报告模板的完整交付手册。'),
    '02': ('gpu-ops-02-driver-cuda-stack', 'GPU 集群运维（02）：驱动、固件与 CUDA 软件栈',
           ['GPU', 'NVIDIA', 'CUDA', '驱动'],
           'NVIDIA 驱动三种安装方式、Fabric Manager、nvidia-persistenced、内核模块体系、'
           '驱动升级完整 SOP、固件管理、nvidia-container-toolkit，以及 nvidia-smi 命令大全与安装故障排查表。'),
    '03': ('gpu-ops-03-kubernetes-platform', 'GPU 集群运维（03）：Kubernetes 平台层',
           ['Kubernetes', 'GPU', '调度'],
           'GPU 组件栈全景、Device Plugin 深入、MIG 完整实操、GPU 共享方案对比、批量调度器、'
           'NUMA 拓扑、训练作业编排、DRA 动态资源分配，以及集群自身运维与故障速查表。'),
    '04': ('gpu-ops-04-storage', 'GPU 集群运维（04）：存储',
           ['存储', 'Lustre', 'JuiceFS', 'GPU'],
           '训练场景 IO 特征、存储方案对比、Lustre 客户端运维、JuiceFS 与 Fluid 完整实操、'
           '镜像与数据集分发、Checkpoint 优化、fio 测试、存储监控与容量治理。'),
    '05': ('gpu-ops-05-network-nccl', 'GPU 集群运维（05）：网络 —— IB/RoCE 与 NCCL',
           ['网络', 'RDMA', 'NCCL', 'GPU'],
           'InfiniBand 概念体系与硬件拓扑、IB 运维命令大全、OpenSM 子网管理器、端口错误计数器巡检、'
           'RoCE v2 完整配置、GPUDirect RDMA、NCCL 深入与排障决策树。'),
    '06': ('gpu-ops-06-observability', 'GPU 集群运维（06）：监控与可观测性',
           ['监控', 'Prometheus', 'DCGM'],
           'DCGM 与 dcgm-exporter 部署、DCGM 指标全表、PromQL 查询大全、告警规则、Grafana 大盘设计、'
           'Prometheus 容量规划、网络与存储监控，以及 MFU 计算方法。'),
    '07': ('gpu-ops-07-node-operations', 'GPU 集群运维（07）：日常运维与节点管理',
           ['运维', '巡检', 'GPU'],
           '每日巡检体系、节点健康池管理、慢节点（Straggler）检测、变更管理、RMA 与备件、'
           '大任务护航 SOP、值班体系，附 GPU XID 79 掉卡 Runbook。'),
    '08': ('gpu-ops-08-troubleshooting', 'GPU 集群运维（08）：故障排查与案例库',
           ['排障', 'XID', '案例'],
           '排查方法论、日志源大全表、XID 完整手册、ECC 与显存健康判读、GPU 掉卡（XID 79）专题、'
           '性能类排查、K8s 层 GPU 故障案例、经典案例库与应急工具箱。'),
    '09': ('gpu-ops-09-capacity-cost', 'GPU 集群运维（09）：容量、成本与效率运营',
           ['容量', '成本', '多租户'],
           '容量规划方法、多租户治理完整方案、配额借还与公平调度原理、利用率分析体系与周报模板、'
           '成本核算，以及电力与散热运营。'),
    '10': ('gpu-ops-10-automation', 'GPU 集群运维（10）：自动化与平台工程',
           ['自动化', 'GitOps', 'Ansible'],
           '自动化体系总览、Ansible 实战、批量执行工具、装机自动化、自愈系统、GitOps、'
           '巡检平台化、平台门户功能清单与工具箱沉淀方法。'),
    '11': ('gpu-ops-11-security', 'GPU 集群运维（11）：安全与合规',
           ['安全', '合规'],
           'GPU 集群攻击面总览、驱动与供应链安全、容器与 GPU 设备安全、Kubernetes 安全、'
           '网络安全、数据安全与审计合规。'),
    '12': ('gpu-ops-12-frontier', 'GPU 集群运维（12）：前沿方向与异构算力',
           ['GPU', '前沿', '异构算力'],
           '超节点时代、液冷运维、网络演进、国产算力运维、DRA 与调度前沿、大规模训练容错、'
           '推理运维前沿、算力运营前沿，以及学习资源与信息渠道清单。'),
}

# 脱敏规则：顺序敏感，先替换长的
SANITIZE = [
    ('aicoda123!', 'REPLACE_WITH_STRONG_SECRET'),          # 真实 MinIO secret key
    ('10.240.0.58:12888', '10.0.0.58:9000'),               # 真实内网 MinIO 地址+端口
    ('10.240.0.58', '10.0.0.58'),                          # 真实内网服务器
    ('gitlab.aicoda.tech', 'gitlab.example.com'),          # 真实公司 GitLab
]

# 指向本地相对路径的系列引用块（在博客里是死链）
SERIES_QUOTE = re.compile(
    r'^> 本分册属于《GPU 集群高级运维知识体系》系列.*$(?:\n^>.*$)*', re.M)
# 带编号的一级标题（front matter title 已提供 h1）
NUMBERED_H1 = re.compile(r'^# \d{2} [·-] .*$', re.M)

# ---- 代码块语言推断（保守：认不准就 text，宁可不高亮也不猜错）----
BOX_CHARS = '┌┐└┘├┤┬┴┼─│═║╔╗╚╝'          # ASCII 框线图，高亮只会添乱
CJK = re.compile(r'[一-鿿]')
FENCE = re.compile(r'^```([^\n`]*)$')
SHELL_FIRST = re.compile(
    r'^\s*(?:[$#]\s+\S|'
    r'(?:kubectl|nvidia-smi|systemctl|iptables|ip\s|ethtool|ibstat|ibv_\w+|'
    r'mc\s|ansible\w*|helm\s|apt(?:-get)?\s|dnf\s|yum\s|modprobe|lsmod|dmesg|'
    r'journalctl|curl\s|wget\s|git\s|docker\s|ctr\s|crictl|ssh\s|scp\s|rsync\s|'
    r'tar\s|fio\s|chronyc|hwclock|lspci|lsblk|ofed_info|mlxconfig|sudo\s|'
    r'echo\s|cat\s|grep\s|awk\s|sed\s|chmod|chown|mount\s|umount)\b)')


def guess_lang(body: str) -> str:
    if any(c in body for c in BOX_CHARS):
        return 'text'
    lines = [l for l in body.split('\n') if l.strip()]
    if not lines:
        return 'text'
    first = lines[0].strip()
    if first.startswith('**'):
        return 'text'                       # 误入代码块的 markdown 正文
    if first.startswith('#'):
        return 'text' if CJK.search(first) else 'bash'
    if first.startswith('###') or first.startswith('> '):
        return 'text'
    if SHELL_FIRST.match(first):
        return 'bash'
    if first.startswith('{') or first.startswith('['):
        return 'json'
    if re.match(r'^\[[\w.-]+\]$', first):
        return 'ini'
    kv = sum(1 for l in lines if re.match(r'^\s*-?\s*[A-Za-z_][\w.-]*:\s', l))
    if kv >= 2 and kv >= len(lines) // 3:
        return 'yaml'
    if first == '---' and kv >= 1:
        return 'yaml'
    return 'text'


def annotate_code_blocks(text: str) -> str:
    """给无语言标识的围栏补一个保守的语言标记。

    围栏必须正确配对：有标识块的结束围栏同样是空标识的 ```，
    若不分状态跟踪，结束围栏会被误当成新的无标识块开始，
    把块间正文吞成代码块、并把真正的代码块错位。
    源文档无四反引号嵌套，简单配对即可。
    """
    lines = text.split('\n')
    out, i, n = [], 0, len(lines)
    while i < n:
        m = FENCE.match(lines[i])
        if m and m.group(1).strip() == '':
            # 无标识围栏开始：收集到下一个围栏行（即它的结束围栏）
            j = i + 1
            body = []
            while j < n and not lines[j].startswith('```'):
                body.append(lines[j])
                j += 1
            out.append('```' + guess_lang('\n'.join(body)))
            out.extend(body)
            if j < n:
                out.append(lines[j])          # 结束围栏
            i = j + 1
        elif m:
            # 有标识围栏开始：原样保留整块，跳到它的结束围栏
            out.append(lines[i])
            i += 1
            while i < n and not lines[i].startswith('```'):
                out.append(lines[i])
                i += 1
            if i < n:
                out.append(lines[i])          # 结束围栏
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return '\n'.join(out)


def main():
    if not SRC.exists():
        sys.exit(f'源目录不存在: {SRC}')
    DST.mkdir(parents=True, exist_ok=True)

    start = datetime.date(2026, 11, 6)   # 00 篇的日期，逐篇 +1 天，让系列按时间线排
    problems = []

    for i, prefix in enumerate(sorted(META)):
        slug, title, tags, desc = META[prefix]
        src = next(SRC.glob(f'{prefix}-*.md'), None)
        if src is None:
            problems.append(f'{prefix}: 源文件缺失')
            continue

        s = src.read_text(encoding='utf-8')
        for old, new in SANITIZE:
            s = s.replace(old, new)
        s = SERIES_QUOTE.sub('', s)
        s = NUMBERED_H1.sub('', s)
        s = annotate_code_blocks(s)
        s = s.lstrip('\n')

        # 校验脱敏与清理彻底
        for bad in ('aicoda', '10.240.', '本分册属于', '../GPU集群'):
            if bad in s:
                problems.append(f'{prefix}: 仍含 {bad!r}')

        date = start + datetime.timedelta(days=i)
        tags_yaml = ', '.join(f'"{t}"' for t in tags)
        fm = (f'---\n'
              f'title: "{title}"\n'
              f'date: {date.isoformat()}T09:00:00+08:00\n'
              f'draft: false\n'
              f'description: "{desc}"\n'
              f'tags: [{tags_yaml}]\n'
              f'series: "{SERIES}"\n'
              f'---\n\n')
        (DST / f'{slug}.md').write_text(fm + s, encoding='utf-8')
        print(f'  ✓ {slug}.md  ({len(s)//1024} KB, {date.isoformat()})')

    if problems:
        print('\n❌ 有问题:')
        for p in problems:
            print('   ', p)
        sys.exit(1)
    print(f'\n✅ 共导入 {len(META)} 篇到 {DST}')


if __name__ == '__main__':
    main()
