# Jekbon's Blog

个人博客，[https://jekbon.github.io](https://jekbon.github.io)

- **框架**：[Hugo](https://gohugo.io/)（单二进制，构建 < 100ms）
- **主题**：[Papermod](https://github.com/adityatelange/hugo-Papermod)（git submodule）
- **发布**：push 到 `main` → GitHub Actions 构建 → GitHub Pages 上线，约 1 分钟

## 本地环境

```bash
brew install hugo
```

版本需与 `.github/workflows/hugo.yaml` 里的 `HUGO_VERSION` 一致（当前 `0.170.1`），
否则可能出现「本地正常、线上失败」。

## 写一篇文章

```bash
./new-post.sh my-post-title "文章标题"   # 生成 content/posts/my-post-title.md（draft: true）
hugo server -D                           # http://localhost:1313 预览，-D 会渲染草稿
```

写完后把文件头的 `draft: true` 改成 `false`（或删掉该行），然后：

```bash
git add -A && git commit -m "post: 文章标题" && git push
```

推完约 1 分钟自动上线。查看构建状态：

```bash
gh run list --limit 5
gh run watch
```

## 更新主题

```bash
git submodule update --remote --merge themes/Papermod
hugo --minify          # 确认没有新告警
git add -A && git commit -m "chore: bump Papermod" && git push
```

## 配色

默认**纯黑背景**。覆盖层在 `assets/css/extended/dark.css`（Papermod 会把
`assets/css/extended/*.css` 合并到主题样式**之后**，同特异性靠后胜出，所以能覆盖）：

| 想怎么改 | 做法 |
|---|---|
| 调回官方深灰 `rgb(29,30,32)` | 删掉 `dark.css` 这一个文件，其他不动 |
| 微调黑度 | 只改 `--theme` 一行；`rgb(0,0,0)` 最黑，`rgb(18,18,20)` 稍柔和 |
| 改正文颜色 | `--content`（正文）、`--primary`（标题） |

正文刻意用 `rgb(197,199,204)` 而不是纯白——纯黑配纯白对比过强，长时间阅读会光晕刺眼。

右上角按钮可临时切亮色，选择存在浏览器 `localStorage` 的 `pref-theme` 键里。
如果哪次打开发现变白了，多半是之前点过切换，控制台执行
`localStorage.removeItem("pref-theme")` 再刷新即可。

## 踩过的坑

**1. 文章「消失」——日期写成了未来时间**

`hugo.toml` 里 `buildFuture = false`。如果 front matter 的 `date` 晚于当前时刻，
文章会被**静默跳过**：不报错、不告警，首页和归档都看不到它。
`./new-post.sh` 用当前时间生成日期就是为了避开这个。排查命令：

```bash
hugo list future      # 列出所有因未来日期被排除的文章
```

想定时发布（先写好、到点自动出现），把 `buildFuture` 改成 `true` 即可。

**2. 首页不显示任何文章——缺 `mainSections`**

Papermod 用 `where site.RegularPages "Type" "in" site.Params.mainSections` 取首页文章。
`hugo.toml` 必须显式写：

```toml
[params]
mainSections = ["posts"]
```

漏掉这行不会报错，首页只会显示欢迎语，一篇文章都没有。

**3. 线上构建拉不到主题——submodule 用了 SSH 地址**

`.gitmodules` 里的 URL 必须是 `https://`，不能是 `git@github.com:`。
Actions runner 上没有你的 SSH 私钥，本地一切正常、线上 checkout 直接
`Permission denied (publickey)`。已写错的话：

```bash
git config -f .gitmodules submodule.themes/Papermod.url \
  https://github.com/adityatelange/hugo-Papermod.git
git submodule sync --recursive
```

**4. `--minify` 破坏 RSS**

`hugo.toml` 里的 `[minify] disableXML = true` 别删，否则 `/index.xml` 会被压坏。
