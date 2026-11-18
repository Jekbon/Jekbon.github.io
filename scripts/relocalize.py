#!/usr/bin/env python3
"""
重新生成 layouts/ 下的中文化覆盖模板。

主题升级后 layouts/ 里的覆盖是旧版拷贝，跑这个脚本可以从新版主题重新生成，
替换规则集中在 PLAN 里，便于维护。

用法: python3 scripts/relocalize.py
"""
import pathlib, shutil, sys

ROOT  = pathlib.Path(__file__).resolve().parent.parent
THEME = ROOT / 'themes/rusty-poison/layouts'
SITE  = ROOT / 'layouts'

# (主题内相对路径, [(原文, 中文), ...])
PLAN = {
'_default/baseof.html': [
  ('>Skip to content<', '>跳到正文<'),
],
'_default/single.html': [
  ('<summary>on this page</summary>', '<summary>本页目录</summary>'),
],
'_default/list.html': [
  ('.Date.Format "Jan 02"', '.Date.Format "01月02日"'),
  ('>project<', '>项目<'),
  # 修主题 bug：series 名被 urlize 成百分号编码后与 taxonomy key 不匹配
  ('$.Site.GetPage (print "series/" ($seriesTerm | urlize))',
   '$.Site.GetPage (print "series/" $seriesTerm)'),
],
'index.html': [
  ('.Date.Format "Jan 02"', '.Date.Format "01月02日"'),
  ('>project<', '>项目<'),
  ('$.Site.GetPage (print "series/" ($seriesTerm | urlize))',
   '$.Site.GetPage (print "series/" $seriesTerm)'),
],
'404.html': [
  # 终端仿真块（resolve / ENOENT / suggest --next / rusty@daemon）刻意保留英文
  (">The daemon couldn't find that page<", '>守护进程找不到这个页面<'),
  ('The path you followed has drifted out of orbit, was renamed, or never existed in the first place.',
   '你访问的路径可能已经偏移出轨道、被重命名过，或者从一开始就不存在。'),
  ('aria-label="Where to go next"', 'aria-label="接下来去哪"'),
  ('</span> Back to home', '</span> 返回首页'),
  ('>All posts</a>',   '>全部文章</a>'),
  ('>Browse tags</a>', '>浏览标签</a>'),
],
'projects/list.html': [
  ('.Date.Format "Jan 2006"', '.Date.Format "2006年01月"'),
  ("No projects yet - set <code>project = true</code> in any post's frontmatter to surface it here.",
   '还没有项目 —— 在任意文章的 front matter 里加上 <code>project = true</code>，它就会显示在这里。'),
],
'partials/post/info.html': [
  ('.Date.Format "Jan 02, 2006"', '.Date.Format "2006年01月02日"'),
  (' min read</span>', ' 分钟阅读</span>'),
  (' words</span>',    ' 字</span>'),
  ('// part of <',     '// 所属系列：<'),
  ('· part {{ $idx }} of {{ $total }}', '· 第 {{ $idx }} / {{ $total }} 篇'),
  ('print "series/" (. | urlize)', 'print "series/" .'),
],
'partials/post/series.html': [
  ('// part of <a',               '// 所属系列：<a'),
  ('.Date.Format "Jan 02, 2006"', '.Date.Format "2006年01月02日"'),
  ('{{ $seriesURL := print "series/" (. | urlize) }}',
   '{{ $seriesURL := print "series/" . }}'),
],
'partials/post/related.html': [
  ('>// related</h2>',            '>// 相关文章</h2>'),
  ('.Date.Format "Jan 02, 2006"', '.Date.Format "2006年01月02日"'),
],
'partials/post/toc.html': [
  ('>// on this page<',                       '>// 本页目录<'),
  ('aria-label="Table of contents"',          'aria-label="目录"'),
  ('aria-label="Collapse table of contents"', 'aria-label="收起目录"'),
  ('title="Collapse table of contents"',      'title="收起目录"'),
],
'partials/post/navigation.html': [
  ('aria-label="Post navigation"',        'aria-label="文章导航"'),
  ('<span class="label">previous</span>', '<span class="label">上一篇</span>'),
  ('<span class="label">next</span>',     '<span class="label">下一篇</span>'),
],
'partials/post/breadcrumbs.html': [
  ('aria-label="Breadcrumb"', 'aria-label="面包屑导航"'),
  ('$.Site.GetPage (print "series/" (. | urlize))', '$.Site.GetPage (print "series/" .)'),
],
'partials/pagination.html': [
  ('aria-label="Pagination"', 'aria-label="分页"'),
  ('>‹ prev</a>', '>‹ 上一页</a>'),
  ('>next ›</a>', '>下一页 ›</a>'),
],
'partials/search/modal.html': [
  ('placeholder="search something…"',                'placeholder="搜索文章…"'),
  ('<div class="search-empty">type to search</div>', '<div class="search-empty">输入关键词开始搜索</div>'),
  ('</span> navigate</span>', '</span> 选择</span>'),
  ('</span> open</span>',     '</span> 打开</span>'),
  ('</span> close</span>',    '</span> 关闭</span>'),
],
'partials/header/topbar.html': [
  ('aria-label="Open search"',                     'aria-label="打开搜索"'),
  ('<span class="label">search something…</span>', '<span class="label">搜索文章…</span>'),
  ('aria-label="Toggle light/dark mode" title="Toggle theme"',
   'aria-label="切换明暗主题" title="切换主题"'),
],
'partials/sidebar/sidebar.html': [
  # 启用主题注释掉的站点状态块
  ('<!-- {{ partial "sidebar/status.html" . }} -->', '{{ partial "sidebar/status.html" . }}'),
  ('aria-label="Toggle navigation" aria-expanded="false" title="Menu"',
   'aria-label="切换导航" aria-expanded="false" title="菜单"'),
  ('aria-label="Toggle light/dark mode" title="Toggle theme"',
   'aria-label="切换明暗主题" title="切换主题"'),
  ('<span>toggle theme</span>', '<span>切换主题</span>'),
  ('aria-label="Social links"', 'aria-label="社交链接"'),
  ('>// links<',                '>// 链接<'),
],
'partials/sidebar/menu.html': [
  ('aria-label="Primary"', 'aria-label="主导航"'),
  ('>// nav<',             '>// 导航<'),
  ('>home</a>',            '>首页</a>'),
],
'partials/sidebar/projects.html': [
  ('aria-label="Projects"', 'aria-label="项目"'),
  ('>// projects<',         '>// 项目<'),
],
'partials/sidebar/status.html': [
  ('aria-label="Site status"', 'aria-label="站点状态"'),
  ('</span> online</div>',     '</span> 在线</div>'),
  ('<div>built {{',            '<div>构建于 {{'),
  # hugo.CommitHash 在 brew/release 二进制里通常为空，回退到当前页面的 git commit
  ('    {{ with hugo.CommitHash }}<div>rev {{ . | truncate 7 "" }}</div>{{ end }}',
   '    {{- $rev := hugo.CommitHash -}}\n'
   '    {{- if not $rev }}{{ with .GitInfo }}{{ $rev = .Hash }}{{ end }}{{ end -}}\n'
   '    {{- with $rev }}<div>版本 {{ $rev | truncate 7 "" }}</div>{{ end }}'),
],
'partials/sidebar/title.html': [
  ('aria-label="Home"', 'aria-label="首页"'),
],
}


def main():
    if not THEME.exists():
        sys.exit(f"找不到主题模板目录: {THEME}")
    if SITE.exists():
        shutil.rmtree(SITE)
    fails = []
    for rel, subs in PLAN.items():
        src = THEME / rel
        if not src.exists():
            fails.append(f"{rel}: 主题里已不存在（上游可能改了结构）")
            continue
        s = src.read_text()
        for old, new in subs:
            if old not in s:
                fails.append(f"{rel}: 未命中 {old[:56]!r}")
            else:
                s = s.replace(old, new)
        dst = SITE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(s)
    total = sum(len(v) for v in PLAN.values())
    if fails:
        print(f"⚠ 生成了覆盖，但有 {len(fails)} 处需要人工检查（主题上游改动）:")
        for f in fails:
            print("   ", f)
        sys.exit(1)
    print(f"✅ 覆盖 {len(PLAN)} 个模板，{total} 处替换全部命中")


if __name__ == '__main__':
    main()
