"""
narrative_profile.py v0.1 — 文字生成阶段的题材常识库
===============================================================
姐妹模块: theme_profile.py (服务出图阶段)
本模块服务: outline.py + outline_facet.py + chapter_facet.py
           (统称"文字生成"阶段:大纲、章节正文)

为什么有这个文件?
  之前 outline.py / outline_facet.py / chapter_facet.py 里 5 处都
  写死了"中文恐怖编剧""对标老妖鬼故事""信任崩塌是恐怖片的强戏"
  这种恐怖向的引导文本。导致用户即使选了 lyrical_storytelling /
  children_storybook 主题,产出的大纲仍然是恐怖故事。
  
  本模块把"文字阶段的写作风格"也变成 theme-driven 的配置,跟 
  theme_profile 服务出图阶段一样,做到真正的 theme 单一真相源。

加载优先级(完全模仿 theme_profile.load_profile):
  1. themes/<theme_id>.yaml :: narrative 段 (外置,推荐)
  2. DEFAULT_NARRATIVES[theme_id]                (内置默认)
  3. 从 themes/<theme_id>.yaml :: agent.style_guidance 软推断 (兜底)
  4. DEFAULT_NARRATIVES["default"]               (终极兜底)

字段含义:
  identity         — writer 角色定位,写进 LLM system_prompt 开头
                     ("你是 XX 编剧")
  benchmark        — 对标账号/作品,告诉 LLM 学谁
                     ("对标 老妖鬼故事 / 一条 / 凯叔讲故事")
  tone_options     — 允许的 tone 标签列表,写进大纲 chapter.tone
                     (恐怖: eerie/tension; 抒情: tender/melancholy;
                      儿童: cheerful/curious)
  forbidden_tones  — 禁止出现的 tone (儿童禁 scary/violent)
  arc_emphasis     — 叙事节奏侧重
                     (恐怖: 悬念铺垫→高潮揭示;
                      抒情: 情感张力→意象铺陈;
                      儿童: 好奇心→友谊→温暖)
  pacing_guide     — 节奏指引 (前 30 秒抓眼 vs 前 90 秒铺设氛围)
  visual_hook_focus— 视觉钩子方向 (真实道具 vs 诗意意象 vs 鲜艳色彩)
  rubric           — 评估维度列表 [{name, criteria}, ...]
                     用于对话精炼时让 LLM 看什么标准给建议
  story_safety     — 内容安全约束 (儿童禁血腥, 抒情禁惊悚等)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class RubricItem:
    """评估维度。"""
    name: str           # "钩子强度"
    criteria: str       # "hook 章能否在前 30 秒抓住短视频观众?"


@dataclass
class NarrativeProfile:
    """文字生成阶段的题材常识库。"""

    theme_id: str = "default"

    # ── writer 角色定位 ──
    identity: str = "你是资深的中文故事编剧"
    benchmark: str = "对标主流叙事内容平台"

    # ── 风格约束 ──
    tone_options: list = field(default_factory=lambda: ["neutral"])
    forbidden_tones: list = field(default_factory=list)
    arc_emphasis: str = "起承转合,自然过渡"
    pacing_guide: str = "节奏匹配题材,详略得当"
    visual_hook_focus: str = "具体可视化场景,避免抽象描写"

    # ── 评估维度 ──
    rubric: list = field(default_factory=list)   # list[RubricItem]

    # ── 内容安全 ──
    story_safety: str = ""    # 题材禁忌,如"不出现暴力/血腥"

    # ── 衍生方法 ──

    def rubric_text(self) -> str:
        """把 rubric 拼成 prompt 友好的文本块。"""
        if not self.rubric:
            return "(此题材无具体评估 rubric,以读者直觉为准)"
        lines = []
        for i, r in enumerate(self.rubric, 1):
            lines.append(f"{i}. {r.name}")
            lines.append(f"   {r.criteria}")
        return "\n".join(lines)

    def tone_hint(self) -> str:
        """给 LLM 的 tone 引导文本。"""
        bits = []
        if self.tone_options:
            bits.append(f"tone 字段可选: {' / '.join(self.tone_options)}")
        if self.forbidden_tones:
            bits.append(f"严禁出现的 tone: {' / '.join(self.forbidden_tones)}")
        return ";".join(bits)


# ════════════════════════════════════════════════════════════════
# 内置 fallback narratives (与 themes/*.yaml 对齐)
# ════════════════════════════════════════════════════════════════
# 设计原则:
#   - chinese_horror_tales 必须 100% 等价于原 outline.py / facet 的
#     写死内容,确保零回归
#   - 其他主题用各自题材的最佳实践

DEFAULT_NARRATIVES = {

    # ─────────────────────────────────────────────────────────
    # default — 终极兜底,中性叙事
    # ─────────────────────────────────────────────────────────
    "default": NarrativeProfile(
        theme_id="default",
        identity="你是资深的中文故事编剧",
        benchmark="对标主流叙事内容,讲求节奏与共鸣",
        tone_options=["neutral", "warm", "tense", "melancholy", "hopeful"],
        forbidden_tones=[],
        arc_emphasis="起承转合,情感推进自然",
        pacing_guide="开篇抓住注意力,中段推进有力,结尾留余韵",
        visual_hook_focus="具体可视化场景,真实细节,避免抽象描写",
        rubric=[
            RubricItem("钩子强度",
                "开篇能否在短时间内抓住读者?"),
            RubricItem("角色厚度",
                "主要角色是否有可信的动机和成长?"),
            RubricItem("节奏",
                "情节推进是否得当,有无拖沓或仓促?"),
            RubricItem("视觉一致性",
                "visual_palette / bgm_palette 是否与故事氛围匹配?"),
        ],
        story_safety="",
    ),

    # ─────────────────────────────────────────────────────────
    # chinese_horror_tales — 中式怪谈/民间鬼故事
    # ⚠ 这一段必须完全等价于原 outline.py / facet 里的写死内容
    # ─────────────────────────────────────────────────────────
    "chinese_horror_tales": NarrativeProfile(
        theme_id="chinese_horror_tales",
        identity="你是15分钟长篇评书/唱故事的资深编剧 + 短视频内容策划",
        benchmark="对标'老妖鬼故事''听老梁讲故事'等头部账号",
        tone_options=["tension", "eerie", "melancholy", "peaceful"],
        forbidden_tones=["cheerful", "playful"],
        arc_emphasis="强开篇钩子 → 谜团加深 → 高潮揭示 → 反转 → 余韵",
        pacing_guide=(
            "第1章必须在前 200 字内抛出最大悬念。"
            "每章一个小转折/小揭秘,不允许'无事章节'。"
            "rising 章节制造谜团,climax/twist 章节给答案。"
            "最后一章不要'从此过上幸福生活',要留思考或未解之谜。"
        ),
        visual_hook_focus=(
            "真实地点(民国上海/清末江南/八九十年代县城),"
            "真实道具(青铜镜/旧照片/老式收音机),"
            "具体可拍画面,避免抽象描写"
        ),
        rubric=[
            RubricItem("钩子强度",
                "hook 章节能否在前 30 秒抓住短视频观众?"
                "premise 是否同时含【视觉冲击】+【悬念问题】?"),
            RubricItem("角色动机",
                "每个 lead 角色有没有内在冲突或动机?"
                "反派角色(鬼/怪)有没有可挖掘的来由?"
                "单纯'被诅咒'的角色容易拍成单调打怪"),
            RubricItem("配角生态",
                "配角数量是否合适?(2-4 个 lead/extra)"
                "信任崩塌是恐怖片的强戏 —— 配角与主角有无信任关系?"),
            RubricItem("悬念结构",
                "谜底什么时候揭晓? 提前泄底 vs 揭晓太晚都是问题。"
                "hook → dread → climax → falling 的节奏合不合理?"),
            RubricItem("视觉/BGM 一致性",
                "visual_palette 和 bgm_palette 是否互相支撑?"
                "是否与 theme + 时代背景匹配?"),
            RubricItem("章节长度分布",
                "hook 章是否过短?(< 100 字会显得仓促)"
                "climax 章字数是否充足?(一般占 25-35%)"),
            RubricItem("市场点",
                "概念是否有抖音/YouTube 短视频的传播力?"
                "标题是否够'勾'? premise 能不能直接当封面文案?"),
        ],
        story_safety="保持'恐怖'但不'血腥',氛围悬念为主,不写过度暴力细节",
    ),

    # ─────────────────────────────────────────────────────────
    # chinese_ghost_flux — 聊斋志异风格(古风鬼怪)
    # ─────────────────────────────────────────────────────────
    "chinese_ghost_flux": NarrativeProfile(
        theme_id="chinese_ghost_flux",
        identity="你是聊斋志异风格的古风志怪故事编剧",
        benchmark="对标蒲松龄《聊斋志异》、纪晓岚《阅微草堂笔记》的笔法",
        tone_options=["eerie", "tragic", "tender", "mystic", "melancholy"],
        forbidden_tones=["cheerful", "playful"],
        arc_emphasis="奇遇 → 情感纠葛 → 道德困境 → 因果报应",
        pacing_guide=(
            "古风志怪重'氛围'与'情'。"
            "开篇可以先铺一段静谧或诡异的场景(古宅、深山、夜雨),"
            "中段慢慢揭示人鬼之间的关系,"
            "结尾常带'天理'或'痴情'的感慨"
        ),
        visual_hook_focus=(
            "古风意象(青灯古卷、雨夜小亭、月下女鬼),"
            "汉服与古建场景,水墨电影感"
        ),
        rubric=[
            RubricItem("奇遇钩子", "开篇是否有'人遇鬼/狐/妖'的奇特相逢?"),
            RubricItem("情感张力", "人鬼之间的关系是否真挚?是否有可惜可叹之处?"),
            RubricItem("因果设计", "故事是否隐含'因果''天理''报应'的古典道德观?"),
            RubricItem("古风感", "用语、人物名、场景是否古朴自然,不出戏?"),
            RubricItem("结尾余韵", "是否给读者留'唏嘘''感慨'的余地?"),
        ],
        story_safety="保留古典含蓄,不写血腥/重口,情感为主",
    ),

    # ─────────────────────────────────────────────────────────
    # lyrical_storytelling — 抒情叙事
    # ─────────────────────────────────────────────────────────
    "lyrical_storytelling": NarrativeProfile(
        theme_id="lyrical_storytelling",
        identity="你是资深的抒情/文艺向中文故事编剧 + 诗意视频内容策划",
        benchmark="对标'一条''单向街''央视纪录片'的文艺叙事质感",
        tone_options=[
            "tender", "melancholy", "nostalgic", "hopeful",
            "contemplative", "bittersweet", "peaceful",
        ],
        forbidden_tones=["eerie", "tension", "scary"],
        arc_emphasis="情感张力 → 意象铺陈 → 情绪转折 → 余韵悠长",
        pacing_guide=(
            "前 90 秒可以慢慢铺设情感氛围,不必急于抛悬念。"
            "重在情绪节奏和意象呼应,而非情节冲突。"
            "结尾常以一个温柔意象收束(信件、风、季节)。"
            "不要走类型化的'反转''高潮'套路,情感真挚比情节曲折重要。"
        ),
        visual_hook_focus=(
            "诗意意象(光影、风、雨、季节、信物),"
            "情感载体(老照片、旧物、远去的背影),"
            "暖色调与留白构图,避免血腥/惊悚元素"
        ),
        rubric=[
            RubricItem("情感张力",
                "故事的核心情感冲突是否真挚?"
                "是不是让读者愿意慢下来、读到落泪或微笑?"),
            RubricItem("意象诗意",
                "有没有可以反复回味的意象?(光、风、季节、信物)"
                "意象之间是否呼应,形成情绪共振?"),
            RubricItem("节奏温柔",
                "节奏是否抒情而非紧张?"
                "有没有该静的地方却急了、该铺垫的地方跳了?"),
            RubricItem("人物厚度",
                "人物的情感转变是否细腻可信?"
                "不需要复杂动机,但情感弧线要清晰"),
            RubricItem("余韵",
                "结尾是否留有想象空间,而非闭合式结局?"
                "好的抒情故事让人放下后还在想"),
            RubricItem("视觉与文字共鸣",
                "visual_palette 与情感氛围是否同频?"
                "暖色调对应思念、冷色调对应离别,等等"),
        ],
        story_safety=(
            "题材应温柔抒情,可以有伤感、离别、遗憾,"
            "但不写恐怖、惊悚、血腥、暴力元素"
        ),
    ),

    # ─────────────────────────────────────────────────────────
    # children_storybook — 儿童绘本
    # ─────────────────────────────────────────────────────────
    "children_storybook": NarrativeProfile(
        theme_id="children_storybook",
        identity="你是资深的中文儿童故事/绘本编剧",
        benchmark="对标'凯叔讲故事''小灯塔'等头部儿童内容,以及经典绘本《猜猜我有多爱你》《好饿的毛毛虫》的笔法",
        tone_options=[
            "cheerful", "gentle", "curious", "adventure",
            "dreamy", "warm", "playful",
        ],
        forbidden_tones=[
            "eerie", "tension", "scary", "horror", "tragic", "violent",
        ],
        arc_emphasis="好奇心 → 友谊/勇气 → 小小冒险 → 温暖收获",
        pacing_guide=(
            "每章一个小惊喜或小学习,情节温和推进。"
            "不要恐惧、暴力、孤独、死亡等负面元素。"
            "节奏明快但不仓促,语言简洁有节奏感(可以有拟声词、重复句)。"
            "结尾必须温暖、积极、有教育意义"
        ),
        visual_hook_focus=(
            "鲜艳明亮的色彩、可爱友好的角色、温暖的场景,"
            "拟人化动物常用,自然元素(花、星星、彩虹、月亮)友好出现"
        ),
        rubric=[
            RubricItem("童真趣味",
                "故事是否让 4-10 岁的孩子愿意一直听下去?"
                "有没有'孩子能立刻共鸣'的瞬间?"),
            RubricItem("教育价值",
                "传递了什么积极的品质或知识?"
                "(友谊/勇气/分享/好奇/坚持/感恩/认知世界)"),
            RubricItem("安全友好",
                "有无可能让小朋友害怕或困惑的元素?"
                "(妖怪/失踪/死亡/欺骗等需要避免或软化处理)"),
            RubricItem("角色可爱度",
                "角色形象是不是孩子愿意亲近、想去拥抱的?"
                "拟人化设计是否成功?"),
            RubricItem("结尾温暖",
                "结尾是否给孩子安全感、获得感?"
                "避免悬念式或开放式结局"),
            RubricItem("视觉友好",
                "visual_palette 明亮、温暖、清晰?"
                "适合绘本/动画呈现?"),
        ],
        story_safety=(
            "严格儿童安全:不出现血腥、暴力、惊悚、死亡、欺骗、"
            "孤独无助等负面情节。妖怪要友好可爱,冒险要安全有限度,"
            "矛盾要温和可解"
        ),
    ),

    # ─────────────────────────────────────────────────────────
    # victorian_gothic_tales — 维多利亚哥特
    # ─────────────────────────────────────────────────────────
    "victorian_gothic_tales": NarrativeProfile(
        theme_id="victorian_gothic_tales",
        identity=(
            "You are a seasoned writer of Victorian Gothic fiction, "
            "in the literary tradition of Edgar Allan Poe, Mary Shelley, "
            "Bram Stoker, and Henry James"
        ),
        benchmark=(
            "Benchmark against Poe's short tales, Stevenson's 'Jekyll & Hyde', "
            "James' 'The Turn of the Screw'. Atmosphere over gore"
        ),
        tone_options=[
            "dread", "horror", "eerie", "suspense",
            "melancholy", "decay", "elegant",
        ],
        forbidden_tones=["cheerful", "playful"],
        arc_emphasis=(
            "Slow-burn atmospheric build → uncanny revelation → "
            "psychological horror climax → unsettling resolution"
        ),
        pacing_guide=(
            "Victorian Gothic prizes mood and dread over jump scares. "
            "Open with atmosphere (gaslight, fog, ornate but decaying interiors). "
            "Build slowly through psychological unease. "
            "Climax is often a revelation of identity or hidden truth, "
            "not violence. End with lingering ambiguity"
        ),
        visual_hook_focus=(
            "Victorian-era specifics: gaslights, fog, ornate Victorian interiors, "
            "moldering manor houses, mirrors, portraits, oil paintings. "
            "Avoid modern, Asian, or fantasy elements"
        ),
        rubric=[
            RubricItem("Atmosphere",
                "Does each scene evoke Victorian Gothic mood "
                "(fog, gaslight, dread, decay)?"),
            RubricItem("Psychological dread",
                "Is the horror psychological rather than visceral? "
                "Does it lurk and suggest rather than show?"),
            RubricItem("Character credibility",
                "Are characters period-authentic in speech, manner, social role?"),
            RubricItem("Pacing & restraint",
                "Is the pacing slow-burn appropriately? "
                "Avoid modern-style rapid plot beats"),
            RubricItem("Visual cohesion",
                "Does visual_palette evoke gaslight chiaroscuro / oil-painting depth?"),
            RubricItem("Period authenticity",
                "Are settings, language, social mores believably Victorian?"),
        ],
        story_safety=(
            "Gothic literature can be dark but should remain literary; "
            "avoid gratuitous gore, modern slang, anachronisms"
        ),
    ),

    # ─────────────────────────────────────────────────────────
    # victorian_whimsical_children — 维多利亚童趣经典
    # ─────────────────────────────────────────────────────────
    "victorian_whimsical_children": NarrativeProfile(
        theme_id="victorian_whimsical_children",
        identity=(
            "You are a writer of classic Victorian children's literature, "
            "in the tradition of Lewis Carroll, J.M. Barrie, Beatrix Potter, "
            "Kenneth Grahame, and Frances Burnett"
        ),
        benchmark=(
            "Benchmark against 'Alice in Wonderland', 'Peter Pan', "
            "'The Wind in the Willows', 'The Tale of Peter Rabbit'. "
            "Whimsy and wonder, not horror"
        ),
        tone_options=[
            "wonder", "curious", "adventure", "cozy",
            "enchanted", "bright", "playful",
        ],
        forbidden_tones=[
            "horror", "dread", "eerie", "scary", "tragic", "violent",
        ],
        arc_emphasis=(
            "Curiosity → magical adventure → small challenges → "
            "warm homecoming with a lesson"
        ),
        pacing_guide=(
            "Classic Victorian whimsy: friendly creatures, gentle magic, "
            "small adventures with safe stakes. Language can be slightly "
            "archaic ('what a splendid day!') for charm. End warmly, "
            "with a sense of wonder preserved"
        ),
        visual_hook_focus=(
            "Storybook English countryside, enchanted woodlands, "
            "anthropomorphic animals in waistcoats, Victorian children "
            "in pinafores. Watercolor/ink illustration aesthetic"
        ),
        rubric=[
            RubricItem("Whimsy", "Does it preserve wonder and gentle magic?"),
            RubricItem("Child safety",
                "Is every scene appropriate for ages 4-10? "
                "No real danger, no death, no betrayal"),
            RubricItem("Character charm",
                "Are characters lovable, animated, distinct? "
                "Anthropomorphic animals feel real and friendly?"),
            RubricItem("Language",
                "Does the language have classic storybook rhythm? "
                "Slightly elevated but still accessible to children"),
            RubricItem("Visual harmony",
                "Does visual_palette evoke watercolor/Tenniel/Rackham aesthetic?"),
            RubricItem("Warmth",
                "Does the story leave the reader warm and reassured?"),
        ],
        story_safety=(
            "Strict children's safety: no violence, no death (or only "
            "mentioned gently and offstage), no scary creatures, no "
            "real danger. Adventures are safe and the world is kind"
        ),
    ),
}


# ════════════════════════════════════════════════════════════════
# 加载器
# ════════════════════════════════════════════════════════════════

def _build_from_yaml_narrative(theme_id: str, sec: dict,
                                base: NarrativeProfile) -> NarrativeProfile:
    """从 yaml::narrative 段构造 NarrativeProfile,缺字段从 base 继承。"""
    rubric_raw = sec.get("rubric")
    if rubric_raw is None:
        rubric = list(base.rubric)
    else:
        rubric = []
        for item in rubric_raw:
            if isinstance(item, dict):
                rubric.append(RubricItem(
                    name=item.get("name", ""),
                    criteria=item.get("criteria", ""),
                ))
    return NarrativeProfile(
        theme_id=theme_id,
        identity=sec.get("identity", base.identity),
        benchmark=sec.get("benchmark", base.benchmark),
        tone_options=sec.get("tone_options", list(base.tone_options)),
        forbidden_tones=sec.get("forbidden_tones", list(base.forbidden_tones)),
        arc_emphasis=sec.get("arc_emphasis", base.arc_emphasis),
        pacing_guide=sec.get("pacing_guide", base.pacing_guide),
        visual_hook_focus=sec.get("visual_hook_focus", base.visual_hook_focus),
        rubric=rubric,
        story_safety=sec.get("story_safety", base.story_safety),
    )


def _soft_infer_from_agent(theme_id: str, data: dict,
                            base: NarrativeProfile) -> NarrativeProfile:
    """yaml 没写 narrative 段时,从 agent.style_guidance 软推断。

    这是兜底逻辑,意在让"还没补 narrative 段的旧 yaml"也能得到
    比 default 更贴题材的引导。
    """
    agent = data.get("agent") or {}
    desc = data.get("description") or ""
    style_g = agent.get("style_guidance", "")
    identity = agent.get("identity", "")

    # 用 agent 的信号微调 base
    extra_identity = ""
    if identity:
        extra_identity = f" (主题视觉指引: {identity})"

    extra_visual = ""
    if style_g:
        extra_visual = f" 风格指引: {style_g}"

    extra_safety = ""
    # 简单关键词嗅探,补充安全约束
    safety_keywords_kid = ["儿童", "童趣", "绘本", "child", "kid", "whimsical"]
    if any(k in desc + style_g for k in safety_keywords_kid):
        extra_safety = "题材偏童趣,避免血腥/暴力/惊悚元素"

    return NarrativeProfile(
        theme_id=theme_id,
        identity=base.identity + extra_identity,
        benchmark=base.benchmark,
        tone_options=list(base.tone_options),
        forbidden_tones=list(base.forbidden_tones),
        arc_emphasis=base.arc_emphasis,
        pacing_guide=base.pacing_guide,
        visual_hook_focus=base.visual_hook_focus + extra_visual,
        rubric=list(base.rubric),
        story_safety=base.story_safety + extra_safety,
    )


def load_narrative(theme_id: str,
                   themes_dir: Optional[Path] = None) -> NarrativeProfile:
    """加载 theme 对应的 NarrativeProfile。
    
    优先级(模仿 theme_profile.load_profile):
      1. themes/<theme_id>.yaml :: narrative 段
      2. DEFAULT_NARRATIVES[theme_id]
      3. 从 themes/<theme_id>.yaml :: agent.style_guidance 软推断
      4. DEFAULT_NARRATIVES["default"]
    
    永远返回一个合法的 NarrativeProfile,绝不抛异常。
    """
    if themes_dir is None:
        # narrative_profile.py 在 tools/long_writer/ 下,跟 theme_profile.py 同位置
        themes_dir = Path(__file__).parent.parent.parent / "themes"

    yaml_path = themes_dir / f"{theme_id}.yaml"
    base = DEFAULT_NARRATIVES.get(theme_id) or DEFAULT_NARRATIVES["default"]

    if not yaml_path.exists():
        return base

    try:
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"  [narrative_profile] 读取 {yaml_path} 失败,用内置默认: {e}")
        return base

    narrative_sec = data.get("narrative")
    if narrative_sec:
        # 路径 1: yaml 里有 narrative 段
        try:
            return _build_from_yaml_narrative(theme_id, narrative_sec, base)
        except Exception as e:
            print(f"  [narrative_profile] 解析 {theme_id}::narrative 失败,用内置默认: {e}")
            return base

    if theme_id in DEFAULT_NARRATIVES:
        # 路径 2: 内置默认有这个 theme
        return base

    # 路径 3: yaml 没 narrative 段,内置默认也没这个 theme → 软推断
    return _soft_infer_from_agent(theme_id, data, DEFAULT_NARRATIVES["default"])


# ════════════════════════════════════════════════════════════════
# 自测
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 简单冒烟测试
    for tid in ["chinese_horror_tales", "lyrical_storytelling",
                "children_storybook", "victorian_gothic_tales",
                "victorian_whimsical_children", "chinese_ghost_flux",
                "nonexistent_theme"]:
        n = load_narrative(tid, themes_dir=Path("/tmp/nonexistent"))
        print(f"\n── {tid} ──")
        print(f"  identity: {n.identity}")
        print(f"  tone_options: {n.tone_options}")
        print(f"  rubric_count: {len(n.rubric)}")
