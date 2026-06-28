"""防回归守卫：保护 conftest `_reset_process_singletons` 依赖的「重置契约」。

conftest 的 autouse 重置对累积型进程内单例（MetricsStore / EventBus）做隔离，
但其实现用 try/except 包裹——若有人重命名/移除被重置的属性（`MetricsStore._instance`
或 event_bus 模块的 `_bus`），重置会**静默退化为 no-op**，#74 那类「本地绿 / CI 红」
的串测 flaky 会悄悄回归而不报错。

本文件直接断言重置契约：
1. 被重置的属性确实存在；
2. 重置后零参 getter 会换出全新实例（而非复用旧的）。

注：不写「A 测试污染 → B 测试断言已清」式跨测试守卫——`-n auto` 默认按单测跨
worker（多进程）分发，跨测试污染不可靠，那类守卫会偶发假绿/假红。
"""

from __future__ import annotations


def test_metrics_store_reset_contract():
    from src.monitoring import metrics_store as ms

    # 契约 1：conftest 重置点位的属性必须存在
    assert hasattr(ms.MetricsStore, "_instance"), \
        "MetricsStore._instance 被移除/改名 → conftest 重置会静默失效"

    store = ms.get_metrics_store()
    for _ in range(5):
        store.record_inbox_draft_event("generated")

    # 契约 2：重置后换出全新实例（累积计数不再带过来）
    ms.MetricsStore._instance = None
    fresh = ms.get_metrics_store()
    assert fresh is not store, "重置 _instance 后 getter 仍复用旧实例 → 隔离失效"


def test_event_bus_reset_contract():
    from src.integrations.shared import event_bus as eb

    # 契约 1：模块级 _bus 必须存在
    assert hasattr(eb, "_bus"), \
        "event_bus._bus 被移除/改名 → conftest 重置会静默失效"

    bus = eb.get_event_bus()
    assert hasattr(bus, "subscriber_count"), "EventBus.subscriber_count 契约缺失"

    # 契约 2：重置后换出全新总线（旧订阅者/history 不再带过来）
    eb._bus = None
    fresh = eb.get_event_bus()
    assert fresh is not bus, "重置 _bus 后 getter 仍复用旧总线 → 隔离失效"


def test_autouse_fixture_gives_clean_slate_at_entry():
    """轻量正向校验：因 autouse 重置在每个 test 前先跑，进入用例时单例应是干净的。

    （不依赖其它测试的污染；仅确认进入态符合「每测试新建」语义。）
    """
    from src.monitoring import metrics_store as ms
    from src.integrations.shared import event_bus as eb

    metrics = ms.get_metrics_store().get_inbox_draft_metrics()
    # "total" 是各事件累计 dict；fresh store 下应无 generated 计数
    assert int(metrics.get("total", {}).get("generated", 0)) == 0, \
        "进入用例时 MetricsStore 应无累积 draft 指标"
    assert eb.get_event_bus().subscriber_count == 0, "进入用例时 EventBus 应无残留订阅者"
