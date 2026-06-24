# v2.6 多角色多区域路径接入契约

> 解决 FLUX 多角色"双胞胎脸"问题(p28 翻车)。
> 设计哲学:跟 v2.3.5 路径**并行存在,互不影响**,flag 关时 100% 走老路。

---

## 一、问题与解法

### 问题
FLUX 原生分不清两个亚洲角色——p28 看到的就是两张完全一样的脸。这是 FLUX **底模均值倾向**,不是 prompt 不够好的问题。

### 解法
**Regional Prompter + 双 PuLID**:把画面用 mask 切成两个区域,每区独立走自己的 prompt + PuLID 参考,在 cross-attention 层强制分离。

工业界 2025-2026 处理"多角色一致性"的事实标准。

---

## 二、触发条件(全满足才走)

| 条件 | 含义 |
|---|---|
| `config.ENABLE_V260_REGIONAL = True` | flag 开关(默认 False) |
| `page_cfg.characters` 含 2 个及以上 lead | 单角色镜不走 |
| 每个 lead 角色都有 `portrait_ref` | 缺 portrait 降级 v2.3.5 |
| `shot_type` 不是 wide/extreme_wide | 远景细节不可见,投入产出不划算 |

任一不满足 → 自动降级到 v2.3.5 单 PuLID 路径(或普通 FLUX)。

---

## 三、数据流

```
storyboard.yaml (新字段 v2.6):
  - page: 28
    characters: ["裴拾月", "沈烬穹"]      ← focal_director v2.6 自动填
    focal_subject: "Pei Shisha (long hair, cold) tracing Shen Jinqiong (short topknot, terrified)'s lips"
    _region_prompts:                       ← 新字段
      - {character, en_name, region, prompt}
    _mask_hint: "face_to_face"             ← 可选,显式 mask 模板
    _portrait_refs: [...]                  ← 已有字段
        ↓
pipeline.py 路由判断:
  1. v2.6 优先:should_use_regional() → True
  2. 失败:fallback v2.3.5 单 PuLID
  3. 失败:fallback 普通 FLUX
        ↓
renderer.py::comfy_generate_flux_v260_multichar:
  - 上传 portrait × N + mask × 2
  - 顺序 patch UNet:ApplyPulidFlux × N
  - AttentionCouple(unet, base_cond, cond_L, mask_L, cond_R, mask_R)
  - KSampler → VAE → SaveImage
```

---

## 四、文件清单(v2.6 新增/改动)

| 文件 | 性质 | 说明 |
|---|---|---|
| `core/pipeline_v260_router.py` | **新建** | 路由判断 + 参数注入(仿 v235 router) |
| `core/mask_templates.py` | **新建** | 5 个 mask 模板 + 自动选 + PNG 生成 |
| `core/renderer.py::comfy_generate_flux_v260_multichar` | **新函数** | 双 PuLID + AttentionCouple workflow |
| `core/pipeline.py` | **改** | 加 v2.6 优先判断(15 行) |
| `tools/long_writer/focal_director.py` | **改** | 加多人镜识别 + 输出 _region_prompts |
| `tools/long_writer/long_storyboard.py` | **改** | 加铁律 16(分镜大师源头治理) |
| `tools/long_writer/reviewers.py` | **改** | 扩展 focal_director 字段白名单 |

---

## 五、ComfyUI 节点依赖

**必装**:
1. **ComfyUI-ppm** — `AttentionCouple` 节点(GitHub: `pamparamm/ComfyUI-ppm`)
2. **comfyui_pulid_flux_ll** — PuLID for FLUX(已装)

**已有(确认)**:
- `pulid_flux_v0.9.1.safetensors` (PuLID 模型)
- `EVA02_CLIP_L_336_psz14_s6B.pt` (PuLID 用 CLIP)
- `flux1-dev-Q4_K_S.gguf` (FLUX UNet)

**节点缺失行为**: renderer 自动检测,**降级到 v2.3.5 单 PuLID** + 打 warning,不会崩。

---

## 六、Mask 模板

| 模板 | left% | right% | blend | 适用场景 |
|---|---|---|---|---|
| `left_right` | 48 | 48 | 4% | 默认 |
| `face_to_face` | 45 | 45 | 10% | 对视/亲密双人 |
| `over_shoulder_left` | 65 | 30 | 5% | 左角色肩后镜 |
| `over_shoulder_right` | 30 | 65 | 5% | 镜像 |
| `foreground_background` | 60 | 35 | 5% | 前后景关系 |

`choose_mask_template()` 按 `_mask_hint` > `focal_subject` 关键词 > 默认 三级优先选模板。

---

## 七、降级矩阵

| 失败情况 | 自动行为 |
|---|---|
| ENABLE_V260_REGIONAL=False | 跳过 v2.6,直接 v2.3.5 |
| 角色 < 2 | 跳过 v2.6 |
| portrait 缺失 | 跳过 v2.6,v2.3.5 用第一个角色 |
| shot_type=wide | 跳过 v2.6 |
| ComfyUI-ppm 节点缺失 | renderer 内降级,v2.3.5 |
| AttentionCouple 签名不匹配 | renderer 内降级,v2.3.5 |

**每一层降级都打 warning,不静默失败**。

---

## 八、智能体接入预留

跟 `FOCAL_DIRECTOR_CONTRACT.md` / `CREATOR_AGENT_CONTRACT.md` 一脉相承:

`focal_director` 的 LLM 调用走 `reviewers._call_llm`(将来抽象成 LLMEngine)。要接入 ziv_agent_v5,**不需要改 v2.6 任何文件**——只需要替换底层 engine。

`prepare_v260_params` / mask 选择 / portrait 解析等都是**纯逻辑**,智能体不参与。

---

## 九、配置开关

```python
# infra/config.py 新增
ENABLE_V260_REGIONAL = True   # v2.6 多角色路径(默认 False,实战验证后开)
```

测试时:
```bash
# 启用
export ENABLE_V260_REGIONAL=1
python run.py twophase stories/<id>.yaml

# 禁用(回退老行为)
export ENABLE_V260_REGIONAL=0
```

---

## 十、向后兼容承诺

| 不变的事 | 含义 |
|---|---|
| 老 yaml(没 `_region_prompts`)| 不影响,走 v2.3.5 |
| 单角色镜头 | 不走 v2.6,继续 v2.3.5 |
| 节点没装 | renderer 检测降级 |
| flag 关 | 100% 跟 v2.5.2 行为一致 |
| 下游(producer/coordinator) | 完全无感,不知道 v2.6 存在 |

---

## 十一、问题反馈

接入实战后可能碰到:
- `AttentionCouple` 签名跟我假设不同 → 改 `comfy_generate_flux_v260_multichar` 里的 try/except 那块
- 两人 mask 中间融合带过窄/过宽 → 调 `mask_templates.MASK_TEMPLATES` 的 blend_band
- 单 PuLID + Regional 实测效果就够好 → 可以关 v2.6 用 v2.3.5
- PuLID 多次 patch UNet 不工作 → 改成接法 2(AttentionCouple 接管 conditioning)

每条都不需要改架构,只改对应小段代码。
