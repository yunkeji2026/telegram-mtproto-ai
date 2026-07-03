"""P0 协议接入补全：好友名单 + 全量会话列表 + 出站 wamid 去重 的落库回归。

覆盖 store 层新增：upsert_protocol_contacts / list_protocol_contacts /
get_protocol_contact_name / upsert_protocol_chats，以及号码补名、占位会话与真实
消息按 conversation_id 归并、出站 wamid 幂等去重。纯 store 层，无需网络/Node。
"""

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore


def test_upsert_and_list_protocol_contacts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    n = store.upsert_protocol_contacts("whatsapp", "acct1", [
        {"jid": "639111", "name": "Alice", "notify": "ally"},
        {"jid": "639222", "name": "", "notify": "Bob"},
        {"jid": "639333", "name": "", "notify": ""},   # 无名条目仍存（Node 侧已过滤，store 不拒）
        "not-a-dict",                                    # 脏数据被跳过
    ])
    assert n == 3
    rows = store.list_protocol_contacts("whatsapp", "acct1")
    # 有名字的排前
    assert rows[0]["chat_key"] == "639111"
    assert rows[0]["name"] == "Alice"
    assert store.get_protocol_contact_name("whatsapp", "acct1", "639111") == "Alice"
    # name 空 → 回落 notify_name
    assert store.get_protocol_contact_name("whatsapp", "acct1", "639222") == "Bob"
    assert store.get_protocol_contact_name("whatsapp", "acct1", "639333") == ""
    assert store.get_protocol_contact_name("whatsapp", "acct1", "nope") == ""
    store.close()


def test_contact_sync_backfills_bare_number_conversation_name(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 会话最初显名是裸号码
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:639111", platform="whatsapp",
        account_id="acct1", chat_key="639111", display_name="639111", last_ts=10,
    ))
    store.upsert_protocol_contacts("whatsapp", "acct1", [{"jid": "639111", "name": "Alice"}])
    row = [c for c in store.list_conversations(platform="whatsapp")
           if c["conversation_id"] == "whatsapp:acct1:639111"][0]
    assert row["display_name"] == "Alice"    # 裸号码被通讯录名补齐
    store.close()


def test_contact_sync_does_not_overwrite_real_name(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:639111", platform="whatsapp",
        account_id="acct1", chat_key="639111", display_name="老板", last_ts=10,
    ))
    store.upsert_protocol_contacts("whatsapp", "acct1", [{"jid": "639111", "name": "Alice"}])
    row = [c for c in store.list_conversations(platform="whatsapp")
           if c["conversation_id"] == "whatsapp:acct1:639111"][0]
    assert row["display_name"] == "老板"      # 已有真名不被覆盖
    store.close()


