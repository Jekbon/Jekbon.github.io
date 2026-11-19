---
title: "Jenkins 走 Authentik OIDC 登录慢到 1 分钟：九个发现与一次自造事故"
date: 2026-11-17T09:00:00+08:00
draft: false
description: "从 1 分钟+ 的首次登录优化到 5~8 秒：Authentik 资源不足、access token 5 分钟、Clash fake-ip 劫持内网、Flow Executor 固有 2 秒过载，以及一次把所有人锁在门外的 timedelta 事故。"
tags: ["Jenkins", "Authentik", "OIDC", "排障"]
series: "统一认证与 DevOps 工具链"
---

## 问题现象

1. Jenkins 跳转 Authentik 登录很慢，首次登录 1 分钟以上
2. 登录几个小时后就被踢出重登，重登同样慢

两个现象其实是同一条链路上的不同环节出了问题。下面按排查顺序记录九个发现，
以及每一项对应的优化和实测数据。

## 发现一：Jenkins 实际走的是 OIDC，不是文档写的 LDAP

部署文档记录的是 LDAP 认证，`init.groovy.d` 里也躺着 LDAP 配置脚本，
但 Jenkins `config.xml` 的 `securityRealm` 早已被覆盖为 OIDC：

```xml
<securityRealm class="org.jenkinsci.plugins.oic.OicSecurityRealm" plugin="oic-auth@4.609.v9de140f63d01">
  <serverConfiguration class="org.jenkinsci.plugins.oic.OicServerWellKnownConfiguration">
    <wellKnownOpenIDConfigurationUrl>
      http://10.0.0.58:9000/application/o/jenkins-oidc/.well-known/openid-configuration
    </wellKnownOpenIDConfigurationUrl>
  </serverConfiguration>
</securityRealm>
```

先确认真实链路，后面的排查才有意义：

```text
用户访问 Jenkins
  → Jenkins 302 跳转到 Authentik /application/o/authorize/
  → 用户在 Authentik 登录
  → Authentik 回调 Jenkins /securityRealm/finishLogin
  → Jenkins 向 Authentik /application/o/token/ 请求 token
  → 登录完成
```

OIDC 一次登录至少 5 次浏览器跳转，**任何一跳慢都会乘进总耗时**——这是理解后面所有优化的前提。

## 发现二：Authentik Server 资源给得太抠

排查时的容器资源使用：

| 容器 | 内存使用 | 内存限制 | 使用率 | CPU 限制 |
|---|---|---|---|---|
| **authentik-server** | **464.6 MiB** | **512 MiB** | **90.75%** | **0.5 核** |
| authentik-worker | 259.8 MiB | 512 MiB | 50.74% | 0.5 核 |
| authentik-ldap-outpost | 20.58 MiB | 256 MiB | 8.04% | 0.25 核 |
| nexus-postgres | 283.7 MiB | 512 MiB | 55.40% | 0.5 核 |
| jenkins | 1.307 GiB | 2.5 GiB | 52.29% | 1.5 核 |

Server 内存余量仅 ~47 MiB，CPU 0.5 核，gunicorn 只有 2 个 worker。
处理登录请求时各步骤耗时 1~5 秒，甚至触发 worker 被强杀：

```text
"event": "WORKER TIMEOUT (pid:63)"
"event": "Worker (pid:63) was sent SIGKILL! Perhaps out of memory?"
```

## 发现三：token 刷新频繁超时，把用户踢回登录页

Jenkins 日志里 5 天内 **26 次** `SocketTimeoutException`：

```text
WARNING o.j.plugins.oic.OicSecurityRealm#refreshExpiredToken: Failed to refresh expired token
java.net.SocketTimeoutException: Read timed out
  at org.jenkinsci.plugins.oic.OicSecurityRealm.refreshExpiredToken(OicSecurityRealm.java:1307)
  at org.jenkinsci.plugins.oic.OicSecurityRealm.handleTokenExpiration(OicSecurityRealm.java:1223)
```

正常情况下 token 过期后 Jenkins 能静默刷新、用户无感；**刷新一旦超时，
用户就被整个重定向回 Authentik 重新走完整登录流程**。
这就解释了「几小时后要重登且重登很慢」。

## 发现四：access token 有效期只有 5 分钟

Authentik 里 jenkins-oidc Provider 的有效期配置：

```text
access_token_validity  = 5 分钟    ← 每 5 分钟触发一次刷新
refresh_token_validity = 30 天
access_code_validity   = 1 分钟
```

每 5 分钟刷一次新，把发现二（服务端慢）和发现三（刷新超时）的概率同时放大。
这是一个配置问题，却同时制造了「慢」和「被踢」两个症状。

## 发现五：授权流程用了 explicit-consent

Provider 的 `authorization_flow` 配的是
`default-provider-authorization-explicit-consent`，**每次登录都弹授权确认页**，
凭空多一次完整页面跳转。内部工具完全不需要这个确认。

## 发现六：系统 swap 用了 3.5G / 4G

