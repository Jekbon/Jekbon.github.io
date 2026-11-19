---
title: "一台 4 核 8G 服务器跑 Nexus + Jenkins + Authentik 统一认证平台"
date: 2026-11-16T09:00:00+08:00
draft: false
description: "小团队统一认证与 DevOps 工具链的完整部署：三件套容器编排与资源限额、共享 PostgreSQL、LDAP Outpost 对接 Jenkins 与 Nexus、35 个代理仓库规划、备份与恢复。"
tags: ["Jenkins", "Nexus", "Authentik", "Docker", "LDAP"]
series: "统一认证与 DevOps 工具链"
---

> 硬件前提：一台 Ubuntu 22.04、4 核、7.7 GB 内存、787 GB 数据盘的服务器。
> 小团队（<50 人）不需要三台机器，一台就够——前提是资源限额算清楚。

## 架构总览

![单机部署架构：Jenkins 与 Nexus 经 LDAP 接入 Authentik Outpost，Authentik 与 Nexus 共享一个仅本机访问的 PostgreSQL 实例](/images/sso-arch.svg)

两个设计决策值得说明：

- **共享一个 PostgreSQL 实例**，建 `nexus` 和 `authentik` 两个库。
  小机器上跑两个独立数据库实例是浪费，共享实例 + 分库是合理的折中；
  备份脚本一次 `pg_dump` 两个库即可
- **LDAP Outpost 是 Authentik 的组件**，不是独立 LDAP 服务器。
  Jenkins/Nexus 走标准 LDAP 协议对接它，从而不需要改应用侧的 OIDC 配置
  也能接入统一认证（OIDC 路线见本系列排障篇）

## 资源限额：7.7G 内存怎么分

| 服务 | 内存上限 | 实测使用 | CPU 上限 | 磁盘 |
|---|---|---|---|---|
| Nexus | 3G | ~2.2G | 2 核 | 7.1G (blobs) |
| PostgreSQL | 512M | ~143M | 0.5 核 | 87M |
| Jenkins | 2.5G | ~1.1G | 1.5 核 | 17G |
| Authentik Server | 512M | ~370M | 0.5 核 | - |
| Authentik Worker | 512M | ~452M | 0.5 核 | - |
| Authentik Redis | 128M | ~5M | 0.25 核 | - |
| LDAP Outpost | 128M | ~30M | 0.25 核 | - |
| **合计** | **~7.3G** | **~4.3G** | **5.5 核** | **~24G** |

限额总和（7.3G）接近物理内存（7.7G）是**故意的**：实测使用只有 4.3G，
限额是防雪崩的天花板而非预留。但 Authentik Server 给 512M 是错的——
登录慢、worker 被强杀的根因就是它，详见本系列排障篇，应给到 1G。

## Compose 配置全文

三个文件都可以直接复制使用。规则只有一条：**所有 `CHANGE_ME_` 开头的值必须替换**
（两个数据库密码 + Authentik secret key，后者用 `openssl rand -base64 36` 生成）。
镜像已换回公共源地址，路径按你的磁盘布局调整。

