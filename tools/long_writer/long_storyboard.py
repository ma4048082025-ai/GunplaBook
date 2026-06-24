"""
long_storyboard.py ── 章节 → 智能分镜 (Step 5) v3.0 (2026-05-28)
==================================================================
LLM 既是分镜师又是编辑: 把 1 段叙事拆成 N 个镜头, 每镜独立 narration。

输出结构 (每章 shots[] 取代 segments[]):
  shots:
    - shot_type:         extreme_wide | wide | medium | closeup | extreme_closeup
      focal_subject:     英文焦点描述 (给 FLUX)
      narration:         改写后的简短旁白 (建议 ≤60 字, 装不下拆镜)
      dialogue:          [{speaker, text}]  ← 引号对话归这里, 见铁律 1/2
      transition_in:     fade_in | match_cut | push_in | pull_out |
                         eyeline_match | whip_pan | j_cut | hold
      kb_direction:      zoom_in | zoom_out | pan_left | pan_right | pan_up
      visual_must_haves: [{element, attributes, state, exclude, mood}]
      bgm_mood:          tension | climax | melancholy | ...
      dynamic:           bool
      visible_characters: 主角名 (≤ 1 个)
      _hold_type:        extend | cutaway   (transition_in=hold 时必填)

【v3.0 精简记录】(2026-05-28)
  - DIRECTOR_RULES: 30 条铁律压缩为 8 条核心 (931 行 → 192 行)
  - V234_EXT: 5 条 EXT 模块精简 (211 行 → 100 行)
  - _build_chapter_prompt 末尾 14 条 ⚠️ 提醒清单删除 (跟 RULES 重复)
  - "1.3-1.8 倍 / 12-60 字" 硬约束话术改为软建议 (信息保留优先)

新增配套防线 (独立文件):
  - narration_integrity.py  关键信息抽取 + 引号自动迁移 + 程序对账

【保留不动的所有程序逻辑】
  - _build_chapter_prompt           prompt 拼接
  - _validate_and_clean_shots       LLM 输出校验/清洗
  - _enforce_seg_narration_no_overlap  铁律 14.3 程序兜底
  - _split_segments_for_llm_calls (v2.9.1 P0 截断修复) 通过 _generate_storyboard_chunked
  - 各 fallback / 字段截断 / 残字清理函数
  这些是项目宪法层的程序兜底, 改 prompt 不动它们才能保证零退化。

输出: scripts/<id>_storyboard.yaml (结构兼容 to_pipeline.py)
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

# v2.10 角色字段重构: render_characters 单字段 + visible_characters 派生
# storyboard_characters.py 同目录 (tools/long_writer/)。
# 双路径 import: script 模式用 `from storyboard_characters`,
#               module 模式(python -m)用 `from .storyboard_characters`。
# 模块缺失时回退到内置旧逻辑 (见 _validate_and_clean_shots)。
try:
    from storyboard_characters import (
        build_character_index, process_shot_characters,
    )
    _HAS_STORYBOARD_CHARACTERS = True
except ImportError:
    try:
        from .storyboard_characters import (
            build_character_index, process_shot_characters,
        )
        _HAS_STORYBOARD_CHARACTERS = True
    except ImportError:
        _HAS_STORYBOARD_CHARACTERS = False


SCRIPTS_DIR = Path("scripts")

VALID_SHOT_TYPES = (
    "extreme_wide", "wide", "medium", "closeup", "extreme_closeup"
)
VALID_BGM_MOODS = (
    "tension", "climax", "melancholy",
    "playful", "warm", "mystery", "epic", "serene",
)
VALID_TRANSITIONS = (
    "fade_in", "match_cut", "push_in", "pull_out",
    "eyeline_match", "whip_pan", "j_cut", "hold"
)
VALID_KB_DIRECTIONS = (
    "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"
)

DEFAULT_MAX_DYNAMIC_TOTAL = 5

# narration 长度硬约束
NARRATION_MIN_CHARS = 12
NARRATION_MAX_CHARS = 60
NARRATION_IDEAL_MAX = 50

# 缓存版本号——升级 prompt 时改这个会让旧缓存失效
SB_PROMPT_VERSION = "v3.1.0"

# ════════════════════════════════════════════════════════════════
# v2.9.1: LLM 单次调用 shot 数上限（防 output 截断）
# ════════════════════════════════════════════════════════════════
# 根本问题: SiliconFlow + DeepSeek-V3 实测 output 上限 ~5800 tokens,
# 单章 10 shots × 500 tokens = 5000+ 必撞顶,JSON 截断。
#
# 策略: segments > MAX_SHOTS_PER_LLM_CALL 时拆成多次调用,每次产 ≤ N 镜,
# 后续调用把前面已产的 shots 完整 JSON 塞进 prompt 当上下文,保证连贯性。
#
# 调参指南:
#   - SiliconFlow/DeepSeek-V3: 5 (当前)
#   - Qwen2.5-72B (假定 8192 真实可用): 7
#   - GPT-4o / Claude Sonnet:   10-12
#   - Claude Opus / Gemini 1.5: 20+ (基本不用切)
#
# 当 len(segments) <= 此值时走单次调用老路径,零行为变化。
MAX_SHOTS_PER_LLM_CALL = 5

# v2.3.4 feature flag：A 线 prompt 升级（铁律 15-19）
# 关闭时 DIRECTOR_RULES 退化为 v2.3.3 内容，行为完全跟之前一致
def _is_prompt_upgrade_enabled() -> bool:
    """读 config.ENABLE_V234_PROMPT_UPGRADE；config 没定义这个常量时默认关"""
    try:
        from config import ENABLE_V234_PROMPT_UPGRADE
        return bool(ENABLE_V234_PROMPT_UPGRADE)
    except (ImportError, AttributeError):
        return False


def _is_redux_enabled() -> bool:
    """读 config.ENABLE_V234_REDUX；用于决定是否在 prompt 里加铁律 20"""
    try:
        from config import ENABLE_V234_REDUX
        return bool(ENABLE_V234_REDUX)
    except (ImportError, AttributeError):
        return False
# 审稿器版本号
SB_REVIEW_VERSION = "v2.4.0"


# ════════════════════════════════════════════════════════════════
# 核心 prompt——智能导演 + 编辑
# ════════════════════════════════════════════════════════════════

DIRECTOR_RULES = """
【你的角色】
你是漫画/短剧分镜师 + 文字编辑。你把"叙事段落"拆成"镜头序列",
每镜自带改写后的旁白(narration)和必要的对话(dialogue)。

【核心心法】
视频不是小说, 该砍就砍——重复修饰、画面能拍的视觉细节、过场动作可省。
但故事钩子绝不能丢: 引号对话、剧情道具、悬念短语、关键事件。

═══════════════════════════════════════════════════════════════
铁律 1: 叙述层 vs 对话层 (最常踩的坑)
═══════════════════════════════════════════════════════════════
段落里三类内容, 处理方式完全不同:

  A. 旁白叙述 (无引号)     → 这是当前画面素材, 可以直接画
  B. 角色对话 ("..." 包裹)  → 这是【声音】, 画面要画【说话的人】
                              绝不可以把【台词内容】画成画面
  C. 角色心理/回忆          → 画说话的人 + 环境氛围

❌ 反例: 原文 '胡三娘冷笑: "三百年前用簪子扎进我心窝。"'
   错误产出: focal_subject 画 "300 年前簪子扎进胸口"  ← 把台词画成画面
✓ 正例:
   focal_subject: "Hu Sanniang face closeup, cold smile, speaking"
   dialogue: [{ "speaker": "胡三娘", "text": "三百年前用簪子扎进我心窝。" }]

═══════════════════════════════════════════════════════════════
铁律 2: 信息保留契约 ⭐ 故事性硬约束
═══════════════════════════════════════════════════════════════
你的工作是【拆分】, 不是【缩写】。允许改措辞、调顺序、合并冗余,
但下面 4 类信息绝对禁止丢失——它们是故事钩子, 丢一个观众就跟不上。

  ① 引号对话 ("...")
     必须出现在某镜的 dialogue 字段。
     若引号属于旁述类内心独白, speaker 用 "narrator_quote"。
     绝不允许把引号内容删除, 也不允许把引号直接塞进 narration。

  ② 剧情道具 (信件/印章/钥匙/书页等首次引入的物件)
     必须出现在某镜的 narration 或 visual_must_haves。
     这些是后续剧情的线索, 丢了观众跟不上后面情节。

  ③ 悬念短语 (以 "..." 结尾、未完成的句子, 问句)
     必须保留, 它们是故事钩子。

  ④ 关键状态变化 (含动词: 滑落/碎裂/燃起/消失/出现/抖落 等)
     必须出现在某镜的 narration。

【当信息装不下时】
  60 字的 narration 上限是【建议值】, 不是硬约束。
  - 信息装不下 → 拆成更多镜头, 每镜承载一部分
  - 一段可以拆 1-5 镜 (默认 1.3-1.8 倍, 信息密集段允许到 5 倍)
  - 不允许为压字数而砍掉上述 4 类信息

❌ 反例 (原文 5 个关键点全丢):
   原文: '"还是老规矩啊。"张三的蓝布褂扫过门楣, 牛皮信封簌簌抖落几星墨渍。'
   错误产出:
     sh01: "夕照渗进青石板缝里。"
     sh02: "王二麻子搓油污。"
   [对话、入场、信封、墨渍 — 整段关键信息全丢]

✓ 正例:
   sh01 narration: "张三的蓝布褂扫过门楣。"
        dialogue:  [{ "speaker": "张三", "text": "还是老规矩啊。" }]
   sh02 narration: "牛皮信封簌簌抖落几星墨渍。"
        focal: closeup of yellow paper envelope, ink stains, on stone floor

⚠️ 重要: 同一段原文不允许两镜反复摘抄。N 个镜头的 narration 拼起来
   ≈ 原段一遍 (不重不漏)。后置程序会扫 ≥8 字重叠并自动剥离, 你不用怕
   "拆多了"会造成重复——重复会被程序处理。

═══════════════════════════════════════════════════════════════
铁律 3: 双 lead 互动 — 同框 or 拆镜
═══════════════════════════════════════════════════════════════
两个主角互动时, 你有两个选择, 按"画面是否需要同时看到两张脸"决定:

【选择一: 双人同框】(填 render_characters 两个角色)
适用: 对话/对峙/对视/并肩/亲密互动 —— 两人都是动作主体, 观众需要
      同时看到两人的反应。
  例 "韩砚清与沈淮舟隔案对坐":
     render_characters:
       - {name: 韩砚清, region: left}
       - {name: 沈淮舟, region: right}
     focal: "two scholars sitting across a table, tense conversation,
             dim lamplight, ink path study"  (描述构图, 不堆单人细节)
  系统会用多区域 PuLID 给两人各自锁脸, 不会糊。

【选择二: 拆成单人镜】(各填 render_characters 一个角色)
适用以下情况, 拆成正反打或分镜:
  ▌ A. 物理接触 (抓/推/抱/按/拉) → 部位特写, 不锁脸
    "陈按住林的手腕": focal closeup of hand gripping wrist
                     render_characters: []   ← 都不锁脸
  ▌ B. 动作幅度大 (推/挡/救) → 拆 2 镜各一人
    sh-A [陈]: arm extended backwards, alarm   render_characters:[{name:陈}]
    sh-B [林]: stumbling backwards, surprise   render_characters:[{name:林}]
  ▌ C. 情绪需要特写 → 正反打, 各一镜单人
  ▌ D. 两人共处但无对视 (静坐/同行) → 环境镜
    "两人静坐一夜": focal wide shot of two empty stools by campfire
                   render_characters: []   narration 讲述

【判断口诀】
  需要同时看两张脸 (对话/对峙/对视) → 同框, render_characters 填 2 个
  一次只关注一人 / 物理接触 / 纯氛围 → 拆镜或环境镜, 填 0-1 个

⚠️ 远景镜 (wide/extreme_wide) 即使两人在场也别填 2 个 render_characters,
   远景脸太小 PuLID 收益低, 系统会降级。远景就 render_characters: [] 用
   focal 描述人物轮廓即可。

═══════════════════════════════════════════════════════════════
铁律 4: focal_subject 四要素 + 长度
═══════════════════════════════════════════════════════════════
focal_subject 是给 FLUX 看的英文 prompt, 必须四要素齐全:

  1. 主体 (谁/什么)        — Lin Mo face / yellow envelope / dim corridor
  2. 状态 (在做什么)        — eyes wide / falling onto floor / lit by lamp
  3. 环境 (在哪里, 光线)    — in 1990s dim room / outdoors at dusk
  4. 风格 (FLUX 知道的词)   — cinematic / shallow depth / 35mm photograph

长度: 建议 30-80 词, 上限 90 词 (超长程序会自动截断到 80 词)。

