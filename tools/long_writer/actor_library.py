"""
tools/long_writer/actor_library.py ── 演员库 v2.8
====================================================
跨故事/跨题材复用角色定妆的"演员资产库"。

设计原则:
  1. 完全解耦: 不依赖 portraits.py / to_pipeline.py 内部逻辑
  2. 接口稳定: 对外只暴露 6 个公开函数
  3. 文件系统即数据库: 所有状态写盘,可手动 cat/edit
  4. 向后兼容: story.yaml 的 portrait_ref 字段含义不变

目录结构:
  refs/actors/
  ├── _index.yaml                ← 全库索引(快速 list 用)
  ├── _tags_dictionary.yaml      ← desc 关键词 → tag 映射表(可编辑)
  ├── elder_male/
  │   ├── elder_male_001/
  │   │   ├── portrait.png       ← 主图
  │   │   ├── meta.yaml          ← 演员属性
  │   │   └── usage.log          ← 使用记录
  │   └── ...
  ├── young_male/, adult_male/,
  ├── elder_female/, young_female/, adult_female/,
  └── boy_child/, girl_child/

8 个 category:
  elder_male / adult_male / young_male / boy_child
  elder_female / adult_female / young_female / girl_child
"""

from __future__ import annotations
import shutil
import yaml
import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Any


# ════════════════════════════════════════════════════════════════
# 工具: yaml 字段类型兜底
# ════════════════════════════════════════════════════════════════
# yaml 加载的字段可能是 int/float/None/bool,所有要 .lower()/.strip()
# 等字符串方法的地方都要先 _safe_str 一遍。
# ════════════════════════════════════════════════════════════════

