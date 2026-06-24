"""
chapter_writer.py ── 大纲 → 章节正文（Step 3）
====================================================
读取 scripts/<id>_outline.yaml，逐章生成正文。

特点：
  - 每章独立 LLM 调用（避免长上下文质量崩坏）
  - 写章节时把"前一章末尾 + 后一章开头摘要"作为上下文，保证衔接
  - 断点续写：已生成的章节跳过，可单独 --chapter ch03 重写
  - 双输出：md 主稿（人读编辑） + segments.yaml（机器消费）

输出结构：
  scripts/<id>.md            人类可读全文，每章一个 ## 标题
  scripts/<id>_segments.yaml 机器格式：
    chapters:
      - id: ch01
        title: ...
        arc_role: ...
        tone: ...
        body: 章节正文（中文，可能含对白）
        word_count: 实际字数
        segments: [按句切分的小段，用于后续分镜]

输入流程：
  默认从 outline.yaml 读
  如果存在 .md，先按 markdown 反向解析（人工修改后同步用）
"""

import argparse
import json
import re
from pathlib import Path

import yaml


SCRIPTS_DIR = Path("scripts")


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _split_into_segments(body: str, max_chars: int = 80) -> list:
    """
    把章节正文按句切分成小段（用于后续分镜规划）。
    每段约 1-3 句中文，适合一帧画面表达。
    """
    # 按中文标点切分
    parts = re.split(r'(?<=[。！？!?])\s*', body)
    parts = [p.strip() for p in parts if p.strip()]

    # 短句合并（超长句保持独立）
    segments = []
    cur = ""
    for p in parts:
        if len(p) > max_chars:
            if cur:
                segments.append(cur)
                cur = ""
            segments.append(p)
        elif len(cur) + len(p) <= max_chars:
            cur = (cur + p) if cur else p
        else:
            if cur:
                segments.append(cur)
            cur = p
    if cur:
        segments.append(cur)
    return segments


def _build_md(outline: dict, chapters_data: list) -> str:
    """生成 markdown 主稿（人类可读）"""
    lines = []
    lines.append(f"# {outline['title']}\n")
    lines.append(f"> {outline['premise']}\n")
    lines.append(f"**总字数**: {sum(c.get('word_count', 0) for c in chapters_data)} / "
                 f"目标 {outline['total_words']}\n")
    lines.append(f"**人物**: {', '.join(c['name'] for c in outline['characters'])}\n")
    lines.append("\n---\n")

    for ch_meta, ch_data in zip(outline["chapters"], chapters_data):
        lines.append(f"\n## {ch_meta['id']} {ch_meta['title']}\n")
        lines.append(f"`arc_role: {ch_meta['arc_role']}` · "
                     f"`tone: {ch_meta.get('tone', '?')}` · "
                     f"`{ch_data.get('word_count', 0)}字`\n\n")
        lines.append(ch_data.get("body", "") + "\n")
    return "\n".join(lines)


def _parse_md_back(md_path: Path, outline: dict) -> dict:
    """
    反向解析 .md 文件，提取每章正文（用于人工编辑后同步）。
    返回 {chapter_id: body_text}
    """
    if not md_path.exists():
        return {}

    text = md_path.read_text(encoding="utf-8")
    bodies = {}

    # 匹配 ## ch01 标题 ... 直到下一个 ## 或文件结尾
    # 章节 id 都是 chXX 格式
    pattern = r'##\s+(ch\d+)[^\n]*\n(.+?)(?=\n##\s+ch\d+|\Z)'
    for m in re.finditer(pattern, text, re.DOTALL):
        ch_id = m.group(1)
        body  = m.group(2)
        # 去掉 metadata 行（带反引号的）
        lines = [ln for ln in body.split("\n")
                 if not ln.strip().startswith("`")]
        body = "\n".join(lines).strip()
        if body:
            bodies[ch_id] = body
    return bodies


# ═══════════════════════════════════════════════════════════════
# 单章节生成
# ═══════════════════════════════════════════════════════════════

