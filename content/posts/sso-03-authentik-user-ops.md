---
title: "把 Authentik 推广给全公司之前：安全加固、分组设计与入职离职 SOP"
date: 2026-11-18T09:00:00+08:00
draft: false
description: "一套自建 SSO 从「能跑」到「敢推广」的差距：重复用户清理、LDAP 绑定账号降权、密码策略、首次登录强制改密、两组覆盖 50 人团队、入职离职流程与管理员 CLI 速查。"
tags: ["Authentik", "SSO", "LDAP", "安全"]
series: "统一认证与 DevOps 工具链"
---

> 背景：一套 Authentik（Server + Worker + Redis + LDAP Outpost）已部署完成，
> Jenkins 与 Nexus 已通过 LDAP 对接，33 个用户手动创建。
> 团队规模 <50 人、研发约 25 人。本文记录从「能用」到「敢全员推广」之间要补的课。

## 一、现状盘点：六个问题

| 类别 | 问题 | 风险 |
|---|---|---|
| 用户数据 | 同一人存在多个历史账号（如 `zhangsan` / `zhangsan2` / `zs2024` 实为一人），重复且无从判断哪个在用 | 中 |
| 密码安全 | 所有用户统一默认密码，无复杂度要求、无过期机制 | 高 |
| 权限过大 | Jenkins/Nexus 的 LDAP Bind 使用超级管理员账号，应换只读服务账号 | 高 |
| 无标准流程 | 新用户创建全靠管理员命令行，无入职/离职 SOP | 高 |
| 无自助服务 | 员工不能自助改密/找回，全部依赖管理员 | 中 |
| 文档安全 | 部署文档明文记录所有密码与 Secret Key | 中 |

## 二、安全加固（推广前必须完成）

### 1. 清理重复用户

在 `Directory → Users` 排查。原则：**不直接删除，先标记 inactive**，保留审计记录。

1. 确认员工实际在用的用户名
2. 多余账号标记为 inactive
3. 通知员工使用保留账号登录

### 2. 创建专用 LDAP 绑定账号

用只读服务账号替代超管做 LDAP 绑定：

```text
用户名:  svc-ldap-bind
姓名:    LDAP Bind Service
邮箱:    svc-ldap-bind@example.local
密码:    <24 位随机强密码>
权限:    仅 LDAP 查询，无管理后台权限
```

创建后更新两处消费方配置：

Nexus（`Server administration → Security → LDAP`）：

```text
Auth Username: cn=svc-ldap-bind,ou=users,dc=example,dc=local
Auth Password: <新密码>
```

Jenkins（`init.groovy.d/ldap-config.groovy`）：

```groovy
managerDN:       'cn=svc-ldap-bind,ou=users,dc=example,dc=local'
managerPassword: '<新密码>'
```

改完重启 Jenkins 生效。**这一步的意义**：LDAP 绑定凭据会出现在多个应用的配置里，
用超管账号等于把 SSO 的管理权散落到每个接入方的配置文件里。

### 3. 配置密码策略

`Flows and Stages → Policies → Password`：最小长度 8、大小写+数字+特殊字符必填、
启用内置弱密码字典。将该 Policy 绑定到 `default-password-change` Flow。

### 4. 强制首次登录改密

确认 `default-authentication-flow` 的 Stages 里包含 password change stage。
新用户用临时密码首次登录时会被强制跳转改密页——这是默认密码方案唯一安全的前提。

## 三、分组设计：两个组够用

50 人团队不需要细粒度分组，过度设计只会增加维护成本：

| 组名 | 成员 | Jenkins 权限 | Nexus 权限 |
|---|---|---|---|
| `admins` | 运维/技术负责人（2~3 人） | 管理员 | 管理员 |
| `developers` | 全体研发（~25 人） | 开发权限 | 读写 |

后续需要区分测试、产品等角色时再加 `qa-team`、`viewers`。
创建后在 `Applications → Jenkins/Nexus → Policy` 按组配访问规则。

## 四、入职与离职流程

入职六步：

```text
1. 管理员登录 Authentik 后台
2. Directory → Users → Create
   用户名拼音全拼 / 姓名 / 邮箱 / 随机临时密码
   勾选「需要首次登录修改密码」
3. 加入 developers 组
4. 按模板通知员工（登录地址 + 用户名 + 临时密码 + 改密要求）
5. 员工首次登录 → 被强制改密
6. 同一账号即可登录 Jenkins 和 Nexus
```

离职三步，核心原则是**禁用不删除**：

```text
1. Directory → Users → 找到用户
2. Deactivate（禁用，不删除）
   - 保留账号用于审计追溯
   - 立即无法登录所有接入应用
3. 从所有用户组移除
```

直接删除用户会丢失审计链路，也让历史日志里的用户名变成无主字符串。

## 五、自助服务

- **自助改密**：登录后右上角头像 → My applications → Password
- **自助找回**：启用 `default-recovery` Flow，前提是配好 SMTP
  （`System → Email`，587 STARTTLS 或 465 SSL）。
  暂时不配也可以——25 人团队忘记密码找管理员重置，工作量很小，别为小团队提前上复杂度
- **MFA**：初期不强制，会增加抵触。等团队习惯 SSO 后再逐步引入 TOTP

## 六、推广节奏

小团队推广可以很快，但要留过渡期：

**第 1~2 天（准备）**：清理重复用户；创建 `svc-ldap-bind` 并替换两处 LDAP 绑定；
配密码策略 + 首次登录强制改密；建两个组并把现有用户分组；
用 2~3 个普通账号实测 Jenkins 和 Nexus 登录。

**第 3 天（全员）**：IM 群发通知附登录地址与操作说明；设 3 天过渡期协助未完成首登的员工；
确认所有人改完默认密码。

**后续**：配 SMTP；按需接入更多内部应用（GitLab、Grafana 等）；
团队过百人再考虑 MFA 与目录自动同步。

## 七、管理员 CLI 速查

Web UI 适合偶尔操作，批量和脚本场景用 Django shell 更快：

```bash
# 创建用户
docker exec authentik-server python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'authentik.root.settings')
django.setup()
from authentik.core.models import User
u = User.objects.create(username='新用户名', email='user@example.local', name='姓名')
u.set_password('临时强密码')
u.save()
print('User created:', u.username)
"
```

```bash
# 重置密码 / 禁用用户（离职）
docker exec authentik-server python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'authentik.root.settings')
django.setup()
from authentik.core.models import User
u = User.objects.get(username='用户名')
u.is_active = False          # 或 u.set_password('新临时密码')
u.save()
print('Done:', u.username)
"
```

```bash
# 查看活跃用户
docker exec authentik-server python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'authentik.root.settings')
django.setup()
from authentik.core.models import User
for u in User.objects.filter(is_active=True).order_by('username'):
    print(f'{u.username:20s} {u.name:15s} {u.email}')
" 2>&1 | grep -v '^{'
```

## 八、推广前检查清单

- [ ] 重复用户已清理（inactive 而非删除）
- [ ] LDAP 绑定已切换为 `svc-ldap-bind`，超管不再出现在任何应用配置里
- [ ] 密码策略已配置（复杂度 + 首次登录强制修改）
- [ ] `admins` / `developers` 组已创建，现有用户已分组
- [ ] 用普通员工账号实测 Jenkins 与 Nexus 登录
- [ ] 员工自助改密路径确认可用
- [ ] 超管密码已改为强密码且仅管理员知晓
- [ ] **部署文档中的密码与 Secret Key 已脱敏或限制访问**
