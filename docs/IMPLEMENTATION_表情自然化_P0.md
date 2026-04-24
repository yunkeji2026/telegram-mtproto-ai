# 实施记录：表情自然化（P0）

## 已完成

### 1. 根因修复

- **`context_manager._suggest_emoticons`** 中性建议由 `👉/📝/🔍` 改为与 `config/emoticons.yaml` 一致的 `💭/✨/🤗`，消除上下文分析注入的「固定水印」源。
- **`EmotionEnhancer` 内置缺省池** 中性项去掉 `👉/📝`，避免未加载 YAML 时回退到旧水印。

### 2. 自然化策略（`emoticons.naturalization`）

配置见 `config/emoticons.yaml`：

| 字段 | 含义 |
|------|------|
| `skip_emoticon_pass_probability` | 本轮不追加新表情（语气微调仍可能生效） |
| `ignore_context_suggestions_probability` | 忽略上下文建议表情，增加随机感 |
| `max_consecutive_decorated` | 连续多条带装饰后强制一条「无新表情」 |
| `forbidden_emoticons` | 永不自动追加（默认 👉 📝） |

未配置 `naturalization` 时，使用与 YAML 一致的默认合并到内部 `_naturalization`。

### 3. 会话键

- `telegram_client` 调用 `enhance_reply(..., chat_id=str(chat_id))`，用于按会话维护 `dec_streak`。
- 状态字典超过 5000 键时清空，防内存膨胀。

### 4. 测试

- `tests/test_emotion_naturalization.py`：禁止表情过滤、关闭自然化路径、上下文建议无 👉📝。

## 相对 PRD 的额外优化

- **部分配置合并**：用户只写 `naturalization: { enabled: false }` 时，其余键仍用默认值，避免缺键。
- **双保险**：即使中性池误含 👉📝，也会在 `_filter_forbidden_emoticons` 中剔除。

## 下一阶段（P1/P2 建议）

1. **关系阶段**：在 `ContextStore` 或新表持久化 `relationship_stage`，并在 `AIClient` 注入短提示块（见 PRD）。
2. **记忆与阶段联动**：在 `_inject_episodic_into_context` 前按阶段筛选/重排 bullet；抽取时写入 `category`。
3. **后台**：`emoticons` 页或设置页暴露 `naturalization` 滑条（概率、连续条数），免改 YAML。
4. **质检**：日志抽样统计句首 emoji 分布与 👉 出现率（可为 0）。

---

*与 `docs/PRD_陪伴关系与表情策略_v1.md` 配套。*
