"""W2-D1.5：陪护产品安全护栏（guardrail）。

设计目标：
- 输入侧：用户消息含危机/未成年/AI 身份追问 → 走特殊话术，不走普通 LLM
- 输出侧：AI 出戏（自报"我是 AI"）→ 重生成；露骨内容 → 拦
- 触发危机时推 admin（telegram）；不进黑名单（保留沟通窗口）

只暴露 GuardrailEngine + Action + GuardCategory；细节在 guardrail.py 内部。
"""
from src.integrations.safety.guardrail import (
    Action,
    GuardCategory,
    GuardrailEngine,
)

__all__ = ["Action", "GuardCategory", "GuardrailEngine"]