```text
Nexus (java):          1.8 GB swap
Jenkins (java):        550 MB swap
Authentik gunicorn:    ~520 MB swap (master + 4 workers)
celery beat:           190 MB swap
```

`vm.swappiness=60`（默认值），Authentik 进程的内存页被换到磁盘，
每次请求处理都要 swap in，延迟直接叠加。

## 发现七：Clash Verge 的 fake-ip 劫持了内网流量（最大瓶颈）

本机装了 Clash Verge（TUN 模式）。`jenkins.example.com` 的 DNS 被 Clash 的
fake-ip 机制解析成 `198.18.1.80`（Clash 假 IP 段），于是浏览器的所有请求
先绕到海外代理节点，再由代理转发回内网服务器。

实测对比：`ping 10.0.0.58` 只有 15~28ms，但通过代理的浏览器跳转要 12 秒。
OIDC 登录至少 5 次跳转，每次多 2~3 秒，**总计凭空增加 10~15 秒**。

如果你用 TUN 模式的代理客户端且内网服务「莫名其妙地慢」，先查这个。
特征很明显：DNS 解析结果是 `198.18.x.x`。

## 发现八：Authentik Flow Executor 有约 2 秒的固有过载

用 cProfile 分析 `/api/v3/flows/executor/default-authentication-flow/`：

```text
4.4s 总耗时分解:
  2.27s → Django URL 路由解析 (resolve)
  2.19s → Python 模块导入 (importlib._find_and_load, ~500 个模块)
  2.16s → authentik/root/urls.py 加载
```

这是 Authentik 的架构成本：Django URL 路由表要加载约 500 个 Python 模块。
**无法通过配置优化**，只能接受，或者等官方改进。知道「这 2 秒优化不掉」
本身就有价值——否则会一直在这上面浪费时间。

## 发现九：三个额外问题

- **healthcheck 假故障**：authentik-server 容器内没有 `curl`，healthcheck 连续失败
  八万多次、状态显示 `unhealthy`，但服务实际正常
- **groups 未返回**：日志 `Could not identify groups in groups=[]`，
  OIDC 配了 `groupsFieldName=groups` 但 Authentik 没返回组信息，导致无法按组授权
- **issuer URL 不匹配**：well-known 返回 `issuer: http://localhost:9000/...`，
  与 Jenkins 配置的外部地址不一致（应配置 Authentik 的 external host）

## 优化措施与实测效果

### 资源提升

| 配置项 | 修改前 | 修改后 |
|---|---|---|
| 内存 limit | 512m | 1g |
| CPU limit | 0.5 | 2.0 |
| gunicorn workers | 2（默认） | 4（`AUTHENTIK_WEB__WORKERS: "4"`） |

```yaml
  authentik-server:
    environment:
      AUTHENTIK_WEB__WORKERS: "4"
    deploy:
      resources:
        limits:
          memory: 1g
          cpus: "2.0"
```

效果：

```text
内存:  464.6 MiB / 512 MiB (90.75%)  →  369.3 MiB / 1 GiB (36.06%)

服务端各步骤耗时:
                         修改前         修改后
auth flow page:          1.5-2.3s  →   46-75ms
authorize:               1.3s      →   63-148ms
consent flow:            1.8-5.0s  →   43-70ms
token:                   2.4s      →   30ms
密码验证:                4.5s      →   0.5s
```

### access token 有效期 5 分钟 → 24 小时

通过 Authentik 的 Django ORM 修改：

```python
from authentik.providers.oauth2.models import OAuth2Provider
p = OAuth2Provider.objects.get(name='jenkins-oidc')
p.access_token_validity = 'hours=24'   # 必须是字符串格式
p.save()
```

**注意**：`access_token_validity` 是 Authentik 自定义的字符串格式
（`"minutes=5"`、`"hours=24"`、`"days=30"`），**不是** Python 的 `timedelta` 对象。
传 `timedelta` 会怎样？见下一节的事故记录。

### explicit-consent → implicit-consent

跳过每次登录的授权确认页，省一次完整跳转：

```python
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.flows.models import Flow
p = OAuth2Provider.objects.get(name='jenkins-oidc')
p.authorization_flow = Flow.objects.get(slug='default-provider-authorization-implicit-consent')
p.save()
```

### 降低 swappiness

```bash
sysctl vm.swappiness=10                      # 临时
echo 'vm.swappiness=10' >> /etc/sysctl.d/99-swappiness.conf   # 永久
```

### Clash 内网直连（关键优化）

编辑 Clash Verge 的 Merge profile，让内网域名和网段绕过代理：

```yaml
dns:
  use-system-hosts: true
  fake-ip-filter:
    - '*.example.com'
    - 'jenkins.example.com'
  hosts:
    jenkins.example.com: 10.0.0.58

tun:
  route-exclude-address:
    - 10.0.0.0/16
```

也可以走 mihomo 的 unix socket 热更新，不用重启客户端：