### nexus/docker-compose.yml（Nexus + 共享 PostgreSQL）

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: nexus-postgres
    restart: unless-stopped
    stop_grace_period: 30s
    command: >
      postgres
      -c shared_buffers=256MB
      -c effective_cache_size=384MB
      -c work_mem=8MB
      -c maintenance_work_mem=128MB
      -c wal_buffers=8MB
      -c max_connections=200
      -c random_page_cost=1.1
      -c synchronous_commit=off
      -c min_wal_size=512MB
      -c max_wal_size=2GB
      -c checkpoint_timeout=15min
    environment:
      TZ: Asia/Shanghai
      POSTGRES_DB: nexus
      POSTGRES_USER: nexus
      POSTGRES_PASSWORD: CHANGE_ME_pg_nexus
      POSTGRES_INITDB_ARGS: --encoding=UTF8 --locale=C
    volumes:
      - /app/postgres/data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"     # 数据库只暴露给本机
    deploy:
      resources:
        limits: { memory: 512m, cpus: "0.5" }
        reservations: { memory: 128m }
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U nexus -d nexus"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  nexus:
    image: sonatype/nexus3:3.93.2
    container_name: nexus
    restart: unless-stopped
    stop_grace_period: 60s
    stop_signal: SIGTERM
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      TZ: Asia/Shanghai
      INSTALL4J_ADD_VM_PARAMS: >-
        -Xms2g -Xmx2g
        -XX:MaxDirectMemorySize=1200m
        -XX:+HeapDumpOnOutOfMemoryError
        -XX:HeapDumpPath=/nexus-data/heapdump.hprof
        -Xlog:gc*:file=/nexus-data/log/gc.log:time,uptime,level,tags:filecount=5,filesize=10m
        -Djava.util.prefs.userRoot=/nexus-data/.java
    ports:
      - "8081:8081"
      - "8082:8082"
      - "8083:8083"
    volumes:
      - /app/nexus:/nexus-data
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8081/service/rest/v1/status"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 120s
    deploy:
      resources:
        limits: { memory: 3g, cpus: "2.0" }
        reservations: { memory: 2g }
    ulimits:
      nofile: { soft: 65536, hard: 65536 }
      nproc: { soft: 4096, hard: 4096 }
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "3" }
```

### authentik/docker-compose.yml

```yaml
services:
  authentik-server:
    image: ghcr.io/goauthentik/server:2025.6.4
    container_name: authentik-server
    restart: unless-stopped
    stop_grace_period: 30s
    command: server
    environment:
      AUTHENTIK_SECRET_KEY: "CHANGE_ME_authentik_secret_key"   # openssl rand -base64 36
      AUTHENTIK_POSTGRESQL__HOST: nexus-postgres
      AUTHENTIK_POSTGRESQL__PORT: "5432"
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: "CHANGE_ME_pg_authentik"
      AUTHENTIK_REDIS__HOST: authentik-redis
      AUTHENTIK_REDIS__PORT: "6379"
      AUTHENTIK_ERROR_REPORTING__ENABLED: "false"
      AUTHENTIK_LOG_LEVEL: info
      AUTHENTIK_WEB__WORKERS: "4"    # 默认 2 在小机器上不够，见排障篇
    volumes:
      - /app/authentik/media:/media
      - /app/authentik/custom-templates:/templates
      - /app/authentik/certs:/certs
      - /var/run/docker.sock:/var/run/docker.sock
    ports:
      - "9000:9000"
      - "9443:9443"
    deploy:
      resources:
        limits: { memory: 1g, cpus: "2.0" }   # 512m 会拖慢登录，见排障篇
        reservations: { memory: 128m }
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:9000/-/health/live/"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    logging:
      driver: json-file
      options: { max-size: "30m", max-file: "3" }

  authentik-worker:
    image: ghcr.io/goauthentik/server:2025.6.4
    container_name: authentik-worker
    restart: unless-stopped
    stop_grace_period: 30s
    command: worker
    environment:
      # 与 server 相同的 AUTHENTIK_* 变量（SECRET_KEY / POSTGRESQL / REDIS）
      AUTHENTIK_SECRET_KEY: "CHANGE_ME_authentik_secret_key"
      AUTHENTIK_POSTGRESQL__HOST: nexus-postgres
      AUTHENTIK_POSTGRESQL__PORT: "5432"
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: "CHANGE_ME_pg_authentik"
      AUTHENTIK_REDIS__HOST: authentik-redis
      AUTHENTIK_REDIS__PORT: "6379"
      AUTHENTIK_ERROR_REPORTING__ENABLED: "false"
    volumes:
      - /app/authentik/media:/media
      - /app/authentik/custom-templates:/templates
      - /app/authentik/certs:/certs
    deploy:
      resources:
        limits: { memory: 1g, cpus: "2.0" }
        reservations: { memory: 128m }
    logging:
      driver: json-file
      options: { max-size: "30m", max-file: "3" }

  authentik-redis:
    image: redis:7.0.15
    container_name: authentik-redis
    restart: unless-stopped
    command: redis-server --save 60 1 --loglevel warning --maxmemory 128mb --maxmemory-policy allkeys-lru
    volumes:
      - /app/authentik/redis-data:/data
    deploy:
      resources:
        limits: { memory: 128m, cpus: "0.25" }
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  # LDAP Outpost：让 Jenkins/Nexus 这类只懂 LDAP 的应用接入统一认证
  authentik-ldap-outpost:
    image: ghcr.io/goauthentik/ldap:2025.6.4
    container_name: authentik-ldap-outpost
    restart: unless-stopped
    environment:
      AUTHENTIK_HOST: http://authentik-server:9000
      AUTHENTIK_TOKEN: "CHANGE_ME_outpost_token"   # Authentik 后台生成
    ports:
      - "3389:3389"
      - "6636:6636"
    deploy:
      resources:
        limits: { memory: 128m, cpus: "0.25" }
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

networks:
  default:
    name: nexus_default
    external: true
