"""``vision_metrics`` SQLite store 单测 + 集成。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.integrations.messenger_rpa import vision_metrics as vm


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path):
    """每个 test 用独立 db，避免共享 config/vision_metrics.db。"""
    db = tmp_path / "vm_test.db"
    vm.configure(db)
    yield
    vm.configure(None)   # 重置为默认


# ── 基础读写 ──────────────────────────────────────────────

def test_record_then_summary_basic():
    vm.record(
        task_name="title_verify", model="glm-4v-flash", api_provider="zhipu",
        duration_ms=5000, ok=True,
    )
    vm.record(
        task_name="title_verify", model="glm-4v-flash", api_provider="zhipu",
        duration_ms=7000, ok=True,
    )
    s = vm.summary(since_sec=60)
    assert len(s) == 1
    row = s[0]
    assert row.task_name == "title_verify"
    assert row.model == "glm-4v-flash"
    assert row.count == 2
    assert row.ok_count == 2
    assert row.fail_count == 0
    assert row.ok_rate == 1.0
    assert row.avg_ms == 6000


def test_summary_empty_when_no_data():
    assert vm.summary() == []


def test_summary_filtered_by_task_name():
    vm.record(task_name="title_verify", model="m1", api_provider="zhipu",
              duration_ms=100, ok=True)
    vm.record(task_name="input_verify", model="m2", api_provider="zhipu",
              duration_ms=200, ok=True)
    only_title = vm.summary(task_name="title_verify")
    assert len(only_title) == 1
    assert only_title[0].task_name == "title_verify"


def test_summary_buckets_by_model():
    """同 task 不同 model（比如老配置 vs 新配置切换）应分开统计。"""
    vm.record(task_name="title_verify", model="glm-4v-flash",
              api_provider="zhipu", duration_ms=100, ok=True)
    vm.record(task_name="title_verify", model="glm-4v-plus",
              api_provider="zhipu", duration_ms=200, ok=True)
    s = vm.summary()
    models = {r.model for r in s}
    assert models == {"glm-4v-flash", "glm-4v-plus"}


def test_summary_excludes_old_entries():
    old_ts = time.time() - 10000
    vm.record(task_name="title_verify", model="m", api_provider="zhipu",
              duration_ms=100, ok=True, ts=old_ts)
    vm.record(task_name="title_verify", model="m", api_provider="zhipu",
              duration_ms=200, ok=True)
    s = vm.summary(since_sec=3600)
    assert len(s) == 1
    assert s[0].count == 1   # 老条目过滤掉


def test_percentiles_with_known_distribution():
    """计算 p50/p95——确认百分位算法对。"""
    durs = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    for d in durs:
        vm.record(task_name="t", model="m", api_provider="p",
                  duration_ms=d, ok=True)
    [s] = vm.summary()
    assert s.count == 10
    # p50 应该在中间位置（500-600 间）
    assert 500 <= s.p50_ms <= 600
    assert s.p95_ms >= 900
    assert s.max_ms == 1000


def test_record_does_not_throw_on_invalid_input():
    """metrics 是 best-effort——异常不能影响调用方。"""
    # 空 task_name 应该是 noop
    vm.record(task_name="", model=None, api_provider=None,
              duration_ms=0, ok=False)
    # 不该崩
    s = vm.summary()
    assert s == []


def test_error_breakdown():
    vm.record(task_name="t", model="m", api_provider="p",
              duration_ms=100, ok=False, error_class="vision_init_fail")
    vm.record(task_name="t", model="m", api_provider="p",
              duration_ms=100, ok=False, error_class="vision_init_fail")
    vm.record(task_name="t", model="m", api_provider="p",
              duration_ms=100, ok=False, error_class="parse_fail")
    vm.record(task_name="t", model="m", api_provider="p",
              duration_ms=100, ok=True)
    eb = vm.error_breakdown()
    assert eb == {"vision_init_fail": 2, "parse_fail": 1}


def test_error_breakdown_filtered_by_task():
    vm.record(task_name="A", model="m", api_provider="p",
              duration_ms=100, ok=False, error_class="X")
    vm.record(task_name="B", model="m", api_provider="p",
              duration_ms=100, ok=False, error_class="Y")
    eb = vm.error_breakdown(task_name="A")
    assert eb == {"X": 1}


def test_summary_sort_order():
    """count 多的 task 排前面——dashboard 一目了然。"""
    for _ in range(5):
        vm.record(task_name="rare", model="m", api_provider="p",
                  duration_ms=100, ok=True)
    for _ in range(50):
        vm.record(task_name="common", model="m", api_provider="p",
                  duration_ms=100, ok=True)
    s = vm.summary()
    assert s[0].task_name == "common"
    assert s[1].task_name == "rare"


# ── 集成：thread_title_vision / input_text_vision 自动 emit ──

def test_thread_title_vision_emits_metric_on_success(monkeypatch):
    from src.integrations.messenger_rpa import thread_title_vision as ttv
    from io import BytesIO

    # 准备一个伪截图 + 一个伪 VisionClient（返回有效 title JSON）
    monkeypatch.setattr(
        ttv, "screencap_top_strip",
        lambda serial, **kw: _make_tmp_png(),
    )

    class _OkVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return True

        def describe_image_sync(self, path, prompt=None):
            return '{"title":"Alice"}'

    monkeypatch.setattr("src.vision_client.VisionClient", _OkVC)

    r = ttv.read_thread_title_via_vision(
        "abc", {"provider": "zhipu", "api_key": "k"}
    )
    assert r.title == "Alice"
    s = vm.summary(task_name="title_verify")
    assert len(s) == 1
    assert s[0].ok_count == 1


def test_thread_title_vision_emits_metric_on_screencap_fail(monkeypatch):
    """screencap 失败时 *不* emit——因为还没真调 vision，统计意义不同。"""
    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "screencap_top_strip",
        lambda serial, **kw: None,
    )
    r = ttv.read_thread_title_via_vision("abc", {"provider": "zhipu", "api_key": "k"})
    assert r.title is None
    assert r.debug == "screencap_failed"
    # 没 emit
    assert vm.summary() == []


def test_input_text_vision_emits_metric_on_success(monkeypatch):
    from src.integrations.messenger_rpa import input_text_vision as itv

    monkeypatch.setattr(
        itv, "screencap_bottom_strip",
        lambda serial, **kw: _make_tmp_png(),
    )

    class _OkVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return True

        def describe_image_sync(self, path, prompt=None):
            return "hello"

    monkeypatch.setattr("src.vision_client.VisionClient", _OkVC)

    r = itv.read_input_text_via_vision(
        "abc", {"provider": "zhipu", "api_key": "k"}
    )
    assert r.text == "hello"
    s = vm.summary(task_name="input_verify")
    assert len(s) == 1
    assert s[0].ok_rate == 1.0
    # 任务表强制 plus，metrics 应反映
    assert s[0].model == "glm-4v-plus"


def test_thread_title_vision_emits_metric_on_parse_fail(monkeypatch):
    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "screencap_top_strip",
        lambda serial, **kw: _make_tmp_png(),
    )

    class _BadVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return True

        def describe_image_sync(self, path, prompt=None):
            return "garbage with no JSON"

    monkeypatch.setattr("src.vision_client.VisionClient", _BadVC)

    r = ttv.read_thread_title_via_vision("abc", {"provider": "zhipu", "api_key": "k"})
    # parse 也可能容错命中（裸文本路径），看是否走 fail
    s = vm.summary()
    assert len(s) == 1
    # 不论命中还是 fail，都要 emit 一条
    assert s[0].count == 1


def _make_tmp_png() -> Path:
    """生成一张极小 PNG，写到临时位置。"""
    import os, tempfile
    from PIL import Image
    img = Image.new("RGB", (100, 50), color=(0, 0, 0))
    fd, name = tempfile.mkstemp(prefix="_vmtest_", suffix=".png")
    os.close(fd)
    img.save(name, format="PNG")
    return Path(name)