⚠️ FLUX 不擅长抽象表达, 以下要改写:
  - "X 的影子"           → "humanoid shadow next to X"
  - "第 N 个 Y"          → "another Y" 或具体描述
  - 情绪元指令            → 改成肢体/表情描述

⚠️ visual_must_haves[].element 只写"画面看得见的物", 禁人名禁抽象叙事。

═══════════════════════════════════════════════════════════════
铁律 5: render_characters (画面角色) + dialogue.speaker
═══════════════════════════════════════════════════════════════
每个镜头填 render_characters = 画面里实际可见的角色, 有序。
顺序 = 画面从左到右 (第一个→左, 第二个→右)。焦点主角放第一个。

格式:
  render_characters:
    - name: 韩砚清          # 角色中文名 (必须在角色表里)
      region: left          # 可选: left/right/center, 不填则按顺序自动分
    - name: 沈淮舟

填几个的判断标准:
  ✓ 填 2 个: 两人都是动作主体 (对话/对视/并肩, 见铁律 3 "双人同框")
  ✓ 填 1 个: 只有一人是动作主体
    "A 听到门外脚步" (B 被提及但不出镜 → 只填 A)
    "A 立于 B 的牌位前" (B 是死者/不在场 → 只填 A)
  ✓ 填 0 个 (留空): 纯景物/空镜 / 物理接触部位特写 / 远景

不要填的:
  ✗ 不在角色表里的随手编造名 (会被剔除)
  ✗ 不要填 en 英文描述 (系统自动从角色表查)
  ✗ 不要填 visible_characters (系统自动派生 LoRA 名单)

dialogue.speaker 合法范围 (与出镜无关, 画外音也能说话):
  ✓ lead 主角名 / extra 配角名 (在 characters 里声明过的)
  ✓ "narrator"        — 普通旁白
  ✓ "narrator_quote"  — 旁述里带引号的内心独白
  ✗ 不在 characters 里的随手编造名

注意: hold 镜 (extend/cutaway) 的 render_characters 通常 0-1 个。

═══════════════════════════════════════════════════════════════
铁律 6: hold 镜头 (extend + cutaway)
═══════════════════════════════════════════════════════════════
hold 镜头 = 复用上一镜画面, narration 是新内容。
transition_in="hold" 时必须填 _hold_type ("extend" 或 "cutaway")。

  ▌ extend: 画面延续, narration 是上一镜的【后续动作/细节】
    sh05 "他掀开白布, 托盘当啷一响。"
    sh06 (extend) "他忽然压低声线。"   ← 不重复 sh05 内容

  ▌ cutaway: 画面切到反应/道具特写, narration 通常是别人台词
    sh06 (cutaway) focal: 林陌面部反应特写 (focal 必须自给自足!)
    dialogue: [{ speaker: 胡三娘, text: "三百年前..." }]

强制约束:
  - hold 镜的 narration 绝不重复上一镜 (后置程序扫 ≥8 字重叠会剥离)
  - cutaway 镜的 focal_subject 必须自给自足 (含 4 要素, 不能假设延续上一镜)
  - hold 镜的 render_characters 通常 0-1 个 (cutaway 反应镜单人或纯道具)

【silent_beat 字段】(可选)
故意无旁白的留白镜, narration="" + dialogue=[] + intended_duration_sec=2.5
全章 silent_beat ≤ 2 个, 重锤勿滥用。

【intended_duration_sec 字段】(可选)
画面在音频结束后额外停留 N 秒。15-25% 镜头加此字段是合理调味, 别全加。
silent_beat=true 时该字段是整镜时长 (无音频)。

═══════════════════════════════════════════════════════════════
铁律 7: FLUX 文字回避
═══════════════════════════════════════════════════════════════
FLUX 画文字几乎必崩。涉及文字类道具 (信件/书页/票根/印章/字幕)
要在 visual_must_haves 加 "exclude" 字段排除文字:

  visual_must_haves: [{
    element: "yellowed letter on table",
    attributes: "weathered paper, brown stains",
    state: "partially open",
    exclude: "any readable text, written characters, words"
  }]

或改成"远景看不清文字"的表达。

═══════════════════════════════════════════════════════════════
铁律 8: 旁白流自洽
═══════════════════════════════════════════════════════════════
全章 narration 串起来要像一篇短文能独立读懂:

  1. 主语明确: 跨镜重新出现的角色用全名, 不要"他/她"开头
  2. 时空桥接: 场景切换处加"循着声响"/"片刻后"/"踏上石阶后"等过渡词
  3. 重复事件标"再次"/"又"以区分新旧
  4. 关键转折用 dialogue (而非把引号塞进 narration)
  5. 节奏均衡: 相邻两镜字数差 ≤ 3 倍 (允许信息保留压过此项)

═══════════════════════════════════════════════════════════════
"""


# ════════════════════════════════════════════════════════════════
# v2.3.4 A 线增量：铁律 15/16 + intended_duration + 场景记忆
# 通过 feature flag 控制拼接；关闭时分镜大师收到的是 v2.3.3 规则
# ════════════════════════════════════════════════════════════════

DIRECTOR_RULES_V234_EXT = """

═══════════════════════════════════════════════════════════════
EXT-A: 情绪曲线 (跟着 arc_role 走)
═══════════════════════════════════════════════════════════════
单镜满足规则是"语法正确", 整章节奏跟 arc_role 才是"导演意识"。

  ▌ hook 钩子      前段慢 (extreme_wide), 后段紧 (closeup), 留钩子
  ▌ rising 上升    中景为主, 景别交替, eyeline_match 多用
  ▌ climax 高潮    extreme_closeup + dynamic, push_in / whip_pan
  ▌ twist 反转     类 climax + 突兀 match_cut
  ▌ falling 收束   pull_out 拉远, 长镜头, intended_duration 偏长

这是偏好不是硬约束, 局部反向使用没问题。

═══════════════════════════════════════════════════════════════
EXT-B: 信息揭露设计 (information rationing)
═══════════════════════════════════════════════════════════════
优秀分镜不是按 narration 流水拆镜, 而是按"信息揭露的戏剧性"拆。

  ▌ 模板 A 延迟揭露
    触发: narration 含 "看见 / 发现 / 转过身 / 掀开 / 露出 / 这才看清"
    做法: 拆 3-4 镜, 局部细节 → 完整画面递进
    例 "她举起油灯, 光照向墙角, 那里坐着一个红衣女人":
      s1 closeup 油灯被举起
      s2 closeup 她瞳孔放大屏息
      s3 extreme_wide 顺光线方向看过去的房间一角 (暗)
      s4 push_in 进入: 墙角红衣轮廓渐渐清晰
      s5 closeup 红衣女人侧脸

  ▌ 模板 B 因果倒置
    触发: narration 含 "突然 / 砰 / 咔嗒 / 掉下来 / 血滴 / 碎了 / 猛地"
    做法: 先给"果"特写, 再揭"因"全貌
    例 "一滴血落在桌上":
      s1 extreme_closeup 血滴在木桌缓缓晕开 (无来源)
      s2 push_in 抬升: 镜头从桌面缓慢上摇
      s3 closeup 揭示天花板上倒挂的尸体

  ▌ 模板 C 伏笔暗藏
    触发: hook 章节中关键道具首次出现, 且后章会再次出现
    做法: 道具第一次出现给 1.5 秒 extreme_closeup, 不解释不强调

优先级 B > A > C, 不叠加。命中触发 → 在 source_seg 同号下拆多镜共享。
没命中就走常规拆分, 别强加戏。

═══════════════════════════════════════════════════════════════
EXT-C: intended_duration_sec 字段
═══════════════════════════════════════════════════════════════
每镜可选输出 intended_duration_sec (浮点秒数)。
语义: 配音结束后, 画面再静止保留 N 秒。

什么时候应该输出此字段:
  1. 情绪沉淀镜头 (角色无言反应 closeup): 1.5-3.0 秒
  2. 高潮特写让观众停留: 2.0-3.5 秒
  3. 章节结尾留白: 3.0-5.0 秒
  4. 关键道具伏笔暗藏: 1.0-2.0 秒

什么时候不输出: 普通叙事镜、快切高潮、转场镜。
全片大概 15-25% 的镜头加此字段是合理调味。

═══════════════════════════════════════════════════════════════
EXT-D: 跨章节场景记忆 (scene_registry)
═══════════════════════════════════════════════════════════════
你会收到前序章节已建立的【场景注册表】。本章中:
  1. 复用前章已建立的场景描述 (保持同一空间的视觉一致性)
  2. 复用前章已登场的关键道具描述
  3. 不要在不同章节里描述同一个客厅时给出不一致的细节

注册表格式 (你会在 prompt 里看到):
  - id="weigong_mansion_interior"
    anchor: "1990s old mansion interior, oil lamp, wooden walls, dim moonlight"

本章某镜需要还原前章场景时, 在 focal_subject 里复用 anchor 短语,
而不是凭空再造一个英文描述。FLUX 看到相似词越多, 画面越容易延续。

═══════════════════════════════════════════════════════════════
EXT-E: 本章末尾输出 chapter_summary
═══════════════════════════════════════════════════════════════
除了 shots 数组, 在 JSON 顶层额外输出 chapter_summary 字段, 登记本章
建立的场景和关键道具供后续章节复用:

{
  "shots": [ ... ],
  "chapter_summary": {
    "spaces": [
      { "id": "weigong_mansion_interior",
        "anchor": "1990s old mansion, oil lamp, wooden table",
        "first_appeared_shot": 2 }
    ],
    "key_props": [
      { "id": "yellowed_painting",
        "anchor": "yellowed Qing-era painting of a lady with back turned",
        "first_appeared_shot": 4 }
    ]
  }
}

space.id 用全小写+下划线, 跨章一致。
key_props 只登记会在后章再次出现的道具, 不要登记一次性道具。
"""


# v2.3.4 B 线增量：铁律 20（visual_ref），仅在 ENABLE_V234_REDUX=True 时拼入
DIRECTOR_RULES_V234_REDUX_EXT = """

═══════════════════════════════════════════════════════════════
铁律 20：visual_ref 字段（v2.3.4 B 线，Redux 视觉参考链）
═══════════════════════════════════════════════════════════════
每镜按 transition_in 判断 visual_ref——属于"延续型"transition 的镜头
【必须】输出 visual_ref，属于"断裂型"的【必须】留 null。
不要因为"拿不准"而省略：transition_in 已经明确告诉你这一镜与前镜的关系，
据此填写即可，这不是一个需要犹豫的字段。

字段结构：
  visual_ref:
    from_shot: <int>     # 同章节内前镜的 shot 序号（1-based，本章内）
    strength:  <float>   # 0.3-0.7 范围（Redux 强度）

下游 pipeline 收到此字段后，会用前镜真实生成的图作为 FLUX Redux 参考，
让本镜在色调/构图/光线上延续前镜，**消除"每张图凭空想象"的视觉断裂**。

【transition_in → visual_ref 规则（强制，不是建议）】

  transition_in        visual_ref
  ────────────────────────────────────────────────────────
  match_cut            必须输出，from_shot=上一镜，strength 0.6-0.7
  push_in / pull_out   必须输出，from_shot=上一镜，strength 0.4-0.5
  eyeline_match        必须输出，from_shot=上一镜，strength 0.3-0.4
  hold (cutaway)       必须输出，from_shot=源镜，strength 0.3-0.4
  fade_in / fade_out   必须 null（新场景/转场，参考会造成污染）
  whip_pan             必须 null（视觉断裂是设计意图）
  hold (extend)        必须 null（画面本身就在复用）

【规则细节】
1. from_shot 必须是 ≥1 且 < 当前 shot 序号（不能指向后面/自己）
2. 跨章节场景延续（铁律 18）由 focal_subject 文字延续承担，不由 visual_ref
   （Redux 现阶段只做同章节链路，跨章节通过场景注册表锚定）
3. strength 越高，画面跟参考图越像；越低，prompt 影响越大
   注意：strength 太高（>0.7）会让"换景别"做不出来——push_in 不要超 0.5
4. transition_in 已决定了规则（见上表），照表填写。只有 fade_in / fade_out /
   whip_pan / hold-extend 这四种才留 null，其余延续型镜头都要输出 visual_ref。
   下游若参考图缺失会自动回退普通 FLUX，所以填了不会有副作用。

【示例】

  shot 1（章节开头，establishing）：
    transition_in: fade_in
    visual_ref: null    ← 新场景

  shot 2（match_cut 接 shot 1）：
    transition_in: match_cut
    visual_ref:
      from_shot: 1
      strength: 0.65    ← 强参考，构图延续

  shot 3（hold-cutaway 反应镜头，源页 = shot 2）：
    transition_in: hold
    _hold_type: cutaway
    visual_ref:
      from_shot: 2
      strength: 0.35    ← 弱参考，保色调，换主体

  shot 4（push_in 拉到 closeup）：
    transition_in: push_in
    visual_ref:
      from_shot: 3
      strength: 0.45    ← 中等强度，换景别