def test_upsert_protocol_chats_creates_placeholder_and_merges(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 先建通讯录名 → 会话占位应带上名字
    store.upsert_protocol_contacts("whatsapp", "acct1", [{"jid": "639111", "name": "Alice"}])
    n = store.upsert_protocol_chats("whatsapp", "acct1", [
        {"jid": "639111", "ts": 100, "unread": 2},
        {"jid": "639222", "name": "Bob Chat", "ts": 200},
    ])
    assert n == 2
    convs = {c["conversation_id"]: c for c in store.list_conversations(platform="whatsapp")}
    assert convs["whatsapp:acct1:639111"]["display_name"] == "Alice"   # 通讯录补名
    assert convs["whatsapp:acct1:639222"]["display_name"] == "Bob Chat"
    # 占位会话之后收到真实消息 → 同 conversation_id 归并（不新建重复会话）
    store.ingest_batch(
        InboxConversation(conversation_id="whatsapp:acct1:639111", platform="whatsapp",
                          account_id="acct1", chat_key="639111", display_name="Alice",
                          last_text="hi there", last_ts=300),
        [InboxMessage(conversation_id="whatsapp:acct1:639111",
                      platform_msg_id="W1", text="hi there", ts=300)],
    )
    convs = {c["conversation_id"]: c for c in store.list_conversations(platform="whatsapp")}
    assert len(convs) == 2                          # 未凭空多出会话
    assert convs["whatsapp:acct1:639111"]["last_text"] == "hi there"
    store.close()


def test_get_oldest_message_anchor(tmp_path):
    """P1：按需拉更早历史锚点——取最旧的带 platform_msg_id 的消息，跳过无 id 的行。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = InboxConversation(conversation_id="whatsapp:acct1:639111", platform="whatsapp",
                             account_id="acct1", chat_key="639111", last_ts=10)
    # 无 id 的行（更早）不应被选作锚点（fetchMessageHistory 需要 wamid）
    store.ingest_batch(conv, [
        InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="",
                     direction="in", text="no-id oldest", ts=50),
        InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="W_OLD",
                     direction="in", text="anchor", ts=100),
        InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="W_NEW",
                     direction="out", text="newer", ts=200),
    ])
    anchor = store.get_oldest_message("whatsapp:acct1:639111")
    assert anchor is not None
    assert anchor["platform_msg_id"] == "W_OLD"   # 最旧的带 id 行
    assert anchor["direction"] == "in"
    # 空会话 → None
    assert store.get_oldest_message("whatsapp:acct1:none") is None
    store.close()


def test_upsert_protocol_chats_group_flag_sets_chat_type(tmp_path):
    """P2：is_group=True 的会话占位落 chat_type=group（分流「群组动态」，不刷 SLA）。"""
    store = InboxStore(tmp_path / "inbox.db")
    n = store.upsert_protocol_chats("whatsapp", "acct1", [
        {"jid": "639111", "name": "Alice", "ts": 100},                 # 私聊
        {"jid": "120363-777", "name": "家族群", "ts": 200, "is_group": True},  # 群
    ])
    assert n == 2
    convs = {c["conversation_id"]: c for c in store.list_conversations(platform="whatsapp")}
    assert convs["whatsapp:acct1:639111"]["chat_type"] == "private"
    assert convs["whatsapp:acct1:120363-777"]["chat_type"] == "group"
    assert convs["whatsapp:acct1:120363-777"]["display_name"] == "家族群"
    store.close()


def test_ingest_incoming_group_chat_type(tmp_path):
    """P2：ingest_incoming(chat_type=group) → 会话归一为群，供下游分流。"""
    from src.integrations.protocol_bridge import ingest_incoming
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="whatsapp", account_id="acct1", chat_key="120363-888",
        name="工作群", text="张三：开会了", ts=300, msg_id="G1",
        direction="in", chat_type="group",
    )
    assert cid == "whatsapp:acct1:120363-888"
    row = [c for c in store.list_conversations(platform="whatsapp")
           if c["conversation_id"] == cid][0]
    assert row["chat_type"] == "group"
    assert row["last_text"] == "张三：开会了"
    store.close()


def test_ingest_incoming_reply_to_roundtrip(tmp_path):
    """P4-2：ingest_incoming(reply_to=…) → messages 落 reply_to_*，thread 映射回带引用字段。"""
    from src.integrations.protocol_bridge import ingest_incoming
    from src.inbox.normalizer import store_message_to_obj
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="whatsapp", account_id="acct1", chat_key="639111",
        name="Alice", text="是的没错", ts=500, msg_id="R1", direction="in",
        reply_to={"id": "Q1", "text": "你确认要下单吗？", "sender": "客服小美"},
    )
    assert cid == "whatsapp:acct1:639111"
    rows = store.list_recent_messages(cid, limit=10)
    row = [r for r in rows if r.get("platform_msg_id") == "R1"][0]
    assert row["reply_to_id"] == "Q1"
    assert row["reply_to_text"] == "你确认要下单吗？"
    assert row["reply_to_sender"] == "客服小美"
    obj = store_message_to_obj(row)
    assert obj["reply_to_text"] == "你确认要下单吗？"
    assert obj["reply_to_sender"] == "客服小美"
    store.close()


def test_ingest_incoming_without_reply_to_is_blank(tmp_path):
    """无引用的普通消息 → reply_to_* 全空（纯加法不影响既有行为）。"""
    from src.integrations.protocol_bridge import ingest_incoming
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="whatsapp", account_id="acct1", chat_key="639222",
        name="Bob", text="hello", ts=10, msg_id="N1", direction="in",
    )
    row = [r for r in store.list_recent_messages(cid, limit=10)
           if r.get("platform_msg_id") == "N1"][0]
    assert row["reply_to_id"] == ""
    assert row["reply_to_text"] == ""
    store.close()


def test_set_reaction_aggregates_and_removes(tmp_path):
    """P4-3：set_reaction 按 sender 键落库，thread 聚合成 [{emoji,count}]；空 emoji=撤销。"""
    from src.integrations.protocol_bridge import ingest_incoming
    from src.inbox.normalizer import store_message_to_obj
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="whatsapp", account_id="acct1", chat_key="639111",
        name="Alice", text="生日快乐", ts=100, msg_id="M1", direction="out",
    )
    # 两人点 👍、一人点 ❤️
    assert store.set_reaction(cid, "M1", "u1", "👍") is True
    assert store.set_reaction(cid, "M1", "u2", "👍") is True
    assert store.set_reaction(cid, "M1", "u3", "❤️") is True
    row = [r for r in store.list_recent_messages(cid, limit=10)
           if r.get("platform_msg_id") == "M1"][0]
    reacts = store_message_to_obj(row)["reactions"]
    by = {r["emoji"]: r["count"] for r in reacts}
    assert by == {"👍": 2, "❤️": 1}
    assert reacts[0]["emoji"] == "👍"          # 计数降序
    # u1 改成 😂（替换，不是新增）
    store.set_reaction(cid, "M1", "u1", "😂")
    row = [r for r in store.list_recent_messages(cid, limit=10)
           if r.get("platform_msg_id") == "M1"][0]
    by = {r["emoji"]: r["count"] for r in store_message_to_obj(row)["reactions"]}
    assert by == {"👍": 1, "❤️": 1, "😂": 1}
    # u2 撤销（空 emoji）
    store.set_reaction(cid, "M1", "u2", "")
    row = [r for r in store.list_recent_messages(cid, limit=10)
           if r.get("platform_msg_id") == "M1"][0]
    by = {r["emoji"]: r["count"] for r in store_message_to_obj(row)["reactions"]}
    assert "👍" not in by
    store.close()


def test_set_reaction_missing_message_is_noop(tmp_path):
    """目标消息未落库（更早历史未同步）→ set_reaction 返回 False，不建空消息。"""
    store = InboxStore(tmp_path / "inbox.db")
    assert store.set_reaction("whatsapp:acct1:639111", "NOPE", "u1", "👍") is False
    store.close()


def test_set_message_status_monotonic_upgrade(tmp_path):
    """P4-4 已读回执：状态单调升级 sent→delivered→read，回执乱序不降级。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = InboxConversation(conversation_id="whatsapp:acct1:639111", platform="whatsapp",
                             account_id="acct1", chat_key="639111", last_ts=10)
    out = InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="WAMIDX",
                       direction="out", text="您好", ts=10)
    assert store.ingest_batch(conv, [out]) == 1
    from src.inbox.normalizer import store_message_to_obj

    def _status():
        rows = store.list_recent_messages("whatsapp:acct1:639111", limit=10)
        row = [r for r in rows if r["platform_msg_id"] == "WAMIDX"][0]
        return store_message_to_obj(row)["status"]

    assert store.set_message_status("whatsapp:acct1:639111", "WAMIDX", "delivered") is True
    assert _status() == "delivered"
    # read 更高 → 升级
    assert store.set_message_status("whatsapp:acct1:639111", "WAMIDX", "read") is True
    assert _status() == "read"
    # 乱序回来的 delivered 更低 → 幂等成功但不降级
    assert store.set_message_status("whatsapp:acct1:639111", "WAMIDX", "delivered") is True
    assert _status() == "read"
    # sent 比 delivered 更低（先到但先落 delivered 场景）→ 仍不降
    assert store.set_message_status("whatsapp:acct1:639111", "WAMIDX", "sent") is True
    assert _status() == "read"
    store.close()


