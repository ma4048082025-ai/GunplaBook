"""
tools/long_writer/storyboard_characters.py ── 镜头角色字段派生与校验 (v2.10)
==========================================================================
放在 long_writer/ (跟 long_storyboard.py、reviewers.py 同目录), 因为它属于
分镜大师【写入】阶段的逻辑。渲染【读取】阶段的 get_render_characters 在
core/pipeline_v260_router.py —— 一写一读, 分属两个阶段, 不要混淆。

把"画面角色"相关的所有逻辑收敛到一个模块,服务 long_storyboard.py。

设计原则 (本次重构核心):
  ★ LLM 只填一个镜头级字段: render_characters (画面里实际可见的角色, 有序)
  ★ visible_characters (LoRA 触发名单) 从 render_characters 派生, 不让 LLM 填
  ★ 英文描述 en 从 story 角色表查, 不让 LLM 在镜头里重复填
  ★ 路径选择 (单人/多人/纯景) 由下游 router 看 render_characters 长度决定

字段职责对照:
  characters          (全书级, outline 产出)   故事总角色表: name/role/en/portrait
  render_characters   (镜头级, LLM 填)         画面里实际出现谁, 有序(0=左 1=右)
  visible_characters  (镜头级, 代码派生)        LoRA 触发名单 = render 里的 lead, ≤1
  dialogue.speaker    (镜头级, LLM 填)          台词归属(可画外音, 与出镜无关)

render_characters LLM 输出格式 (镜头里):
  render_characters:
    - name: 韩砚清        # 必填, 中文名
      region: left        # 可选, left/right/center, 缺省按顺序自动分配
    - name: 沈淮舟
  注意: LLM 不填 en, en 由本模块从角色表查后回填。

LoRA 接口保留但弱依赖:
  当前项目主要靠 PuLID, LoRA 用得少。visible_characters 仍派生出来供 LoRA
  触发, 但 LORA_MAX_LEADS 控制最多挂几个(默认 1, 多挂会糊)。
"""

from __future__ import annotations


# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

# LoRA 最多挂几个 lead (多角色 LoRA 会互相污染, 默认 1)
# 留接口: 将来若用支持多 LoRA 的方案, 调大此值即可
LORA_MAX_LEADS = 1

# render_characters 没写 region 时, 按顺序分配
_DEFAULT_REGIONS = ["left", "right", "center", "background"]


# ════════════════════════════════════════════════════════════════
# 角色表索引 (从 story_meta 建一次, 供整章复用)
# ════════════════════════════════════════════════════════════════

def build_character_index(story_meta: dict) -> dict:
    """从 story_meta['characters'] 建角色索引。

    返回:
      {
        "leads":   set(中文名),
        "extras":  set(中文名),
        "groups":  set(中文名),
        "en":      {中文名: 英文视觉描述},   # 查不到则空串
        "all":     set(全部已建模名, 含 group),
      }

    兼容三种 characters 格式:
      1. list[dict]:  [{name, role, en/en_name/en_desc, ...}]
      2. list[str]:   ["韩砚清", ...]   (旧 schema, 全当 lead)
      3. dict:        {"韩砚清": {...}}  (旧 dict 形式, 全当 lead)
    """
    leads, extras, groups = set(), set(), set()
    en_map = {}

    raw = story_meta.get("characters", [])

    def _extract_en(c: dict) -> str:
        # 兼容多种英文字段命名
        for k in ("en", "en_name", "en_desc", "english", "appearance_en",
                  "visual_en", "prompt_en"):
            v = c.get(k)
            if v and isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                name = c["name"]
                role = (c.get("role", "lead") or "lead").lower()
                if role == "extra":
                    extras.add(name)
                elif role == "group":
                    groups.add(name)
                else:
                    leads.add(name)
                en_map[name] = _extract_en(c)
            elif isinstance(c, str) and c.strip():
                leads.add(c.strip())
                en_map[c.strip()] = ""
    elif isinstance(raw, dict):
        for name, info in raw.items():
            leads.add(name)
            en_map[name] = _extract_en(info) if isinstance(info, dict) else ""

    return {
        "leads": leads,
        "extras": extras,
        "groups": groups,
        "en": en_map,
        "all": leads | extras | groups,
    }


# ════════════════════════════════════════════════════════════════
# 解析 + 校验 render_characters
# ════════════════════════════════════════════════════════════════