```

### jenkins/docker-compose.yml

```yaml
services:
  jenkins:
    image: jenkins/jenkins:2.535-jdk17
    container_name: jenkins
    restart: always
    stop_grace_period: 30s
    stop_signal: SIGTERM
    ports:
      - "80:8080"
      - "50000:50000"
    environment:
      TZ: Asia/Shanghai
      JAVA_OPTS: >-
        -Duser.timezone=Asia/Shanghai
        -Xms1g -Xmx2g
        -XX:+UseG1GC
        -XX:MaxGCPauseMillis=200
        -XX:+HeapDumpOnOutOfMemoryError
        -XX:HeapDumpPath=/var/jenkins_home/heapdump.hprof
    group_add:
      - "998"
    volumes:
      - /app/jenkins:/var/jenkins_home
      - /var/run/docker.sock:/var/run/docker.sock
      - /usr/bin/docker:/usr/bin/docker
      - /usr/libexec/docker/cli-plugins:/usr/libexec/docker/cli-plugins
      - /usr/bin/envsubst:/usr/bin/envsubst
      - /usr/bin/kubectl:/usr/bin/kubectl
    deploy:
      resources:
        limits: { memory: 2.5g, cpus: "1.5" }
        reservations: { memory: 1g }
    ulimits:
      nofile: { soft: 65536, hard: 65536 }
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "3" }
```

几个值得注意的点：

- PostgreSQL 的 ports 绑定 `127.0.0.1`，数据库不进网络暴露面
- Jenkins 挂载宿主 docker.sock 与 docker CLI（Docker-out-of-Docker），
  `group_add: 998` 是宿主 docker 组 GID，按你的系统调整
- Jenkins 的 LDAP 等启动配置放 `init.groovy.d`，容器重建后自动恢复——
  这是容器化 Jenkins 的关键习惯，配置不落在容器内

## LDAP 集成三处配置

**Authentik 侧**（LDAP Provider）：Base DN `dc=example,dc=local`、
Bind/Search Mode 均 direct、UID/GID 起始 2000/4000、绑定到
`ldap-authentication` Application。

**Nexus 侧**（`Server administration → Security → LDAP`）：

| 配置项 | 值 |
|---|---|
| 服务器 | `ldap://10.0.0.58:3389` |
| Search Base | `dc=example,dc=local` |
| Auth Username | `cn=svc-ldap-bind,ou=users,dc=example,dc=local` |
| User Base DN / Object Class | `ou=users` / `person` |
| User ID / Real Name / Email | `sAMAccountName` / `cn` / `mail` |
| 启用的 Realms | NexusAuthenticatingRealm, LdapRealm, NexusAuthorizingRealm |

**Jenkins 侧**（`init.groovy.d/ldap-config.groovy`，每次启动执行）：

```groovy
server:           '10.0.0.58:3389'
rootDN:           'dc=example,dc=local'
managerDN:        'cn=svc-ldap-bind,ou=users,dc=example,dc=local'
managerPassword:  '<从密钥管理获取，不入库不入文档>'
userSearchBase:   'ou=users'
userSearch:       'sAMAccountName={0}'
groupSearchBase:  'ou=groups'
displayNameAttr:  'cn'
mailAttr:         'mail'
```

注意 `managerDN` 用只读服务账号而非超管——绑定凭据会落在每个接入方的配置里，
用超管等于把 SSO 管理权散落各处。理由和做法见本系列用户管理篇。

## Nexus 仓库规划：35 个仓库完整清单

全部指向公共源，内网统一走 Nexus 出口。加粗的 group 仓库是客户端实际配置的地址。

### Maven（5）

| 名称 | 类型 | 远程地址 / 成员 |
|---|---|---|
| maven-releases | hosted | - |
| maven-snapshots | hosted | - |
| maven-central | proxy | https://repo1.maven.org/maven2/ |
| maven-aliyun | proxy | https://maven.aliyun.com/repository/public |
| **maven-public** | **group** | maven-releases, maven-snapshots, maven-aliyun, maven-central |

### NPM（3）

| 名称 | 类型 | 远程地址 / 成员 |
|---|---|---|
| npm-hosted | hosted | - |
| npm-proxy | proxy | https://registry.npmmirror.com |
| **npm-group** | **group** | npm-hosted, npm-proxy |

### Go（3）

| 名称 | 类型 | 远程地址 / 成员 |
|---|---|---|
| go-hosted | hosted | - |
| go-proxy | proxy | https://goproxy.cn |
| **go-group** | **group** | go-hosted, go-proxy |

### NuGet（4）

