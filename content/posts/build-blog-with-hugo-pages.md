---
title: "用 Hugo + GitHub Pages 搭建这个博客"
date: 2026-11-18T09:30:00+08:00
draft: false
tags: ["Hugo", "GitHub Pages", "CI/CD"]
description: "记录本站的搭建过程：选型理由、目录结构、以及推送即发布的 GitHub Actions 流水线。"
---

## 为什么选 Hugo

写博客的工具链越简单越好。Hugo 是一个 Go 编译的单二进制，没有 `node_modules`，没有 gem 版本地狱，构建 100 篇文章也在 1 秒以内。

对比下来：

| 方案 | 运行时依赖 | 构建耗时 | 备注 |
|------|-----------|---------|------|
| Hugo | 单二进制 | < 1s | 官方有 Pages 部署模板 |
| Hexo | Node + 数百 npm 包 | 5~30s | 中文主题生态最丰富 |
| Jekyll | Ruby + Bundler | 5~20s | GitHub 原生支持，但本地 Ruby 版本常出问题 |

## 目录结构

```
.
├── archetypes/default.md   # hugo new 的模板
├── content/
│   ├── posts/              # 文章都放这里
│   ├── archives.md         # 归档页
│   └── search.md           # 搜索页
├── layouts/                # 覆盖主题的局部模板
├── static/                 # 原样拷贝的图片、favicon
├── themes/Papermod/        # git submodule
└── hugo.toml
```

写作只需要关心 `content/posts/`。

## 一个容易踩的坑：submodule 必须用 HTTPS

主题是以 git submodule 引入的：

```bash
git submodule add --depth 1 \
  https://github.com/adityatelange/hugo-Papermod.git themes/Papermod
```

注意 URL 用的是 `https://` 而不是 `git@github.com:`。GitHub Actions 的 runner 上没有你的 SSH 私钥，如果 `.gitmodules` 里写的是 SSH 地址，本地构建一切正常，推到线上就会卡在 checkout 阶段报 `Permission denied (publickey)`。

如果已经写错了，改回来：

```bash
git config -f .gitmodules submodule.themes/Papermod.url \
  https://github.com/adityatelange/hugo-Papermod.git
git submodule sync --recursive
```

对应的 workflow 里要加 `submodules: recursive`，否则主题目录是空的：

```yaml
- uses: actions/checkout@v4
  with:
    submodules: recursive
    fetch-depth: 0
```

## 发布流程

用的是 GitHub Actions + Pages 的官方 artifact 方式，而不是把构建产物提交到 `gh-pages` 分支。好处是仓库历史干净，`public/` 直接进 `.gitignore`。

日常写作就三步：

```bash
hugo new posts/my-post.md   # 新建（默认 draft: true）
hugo server -D              # 本地预览，草稿也会渲染
# 写完把 draft 改成 false，然后
git add -A && git commit -m "post: my-post" && git push
```

推上去大约一分钟后，https://jekbon.github.io 就更新了。
