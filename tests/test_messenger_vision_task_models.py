"""``vision_task_models`` 中央表的单测。"""
from __future__ import annotations

import pytest

from src.integrations.messenger_rpa import vision_task_models as vtm


# ── 表本身的完整性 ────────────────────────────────────────

def test_all_registered_tasks_have_notes():
    """每个登记任务都必须有 notes——避免下次有人删掉实测沉淀。"""
    for name, task in vtm.VISION_TASKS.items():
        assert task.notes, f"task {name!r} 缺 notes"
        assert len(task.notes) >= 20, (
            f"task {name!r} 的 notes 太短，可能只是占位符："
            f"{task.notes!r}"
        )


def test_input_verify_uses_plus_not_flash():
    """P3 真机踩坑沉淀：input_verify 必须 plus，flash 100% false negative。"""
    task = vtm.VISION_TASKS["input_verify"]
    assert task.model == "glm-4v-plus"
    # notes 必须明确指出 flash 不行的原因
    assert "flash" in task.notes.lower()


def test_title_verify_uses_flash():
    """P0 实测：title_verify flash 准确且更快。"""
    task = vtm.VISION_TASKS["title_verify"]
    assert task.model == "glm-4v-flash"


def test_known_tasks_present():
    """关键任务必须登记——下次重构容易漏。"""
    for k in ("title_verify", "input_verify", "inbox_combined"):
        assert k in vtm.VISION_TASKS, f"任务 {k!r} 未登记"


# ── cfg_for_task 行为 ─────────────────────────────────────

def test_cfg_for_task_overrides_model_for_zhipu():
    base = {"provider": "zhipu", "api_key": "k", "model": "WRONG"}
    cfg = vtm.cfg_for_task("title_verify", base_cfg=base)
    assert cfg["model"] == "glm-4v-flash"
    assert cfg["api_key"] == "k"  # 保留 base 的其他字段


def test_cfg_for_task_passes_timeout_and_max_tokens():
    cfg = vtm.cfg_for_task(
        "input_verify",
        base_cfg={"provider": "zhipu", "api_key": "k"},
    )
    # 任务表的 timeout/max_tokens 应该被 propagate
    assert cfg["timeout"] == 30.0
    assert "max_tokens" in cfg


def test_cfg_for_task_does_not_override_for_ollama():
    """非 zhipu provider（如本地 ollama）→ model 不强制覆盖。

    避免"任务表说 glm-4v-plus 但你跑 ollama"的悖论。
    """
    base = {"provider": "ollama", "base_url": "http://x", "model": "llava"}
    cfg = vtm.cfg_for_task("input_verify", base_cfg=base)
    assert cfg["model"] == "llava"   # 没被改


def test_cfg_for_task_unknown_task_falls_back_to_base():
    base = {"provider": "zhipu", "api_key": "k", "model": "glm-4v-plus"}
    cfg = vtm.cfg_for_task("never_registered_task", base_cfg=base)
    assert cfg == base   # 没改任何字段


def test_cfg_for_task_overrides_param_wins_over_table():
    """``overrides`` 参数应当压过任务表（给 tests/急救）。"""
    base = {"provider": "zhipu", "api_key": "k"}
    cfg = vtm.cfg_for_task(
        "title_verify", base_cfg=base,
        overrides={"model": "custom-model"},
    )
    assert cfg["model"] == "custom-model"   # 不是 glm-4v-flash


def test_cfg_for_task_with_empty_base():
    cfg = vtm.cfg_for_task("title_verify", base_cfg=None)
    # base 为 None：默认 zhipu provider → 应该被设为 flash
    assert cfg["model"] == "glm-4v-flash"


def test_list_tasks_returns_copy_not_internal():
    """list_tasks 返回副本——外部修改不污染内部表。"""
    tasks1 = vtm.list_tasks()
    assert "title_verify" in tasks1
    tasks1["bogus"] = "x"
    tasks2 = vtm.list_tasks()
    assert "bogus" not in tasks2