def test_set_message_status_missing_message_is_noop(tmp_path):
    """目标出站消息未落库 → set_message_status 返回 False，不建空消息。"""
    store = InboxStore(tmp_path / "inbox.db")
    assert store.set_message_status("whatsapp:acct1:639111", "NOPE", "read") is False
    store.close()


def test_revoke_and_edit_roundtrip(tmp_path):
    """P4-6A：撤回置 revoked=1（气泡置灰）；编辑改正文 + edited=1（清译文缓存）。"""
    from src.inbox.normalizer import store_message_to_obj
    store = InboxStore(tmp_path / "inbox.db")
    cid = "whatsapp:acct1:639111"
    conv = InboxConversation(conversation_id=cid, platform="whatsapp",
                             account_id="acct1", chat_key="639111", last_ts=10)
    m1 = InboxMessage(conversation_id=cid, platform_msg_id="WAMID_A",
                      direction="in", text="原始内容", ts=10)
    m2 = InboxMessage(conversation_id=cid, platform_msg_id="WAMID_B",
                      direction="in", text="待撤回", ts=11)
    assert store.ingest_batch(conv, [m1, m2]) == 2

    def _obj(pmid):
        rows = store.list_recent_messages(cid, limit=10)
        row = [r for r in rows if r["platform_msg_id"] == pmid][0]
        return store_message_to_obj(row)

    # 编辑 WAMID_A
    assert store.apply_message_edit(cid, "WAMID_A", "编辑后的内容") is True
    a = _obj("WAMID_A")
    assert a["text"] == "编辑后的内容" and a["edited"] is True and a["revoked"] is False
    # 撤回 WAMID_B
    assert store.mark_message_revoked(cid, "WAMID_B") is True
    b = _obj("WAMID_B")
    assert b["revoked"] is True
    # 幂等：再撤回仍 True
    assert store.mark_message_revoked(cid, "WAMID_B") is True
    store.close()