"""


def _build_chapter_prompt(chapter: dict, story_meta: dict,
                           max_dynamic_remaining: int,
                           prev_chapters_summary: str,
                           scene_registry: dict = None,
                           chapter_idx: int = 0,
                           total_chapters: int = 1) -> str:
    """
    构造分镜大师 prompt。

    v2.3.4 新增参数：
      scene_registry:  跨章节场景注册表（dict[id → {anchor, first_appeared}]）
      chapter_idx:     本章在故事中的索引（0-based）
      total_chapters:  全片章节数
    """
    seg_lines = "\n".join(
        f"  [seg{i+1:02d}] {s}" for i, s in enumerate(chapter.get("segments", []))
    )

    prev_block = ""
    if prev_chapters_summary:
        prev_block = f"\n【前几章已规划的分镜】\n{prev_chapters_summary}\n"

    char_block = ""
    if story_meta.get("characters"):
        # v2.3.3：按 role 分组展示
        chars = story_meta["characters"]
        leads  = [c for c in chars if c.get("role", "lead") == "lead"]
        extras = [c for c in chars if c.get("role") == "extra"]
        groups = [c for c in chars if c.get("role") == "group"]
        parts = []
        if leads:
            parts.append("【主角（lead，必须画出稳定一致的脸，会触发 LoRA）】")
            for c in leads[:6]:
                name = c.get("name", "")
                features = c.get("key_features", "")
                if name:
                    parts.append(f"  - {name}: {features}")
        if extras:
            parts.append("【配角（extra，有戏份/有台词但无 LoRA，由 focal_subject 描述）】")
            for c in extras[:8]:
                name = c.get("name", "")
                desc = c.get("desc", "")
                features = c.get("key_features", "") or desc
                if name:
                    parts.append(f"  - {name}: {features}")
        if groups:
            parts.append("【群体（group，无个人身份，台词归 narrator 旁白代述）】")
            for c in groups[:8]:
                name = c.get("name", "")
                desc = c.get("desc", "")
                if name:
                    parts.append(f"  - {name}: {desc}")
        if parts:
            char_block = "\n" + "\n".join(parts) + "\n"

    # v2.3.4：选择 director rules（开 flag 拼 V234 增量铁律 15-19）
    if _is_prompt_upgrade_enabled():
        director_rules_full = DIRECTOR_RULES + DIRECTOR_RULES_V234_EXT
        # 进一步：如果 B 线 Redux 也开启，拼铁律 20
        if _is_redux_enabled():
            director_rules_full += DIRECTOR_RULES_V234_REDUX_EXT
    else:
        director_rules_full = DIRECTOR_RULES

    # v2.3.4：场景注册表 block（V234 feature flag 开启且 registry 非空时）
    scene_registry_block = ""
    if _is_prompt_upgrade_enabled() and scene_registry:
        lines = ["", "【前序章节已建立的场景注册表（铁律 18）】"]
        for sp_id, sp_info in (scene_registry.get("spaces", {}) or {}).items():
            anchor = sp_info.get("anchor", "")
            first_at = sp_info.get("first_appeared_global_shot", "?")
            lines.append(f"  - id=\"{sp_id}\"")
            lines.append(f"    anchor: \"{anchor}\"")
            lines.append(f"    first_appeared_global_shot: {first_at}")
        for pr_id, pr_info in (scene_registry.get("key_props", {}) or {}).items():
            anchor = pr_info.get("anchor", "")
            first_at = pr_info.get("first_appeared_global_shot", "?")
            lines.append(f"  - prop_id=\"{pr_id}\"")
            lines.append(f"    anchor: \"{anchor}\"")
            lines.append(f"    first_appeared_global_shot: {first_at}")
        lines.append("")
        lines.append("⚠️ 如果本章某镜需要还原前章场景，在 focal_subject 里复用对应")
        lines.append("    anchor 短语，让 FLUX 看到相似词以维持视觉延续。")
        scene_registry_block = "\n".join(lines) + "\n"

    # v2.3.4：章节位置信息（V234 开启时输出）
    position_block = ""
    if _is_prompt_upgrade_enabled():
        position_block = (
            f"\n【本章节在全片的位置（铁律 15 用）】\n"
            f"  章节序号：{chapter_idx + 1} / {total_chapters}\n"
            f"  arc_role：{chapter.get('arc_role', 'rising')}\n"
        )

    return f"""{director_rules_full}

═══════════════════════════════════════════════════════════════
本次任务
═══════════════════════════════════════════════════════════════

【故事整体】
标题：{story_meta.get('title', '')}
视觉色调：{story_meta.get('visual_palette', '')}
BGM色调：{story_meta.get('bgm_palette', '')}
{char_block}{prev_block}{scene_registry_block}{position_block}
【本章节】
ID：{chapter['id']}  arc_role：{chapter['arc_role']}  tone：{chapter.get('tone', 'tension')}
标题：{chapter['title']}

【本章原始段落（你需要拆成镜头序列）】
{seg_lines}

【约束】
- 本章可用 dynamic 镜头数：{max_dynamic_remaining}（不能超）
- 镜头总数预期：原段数 × 1.3~1.8 倍（信息密集段允许到 5 倍，见铁律 2）
- 每镜头 narration 建议 ≤60 字，装不下时优先拆镜，不可砍铁律 2 的 4 类信息
- 每镜头必须输出：shot_type / focal_subject / narration / transition_in /
  kb_direction / visual_must_haves / bgm_mood / dynamic /
  render_characters / previous_shot_anchor / _hold_type / dialogue
- 每镜头可选输出：intended_duration_sec、silent_beat、visual_ref（仅 Redux 模式）
- 注意：填 render_characters（画面有谁），不要填 visible_characters / en（系统派生）

【输出 JSON 格式】
返回严格 JSON（不要 markdown 代码块）：
{{
  "shots": [
    {{
      "_comment": "示例 1：establishing 镜头（无人物）",
      "source_seg": "seg01",
      "shot_type": "extreme_wide",
      "focal_subject": "1930s Shanghai French concession at night, sweltering summer haze, mansion silhouettes",
      "narration": "夏夜闷热如蒸笼，法租界沉在霉味里。",
      "dialogue": [],
      "scene_audio": {{
        "ambient": ["wind_howl"],
        "sfx": []
      }},
      "narration_emotion": "neutral",
      "transition_in": "fade_in",
      "kb_direction": "zoom_out",
      "visual_must_haves": [
        {{
          "element": "1930s Shanghai French concession exterior",
          "attributes": "vintage architecture, gas streetlamps, humid summer night",
          "state": "wide establishing shot",
          "mood": "ominous, claustrophobic",
          "exclude": "modern buildings, cars, daylight, neon signs"
        }}
      ],
      "bgm_mood": "serene",
      "dynamic": false,
      "render_characters": [],
      "previous_shot_anchor": "",
      "_hold_type": ""
    }},
    {{
      "_comment": "示例 3：说话人主镜（普通镜头 + dialogue 字段，v2.3.3 推荐模式）",
      "source_seg": "seg03",
      "shot_type": "closeup",
      "focal_subject": "Hu Sanniang's face in closeup, cold smile, red lipstick, fox-like eyes glinting, oil lamp on wooden table to her left casting warm light, dark wood paneling behind",
      "narration": "胡三娘冷笑着撩起旗袍下摆。",
      "scene_audio": {{
        "ambient": ["empty_corridor"],
        "sfx": [{{"id": "fabric_swish", "at": 1.0}}]
      }},
      "narration_emotion": "cold",
      "dialogue": [
        {{ "speaker": "胡三娘", "text": "今夜可巧，您说该不该偿命？" }}
      ],
      "transition_in": "match_cut",
      "kb_direction": "push_in",
      "visual_must_haves": [
        {{
          "element": "Hu Sanniang's face",
          "attributes": "cold smile, red lipstick, fox-like eyes",
          "state": "speaking, looking at camera",
          "mood": "menacing, supernatural",
          "exclude": "kind expression, modern clothing"
        }}
      ],
      "bgm_mood": "mystery",
      "dynamic": false,
      "render_characters": [{{ "name": "胡三娘" }}],
      "previous_shot_anchor": "dim 1990s room, oil lamp on wooden table",
      "_hold_type": ""
    }},
    {{
      "_comment": "示例 5：hold-cutaway 反应镜头（v2.3.3 单人 + focal 4 要素）",
      "source_seg": "seg05",
      "shot_type": "closeup",
      "focal_subject": "Lin Mo's face in closeup, eyes wide with shock, sweat beads on forehead and temple, oil lamp on wooden table to his left casting warm light, blurred dark wall with peeling paint in background",
      "narration": "",
      "scene_audio": {{
        "ambient": ["low_drone"],
        "sfx": [{{"id": "glass_shatter", "at": 0.5, "important": true}}]
      }},
      "narration_emotion": "neutral",
      "dialogue": [
        {{ "speaker": "胡三娘", "text": "三百年前举人老爷用这簪子扎进我姐妹心窝。" }}
      ],
      "transition_in": "hold",
      "_hold_type": "cutaway",
      "kb_direction": "zoom_in",
      "visual_must_haves": [
        {{
          "element": "Lin Mo's face",
          "attributes": "eyes wide with shock, sweat beads",
          "state": "frozen, listening, mouth slightly open",
          "mood": "terrified, paralyzed",
          "exclude": "calm, smiling, looking away"
        }}
      ],
      "bgm_mood": "climax",
      "dynamic": false,
      "render_characters": [{{ "name": "林陌" }}],
      "previous_shot_anchor": "dim 1990s room, oil lamp on wooden table"
    }},
    {{
      "_comment": "示例 6：双人同框（对话/对视，render_characters 填 2 个，系统多区域 PuLID 各锁脸）",
      "source_seg": "seg06",
      "shot_type": "medium",
      "focal_subject": "two people in a dim study facing each other across a wooden table, tense conversation, oil lamp between them, ink paintings on the wall, cinematic shallow depth",
      "narration": "两人隔案对坐，烛火在他们之间摇曳。",
      "scene_audio": {{
        "ambient": ["empty_corridor"],
        "sfx": []
      }},
      "narration_emotion": "tense",
      "dialogue": [
        {{ "speaker": "韩砚清", "text": "你当真要趟这浑水？" }}
      ],
      "transition_in": "match_cut",
      "kb_direction": "push_in",
      "visual_must_haves": [
        {{
          "element": "two people facing each other across a table",
          "attributes": "dim study, oil lamp between them",
          "state": "tense conversation, seated",
          "mood": "ominous, charged",
          "exclude": "modern clothing, bright daylight"
        }}
      ],
      "bgm_mood": "tension",
      "dynamic": false,
      "render_characters": [
        {{ "name": "韩砚清", "region": "left" }},
        {{ "name": "沈淮舟", "region": "right" }}
      ],
      "previous_shot_anchor": "dim 1990s study, oil lamp on wooden table"
    }}
  ]
}}

source_seg 是溯源标记，告诉我这个镜头来源于哪个原段（seg01/seg02/...）。
一个原段拆出多镜头时，多个镜头共享同一 source_seg 即可。

⚠️ 关键约束速查 (详见上方铁律):
  - 引号对话必须进 dialogue 字段, 不可塞 narration (铁律 1, 2)
  - 故事钩子 4 类绝不丢失: 引号/道具/悬念/事件 (铁律 2)
  - render_characters 填"画面有谁"(有序,0=左): 对话/对视填 2 个, 单人填 1 个,
    纯景物/远景/部位特写填 [] (铁律 3, 5)
  - 不要填 visible_characters / en, 系统自动派生 (铁律 5)
  - hold 必填 _hold_type, cutaway 的 focal 必须自给自足 (铁律 6)

