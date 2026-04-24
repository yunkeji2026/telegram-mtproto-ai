"""channel_status_format 纯函数单测"""

from src.utils.channel_status_format import (
    customer_should_omit_channel,
    format_live_channel_status_text,
    is_channel_disabled,
)


def _channels_sample():
    return {
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


class TestFormatLiveChannelStatusText:
    def test_omit_fee_by_default(self):
        out = format_live_channel_status_text(_channels_sample(), include_fee=False)
        assert "费率=" not in out
        assert "成功率=99.2%" in out
        assert "已禁用通道" in out

    def test_include_fee(self):
        out = format_live_channel_status_text(_channels_sample(), include_fee=True)
        assert "费率=0.5%" in out

    def test_empty(self):
        assert format_live_channel_status_text({}, include_fee=True) == ""


class TestCustomerOmitPix:
    def test_format_omits_pix_entirely(self):
        channels = {
            "other": {
                "display_name": "PIX通道",
                "names": ["PIX", "pix"],
                "status": "禁用",
                "success_rate": 90.0,
            },
            "ep": {
                "display_name": "EP通道",
                "status": "正常",
                "success_rate": 50.0,
                "minimum_amount": "100",
                "maximum_amount": "20000",
            },
        }
        out = format_live_channel_status_text(channels, include_fee=False)
        assert "PIX" not in out
        assert "pix" not in out.lower()
        assert "EP" in out
        assert "已禁用通道" not in out

    def test_customer_should_omit_channel(self):
        assert customer_should_omit_channel("other", {"display_name": "PIX通道"})
        assert not customer_should_omit_channel("ep", {"display_name": "EP通道"})


class TestIsChannelDisabled:
    def test_disabled(self):
        assert is_channel_disabled({"status": "disabled"})
        assert is_channel_disabled({"status": " 禁用 "})
        assert is_channel_disabled({"status": "停用"})

    def test_active(self):
        assert not is_channel_disabled({"status": "正常"})
