"""
tools/long_writer/actor_cli.py ── 演员库独立 CLI
====================================================
用法:
  python -m tools.long_writer.actor_cli list                       # 看所有演员
  python -m tools.long_writer.actor_cli list --category elder_male # 按类过滤
  python -m tools.long_writer.actor_cli list --tag temple_keeper   # 按 tag 过滤
  python -m tools.long_writer.actor_cli show <actor_id>            # 看某演员
  python -m tools.long_writer.actor_cli register <portrait_path> \
      --category elder_male --gender male --age elder \
      --name 驼背庙祝型 --tags temple_keeper,hunched         # 注册
  python -m tools.long_writer.actor_cli suggest <story_yaml>       # 推荐(可选)
  python -m tools.long_writer.actor_cli cast <story_yaml> \
      --character 老庙祝 --actor elder_male_001                   # 选角
  python -m tools.long_writer.actor_cli pool                       # 从已有 portrait 入库
  python -m tools.long_writer.actor_cli pool --dry-run             # 只看不写
"""

from __future__ import annotations
import argparse
import sys
import yaml
from pathlib import Path


# ════════════════════════════════════════════════════════════════
# list
# ════════════════════════════════════════════════════════════════

def cmd_list(args):
    from .actor_library import list_actors, VALID_CATEGORIES

    actors = list_actors(category=args.category, gender=args.gender,
                         tag=args.tag)
    if not actors:
        print("(无演员匹配)")
        return

    # 按 category 分组
    by_cat = {}
    for a in actors:
        by_cat.setdefault(a.category, []).append(a)

    print(f"\n共 {len(actors)} 个演员\n")
    for cat in VALID_CATEGORIES:
        items = by_cat.get(cat, [])
        if not items:
            continue
        print(f"━━ {cat} ({len(items)}) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for a in items:
            tags_str = ", ".join(a.identity_tags[:4]) or "(无 tag)"
            used = len(a.used_in_stories or [])
            print(f"  {a.actor_id:25s} {a.display_name:20s}")
            print(f"    tags: {tags_str}")
            print(f"    用过 {used} 次  portrait: {a.portrait_path}")
        print()


# ════════════════════════════════════════════════════════════════
# show
# ════════════════════════════════════════════════════════════════

def cmd_show(args):
    from .actor_library import get_actor

    a = get_actor(args.actor_id)
    if not a:
        print(f"找不到演员: {args.actor_id}")
        sys.exit(1)

    print(f"\n演员: {a.actor_id}\n" + "="*50)
    print(yaml.safe_dump(a.to_dict(), allow_unicode=True,
                          default_flow_style=False, sort_keys=False))


# ════════════════════════════════════════════════════════════════
# register
# ════════════════════════════════════════════════════════════════

def cmd_register(args):
    from .actor_library import register_actor, VALID_CATEGORIES, AGE_TO_CAT_SUFFIX

    if args.category not in VALID_CATEGORIES:
        print(f"非法 category: {args.category}")
        print(f"合法值: {', '.join(VALID_CATEGORIES)}")
        sys.exit(1)

    age_band = AGE_TO_CAT_SUFFIX.get(args.age, "adult")
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    feats = [f.strip() for f in (args.features or "").split(",") if f.strip()]

    actor = register_actor(
        portrait_path=args.portrait,
        category=args.category,
        gender=args.gender,
        age_band=age_band,
        display_name=args.name or "",
        identity_tags=tags,
        distinctive_features=feats,
        note=args.note or "",
    )
    print(f"\n✓ 注册成功: {actor.actor_id}")
    print(f"  → refs/actors/{actor.category}/{actor.actor_id}/")


# ════════════════════════════════════════════════════════════════
# suggest
# ════════════════════════════════════════════════════════════════

def cmd_suggest(args):
    from .actor_library import suggest_actor

    story_path = Path(args.story_yaml)
    if not story_path.exists():
        print(f"找不到 story: {args.story_yaml}")
        sys.exit(1)

    with open(story_path, "r", encoding="utf-8") as f:
        sdata = yaml.safe_load(f) or {}

    # 取 characters
    chars_raw = sdata.get("characters", {})
    if isinstance(chars_raw, dict):
        chars = [{"name": k, **(v or {})} for k, v in chars_raw.items()]
    elif isinstance(chars_raw, list):
        chars = chars_raw
    else:
        print("characters 字段格式异常")
        sys.exit(1)

    # outline 里可能没 gender/age,从 outline yaml 找
    outline_path = Path("scripts") / f"{story_path.stem}_outline.yaml"
    if outline_path.exists():
        try:
            with open(outline_path, "r", encoding="utf-8") as f:
                odata = yaml.safe_load(f) or {}
            outline_chars = {c.get("name"): c for c in odata.get("characters", [])
                             if isinstance(c, dict) and c.get("name")}
            # 合并 outline 信息到 chars
            for ch in chars:
                if ch.get("name") in outline_chars:
                    oc = outline_chars[ch["name"]]
                    ch.setdefault("gender", oc.get("gender", ""))
                    ch.setdefault("age", oc.get("age", ""))
                    ch.setdefault("desc", oc.get("desc", ""))
        except Exception:
            pass

    print(f"\n故事: {story_path.stem}")
    print(f"角色数: {len(chars)}\n")

    for ch in chars:
        name = ch.get("name", "?")
        if name in ("narrator", "narrator_quote"):
            continue
        gender = ch.get("gender", "?")
        age = ch.get("age", "?")
        desc = (ch.get("desc") or "")[:40]
        print(f"━━ {name} ({gender}/{age}) ━━━━━━━━━━━━")
        print(f"  desc: {desc}")
        suggestions = suggest_actor(ch, top_k=args.top)
        if not suggestions:
            print(f"  (无匹配演员,建议新生成)")
        else:
            for actor, score in suggestions:
                marker = "★" if score >= 0.6 else " "
                tags_str = ", ".join(actor.identity_tags[:3])
                print(f"  {marker} {actor.actor_id:22s} ({actor.display_name}) "
                      f"score={score:.2f}")
                print(f"      tags: {tags_str}")
        print()


# ════════════════════════════════════════════════════════════════
# cast
# ════════════════════════════════════════════════════════════════

def cmd_cast(args):
    from .actor_library import cast_actor_to_character

    story_path = Path(args.story_yaml)
    if not story_path.exists():
        print(f"找不到 story: {args.story_yaml}")
        sys.exit(1)
    story_id = story_path.stem

    target = cast_actor_to_character(
        story_id=story_id,
        character_name=args.character,
        actor_id=args.actor,
        mode=args.mode,
    )
    print(f"\n✓ 选角完成")
    print(f"  演员 {args.actor} → {story_id}/{args.character}")
    print(f"  → {target}")
    print()
    print("  ⚠ 注意: 此命令只复制了 portrait 文件。")
    print("  你的 story.yaml 里 portrait_ref 字段需要手动指向上面的路径,")
    print("  或者跑 portraits_pick 命令固化。")


# ════════════════════════════════════════════════════════════════
# pool
# ════════════════════════════════════════════════════════════════

def cmd_pool(args):
    from .actor_library import pool_from_existing_portraits

    print(f"\n扫描 refs/character_portraits/ ...")
    if args.dry_run:
        print(f"(dry-run,不写盘)\n")

    report = pool_from_existing_portraits(dry_run=args.dry_run)

    print(f"\n=== 扫描报告 ===")
    print(f"  新增/将新增: {len(report['added'])}")
    print(f"  跳过:        {len(report['skipped'])}")
    print(f"  错误:        {len(report['errors'])}")
    print()

    if report["added"]:
        print("新增演员:")
        for item in report["added"]:
            print(f"  {item['from']:50s} → "
                  f"{item.get('actor_id') or item.get('would_be')}")
            if item.get('tags'):
                print(f"     tags: {', '.join(item['tags'][:4])}")
        print()

    if report["errors"]:
        print("错误:")
        for e in report["errors"]:
            print(f"  ! {e}")


# ════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="演员库 CLI (v2.8) - 跨故事角色复用"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p = sub.add_parser("list", help="列出所有演员")
    p.add_argument("--category", help="按 category 过滤")
    p.add_argument("--gender",   help="按 gender 过滤")
    p.add_argument("--tag",      help="按 tag 过滤")
    p.set_defaults(func=cmd_list)

    # show
    p = sub.add_parser("show", help="显示某演员详情")
    p.add_argument("actor_id")
    p.set_defaults(func=cmd_show)

    # register
    p = sub.add_parser("register", help="把现有图片注册为演员")
    p.add_argument("portrait",    help="图片路径")
    p.add_argument("--category",  required=True,
                   help="elder_male/adult_male/young_male/boy_child/"
                        "elder_female/adult_female/young_female/girl_child")
    p.add_argument("--gender",    required=True, choices=("male","female"))
    p.add_argument("--age",       required=True,
                   help="child/young/adult/middle/senior/elder")
    p.add_argument("--name",      help="display_name")
    p.add_argument("--tags",      help="逗号分隔的 tag 列表")
    p.add_argument("--features",  help="逗号分隔的 distinctive_features")
    p.add_argument("--note",      help="备注")
    p.set_defaults(func=cmd_register)

    # suggest
    p = sub.add_parser("suggest", help="为某故事的角色推荐演员")
    p.add_argument("story_yaml")
    p.add_argument("--top", type=int, default=3, help="返回前 N 名,默认 3")
    p.set_defaults(func=cmd_suggest)

    # cast
    p = sub.add_parser("cast", help="把演员选给某角色")
    p.add_argument("story_yaml")
    p.add_argument("--character", required=True)
    p.add_argument("--actor",     required=True)
    p.add_argument("--mode",      default="copy", choices=("copy","symlink"))
    p.set_defaults(func=cmd_cast)

    # pool
    p = sub.add_parser("pool", help="从已有 character_portraits 反向入库")
    p.add_argument("--dry-run", action="store_true",
                   help="只输出计划,不写盘")
    p.set_defaults(func=cmd_pool)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
