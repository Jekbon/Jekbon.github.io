---
name: doc-to-blog
description: 把内部技术文档/笔记整理成 Jekbon.github.io（Hugo + rusty-poison）博客文章的完整流程：敏感扫描与脱敏、Markdown 规范化（表格/围栏/引用块四大坑）、ASCII 架构图转手写 SVG、本地预览与 headless Chrome 验证、提交发布。当用户说"整理成博客/发博客/把某目录的文档放进站点/读一下 xx 整理成文章"时使用。
---

# 内部文档 → 博客文章

目标站点：`/Users/wangbin/devops/github/Jekbon.github.io`，public GitHub Pages。
可复用的规范化函数在仓库 `scripts/import-gpu-ops.py`
（`fix_table_delimiters` / `separate_quote_and_table` / `reattach_orphan_table_rows` /
`annotate_code_blocks` / `guess_lang`），新批次优先复用或照抄其逻辑。

## 0. 环境与发布约定

- PATH 必须 `export PATH="$PWD/node_modules/.bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"`。
  brew 的 node v23 CA 库损坏，所有 HTTPS 都报 `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`；
  v22 在 `~/.local/bin`。详见 memory `mac-dual-node-brew-ca-broken`
- 本地预览**只能** `npm run preview`（本地 baseURL）。直接 `npm run build` 后开静态服务
  会白屏：主题 head 里有 `<base href="{{ .Permalink }}">`，root-relative 资源全被拼到线上域
- 提交作者 wangbin，**不写 Co-Authored-By trailer**（用户明确要求）
- 推送后 `gh run watch <id> --interval 5`，再轮询线上直到新内容命中

## 1. 先扫描再动笔（public 站点，脱敏是硬门槛）

对源目录 grep 这些模式，逐条处置：

| 类别 | 处置 |
|---|---|
| 活服务凭据（API key/secret/密码） | 占位 `CHANGE_ME_*`；**动态生成模板**（如 `openssl rand -hex 24`）保留作好实践示范 |
| 内网 IP | → `10.0.0.x` |
| 公网 IP | → RFC5737 示例段 `203.0.113.0/24` 或删除；**扫描者截断段**（85.217.x）可保留作安全情报 |
| 公司域名 / 内部 Harbor / GitLab 路径 | → `example.com` / 公共镜像地址 |
| **员工姓名、账号、邮箱（PII）** | **整节丢弃**，比密码更不该公开；示例改用 zhangsan/lisi |
| 子仓库 `.git/config` 的 remote | 绝不进博客（暴露内部仓库组） |
| 主机名含机房/机柜编号 | 泛化或删除 |
| 对话残留口吻（"把输出发我""按你实际的"） | 清成陈述式 |
| 公司关联词（"公司生产化实践"） | 改"经生产验证"等中性表述 |

发现活凭据时**在回复里明确列轮换清单**（博客脱敏 ≠ 源头安全）。
源文档若自带 checklist 写着"密码已脱敏"而实际没有，照实扫描，不信文档自述。

## 2. Markdown 规范化（Hugo/GFM 的坑，全部真实踩过）

1. **表格分隔行列数必须等于表头列数**，否则整表退化成段落，smart typography 再把
   `---` 转成 `—`，看起来像"全角破折号问题"，实际是列数。重写分隔行用
   `'|' + '---|' * n`，**不是** `'|---|' * n`（后者拼出 `|---||---|` 双竖线空单元格）
2. **引用块后紧跟表格行会被 lazy continuation 吸收进引用** → 中间插空行
3. **被引用块/空行打断的表格行沦为孤儿段落** → 接回打断它的那张表末尾
4. 代码块语言保守推断：bash/yaml/json/ini 有高把握才标，其余标 `text`。
   **不开全局 `guessSyntax`**——它把 ASCII 框线图猜成 `gdscript3` 还带 `class=err`
5. 围栏配对必须带 `in_code` 状态：有标识块的**结束围栏也是空标识** ```` ``` ````，
   不分状态跟踪会把块间正文吞成代码块、真代码块错位（症状：产物代码块数远少于源围栏数/2）
6. front matter：`title / date(带 +08:00，别写未来时间，buildFuture=false 静默吞) /
   description / tags / series`；删掉与 title 重复的正文一级标题；
   删 `> 本册属于…见 ../x.md` 这类死链引用块
7. 正则字符类里 `-` 放最后：`[\s:|-]`。写 `[\s:-|]` 会被解析成 ASCII 范围 58-124，
   反而不含连字符（曾导致表格检测全空）
8. 系列文章日期逐日递增构成时间线；中文 series 名可用（urlize 的 GetPage bug
   已在覆盖模板修掉，见 `scripts/relocalize.py`）

## 3. 图：带右框的 ASCII 架构图 → 手写 SVG

- **不用 ASCII**：中文在等宽字体占 2 宽且读者字体不可控，右框 `│` 必错位
- **不用 mermaid**：本主题下节点标签 `<br/>` 不生效（文字挤成一坨）、图按原始尺寸
  缩在容器里字极小、svg-pan-zoom 缩放不直观。嵌套 subgraph 还会被 dagre 排成大片空白
- SVG 规格（与存量 12 张一致，见 `static/images/`）：viewBox 宽 960；背景
  `#16171a` rx12；节点 `.box` fill `#1c1d20` stroke `#e25822` 1.4 rx6；分区 `.zone`
  虚线 `#4a4b50`；连线 `.ln` `#8a8a8a` 1.4 + marker 箭头；文字 `.t` 15px `#e6e6e6`、
  `.ts` 12.5px `#a4a4a4`、`.ttl` 16px 600 `#e25822`；
  font-family `Inter, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
- 放 `static/images/`，正文 `![完整描述的 alt](/images/x.svg)`（alt 进 Pagefind 索引）；
  主题 lightbox 点击图片放大，解决"放大不方便"
- **决策树 / 时序 / 目录结构保留文本**：无右框错位问题，且可复制可搜索
- 代码块里混着的说明文字（如"三者关系""时序 T0-T5"）转成正文列表，别随块删除

## 4. 验证：grep HTML 不算数，必须真渲染

- 截图：`chrome --headless=new --disable-gpu --screenshot=/tmp/x.png --window-size=1280,H
  --virtual-time-budget=6000 URL`。**锚点滚动在 headless 不生效**；要看深层内容就加大 H，
  或临时造一篇开头即目标内容的测试文章（截完删除）
- 控制台：加 `--enable-logging=stderr --dump-dom`，grep
  `CONSOLE.*(error|blocked|OTS)|net::ERR`
- minify 去属性引号：grep 用 `class=chroma`、`placeholder=`，**不带引号**，否则假阴性
- 脱敏终检：对 `content/` 和 `public/` 同时 grep 全部敏感模式，要求 0 命中
- 读者要复制即用的内容（compose/YAML）用解析器验：
  `ruby -ryaml -e 'YAML.load_file(ARGV[0])'`（Mac 自带 ruby 无 pyyaml 时）
- "验证通过"前问自己：这次是看了渲染结果，还是只看了文本？白屏和字体损坏都只有渲染能发现

## 5. 内容取舍判断

- 纯硬件清单/环境记录不单独成文，精简并入相关文的"环境基线"节
- 凭据表、人员清单整节丢弃时在文末留一句事实性说明（"凭据不在本文"），
  **不写说教/评论性文字**（用户明确不要）
- 事故复盘保留第一人称与前后对比数据，这是文章最值钱的部分
- 源文档若与已发文章重叠（如特定服务器的部署记录），不成文，避免稀释