def parse_render_characters(raw_shot: dict, char_index: dict,
                             shot_id: str = "?") -> list:
    """从 LLM 输出的 raw_shot 解析并清洗 render_characters。

    清洗规则:
      - 只保留在角色表里已建模的 name (lead/extra/group 都可出镜)
      - 未建模名字 → 剔除并 warning (建议补到 outline.characters)
      - 回填 en (从角色表查), LLM 填的 en 忽略 (角色表是单一真相源)
      - region 缺省按顺序分配

    返回 [{name, en, region, role}, ...] (有序, 0=左)。
    纯景物镜返回 []。

    兼容:
      - render_characters 不存在时, 回退读 visible_characters (老 yaml 过渡)
    """
    rc = raw_shot.get("render_characters")

    # 过渡兼容: 老 yaml 只有 visible_characters
    if rc is None:
        legacy = raw_shot.get("visible_characters") or []
        rc = [{"name": n} for n in legacy
              if isinstance(n, (str, int)) and str(n).strip()]

    if not isinstance(rc, list):
        return []

    leads = char_index["leads"]
    extras = char_index["extras"]
    groups = char_index["groups"]
    all_known = char_index["all"]
    en_map = char_index["en"]

    out = []
    seen = set()
    for idx, item in enumerate(rc):
        # 取 name (支持 dict 或纯字符串)
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            region = str(item.get("region", "")).strip().lower()
        elif isinstance(item, (str, int)):
            name = str(item).strip()
            region = ""
        else:
            continue

        if not name or name in seen:
            continue

        # 未建模 → 剔除
        if name not in all_known:
            print(f"  [storyboard] ⚠ {shot_id} render_characters 含未建模角色 "
                  f"'{name}', 已剔除 (建议补到 outline.characters)")
            continue

        seen.add(name)

        # 确定 role
        if name in leads:
            role = "lead"
        elif name in extras:
            role = "extra"
        elif name in groups:
            role = "group"
        else:
            role = "lead"

        # region 缺省按顺序
        if region not in ("left", "right", "center", "background"):
            region = _DEFAULT_REGIONS[len(out) % len(_DEFAULT_REGIONS)]

        out.append({
            "name": name,
            "en": en_map.get(name, ""),
            "region": region,
            "role": role,
        })

    return out


# ════════════════════════════════════════════════════════════════
# 从 render_characters 派生 visible_characters (LoRA 名单)
# ════════════════════════════════════════════════════════════════

def derive_visible_characters(render_characters: list,
                              char_index: dict,
                              max_leads: int = LORA_MAX_LEADS) -> list:
    """从 render_characters 派生 LoRA 触发名单。

    规则:
      - 只取 role == lead 的角色 (extra/group 不触发 LoRA)
      - 最多取 max_leads 个 (默认 1, 多角色 LoRA 会互相污染)
      - 保持 render_characters 的顺序 (焦点主角通常在前)

    这是"代码派生"而非"LLM 手填", 解决了 visible_characters 既管显示
    又管 LoRA 的职责冲突。
    """
    leads = char_index["leads"]
    visible = [ch["name"] for ch in render_characters
               if ch["name"] in leads]
    return visible[:max_leads]


# ════════════════════════════════════════════════════════════════
# 一站式: 处理一个镜头的角色字段
# ════════════════════════════════════════════════════════════════

def process_shot_characters(raw_shot: dict, char_index: dict,
                            shot_id: str = "?") -> tuple:
    """处理一个镜头的角色字段, 返回 (render_characters, visible_characters)。

    供 long_storyboard.py 的 _validate_and_clean 调用, 替换原来手写的
    visible_characters 校验 + 强制截断逻辑。

    render_characters: list[{name, en, region, role}]  — 画面角色, 喂 PuLID
    visible_characters: list[str]                       — LoRA 名单(派生, ≤max)
    """
    render_chars = parse_render_characters(raw_shot, char_index, shot_id)
    visible_chars = derive_visible_characters(render_chars, char_index)

    # 日志: 多角色镜头提示 (不再强制截断, 双人是常态)
    if len(render_chars) >= 2:
        names = [c["name"] for c in render_chars]
        print(f"  [storyboard] {shot_id} 多角色镜头: {names} "
              f"(LoRA={visible_chars})")

    return render_chars, visible_chars