只输出 JSON，不要任何解释文字。"""


def _generate_chapter_storyboard(chapter: dict, story_meta: dict,
                                  max_dynamic_remaining: int,
                                  prev_chapters_summary: str = "",
                                  scene_registry: dict = None,
                                  chapter_idx: int = 0,
                                  total_chapters: int = 1) -> tuple:
    """
    生成单章分镜。
    
    v2.3.4 升级：
      - 入参增加 scene_registry / chapter_idx / total_chapters
      - 返回 (shots, chapter_summary) 元组（chapter_summary 可能为空 dict）
      
    旧调用方（不传 v2.3.4 参数）仍能跑，返回值是 tuple 但旧代码若用列表推导也能 work。
    （但出于安全，所有调用方都已升级。）
    """
    segments = chapter.get("segments", [])
    if not segments:
        return [], {}

    # ───────────────────────────────────────────────────────────
    # v2.9.1: 切片调度
    # segments ≤ MAX_SHOTS_PER_LLM_CALL → 单次调用(老路径,零变化)
    # segments  > MAX_SHOTS_PER_LLM_CALL → 切两片,第二片把第一片
    #                                       完整 shots JSON 当上下文喂回去
    # ───────────────────────────────────────────────────────────
    if len(segments) <= MAX_SHOTS_PER_LLM_CALL:
        return _generate_storyboard_single_call(
            chapter, story_meta, max_dynamic_remaining,
            prev_chapters_summary, scene_registry,
            chapter_idx, total_chapters,
        )

    return _generate_storyboard_chunked(
        chapter, story_meta, max_dynamic_remaining,
        prev_chapters_summary, scene_registry,
        chapter_idx, total_chapters,
    )


def _call_llm_storyboard(prompt: str, chapter_id: str,
                          segments: list) -> tuple:
    """单次 LLM 调用,返回 (raw_shots_list, raw_chapter_summary_dict, ok)。
    
    ok=False 表示 JSON 解析失败,调用方应自己 fallback。
    raw_shots 和 raw_chapter_summary 是 LLM 原始输出(未清洗)。
    """
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage
    from feedback import clean_json

    llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                     base_url=LLM_BASE_URL, temperature=0.4,
                     max_tokens=8192,  # v2.7: 加 scene_audio 后 JSON 变长,提高上限(注意服务端上限)
                     timeout=300)  # 防止流式传输超时
    full = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        full += chunk.content

    t = clean_json(full)
    if "```" in t:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, re.DOTALL)
        if m:
            t = m.group(1)
        else:
            t = t.replace("```json", "").replace("```", "")

    try:
        raw = json.loads(t.strip())
    except json.JSONDecodeError as e:
        print(f"  [storyboard] ❌ JSON 解析失败 ({chapter_id}): {e}")
        return [], {}, False

    raw_shots = raw.get("shots", [])
    if not isinstance(raw_shots, list) or not raw_shots:
        print(f"  [storyboard] ⚠ 返回 shots 为空 ({chapter_id})")
        return [], {}, False

    raw_summary = raw.get("chapter_summary", {}) or {}
    if not isinstance(raw_summary, dict):
        raw_summary = {}
    return raw_shots, raw_summary, True


def _generate_storyboard_single_call(chapter, story_meta,
                                       max_dynamic_remaining,
                                       prev_chapters_summary,
                                       scene_registry,
                                       chapter_idx,
                                       total_chapters) -> tuple:
    """老路径:一次 LLM 调用产出整章 shots(segments ≤ MAX_SHOTS_PER_LLM_CALL)。"""
    segments = chapter.get("segments", [])
    prompt = _build_chapter_prompt(
        chapter, story_meta, max_dynamic_remaining, prev_chapters_summary,
        scene_registry=scene_registry,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
    )

    raw_shots, raw_summary, ok = _call_llm_storyboard(
        prompt, chapter["id"], segments,
    )
    if not ok:
        return _fallback_shots_from_segments(segments), {}

    cleaned_shots = _validate_and_clean_shots(
        raw_shots, max_dynamic_remaining, segments, story_meta)

    chapter_summary = {}
    if isinstance(raw_summary, dict) and _is_prompt_upgrade_enabled():
        chapter_summary = _validate_chapter_summary(raw_summary)

    return cleaned_shots, chapter_summary


def _generate_storyboard_chunked(chapter, story_meta,
                                   max_dynamic_remaining,
                                   prev_chapters_summary,
                                   scene_registry,
                                   chapter_idx,
                                   total_chapters) -> tuple:
    """v2.9.1: 切片路径,segments > MAX_SHOTS_PER_LLM_CALL 时启用。
    
    把 segments 切两半:
      - 第一片正常调 LLM
      - 第二片把第一片完整 shots 当上下文塞回 prompt
    
    chapter_summary 只在第二片产出(因为它要总览全章 spaces/key_props)。
    若第一片失败,整章 fallback。
    若第二片失败,前半保留 + 后半用 fallback,至少前半质量好。
    """
    segments = chapter.get("segments", [])
    n = len(segments)
    mid = (n + 1) // 2   # 10→5+5, 7→4+3, 11→6+5
    seg_chunk1 = segments[:mid]
    seg_chunk2 = segments[mid:]
    print(f"  [storyboard] {chapter['id']} 切片: {len(seg_chunk1)} + {len(seg_chunk2)} 段(总 {n})")

    # ─── 第一片 ───
    # 构造一个浅拷贝 chapter,只含前半 segments
    chapter_chunk1 = dict(chapter)
    chapter_chunk1["segments"] = seg_chunk1
    prompt1 = _build_chapter_prompt(
        chapter_chunk1, story_meta, max_dynamic_remaining,
        prev_chapters_summary,
        scene_registry=scene_registry,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
    )
    # 第一片提示 LLM "本次只产前 N 镜,不要产 chapter_summary,等续作"
    prompt1 = prompt1 + _build_chunk1_suffix(len(seg_chunk1), n)

    raw_shots1, _summary1, ok1 = _call_llm_storyboard(
        prompt1, f"{chapter['id']}/片1", seg_chunk1,
    )
    if not ok1:
        print(f"  [storyboard] {chapter['id']}/片1 失败,整章 fallback")
        return _fallback_shots_from_segments(segments), {}

    # ─── 第二片 ───
    chapter_chunk2 = dict(chapter)
    chapter_chunk2["segments"] = seg_chunk2
    # 第二片 prompt 的 segments 用全章 segments 编号(seg_lines 重新生成时
    # 用前缀偏移),但 _build_chapter_prompt 里 seg_lines 是 [seg01..segN] 顺序编号。
    # 为了让第二片 LLM 看到正确编号(seg06-seg10),我们传入一个 chapter 副本
    # 但 segments 内容是后半 + 用 _build_chapter_prompt_continuation 包装。
    prompt2 = _build_chapter_prompt(
        chapter_chunk2, story_meta, max_dynamic_remaining,
        prev_chapters_summary,
        scene_registry=scene_registry,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
    )
    # 替换 prompt 里的 seg_lines 编号:从 seg01 起改成 seg{mid+1:02d} 起
    # 实现方式: 用 segments 全集重新生成 seg_lines 然后只保留后半
    seg_lines_all = "\n".join(
        f"  [seg{i+1:02d}] {s}" for i, s in enumerate(segments)
    )
    seg_lines_chunk2 = "\n".join(
        f"  [seg{i+1:02d}] {s}" for i, s in enumerate(segments)
        if i >= mid
    )
    # 把 prompt2 里那段 seg_lines(只含后半 1-based 重编号的)替换为正确编号
    seg_lines_chunk2_wrong = "\n".join(
        f"  [seg{i+1:02d}] {s}" for i, s in enumerate(seg_chunk2)
    )
    if seg_lines_chunk2_wrong in prompt2:
        prompt2 = prompt2.replace(seg_lines_chunk2_wrong, seg_lines_chunk2)
    # 在 prompt2 末尾追加"续作"指示 + 第一片 JSON 上下文
    prompt2 = prompt2 + _build_chunk2_suffix(
        raw_shots1, mid, n,
    )

    raw_shots2, raw_summary2, ok2 = _call_llm_storyboard(
        prompt2, f"{chapter['id']}/片2", seg_chunk2,
    )
    if not ok2:
        print(f"  [storyboard] {chapter['id']}/片2 失败,后半用 fallback")
        # 第一片清洗后保留,第二片用 fallback
        cleaned1 = _validate_and_clean_shots(
            raw_shots1, max_dynamic_remaining, segments, story_meta)
        fallback2 = _fallback_shots_from_segments(seg_chunk2)
        # fallback2 的 source_seg 是 seg01 起,要改成 seg{mid+1:02d} 起
        for i, sh in enumerate(fallback2):
            sh["source_seg"] = f"seg{mid + i + 1:02d}"
        return cleaned1 + fallback2, {}

    # ─── 合并 + 清洗 ───
    raw_shots_all = list(raw_shots1) + list(raw_shots2)
    cleaned_shots = _validate_and_clean_shots(
        raw_shots_all, max_dynamic_remaining, segments, story_meta)

    chapter_summary = {}
    if isinstance(raw_summary2, dict) and _is_prompt_upgrade_enabled():
        chapter_summary = _validate_chapter_summary(raw_summary2)

    return cleaned_shots, chapter_summary


def _build_chunk1_suffix(chunk1_count: int, total_count: int) -> str:
    """第一片 prompt 末尾追加的提示:只产前 N 镜,不要 chapter_summary。"""
    return f"""

═══════════════════════════════════════════════════════════════
【v2.9.1 切片调度——本次只产前 {chunk1_count} 段的镜头】
═══════════════════════════════════════════════════════════════

⚠ 重要: 本章总共 {total_count} 段,但本次调用你【只需要产出前 {chunk1_count} 段】
       (seg01 - seg{chunk1_count:02d})的镜头。剩余 seg{chunk1_count+1:02d} 及以后
       的镜头会在下一次调用补全。

⚠ 不要】输出 chapter_summary 字段,它在续作时统一产出。

⚠ 输出 JSON 仍然是 {{"shots": [...]}} 结构,只是 shots 数组限定在
   source_seg ∈ [seg01..seg{chunk1_count:02d}] 范围内。

只输出 JSON,不要解释。"""


def _build_chunk2_suffix(prev_shots: list, mid: int, total: int) -> str:
    """第二片 prompt 末尾追加:第一片 shots 完整 JSON + 续作指示。
    
    prev_shots 是第一片的 raw shots(未清洗,直接给 LLM 看,保留它自己的措辞)。
    """
    try:
        prev_json = json.dumps({"shots": prev_shots}, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # 如果 prev_shots 含不可序列化对象,退化为字符串
        prev_json = str(prev_shots)
    return f"""

═══════════════════════════════════════════════════════════════
【v2.9.1 切片调度——续作:产出后 {total - mid} 段的镜头】
═══════════════════════════════════════════════════════════════

⚠ 重要: 本章总共 {total} 段,前 {mid} 段(seg01-seg{mid:02d})已经在上一次
       调用中完成。本次调用你【只需要产出后 {total - mid} 段】(seg{mid+1:02d}-seg{total:02d})的镜头。

⚠ 上一次调用产出的前 {mid} 段镜头(完整 JSON,供你保持连贯性):

```json
{prev_json}
```

⚠ 续作要求:
   1. source_seg 必须 ∈ [seg{mid+1:02d}..seg{total:02d}]
   2. 保持视觉连贯:transition_in / kb_direction / 角色描述衔接上文
   3. 不要重复上文已经画过的 focal_subject
   4. 本章末尾(最后一镜)如果是收尾性质,可以输出 chapter_summary 字段
   5. 角色英文名、外形锚点跟上文保持一致(同一角色不要换描述)

⚠ 输出 JSON: {{"shots": [...]}} (只含后半的 shots) + 可选 chapter_summary。

