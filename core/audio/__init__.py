"""
core.audio ── 音频工程独立模块
================================================================

把音频相关的事情从 producer_v2.py 抽离出来,自成体系。

模块规划:
  contract.py          v2.6 字段契约的 Python 化(dataclass)
  voice_engine.py      从 core/ 搬过来,加 emotion 路由
  voice_emotion.py     ffmpeg emotion filter 后处理
  bgm_builder.py       从 producer_v2.py 抽出 _build_bgm_track
  ambient_builder.py   仿 BGM,按 scene_audio.ambient 铺底床
  sfx_builder.py       按 scene_audio.sfx 精确触发点状音效
  mixer.py             统一三层叠加: voice + bgm + ambient + sfx

设计原则:
  1. 所有模块对外只暴露纯函数 / 简单类,不依赖 producer 内部状态
  2. 输入: 字典/列表/路径字符串。输出: 文件路径或音频时长。
  3. 配置走 yaml(refs/sfx/registry.yaml, refs/audio/emotion_filters.yaml)
  4. 不依赖外部 API,全本地 ffmpeg + edge-tts + GPT-SoVITS

向后兼容:
  - 旧 yaml(无 scene_audio 字段)跑全流程必须结果一致
  - core/voice_engine.py 保留 re-export,旧 import 路径不挂

测试:
  每个 builder 都支持单独跑:
    python -m core.audio.ambient_builder <page.json> <out.mp3>
"""

# 第一版先空着,等模块陆续就位再 export
__all__ = []
