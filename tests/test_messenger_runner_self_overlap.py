from __future__ import annotations

from src.integrations.messenger_rpa.runner import _self_reply_overlap_ratio


def test_self_reply_overlap_catches_cjk_without_spaces() -> None:
    last_reply = "干杯～🍻 你那边存货还挺多的嘛，我这边只剩半罐了，得省着点喝哈哈。今晚打算聊到几点呀？"
    peer_text = "干杯～ 你那边存货还挺多的嘛，我这边只剩半罐了，得省着点喝哈哈。"
    assert _self_reply_overlap_ratio(last_reply, peer_text) >= 0.7


def test_self_reply_overlap_ignores_unrelated_japanese() -> None:
    last_reply = "今日は仕事が少し長かったけど、今は落ち着いたよ。あなたはどうだった？"
    peer_text = "どこにいるの？今から少し話せる？"
    assert _self_reply_overlap_ratio(last_reply, peer_text) < 0.7