只输出 JSON,不要解释。"""


def _validate_chapter_summary(raw_summary: dict) -> dict:
    """
    校验并清洗 LLM 输出的 chapter_summary（铁律 19）。
    返回 {spaces: [...], key_props: [...]}，字段缺失/非法值视为空。
    """
    out = {"spaces": [], "key_props": []}

    for sp in raw_summary.get("spaces", []) or []:
        if not isinstance(sp, dict):
            continue
        sp_id = str(sp.get("id", "")).strip().lower().replace(" ", "_")
        anchor = str(sp.get("anchor", "")).strip()
        first_at = sp.get("first_appeared_shot", 0)
        if not sp_id or not anchor:
            continue
        try:
            first_at = int(first_at)
        except (TypeError, ValueError):
            first_at = 0
        out["spaces"].append({
            "id": sp_id, "anchor": anchor,
            "first_appeared_shot": first_at,
        })

    for pr in raw_summary.get("key_props", []) or []:
        if not isinstance(pr, dict):
            continue
        pr_id = str(pr.get("id", "")).strip().lower().replace(" ", "_")
        anchor = str(pr.get("anchor", "")).strip()
        first_at = pr.get("first_appeared_shot", 0)
        if not pr_id or not anchor:
            continue
        try:
            first_at = int(first_at)
        except (TypeError, ValueError):
            first_at = 0
        out["key_props"].append({
            "id": pr_id, "anchor": anchor,
            "first_appeared_shot": first_at,
        })

    return out


def _merge_scene_registry(registry: dict, chapter_summary: dict,
                           chapter_idx: int, global_shot_offset: int) -> dict:
    """
    把本章 chapter_summary 合并进全片 scene_registry。
    
    registry 结构：
      {
        "spaces":    {sp_id: {anchor, first_chapter, first_appeared_global_shot}},
        "key_props": {pr_id: {anchor, first_chapter, first_appeared_global_shot}},
      }
    
    合并规则：
      - 已存在的 id 不覆盖（保留最早登记的 anchor）
      - 新 id 加入注册表
      - first_appeared_global_shot 用全局 shot 编号（跨章累计）
    """
    if not registry:
        registry = {"spaces": {}, "key_props": {}}

    for sp in chapter_summary.get("spaces", []):
        sp_id = sp["id"]
        if sp_id not in registry["spaces"]:
            registry["spaces"][sp_id] = {
                "anchor":   sp["anchor"],
                "first_chapter": chapter_idx,
                "first_appeared_global_shot":
                    global_shot_offset + sp.get("first_appeared_shot", 1),
            }
    for pr in chapter_summary.get("key_props", []):
        pr_id = pr["id"]
        if pr_id not in registry["key_props"]:
            registry["key_props"][pr_id] = {
                "anchor":   pr["anchor"],
                "first_chapter": chapter_idx,
                "first_appeared_global_shot":
                    global_shot_offset + pr.get("first_appeared_shot", 1),
            }
    return registry


def _validate_and_clean_shots(raw_shots: list,
                                max_dynamic: int,
                                original_segments: list,
                                story_meta: dict = None) -> list:
    """校验并清洗 LLM 输出"""
    story_meta = story_meta or {}
    # v2.3.3：按 role 提取白名单
    leads_set  = set()
    extras_set = set()
    groups_set = set()
    raw_chars = story_meta.get("characters", [])
    if isinstance(raw_chars, list):
        for c in raw_chars:
            if isinstance(c, dict) and c.get("name"):
                role = (c.get("role", "lead") or "lead").lower()
                if role == "extra":
                    extras_set.add(c["name"])
                elif role == "group":
                    groups_set.add(c["name"])
                else:
                    leads_set.add(c["name"])
            elif isinstance(c, str) and c.strip():
                leads_set.add(c.strip())   # 旧 schema：当作 lead
    elif isinstance(raw_chars, dict):
        # 旧 dict 形式：全部 lead
        leads_set = set(raw_chars.keys())
    # 合法 speaker = lead + extra + narrator（group/未知降级 narrator）
    valid_speakers = leads_set | extras_set | {"narrator"}
    # 完整角色名集（含 group）
    all_known_names = leads_set | extras_set | groups_set

    # v2.10: 角色索引 (供 render_characters 解析/派生)。模块缺失则为 None,
    #        走下方旧 visible_characters 逻辑兜底。
    char_index = None
    if _HAS_STORYBOARD_CHARACTERS:
        char_index = build_character_index(story_meta)

    cleaned = []
    dynamic_used = 0

    for raw in raw_shots:
        if not isinstance(raw, dict):
            continue

        shot_type = raw.get("shot_type", "medium")
        if shot_type not in VALID_SHOT_TYPES:
            shot_type = "medium"

        transition = raw.get("transition_in", "match_cut")
        if transition not in VALID_TRANSITIONS:
            transition = "match_cut"

        kb = raw.get("kb_direction", "zoom_in")
        if kb not in VALID_KB_DIRECTIONS:
            kb = "zoom_in"

        bgm = raw.get("bgm_mood", "tension")
        if bgm not in VALID_BGM_MOODS:
            bgm = "tension"  # 临时 fallback,coordinator 会按规则重判  下一步这里要改为空

        dynamic = bool(raw.get("dynamic", False))
        if dynamic and dynamic_used >= max_dynamic:
            dynamic = False
        if dynamic:
            dynamic_used += 1

        # narration 校验
        narration = str(raw.get("narration", "")).strip()
        if not narration:
            # 空 narration 兜底——尽量从 source_seg 拿
            src = raw.get("source_seg", "")
            m = re.match(r'seg(\d+)', src)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(original_segments):
                    # v2.4.3: 用 _truncate_to_punct 替代裸切片,避免切到词中间。
                    # 旧版 [:NARRATION_IDEAL_MAX] 会直接在第 50 字切,
                    # 实际案例(ch03-sh10):"...考古铲,刃口还黏着..." 被切成
                    # "...考古铲,刃",末尾"刃"是残字。
                    narration = _truncate_to_punct(
                        original_segments[idx], NARRATION_IDEAL_MAX)
        if len(narration) > NARRATION_MAX_CHARS:
            # 硬截断到最近一个标点
            narration = _truncate_to_punct(narration, NARRATION_MAX_CHARS)

        # v2.4.3: 残字清理(独立 helper,见下方 _strip_dangling_residual)
        narration = _strip_dangling_residual(narration)

        # visual_must_haves 校验（v2.1：兼容字符串/dict 两种格式）
        # LLM 在批量场景下倾向简化为 ["broken glasses", "gramophone"] 字符串列表，
        # 这里把字符串当作 element 字段保留，不丢信息
        raw_haves = raw.get("visual_must_haves", [])
        if not isinstance(raw_haves, list):
            raw_haves = []
        clean_haves = []
        for m in raw_haves[:3]:
            if isinstance(m, dict) and m.get("element"):
                clean_haves.append({
                    "element":    str(m.get("element", "")).strip(),
                    "attributes": str(m.get("attributes", "")).strip(),
                    "state":      str(m.get("state", "")).strip(),
                    "mood":       str(m.get("mood", "")).strip(),
                    "exclude":    str(m.get("exclude", "")).strip(),
                })
            elif isinstance(m, str) and m.strip():
                # 字符串简化格式：整句作为 element
                clean_haves.append({
                    "element":    m.strip(),
                    "attributes": "",
                    "state":      "",
                    "mood":       "",
                    "exclude":    "",
                })

        # v2.10: 角色字段重构 —— render_characters 单字段 + visible_characters 派生
        # LLM 只填 render_characters (画面有谁,有序), visible_characters (LoRA 名单)
        # 由 process_shot_characters 从中派生 (只取 lead, ≤LORA_MAX_LEADS)。
        # 模块缺失时回退旧逻辑 (手解析 visible_characters,按 role 过滤,只保留 lead)。
        shot_id_for_log = str(raw.get("title", "") or f"sh{len(cleaned)+1:02d}")
        if char_index is not None:
            render_characters, visible_characters = process_shot_characters(
                raw, char_index, shot_id=shot_id_for_log
            )
        else:
            # ── 旧逻辑兜底 (模块缺失时) ──
            render_characters = []
            raw_visible = raw.get("visible_characters", [])
            if not isinstance(raw_visible, list):
                raw_visible = []
            raw_visible_names = [str(x).strip() for x in raw_visible
                                  if isinstance(x, (str, int)) and str(x).strip()]
            visible_characters = []
            for name in raw_visible_names:
                if name in leads_set:
                    visible_characters.append(name)
            if len(visible_characters) > 1:
                print(f"  [storyboard] ⚠ shot visible_characters 多人 "
                      f"{visible_characters} → 强制保留首个: {visible_characters[0]}")
                visible_characters = visible_characters[:1]

        # previous_shot_anchor 校验（v2.3 新增）
        prev_anchor = str(raw.get("previous_shot_anchor", "")).strip()

        # v2.3.2 _hold_type 校验
        # 规则：
        #   transition_in != "hold" → _hold_type 强制为 ""
        #   transition_in == "hold" 但 LLM 没给 _hold_type：
        #       有 focal_subject  → 默认 cutaway
        #       没 focal_subject  → 默认 extend
        #   transition_in == "hold" 给了 _hold_type：取 lower，必须是 extend/cutaway
        focal_str = str(raw.get("focal_subject", "")).strip()
        raw_hold_type = str(raw.get("_hold_type", "")).strip().lower()
        if transition != "hold":
            hold_type = ""
        else:
            if raw_hold_type in ("extend", "cutaway"):
                hold_type = raw_hold_type
            else:
                # LLM 没标，推断
                hold_type = "cutaway" if focal_str else "extend"

            # 强一致性：cutaway 必须有 focal；extend 应该没有 focal（清空）
            if hold_type == "cutaway" and not focal_str:
                # 标了 cutaway 却没给 focal —— 降级为 extend
                hold_type = "extend"
                focal_str = ""
                clean_haves = []
            if hold_type == "extend":
                focal_str = ""        # extend 类不需要 focal
                clean_haves = []      # 不需要 must_haves

        # v2.10: visible_characters (LoRA 名单) 已由 process_shot_characters 派生为
        # ≤LORA_MAX_LEADS (默认 1)。render_characters 不截断 (双人同框是 v2.6 常态)。
        # 旧逻辑兜底路径下这里再保险一次。
        if char_index is None and len(visible_characters) > 1:
            print(f"  [storyboard] ⚠ shot visible_characters 多人 "
                  f"{visible_characters} → 强制保留首个: {visible_characters[0]}")
            visible_characters = visible_characters[:1]

        # v2.3.3：dialogue 字段清洗（铁律 14）
        # speaker 分级：lead/extra 通过；group/未知降级 narrator
        raw_dialogue = raw.get("dialogue", [])
        if not isinstance(raw_dialogue, list):
            raw_dialogue = []
        dialogue_clean = []
        for dl in raw_dialogue:
            if not isinstance(dl, dict):
                continue
            speaker = str(dl.get("speaker", "")).strip()
            text    = str(dl.get("text", "")).strip()
            if not speaker or not text:
                continue
            # speaker 分级处理（v2.3.3）
            # narrator / narrator_quote：合法（旁白和代述旁白）
            # lead / extra：合法（专属/通用 voice）
            # group / 未建模：降级到 narrator_quote（说书人腔，比平静旁白更适合念引语）
            if speaker == "narrator" or speaker == "narrator_quote":
                pass
            elif speaker in leads_set or speaker in extras_set:
                pass
            elif speaker in groups_set:
                print(f"  [storyboard] ⚠ dialogue speaker '{speaker}' 是 group "
                      f"群体（无个人 voice），降级 narrator_quote（说书人腔代述）")
                speaker = "narrator_quote"
            else:
                # 未建模
                print(f"  [storyboard] ⚠ dialogue speaker '{speaker}' 未建模，"
                      f"降级 narrator_quote（建议补到 outline.characters）")
                speaker = "narrator_quote"
            dialogue_clean.append({"speaker": speaker, "text": text})

        # v2.3.3：narration 去重（铁律 14 二次防御）
        # 即使 prompt 反复强调，LLM 仍可能让 narration 残留 dialogue.text
        # 这里自动剥离，防止 producer 播放两遍同一台词
        if dialogue_clean and narration:
            original_narration = narration
            for dl in dialogue_clean:
                text = dl["text"]
                if not text:
                    continue
                # 尝试多种引号包裹形式 + 裸文本
                variants = [
                    f'"{text}"', f"'{text}'",
                    f'"{text}"', f"'{text}'",
                    f"「{text}」", f"『{text}』",
                    text,
                ]
                for v in variants:
                    if v and v in narration:
                        narration = narration.replace(v, "")
                        break
            # 清理孤立标点
            import re as _re2
            narration = _re2.sub(r'^[，。、！？\s"\'""''「」『』]+', '', narration)
            narration = _re2.sub(r'[，、\s"\'""''「」『』]+$', '', narration)
            narration = _re2.sub(r'[，。]{2,}', '。', narration)
            narration = narration.strip()
            if narration != original_narration.strip() and original_narration:
                # 只在被剥离时打印一次
                pass  # 不打太细日志，留给 to_pipeline 总报告

        # v2.3.4：intended_duration_sec 校验
        # 语义：音频播完后画面再静止 N 秒。仅在分镜大师认为该镜需要"停留"时输出。
        # v2.4.1：上限按 silent_beat 区分——
        #   silent_beat=true  → 这是该镜【完整】时长，上限放宽到 8.0s
        #   silent_beat=false → 这是【附加】时长，仍 ≤ 6.0s（铁律 17 旧语义）
        raw_silent = bool(raw.get("silent_beat", False))
        raw_idur = raw.get("intended_duration_sec", 0)
        try:
            intended_duration = float(raw_idur) if raw_idur is not None else 0.0
            if intended_duration < 0:
                intended_duration = 0.0
            _idur_cap = 8.0 if raw_silent else 6.0
            if intended_duration > _idur_cap:
                intended_duration = _idur_cap
        except (TypeError, ValueError):
            intended_duration = 0.0

        # v2.4.1：silent_beat 镜头兜底——必须有完整时长，缺失则给 3.0s
        if raw_silent and intended_duration <= 0:
            intended_duration = 3.0

        # v2.3.4 B 线：visual_ref 字段校验（Redux 视觉参考链）
        # 当前 shot 在同章内的索引（cleaned 长度即将到达的位置）= len(cleaned) + 1
        # （from_shot 必须 ≥1 且 < 当前 shot 序号）
        visual_ref = None
        raw_vr = raw.get("visual_ref")
        if isinstance(raw_vr, dict):
            try:
                from_shot = int(raw_vr.get("from_shot", 0))
                vr_strength = float(raw_vr.get("strength", 0.5))
                current_shot_idx = len(cleaned) + 1   # 本镜在章内的序号（1-based）
                if 1 <= from_shot < current_shot_idx and 0.0 < vr_strength <= 1.0:
                    visual_ref = {
                        "from_shot": from_shot,
                        "strength":  vr_strength,
                    }
                else:
                    if from_shot >= current_shot_idx:
                        print(f"  [storyboard] ⚠ visual_ref.from_shot={from_shot} "
                              f">= 当前 shot {current_shot_idx}，忽略")
                    elif from_shot < 1:
                        # 等同于不输出（章节开头）
                        pass
                    else:
                        print(f"  [storyboard] ⚠ visual_ref.strength={vr_strength} "
                              f"超界，忽略")
            except (TypeError, ValueError) as e:
                print(f"  [storyboard] ⚠ visual_ref 解析失败 ({e})，忽略")

        # v2.4.1：silent_beat 镜头不应携带 narration/dialogue（契约要求）。
        # coordinator 也会兜底清理,这里在源头先清一次,产出更干净。
        shot_narration = narration
        shot_dialogue = dialogue_clean
        if raw_silent:
            if shot_narration or shot_dialogue:
                print(f"  [storyboard] silent_beat 镜头携带 narration/dialogue,"
                      f"已清空")
            shot_narration = ""
            shot_dialogue = []

        cleaned.append({
            "source_seg":           raw.get("source_seg", ""),
            "shot_type":            shot_type,
            "focal_subject":        focal_str,
            "narration":            shot_narration,
            "transition_in":        transition,
            "kb_direction":         kb,
            "visual_must_haves":    clean_haves,
            "bgm_mood":             bgm,
            "dynamic":              dynamic,
            "visible_characters":   visible_characters,   # v2.3 (LoRA 名单,派生≤1)
            "render_characters":    render_characters,     # v2.10 画面角色(有序,喂 PuLID)
            "previous_shot_anchor": prev_anchor,           # v2.3
            "_hold_type":           hold_type,             # v2.3.2
            "dialogue":             shot_dialogue,         # v2.3.3
            "intended_duration_sec": intended_duration,    # v2.3.4
            "silent_beat":           raw_silent,           # v2.4.1
            "visual_ref":            visual_ref,            # v2.3.4 B 线
            # v2.7 新增: 音频工程字段
            "scene_audio": _validate_scene_audio(raw.get("scene_audio")),
            "narration_emotion": _validate_emotion(raw.get("narration_emotion", "")),
        })

    if not cleaned:
        return _fallback_shots_from_segments(original_segments)

    # ─────────────────────────────────────────────────────────────────────
    # v2.4.2 铁律 14.3 后置校验：同 source_seg 内 narration 不重叠
    # ─────────────────────────────────────────────────────────────────────
    cleaned = _enforce_seg_narration_no_overlap(cleaned)

    return cleaned


# ────────────────────────────────────────────────────────────────────────
# v2.4.2 铁律 14.3 工具函数
# ────────────────────────────────────────────────────────────────────────

# 子串包含的最小匹配长度（与铁律 14.2 / coordinator 窗口去重的阈值一致）
_SEG_OVERLAP_MIN_CHARS = 8

# narration 去标点用的字符集（与 coordinator._strip_punct 对齐：含破折号、省略号）
_SEG_OVERLAP_PUNCT = "，。！？、；：,.!?;:\"\"''「」『』《》〈〉()（）—…·～~ \t\n\r"


def _seg_overlap_strip_punct(text: str) -> str:
    """剥掉标点和空白，留下纯文字用于子串比对。"""
    if not text:
        return ""
    return "".join(c for c in text if c not in _SEG_OVERLAP_PUNCT)


def _seg_overlap_find_overlap(curr_core: str, prev_core: str) -> str:
    """
    返回 prev_core 中【在 curr_core 里出现过】的最长子串（≥ _SEG_OVERLAP_MIN_CHARS）。
    双向：同时检查 curr 是否为 prev 的子串、prev 是否为 curr 的子串。
    没命中返回 ""。
    """
    if not curr_core or not prev_core:
        return ""
    # 取较短者作为"可能被包含的一方"，在较长者里直接子串查找
    short, long = (curr_core, prev_core) if len(curr_core) <= len(prev_core) else (prev_core, curr_core)
    if len(short) >= _SEG_OVERLAP_MIN_CHARS and short in long:
        return short
    # 不是整段包含 —— 滑窗找最长公共子串（控制在合理 O(n*m) 内，n/m 一般 < 100）
    best = ""
    n, m = len(curr_core), len(prev_core)
    for i in range(n):
        for j in range(m):
            k = 0
            while (i + k < n and j + k < m
                   and curr_core[i + k] == prev_core[j + k]):
                k += 1
            if k >= _SEG_OVERLAP_MIN_CHARS and k > len(best):
                best = curr_core[i:i + k]
    return best


def _seg_overlap_strip_substr(text: str, substr_core: str) -> str:
    """
    把 text 中【去标点后等于 substr_core 的连续段】整体剥掉。
    因为 substr_core 是去标点形式，原 text 里对应位置可能夹着标点，
    所以用"逐字符匹配 core"的方式扫描原文，命中就连同标点一起删。
    """
    if not text or not substr_core:
        return text
    result = []
    i = 0
    n = len(text)
    cs = len(substr_core)
    while i < n:
        # 尝试从位置 i 开始匹配 substr_core（允许中间夹标点）
        j = i
        k = 0
        while j < n and k < cs:
            ch = text[j]
            if ch in _SEG_OVERLAP_PUNCT:
                j += 1
                continue
            if ch == substr_core[k]:
                j += 1
                k += 1
            else:
                break
        if k == cs:
            # 完全匹配 —— 跳过 [i, j)，不写入 result
            i = j
        else:
            result.append(text[i])
            i += 1
    out = "".join(result)
    # 收尾：清掉残留的开头/结尾标点和首位破折号串
    out = out.strip()
    while out and out[0] in "，。！？、；：—…·":
        out = out[1:].strip()
    while out and out[-1] in "，、；：—…·":
        out = out[:-1].strip()

    # v2.4.3: 清掉"孤立悬空残字"
    # 病例(实际项目 ch03-sh10):剥离重叠后留下 "...考古铲，刃",
    #         末尾"刃"是个前后断了的孤字(本来是"刃口...")—— 配音会读出语义残缺。
    # 策略:把 narration 按中文标点切段,
    #     - 末尾片段如果只剩 1 个汉字(中文断点切的"残半词")→ 整段砍掉
    #     - 同样适用于开头(虽然实战极少出现)
    # 之所以是"1 字"而不是"2 字":2 字常常是合法词("月光""鬼臂"),误伤风险高;
    # 1 字几乎一定是残半词(中文 narration 几乎不会以单字成句结尾)。
    _CN_PUNCT = "，。！？、；：—…·"
    if out:
        # 末尾片段
        last_punct_idx = max(
            (out.rfind(p) for p in _CN_PUNCT), default=-1)
        if last_punct_idx >= 0:
            tail = out[last_punct_idx + 1:].strip()
            if len(tail) == 1 and '\u4e00' <= tail <= '\u9fff':
                # 末尾孤字 → 连同前面的标点一起砍
                out = out[:last_punct_idx].rstrip(_CN_PUNCT + " ")
        # 开头片段(类似逻辑)
        first_punct_idx = -1
        for i, ch in enumerate(out):
            if ch in _CN_PUNCT:
                first_punct_idx = i
                break
        if first_punct_idx > 0:
            head = out[:first_punct_idx].strip()
            if len(head) == 1 and '\u4e00' <= head <= '\u9fff':
                out = out[first_punct_idx + 1:].lstrip(_CN_PUNCT + " ")

    return out


def _enforce_seg_narration_no_overlap(shots: list) -> list:
    """
    铁律 14.3 后置校验：按 source_seg 分组，扫描组内任意两镜的 narration 重叠。

    策略：组内按出现顺序遍历，每个镜头 (curr) 跟它前面所有同组镜头 (prev) 对比。
    命中（≥ 8 字子串包含，双向）→ 从 curr 剥掉重叠片段，记 _revision_notes。
    剥光后若 narration 为空 且 非 hold 且 非 silent_beat → 转 _hold=extend，
    并入前一镜（不是前一同组镜，是真正紧邻的前一镜，跟 coordinator 行为一致）。

    hold 镜头本身已被铁律 14.2 管，这里跳过（_hold=true 的镜头不做 14.3 校验）。
    silent_beat 镜头 narration 本来就该是空，跳过。
    """
    if not shots:
        return shots

    # 按 source_seg 分组（保留全局索引）
    seg_groups: dict[str, list[int]] = {}
    for idx, sh in enumerate(shots):
        # 跳过 hold / silent_beat / 空 narration 的镜头（不做 14.3 检查）
        if sh.get("_hold") or sh.get("silent_beat"):
            continue
        narr = (sh.get("narration") or "").strip()
        if not narr:
            continue
        seg = sh.get("source_seg") or ""
        if not seg:
            continue
        seg_groups.setdefault(seg, []).append(idx)

    for seg_id, idx_list in seg_groups.items():
        if len(idx_list) < 2:
            continue  # 单镜头独占 segment，无重叠可能

        for pos, curr_idx in enumerate(idx_list):
            if pos == 0:
                continue  # 组内第一镜，无前驱
            curr_sh = shots[curr_idx]
            curr_narr = (curr_sh.get("narration") or "").strip()
            if not curr_narr:
                continue
            curr_core = _seg_overlap_strip_punct(curr_narr)
            if len(curr_core) < _SEG_OVERLAP_MIN_CHARS:
                continue

            hit_sources: list[str] = []  # 记录"抄了哪些 prev"，用于日志
            overlap_substrs: list[tuple[int, str]] = []  # (prev_idx, core_substr)

            # 跟组内所有前驱比对（不止前一个，因为 sh04 同时抄 sh02+sh03）
            for prev_pos in range(pos):
                prev_idx = idx_list[prev_pos]
                prev_narr = (shots[prev_idx].get("narration") or "").strip()
                if not prev_narr:
                    continue
                prev_core = _seg_overlap_strip_punct(prev_narr)
                if len(prev_core) < _SEG_OVERLAP_MIN_CHARS:
                    continue
                overlap = _seg_overlap_find_overlap(curr_core, prev_core)
                if overlap:
                    overlap_substrs.append((prev_idx, overlap))
                    hit_sources.append(f"shot#{prev_idx}")

            if not overlap_substrs:
                continue

            # 一次性剥光所有重叠：按 substring 长度从长到短剥，避免短串先剥掉破坏长串匹配
            new_narr = curr_narr
            for _, sub in sorted(overlap_substrs, key=lambda x: -len(x[1])):
                new_narr = _seg_overlap_strip_substr(new_narr, sub)
                # 重新算 core，防止把后续短串当作"已剥过"
                if not _seg_overlap_strip_punct(new_narr):
                    break

            # 记录修订笔记（reviewer="seg_overlap"，跟其他 reviewer 同结构）
            notes = curr_sh.setdefault("_revision_notes", [])
            notes.append({
                "reviewer": "seg_overlap",
                "field": "narration",
                "issue": f"铁律 14.3：与同 segment 前驱 {','.join(hit_sources)} 存在 ≥{_SEG_OVERLAP_MIN_CHARS} 字重叠，已自动剥离",
                "before": curr_narr,
                "after": new_narr,
            })

            # 剥光了 —— 镜头无独立内容，按文档承诺转 extend hold 并入前一镜
            if not _seg_overlap_strip_punct(new_narr):
                curr_sh["narration"] = ""
                # 真正紧邻的前一镜（不是同 seg 的前一镜，而是 shots 列表里的前一个）
                if curr_idx > 0:
                    curr_sh["_hold"] = True
                    curr_sh["_hold_type"] = "extend"
                    # _hold_source_page 留给 to_pipeline / coordinator 填，
                    # 此处不强行写入，保持职责分层
                # else: 章首镜头剥光 —— 极少见，留空 narration，由 coordinator 兜底
            else:
                # v2.4.3: 剥离前缀可能让原本被覆盖的末尾残字暴露出来 → 再清一次
                curr_sh["narration"] = _strip_dangling_residual(new_narr)

    return shots


def _strip_dangling_residual(narration: str) -> str:
    """
    清掉 narration 末尾的"孤立汉字残片"(v2.4.3 新增)。

    病例:LLM 按字符数硬切 narration 时,可能切到词中,留下半个词。
    例(实际项目 ch03-sh10):
      原文: "...高举考古铲，刃口黏着碎肉"
      LLM 截: "...高举考古铲，刃"  ← 末尾"刃"是"刃口"的半个字
      配音读出"...高举考古铲、刃"听感灾难。

    判定:末尾片段(最后一个标点之后)是 1 个孤立汉字 → 砍。
    保护:
      - 砍后至少留 8 字(避免把整句砍光,8 字是 narration 最小可用长度的一半)
      - 末尾已经是标点或合法多字结尾 → 不动
      - 完全没有标点的极短 narration → 不动

    误伤分析:
      合法 1 字结尾(如"...好啊!")通常以 ! / ? / 。 收尾,会被标点保护命中,不进剥离;
      "...,啊" 这种纯感叹收尾合法但极罕见,代价可接受。
    """
    if not narration or len(narration) < 12:
        return narration
    # 末尾本来就是标点 → 不动
    if narration[-1] in "。！？，、；：,;:.!?\"\"''…—":
        return narration
    # 找最后一个标点位置
    last_punct_pos = -1
    for p in "，。、；：,;:.!?——……":
        idx = narration.rfind(p)
        if idx > last_punct_pos:
            last_punct_pos = idx
    if last_punct_pos < 0:
        return narration   # 整段无标点 → 不敢动
    tail = narration[last_punct_pos + 1:].strip()
    # 末尾片段 = 1 个孤立汉字 → 残片嫌疑
    if (len(tail) == 1
        and '\u4e00' <= tail[0] <= '\u9fff'):
        cleaned = narration[:last_punct_pos].rstrip(
            "，。、；：,;:.!?——…… ")
        if len(cleaned) >= 8:
            return cleaned
    return narration

# ────────────────────────────────────────────────────────────────────
# v2.7 scene_audio / narration_emotion validator
# ────────────────────────────────────────────────────────────────────

_VALID_AMBIENT_IDS = {
    "rain_light", "rain_heavy", "wind_howl", "thunder_distant",
    "crickets_night", "temple_interior", "old_house_creak",
    "graveyard_night", "marketplace_chinese", "well_echo",
    "mountain_night", "candle_flame", "empty_corridor",
    "low_drone", "basement_drip",
}

_VALID_SFX_IDS = {
    "thunder_crack", "bell_toll", "small_bell_ring", "glass_shatter",
    "door_creak", "door_slam", "footstep_wood", "footstep_stone",
    "heartbeat_single", "heartbeat_loop", "breath_gasp", "body_fall",
    "paper_rustle", "coin_drop", "blood_drip", "lamp_drop_metal",
    "magic_shimmer", "ghost_whisper", "branch_snap", "wind_gust",
    "fabric_swish", "cloth_tear", "mirror_resonance",
    # transition
    "whoosh", "riser", "sub_drop", "chapter_strike",
}

_VALID_EMOTIONS = {
    "neutral", "whisper", "desperate", "cold", "trembling",
    "eerie", "grave", "urgent",
}


def _validate_scene_audio(sa) -> dict:
    """校验 + 清洗 scene_audio 字段。非法 id 过滤,超出上限截断。"""
    if not isinstance(sa, dict):
        return {"ambient": [], "sfx": []}

    # ambient: 白名单过滤,最多 2 个
    raw_amb = sa.get("ambient") or []
    if not isinstance(raw_amb, list):
        raw_amb = []
    clean_amb = []
    for a in raw_amb[:2]:
        if isinstance(a, str) and a in _VALID_AMBIENT_IDS:
            clean_amb.append(a)

    # sfx: 白名单过滤,最多 2 个
    raw_sfx = sa.get("sfx") or []
    if not isinstance(raw_sfx, list):
        raw_sfx = []
    clean_sfx = []
    for s in raw_sfx[:2]:
        if not isinstance(s, dict):
            continue
        sid = s.get("id", "")
        if sid not in _VALID_SFX_IDS:
            continue
        clean_sfx.append({
            "id":        sid,
            "at":        float(s.get("at", 0.0) or 0.0),
            "important": bool(s.get("important", False)),
        })

    return {"ambient": clean_amb, "sfx": clean_sfx}


def _validate_emotion(emo: str) -> str:
    """校验 emotion 字段,非法的归为 neutral。"""
    if not isinstance(emo, str):
        return "neutral"
    emo = emo.strip().lower()
    return emo if emo in _VALID_EMOTIONS else "neutral"

def _truncate_to_punct(text: str, max_len: int) -> str:
    """截到 max_len 内最近的安全切点(v2.4.3 升级)。

    输出保证: 返回长度 <= max_len(契约,下游靠这个)。

    切点优先级(由强到弱):
      1. 中文句末标点 (。！？)            ── 最干净,语义完整
      2. 中文/英文句中标点 (，、；,.;)    ── 次干净,语义完整
      3. 破折号、省略号 (——……)          ── 视觉边界,可接受
      4. 空格 (英文 / 混排)                ── 词边界
      5. 硬切并砍掉末尾的孤立汉字残字     ── 兜底,保证不留半个词

    旧版的"找不到就硬切"会切到中文词中间(实际案例 ch03-sh10:
    "考古铲,刃[口...]" → "...,刃"),配音读出来是半个词。
    新版第 5 优先级会把切后的末尾孤字也砍掉,例:"...考古铲，刃" → "...考古铲"。
    """
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # 优先级 1:句末标点 (最干净)
    for punct in "。！？.!?":
        idx = cut.rfind(punct)
        if idx > max_len * 0.5:
            return cut[:idx + 1]
    # 优先级 2:句中标点 (语义完整)
    for punct in "，、；,;:":
        idx = cut.rfind(punct)
        if idx > max_len * 0.5:
            return cut[:idx]   # 切在标点前,不保留逗号
    # 优先级 3:破折号、省略号
    for punct in ["——", "……"]:
        idx = cut.rfind(punct)
        if idx > max_len * 0.5:
            return cut[:idx]
    # 优先级 4:空格
    idx = cut.rfind(" ")
    if idx > max_len * 0.5:
        return cut[:idx]
    # 优先级 5:硬切,但末尾如果是中文孤字残片(末段无标点,长度过短)则砍掉。
    # 例:"...考古铲,刃" 在前面 4 道都没命中时,这里会再砍一道。
    # 实战中这条几乎不会触发(因为优先级 2 通常能命中),只在罕见"一气呵成中文长句"时兜底。
    # 找 cut 中最后一个标点,看末尾片段是否是孤字
    last_punct = -1
    for p in "，。、；：,;:.!?——……":
        idx = cut.rfind(p)
        if idx > last_punct:
            last_punct = idx
    if last_punct >= 0:
        tail = cut[last_punct + 1:]
        if len(tail.strip()) <= 1:
            # 末尾片段只有 0-1 字 → 跟标点一起砍
            return cut[:last_punct].rstrip("，。、；：,;:.!? ")
    return cut


def _summarize_revisions(chapter_id: str, shots: list) -> dict:
    """从修订后的 shots 提取统计数据，给全片总结用"""
    by_reviewer = {}
    n_revisions = 0
    for s in shots:
        notes = s.get("_revision_notes", []) or []
        for n in notes:
            rv = n.get("reviewer", "unknown")
            by_reviewer[rv] = by_reviewer.get(rv, 0) + 1
            n_revisions += 1
    return {
        "id": chapter_id,
        "n_shots": len(shots),
        "n_revisions": n_revisions,
        "by_reviewer": by_reviewer,
    }


def _fallback_shots_from_segments(segments: list) -> list:
    """LLM 失败时的兜底：每段一个 medium 镜头"""
    fallback_dirs = ["zoom_in", "pan_right", "zoom_out", "pan_left", "zoom_in"]
    out = []
    for i, seg in enumerate(segments):
        narration = seg if len(seg) <= NARRATION_MAX_CHARS else _truncate_to_punct(seg, NARRATION_MAX_CHARS)
        out.append({
            "source_seg":           f"seg{i+1:02d}",
            "shot_type":            "medium",
            "focal_subject":        "atmospheric scene",
            "narration":            narration,
            "transition_in":        "match_cut" if i > 0 else "fade_in",
            "kb_direction":         fallback_dirs[i % len(fallback_dirs)],
            "visual_must_haves":    [],
            "bgm_mood":             "tension",
            "dynamic":               False,
            "visible_characters":    [],
            "render_characters":     [],        # v2.10
            "previous_shot_anchor":  "",
            "_hold_type":            "",         # v2.3.2 fallback 不用 hold
            "dialogue":              [],         # v2.3.3
            "intended_duration_sec": 0.0,         # v2.3.4
            "silent_beat":           False,       # v2.4.1
            "visual_ref":            None,         # v2.3.4 B 线
        })
    return out


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def generate_storyboard(segments_path: str,
                        max_dynamic_total: int = DEFAULT_MAX_DYNAMIC_TOTAL,
                        enable_review: bool = True,
                        enabled_reviewers: list = None,
                        review_only: str = None):
    """
    生成全片分镜表。
    
    enable_review:     是否启用审稿（默认 True）
    enabled_reviewers: 启用哪些审稿员，None 表示全开
                       可传 ["narrative", "flux"] 之类
    review_only:       只重审某章（如 "ch03"），跳过未指定章节的生成和审稿
    """
    seg_path = Path(segments_path)
    if not seg_path.exists():
        print(f"  ❌ 找不到 segments 文件: {seg_path}")
        return

    with open(seg_path, encoding="utf-8") as f:
        seg_data = yaml.safe_load(f)

    # 读 outline 拿 visual_palette/bgm_palette/characters
    outline_path = seg_data.get("outline_path")
    story_meta = {
        "title":    seg_data.get("title", ""),
        "story_id": seg_data.get("story_id", ""),   # v2.3：传给 reviewers 写日志
    }
    if outline_path and Path(outline_path).exists():
        with open(outline_path, encoding="utf-8") as f:
            outline = yaml.safe_load(f)
        story_meta.update({
            "visual_palette": outline.get("visual_palette", ""),
            "bgm_palette":    outline.get("bgm_palette", ""),
            "characters":     outline.get("characters", []),
        })

    chapters = seg_data.get("chapters", [])
    valid_chapters = [c for c in chapters if c.get("body")]

    print(f"\n{'='*55}")
    print(f"  长故事智能分镜规划 v2.2")
    print(f"  故事：{seg_data.get('title')}")
    print(f"  共 {len(valid_chapters)} 章 / 段总数 ≈ "
          f"{sum(len(c.get('segments', [])) for c in valid_chapters)}")
    print(f"  动态镜头预算：{max_dynamic_total}")
    if enable_review:
        rv_list = enabled_reviewers or ["narrative", "visual", "flux", "dialogue"]
        print(f"  审稿员：{', '.join(rv_list)}（每章 +{len(rv_list)} 次 LLM 调用）")
    else:
        print(f"  审稿员：禁用 (--no-review)")
    if review_only:
        print(f"  ⚠ 只处理章节: {review_only}")
    print(f"{'='*55}\n")

    out_chapters = []
    dynamic_used_total = 0
    chapter_summaries = []   # 全片审稿总结用

    # v2.3.4：跨章节场景注册表（铁律 18/19）
    scene_registry = {"spaces": {}, "key_props": {}}
    global_shot_count = 0    # 用于在注册表里登记 first_appeared_global_shot

    cache_root = SCRIPTS_DIR / f"_{seg_data['story_id']}_sb_cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    for i, ch in enumerate(valid_chapters):
        # review_only：跳过未指定章节（保留已有缓存内容）
        if review_only and ch.get("id") != review_only:
            # 仍尝试从缓存读取，让 out_chapters 完整
            ch_id = ch.get("id", f"ch{i:02d}")
            existing = list(cache_root.glob(f"{ch_id}_*_reviewed_*.json"))
            if not existing:
                existing = list(cache_root.glob(f"{ch_id}_*.json"))
                # 排除审稿缓存（避免重复）
                existing = [p for p in existing if "_reviewed_" not in p.name]
            if existing:
                try:
                    raw_cached = json.loads(existing[0].read_text(encoding="utf-8"))
                    # v2.3.4：缓存可能是 list 或 dict
                    if isinstance(raw_cached, dict):
                        cached_shots = raw_cached.get("shots")
                        ch_summary   = raw_cached.get("chapter_summary", {}) or {}
                    elif isinstance(raw_cached, list):
                        cached_shots = raw_cached
                        ch_summary   = {}
                    else:
                        cached_shots = None
                        ch_summary   = {}
                    if isinstance(cached_shots, list):
                        out_chapters.append({
                            "id": ch["id"], "title": ch["title"],
                            "arc_role": ch["arc_role"],
                            "tone": ch.get("tone", "tension"),
                            "shots": cached_shots,
                            "chapter_summary": ch_summary,   # v2.3.4
                        })
                        dynamic_used_total += sum(1 for s in cached_shots if s.get("dynamic"))
                        # v2.3.4：更新全片注册表
                        if _is_prompt_upgrade_enabled() and ch_summary:
                            scene_registry = _merge_scene_registry(
                                scene_registry, ch_summary,
                                chapter_idx=i, global_shot_offset=global_shot_count,
                            )
                        global_shot_count += len(cached_shots)
                        continue
                except Exception:
                    pass
            # 没有缓存就跳过
            print(f"  [{i+1}/{len(valid_chapters)}] {ch['id']} [跳过：review-only={review_only}]")
            continue

        remaining = max(0, max_dynamic_total - dynamic_used_total)

        arc = ch.get("arc_role", "rising")
        budget = remaining
        if arc in ("hook", "setup", "falling", "resolution"):
            budget = min(remaining, 0)
        elif arc == "rising":
            budget = min(remaining, 1)
        elif arc in ("climax", "twist"):
            budget = min(remaining, 2)

        # 缓存 key 加 prompt 版本号——升级 prompt 自动失效旧缓存
        ch_sig = "|".join([
            f"version={SB_PROMPT_VERSION}",
            str(ch.get("id", "")),
            str(ch.get("title", "")),
            str(ch.get("body", ""))[:5000],
            "|".join(str(s) for s in ch.get("segments", [])),
            f"budget={budget}",
            f"arc={arc}",
        ])
        ch_key = hashlib.md5(ch_sig.encode("utf-8")).hexdigest()[:12]
        cache_file = cache_root / f"{ch.get('id', f'ch{i:02d}')}_{ch_key}.json"

        # 审稿缓存独立文件（key 包含审稿员列表 + 审稿版本号）
        rv_list = enabled_reviewers or ["narrative", "visual", "flux", "dialogue"]
        rv_sig = ",".join(sorted(rv_list)) if enable_review else "no_review"
        review_key = hashlib.md5(
            f"{ch_key}|{SB_REVIEW_VERSION}|{rv_sig}".encode("utf-8")
        ).hexdigest()[:8]
        reviewed_cache_file = cache_root / (
            f"{ch.get('id', f'ch{i:02d}')}_{ch_key}_reviewed_{review_key}.json"
        )

        # ── 优先使用审稿后的缓存 ──────────────────────────────
        if reviewed_cache_file.exists() and ch.get("id") != review_only:
            try:
                cached = json.loads(reviewed_cache_file.read_text(encoding="utf-8"))
                shots = cached.get("shots") if isinstance(cached, dict) else cached
                # v2.3.4：审稿缓存可能也带 chapter_summary
                reviewed_ch_summary = (cached.get("chapter_summary", {})
                                       if isinstance(cached, dict) else {}) or {}
                if shots and isinstance(shots, list):
                    n_revised = sum(1 for s in shots if s.get("_revision_notes"))
                    print(f"  [{i+1}/{len(valid_chapters)}] {ch['id']} "
                          f"[{arc:10s}] [reviewed cache ✓] {len(shots)} 镜头, "
                          f"{n_revised} 已修订")
                    ch_dyn = sum(1 for s in shots if s.get("dynamic"))
                    dynamic_used_total += ch_dyn
                    out_chapters.append({
                        "id": ch["id"], "title": ch["title"],
                        "arc_role": ch["arc_role"],
                        "tone": ch.get("tone", "tension"),
                        "shots": shots,
                        "chapter_summary": reviewed_ch_summary,  # v2.3.4
                    })
                    chapter_summaries.append(_summarize_revisions(ch["id"], shots))
                    # v2.3.4：更新全片场景注册表
                    if _is_prompt_upgrade_enabled() and reviewed_ch_summary:
                        scene_registry = _merge_scene_registry(
                            scene_registry, reviewed_ch_summary,
                            chapter_idx=i, global_shot_offset=global_shot_count,
                        )
                    global_shot_count += len(shots)
                    continue
            except Exception as e:
                print(f"  [cache] 审稿缓存损坏: {e}")

        # ── 优先使用生成缓存（避免重复 LLM 第一遍）─────────────
        shots = None
        cached_chapter_summary = {}  # v2.3.4
        if cache_file.exists():
            try:
                raw_cached = json.loads(cache_file.read_text(encoding="utf-8"))
                # v2.3.4 缓存格式兼容：旧版是 list，新版是 dict
                if isinstance(raw_cached, dict):
                    shots = raw_cached.get("shots")
                    cached_chapter_summary = raw_cached.get("chapter_summary", {}) or {}
                elif isinstance(raw_cached, list):
                    shots = raw_cached  # 旧 cache 兼容
                if shots and isinstance(shots, list):
                    print(f"  [{i+1}/{len(valid_chapters)}] {ch['id']} "
                          f"[{arc:10s}] [生成缓存 ✓] {len(shots)} 镜头")
                else:
                    shots = None
            except Exception as e:
                print(f"  [cache] 生成缓存损坏（重生）: {e}")
                shots = None

        # ── 没有生成缓存，调 LLM 生成 ─────────────────────────
        if shots is None:
            n_segs = len(ch.get("segments", []))
            print(f"  [{i+1}/{len(valid_chapters)}] {ch['id']} "
                  f"[{arc:10s}] {n_segs} 段输入，预算 {budget} 动态")

            # 前章摘要（最近 3 章）
            prev_summary_lines = []
            for prev_ch in out_chapters[-3:]:
                prev_shots = prev_ch.get("shots", [])
                shot_types = [s.get("shot_type", "?") for s in prev_shots]
                shot_str = " / ".join(shot_types[:8])
                if len(shot_types) > 8:
                    shot_str += " / ..."
                focals = [s.get("focal_subject", "")
                          for s in prev_shots if s.get("focal_subject")]
                focal_str = " | ".join(focals[:2])
                prev_summary_lines.append(
                    f"{prev_ch['id']} [{prev_ch.get('arc_role', '?')}]: "
                    f"shots={shot_str}; key_focals={focal_str}")
            prev_summary = "\n".join(prev_summary_lines)

            # v2.3.4：传入跨章注册表 + 章节位置
            shots, cached_chapter_summary = _generate_chapter_storyboard(
                ch, story_meta, budget,
                prev_chapters_summary=prev_summary,
                scene_registry=scene_registry if _is_prompt_upgrade_enabled() else None,
                chapter_idx=i,
                total_chapters=len(valid_chapters),
            )

            # 写第一遍生成缓存（v2.3.4 新格式：dict 含 shots + chapter_summary）
            try:
                cache_file.write_text(
                    json.dumps({
                        "shots": shots,
                        "chapter_summary": cached_chapter_summary,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception as e:
                print(f"  [cache] 写入失败（不影响主流程）: {e}")

        # v2.3.4：把本章 chapter_summary 合并进全片注册表
        if _is_prompt_upgrade_enabled() and cached_chapter_summary:
            scene_registry = _merge_scene_registry(
                scene_registry, cached_chapter_summary,
                chapter_idx=i, global_shot_offset=global_shot_count,
            )
        # 累加全局 shot 编号
        global_shot_count += len(shots) if shots else 0

        # ── 审稿（如果启用）──────────────────────────────────
        revision_log = []
        if enable_review and shots:
            try:
                from long_writer.reviewers import run_all_reviewers
                from revision_printer import print_revision_report
            except ImportError:
                try:
                    from .reviewers import run_all_reviewers
                    from .revision_printer import print_revision_report
                except ImportError as e:
                    print(f"  [审稿] 模块导入失败，跳过: {e}")
                    run_all_reviewers = None

            if run_all_reviewers:
                # 给审稿员准备 chapter context（含 body）
                chapter_for_review = {
                    "id":       ch.get("id"),
                    "title":    ch.get("title"),
                    "arc_role": ch.get("arc_role"),
                    "tone":     ch.get("tone", "tension"),
                    "body":     ch.get("body", ""),
                    "segments": ch.get("segments", []),
                }
                review_meta = {
                    **story_meta,
                    "chapter_position": f"{i+1}/{len(valid_chapters)}",
                    # v2.4: 透传 concept 给 coherence reviewer 做常识判断
                    "concept": seg_data.get("concept", "") or story_meta.get("concept", ""),
                }
                try:
                    shots, revision_log, auto_fixes = run_all_reviewers(
                        shots, chapter_for_review, review_meta,
                        enabled_reviewers=enabled_reviewers,
                    )
                    print_revision_report(ch.get("id", "?"), shots, revision_log,
                                          auto_fixes=auto_fixes)
                except Exception as e:
                    print(f"  [审稿] 异常（保留未审稿版本）: {e}")
                    revision_log = []

            # 写审稿后的缓存
            try:
                reviewed_cache_file.write_text(
                    json.dumps({
                        "shots": shots,
                        "revision_log": revision_log,
                        "review_version": SB_REVIEW_VERSION,
                        "reviewers": rv_list,
                        "chapter_summary": cached_chapter_summary,  # v2.3.4
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"  [cache] 审稿缓存写入失败: {e}")

        ch_dynamic = sum(1 for s in shots if s["dynamic"])
        dynamic_used_total += ch_dynamic

        # 打印摘要
        for j, s in enumerate(shots):
            mark = "🎬" if s["dynamic"] else "📷"
            revised_mark = "✏️ " if s.get("_revision_notes") else "  "
            narr_preview = s["narration"][:25] + ("…" if len(s["narration"]) > 25 else "")
            print(f"      {revised_mark}{mark} sh{j+1:02d} [{s['shot_type']:16s}] "
                  f"[{s['transition_in']:13s}→{s['kb_direction']:10s}] "
                  f"{narr_preview}")

        out_chapters.append({
            "id":       ch["id"],
            "title":    ch["title"],
            "arc_role": ch["arc_role"],
            "tone":     ch.get("tone", "tension"),
            "shots":    shots,
            "chapter_summary": cached_chapter_summary,   # v2.3.4
        })
        chapter_summaries.append(_summarize_revisions(ch["id"], shots))

    # 输出
    sb_path = SCRIPTS_DIR / f"{seg_data['story_id']}_storyboard.yaml"
    out_data = {
        "story_id":      seg_data["story_id"],
        "title":         seg_data["title"],
        "segments_path": str(seg_path),
        "outline_path":  outline_path,
        "dynamic_used":  dynamic_used_total,
        "dynamic_total": max_dynamic_total,
        "schema_version": "2.0",
        "chapters":      out_chapters,
        # v2.3.4：全片场景注册表（铁律 18/19），供 to_pipeline 和后续模块参考
        "scene_registry": scene_registry,
    }
    with open(sb_path, "w", encoding="utf-8") as f:
        yaml.dump(out_data, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)

    total_shots = sum(len(c["shots"]) for c in out_chapters)
    print(f"\n  ✓ 分镜已生成: {sb_path}")
    print(f"  总镜头数: {total_shots}（原段数 × {total_shots/max(1, sum(len(c.get('segments', [])) for c in valid_chapters)):.1f}）")
    print(f"  动态镜头使用: {dynamic_used_total}/{max_dynamic_total}")

    # 全片审稿总结
    if enable_review and chapter_summaries:
        try:
            from revision_printer import print_overall_summary
        except ImportError:
            try:
                from .revision_printer import print_overall_summary
            except ImportError:
                print_overall_summary = None
        if print_overall_summary:
            print_overall_summary(chapter_summaries)

    print(f"  下一步:")
    print(f"    python -m tools.long_writer.to_pipeline {sb_path}")


def main():
    parser = argparse.ArgumentParser(description="长故事智能分镜规划 v2.2（含审稿）")
    parser.add_argument("segments", help="scripts/<id>_segments.yaml 路径")
    parser.add_argument("--max-dynamic", type=int, default=DEFAULT_MAX_DYNAMIC_TOTAL,
                        help=f"全片最多动态镜头数（默认 {DEFAULT_MAX_DYNAMIC_TOTAL}）")
    parser.add_argument("--no-review", action="store_true",
                        help="禁用审稿（默认开启 4 个审稿员）")
    parser.add_argument("--reviewers", default=None,
                        help="逗号分隔的审稿员列表，例如 'narrative,flux'。"
                             "可选: narrative/visual/flux/dialogue。默认全开")
    parser.add_argument("--review-only", default=None,
                        help="只重审某章（如 'ch03'），跳过其他章节")
    args = parser.parse_args()

    enabled_reviewers = None
    if args.reviewers:
        enabled_reviewers = [r.strip() for r in args.reviewers.split(",") if r.strip()]

    generate_storyboard(
        args.segments,
        max_dynamic_total=args.max_dynamic,
        enable_review=not args.no_review,
        enabled_reviewers=enabled_reviewers,
        review_only=args.review_only,
    )


if __name__ == "__main__":
    main()
