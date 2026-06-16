"""Phase B：可选统计语种检测回退的接入与门控测试。

用「假注入检测器」验证门控逻辑，不依赖任何真实统计库 → CI 零新增依赖、可复现。
关键：每个用例后清除全局钩子，避免污染同进程/并行 worker 内的其他用例。
"""

import pytest

from src.ai import translation_service as ts
from src.ai.translation_service import detect_language, set_statistical_detector


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    set_statistical_detector(None)  # 用例结束后还原纯确定性，防止状态泄漏


def test_default_no_hook_behavior_unchanged():
    # 未注入钩子时，含糊拉丁仍走确定性回退（en）
    assert detect_language("this is some generic message here") == "en"


def test_hook_refines_weak_latin_when_long_enough():
    # 假检测器：把任何输入判成印尼语；仅在弱结果 + 文本够长时被采信
    set_statistical_detector(lambda t: "id", min_chars=12)
    assert detect_language("kabar terbaru produk terlaris") == "id"


def test_hook_not_consulted_for_short_text():
    set_statistical_detector(lambda t: "id", min_chars=12)
    # 短文本（< min_chars）不咨询统计层 → 保持确定性结果
    assert detect_language("ok thx") == "en"


def test_hook_not_consulted_for_script_languages():
    # 脚本类/越南语已被确定性核心捕获，绝不被统计层覆盖
    set_statistical_detector(lambda t: "id", min_chars=4)
    assert detect_language("สวัสดีครับ อยากสอบถามราคา") == "th"
    assert detect_language("Xin chào, tôi muốn mua sản phẩm này") == "vi"
    assert detect_language("你好，请问有货吗") == "zh"


def test_hook_invalid_result_keeps_deterministic():
    # 统计层返回未知/库外语种 → 保持确定性弱结果
    set_statistical_detector(lambda t: "xx-unknown", min_chars=4)
    assert detect_language("this is some generic message here") == "en"


def test_hook_exception_is_safe():
    def _boom(_t):
        raise RuntimeError("backend down")

    set_statistical_detector(_boom, min_chars=4)
    # 后端异常不应冒泡，回落确定性结果
    assert detect_language("this is some generic message here") == "en"


def test_normalizes_hook_output():
    # 钩子返回 zh-cn 应被 normalize 成 zh
    set_statistical_detector(lambda t: "zh-cn", min_chars=4)
    assert detect_language("aaaa bbbb cccc dddd") == "zh"


def test_clear_hook_restores_determinism():
    set_statistical_detector(lambda t: "id", min_chars=4)
    assert detect_language("generic latin message") == "id"
    set_statistical_detector(None)
    assert detect_language("generic latin message") == "en"
    assert ts._STATISTICAL_HOOK is None
