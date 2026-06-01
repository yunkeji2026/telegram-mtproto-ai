"""KB AI 辅助函数（Phase E1 批 5F：解耦步）。

把原 admin.py 内的两个大闭包 `_ai_translate_entry` / `_auto_fill_entry` 的实现
抽成独立纯函数（显式接 config_manager / kb_store），admin.py 保留同名薄包装委托
到这里。好处：

- 减少 admin.py 体量，且**不动任何端点 / 调用点 / 路由**（薄包装签名不变）。
- 解耦 AI 翻译/自动填充逻辑，为将来整组抽出「KB AI 自动化」端点 + 后台循环铺路
  （那些端点/循环可直接 import 本模块的纯函数，不再依赖 admin 闭包）。

逐行搬迁自 admin.py，仅把 `config_manager` / `_kb_store` 由闭包改为显式参数。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


async def ai_translate_entry(config_manager: Any, entry: Dict[str, Any], langs: List[str]) -> Dict[str, Any]:
    """
    一次 API 调用同时翻译到所有目标语言（批量 4 语言 1 次调用优化）。
    返回 {"en": {...fields...}, "ur": {...}, "pt": {...}, "ar": {...}}
    """
    import httpx as _httpx, re as _re
    ai_cfg = config_manager.config.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
    model = ai_cfg.get("model", "deepseek-chat")
    if not api_key:
        return {}

    fields_to_translate = {
        k: v for k, v in {
            "title":        entry.get("title", ""),
            "scenario":     entry.get("scenario", ""),
            "steps":        entry.get("steps", ""),
            "principles":   entry.get("principles", ""),
            "example_reply": entry.get("example_reply_zh", ""),
            "forbidden":    entry.get("forbidden", ""),
        }.items() if v
    }
    if not fields_to_translate:
        return {}

    _LANG_NAMES = {
        "en": "English",
        "ur": "Urdu (اردو)",
        "pt": "Portuguese (Brazilian)",
        "ar": "Arabic (عربي)",
    }
    target_desc = "; ".join(f'key="{l}" → {_LANG_NAMES.get(l,l)}' for l in langs)
    prompt = (
        "你是客服知识库专业翻译。请将下列中文字段同时翻译成多种语言。\n"
        "要求：保持客服专业语气；金融/支付术语使用目标语言的行业标准用词；"
        "EP/JC/Pay in 等系统专有名词保持原样不翻译。\n"
        "严格只返回纯 JSON，不得有任何额外说明。\n"
        f"目标语言：{target_desc}\n"
        f"JSON 结构：{{{', '.join(repr(l)+': {{...字段...}}' for l in langs)}}}\n"
        "源字段（中文）：\n"
        + json.dumps(fields_to_translate, ensure_ascii=False, indent=2)
    )
    try:
        async with _httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2500,
                    "temperature": 0.2,
                },
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            match = _re.search(r"\{[\s\S]*\}", content)
            if match:
                parsed = json.loads(match.group())
                return {k: v for k, v in parsed.items() if k in langs}
    except Exception as _e:
        logger.warning("KB 自动翻译失败: %s", _e)
    return {}


async def auto_fill_entry(config_manager: Any, kb_store: Any, entry_id: str,
                          title: str, category: str, source_query: str = "") -> None:
    """
    后台自动填充新建 KB 条目的内容（fire-and-forget）。
    用 AI 生成 scenario/steps/principles/example_reply_zh，然后 UPDATE 到已有条目。
    """
    import httpx as _httpx, re as _re
    ai_cfg = config_manager.config.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = (ai_cfg.get("base_url", "")).rstrip("/")
    model = ai_cfg.get("model", "gemini-2.5-flash")
    if not api_key or not base_url:
        return

    hint = f"用户原始问题: 「{source_query}」" if source_query else ""
    sys_prompt = (
        "你是一位资深客服话术专家，专注于支付/金融领域。"
        "请根据标题和用户问题，生成完整的客服知识条目。"
        "严格只返回纯 JSON，不要代码块标记或额外说明。"
    )
    user_prompt = (
        f"标题：{title}\n分类：{category}\n{hint}\n\n"
        "返回 JSON：\n"
        '{"triggers":["关键词1","关键词2","关键词3"],'
        '"scenario":"什么场景下用户会问",'
        '"steps":"1. 步骤1\\n2. 步骤2\\n3. 步骤3",'
        '"principles":"处理原则",'
        '"example_reply_zh":"客服标准回复(100字内,友好专业)",'
        '"forbidden":"不能做的事"}'
    )
    try:
        async with _httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [
                          {"role": "system", "content": sys_prompt},
                          {"role": "user", "content": user_prompt},
                      ],
                      "max_tokens": 800, "temperature": 0.6,
                      "response_format": {"type": "json_object"}},
            )
        raw = resp.json()["choices"][0]["message"]["content"]
        generated = json.loads(raw)
    except json.JSONDecodeError:
        m = _re.search(r'\{[\s\S]+\}', raw)
        if not m:
            return
        try:
            generated = json.loads(m.group())
        except Exception:
            return
    except Exception:
        return

    update_fields = {}
    for field in ("scenario", "steps", "principles", "example_reply_zh", "forbidden"):
        val = (generated.get(field) or "").strip()
        if val:
            update_fields[field] = val
    new_triggers = generated.get("triggers", [])
    if isinstance(new_triggers, list) and new_triggers:
        update_fields["triggers"] = new_triggers

    if update_fields:
        kb_store.update_entry(entry_id, update_fields)
        logger.info("L1 自动填充完成: entry=%s fields=%s",
                    entry_id, list(update_fields.keys()))
