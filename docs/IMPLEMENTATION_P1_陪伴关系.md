# 实施记录：陪伴关系阶段（P1）

## 已完成

### 数据

- 持久化路径：`ContextStore` → `user_context` 行 JSON 中的 **`companion_relationship`**（已加入 `_PERSIST_KEYS`）。
- 结构：`companion_relationship[chat_id 字符串] = { stage, exchange_count, updated_at, suppress_advance_until? }`。
- 单次请求的 **`_relationship_prompt_block` / `relationship_stage`** 不持久化（已加入 `_NON_PERSIST`）。

### 逻辑模块

- `src/utils/companion_relationship.py`：阶段常量、`get_rel_state`、`downgrade_from_user_text`、`reconcile_stage_after_assistant_reply`、`build_relationship_prompt_block`。

### 集成点

- **`SkillManager._handle_message_guarded`**（情景记忆注入之后）：若 `effective_domain_name == conversion` 且 `companion.enabled`，则对用户文本做降级检测，生成 `_relationship_prompt_block`。
- **`SkillManager._update_after_reply`**：`exchange_count += 1`，再 `reconcile_stage_after_assistant_reply`。
- **`AIClient._build_context_prompt`**：陪聊域下在情景记忆块**之前**追加关系阶段提示。

### 配置

- 根配置 `companion:`（见 `config/config.yaml` 与 `config.example.yaml`）。

## 相对初版方案的改进

- **按 chat 分桶**：同一 Telegram 用户在不同群/私聊使用不同 `chat_id` 键，避免阶段混用。
- **降级冷却**：`suppress_advance_until`，避免用户说「别腻」后下一轮立刻被轮次阈值拉回高阶段。

## 下一阶段（P2）

1. **情景记忆联动**：按 `relationship_stage` 过滤 episodic bullet 或调整排序权重。
2. **后台**：情境记忆或新页展示 `companion_relationship` 只读/重置。
3. **指标**：阶段分布、降级次数审计。