def test_revoke_edit_missing_message_is_noop(tmp_path):
    """目标消息未落库 / 编辑空正文 → 返回 False，不建空消息。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "whatsapp:acct1:639111"
    assert store.mark_message_revoked(cid, "NOPE") is False
    assert store.apply_message_edit(cid, "NOPE", "x") is False
    conv = InboxConversation(conversation_id=cid, platform="whatsapp",
                             account_id="acct1", chat_key="639111", last_ts=10)
    store.ingest_batch(conv, [InboxMessage(conversation_id=cid,
                       platform_msg_id="WAMID_C", direction="in", text="hi", ts=10)])
    assert store.apply_message_edit(cid, "WAMID_C", "") is False  # 空正文不改
    store.close()


def test_mark_outbound_read_upto_telegram(tmp_path):
    """P4-4（Telegram）：UpdateReadHistoryOutbox 已读到 max_id → 该会话所有出站
    消息中 id ≤ max_id 一次性升级为 read；> max_id 与入站消息不受影响。"""
    conv_id = "telegram:acct1:12345"
    store = InboxStore(tmp_path / "inbox.db")
    conv = InboxConversation(conversation_id=conv_id, platform="telegram",
                             account_id="acct1", chat_key="12345", last_ts=10)
    msgs = [
        InboxMessage(conversation_id=conv_id, platform_msg_id="100",
                     direction="out", text="a", ts=10),
        InboxMessage(conversation_id=conv_id, platform_msg_id="101",
                     direction="out", text="b", ts=11),
        InboxMessage(conversation_id=conv_id, platform_msg_id="105",
                     direction="out", text="c", ts=12),   # > max_id → 仍未读
        InboxMessage(conversation_id=conv_id, platform_msg_id="102",
                     direction="in", text="hi", ts=13),    # 入站 → 不动
    ]
    assert store.ingest_batch(conv, msgs) == 4
    from src.inbox.normalizer import store_message_to_obj

    def _status(pid):
        rows = store.list_recent_messages(conv_id, limit=20)
        row = [r for r in rows if r["platform_msg_id"] == pid][0]
        return store_message_to_obj(row)["status"]

    n = store.mark_outbound_read_upto(conv_id, 101)
    assert n == 2
    assert _status("100") == "read"
    assert _status("101") == "read"
    assert _status("105") == ""     # 晚于已读游标
    assert _status("102") == ""     # 入站不受影响
    # 幂等：再次上报同一游标 → 无新升级
    assert store.mark_outbound_read_upto(conv_id, 101) == 0
    # 推进游标 → 覆盖 105
    assert store.mark_outbound_read_upto(conv_id, 999) == 1
    assert _status("105") == "read"
    store.close()


def test_mark_outbound_read_upto_bad_input(tmp_path):
    """非数字 max_id / 空会话 → 返回 0，绝不抛。"""
    store = InboxStore(tmp_path / "inbox.db")
    assert store.mark_outbound_read_upto("telegram:acct1:1", "abc") == 0
    assert store.mark_outbound_read_upto("", 100) == 0
    assert store.mark_outbound_read_upto("telegram:acct1:1", 0) == 0
    store.close()


def test_tg_peer_to_chat_key():
    """pyrogram raw Peer → 收件箱 chat_key 归一（用户正数 / 群负数 / 频道 -100 前缀）。"""
    from src.integrations.protocol_bridge import tg_peer_to_chat_key

    class _PeerUser:
        user_id = 777

    class _PeerChat:
        chat_id = 555

    class _PeerChannel:
        channel_id = 4242

    assert tg_peer_to_chat_key(_PeerUser()) == "777"
    assert tg_peer_to_chat_key(_PeerChat()) == "-555"
    assert tg_peer_to_chat_key(_PeerChannel()) == "-1004242"
    assert tg_peer_to_chat_key(None) == ""
    assert tg_peer_to_chat_key(object()) == ""


def test_outbound_wamid_dedup_agent_vs_phone_echo(tmp_path):
    """坐席发送镜像(带 wamid) 与手机端 fromMe 回显(同 wamid) → 同键去重，只落一条。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = InboxConversation(conversation_id="whatsapp:acct1:639111", platform="whatsapp",
                             account_id="acct1", chat_key="639111", last_ts=10)
    out1 = InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="WAMID9",
                        direction="out", text="您好", ts=10)
    out2 = InboxMessage(conversation_id="whatsapp:acct1:639111", platform_msg_id="WAMID9",
                        direction="out", text="您好", ts=11)  # 回显 ts 有秒级漂移
    assert store.ingest_batch(conv, [out1]) == 1
    assert store.ingest_batch(conv, [out2]) == 0    # 同 wamid → INSERT OR IGNORE 去重
    assert store.count_messages("whatsapp:acct1:639111") == 1
    store.close()
