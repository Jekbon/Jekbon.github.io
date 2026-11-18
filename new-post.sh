#!/usr/bin/env bash
# 新建文章：./new-post.sh <slug> "<标题>"
# slug 决定 URL（用英文短横线），标题显示在页面上
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 2 ]; then
  echo "用法: $0 <slug> \"<标题>\""
  echo "示例: $0 k8s-pvc-pending \"PVC 一直 Pending 的排查\""
  exit 1
fi

slug="$1"; title="$2"
file="content/posts/${slug}.md"

if [[ ! "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "错误: slug 只能用小写字母、数字和短横线，收到: $slug"
  exit 1
fi
if [ -e "$file" ]; then
  echo "错误: 已存在 $file"
  exit 1
fi

hugo new "posts/${slug}.md"

# 用真实标题覆盖 archetype 自动生成的标题（hugo new 已渲染过模板，故按行首匹配）
python3 -I - "$file" "$title" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
title = sys.argv[2].replace('"', '\\"')
s = p.read_text()
s = re.sub(r'^(title\s*[:=]\s*)["\'].*?["\']\s*$',
           lambda m: f'{m.group(1)}"{title}"', s, count=1, flags=re.M)
p.write_text(s)
PY

echo
echo "已创建: $file"
echo "  1. 编辑内容，写完把 draft: true 改成 false"
echo "  2. hugo server -D          # 本地预览 http://localhost:1313"
echo "  3. git add -A && git commit -m 'post: ${title}' && git push"
