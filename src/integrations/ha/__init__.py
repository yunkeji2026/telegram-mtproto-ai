"""High-availability / multi-region utilities.

P7-1：
- leader_lock：跨节点单点写入锁（文件后端 + redis 后端）
- failover：热备节点提升 / 回收流程
"""

from .leader_lock import (  # noqa: F401
    LeaderLock,
    LeaderLockBackend,
    FileLeaderLock,
    RedisLeaderLock,
    create_leader_lock,
)
