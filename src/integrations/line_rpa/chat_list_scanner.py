"""从 LINE 聊天列表 XML 中识别"未读会话行"。

支持两类未读：
  (1) 数字徽章："3" / "12" / "99+" 等 TextView（主流 LINE 版本的常见表现）
  (2) 纯红点徽章（无数字）：通过截图像素检测行右侧红色比例（P2-2，可选）
  (3) P6-B1: vision 驱动的回退（uiautomator OOM 时）

识别流程：
  1) 第一次遍历：收集所有"姓名候选"（姓名 TextView）与"数字徽章候选"（TextView
     纯数字小尺寸 或 resource-id 含 unread）。
  2) 数字徽章行：同 y 带内挑最左 name 作为对方名。
  3) 若提供 png 且开启 red_dot_fallback：对已识别姓名行但无数字徽章者，
     检查行右侧条带的红像素比；超阈值则计入未读（标 count=1, red_dot=True）。
  4) 按 tap_y 从上到下返回（LINE 列表顶端即最新会话）。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"^(\d{1,2}[:：]\d{1,2}([:：]\d{1,2})?)$|"  # 10:24 / 10:24:03
    r"^(上午|下午|AM|PM)\s*\d{1,2}[:：]\d{1,2}$|"
    r"^(\d+)(分|小時|小时|天|週|周)前$|"
    r"^(昨天|yesterday)$",
    flags=re.IGNORECASE,
)

_NAME_SKIP_PREFIX = (
    "CHATS",
    "VOOM",
    "LINE",
    "Wallet",
    "Home",
    "Friends",
    "聊天",
    "通話",
    "通话",
    "貼文",
    "贴文",
    "好友",
    "錢包",
    "钱包",
    "主頁",
    "主页",
)


def _parse_bounds(b: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (b or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


@dataclass
class UnreadRow:
    name: str
    unread_count: int           # 未读数（若看不到具体数字则为 1）
    tap_x: int
    tap_y: int
    bounds: Tuple[int, int, int, int]  # 整行估算 bounds（左, 上, 右, 下）
    badge_bounds: Tuple[int, int, int, int]
    name_rid: str
    badge_rid: str
    source: str = "digit"       # "digit" | "red_dot" | "vision"
    preview: str = ""           # P6-B1: 消息预览（vision 扫描时填充；XML 路径为空）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unread_count": self.unread_count,
            "tap_x": self.tap_x,
            "tap_y": self.tap_y,
            "bounds": list(self.bounds),
            "badge_bounds": list(self.badge_bounds),
            "source": self.source,
            "preview": self.preview,
        }


# ── P6-B1: vision 驱动的聊天列表扫描 ────────────────────────────────────────

LIST_VISION_PROMPT = (
    "你在分析一张 LINE 手机的【聊天列表】页截图（不是聊天详情页）。\n"
    "典型布局：顶部可能有搜索框；往下是一行行聊天条目，每条从左到右依次是：\n"
    "  圆形头像 | 昵称 + 消息预览文字 | 时间戳 + 可能的红底白字未读数角标\n"
    "\n"
    "判断规则：\n"
    "- 红底白字圆形角标里的数字 → unread 字段填该数字\n"
    "- 绿色『N』角标（LINE 新功能提示）→ unread=0\n"
    "- LINE 官方账号（昵称是『LINE』或含『LINE』或 Keep 备忘录）→ is_system=true\n"
    "- 没有任何角标的行 → unread=0\n"
    "\n"
    "从上到下列出当前可见的**所有聊天行**，严格按下列 JSON 数组输出\n"
    "（只输出 JSON，不要 markdown，不要解释）：\n"
    '[{"name":"...","preview":"...","time":"...","unread":0,"is_system":false}]\n'
    "字段说明：\n"
    " - name：对方昵称或群名（原样输出）\n"
    " - preview：最后一条消息预览（原样，可含 'XX 发送了贴图/图片' 等）\n"
    " - time：右侧时间（'上午10:17' 等）\n"
    " - unread：数字角标里的数；无角标填 0\n"
    " - is_system：LINE 官方/系统账号填 true，否则 false\n"
    "整屏无聊天行时输出 []。"
)


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    return s


def _crop_list_area(png_bytes: bytes, *, top_ratio: float = 0.11, bottom_ratio: float = 0.90) -> bytes:
    """裁去顶部标题/搜索框和底部 tab 栏，保留中间列表区域。"""
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        return png_bytes
    try:
        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        W, H = img.size
        y0 = int(H * top_ratio)
        y1 = int(H * bottom_ratio)
        buf = _io.BytesIO()
        img.crop((0, y0, W, y1)).save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return png_bytes


def parse_unread_rows_vision(
    png_bytes: bytes,
    *,
    vision_cfg: Dict[str, Any],
    global_vision_cfg: Dict[str, Any],
    max_rows: int = 10,
    screen_w: int = 720,
    screen_h: int = 1600,
    prompt_override: str = "",
) -> Tuple[List[UnreadRow], str]:
    """P6-B1：用 vision 模型解析聊天列表截图，返回 (rows, debug)。

    - `vision_cfg`：line_rpa.vision_read_fallback 或 vision_scan 段（必须含 api_key 或 enabled）
    - `global_vision_cfg`：全局 vision 段（含 api_key / model）
    - `screen_w/h`：设备分辨率（用于估算 tap 坐标）

    返回 UnreadRow 列表，未读数 > 0 且非系统账号的排在最前。
    """
    import base64
    import requests as _req

    merged: Dict[str, Any] = {**global_vision_cfg, **vision_cfg}
    api_key: str = str(merged.get("api_key") or "")
    model: str = str(merged.get("model") or "glm-4v-flash")
    if not api_key:
        return [], "vision_scan:no_api_key"

    cropped = _crop_list_area(png_bytes)
    b64 = base64.b64encode(cropped).decode("ascii")
    prompt = prompt_override or LIST_VISION_PROMPT
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "temperature": 0.1,
    }
    try:
        r = _req.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body, timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        return [], f"vision_scan:api_error:{e}"

    raw = _strip_code_fence(raw)
    try:
        items: List[dict] = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("not list")
    except Exception:
        return [], f"vision_scan:parse_error:{raw[:80]}"

    # 估算每行的 tap_y（等高切割：top_ratio + 每行占 ~ row_h 像素）
    list_top_ratio = 0.11
    list_bottom_ratio = 0.90
    list_h = int(screen_h * (list_bottom_ratio - list_top_ratio))
    n_rows = max(1, len(items))
    row_h = list_h // n_rows
    list_top_px = int(screen_h * list_top_ratio)

    rows: List[UnreadRow] = []
    for idx, item in enumerate(items[:max_rows]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        preview = str(item.get("preview") or "")
        time_str = str(item.get("time") or "")
        unread = int(item.get("unread") or 0)
        is_sys = bool(item.get("is_system"))

        # 估 tap_y：以行中心为准
        tap_y = list_top_px + idx * row_h + row_h // 2
        tap_x = screen_w // 2
        row_top = list_top_px + idx * row_h
        row_bot = row_top + row_h

        rows.append(UnreadRow(
            name=name,
            unread_count=unread,
            tap_x=tap_x,
            tap_y=tap_y,
            bounds=(0, row_top, screen_w, row_bot),
            badge_bounds=(0, 0, 0, 0),
            name_rid="",
            badge_rid=f"vision:{unread}",
            source="vision",
            preview=preview,
        ))

    # 系统账号 unread 视为 0（不处理）
    for row in rows:
        if any(k in row.name for k in ("LINE", "Keep")):
            row.unread_count = 0

    rows.sort(key=lambda r: r.tap_y)
    unread_cnt = sum(1 for r in rows if r.unread_count > 0)
    return rows, f"vision_scan ok rows={len(rows)} unread={unread_cnt}"


def _is_digit_badge_text(text: str) -> Optional[int]:
    """返回解析出的未读数（1..999）；否则 None。"""
    s = (text or "").strip()
    if not s:
        return None
    # LINE 超过 99 时会显示 "99+"
    if s in {"99+", "999+", "N"}:
        return 99
    if s.isdigit() and 1 <= len(s) <= 3:
        try:
            n = int(s)
            if 0 < n < 1000:
                return n
        except ValueError:
            return None
    return None


def _looks_like_name_text(s: str) -> bool:
    if not s:
        return False
    if len(s) > 40:
        return False
    if _TIME_RE.match(s):
        return False
    if _is_digit_badge_text(s) is not None:
        return False
    if any(s.startswith(p) for p in _NAME_SKIP_PREFIX) and len(s) <= 12:
        return False
    # 过滤只含标点/emoji 的
    if not re.search(r"[\w\u3040-\u30ff\u3400-\u9fff]", s):
        return False
    return True


def _is_red_pixel(r: int, g: int, b: int) -> bool:
    """粗粒度判定：LINE 未读红点常见近 (247, 0, 66)；容差放宽。"""
    return r >= 200 and g <= 110 and b <= 140 and (r - max(g, b)) >= 60


def red_ratio_in_box(
    png_bytes: bytes,
    box: Tuple[int, int, int, int],
) -> float:
    """对 PNG 的给定区域统计红像素占比。失败返回 0.0。"""
    if not png_bytes:
        return 0.0
    try:
        from PIL import Image
        import io as _io
    except Exception:
        return 0.0
    try:
        im = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return 0.0
    l, t, r, b = box
    iw, ih = im.size
    l = max(0, min(iw - 1, int(l)))
    r = max(l + 1, min(iw, int(r)))
    t = max(0, min(ih - 1, int(t)))
    b = max(t + 1, min(ih, int(b)))
    try:
        crop = im.crop((l, t, r, b))
    except Exception:
        return 0.0
    # 降采样加速（行右侧条带通常 < 200x200，够精细）
    if crop.size[0] * crop.size[1] > 20000:
        crop = crop.resize((
            max(10, crop.size[0] // 2),
            max(10, crop.size[1] // 2),
        ))
    pixels = list(crop.getdata())
    if not pixels:
        return 0.0
    hits = sum(1 for (pr, pg, pb) in pixels if _is_red_pixel(pr, pg, pb))
    return hits / float(len(pixels))


def _detect_red_dot_rows(
    name_nodes: list,
    used_y: List[int],
    min_row_gap_px: int,
    png_bytes: bytes,
    *,
    right_strip_ratio: float,
    min_red_ratio: float,
    approx_row_h: int,
    max_rows: int,
    screen_w: int = 1080,
) -> List[UnreadRow]:
    """对"有姓名但没数字徽章"的行，在截图里查右侧红像素比；超阈值视为未读。"""
    out: List[UnreadRow] = []
    if not png_bytes:
        return out
    # 先聚类姓名行：相近 y 的姓名按最靠左那条代表
    sorted_names = sorted(name_nodes, key=lambda n: n["cy"])
    clusters: List[dict] = []
    for n in sorted_names:
        if clusters and abs(n["cy"] - clusters[-1]["cy"]) < min_row_gap_px:
            prev = clusters[-1]
            if n["cx"] < prev["cx"]:  # 留最靠左
                clusters[-1] = n
            continue
        clusters.append(n)

    # 行右边界用屏幕宽度（TextView 姓名很少延伸到全宽），这样红点区域才能覆盖到行尾
    row_right_default = max(screen_w, (
        max(n["bounds"][2] for n in name_nodes) if name_nodes else 1080
    ))
    strip = max(0.08, min(0.5, float(right_strip_ratio)))
    thresh = max(0.005, float(min_red_ratio))

    for n in clusters:
        cy = n["cy"]
        if any(abs(cy - y) < min_row_gap_px for y in used_y):
            continue  # 已被数字徽章行占据
        # 行 bounds：用 approx_row_h 估计；左 = name.left, 右 = 尽量到屏幕最右
        row_top = max(0, cy - approx_row_h // 2)
        row_bot = cy + approx_row_h // 2
        row_left = n["bounds"][0]
        row_right = row_right_default
        # 右侧条带
        strip_left = int(row_right - (row_right - row_left) * strip)
        box = (strip_left, row_top, row_right, row_bot)
        ratio = red_ratio_in_box(png_bytes, box)
        if ratio >= thresh:
            out.append(UnreadRow(
                name=str(n["text"]),
                unread_count=1,
                tap_x=(row_left + row_right) // 2,
                tap_y=cy,
                bounds=(row_left, row_top, row_right, row_bot),
                badge_bounds=box,
                name_rid=str(n["rid"])[:48],
                badge_rid=f"red_dot:{ratio:.3f}",
                source="red_dot",
            ))
            used_y.append(cy)
            if len(out) >= max_rows:
                break
    return out


def parse_unread_rows(
    xml_bytes: bytes,
    *,
    max_rows: int = 10,
    min_row_gap_px: int = 40,
    png_bytes: Optional[bytes] = None,
    red_dot_cfg: Optional[dict] = None,
) -> Tuple[List[UnreadRow], str]:
    """解析聊天列表 XML → 未读行列表。

    返回 (rows, debug)。当提供 `png_bytes` 且 `red_dot_cfg.enabled=True` 时，
    对没有数字徽章的姓名行追加红点像素兜底识别。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return [], f"xml_parse_error:{e}"

    # 估屏高/屏宽：忽略顶部 1/12 的时间/状态栏，避开把"9:41"当成"9 条未读"
    screen_h = 1920
    screen_w = 1080
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if bb and bb[0] == 0 and bb[1] == 0 and bb[2] > 0 and bb[3] > 0:
            screen_h = max(screen_h, bb[3])
            screen_w = max(screen_w, bb[2])
            break
    top_exclude_y = max(80, screen_h // 12)
    bottom_exclude_y = screen_h - max(80, screen_h // 14)

    # ── 阶段 1：收集徽章 ───────────────────────────────
    badges: List[dict] = []
    name_nodes: List[dict] = []
    for el in root.iter():
        text = (el.get("text") or "").strip()
        rid = (el.get("resource-id") or "").lower()
        cls = (el.get("class") or "")
        if "TextView" not in cls:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if b <= top_exclude_y or t >= bottom_exclude_y:
            continue
        w = r - l
        h = b - t
        cx = (l + r) // 2
        cy = (t + b) // 2

        # 徽章：resource-id 含 unread & 数字 / 或纯数字 + 小尺寸
        n = _is_digit_badge_text(text)
        if n is not None:
            is_unread_rid = "unread" in rid
            if is_unread_rid or (w <= 140 and h <= 140):
                badges.append({
                    "rid": rid,
                    "n": n,
                    "bounds": (l, t, r, b),
                    "cx": cx,
                    "cy": cy,
                })
                continue  # 徽章节点不再作为 name 候选

        # 姓名候选：较长文字，非时间，非数字徽章
        if _looks_like_name_text(text):
            name_nodes.append({
                "text": text,
                "rid": rid,
                "bounds": (l, t, r, b),
                "cx": cx,
                "cy": cy,
                "w": w,
            })

    fallback_enabled = bool(
        png_bytes and isinstance(red_dot_cfg, dict) and red_dot_cfg.get("enabled")
    )
    if not name_nodes:
        # 既没徽章也没姓名（且红点兜底也找不到落点）
        if not badges and not fallback_enabled:
            return [], "no_unread_badges_and_no_names"
        return [], f"badges={len(badges)} but no_name_nodes"
    if not badges and not fallback_enabled:
        return [], "no_unread_badges"

    # 估计行高：用 badges 的高度平均值兜底；没徽章时退回按姓名高度估
    if badges:
        approx_row_h = int(
            sum(b["bounds"][3] - b["bounds"][1] for b in badges) / len(badges)
        )
        approx_row_h = max(80, min(220, approx_row_h * 2))
    else:
        approx_row_h = int(
            sum(n["bounds"][3] - n["bounds"][1] for n in name_nodes)
            / max(1, len(name_nodes))
        )
        approx_row_h = max(80, min(220, int(approx_row_h * 1.5)))

    # ── 阶段 2：为每个徽章找同行最左 name ─────────────
    seen_y: List[int] = []
    rows: List[UnreadRow] = []
    for bd in sorted(badges, key=lambda x: x["cy"]):
        bcy = bd["cy"]
        band_lo = bcy - approx_row_h // 2
        band_hi = bcy + approx_row_h // 2

        # 与已接受行去重（避免同一行出现多个徽章误算成两行）
        if any(abs(bcy - y) < min_row_gap_px for y in seen_y):
            continue

        candidates = [
            n
            for n in name_nodes
            if band_lo <= n["cy"] <= band_hi
        ]
        if not candidates:
            continue
        # 取最靠左的（排除已知就在右侧的时间）
        candidates.sort(key=lambda x: (x["cx"], -x["w"]))
        name = candidates[0]

        # 行整体 bounds = min(left) .. max(right) within band
        row_left = min(c["bounds"][0] for c in candidates + [bd])
        row_right = max(c["bounds"][2] for c in candidates + [bd])
        row_top = min(c["bounds"][1] for c in candidates + [bd])
        row_bot = max(c["bounds"][3] for c in candidates + [bd])
        tap_x = (row_left + row_right) // 2
        tap_y = name["cy"]

        rows.append(UnreadRow(
            name=name["text"],
            unread_count=int(bd["n"]),
            tap_x=int(tap_x),
            tap_y=int(tap_y),
            bounds=(row_left, row_top, row_right, row_bot),
            badge_bounds=bd["bounds"],
            name_rid=str(name["rid"])[:48],
            badge_rid=str(bd["rid"])[:48],
        ))
        seen_y.append(bcy)
        if len(rows) >= max_rows:
            break

    # ── 阶段 3（可选）：红点兜底 ───────────────────────
    red_cnt = 0
    if fallback_enabled:
        red_rows = _detect_red_dot_rows(
            name_nodes,
            used_y=seen_y,
            min_row_gap_px=min_row_gap_px,
            png_bytes=png_bytes,
            right_strip_ratio=float(red_dot_cfg.get("right_strip_ratio", 0.2) or 0.2),
            min_red_ratio=float(red_dot_cfg.get("min_red_ratio", 0.06) or 0.06),
            approx_row_h=approx_row_h,
            max_rows=max_rows - len(rows),
            screen_w=int(screen_w),
        )
        red_cnt = len(red_rows)
        rows.extend(red_rows)

    rows.sort(key=lambda r: r.tap_y)
    return rows, (
        f"ok rows={len(rows)} badges={len(badges)} names={len(name_nodes)}"
        f" red_dot={red_cnt}"
    )


def find_chat_row_by_name(
    xml_bytes: bytes,
    name: str,
    *,
    approx_row_h: int = 160,
) -> Optional[UnreadRow]:
    """P4-3：在聊天列表 XML 中按 name 精确/前缀匹配查找任一行（不要求未读）。

    返回一条类 UnreadRow（`unread_count=0, source="name_match"`）用于已审批回复的精确落点。
    未匹配返回 None。
    """
    target = (name or "").strip()
    if not target:
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    screen_w = 1080
    screen_h = 1920
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if bb and bb[0] == 0 and bb[1] == 0 and bb[2] > 0 and bb[3] > 0:
            screen_w = max(screen_w, bb[2])
            screen_h = max(screen_h, bb[3])
            break

    top_exclude_y = max(80, screen_h // 12)
    bot_exclude_y = screen_h - max(80, screen_h // 14)

    def _score(text: str) -> int:
        t = text.strip()
        if t == target:
            return 100
        if t.startswith(target):
            return 70
        if target in t:
            return 40
        return 0

    best = None
    best_score = 0
    for el in root.iter():
        cls = el.get("class") or ""
        if "TextView" not in cls:
            continue
        text = (el.get("text") or "").strip()
        if not text:
            continue
        sc = _score(text)
        if sc == 0:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        if b <= top_exclude_y or t >= bot_exclude_y:
            continue
        if sc > best_score:
            best_score = sc
            best = (text, l, t, r, b, el.get("resource-id") or "")

    if best is None:
        return None
    text, l, t, r, b, rid = best
    cy = (t + b) // 2
    row_top = max(0, cy - approx_row_h // 2)
    row_bot = cy + approx_row_h // 2
    return UnreadRow(
        name=text,
        unread_count=0,
        tap_x=screen_w // 2,
        tap_y=cy,
        bounds=(0, row_top, screen_w, row_bot),
        badge_bounds=(0, 0, 0, 0),
        name_rid=str(rid)[:48],
        badge_rid="",
        source="name_match",
    )
