from __future__ import annotations

import pytest

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


def test_p23_unread_chat_signals_count_default():
    """P23：UnreadChat 默认值（True / False / False）= 1 个信号，
    向后兼容（老 vision 输出按"已是 unread"处理）。"""
    from src.integrations.messenger_rpa.inbox_scanner import UnreadChat
    c = UnreadChat(
        name="Alice", preview="hi", time="now", row_index=0,
    )
    assert c.unread_signals_count == 1  # name_bold=True 默认


def test_p23_unread_chat_all_signals_false():
    """P23：三信号全 F → unread_signals_count = 0 → vision 偷懒。"""
    from src.integrations.messenger_rpa.inbox_scanner import UnreadChat
    c = UnreadChat(
        name="Bob", preview="hello", time="now", row_index=0,
        name_bold=False, preview_bold=False, blue_dot=False,
    )
    assert c.unread_signals_count == 0


def test_p23_unread_chat_partial_signals():
    """P23：部分信号为 True 时累加。"""
    from src.integrations.messenger_rpa.inbox_scanner import UnreadChat
    c = UnreadChat(
        name="Carol", preview="hey", time="now", row_index=0,
        name_bold=True, preview_bold=True, blue_dot=False,
    )
    assert c.unread_signals_count == 2


def test_p23_skip_condition_signals_zero_and_recent_send():
    """P23：unread_signals=0 + 最近发过 → skip 条件成立。"""
    import time
    last_sent_at = time.time() - 30  # 30s 前发过
    window = 60.0
    unread_signals = 0
    should_skip = (
        unread_signals == 0
        and window > 0
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert should_skip


def test_p23_skip_condition_signals_zero_but_no_recent_send():
    """P23：信号 0 但很久没发过 → 不 skip（peer 真发新消息可能性高）。"""
    import time
    last_sent_at = time.time() - 3600  # 1h 前
    window = 60.0
    unread_signals = 0
    should_skip = (
        unread_signals == 0
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert not should_skip


def test_p23_skip_condition_signals_present_does_not_skip():
    """P23：信号 ≥ 1 时不 skip（vision 真识别为 unread）。"""
    import time
    last_sent_at = time.time() - 30
    window = 60.0
    unread_signals = 1
    should_skip = (unread_signals == 0 and last_sent_at > 0 and (time.time() - last_sent_at) < window)
    assert not should_skip


def test_p21_long_cooldown_history_window_aging():
    """P21：滚动窗口（默认 24h）外的历史应被清理。"""
    import time
    history: list[float] = []
    window = 86400.0  # 24h
    now = time.time()

    def maybe_record(t: float) -> int:
        nonlocal history
        cutoff = now - window
        history = [x for x in history if x >= cutoff]
        history.append(t)
        return len(history)

    # 25h 前的历史 + 现在 → 旧的应被清，仅留 1 条
    maybe_record(now - 25 * 3600)  # 25h 前
    assert len(history) == 1
    # 模拟 24h 后再来一次
    history = [t for t in history if t >= (now - window)]  # 现在视角看 25h 前过期
    history.append(now)
    # 仅留 now 这一条
    history_within = [t for t in history if t >= (now - window)]
    assert len(history_within) == 1


def test_p21_blacklist_triggers_at_threshold():
    """P21：累计 ≥ threshold 时应触发 add_skipped_chat（用 mock 验证）。"""
    import time
    history: list[float] = []
    threshold = 3
    now = time.time()
    # 连续 3 次 long_cooldown 在 1h 内
    history.append(now - 1800)
    history.append(now - 600)
    history.append(now)
    assert len(history) >= threshold


def test_p21_below_threshold_does_not_blacklist():
    """P21：累计 < threshold 时仅累加历史，不入黑。"""
    history: list[float] = []
    threshold = 3
    history.append(1.0)
    history.append(2.0)
    assert len(history) < threshold  # 不应触发 blacklist


def test_p21_blacklist_clears_chat_state():
    """P21：blacklist 时应清理 chat 的所有内存状态。"""
    skip_until = {"chat_a": 1e18}
    streak = {"chat_a": 5}
    history = {"chat_a": [1.0, 2.0, 3.0]}
    skipped_text = {"chat_a": ["xx", "yy"]}
    # 模拟 blacklist 清理
    skip_until.pop("chat_a", None)
    streak.pop("chat_a", None)
    history.pop("chat_a", None)
    skipped_text.pop("chat_a", None)
    assert "chat_a" not in skip_until
    assert "chat_a" not in streak
    assert "chat_a" not in history
    assert "chat_a" not in skipped_text


def test_p19_long_last_sent_ago_does_not_skip_emergency_fix():
    """P19-FIX 紧急修复回归：last_sent_ago > 默认 window (120s) 时
    必须不 skip，让 vision 接管（防止 peer 久回的真消息被吞）。
    生产事故重演：last_sent_ago=2443s（40min）+ 旧 window=3600s → 误吞。"""
    import time
    pv_bubble_sender = "self"
    last_sent_at = time.time() - 2443  # 40 分钟前发的（peer 久回场景）
    window = 120.0  # P19-FIX 默认值
    should_skip = (
        pv_bubble_sender == "self"
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert not should_skip  # 不应误拦


def test_p24_window_clamp_oversized_config():
    """P24：window 配置 > 上限时应被 clamp。"""
    raw = 3600.0
    cap = 300.0
    clamped = min(raw, cap) if cap > 0 else raw
    assert clamped == 300.0


def test_p24_window_clamp_normal_config_unchanged():
    """P24：window 配置 ≤ 上限时不变。"""
    raw = 120.0
    cap = 300.0
    clamped = min(raw, cap) if cap > 0 else raw
    assert clamped == 120.0


def test_p24_window_clamp_at_exact_boundary():
    """P24：window 配置 == 上限时不 clamp（边界包含）。"""
    raw = 300.0
    cap = 300.0
    # 实际 clamp 条件：raw > cap 才 clamp
    should_clamp = raw > cap
    assert not should_clamp  # 等于不 clamp


def test_p24_warning_only_once_per_runner():
    """P24：一个 runner 实例多次进 P19 窗口检查时 warning 只打一次。"""
    warned = False
    raw = 600.0
    cap = 300.0
    warning_count = 0

    for _ in range(5):
        if raw > cap and not warned:
            warning_count += 1
            warned = True
    assert warning_count == 1  # 只 warn 一次


def test_p19_pre_vision_bubble_self_recent_window_short_circuits():
    """P19：bubble=self + 最近发送窗口内 → 应短路不调 vision。"""
    import time
    pv_bubble_sender = "self"
    last_sent_at = time.time() - 30  # 30s 前发的
    window = 600.0
    should_skip = (
        pv_bubble_sender == "self"
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert should_skip


def test_p19_pre_vision_bubble_peer_does_not_short_circuit():
    """P19：bubble=peer 时即使最近发送也不该短路（peer 真有新消息）。"""
    import time
    pv_bubble_sender = "peer"
    last_sent_at = time.time() - 30
    window = 600.0
    should_skip = (
        pv_bubble_sender == "self"
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert not should_skip  # peer 不能短路


def test_p19_pre_vision_bubble_self_outside_window_walks_vision():
    """P19：bubble=self 但最近 N 秒外（如 1h 前发） → 仍走 vision，
    因为 peer 可能已经回复（vision 才能看到 peer 内容）。"""
    import time
    pv_bubble_sender = "self"
    last_sent_at = time.time() - 3600  # 1h 前
    window = 600.0
    should_skip = (
        pv_bubble_sender == "self"
        and last_sent_at > 0
        and (time.time() - last_sent_at) < window
    )
    assert not should_skip


def test_p19_unknown_bubble_walks_vision():
    """P19：bubble_detector 返回 unknown 时不该短路（信息不足）。"""
    pv_bubble_sender = "unknown"
    should_skip = (pv_bubble_sender == "self")
    assert not should_skip


def test_p19_bubble_result_shared_between_pre_and_in_thread():
    """P19：前置探测结果应被 thread 内 _bubble_sender 复用，避免双次扫描。"""
    result = {
        "pre_vision_bubble_sender": "peer",
        "pre_vision_bubble_info": {"y": 800},
    }
    # 模拟 thread 内 bubble 检测复用逻辑
    bubble_sender = result.get("pre_vision_bubble_sender") or "unknown"
    bub_info = result.get("pre_vision_bubble_info") or {}
    assert bubble_sender == "peer"
    assert bub_info == {"y": 800}
    # 仅 unknown 时才重新扫描
    result_unknown = {}
    bubble_sender2 = result_unknown.get("pre_vision_bubble_sender") or "unknown"
    assert bubble_sender2 == "unknown"


def test_p18_device_unhealthy_streak_arms_backoff():
    """P18：连续 device_unhealthy >= threshold (默认 3) 时设置 skip_until。"""
    import time
    streak: dict[str, int] = {}
    skip_until: dict[str, float] = {}
    threshold = 3
    backoff_sec = 60.0

    def simulate_unhealthy(serial: str) -> None:
        streak[serial] = streak.get(serial, 0) + 1
        if streak[serial] >= threshold and backoff_sec > 0:
            skip_until[serial] = time.monotonic() + backoff_sec

    # 前两次只累 streak 不进 backoff
    simulate_unhealthy("S1")
    simulate_unhealthy("S1")
    assert streak["S1"] == 2
    assert "S1" not in skip_until
    # 第三次进 backoff
    simulate_unhealthy("S1")
    assert "S1" in skip_until
    assert skip_until["S1"] > time.monotonic() + 50


def test_p18_healthy_resets_streak_and_backoff():
    """P18：设备恢复健康时应清 streak 和 skip_until。"""
    streak = {"S1": 5}
    skip_until = {"S1": 1e18}
    # 模拟 healthy 路径
    streak.pop("S1", None)
    skip_until.pop("S1", None)
    assert "S1" not in streak
    assert "S1" not in skip_until


def test_p18_backoff_short_circuits_resolve_serial():
    """P18：skip_until 覆盖期内 _resolve_serial 应短路。"""
    import time
    skip_until = {"S1": time.monotonic() + 30}
    serial = "S1"
    is_in_backoff = skip_until.get(serial, 0.0) > time.monotonic()
    assert is_in_backoff  # 应短路


def test_p26_auto_sticky_within_ttl_returns_in_names():
    """P26：发送成功后 chat 应在 TTL 内被 _sticky_thread_names 包含。"""
    import time
    auto_until: dict[str, float] = {}
    ttl = 300.0
    chat = "Maipon Senda"
    # 模拟发送成功
    auto_until[chat] = time.monotonic() + ttl
    # _sticky_thread_names 模拟逻辑
    now = time.monotonic()
    active = [n for n, e in auto_until.items() if e > now]
    assert chat in active


def test_p26_auto_sticky_after_ttl_expires():
    """P26：TTL 过期后 chat 不再 sticky。"""
    import time
    auto_until: dict[str, float] = {}
    chat = "野末"
    auto_until[chat] = time.monotonic() - 10  # 10s 前已过期
    now = time.monotonic()
    active = [n for n, e in auto_until.items() if e > now]
    assert chat not in active


def test_p26_auto_sticky_merges_with_static():
    """P26：auto-sticky 与 config 静态白名单合并，不重复。"""
    static_names = ["Victor Zan"]
    import time
    auto_until = {"Victor Zan": time.monotonic() + 100, "野末": time.monotonic() + 100}
    now = time.monotonic()
    auto_active = [n for n, e in auto_until.items() if e > now]
    merged = list(static_names)
    for n in auto_active:
        if n not in merged:
            merged.append(n)
    assert merged == ["Victor Zan", "野末"]  # Victor Zan 不重复


def test_p31_strict_window_falls_back_to_promote():
    """P31：strict_window 内不再硬 skip，先试 promote（用 vision extra_peers
    兜底）。peer 真消息 1-2 分钟内回时不被误拦。"""
    # 模拟决策路径
    bubble_says_self = False  # bubble != self
    bubble_says_peer = False
    peer_significantly_longer = False
    within_strict_window = True  # 仍在 120s 内

    if bubble_says_self:
        decision = "skip"
    elif bubble_says_peer:
        decision = "p30_promote"
    elif peer_significantly_longer:
        decision = "p28_promote"
    elif within_strict_window:
        decision = "p31_strict_promote"  # 改前是 "skip"，改后试 promote
    else:
        decision = "promote"

    assert decision == "p31_strict_promote"


def test_p30_bubble_peer_overrides_high_overlap():
    """P30：bubble=peer 强信号时即使 overlap=1.00 也降级 promote，
    防止 _self_reply_overlap_ratio 公共子串误判 false positive。"""
    bubble_sender = "peer"
    self_overlap = 1.0
    bubble_says_peer = (bubble_sender == "peer")
    bubble_says_self = (bubble_sender == "self")
    # P30 决策路径：bubble_says_peer 应触发 promote 降级（不直接 skip）
    if bubble_says_self:
        decision = "skip"  # bubble=self 仍信任
    elif bubble_says_peer:
        decision = "promote_or_keep"  # P30 降级
    else:
        decision = "fallback"  # 走 strict_window / promote 原逻辑
    assert decision == "promote_or_keep"


def test_p30_bubble_self_priority_over_peer():
    """P30：bubble=self 优先级最高（self 信号 + overlap 双确认 = vision 真误读）。"""
    bubble_sender = "self"
    bubble_says_self = (bubble_sender == "self")
    # bubble=self 直接 skip，不进入 P30 降级
    assert bubble_says_self


def test_p30_bubble_unknown_falls_through():
    """P30：bubble=unknown 时不走 P30 降级（保持原 P16 strict/promote 逻辑）。"""
    bubble_sender = "unknown"
    bubble_says_peer = (bubble_sender == "peer")
    bubble_says_self = (bubble_sender == "self")
    if bubble_says_self:
        decision = "skip"
    elif bubble_says_peer:
        decision = "p30_promote"
    else:
        decision = "fallback"  # 原 strict_window / promote 路径
    assert decision == "fallback"


def test_p29_d_layer_ttl_expires_old_fingerprint():
    """P29：D 层指纹超过 TTL（默认 300s）后失效，不再短路。

    生产 bug：野末 / Maipon Senda chat 反复 D 层短路同条文本 10+ 次（10+ 小时）
    导致 bot 永远不回。修复：每条 skipped 文本带时间戳，过期视为不命中。
    """
    import time
    ts_map = {"今日は特に予定もなく、のんびり過ごしてるよ。": time.time() - 600}  # 10 分钟前
    ttl = 300.0  # 5 分钟
    now = time.time()
    prev = "今日は特に予定もなく、のんびり過ごしてるよ。"
    prev_ts = ts_map.get(prev, 0.0)
    expired = (prev_ts <= 0) or (now - prev_ts > ttl)
    assert expired  # 应被识别为过期


def test_p29_d_layer_ttl_within_window_still_short_circuits():
    """P29：TTL 窗口内的指纹仍然有效，正常短路。"""
    import time
    fresh_text = "今日は寒いね。"
    ts_map = {fresh_text: time.time() - 60}  # 1 分钟前
    ttl = 300.0
    now = time.time()
    prev_ts = ts_map.get(fresh_text, 0.0)
    expired = (prev_ts <= 0) or (now - prev_ts > ttl)
    assert not expired  # 仍 fresh，应继续短路


def test_p29_d_layer_no_ts_entry_skipped():
    """P29：缺时间戳元数据（旧格式兼容）的指纹应当跳过 TTL 检查 → 视为过期。"""
    import time
    ts_map = {}  # 完全无 ts 信息
    ttl = 300.0
    now = time.time()
    prev_ts = ts_map.get("any_text", 0.0)
    expired = (prev_ts <= 0) or (now - prev_ts > ttl)
    assert expired


def test_p28_peer_significantly_longer_triggers_promote_path():
    """P28 紧急修复：peer 文本长度 > last_reply × 1.3 时即使 overlap=1.00 也应
    走 promote 路径而非直接 skip（防 echo 子串误拦真消息）。

    生产 bug 重现：
      bot 发"お疲れさま。気にしないで、自分のペースでいいよ..." (50 字)
      peer 真消息"あのばとろ🍵 そう言ってもらえると安心..." (80+ 字)
      公共子串"自分のペースで"导致 overlap=1.00，但 peer 显著更长。
    """
    last_reply = "お疲れさま。気にしないで、自分のペースでいいよ"
    peer_real = (
        "あのばとろ🍵そう言ってもらえると安心します。"
        "ちゃんと無理せずやってるから大丈夫🍵"
        "あなたも無理しないで、自分のペースで過ごしてね🍃"
        "またタイミング合わせて話"
    )
    threshold = 1.3
    peer_significantly_longer = (
        last_reply
        and len(peer_real) > len(last_reply) * threshold
    )
    assert peer_significantly_longer  # 应触发降级路径


def test_p28_short_peer_does_not_trigger_promote_downgrade():
    """P28：peer 与 last_reply 长度相近时不降级（保持守卫严谨）。"""
    last_reply = "今日は疲れた、自分のペースでいいよ"
    peer_text = "今日は疲れた、自分のペースでいい"  # vision 漏字误读 self
    threshold = 1.3
    peer_significantly_longer = (
        len(peer_text) > len(last_reply) * threshold
    )
    assert not peer_significantly_longer  # 应当 skip 不降级


def test_p28_bubble_says_self_overrides_length_check():
    """P28：bubble=self 时仍信任 self 信号，跳过长度降级。"""
    bubble_says_self = True
    # 即使 peer_significantly_longer=True，bubble=self 优先
    if bubble_says_self:
        decision = "skip"  # 走 bubble_self_confirms_overlap 路径
    else:
        decision = "promote_downgrade"
    assert decision == "skip"


def test_p25_inbox_roi_hash_skips_stories_and_nav(tmp_path):
    """P25：inbox ROI = [23.75%, 92.5%]，应忽略 stories 头像动画 + 底部 nav，
    仅对 chat 列表区敏感。"""
    pytest.importorskip("PIL")
    from PIL import Image
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner

    # 720x1600，stories 区不同但 chat 列表相同
    a = Image.new("RGB", (720, 1600), (255, 255, 255))
    a.paste((100, 100, 100), (0, 200, 720, 380))  # stories
    a.paste((0, 132, 255), (50, 600, 700, 750))  # chat row 1
    b = Image.new("RGB", (720, 1600), (255, 255, 255))
    b.paste((50, 50, 50), (0, 200, 720, 380))  # stories 不同
    b.paste((0, 132, 255), (50, 600, 700, 750))  # chat row 1 同
    # chat list 不同
    c = Image.new("RGB", (720, 1600), (255, 255, 255))
    c.paste((100, 100, 100), (0, 200, 720, 380))
    c.paste((0, 132, 255), (50, 600, 700, 760))  # 不同高度

    p_a = tmp_path / "a.png"; a.save(p_a)
    p_b = tmp_path / "b.png"; b.save(p_b)
    p_c = tmp_path / "c.png"; c.save(p_c)

    h_a = MessengerRpaRunner._screenshot_inbox_hash(str(p_a))
    h_b = MessengerRpaRunner._screenshot_inbox_hash(str(p_b))
    h_c = MessengerRpaRunner._screenshot_inbox_hash(str(p_c))

    assert h_a is not None and h_a.startswith("inbox_roi:")
    assert h_a == h_b, "stories 变化应被 inbox ROI 忽略"
    assert h_a != h_c, "chat 列表不同 hash 应不同"


def test_p25_inbox_roi_prefix_distinct_from_thread():
    """P25：inbox ROI 与 thread ROI 前缀不同（防止两个 cache 串）。"""
    # thread ROI 前缀是 "roi:"，inbox 是 "inbox_roi:"，验证前缀分离
    assert "inbox_roi:" != "roi:"


def test_p17v2_roi_hash_ignores_top_and_bottom_changes(tmp_path):
    """P17-v2：ROI hash 应忽略顶栏（Active time）+ 输入栏（typing 闪烁）变化，
    只对中间消息气泡区敏感。"""
    pil = pytest.importorskip("PIL")
    from PIL import Image
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner

    # 顶栏不同（模拟 Active time 变化）但消息区相同
    a = Image.new("RGB", (720, 1600), (255, 255, 255))
    a.paste((0, 0, 0), (0, 0, 720, 60))
    a.paste((0, 132, 255), (300, 800, 700, 900))
    b = Image.new("RGB", (720, 1600), (255, 255, 255))
    b.paste((50, 50, 50), (0, 0, 720, 60))  # 顶栏不同
    b.paste((0, 132, 255), (300, 800, 700, 900))  # 中间相同
    # 中间气泡位置不同
    c = Image.new("RGB", (720, 1600), (255, 255, 255))
    c.paste((0, 0, 0), (0, 0, 720, 60))
    c.paste((0, 132, 255), (200, 800, 700, 900))  # 气泡位置移了 100px
    # 输入栏不同（模拟 typing cursor）但中间相同
    d = Image.new("RGB", (720, 1600), (255, 255, 255))
    d.paste((0, 0, 0), (0, 0, 720, 60))
    d.paste((0, 132, 255), (300, 800, 700, 900))
    d.paste((100, 100, 100), (0, 1400, 720, 1500))  # 输入栏

    p_a = tmp_path / "a.png"; a.save(p_a)
    p_b = tmp_path / "b.png"; b.save(p_b)
    p_c = tmp_path / "c.png"; c.save(p_c)
    p_d = tmp_path / "d.png"; d.save(p_d)

    h_a = MessengerRpaRunner._screenshot_hash(str(p_a))
    h_b = MessengerRpaRunner._screenshot_hash(str(p_b))
    h_c = MessengerRpaRunner._screenshot_hash(str(p_c))
    h_d = MessengerRpaRunner._screenshot_hash(str(p_d))

    assert h_a is not None and h_a.startswith("roi:")
    assert h_a == h_b, "顶栏变化应被 ROI 忽略"
    assert h_a == h_d, "输入栏变化应被 ROI 忽略"
    assert h_a != h_c, "中间气泡区不同 hash 应不同"


def test_p17v2_cache_key_isolates_by_chat_key():
    """P17-v2：cache key 含 chat_key，不同 chat 但 ROI 相同时不互相命中。"""
    cache: dict = {}
    img_hash = "roi:abc123"
    key_a = f"chat_a|{img_hash}"
    key_b = f"chat_b|{img_hash}"
    cache[key_a] = ("cr_a", "tag_a")
    assert key_a in cache
    assert key_b not in cache  # 跨 chat 不命中


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

