"""网络绑定相关小工具（端口占用判断等）"""


def is_bind_address_in_use_error(exc: BaseException) -> bool:
    """Windows 10048 / Linux EADDRINUSE 等"""
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == 10048:
            return True
        if exc.errno in (98, 48, 10048):
            return True
    s = str(exc).lower()
    return (
        "10048" in s
        or "address already in use" in s
        or "只允许使用一次" in str(exc)
    )
