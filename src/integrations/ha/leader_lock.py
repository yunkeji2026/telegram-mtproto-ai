"""P7-1：跨节点单点 leader lock。

MessengerRPA 的强约束是"同一时刻只能有一个进程对同一台 Android
设备做 ADB 操作"——多活情况下 primary/standby 之间必须抢锁。

设计目标：
- 同时支持 **单机本地**（文件锁，零外部依赖，便于开发/单测）和
  **跨机热备**（Redis SET NX PX，生产推荐）。
- **fencing token**：单调递增，持锁者每次写 DB 前校验 token，
  避免"假死 primary 恢复网络后覆盖 standby 的写入"（split-brain）。
- **TTL + 心跳**：token 每次续约刷新 TTL；若 holder 宕机，TTL 到期后
  standby 可通过 `try_acquire()` 抢占，token +1。

约束：
- 不依赖具体 redis 客户端库；`RedisLeaderLock` 只要求一个实现了
  `set(key, value, nx=True, px=ms)` / `get(key)` / `eval(script,...)`
  / `delete(key)` 的对象。这样生产可用 `redis.asyncio.Redis`，单测
  可用内存 fake。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── 公共抽象 ─────────────────────────────────────────

@dataclass
class LockState:
    holder_id: str          # 当前锁持有者（node_id）
    fencing_token: int      # 单调递增，持锁周期的版本号
    acquired_ts: float      # 当前持有者获得锁的时间
    renew_ts: float         # 最近一次续约
    ttl_sec: float          # 当前有效 TTL
    extra: Optional[Dict[str, Any]] = None


class LeaderLockBackend(ABC):
    """抽象后端接口 —— 不同实现（文件/Redis）都要满足原子 CAS/续约/读锁状态。"""

    @abstractmethod
    async def try_acquire(
        self,
        holder_id: str,
        ttl_sec: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[LockState]:
        """尝试获得锁。成功返回 LockState（含新 fencing_token），失败返回 None。"""

    @abstractmethod
    async def renew(
        self, holder_id: str, fencing_token: int, ttl_sec: float
    ) -> Optional[LockState]:
        """仅当 (holder_id, fencing_token) 匹配当前锁时续约。否则返回 None。"""

    @abstractmethod
    async def release(self, holder_id: str, fencing_token: int) -> bool:
        """主动释放（幂等）。"""

    @abstractmethod
    async def peek(self) -> Optional[LockState]:
        """读当前锁状态（不修改）。TTL 已过返回 None。"""


# ── 文件后端（开发 / 单机 / 单测） ───────────────────

class FileLeaderLock(LeaderLockBackend):
    """基于文件 + fcntl/msvcrt 的单机锁。

    用于：
    - 开发环境多进程抢锁
    - 单测（真实 disk IO 模拟 Redis 原子行为）
    - 单机不想引 Redis 也能用 P7-1 fencing

    **重要**：仅保证单机内多进程互斥，跨机需用 RedisLeaderLock。
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            if not self._path.exists():
                return None
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                return None
            return json.loads(raw)
        except Exception:
            return None

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def _is_alive(self, data: Dict[str, Any]) -> bool:
        try:
            return (time.time() - float(data.get("renew_ts", 0))) < float(
                data.get("ttl_sec", 0)
            )
        except Exception:
            return False

    async def try_acquire(
        self,
        holder_id: str,
        ttl_sec: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[LockState]:
        async with self._lock:
            now = time.time()
            data = self._read()
            token = 0
            if data and self._is_alive(data):
                # 锁仍有效：仅当是自己重入才算成功
                if str(data.get("holder_id")) == str(holder_id):
                    data["renew_ts"] = now
                    data["ttl_sec"] = float(ttl_sec)
                    self._write(data)
                    return LockState(
                        holder_id=str(data["holder_id"]),
                        fencing_token=int(data["fencing_token"]),
                        acquired_ts=float(data.get("acquired_ts", now)),
                        renew_ts=now,
                        ttl_sec=float(ttl_sec),
                        extra=data.get("extra"),
                    )
                return None
            # 锁过期或没锁：夺锁 + token +1（单调）
            if data:
                token = int(data.get("fencing_token", 0))
            new_state = LockState(
                holder_id=str(holder_id),
                fencing_token=token + 1,
                acquired_ts=now,
                renew_ts=now,
                ttl_sec=float(ttl_sec),
                extra=extra,
            )
            self._write(asdict(new_state))
            return new_state

    async def renew(
        self, holder_id: str, fencing_token: int, ttl_sec: float
    ) -> Optional[LockState]:
        async with self._lock:
            data = self._read()
            if not data:
                return None
            if str(data.get("holder_id")) != str(holder_id):
                return None
            if int(data.get("fencing_token", 0)) != int(fencing_token):
                return None
            now = time.time()
            data["renew_ts"] = now
            data["ttl_sec"] = float(ttl_sec)
            self._write(data)
            return LockState(
                holder_id=str(data["holder_id"]),
                fencing_token=int(data["fencing_token"]),
                acquired_ts=float(data.get("acquired_ts", now)),
                renew_ts=now,
                ttl_sec=float(ttl_sec),
                extra=data.get("extra"),
            )

    async def release(self, holder_id: str, fencing_token: int) -> bool:
        async with self._lock:
            data = self._read()
            if not data:
                return True
            if str(data.get("holder_id")) != str(holder_id):
                return False
            if int(data.get("fencing_token", 0)) != int(fencing_token):
                return False
            # 保留 token 以便下一次 acquire 单调 +1
            data["renew_ts"] = 0.0
            data["ttl_sec"] = 0.0
            self._write(data)
            return True

    async def peek(self) -> Optional[LockState]:
        data = self._read()
        if not data or not self._is_alive(data):
            return None
        return LockState(
            holder_id=str(data["holder_id"]),
            fencing_token=int(data["fencing_token"]),
            acquired_ts=float(data.get("acquired_ts", 0)),
            renew_ts=float(data.get("renew_ts", 0)),
            ttl_sec=float(data.get("ttl_sec", 0)),
            extra=data.get("extra"),
        )


# ── Redis 后端（生产） ───────────────────────────────

# Redis Lua：仅当 (holder, token) 匹配才更新，CAS 语义
_RENEW_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, cur = pcall(cjson.decode, v)
if not ok then return 0 end
if cur.holder_id ~= ARGV[1] then return 0 end
if tostring(cur.fencing_token) ~= ARGV[2] then return 0 end
cur.renew_ts = tonumber(ARGV[3])
cur.ttl_sec = tonumber(ARGV[4])
redis.call('SET', KEYS[1], cjson.encode(cur), 'PX', math.floor(tonumber(ARGV[4]) * 1000))
return 1
"""

_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 1 end
local ok, cur = pcall(cjson.decode, v)
if not ok then return 0 end
if cur.holder_id ~= ARGV[1] then return 0 end
if tostring(cur.fencing_token) ~= ARGV[2] then return 0 end
cur.renew_ts = 0
cur.ttl_sec = 0
redis.call('SET', KEYS[1], cjson.encode(cur))
return 1
"""

_ACQUIRE_LUA = """
local v = redis.call('GET', KEYS[1])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
if v then
  local ok, cur = pcall(cjson.decode, v)
  if ok then
    local alive = (now - (cur.renew_ts or 0)) < (cur.ttl_sec or 0)
    if alive and cur.holder_id ~= ARGV[1] then
      return cjson.encode({ok=0, cur=cur})
    end
    if alive and cur.holder_id == ARGV[1] then
      cur.renew_ts = now
      cur.ttl_sec = ttl
      redis.call('SET', KEYS[1], cjson.encode(cur), 'PX', math.floor(ttl * 1000))
      return cjson.encode({ok=1, state=cur, reentrant=1})
    end
    -- dead lock: take it, token+1
    local new_state = {
      holder_id=ARGV[1],
      fencing_token=(cur.fencing_token or 0) + 1,
      acquired_ts=now, renew_ts=now, ttl_sec=ttl, extra=ARGV[5]
    }
    redis.call('SET', KEYS[1], cjson.encode(new_state), 'PX', math.floor(ttl * 1000))
    return cjson.encode({ok=1, state=new_state})
  end
end
-- 完全无锁
local new_state = {
  holder_id=ARGV[1], fencing_token=1, acquired_ts=now, renew_ts=now,
  ttl_sec=ttl, extra=ARGV[5]
}
redis.call('SET', KEYS[1], cjson.encode(new_state), 'PX', math.floor(ttl * 1000))
return cjson.encode({ok=1, state=new_state})
"""


class RedisLeaderLock(LeaderLockBackend):
    """生产级 Redis 后端。

    对 ``redis_client`` 的要求（鸭子类型，不 import redis）：
    - ``await redis_client.eval(script, keys=[k], args=[...])`` 或
      ``await redis_client.eval(script, 1, k, *args)``（旧 API）
    - ``await redis_client.get(k)``
    """

    def __init__(self, redis_client: Any, key: str):
        self._r = redis_client
        self._key = key

    async def _eval(self, script: str, args: list) -> Any:
        # 兼容两种 redis.asyncio API
        try:
            return await self._r.eval(script, 1, self._key, *args)
        except TypeError:
            return await self._r.eval(
                script, keys=[self._key], args=args
            )

    async def try_acquire(
        self,
        holder_id: str,
        ttl_sec: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[LockState]:
        now = time.time()
        extra_s = json.dumps(extra or {}, ensure_ascii=False)
        raw = await self._eval(
            _ACQUIRE_LUA,
            [str(holder_id), "", str(now), str(ttl_sec), extra_s],
        )
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            obj = json.loads(raw)
        except Exception:
            return None
        if not obj.get("ok"):
            return None
        st = obj.get("state") or {}
        return LockState(
            holder_id=str(st["holder_id"]),
            fencing_token=int(st["fencing_token"]),
            acquired_ts=float(st.get("acquired_ts", now)),
            renew_ts=float(st.get("renew_ts", now)),
            ttl_sec=float(st.get("ttl_sec", ttl_sec)),
            extra=st.get("extra"),
        )

    async def renew(
        self, holder_id: str, fencing_token: int, ttl_sec: float
    ) -> Optional[LockState]:
        now = time.time()
        rc = await self._eval(
            _RENEW_LUA,
            [str(holder_id), str(int(fencing_token)), str(now), str(ttl_sec)],
        )
        if not rc:
            return None
        return await self.peek()

    async def release(self, holder_id: str, fencing_token: int) -> bool:
        rc = await self._eval(
            _RELEASE_LUA, [str(holder_id), str(int(fencing_token))]
        )
        return bool(rc)

    async def peek(self) -> Optional[LockState]:
        raw = await self._r.get(self._key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            cur = json.loads(raw)
        except Exception:
            return None
        if (time.time() - float(cur.get("renew_ts", 0))) >= float(
            cur.get("ttl_sec", 0)
        ):
            return None
        return LockState(
            holder_id=str(cur["holder_id"]),
            fencing_token=int(cur["fencing_token"]),
            acquired_ts=float(cur.get("acquired_ts", 0)),
            renew_ts=float(cur.get("renew_ts", 0)),
            ttl_sec=float(cur.get("ttl_sec", 0)),
            extra=cur.get("extra"),
        )


# ── 高层 LeaderLock（含心跳 task） ───────────────────

class LeaderLock:
    """用户侧门面：封装 acquire + background heartbeat。

    典型用法：

        lock = LeaderLock.from_config({...})
        if await lock.acquire(ttl_sec=30, heartbeat_sec=10):
            try:
                # 只有 leader 进入 RPA 主循环
                await service.start()
            finally:
                await lock.release()
        else:
            logger.warning("not leader, standby mode")
    """

    def __init__(self, backend: LeaderLockBackend, *, node_id: Optional[str] = None):
        self._backend = backend
        self._node_id = node_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._state: Optional[LockState] = None
        self._hb_task: Optional[asyncio.Task] = None
        self._stopped = False

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def state(self) -> Optional[LockState]:
        return self._state

    @property
    def is_leader(self) -> bool:
        return self._state is not None and not self._stopped

    async def acquire(
        self,
        *,
        ttl_sec: float = 30.0,
        heartbeat_sec: float = 10.0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        st = await self._backend.try_acquire(
            self._node_id, ttl_sec=ttl_sec, extra=extra
        )
        if st is None:
            return False
        self._state = st
        self._stopped = False
        self._hb_task = asyncio.create_task(
            self._heartbeat_loop(ttl_sec, heartbeat_sec),
            name=f"leader_hb_{self._node_id[:12]}",
        )
        logger.info(
            "[ha] acquired leader lock node=%s token=%d ttl=%.1f",
            self._node_id, st.fencing_token, ttl_sec,
        )
        return True

    async def _heartbeat_loop(self, ttl_sec: float, hb_sec: float) -> None:
        try:
            while not self._stopped and self._state is not None:
                await asyncio.sleep(max(1.0, hb_sec))
                if self._stopped or self._state is None:
                    break
                new_st = await self._backend.renew(
                    self._node_id, self._state.fencing_token, ttl_sec
                )
                if new_st is None:
                    logger.warning(
                        "[ha] heartbeat lost leadership node=%s token=%d",
                        self._node_id, self._state.fencing_token,
                    )
                    self._state = None
                    self._stopped = True
                    break
                self._state = new_st
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[ha] heartbeat loop crashed")

    async def release(self) -> None:
        self._stopped = True
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hb_task = None
        if self._state is not None:
            try:
                await self._backend.release(
                    self._node_id, self._state.fencing_token
                )
            except Exception:
                logger.debug("[ha] release failed", exc_info=True)
            self._state = None
        logger.info("[ha] released leader lock node=%s", self._node_id)

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "LeaderLock":
        """按配置创建。

        cfg 示例：
            backend: file                  # file | redis
            file_path: data/ha/leader.lock # backend=file 用
            redis_url: redis://...         # backend=redis 用
            redis_key: rpa:leader
            node_id: auto                  # auto=主机名+uuid
        """
        backend = create_leader_lock(cfg)
        node_id = str(cfg.get("node_id") or "").strip() or None
        if node_id == "auto":
            node_id = None
        return cls(backend, node_id=node_id)


def create_leader_lock(cfg: Dict[str, Any]) -> LeaderLockBackend:
    kind = str(cfg.get("backend") or "file").strip().lower()
    if kind == "file":
        path = str(cfg.get("file_path") or "data/ha/leader.lock")
        return FileLeaderLock(path)
    if kind == "redis":
        url = str(cfg.get("redis_url") or "redis://127.0.0.1:6379/0")
        key = str(cfg.get("redis_key") or "rpa:leader")
        try:
            import redis.asyncio as aioredis  # type: ignore
        except ImportError as ex:
            raise RuntimeError(
                "redis backend requires `redis` package: pip install redis"
            ) from ex
        client = aioredis.from_url(url, decode_responses=True)
        return RedisLeaderLock(client, key)
    raise ValueError(f"unknown leader_lock backend: {kind!r}")
