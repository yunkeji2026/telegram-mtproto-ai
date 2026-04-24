"""_get_live_channel_status 与 ai.channel_status_include_fee 回归测试"""

from unittest.mock import MagicMock

from src.skills.skill_manager import SkillManager


def _bare_skill_manager(config: MagicMock) -> SkillManager:
    """构造未跑 __init__ 的 SkillManager，仅用于测试纯逻辑方法（LoggerMixin 首次访问 self.logger 会懒创建）。"""
    sm = object.__new__(SkillManager)
    sm.config = config
    return sm


def _sample_rates():
    return {
        "channels": {
            "ep": {
                "display_name": "EP",
                "status": "正常",
                "success_rate": 99.2,
                "fee_rate": "0.5%",
                "minimum_amount": "100",
                "maximum_amount": "20000",
            },
            "off": {
                "display_name": "X",
                "status": "disabled",
                "fee_rate": "1%",
            },
        }
    }


class TestGetLiveChannelStatus:
    def test_empty_channels_returns_empty(self):
        cfg = MagicMock()
        cfg.get_ai_config.return_value = {}
        cfg.get_exchange_rates_config.return_value = {}
        sm = _bare_skill_manager(cfg)
        assert sm._get_live_channel_status() == ""

    def test_default_omits_fee_line(self):
        cfg = MagicMock()
        cfg.get_ai_config.return_value = {"channel_status_include_fee": False}
        cfg.get_exchange_rates_config.return_value = _sample_rates()
        sm = _bare_skill_manager(cfg)
        out = sm._get_live_channel_status()
        assert "费率=" not in out
        assert "成功率=99.2%" in out
        assert "单笔限额=100-20000" in out
        assert "已禁用通道" in out and "X" in out

    def test_config_include_fee_true_adds_fee_line(self):
        cfg = MagicMock()
        cfg.get_ai_config.return_value = {"channel_status_include_fee": True}
        cfg.get_exchange_rates_config.return_value = _sample_rates()
        sm = _bare_skill_manager(cfg)
        out = sm._get_live_channel_status()
        assert "费率=0.5%" in out

    def test_explicit_include_fee_overrides_config(self):
        cfg = MagicMock()
        cfg.get_ai_config.return_value = {"channel_status_include_fee": False}
        cfg.get_exchange_rates_config.return_value = _sample_rates()
        sm = _bare_skill_manager(cfg)
        out = sm._get_live_channel_status(include_fee=True)
        assert "费率=0.5%" in out

    def test_explicit_include_fee_false_overrides_config_true(self):
        cfg = MagicMock()
        cfg.get_ai_config.return_value = {"channel_status_include_fee": True}
        cfg.get_exchange_rates_config.return_value = _sample_rates()
        sm = _bare_skill_manager(cfg)
        out = sm._get_live_channel_status(include_fee=False)
        assert "费率=" not in out

    def test_missing_get_ai_config_defaults_no_fee(self):
        cfg = MagicMock(spec=["get_exchange_rates_config"])
        cfg.get_exchange_rates_config.return_value = _sample_rates()
        sm = _bare_skill_manager(cfg)
        out = sm._get_live_channel_status()
        assert "费率=" not in out