| 名称 | 类型 | 远程地址 / 成员 |
|---|---|---|
| nuget-hosted | hosted | - |
| nuget.org-proxy | proxy | https://api.nuget.org/v3/index.json |
| nuget-huaweicloud | proxy | https://repo.huaweicloud.com/repository/nuget/v3/index.json |
| **nuget-group** | **group** | nuget-hosted, nuget-huaweicloud, nuget.org-proxy |

### PyPI（3）

| 名称 | 类型 | 远程地址 |
|---|---|---|
| pypi-proxy | proxy | https://pypi.org/ |
| pypi-aliyun | proxy | https://mirrors.aliyun.com/pypi/simple/ |
| pypi-tsinghua | proxy | https://pypi.tuna.tsinghua.edu.cn/simple/ |

### Alpine / Raw（8）

| 名称 | 类型 | 远程地址 |
|---|---|---|
| alpine-proxy | raw-proxy | https://dl-cdn.alpinelinux.org/alpine/ |
| alpine-raw-proxy-aliyun | raw-proxy | https://mirrors.aliyun.com/alpine/ |
| alpine-raw-proxy-tsinghua | raw-proxy | https://mirrors.tuna.tsinghua.edu.cn/alpine/ |
| alpine-raw-proxy-official | raw-proxy | https://dl-cdn.alpinelinux.org/alpine/ |
| **alpine-group** | **group** | aliyun, tsinghua, official |
| github-api-proxy | raw-proxy | https://api.github.com |
| vscode-extensions | raw-proxy | https://marketplace.visualstudio.com |
| vscode-extensions-hosted | raw-hosted | - |

### Ubuntu APT（9）

apt-proxy 必须填 Distribution 字段，按仓库名对应：

| 名称 | 类型 | 远程地址 | Distribution |
|---|---|---|---|
| ubuntu-aliyun-noble | apt-proxy | http://mirrors.aliyun.com/ubuntu/ | noble |
| ubuntu-aliyun-noble-updates | apt-proxy | http://mirrors.aliyun.com/ubuntu/ | noble-updates |
| ubuntu-aliyun-noble-security | apt-proxy | http://mirrors.aliyun.com/ubuntu/ | noble-security |
| ubuntu-tsinghua-noble | apt-proxy | https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ | noble |
| ubuntu-tsinghua-noble-updates | apt-proxy | https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ | noble-updates |
| ubuntu-tsinghua-noble-security | apt-proxy | https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ | noble-security |
| ubuntu-noble | apt-proxy | http://archive.ubuntu.com/ubuntu/ | noble |
| ubuntu-noble-updates | apt-proxy | http://archive.ubuntu.com/ubuntu/ | noble-updates |
| ubuntu-noble-security | apt-proxy | http://archive.ubuntu.com/ubuntu/ | noble-security |

规划要点：

- **APT / Alpine 这类系统源也走 Nexus**，装机和容器构建才能完全内网化
- 每个生态用 group 聚合多个 proxy，客户端只配 group 地址；
  group 内成员顺序即回退顺序，国内源放前面、官方源兜底
- 同一远程源按 mirror 拆多个 proxy（aliyun / tsinghua / official），
  某个 mirror 故障时 group 自动回退到下一个
- APT 按发行版代号 + 组件拆仓库（noble / noble-updates / noble-security），
  升级大版本时新增一套而非改旧的，旧构建仍可复现

## 备份与恢复

```cron
0 2 * * * /app/nexus/db/backup.sh >> /app/nexus/db/backup/backup.log 2>&1
```

| 备份项 | 方式 | 说明 |
|---|---|---|
| PostgreSQL（nexus + authentik 两库） | `pg_dump -Fc \| gzip` | 在线备份，无需停服 |
| Nexus 配置目录 | `tar czf` | 含 SSL 证书等配置 |

恢复：

```bash
gunzip -c /app/nexus/db/backup/nexus.pg_dump.YYYYMMDD_HHMMSS.gz | \
  docker exec -i nexus-postgres pg_restore -U nexus -d nexus --clean
tar xzf /app/nexus/db/backup/nexus.etc.YYYYMMDD_HHMMSS.tar.gz -C /app/nexus/
cd /app/nexus && docker compose restart
```

保留最近 7 天自动清理。**Nexus 的 blobs（制品本体）不在这个备份里**——
7.1G 的制品要么单独 rsync，要么接受「可从公共源重新拉取」而不备。
这个取舍要明确写进 Runbook，否则恢复时会发现制品全丢。

## 本文不包含什么

管理员凭据、员工账号清单与 Secret Key 不在本文。
用户与凭据的管理方法论见[用户管理篇](/posts/sso-03-authentik-user-ops/)，
登录链路的排障见[排障篇](/posts/sso-02-jenkins-authentik-oidc-troubleshooting/)。
