"""
long_writer ── 15 分钟唱故事/评书生产线
=========================================
独立于 tools/story_writer.py（短篇图片书生产）。
两条线并存，互不干扰。

工作流：
  1. outline.py          概念 → 大纲（LLM 1 次调用）
  2. [人工审核 outline.yaml]
  3. chapter_writer.py   大纲 → 章节正文（LLM N 次调用，每章一次）
  4. [人工审核 .md 主稿]
  5. long_storyboard.py  章节 → 分镜（每段 shot_type + bgm_mood + dynamic）
  6. to_pipeline.py      转换为兼容 pipeline 的 stories/long_xxx.yaml
  7. python run.py twophase ... + produce ...

入口 CLI：tools/long_writer/cli.py
"""