def _safe_str(value: Any, default: str = "") -> str:
    """任何类型 → str,None/空值 → default。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return " ".join(_safe_str(x) for x in value)
    return str(value)


# ════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════

ACTOR_ROOT = Path("refs/actors")
INDEX_FILE = ACTOR_ROOT / "_index.yaml"
TAGS_DICT_FILE = ACTOR_ROOT / "_tags_dictionary.yaml"

# 8 个类别(category)
VALID_CATEGORIES = (
    "elder_male", "adult_male", "young_male", "boy_child",
    "elder_female", "adult_female", "young_female", "girl_child",
)

# 性别 → category 候选
GENDER_TO_CATEGORIES = {
    "male":   ("boy_child", "young_male", "adult_male", "elder_male"),
    "female": ("girl_child", "young_female", "adult_female", "elder_female"),
}

# 年龄段 → category 后缀
AGE_TO_CAT_SUFFIX = {
    # 标准
    "child":   "child",   # 子=幼童
    "teen":    "young",   # 青少年→青年
    "young":   "young",
    "adult":   "adult",
    "middle":  "adult",   # 中年=adult
    "senior":  "elder",   # 老年
    "elder":   "elder",
    "old":     "elder",
}


# ════════════════════════════════════════════════════════════════
# 默认 tag 库(初版,后续可手动扩 _tags_dictionary.yaml)
# 多主题混合: 古风、民国、现代、儿童、奇幻都覆盖
# ════════════════════════════════════════════════════════════════

DEFAULT_TAG_DICTIONARY = {
    # === 古风/灵异(中国鬼故事主线) ===
    "庙祝":    ["temple_keeper", "elder", "mystical"],
    "村长":    ["village_elder", "authority"],
    "驼背":    ["hunched"],
    "道长":    ["taoist", "mystical", "long_beard"],
    "道士":    ["taoist", "mystical"],
    "和尚":    ["buddhist_monk", "shaven_head"],
    "尼姑":    ["nun", "buddhist", "shaven_head"],
    "员外":    ["wealthy", "ornate_clothing", "authority"],
    "县令":    ["magistrate", "official", "ornate_clothing"],
    "游侠":    ["wanderer", "swordsman", "ronin_like"],
    "书生":    ["scholar", "refined", "blue_robe"],
    "捕快":    ["constable", "tough", "rugged"],
    "村姑":    ["country_girl", "innocent", "simple"],
    "丫鬟":    ["maid", "young", "subservient"],
    "夫人":    ["lady", "elegant", "ornate"],
    "媒婆":    ["matchmaker", "elder", "talkative"],
    "妓女":    ["courtesan", "elegant", "made_up"],
    "鬼":      ["ghost", "ethereal", "pale"],
    "女鬼":    ["female_ghost", "ethereal", "pale", "long_hair"],
    "无面":    ["faceless", "uncanny"],
    "新娘":    ["bride", "red_gown"],
    "新郎":    ["groom"],
    "婴儿":    ["infant"],
    "孩童":    ["child", "innocent"],
    "幼童":    ["child", "innocent"],
    "小孩":    ["child"],
    "学童":    ["pupil", "child"],
    "老人":    ["elderly"],
    "老者":    ["elderly", "wise_look"],
    "老婆婆":  ["old_woman", "wrinkled"],
    "老头":    ["old_man"],
    "老翁":    ["old_man", "weathered"],

    # === 民国(共和 / 老上海) ===
    "巡警":    ["police", "uniform", "early_republic"],
    "马夫":    ["coachman", "labor", "humble"],
    "黄包车夫": ["rickshaw_puller", "labor", "humble"],
    "侦探":    ["detective", "trench_coat"],
    "记者":    ["journalist", "intellectual"],
    "校长":    ["headmaster", "intellectual"],
    "先生":    ["gentleman", "intellectual"],
    "教书":    ["teacher", "intellectual"],
    "戏子":    ["opera_performer", "made_up", "ornate"],
    "票友":    ["opera_amateur", "refined"],
    "汉奸":    ["traitor", "shifty"],
    "军阀":    ["warlord", "uniform", "menacing"],

    # === 西方/维多利亚(为 victorian theme 准备) ===
    "贵族":    ["nobleman", "victorian_aristocrat"],
    "管家":    ["butler", "victorian_servant"],
    "侍女":    ["maid", "victorian_servant"],
    "牧师":    ["priest", "clerical_collar"],
    "巫师":    ["wizard", "long_robes"],
    "女巫":    ["witch", "dark_gown"],

    # === 现代/都市 ===
    "白领":    ["office_worker", "suit"],
    "学生":    ["student", "youthful"],
    "医生":    ["doctor", "white_coat"],
    "护士":    ["nurse", "white_uniform"],
    "警察":    ["police_officer", "modern_uniform"],
    "工人":    ["laborer", "workwear"],
    "司机":    ["driver"],

    # === 视觉特征(独立于职业的特征词) ===
    "枯瘦":    ["thin", "frail"],
    "肥胖":    ["heavy", "rotund"],
    "矮小":    ["short_stature"],
    "高大":    ["tall_stature"],
    "白发":    ["white_hair"],
    "灰发":    ["gray_hair"],
    "黑发":    ["black_hair"],
    "胡须":    ["beard"],
    "短发":    ["short_hair"],
    "长发":    ["long_hair"],
    "马尾":    ["ponytail"],
    "盘发":    ["bun_hairstyle"],
    "卷发":    ["curly_hair"],
    "络腮":    ["full_beard"],
    "光头":    ["shaven_head"],
    "瞎眼":    ["blind"],
    "独眼":    ["one_eye"],
    "残疾":    ["disabled"],
    "瘸腿":    ["limping"],
    "疤痕":    ["scar"],
    "美貌":    ["beautiful"],
    "俊秀":    ["handsome"],
    "丑陋":    ["ugly", "disfigured"],

    # === 性格/神态(影响表情、瞳神) ===
    "诡异":    ["mystical", "uncanny"],
    "凶神":    ["fierce", "menacing"],
    "慈祥":    ["kind", "gentle"],
    "阴险":    ["sinister"],
    "天真":    ["innocent"],
    "狡黠":    ["cunning"],
    "忧郁":    ["melancholic"],
    "粗犷":    ["rugged"],
    "文静":    ["refined", "quiet"],
    "活泼":    ["lively"],
    "妖娆":    ["seductive", "alluring"],
    "清纯":    ["pure", "innocent"],
}


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class Actor:
    """演员实体。"""
    actor_id:           str
    display_name:       str
    category:           str             # 8 类之一
    gender:             str             # male / female
    age_band:           str             # child / young / adult / elder
    ethnicity:          str = "Han Chinese"
    identity_tags:      list = field(default_factory=list)
    distinctive_features: list = field(default_factory=list)
    source_prompt:      str = ""
    portrait_path:      str = ""
    used_in_stories:    list = field(default_factory=list)
    created_at:         str = ""
    note:               str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Actor":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ════════════════════════════════════════════════════════════════
# 路径辅助
# ════════════════════════════════════════════════════════════════

def _actor_dir(category: str, actor_id: str) -> Path:
    return ACTOR_ROOT / category / actor_id


def _actor_meta_path(category: str, actor_id: str) -> Path:
    return _actor_dir(category, actor_id) / "meta.yaml"


def _actor_portrait_path(category: str, actor_id: str) -> Path:
    return _actor_dir(category, actor_id) / "portrait.png"


def _actor_usage_log(category: str, actor_id: str) -> Path:
    return _actor_dir(category, actor_id) / "usage.log"


def _ensure_root():
    """确保根目录和默认文件存在。"""
    ACTOR_ROOT.mkdir(parents=True, exist_ok=True)
    if not TAGS_DICT_FILE.exists():
        with open(TAGS_DICT_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(DEFAULT_TAG_DICTIONARY, f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
    if not INDEX_FILE.exists():
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump({"actors": []}, f, allow_unicode=True)


# ════════════════════════════════════════════════════════════════
# Category 推断
# ════════════════════════════════════════════════════════════════

def _normalize_age_field(age_raw: str) -> str:
    """age 字段标准化。
    
    支持: 字符串("senior"/"young"/...) 或数字字符串("65"/"12"/...)。
    数字按以下范围映射:
      <13:   child
      13-25: young
      26-45: adult
      46-60: middle  (映射到 adult,见 AGE_TO_CAT_SUFFIX)
      >60:   senior
    """
    if not age_raw:
        return ""
    # 尝试当数字解析
    try:
        n = int(age_raw)
        if n < 13:
            return "child"
        elif n < 26:
            return "young"
        elif n < 46:
            return "adult"
        elif n < 61:
            return "middle"
        else:
            return "senior"
    except (ValueError, TypeError):
        # 不是数字,原样返回
        return age_raw



def infer_category(character: dict) -> str:
    """从 character 推断 category。

    character 字段约定(沿用 outline 现有格式):
      gender: male / female
      age:    child / young / adult / middle / senior / elder  或数字(45)
    """
    gender = _safe_str(character.get("gender"), "").lower().strip()
    age_raw = _safe_str(character.get("age"), "").lower().strip()
    # 数字 age 自动映射到段位
    age = _normalize_age_field(age_raw)

    if gender not in ("male", "female"):
        # 兜底: 默认 male (用户应去 outline 修)
        gender = "male"

    age_suffix = AGE_TO_CAT_SUFFIX.get(age, "adult")

    # 命名规则:
    #   child 类: boy_child / girl_child(child 放后面)
    #   其他类: elder_male / adult_female 等(性别放后面)
    if age_suffix == "child":
        category = "boy_child" if gender == "male" else "girl_child"
    else:
        category = f"{age_suffix}_{'male' if gender == 'male' else 'female'}"

    if category not in VALID_CATEGORIES:
        return "adult_male" if gender == "male" else "adult_female"
    return category


# ════════════════════════════════════════════════════════════════
# 索引读写
# ════════════════════════════════════════════════════════════════

def _read_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return []
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("actors", []) or []


def _write_index(actors: list[dict]):
    _ensure_root()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump({"actors": actors}, f, allow_unicode=True,
                       default_flow_style=False, sort_keys=False)


def _read_tags_dictionary() -> dict:
    _ensure_root()
    if not TAGS_DICT_FILE.exists():
        return dict(DEFAULT_TAG_DICTIONARY)
    with open(TAGS_DICT_FILE, "r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    # 合并默认 + 用户扩展(用户优先)
    merged = {**DEFAULT_TAG_DICTIONARY, **d}
    return merged


# ════════════════════════════════════════════════════════════════
# 公开 API #1: list_actors
# ════════════════════════════════════════════════════════════════

def list_actors(category: Optional[str] = None,
                  gender: Optional[str] = None,
                  tag: Optional[str] = None) -> list[Actor]:
    """列出演员,可按 category/gender/tag 过滤。"""
    _ensure_root()
    raw = _read_index()
    results = []
    for a_dict in raw:
        a = Actor.from_dict(a_dict)
        if category and a.category != category:
            continue
        if gender and a.gender != gender:
            continue
        if tag and tag not in (a.identity_tags or []):
            continue
        results.append(a)
    return results


# ════════════════════════════════════════════════════════════════
# 公开 API #2: get_actor
# ════════════════════════════════════════════════════════════════

def get_actor(actor_id: str) -> Optional[Actor]:
    """按 ID 取演员;找不到返回 None。"""
    for a in list_actors():
        if a.actor_id == actor_id:
            return a
    return None


# ════════════════════════════════════════════════════════════════
# 公开 API #3: register_actor
# ════════════════════════════════════════════════════════════════

def register_actor(portrait_path: str,
                     category: str,
                     gender: str,
                     age_band: str,
                     display_name: str = "",
                     identity_tags: Optional[list] = None,
                     distinctive_features: Optional[list] = None,
                     source_prompt: str = "",
                     ethnicity: str = "Han Chinese",
                     note: str = "") -> Actor:
    """把一张定妆照注册成演员。"""
    _ensure_root()

    if category not in VALID_CATEGORIES:
        raise ValueError(f"非法 category: {category},合法值: {VALID_CATEGORIES}")

    src = Path(portrait_path)
    if not src.exists():
        raise FileNotFoundError(f"portrait 不存在: {portrait_path}")

    # 自动分配 actor_id
    existing = list_actors(category=category)
    next_seq = len(existing) + 1
    while True:
        actor_id = f"{category}_{next_seq:03d}"
        if not _actor_dir(category, actor_id).exists():
            break
        next_seq += 1

    # 创建目录 + 复制图片
    actor_dir = _actor_dir(category, actor_id)
    actor_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, _actor_portrait_path(category, actor_id))

    actor = Actor(
        actor_id=actor_id,
        display_name=display_name or actor_id,
        category=category,
        gender=gender,
        age_band=age_band,
        ethnicity=ethnicity,
        identity_tags=identity_tags or [],
        distinctive_features=distinctive_features or [],
        source_prompt=source_prompt,
        portrait_path=str(_actor_portrait_path(category, actor_id)),
        created_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        note=note,
    )

    # 写 meta.yaml
    with open(_actor_meta_path(category, actor_id), "w", encoding="utf-8") as f:
        yaml.safe_dump(actor.to_dict(), f, allow_unicode=True,
                       default_flow_style=False, sort_keys=False)

    # 写 usage.log(空)
    _actor_usage_log(category, actor_id).touch()

    # 写索引
    index = _read_index()
    index.append(actor.to_dict())
    _write_index(index)

    return actor


# ════════════════════════════════════════════════════════════════
# 公开 API #4: suggest_actor
# ════════════════════════════════════════════════════════════════

def suggest_actor(character: dict,
                    top_k: int = 3) -> list[tuple[Actor, float]]:
    """根据 character 推荐演员。返回 [(actor, score), ...]。"""
    category = infer_category(character)
    candidates = list_actors(category=category)
    if not candidates:
        return []

    # 解析 character 的 desc 关键词 → tags
    desc = (
        _safe_str(character.get("desc"), "") + " " +
        _safe_str(character.get("name"), "") + " " +
        _safe_str(character.get("key_features"), "")
    )
    tags_dict = _read_tags_dictionary()

    target_tags = set()
    for kw, tags in tags_dict.items():
        if kw in desc:
            target_tags.update(tags)

    # 给每个候选打分
    scored = []
    for actor in candidates:
        actor_tags = set(actor.identity_tags or [])
        if not target_tags:
            # 没目标 tag: 全部演员得 0.5 分(平局)
            score = 0.5
        elif not actor_tags:
            # 演员没 tag: 给个最低分
            score = 0.1
        else:
            overlap = len(target_tags & actor_tags)
            union = len(target_tags | actor_tags)
            score = overlap / union if union > 0 else 0.0
        scored.append((actor, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


# ════════════════════════════════════════════════════════════════
# 公开 API #5: cast_actor_to_character
# ════════════════════════════════════════════════════════════════

def cast_actor_to_character(story_id: str,
                              character_name: str,
                              actor_id: str,
                              portrait_root: str = "refs/character_portraits",
                              mode: str = "copy") -> str:
    """把演员选给某个故事的角色。
    
    会做:
      1. 把 actor 的 portrait.png 复制(或软链)到
         refs/character_portraits/<story_id>/<character_name>.png
      2. 在演员的 usage.log 加一行
      3. 在演员 meta.yaml 的 used_in_stories 加入故事 ID
    
    Args:
        mode: "copy"(默认,占空间但稳)或"symlink"(省空间但跨机不兼容)
    
    Returns: 目标 portrait 文件路径
    """
    actor = get_actor(actor_id)
    if not actor:
        raise ValueError(f"找不到演员: {actor_id}")

    src = Path(actor.portrait_path)
    if not src.exists():
        raise FileNotFoundError(f"演员 portrait 不存在: {src}")

    target_dir = Path(portrait_root) / story_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{character_name}.png"

    # 安全: 已存在则备份
    if target.exists():
        backup = target.with_suffix(f".png.backup_{datetime.datetime.now():%Y%m%d_%H%M%S}")
        target.rename(backup)
        print(f"  [actor] 备份原 portrait → {backup.name}")

    if mode == "symlink":
        target.symlink_to(src.resolve())
    else:
        shutil.copy2(src, target)

    # 写 usage.log
    log_line = (
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\t"
        f"{story_id}\t{character_name}\n"
    )
    with open(_actor_usage_log(actor.category, actor.actor_id), "a",
              encoding="utf-8") as f:
        f.write(log_line)

    # 更新 meta + index
    used = list(actor.used_in_stories or [])
    sig = f"{story_id}:{character_name}"
    if sig not in used:
        used.append(sig)
        actor.used_in_stories = used
        with open(_actor_meta_path(actor.category, actor_id), "w",
                  encoding="utf-8") as f:
            yaml.safe_dump(actor.to_dict(), f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
        # 同步索引
        idx = _read_index()
        for entry in idx:
            if entry.get("actor_id") == actor_id:
                entry["used_in_stories"] = used
        _write_index(idx)

    return str(target)


# ════════════════════════════════════════════════════════════════
# 公开 API #6: pool_from_existing_portraits
# ════════════════════════════════════════════════════════════════

def pool_from_existing_portraits(
    portrait_root: str = "refs/character_portraits",
    dry_run: bool = False,
) -> dict:
    """扫描已有 character_portraits,反向入库为演员。
    
    会扫:
      refs/character_portraits/<story_id>/<character>.png
    
    会做:
      - 读对应 story.yaml 拿 character 元数据
      - 推断 category
      - 注册成演员(actor_id 自动分配)
      - dry_run=True 时只输出计划,不写盘
    
    Returns: 报告 dict {"added": [...], "skipped": [...], "errors": [...]}
    """
    report = {"added": [], "skipped": [], "errors": []}
    root = Path(portrait_root)
    if not root.exists():
        return report

    _ensure_root()

    for story_dir in sorted(root.iterdir()):
        if not story_dir.is_dir():
            continue
        story_id = story_dir.name
        # 找对应 story.yaml
        story_yaml = Path("stories") / f"{story_id}.yaml"
        characters_meta = {}
        if story_yaml.exists():
            try:
                with open(story_yaml, "r", encoding="utf-8") as f:
                    sdata = yaml.safe_load(f) or {}
                chars = sdata.get("characters", {})
                # 兼容 dict 和 list 两种格式
                if isinstance(chars, dict):
                    characters_meta = chars
                elif isinstance(chars, list):
                    for c in chars:
                        if isinstance(c, dict) and c.get("name"):
                            characters_meta[c["name"]] = c
            except Exception as e:
                report["errors"].append(f"{story_id}: 读 story.yaml 失败 {e}")

        # 扫所有 .png(非 _candidates 目录里的)
        for portrait_file in sorted(story_dir.glob("*.png")):
            char_name = portrait_file.stem
            char_meta = characters_meta.get(char_name, {})

            gender = _safe_str(char_meta.get("gender"), "male").lower()
            age_raw = _safe_str(char_meta.get("age"), "adult").lower()
            age = _normalize_age_field(age_raw) or "adult"

            try:
                category = infer_category({"gender": gender, "age": age})
            except Exception as e:
                report["errors"].append(f"{story_id}/{char_name}: {e}")
                continue

            age_band = AGE_TO_CAT_SUFFIX.get(age, "adult")

            # 从 desc 推 tags
            desc = _safe_str(char_meta.get("desc"), "")
            tags_dict = _read_tags_dictionary()
            tags = set()
            for kw, tag_list in tags_dict.items():
                if kw in desc or kw in char_name:
                    tags.update(tag_list)

            if dry_run:
                report["added"].append({
                    "from":      f"{story_id}/{char_name}",
                    "category":  category,
                    "tags":      sorted(tags),
                    "would_be":  f"{category}_NEXT",
                })
                continue

            try:
                actor = register_actor(
                    portrait_path=str(portrait_file),
                    category=category,
                    gender=gender,
                    age_band=age_band,
                    display_name=char_name,
                    identity_tags=sorted(tags),
                    source_prompt=_safe_str(char_meta.get("desc"), ""),
                    note=f"pooled from {story_id}/{char_name}",
                )
                # 记录 usage(因为它已经被用过了)
                actor.used_in_stories = [f"{story_id}:{char_name}"]
                with open(_actor_meta_path(actor.category, actor.actor_id),
                          "w", encoding="utf-8") as f:
                    yaml.safe_dump(actor.to_dict(), f, allow_unicode=True,
                                   default_flow_style=False, sort_keys=False)
                report["added"].append({
                    "from":     f"{story_id}/{char_name}",
                    "actor_id": actor.actor_id,
                    "category": category,
                    "tags":     sorted(tags),
                })
            except Exception as e:
                report["errors"].append(f"{story_id}/{char_name}: {e}")

    return report
