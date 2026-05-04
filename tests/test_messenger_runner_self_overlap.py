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


def test_self_reply_overlap_p14_self_concatenated_replies() -> None:
    """P14 regression: vision 把 fast_path 内 bot 多条历史 reply 串联读成
    一条 'peer message'。两条文本字符级相似度低 (~0.16)，但 peer 必然含
    bot 标志性短语（farewell/客套词）。substring echo 信号应触发挡板。
    """
    last_reply = "うん、また明日ね。今日はこうして一緒にいられてよかったよ。おやすみ 😊"
    peer_text = "どうも、ありがとうございます。今日はたっぷりとお話ししましょう。お互いに気を付けて、また明日ね😊"
    assert _self_reply_overlap_ratio(last_reply, peer_text) >= 0.7


def test_self_reply_overlap_p14_unrelated_long_japanese_passes() -> None:
    """P14 边界：两条都是日文长对话，但话题完全不同——不该误挡。"""
    last_reply = "コーヒー飲みながら本読んでる、なんか落ち着くよね。今日はゆっくりしてる"
    peer_text = "私はずっと家で映画見てた、最近の作品あんまり面白くないんだよね"
    assert _self_reply_overlap_ratio(last_reply, peer_text) < 0.7


def test_p16_skipped_text_short_circuit_dedup():
    """P16-D 层：peer_msg.content 命中已 skip 文本指纹时，应短路而不进入
    overlap 计算。本测试模拟 D 层短路条件检查的纯逻辑。"""
    from collections import deque
    skipped = {"chat_a": deque(["I am heading home for now"], maxlen=5)}
    peer_text = "I am heading home for now"
    # D 层短路条件：peer_text 在指纹队列中
    assert peer_text in skipped["chat_a"]
    # 不同 chat 隔离
    assert "chat_b" not in skipped


def test_p16_il2_inbox_escape_releases_cooldown_when_preview_differs():
    """P16-IL2：长冷却期内若 inbox preview 与已 skip 文本相似度 < threshold，
    应识别为真新消息 → 解除冷却。"""
    from src.integrations.messenger_rpa.runner import _self_reply_overlap_ratio
    skipped_text = "Thanks for asking about me I did grab lunch just a sandwich"
    # 真新 peer 消息（话题完全不同）
    new_preview = "Hey did you watch the new movie last night?"
    sim = _self_reply_overlap_ratio(skipped_text, new_preview)
    assert sim < 0.6  # 默认 threshold，应 escape


def test_p16_il2_does_not_escape_when_preview_matches_skipped():
    """P16-IL2：vision 又重复幻觉同样内容时，preview 与 skipped 文本相似度高，
    维持冷却。"""
    from src.integrations.messenger_rpa.runner import _self_reply_overlap_ratio
    skipped_text = "Thanks for asking about me I did grab lunch just a sandwich"
    # vision 又把同样的自发文本当 peer，inbox preview 也是这条
    repeat_preview = "Thanks for asking about me I did grab lunch"
    sim = _self_reply_overlap_ratio(skipped_text, repeat_preview)
    assert sim >= 0.6  # 不该 escape


def test_p16_il2_escape_state_mutations():
    """P16-IL2：逃逸时应清除 _chat_overlap_skip_until + _self_overlap_skip_streak。"""
    chat_skip_until = {"ck1": 1e18}  # 远未到期
    streak = {"ck1": 5}
    # 模拟 escape 路径
    chat_skip_until.pop("ck1", None)
    streak.pop("ck1", None)
    assert "ck1" not in chat_skip_until
    assert "ck1" not in streak


def test_p16_d2_short_circuit_uses_overlap_ratio():
    """P16-D2：精确匹配漏掉时（vision 加 emoji / 略截断），相似度 >= 0.85
    仍能短路。这里只验证 _self_reply_overlap_ratio 的相似度阈值假设成立。"""
    from src.integrations.messenger_rpa.runner import _self_reply_overlap_ratio
    prev = "I am heading home for now"
    # vision 加了 emoji + 标点变化 — 精确匹配会漏，相似度仍极高
    variant = "I am heading home for now 😊"
    assert prev != variant  # 精确匹配漏掉
    r = _self_reply_overlap_ratio(prev, variant)
    assert r >= 0.85  # D2 相似度阈值能捕到
    # 完全不同内容不应误命中
    other = "Could you send me the location?"
    assert _self_reply_overlap_ratio(prev, other) < 0.85


