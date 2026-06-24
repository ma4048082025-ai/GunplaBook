"""
tools/long_writer/narration_flow_extractor.py
============================================
人工阅读型旁白+对话流提取工具。

用途:storyboard 产出后,把全片 narration + dialogue 按页顺序提取出来,
     像短文一样平铺打印,方便人眼通读检查"是否通顺"。

不依赖 LLM,纯规则提取 + 启发式高亮可疑位置。

用法:
  python -m tools.long_writer.narration_flow_extractor \\
      stories/long_20260526_132559_风蚀的誓言.yaml

可选参数:
  --no-highlight      不做问题高亮,纯净输出
  --output FILE       同时保存到 txt 文件
  --no-dialogue       只看 narration,跳过 dialogue
  --chapter CH_ID     只看某一章(如 --chapter ch01)

输出格式(默认):
  ════ ch01 ════
  
  p1  藻台上的金粉簌簌落在韩砚清眉间,他轻轻拂去。
  p2  1947 年的工程日志在掌心裂开一道细缝。
  p3  泛黄纸页中滑出半张照片——少女旗袍下摆扫过银杏树根。
      韩砚清: "这照片..."   ← dialogue 用缩进区分
  p4  [⚠主语跳脱?] 他指尖划过这行字。
  ...

启发式高亮(可选):
  ⚠主语跳脱     连续 page 直接以"他/她/它"开头,且上一页未提及人名
  ⚠字数急刹车   相邻两镜 narration 字数差 ≥ 3 倍
  ⚠纯哑镜       narration 为空 + dialogue 也为空(可能是 hold 镜)
"""

from __future__ import annotations
import argparse
import sys
import yaml
from pathlib import Path
from typing import List, Dict, Optional


# ════════════════════════════════════════════════════════════════
# 提取
# ════════════════════════════════════════════════════════════════

def extract_pages(yaml_path: str) -> List[Dict]:
    """从 storyboard yaml 提取每页的 narration + dialogue + 元信息。
    
    返回 list of dict,每项含:
      page:         页码(int)
      title:        镜头标题(如 "ch01-sh01")
      chapter:      章 id(从 _source_chapter 或 title 提取)
      narration:    旁白文本
      dialogue:     [{speaker, text}, ...]
      is_hold:      是否 hold 镜(narration 通常应为空)
    """
    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    pages = data.get('pages', [])
    out = []
    for p in pages:
        title = p.get('title', '') or ''
        chapter = (p.get('_source_chapter')
                   or (title.split('-')[0] if '-' in title else '?'))
        out.append({
            'page':      p.get('page'),
            'title':     title,
            'chapter':   chapter,
            'narration': (p.get('narration') or '').strip(),
            'dialogue':  p.get('dialogue', []) or [],
            'is_hold':   bool(p.get('_hold')),
        })
    return out


# ════════════════════════════════════════════════════════════════
# 启发式问题检测(纯规则)
# ════════════════════════════════════════════════════════════════

# 中文代词开头(主语跳脱判定)
_PRONOUN_OPENERS = ("他", "她", "它", "他们", "她们", "它们")

def detect_issues(pages: List[Dict]) -> Dict[int, List[str]]:
    """检测可疑位置,返回 {page_idx: [issue_tags]}。
    
    启发式规则:
      1. 主语跳脱: 当前 page narration 以代词开头,且上一 page narration
         不含该角色的中文名(粗判,不一定准但能提示)
      2. 字数急刹车: 相邻两 page narration 字数差 ≥ 3 倍(且都非空)
      3. 纯哑镜: narration 空 + dialogue 空 + 非 hold(可能漏了)
    """
    issues: Dict[int, List[str]] = {}

    def _add(idx: int, tag: str):
        issues.setdefault(idx, []).append(tag)

    for i, p in enumerate(pages):
        narr = p['narration']
        prev_narr = pages[i-1]['narration'] if i > 0 else ''

        # 规则 3: 纯哑镜
        if not narr and not p['dialogue'] and not p['is_hold']:
            _add(i, "纯哑镜")

        # 规则 1: 主语跳脱
        if narr and any(narr.startswith(pr) for pr in _PRONOUN_OPENERS):
            # 看上一镜有没有提到具体人名
            # 简化判断: 上一镜没有任何 2-3 字的中文人名词(粗略找大写中文名)
            # 我们用更简单的判定: 上镜 narration 不含"他/她"以外的角色词
            # 实际无法精确判断角色名,只能看上一镜旁白末尾是不是用了代词收尾
            if prev_narr:
                # 如果上一镜也是用代词收尾或开头,认为指代链可能断了
                if any(prev_narr.startswith(pr) for pr in _PRONOUN_OPENERS):
                    _add(i, "主语跳脱?")

        # 规则 2: 字数急刹车
        if narr and prev_narr:
            len_now = len(narr)
            len_prev = len(prev_narr)
            ratio = max(len_now, len_prev) / max(1, min(len_now, len_prev))
            if ratio >= 3.0:
                _add(i, f"字数急刹车 {len_prev}→{len_now}")

    return issues