```bash
curl -sf --unix-socket /tmp/verge/verge-mihomo.sock \
  -X PATCH 'http://localhost/configs' \
  -H 'Content-Type: application/json' \
  -d '{
    "tun":{"enable":true,"stack":"gvisor","auto-route":true,"strict-route":false,
           "auto-detect-interface":true,"dns-hijack":["any:53"],
           "route-exclude-address":["10.0.0.0/16"]},
    "dns":{"use-system-hosts":true,
           "fake-ip-filter":["*.lan","*.local","*.arpa","*.example.com"],
           "hosts":{"jenkins.example.com":"10.0.0.58"}}
  }'
```

效果：

```text
修改前: jenkins.example.com → 198.18.1.80 (fake-ip) → 海外代理 → 10.0.0.58   (12 秒)
修改后: jenkins.example.com → 10.0.0.58 (直连)                                 (22ms)
```

## 事故记录：timedelta 格式错误把所有人锁在门外

优化 access token 有效期时，我第一版写的是：

```python
# 错误写法
p.access_token_validity = timedelta(hours=24)
p.save()
```

保存成功、没有任何报错。然后 Authentik 的 token 端点 `/application/o/token/`
开始返回 405，**所有 Jenkins 用户无法登录**。日志：

```text
File "/authentik/lib/utils/time.py", line 38, in timedelta_from_string
    key, value = duration_pair.split("=")
builtins.ValueError: not enough values to unpack (expected 2, got 1)
```

ORM 写入时不校验格式，读取解析时才炸——而且炸在认证链路上。
改回字符串格式即恢复：

```python
p.access_token_validity = 'hours=24'
p.save()
```

教训：**改认证系统的配置，先在测试 Provider 上验证，再动生产那个。**
以及，「保存成功」不等于「生效正确」。

## 优化效果总对比

| 步骤 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| flow executor（加载登录表单） | ~2000ms | ~1900ms | Authentik 固有过载，优化不掉 |
| 密码验证（PBKDF2 87 万次） | 4532ms | 567ms | CPU 提升 |
| authorize 跳转 | 1345ms | 73ms | implicit consent |
| consent flow | 4977ms | 跳过 | implicit consent |
| token 端点 | 2448ms | 350ms | 资源提升 |
| jwks 端点 | 774ms | 296ms | 资源提升 |
| userinfo 端点 | 442ms | 23ms | 资源提升 |
| 浏览器回调 Jenkins | **12000ms** | **<1000ms** | **Clash 直连** |

整体：

```text
                        修改前              修改后
─────────────────────────────────────────────────────────
首次登录耗时            1 分钟+             ~5-8 秒
重登频率                每 5 分钟           每 24 小时
token 刷新失败          26 次/5天           0 次
服务端处理              ~15 秒              ~3.5 秒
浏览器回调              ~12 秒              <1 秒
```

## 待处理项

| 项目 | 说明 | 优先级 |
|---|---|---|
| healthcheck 修复 | 容器内无 curl，改用 wget 或 Python 脚本 | 低 |
| authentik-worker 内存 | 可一并调到 1G | 中 |
| groups 未返回 | OIDC groups 为空，Jenkins 无法按组授权 | 中 |
| issuer URL 不匹配 | 配置 Authentik 的 external host | 低 |
| flow executor 2s 延迟 | 架构问题，等官方优化 | 无法修复 |

## 排查命令集

```bash
# 查看 Authentik Provider 的 token 有效期
docker exec authentik-server python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'authentik.root.settings')
django.setup()
from authentik.providers.oauth2.models import OAuth2Provider
for p in OAuth2Provider.objects.all():
    print(p.name, '->', p.access_token_validity, '/', p.refresh_token_validity)
"
```

```bash
# Jenkins 侧 token 刷新失败次数
docker logs jenkins --tail 10000 2>&1 | grep -c 'Failed to refresh expired token'
```

```bash
# Authentik 各端点延迟
curl -sf -o /dev/null -w 'well-known: %{time_total}s\n' \
  'http://localhost:9000/application/o/jenkins-oidc/.well-known/openid-configuration'
curl -sf -o /dev/null -w 'token: %{time_total}s\n' \
  -X POST 'http://localhost:9000/application/o/token/' -d 'grant_type=client_credentials'
```

```bash
# 各进程 swap 占用排行
grep -r VmSwap /proc/*/status 2>/dev/null | awk '{print $2, $1}' | sort -rn | head -10 | \
  while read swap path; do
    pid=$(echo $path | cut -d/ -f3)
    name=$(cat /proc/$pid/comm 2>/dev/null)
    echo "$swap kB  PID=$pid  $name"
  done
```

```bash
# 查看 Clash 当前连接的实际路由（确认内网流量是否还在走代理）
curl -sf --unix-socket /tmp/verge/verge-mihomo.sock 'http://localhost/connections' | \
  python3 -c "
import sys,json
data=json.load(sys.stdin)
for c in data.get('connections',[]):
    dest = c.get('metadata',{}).get('destinationIP','')
    host = c.get('metadata',{}).get('host','')
    if '10.0.0' in dest or 'example' in host:
        print(f'{host or dest}:{c[\"metadata\"].get(\"destinationPort\",\"?\")} rule={c[\"rule\"]} chain={c[\"chains\"]}')
"
```