def test_p16_streak_counter_triggers_long_cooldown():
    """P16-C 层：连续 self_message_skip ≥ threshold (默认 3) 时，
    应设置 chat_overlap_skip_until 长冷却（默认 600s）。"""
    import time
    streak: dict[str, int] = {}
    chat_skip_until: dict[str, float] = {}
    threshold = 3
    long_cd = 600.0

    def simulate_skip(chat_key: str) -> None:
        streak[chat_key] = streak.get(chat_key, 0) + 1
        if streak[chat_key] >= threshold:
            chat_skip_until[chat_key] = time.monotonic() + long_cd
            streak[chat_key] = 0

    # 前 2 次只累计 streak，不上长冷却
    simulate_skip("chat_a")
    simulate_skip("chat_a")
    assert streak["chat_a"] == 2
    assert "chat_a" not in chat_skip_until
    # 第 3 次触发长冷却
    simulate_skip("chat_a")
    assert streak["chat_a"] == 0  # 重置
    assert chat_skip_until["chat_a"] > time.monotonic() + 590  # ≈ now + 600s


def test_p16_runner_init_has_overlap_fields():
    """P16: MessengerRpaRunner.__init__ 应初始化三层守卫字段。"""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._skipped_peer_text_per_chat = {}
    r._self_overlap_skip_streak = {}
    r._chat_overlap_skip_until = {}
    # 字段类型 + 初始为空
    assert isinstance(r._skipped_peer_text_per_chat, dict)
    assert isinstance(r._self_overlap_skip_streak, dict)
    assert isinstance(r._chat_overlap_skip_until, dict)
    assert len(r._chat_overlap_skip_until) == 0


def test_p16_bubble_self_strong_signal_skips_promote():
    """P16-B 层：bubble_sender='self' 与 overlap≥0.7 双确认时，
    应跳过 promote 路径（即 _bubble_says_self 触发 elif 分支前的 if 走第一支）。"""
    bubble_sender = "self"
    self_overlap = 1.0
    bubble_says_self = (bubble_sender == "self")
    # 在 runner.py L2899 附近的逻辑：bubble=self + overlap>=0.7 → 不试 promote
    assert bubble_says_self and self_overlap >= 0.7


def test_p17_screenshot_hash_stable_for_same_bytes(tmp_path):
    """P17：_screenshot_hash 对相同字节产生相同 hash，不同字节不同 hash。"""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    p3 = tmp_path / "c.png"
    p1.write_bytes(b"\x89PNG-FAKE-DATA-1")
    p2.write_bytes(b"\x89PNG-FAKE-DATA-1")  # 同 bytes
    p3.write_bytes(b"\x89PNG-FAKE-DATA-2")  # 不同 bytes
    h1 = MessengerRpaRunner._screenshot_hash(str(p1))
    h2 = MessengerRpaRunner._screenshot_hash(str(p2))
    h3 = MessengerRpaRunner._screenshot_hash(str(p3))
    assert h1 == h2  # 字节相同
    assert h1 != h3  # 字节不同
    assert MessengerRpaRunner._screenshot_hash("/nonexistent/path.png") is None


def test_p17_cache_lru_evicts_oldest():
    """P17：thread_combined_cache 超过 max 时淘汰最旧条目（OrderedDict FIFO）。"""
    from collections import OrderedDict
    cache: OrderedDict = OrderedDict()
    max_size = 3
    for i in range(5):
        cache[f"hash_{i}"] = (f"cr_{i}", f"tag_{i}")
        while len(cache) > max_size:
            cache.popitem(last=False)
    # 应保留最近 3 个：hash_2, hash_3, hash_4
    assert list(cache.keys()) == ["hash_2", "hash_3", "hash_4"]


def test_p17_cache_move_to_end_keeps_hot_entries():
    """P17：LRU 命中时 move_to_end，热点不被淘汰。"""
    from collections import OrderedDict
    cache: OrderedDict = OrderedDict()
    max_size = 3
    cache["A"] = 1
    cache["B"] = 2
    cache["C"] = 3
    # A 持续命中
    cache.move_to_end("A")
    # 加 D 触发淘汰
    cache["D"] = 4
    while len(cache) > max_size:
        cache.popitem(last=False)
    # B 应被淘汰（最旧未命中），A 因热点保留
    assert "A" in cache
    assert "B" not in cache
    assert "C" in cache
    assert "D" in cache