def _build_chapter_prompt(outline: dict, chapter: dict,
                           prev_chapter_tail: str = "",
                           next_chapter_head: str = "") -> str:
    """构造单章节的 LLM 提示词"""
    chars_str = "\n".join(
        f"  - {c['name']}：{c['desc']}"
        for c in outline.get("characters", [])
    )

    prev_context = ""
    if prev_chapter_tail:
        prev_context = f"\n【上一章节结尾（确保衔接）】\n{prev_chapter_tail[-300:]}\n"

    next_context = ""
    if next_chapter_head:
        next_context = f"\n【下一章节开头预告（避免剧情冲突）】\n{next_chapter_head[:200]}\n"

    prompt = f"""你是15分钟评书/唱故事的资深编剧。为以下章节创作完整正文。

【故事整体】
标题：{outline['title']}
钩子：{outline['premise']}
人物：
{chars_str}

【本章节】
ID：{chapter['id']}
标题：{chapter['title']}
arc_role：{chapter['arc_role']}（叙事职能）
tone：{chapter.get('tone', 'tension')}
目标字数：{chapter['target_words']}（允许 ±100 字）
章节大纲：{chapter.get('summary', '')}
{prev_context}{next_context}

【写作要求】
1. 严格按章节大纲展开，但不要照抄
2. 字数严格控制在 {chapter['target_words']} ± 100 字
3. 评书/朗诵风格：
   - 每 2-3 句一个画面感强的描写句
   - 对白用中文双引号"..."
   - 旁白第三人称视角，语言节奏感强
   - 关键时刻用短句制造紧张
   - 多用细节（声音、气味、温度），少用概括词
4. {chapter['arc_role']} 章节的特殊要求：
   - hook：开篇 200 字内必须出现强悬念/异常现象
   - setup：交代地点、年代、人物背景，但不超过本章 60%
   - rising：必须有"渐进失控"的层次感，每段比上段更紧
   - climax：揭秘 + 情绪爆点，文字密度提高
   - twist：在读者以为结束时给意外信息
   - falling：处理余波，但保留最后一丝不安
   - resolution：留白结尾，不要"教训"或"道理总结"
5. tone={chapter.get('tone', 'tension')} 的氛围词：
   - tension：紧绷、不安、欲言又止
   - eerie：诡异、超自然、违和
   - melancholy：哀伤、悲悯、无奈
   - peaceful：平静、温暖（少用，仅过渡章节）

直接输出章节正文（不要标题、不要章节号、不要 markdown 标记），从第一句开始：
"""

    return prompt


