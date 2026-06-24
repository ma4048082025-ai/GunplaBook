"""
config.example.py
=================
Copy this to `infra/config.py` and fill in your own values.
The real config.py is git-ignored and must never be committed.

    cp config.example.py infra/config.py
"""

# ── LLM (OpenAI-compatible endpoint) ─────────────────────────
LLM_API_KEY  = "sk-your-key-here"
LLM_BASE_URL = "https://your-llm-endpoint/v1"
LLM_MODEL    = "your-model-name"

# ── Remote GPU worker (image / voice services) ───────────────
# Reach your Windows ComfyUI / GPT-SoVITS node here.
COMFYUI_URL  = "http://127.0.0.1:8188"
SOVITS_URL   = "http://127.0.0.1:9880"

# ── Feature flags (default to stable/legacy behavior) ────────
ENABLE_V234_REDUX            = False
ENABLE_V235_PULID            = False
ENABLE_V260_REGIONAL         = False   # multi-character regional generation

ENABLE_NARRATION_INTEGRITY   = True    # deterministic info-conservation pass
ENABLE_INTEGRITY_LLM_REPAIR  = False   # optional targeted LLM repair
ENABLE_NARRATION_FLOW_REVIEWER = True