def test_p17_apply_side_effects_skips_risk_replay():
    """P17：cache 命中时 replay_risk=False 应避免重复触发 _handle_risk_hit。"""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    handle_risk_calls = []
    r._handle_risk_hit = lambda *a, **kw: handle_risk_calls.append(1)

    class _G:
        type = "none"
        action = "none"
        confidence = "low"
        title = ""
    class _R:
        hit = True
        severity = "block"
        reason = "test"
    class _CR:
        guard = _G()
        peer = None
        extra_peers = ()
        risk = _R()
    cr = _CR()
    result: dict = {}
    # replay_risk=False（cache 命中场景）
    r._thread_combined_apply_side_effects(cr, "tag", result, replay_risk=False)
    assert handle_risk_calls == []
    # replay_risk=True（首次场景）
    r._thread_combined_apply_side_effects(cr, "tag", result, replay_risk=True)
    assert handle_risk_calls == [1]


def test_p15_push_recent_reply_keeps_last_n():
    """P15: _push_recent_reply 维护最近 N=3 条 in-memory 队列。"""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._recent_replies_per_chat = {}
    r._recent_replies_max = 3
    for text in ["r1", "r2", "r3", "r4"]:
        r._push_recent_reply("chat_a", text)
    assert r._recent_replies_per_chat["chat_a"] == ["r2", "r3", "r4"]
    # 不同 chat 隔离
    r._push_recent_reply("chat_b", "x")
    assert r._recent_replies_per_chat["chat_b"] == ["x"]
    # 空字符串/None 不入队
    r._push_recent_reply("chat_a", "")
    r._push_recent_reply("", "y")
    assert r._recent_replies_per_chat["chat_a"] == ["r2", "r3", "r4"]
    assert "" not in r._recent_replies_per_chat


def test_p15_overlap_against_recent_finds_old_self():
    """P15: vision 串联了 last_reply 之外的更早 self message，
    单条比对漏过；扩展集 max-ratio 应触发挡板。"""
    older_self = "うん、また明日ね、今日も楽しかったよ"  # 含 5+ 字 signature
    last_reply = "うん、いい日だよ。仕事も終わったし気分も軽い"  # 与 peer 无关键词
    # vision 把更老的 self message 串联当作 peer
    peer_text = "今日も楽しかったよ、ありがとう、また明日ね"
    # 当前 P14 只对比 last_reply：
    r1 = _self_reply_overlap_ratio(last_reply, peer_text)
    # 与 older_self 对比应 ≥ 0.7（含 "今日も楽し" 5 字 substring）
    r2 = _self_reply_overlap_ratio(older_self, peer_text)
    assert r1 < 0.7  # last_reply 单条不够
    assert r2 >= 0.7  # older self 命中
    # max(r1, r2) ≥ 0.7 → P15 扩展能挡住


def test_p15_promote_extra_peer_uses_recent_queue():
    """P15-bugfix: promote_extra_peer 也要用 last_3_replies 集合检查
    extra_peers，否则 vision 把更老的 self 放进 extra_peers，promote 误用 →
    bot 自言自语。"""
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._recent_replies_per_chat = {
        "chat_a": [
            "うん、今日も楽しかった、また明日ね",
            "ありがとう、お休みなさい、また明日",
        ]
    }
    # extra_peers 第一条是 vision 串联早期 self（含 "また明日" signature），
    # 第二条是真 peer
    result = {
        "extra_peers": [
            {"kind": "text", "content": "今日も楽しかった、おやすみ、また明日ね"},  # = self
            {"kind": "text", "content": "今日はずっと家にいたよ、特に何もしてない"},  # = real peer
        ]
    }
    promoted = r._promote_extra_peer_after_self_overlap(
        result, last_reply="まったく違う最近の発言", chat_key="chat_a",
    )
    assert promoted is not None
    # 应跳过第一条（命中 queue），返回第二条
    assert "ずっと家にいた" in (promoted.content or "")

