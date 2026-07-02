"""resolve_account_persona_id 单测（优化②：根治复数/单数命名不匹配 → 空 _real_pid）。

覆盖优先级：registry meta.persona_id → meta.persona_ids[0] → config[platform].persona_ids[0]。
"""

from src.ai.persona_voice import resolve_account_persona_id


class _FakeRegistry:
    """duck-typed 假注册表：get(platform, account_id) → {'meta': {...}}。"""

    def __init__(self, row):
        self._row = row

    def get(self, platform, account_id):
        return self._row


def test_meta_persona_id_singular_wins():
    reg = _FakeRegistry({"meta": {"persona_id": "alice", "persona_ids": ["bob"]}})
    cfg = {"telegram": {"persona_ids": ["carol"]}}
    assert resolve_account_persona_id(cfg, "telegram", "acc", registry=reg) == "alice"


def test_meta_persona_ids_plural_fallback():
    """sync 写的是复数 persona_ids（旧代码读单数 persona_id 落空）→ 本解析器兜住。"""
    reg = _FakeRegistry({"meta": {"persona_ids": ["lin_xiaoyu", "bob"]}})
    cfg = {"telegram": {"persona_ids": ["carol"]}}
    assert resolve_account_persona_id(
        cfg, "telegram", "acc", registry=reg) == "lin_xiaoyu"


def test_config_default_when_registry_empty():
    """standalone 主号 meta 全空 → 回落 config[platform].persona_ids[0]（即 09:07 修复口径）。"""
    reg = _FakeRegistry({"meta": {}})
    cfg = {"telegram": {"persona_ids": ["lin_xiaoyu"]}}
    assert resolve_account_persona_id(
        cfg, "telegram", "acc", registry=reg) == "lin_xiaoyu"


def test_empty_everywhere_returns_empty_string():
    reg = _FakeRegistry({"meta": {}})
    assert resolve_account_persona_id({}, "telegram", "acc", registry=reg) == ""


def test_registry_none_row_degrades_to_config():
    reg = _FakeRegistry(None)
    cfg = {"telegram": {"persona_ids": ["lin_xiaoyu"]}}
    assert resolve_account_persona_id(
        cfg, "telegram", "acc", registry=reg) == "lin_xiaoyu"


def test_registry_raises_degrades_to_config():
    class _Boom:
        def get(self, *a):
            raise RuntimeError("db down")

    cfg = {"telegram": {"persona_ids": ["lin_xiaoyu"]}}
    assert resolve_account_persona_id(
        cfg, "telegram", "acc", registry=_Boom()) == "lin_xiaoyu"


def test_platform_scoped_config_default():
    """按 platform 取对应默认，不串台。"""
    reg = _FakeRegistry({"meta": {}})
    cfg = {"telegram": {"persona_ids": ["tg_p"]},
           "whatsapp": {"persona_ids": ["wa_p"]}}
    assert resolve_account_persona_id(cfg, "whatsapp", "acc", registry=reg) == "wa_p"


def test_blank_values_skipped():
    """空串/空白 meta 值不算命中，继续回落。"""
    reg = _FakeRegistry({"meta": {"persona_id": "  ", "persona_ids": ["", "  "]}})
    cfg = {"telegram": {"persona_ids": ["lin_xiaoyu"]}}
    assert resolve_account_persona_id(
        cfg, "telegram", "acc", registry=reg) == "lin_xiaoyu"
