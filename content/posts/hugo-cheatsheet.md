---
title: "Hugo 写作速查"
date: 2026-11-16T10:00:00+08:00
draft: false
tags: ["Hugo", "Markdown"]
description: "自己常忘的 front matter 字段和命令，记在这里备查。"
---

## 常用命令

```bash
hugo new posts/title.md      # 按 archetypes/default.md 建草稿
hugo server -D               # 本地预览，-D 包含草稿
hugo server -D --navigateToChanged
hugo --minify                # 生产构建
hugo list drafts             # 列出所有还是草稿的文章
```

## Front matter 字段

```yaml
---
title: "文章标题"
date: 2026-11-16T10:00:00+08:00
lastmod: 2026-11-17T09:00:00+08:00   # 有修改时手动更新
draft: false
weight: 10                            # 同日期时排序，小的在前
tags: ["Go", "Linux"]
description: "列表页显示的一句话摘要，不写则自动截取正文"
cover:
  image: "/images/cover.png"          # 放 static/images/ 下
  alt: "封面图说明"
  relative: false
ShowToc: true                         # 单篇覆盖全局配置
TocOpen: true                         # 默认展开目录
hideSummary: false
---
```

## 时区

日期一定带时区偏移（`+08:00`）。不带的话 Hugo 按 UTC 解析，可能出现「今天写的文章显示成昨天」，进而影响首页排序。

## 图片

放在 `static/images/` 下，用绝对路径引用：

```markdown
![说明](/images/foo.png)
```

`static/` 里的文件会原样拷到站点根，不经过处理，路径就是 `/images/foo.png`。

## 代码块

带语言标识才能正确高亮：

````markdown
```bash
kubectl get pods -A
```
````

`hugo.toml` 里 `guessSyntax = true` 会尝试猜没标语言的代码块，但别依赖它。

## 折叠块（原生 HTML）

`unsafe = true` 已开启，可以直接写 HTML：

```markdown
<details>
<summary>点开看完整日志</summary>

日志内容，注意上下要留空行才会被当 Markdown 解析。

</details>
```