# ════════════════════════════════════════════════════════════════
# 渲染
# ════════════════════════════════════════════════════════════════

def render(pages: List[Dict],
            issues: Optional[Dict[int, List[str]]] = None,
            show_dialogue: bool = True,
            chapter_filter: Optional[str] = None) -> str:
    """把 pages 渲染成可读的纯文本。"""
    lines = []
    cur_chapter = None
    page_count = 0
    issue_count = 0

    for i, p in enumerate(pages):
        if chapter_filter and p['chapter'] != chapter_filter:
            continue

        # 章节分隔
        if p['chapter'] != cur_chapter:
            if cur_chapter is not None:
                lines.append("")
            lines.append(f"════ {p['chapter']} ════")
            lines.append("")
            cur_chapter = p['chapter']

        page_count += 1
        issue_tags = (issues or {}).get(i, [])
        if issue_tags:
            issue_count += 1

        # 主行: page + narration
        narr = p['narration'] or ("(无旁白)" if not p['dialogue'] else "")
        # hold 镜标记
        hold_mark = " [hold]" if p['is_hold'] else ""
        # 问题标记
        tag_str = ""
        if issue_tags:
            tag_str = "  ⚠" + " ".join(issue_tags)

        # 页号对齐
        page_str = f"p{p['page']:<3}"

        lines.append(f"{page_str} {narr}{hold_mark}{tag_str}")

        # dialogue 缩进显示
        if show_dialogue:
            for d in p['dialogue']:
                spk = d.get('speaker', '?')
                txt = (d.get('text') or '').strip()
                if txt:
                    lines.append(f"     [{spk}] {txt}")

    # 尾部统计
    lines.append("")
    lines.append("─" * 60)
    lines.append(f"共 {page_count} 镜  /  发现 {issue_count} 处可疑位置")
    if issues:
        # 分类统计
        from collections import Counter
        all_tags = []
        for tags in issues.values():
            for t in tags:
                # 去掉具体数字,只统计类别
                cat = t.split(" ")[0]
                all_tags.append(cat)
        c = Counter(all_tags)
        for tag, n in c.most_common():
            lines.append(f"  {tag}: {n} 处")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="提取 storyboard yaml 的 narration + dialogue 用于人工通读检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("yaml_path", help="storyboard yaml 路径")
    parser.add_argument("--no-highlight", action="store_true",
                        help="不做问题高亮,纯净输出")
    parser.add_argument("--no-dialogue", action="store_true",
                        help="只看 narration,跳过 dialogue")
    parser.add_argument("--chapter", help="只看某一章(如 ch01)")
    parser.add_argument("--output", help="同时保存到指定 txt 文件")

    args = parser.parse_args()

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        print(f"错误: 文件不存在 {yaml_path}", file=sys.stderr)
        sys.exit(1)

    try:
        pages = extract_pages(str(yaml_path))
    except Exception as e:
        print(f"错误: 解析 yaml 失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not pages:
        print("错误: yaml 中未找到 pages 字段", file=sys.stderr)
        sys.exit(1)

    issues = None if args.no_highlight else detect_issues(pages)
    rendered = render(
        pages, issues,
        show_dialogue=not args.no_dialogue,
        chapter_filter=args.chapter,
    )

    print(rendered)

    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
        print(f"\n已保存到 {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
