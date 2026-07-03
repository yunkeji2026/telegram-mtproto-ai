"""会话 peer 身份「惰性解析 / 自愈补名」观测（进程级单例）。

背景：历史会话在昵称优先级护栏上线前，显名只存了裸 id（Telegram 数字 / LINE mid）。现有
两条自愈链路把它们补成真实昵称，本模块把「补了多少、命中缓存多少、取不到多少、client 掉线
多少」变成可观测计数，闭合「到底 healed 了多少存量数字号」：

- ``tg_open``：坐席**打开**数字号会话时按需 get_chat 解析（resolve-peer 端点）。
- ``tg_avatar``：滚动懒加载头像时（avatar 端点已 get_chat）**顺带**把身份落库（零额外 API）。
- ``line``：LINE 私聊入站按需 getContactsV2 拉发送者显示名。

outcome 语义：``resolved`` 补到真身份并落库 / ``cache_hit`` 命中缓存(免打 API) /
``miss`` 尝试了但取不到（失败或无可用字段）/ ``unavailable`` client 不在线。

**多账号 client 路由**（``routing``）：头像/补名按 ``account_id`` 取 pyrogram client 时，是命中该
账号**受管 worker**（``worker``＝多账号真跑起来了）、还是**回落进程主 client**（``fallback``＝
单号/主账号，或该账号 worker 没跑→非主账号会降级），或**两者皆无**（``none``＝TG 整体离线）。
含**按账号**细分（bounded）——非主账号若长期只在 ``fallback`` 有数，即其协议 worker 掉线、
头像/补名正在静默降级，是本轮多账号路由的自证 + 运维盲点告警。

**入站身份分类**（``ingest``）：经 HTTP ``/ingest`` 的号（WhatsApp/Messenger）每条私聊入站，
其会话显名来源分三类——``named``（来显名本就是真名）/ ``backfilled``（来显名缺失/是裸 id，
靠已同步通讯录名补上）/ ``raw``（仍是裸 chat_key＝用户最初抱怨的「一排数字」）。按平台细分，
``raw`` 占比即「该平台还有多少会话没真名」，把最初的抱怨量化成可跟踪指标（Telegram/LINE 走
in-process，其身份健康见上面的 by_source/routing，两套不重叠互补）。

**头像代理命中**（``avatar``）：头像端点 ``/api/platforms/{platform}/{account_id}/avatar`` 每次请求
的结局，按平台分五类——``cache_hit``（磁盘缓存命中→302，未回源）/ ``fetched``（回源取直链+下载
落盘成功→302）/ ``empty``（上游返回空 url：无头像，或 messenger 轮询尚未缓存该线程）/ ``error``
（上游异常/服务未启用/下载失败）/ ``neg_hit``（负缓存命中，whatsapp「无头像」1 天内不回源）。
**这是「Node 抓头像到底成没成」的线上体检**——messenger 若 ``empty`` 长期高企，即 scontent 抓取
有盲区（选择器失配/未开轮询），闭合「抓取逻辑测不到→线上也看得见」；``(cache_hit+fetched)/total``
即真正拿到图的命中率。

**资料面板就绪度**（``panel``，F3）：坐席**打开**一个会话时（``/thread`` 端点服务该会话），按平台记
「面板将展示的文字身份完整度」——``opens``（打开的**去重**会话数，按 conversation_id 每进程只记一次，
避免轮询/加载更早重复计数）与其中 ``name``（有真实昵称＝非空、非裸 id）/ ``username`` / ``phone`` 命中数。
``name/opens`` 即「坐席实际打开的会话里，多少一眼能认出是谁」，与 ``ingest``（入站时、仅 WA/Messenger）
互补——**覆盖全平台、以「人真正在看」为分母**。口径为 store 侧 serve-time 状态（Telegram 数字号的
open 后自愈另见 ``tg_open``；头像覆盖另见 ``avatar``，故此处只track 文字身份不重复计头像）。

**实时列表回读补齐**（``readback``，F5，量化 F4）：``read_from_store=false`` 时列表/线程走实时聚合，多数
适配器只给裸 name/无头像，而 side-effect ingest 早把 username/phone/头像 + 更优 display_name 落了库；
F4 在 serve 时用 store 已持久身份对 live 行做「仅补空/仅升级」。本维度按平台记这次补齐——``rows``
（被回读救回的**去重**会话数，按 conversation_id 每进程只记一次首补，避免轮询重复计数）与其中补上
``name``/``username``/``phone``/``avatar`` 的次数。某平台 ``rows`` 长期高＝其适配器贫身份、正靠回读兜底
（决策「改适配器 or 保留回读」的依据）；长期为 0＝该平台 live 本就带全身份、回读对它是空转。

风格对齐 ``src/web/frontend_error_stats.py``：无新增依赖，线程安全，进程级单例，**只存计数**。
经 ``dump()``→``/api/workspace/metrics.peer_identity``、``dump_prom()``→Prometheus 观测。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_SOURCES = ("tg_open", "tg_avatar", "line")
_OUTCOMES = ("resolved", "cache_hit", "miss", "unavailable")
_ROUTES = ("worker", "fallback", "none")
_ROUTE_ACCT_MAX = 200  # per-account 路由 distinct 账号上限，防内存无界（超限只记聚合）
_INGEST_OUTCOMES = ("named", "backfilled", "raw")
_INGEST_PLAT_MAX = 32  # 入站身份 distinct 平台上限（平台本就有限，防脏输入撑爆）
_AVATAR_OUTCOMES = ("cache_hit", "fetched", "empty", "error", "neg_hit")
_AVATAR_PLAT_MAX = 32  # 头像代理 distinct 平台上限（同上，防脏输入撑爆）
_PANEL_FIELDS = ("opens", "name", "username", "phone")
_PANEL_PLAT_MAX = 32  # 资料面板就绪度 distinct 平台上限（同上，防脏输入撑爆）
_READBACK_FIELDS = ("rows", "name", "username", "phone", "avatar")
_READBACK_ID_FIELDS = ("name", "username", "phone", "avatar")  # rows 外的字段（record 时校验）
_READBACK_PLAT_MAX = 32  # 实时列表回读 distinct 平台上限（同上，防脏输入撑爆）


class PeerIdentityStats:
    """peer 身份解析计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "_c", "_r", "_ra",
                 "_ing", "_av", "_pn", "_rb")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self._c: Dict[str, Dict[str, int]] = {
            s: {o: 0 for o in _OUTCOMES} for s in _SOURCES
        }
        self._r: Dict[str, int] = {x: 0 for x in _ROUTES}          # 路由聚合
        self._ra: Dict[str, Dict[str, int]] = {}                   # 按账号路由（bounded）
        self._ing: Dict[str, Dict[str, int]] = {}                  # 入站身份分类（按平台，bounded）
        self._av: Dict[str, Dict[str, int]] = {}                   # 头像代理命中（按平台，bounded）
        self._pn: Dict[str, Dict[str, int]] = {}                   # 资料面板就绪度（按平台，bounded）
        self._rb: Dict[str, Dict[str, int]] = {}                   # 实时列表回读补齐（按平台，bounded）

    def record(self, source: str, outcome: str) -> None:
        """记一次解析结果（未知 source/outcome 静默忽略，绝不抛，绝不影响主流程）。"""
        if source not in self._c or outcome not in _OUTCOMES:
            return
        with self._lock:
            self._c[source][outcome] += 1
            self._last_ts = time.time()

    def record_route(self, outcome: str, account_id: str = "") -> None:
        """记一次「按 account_id 取 TG client」的路由决策（``worker``/``fallback``/``none``）。

        聚合恒记；``account_id`` 给定则同时记按账号细分（distinct 账号超 ``_ROUTE_ACCT_MAX``
        时只增聚合、不再新增账号槽）。未知 outcome 静默忽略，绝不抛。
        """
        if outcome not in self._r:
            return
        with self._lock:
            self._r[outcome] += 1
            self._last_ts = time.time()
            acct = str(account_id or "").strip()
            if not acct:
                return
            slot = self._ra.get(acct)
            if slot is None:
                if len(self._ra) >= _ROUTE_ACCT_MAX:
                    return  # 超上限：只记聚合，保护内存
                slot = {x: 0 for x in _ROUTES}
                self._ra[acct] = slot
            slot[outcome] += 1

    def record_ingest(self, platform: str, outcome: str) -> None:
        """记一次入站私聊身份分类（``named``/``backfilled``/``raw``），按平台细分。

        未知 outcome / 空平台静默忽略；distinct 平台超 ``_INGEST_PLAT_MAX`` 不再新增。绝不抛。
        """
        platform = str(platform or "").strip().lower()
        if not platform or outcome not in _INGEST_OUTCOMES:
            return
        with self._lock:
            slot = self._ing.get(platform)
            if slot is None:
                if len(self._ing) >= _INGEST_PLAT_MAX:
                    return
                slot = {o: 0 for o in _INGEST_OUTCOMES}
                self._ing[platform] = slot
            slot[outcome] += 1
            self._last_ts = time.time()

    def record_avatar(self, platform: str, outcome: str) -> None:
        """记一次头像端点请求结局（``cache_hit``/``fetched``/``empty``/``error``/``neg_hit``），按平台细分。

        未知 outcome / 空平台静默忽略；distinct 平台超 ``_AVATAR_PLAT_MAX`` 不再新增。绝不抛。
        """
        platform = str(platform or "").strip().lower()
        if not platform or outcome not in _AVATAR_OUTCOMES:
            return
        with self._lock:
            slot = self._av.get(platform)
            if slot is None:
                if len(self._av) >= _AVATAR_PLAT_MAX:
                    return
                slot = {o: 0 for o in _AVATAR_OUTCOMES}
                self._av[platform] = slot
            slot[outcome] += 1
            self._last_ts = time.time()

    def record_panel(
        self, platform: str, *, has_name: bool = False,
        has_username: bool = False, has_phone: bool = False,
    ) -> None:
        """记一次会话打开时的资料面板文字身份完整度（去重由调用方按 conversation_id 保证）。

        ``opens`` 恒 +1；``name``/``username``/``phone`` 各按对应字段是否已具备 +1。空平台静默忽略；
        distinct 平台超 ``_PANEL_PLAT_MAX`` 不再新增。绝不抛。
        """
        platform = str(platform or "").strip().lower()
        if not platform:
            return
        with self._lock:
            slot = self._pn.get(platform)
            if slot is None:
                if len(self._pn) >= _PANEL_PLAT_MAX:
                    return
                slot = {f: 0 for f in _PANEL_FIELDS}
                self._pn[platform] = slot
            slot["opens"] += 1
            if has_name:
                slot["name"] += 1
            if has_username:
                slot["username"] += 1
            if has_phone:
                slot["phone"] += 1
            self._last_ts = time.time()

    def record_readback(self, platform: str, fields) -> None:
        """记一次「实时列表回读 store 身份」补齐（F5，量化 F4；去重由调用方按 conversation_id 保证）。

        ``rows`` 恒 +1（一个被回读救回的**去重**会话）；``fields`` 中每个已知字段
        （``name``/``username``/``phone``/``avatar``）各 +1。空平台 / 无有效字段静默忽略；
        distinct 平台超 ``_READBACK_PLAT_MAX`` 不再新增。绝不抛。
        """
        platform = str(platform or "").strip().lower()
        fset = {str(f).strip().lower() for f in (fields or [])} & set(_READBACK_ID_FIELDS)
        if not platform or not fset:
            return
        with self._lock:
            slot = self._rb.get(platform)
            if slot is None:
                if len(self._rb) >= _READBACK_PLAT_MAX:
                    return
                slot = {f: 0 for f in _READBACK_FIELDS}
                self._rb[platform] = slot
            slot["rows"] += 1
            for f in fset:
                slot[f] += 1
            self._last_ts = time.time()

    def _totals(self) -> Dict[str, int]:
        agg = {o: 0 for o in _OUTCOMES}
        for ov in self._c.values():
            for o, n in ov.items():
                agg[o] += n
        return agg

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            tot = self._totals()
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": sum(tot.values()),
                "resolved": tot["resolved"],
                "cache_hits": tot["cache_hit"],
                "misses": tot["miss"],
                "unavailable": tot["unavailable"],
                "by_source": {s: dict(v) for s, v in self._c.items()},
                "routing": {
                    "total": sum(self._r.values()),
                    "worker": self._r["worker"],
                    "fallback": self._r["fallback"],
                    "none": self._r["none"],
                    "by_account": {a: dict(v) for a, v in self._ra.items()},
                },
                "ingest": {
                    "total": sum(sum(v.values()) for v in self._ing.values()),
                    "named": sum(v["named"] for v in self._ing.values()),
                    "backfilled": sum(v["backfilled"] for v in self._ing.values()),
                    "raw": sum(v["raw"] for v in self._ing.values()),
                    "by_platform": {p: dict(v) for p, v in self._ing.items()},
                },
                "avatar": {
                    "total": sum(sum(v.values()) for v in self._av.values()),
                    "cache_hit": sum(v["cache_hit"] for v in self._av.values()),
                    "fetched": sum(v["fetched"] for v in self._av.values()),
                    "empty": sum(v["empty"] for v in self._av.values()),
                    "error": sum(v["error"] for v in self._av.values()),
                    "neg_hit": sum(v["neg_hit"] for v in self._av.values()),
                    "by_platform": {p: dict(v) for p, v in self._av.items()},
                },
                "panel": {
                    "opens": sum(v["opens"] for v in self._pn.values()),
                    "name": sum(v["name"] for v in self._pn.values()),
                    "username": sum(v["username"] for v in self._pn.values()),
                    "phone": sum(v["phone"] for v in self._pn.values()),
                    "by_platform": {p: dict(v) for p, v in self._pn.items()},
                },
                "readback": {
                    "rows": sum(v["rows"] for v in self._rb.values()),
                    "name": sum(v["name"] for v in self._rb.values()),
                    "username": sum(v["username"] for v in self._rb.values()),
                    "phone": sum(v["phone"] for v in self._rb.values()),
                    "avatar": sum(v["avatar"] for v in self._rb.values()),
                    "by_platform": {p: dict(v) for p, v in self._rb.items()},
                },
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP peer_identity_resolve_total Peer identity lazy-resolve "
                "outcomes (self-healing numeric ids → real names)",
                "# TYPE peer_identity_resolve_total counter",
            ]
            for s, ov in sorted(self._c.items()):
                for o, n in sorted(ov.items()):
                    lines.append(
                        f'peer_identity_resolve_total{{source="{s}",outcome="{o}"}} {int(n)}'
                    )
            # 多账号 client 路由（聚合；per-account 只在 JSON 出，避免 Prometheus 高基数标签）
            lines.append(
                "# HELP peer_identity_client_route_total TG client routing for "
                "identity/avatar (managed worker vs main-client fallback)"
            )
            lines.append("# TYPE peer_identity_client_route_total counter")
            for o in _ROUTES:
                lines.append(
                    f'peer_identity_client_route_total{{outcome="{o}"}} {int(self._r[o])}'
                )
            # 入站身份分类（按平台 × named/backfilled/raw；raw=仍是裸 id 的量）
            lines.append(
                "# HELP peer_identity_ingest_total Inbound conversation identity "
                "classification (named / address-book backfilled / raw numeric id)"
            )
            lines.append("# TYPE peer_identity_ingest_total counter")
            for p, ov in sorted(self._ing.items()):
                for o in _INGEST_OUTCOMES:
                    lines.append(
                        f'peer_identity_ingest_total{{platform="{p}",outcome="{o}"}} {int(ov[o])}'
                    )
            # 头像代理命中（按平台 × cache_hit/fetched/empty/error/neg_hit）
            lines.append(
                "# HELP peer_identity_avatar_total Avatar proxy request outcomes "
                "(disk cache_hit / re-source fetched / empty / error / neg_hit)"
            )
            lines.append("# TYPE peer_identity_avatar_total counter")
            for p, ov in sorted(self._av.items()):
                for o in _AVATAR_OUTCOMES:
                    lines.append(
                        f'peer_identity_avatar_total{{platform="{p}",outcome="{o}"}} {int(ov[o])}'
                    )
            # 资料面板就绪度（按平台 × opens/name/username/phone；name/opens=打开即可辨识占比）
            lines.append(
                "# HELP peer_identity_panel_total Info-panel text-identity completeness "
                "at conversation open (distinct convos): opens vs real-name/username/phone present"
            )
            lines.append("# TYPE peer_identity_panel_total counter")
            for p, ov in sorted(self._pn.items()):
                for f in _PANEL_FIELDS:
                    lines.append(
                        f'peer_identity_panel_total{{platform="{p}",field="{f}"}} {int(ov[f])}'
                    )
            # 实时列表回读补齐（按平台 × rows/name/username/phone/avatar；rows=去重救回会话数）
            lines.append(
                "# HELP peer_identity_readback_total Live-list identity readback from "
                "store (only-fill-empty at serve time): rows=distinct convos rescued + "
                "fields filled (name/username/phone/avatar)"
            )
            lines.append("# TYPE peer_identity_readback_total counter")
            for p, ov in sorted(self._rb.items()):
                for f in _READBACK_FIELDS:
                    lines.append(
                        f'peer_identity_readback_total{{platform="{p}",field="{f}"}} {int(ov[f])}'
                    )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._c = {s: {o: 0 for o in _OUTCOMES} for s in _SOURCES}
            self._r = {x: 0 for x in _ROUTES}
            self._ra = {}
            self._ing = {}
            self._av = {}
            self._pn = {}
            self._rb = {}
            self._last_ts = 0.0


_SINGLETON: Optional[PeerIdentityStats] = None
_LOCK = threading.Lock()


def get_peer_identity_stats() -> PeerIdentityStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PeerIdentityStats()
    return _SINGLETON


__all__ = ["PeerIdentityStats", "get_peer_identity_stats"]
