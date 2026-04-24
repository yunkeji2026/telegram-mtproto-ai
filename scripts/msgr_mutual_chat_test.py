"""两台 Messenger 后台账号互发一条固定文案（不经 LLM）。

依赖 config.yaml：

* ``messenger_rpa.accounts``：至少 2 条，各自 ``adb_serial``
* ``messenger_rpa.mutual_chat_test.peers``：``account_id -> 对方在本机 Messenger 列表里的显示名`` *（默认值，CLI 可覆盖）*
* ``messenger_rpa.mutual_chat_test.messages``：``account_id -> 要发出的文本`` *（默认值，CLI 可覆盖）*

建议在 **未运行 main.py** 时执行，避免双进程争用 ADB。

用法::

    # 使用 config.yaml 里的 peers/messages
    python scripts/msgr_mutual_chat_test.py

    # CLI 覆盖：两台机对话名不同的情况（强烈推荐，避免 config 硬编错误）
    python scripts/msgr_mutual_chat_test.py \
        --peer-a "Victor Zan" \
        --peer-b "Fernando C" \
        --text-a "A→B 测试" \
        --text-b "B→A 测试"

    # 只打印计划（不操作设备）
    python scripts/msgr_mutual_chat_test.py --dry-run

    # 另一台机未授权 / 只验单路发信（等同单机烟测但沿用 mutual 的 peer 文案）
    python scripts/msgr_mutual_chat_test.py --only-account bg_phone_2

    # 每条发完都做 view-tree ASSERT（默认开；--no-assert 关闭）
    python scripts/msgr_mutual_chat_test.py --no-assert
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _build_skill_manager(cm: Any):
    from src.skills.skill_manager import SkillManager
    from src.ai.ai_client import AIClient

    ai = AIClient(cm)
    init = getattr(ai, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    sk = SkillManager(cm, ai)
    init = getattr(sk, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    return sk


def _serial_for(msgr: Dict[str, Any], account_id: str) -> str:
    for e in msgr.get("accounts") or []:
        if isinstance(e, dict) and str(e.get("id") or "").strip() == account_id:
            return str(e.get("adb_serial") or "").strip()
    return ""


def _pairs_from_cfg(msgr: Dict[str, Any]) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    raw = msgr.get("accounts")
    if not isinstance(raw, list) or len(raw) < 2:
        raise SystemExit("messenger_rpa.accounts 至少需要 2 条")
    ids: List[str] = []
    for e in raw:
        if isinstance(e, dict) and str(e.get("id") or "").strip():
            ids.append(str(e.get("id")).strip())
    if len(ids) < 2:
        raise SystemExit("accounts 里有效 id 不足 2 个")
    mct = msgr.get("mutual_chat_test") or {}
    peers = mct.get("peers") or {}
    msgs = mct.get("messages") or {}
    if not isinstance(peers, dict) or not isinstance(msgs, dict):
        raise SystemExit("mutual_chat_test.peers / messages 须为字典")
    return ids[:2], {str(k): str(v) for k, v in peers.items()}, {
        str(k): str(v) for k, v in msgs.items()
    }


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将发送的内容，不操作设备",
    )
    ap.add_argument(
        "--pause-sec",
        type=float,
        default=4.0,
        help="两次发送之间的间隔（秒）",
    )
    ap.add_argument(
        "--peer-a",
        type=str,
        default=None,
        help="A 机（accounts[0]）上对方的 Messenger 显示名；覆盖 config",
    )
    ap.add_argument(
        "--peer-b",
        type=str,
        default=None,
        help="B 机（accounts[1]）上对方的 Messenger 显示名；覆盖 config",
    )
    ap.add_argument(
        "--text-a",
        type=str,
        default=None,
        help="A 机要发送的文本；覆盖 config",
    )
    ap.add_argument(
        "--text-b",
        type=str,
        default=None,
        help="B 机要发送的文本；覆盖 config",
    )
    ap.add_argument(
        "--no-assert",
        action="store_true",
        help="关闭发送后 view-tree ASSERT（默认开启）",
    )
    ap.add_argument(
        "--only-account",
        type=str,
        default=None,
        metavar="ID",
        help="只跑该账号一条（另一台未连 USB 时），须与 accounts 里 id 一致，如 bg_phone_2",
    )
    args = ap.parse_args()

    from src.utils.config_manager import ConfigManager
    from src.integrations.messenger_rpa.service import MessengerRpaService

    cm = ConfigManager()
    if not await cm.load():
        print("config 加载失败", file=sys.stderr)
        return 2
    msgr = cm.get_messenger_rpa_config() or {}
    pair_ids, peers, msgs = _pairs_from_cfg(msgr)
    a1, a2 = pair_ids[0], pair_ids[1]

    # CLI 覆盖优先：两台机的 peer name / text 从参数拿，缺省回落到 config
    cli_peers = {a1: args.peer_a, a2: args.peer_b}
    cli_texts = {a1: args.text_a, a2: args.text_b}

    def _line(aid: str) -> Tuple[str, str, str]:
        peer = (cli_peers.get(aid) or peers.get(aid) or "").strip()
        text = (cli_texts.get(aid) or msgs.get(aid) or "").strip()
        return aid, peer, text

    l1 = _line(a1)
    l2 = _line(a2)
    for label, tup in (("第一条", l1), ("第二条", l2)):
        aid, peer, text = tup
        if not peer or not text:
            raise SystemExit(
                f"互发配置缺失 account={aid!r} peer={peer!r} text_len={len(text)} "
                f"— 提示：CLI 用 --peer-a/--peer-b/--text-a/--text-b 指定，"
                f"或在 config.yaml 的 messenger_rpa.mutual_chat_test 填写",
            )

    only_one: Optional[Tuple[str, str, str]] = None
    if args.only_account:
        oid = str(args.only_account).strip()
        if oid == l1[0]:
            only_one = l1
        elif oid == l2[0]:
            only_one = l2
        else:
            raise SystemExit(
                f"--only-account 须为 {l1[0]!r} 或 {l2[0]!r}，当前: {oid!r}",
            )

    print("=== 互发计划 ===")
    if only_one is not None:
        print("  (模式: 仅单账号 --only-account)")
        print(
            f"  {only_one[0]} serial={_serial_for(msgr, only_one[0])!r} "
            f"-> 打开会话 {only_one[1]!r}",
        )
        t = only_one[2]
        print(f"    文案: {t[:120]!r}{'…' if len(t) > 120 else ''}")
    else:
        print(
            f"  {l1[0]} serial={_serial_for(msgr, l1[0])!r} "
            f"-> 打开会话 {l1[1]!r}",
        )
        print(f"    文案: {l1[2][:120]!r}{'…' if len(l1[2]) > 120 else ''}")
        print(
            f"  {l2[0]} serial={_serial_for(msgr, l2[0])!r} "
            f"-> 打开会话 {l2[1]!r}",
        )
        print(f"    文案: {l2[2][:120]!r}{'…' if len(l2[2]) > 120 else ''}")

    if args.dry_run:
        print("--dry-run：未操作设备")
        return 0

    # 双发：两机均须 adb「device」；--only-account 时只查该机
    if only_one is not None:
        to_check: List[Tuple[str, str]] = [("only", only_one[0])]
    else:
        to_check = [("A", l1[0]), ("B", l2[0])]
    for _label, _aid in to_check:
        _ser = _serial_for(msgr, _aid)
        if not _ser:
            raise SystemExit(f"account {_aid!r} 未配置 adb_serial")
        try:
            st = subprocess.run(
                ["adb", "-s", _ser, "get-state"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise SystemExit(
                f"adb get-state 失败 account={_aid} serial={_ser!r}: {ex}",
            ) from ex
        _state = (st.stdout or "").strip()
        if _state != "device":
            raise SystemExit(
                f"ADB 未就绪（{_label} 机）account={_aid} serial={_ser!r} "
                f"state={_state!r} stderr={(st.stderr or '')[:200]!r}\n"
                f"  → 请插拔 USB、手机上点「允许 USB 调试」、必要时 adb kill-server 后重试。\n"
                f"  → 单机烟测可用: python scripts/msgr_single_send.py --account ...",
            )

    skill = await _build_skill_manager(cm)
    svc = MessengerRpaService(
        config_manager=cm,
        skill_manager=skill,
        messenger_rpa_cfg=msgr,
    )

    from src.integrations.messenger_rpa import thread_actions as ta

    async def _send(aid: str, peer: str, text: str) -> Dict[str, Any]:
        print(f"\n>>> 发送 account={aid} -> chat_name={peer!r} …")
        r = await svc.send_to_chat_name_for_account(
            aid, chat_name=peer, reply_text=text,
        )
        ok = bool(r.get("ok"))
        step = r.get("step")
        err = r.get("error") or ""
        print(
            f"    send  ok={ok} step={step} total_ms={r.get('total_ms')} "
            f"err={err!r}",
        )
        if not ok:
            print(f"    full: {r}")
            return r

        # U1 + U4 （view-tree 端到端 ASSERT）—— 不依赖 Vision，零成本
        if args.no_assert:
            return r
        serial = _serial_for(msgr, aid)
        if not serial:
            print("    assert 跳过：无 serial")
            return r
        # 发送后 runner 未必退出 thread；立刻 dump 顶栏 + 最后一条气泡
        try:
            vt = ta.verify_thread_title(serial, peer)
            sent = await ta.assert_sent(serial, text, wait_sec=0.5)
            print(
                f"    assert title ok={vt.ok} actual={vt.actual!r} "
                f"reason={vt.reason}  |  "
                f"sent ok={sent.ok} reason={sent.reason} "
                f"seen_by={sent.seen_by!r}",
            )
            r["assert"] = {
                "title_ok": vt.ok,
                "title_actual": vt.actual,
                "title_reason": vt.reason,
                "sent_ok": sent.ok,
                "sent_reason": sent.reason,
                "seen_by": sent.seen_by,
            }
            if not vt.ok:
                r["ok"] = False
                r["error"] = (
                    f"post_send: wrong thread. expected={peer!r} "
                    f"actual={vt.actual!r}"
                )
            elif not sent.ok:
                r["ok"] = False
                r["error"] = f"post_send: not_observed ({sent.reason})"
        except Exception as ex:
            print(f"    assert 异常（忽略）: {type(ex).__name__}: {ex}")
        return r

    if only_one is not None:
        r_only = await _send(only_one[0], only_one[1], only_one[2])
        print("\n=== 最终汇总（单账号） ===")
        print(
            f"  {only_one[0]}  ok={bool(r_only.get('ok'))}  "
            f"assert={r_only.get('assert')}",
        )
        return 0 if r_only.get("ok") else 1

    r1 = await _send(l1[0], l1[1], l1[2])
    await asyncio.sleep(max(0.5, float(args.pause_sec)))
    r2 = await _send(l2[0], l2[1], l2[2])

    print("\n=== 最终汇总 ===")
    print(f"  {l1[0]}  ok={bool(r1.get('ok'))}  assert={r1.get('assert')}")
    print(f"  {l2[0]}  ok={bool(r2.get('ok'))}  assert={r2.get('assert')}")

    return 0 if r1.get("ok") and r2.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
