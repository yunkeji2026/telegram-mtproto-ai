"""CI 依赖守卫。

下列库在 ``src`` 中是**软依赖**（``try/except ImportError`` 缺失即降级，
保证生产环境少装也能跑），但**测试套件硬性需要**它们来验证完整行为。
若有人从 ``requirements-ci.txt`` 漏掉，CI 不会在一个清晰的地方报错，而是
以令人困惑的方式让 ``test_licensing`` / ``test_wa_lang_detect`` 等失败
（曾真实发生：缺 ``cryptography`` / ``langdetect`` 导致 PR CI 红）。

这个测试把「测试套件需要这些库」这一隐性约定**显式化**：缺失时直接在此
给出可读报错，指明该把它加回哪两个依赖清单。
"""

import importlib

import pytest

# (import 名, src 中的软依赖位置, 受影响的测试)
_REQUIRED = [
    (
        "cryptography",
        "src/licensing/license_manager.py (Ed25519 签发/验签)",
        "tests/test_licensing.py",
    ),
    (
        "langdetect",
        "src/integrations/whatsapp_rpa/lang_detect.py (Latin 语种识别)",
        "tests/test_wa_lang_detect.py",
    ),
]


@pytest.mark.parametrize("module, src_usage, affected", _REQUIRED)
def test_required_optional_dependency_importable(module, src_usage, affected):
    try:
        importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - 仅在依赖漏装时触发
        pytest.fail(
            f"测试套件需要 '{module}'（{src_usage}），但导入失败：{exc}\n"
            f"它在 src 中是软依赖、但 {affected} 硬性需要。\n"
            f"请把 '{module}' 同时加入 requirements-ci.txt 与 requirements.txt。"
        )