def write_chapter(outline: dict, chapter: dict,
                  prev_tail: str = "", next_head: str = "") -> dict:
    """生成单章节正文"""
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    prompt = _build_chapter_prompt(outline, chapter, prev_tail, next_head)

    llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                     base_url=LLM_BASE_URL, temperature=0.85)
    full = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        full += chunk.content

    body = full.strip()
    # 去掉可能的多余前缀
    body = re.sub(r'^(章节正文|正文|内容)[:：]\s*', '', body)
    body = re.sub(r'^(第[一二三四五六七八九十]+章[^\n]*\n)', '', body)

    word_count = len(body)
    segments = _split_into_segments(body)

    return {
        "id":         chapter["id"],
        "title":      chapter["title"],
        "arc_role":   chapter["arc_role"],
        "tone":       chapter.get("tone", "tension"),
        "body":       body,
        "word_count": word_count,
        "segments":   segments,
    }


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def write_all_chapters(outline_path: str,
                       only_chapter: str = None,
                       force: bool = False,
                       sync_from_md: bool = False,
                       enable_doctor: bool = True,
                       enabled_doctors: list = None,
                       enable_structural: bool = True,
                       doctor_only: str = None):
    """
    主入口。
    参数:
      outline_path:      scripts/<id>_outline.yaml 路径
      only_chapter:      只重写指定章节（如 "ch03"）
      force:             忽略已生成内容，全部重写
      sync_from_md:      不调 LLM，直接从 .md 解析回 segments.yaml
      enable_doctor:     启用编剧大师审稿（v2.3 新增，默认 True）
      enabled_doctors:   D 层启用列表，None=全开
      enable_structural: 启用 A 层（跨章结构编辑）
      doctor_only:       只重审某章
    """
    outline_path = Path(outline_path)
    if not outline_path.exists():
        print(f"  ❌ 大纲文件不存在: {outline_path}")
        return

    with open(outline_path, encoding="utf-8") as f:
        outline = yaml.safe_load(f)

    story_id   = outline["story_id"]
    md_path    = SCRIPTS_DIR / f"{story_id}.md"
    seg_path   = SCRIPTS_DIR / f"{story_id}_segments.yaml"

    # ── sync_from_md 模式：从 md 反向同步到 segments.yaml ────
    if sync_from_md:
        print(f"\n  [sync] 从 {md_path.name} 反向解析正文...")
        if not md_path.exists():
            print(f"  ❌ {md_path} 不存在")
            return

        bodies = _parse_md_back(md_path, outline)
        chapters_data = []
        for ch_meta in outline["chapters"]:
            ch_id = ch_meta["id"]
            body = bodies.get(ch_id, "")
            chapters_data.append({
                "id":         ch_id,
                "title":      ch_meta["title"],
                "arc_role":   ch_meta["arc_role"],
                "tone":       ch_meta.get("tone", "tension"),
                "body":       body,
                "word_count": len(body),
                "segments":   _split_into_segments(body),
            })

        with open(seg_path, "w", encoding="utf-8") as f:
            yaml.dump({"story_id": story_id,
                       "title":    outline["title"],
                       "outline_path": str(outline_path),
                       "chapters": chapters_data},
                      f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"  [sync] ✓ 已同步 {len(chapters_data)} 章 → {seg_path}")
        return

    # ── 加载已有进度（断点续写）────────────────────────────
    existing = {}
    if seg_path.exists() and not force:
        with open(seg_path, encoding="utf-8") as f:
            old = yaml.safe_load(f)
            for ch in old.get("chapters", []):
                if ch.get("body"):
                    existing[ch["id"]] = ch
        if existing:
            print(f"  [load] 找到已生成 {len(existing)} 章: "
                  f"{', '.join(sorted(existing.keys()))}")

    # ── 逐章生成 ─────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  章节生成 - 《{outline['title']}》")
    print(f"  共 {len(outline['chapters'])} 章 / 目标 {outline['total_words']} 字")
    print(f"{'='*55}\n")

    chapters_data = []
    chapters_meta = outline["chapters"]
    total_actual_words = 0

    for i, ch_meta in enumerate(chapters_meta):
        ch_id = ch_meta["id"]

        # 跳过已生成的（除非 only_chapter 或 force）
        if not force and ch_id in existing and (not only_chapter or only_chapter != ch_id):
            print(f"  [skip] {ch_id} {ch_meta['title']} "
                  f"({existing[ch_id].get('word_count', 0)}字) - 已生成")
            chapters_data.append(existing[ch_id])
            total_actual_words += existing[ch_id].get("word_count", 0)
            continue

        if only_chapter and ch_id != only_chapter:
            # 不在目标章节，但也不重写，复用已有
            chapters_data.append(existing.get(ch_id, {
                "id": ch_id, "body": "", "word_count": 0,
                "title": ch_meta["title"],
                "arc_role": ch_meta["arc_role"],
                "tone": ch_meta.get("tone", "tension"),
                "segments": []}))
            continue

        # 准备上下文（前章尾 + 后章首）
        prev_tail = ""
        next_head = ""
        if i > 0 and (chapters_meta[i-1]["id"] in existing or len(chapters_data) > 0):
            prev_data = existing.get(chapters_meta[i-1]["id"]) or \
                        (chapters_data[i-1] if i-1 < len(chapters_data) else {})
            prev_tail = prev_data.get("body", "")[-300:]
        if i < len(chapters_meta) - 1:
            next_head = chapters_meta[i+1].get("summary", "")[:200]

        print(f"\n  [{i+1}/{len(chapters_meta)}] {ch_id} "
              f"[{ch_meta['arc_role']:10s}] "
              f"目标 {ch_meta['target_words']}字  {ch_meta['title']}")

        ch_data = write_chapter(outline, ch_meta, prev_tail, next_head)
        chapters_data.append(ch_data)
        total_actual_words += ch_data["word_count"]

        delta = ch_data["word_count"] - ch_meta["target_words"]
        sign = "+" if delta >= 0 else ""
        print(f"           ✓ 实际 {ch_data['word_count']}字 ({sign}{delta}) "
              f"/ {len(ch_data['segments'])} 段")

        # 保存进度（每章后保存，防止崩溃丢失）
        seg_data = {
            "story_id":     story_id,
            "title":        outline["title"],
            "outline_path": str(outline_path),
            "chapters":     chapters_data + [
                # 占位未来章节
                {"id": c["id"], "title": c["title"],
                 "arc_role": c["arc_role"], "tone": c.get("tone", "tension"),
                 "body": "", "word_count": 0, "segments": []}
                for c in chapters_meta[i+1:]
                if c["id"] not in {ch["id"] for ch in chapters_data}
            ],
        }
        with open(seg_path, "w", encoding="utf-8") as f:
            yaml.dump(seg_data, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

    # ── 编剧大师审稿（v2.3 新增）────────────────────────────
    if enable_doctor and chapters_data:
        non_empty = [c for c in chapters_data if c.get("body")]
        if non_empty:
            try:
                # 优先相对导入
                from .script_doctors import run_all_doctors, print_revisions
            except ImportError:
                try:
                    from script_doctors import run_all_doctors, print_revisions
                except ImportError as e:
                    print(f"  [doctor] 模块导入失败，跳过审稿: {e}")
                    run_all_doctors = None

            if run_all_doctors:
                # 保留原始 body 副本到 body_v1
                for c in chapters_data:
                    if c.get("body") and "body_v1" not in c:
                        c["body_v1"] = c["body"]

                try:
                    revised, doctor_log = run_all_doctors(
                        chapters_data, outline,
                        enabled_doctors=enabled_doctors,
                        enable_structural=enable_structural,
                        story_id=story_id,
                        only_chapter=doctor_only,
                    )
                    chapters_data = revised
                    # 重新切 segments（body 改了）
                    for c in chapters_data:
                        if c.get("body"):
                            c["segments"] = _split_into_segments(c["body"])
                            c["word_count"] = len(c["body"])
                    print_revisions(doctor_log, max_show=20)
                except Exception as e:
                    print(f"  [doctor] 审稿异常（保留未审版本）: {e}")
                    import traceback
                    traceback.print_exc()

    # ── 写出最终 md 主稿 ────────────────────────────────────
    md_content = _build_md(outline, chapters_data)
    md_path.write_text(md_content, encoding="utf-8")

    # 重新写 segments.yaml（带审稿后的内容）
    seg_data = {
        "story_id":     story_id,
        "title":        outline["title"],
        "outline_path": str(outline_path),
        "chapters":     chapters_data,
    }
    with open(seg_path, "w", encoding="utf-8") as f:
        yaml.dump(seg_data, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)

    print(f"\n{'='*55}")
    print(f"  ✓ 全部完成")
    print(f"  实际字数: {total_actual_words} / 目标 {outline['total_words']}")
    print(f"  Markdown 主稿: {md_path}")
    print(f"  Segments YAML: {seg_path}")
    print(f"{'='*55}")
    print(f"\n  下一步:")
    print(f"    1. 审核 {md_path}（人工编辑章节正文）")
    print(f"    2. 编辑后同步: python -m tools.long_writer.cli sync {outline_path}")
    print(f"    3. 满意后进入分镜:")
    print(f"       python -m tools.long_writer.cli storyboard {seg_path}")


def main():
    parser = argparse.ArgumentParser(description="长故事章节正文生成（v2.3 含编剧大师）")
    parser.add_argument("outline", help="大纲 yaml 路径")
    parser.add_argument("--chapter",      default=None,
                        help="只重写指定章节（如 ch03）")
    parser.add_argument("--force",        action="store_true",
                        help="忽略已生成，全部重写")
    parser.add_argument("--sync-from-md", action="store_true",
                        help="不调 LLM，从 .md 反向同步到 segments.yaml")
    parser.add_argument("--no-doctor",    action="store_true",
                        help="禁用编剧大师审稿（默认开启）")
    parser.add_argument("--doctors",      default=None,
                        help="D 层启用列表，逗号分隔。"
                             "可选: continuity,logic,rhythm,dialogue。默认全开")
    parser.add_argument("--no-structural", action="store_true",
                        help="禁用 A 层结构编辑")
    parser.add_argument("--doctor-only",   default=None,
                        help="只重审某章（如 ch03），其他章节读已有缓存")
    args = parser.parse_args()

    enabled_doctors = None
    if args.doctors:
        enabled_doctors = [r.strip() for r in args.doctors.split(",") if r.strip()]

    write_all_chapters(
        outline_path     = args.outline,
        only_chapter     = args.chapter,
        force            = args.force,
        sync_from_md     = args.sync_from_md,
        enable_doctor    = not args.no_doctor,
        enabled_doctors  = enabled_doctors,
        enable_structural= not args.no_structural,
        doctor_only      = args.doctor_only,
    )


if __name__ == "__main__":
    main()
