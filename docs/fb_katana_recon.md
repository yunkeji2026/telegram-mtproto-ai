# Facebook 主 App (com.facebook.katana) 勘察报告

> 编制日期：2026-04-20  
> 设备：d113 (192.168.0.113:5555, 720×1600 物理像素)  
> 项目模块：`src/integrations/messenger_rpa/katana_coords.py`

---

## 1. 基本结论

| 维度 | 结论 |
|---|---|
| `uiautomator dump` | ❌ OOM，与 Messenger 同样不可用 |
| `input tap` | ✅ 全部测试控件都响应 |
| 全屏 vision OCR | ✅ glm-4v-flash 可读 |
| 顶部 Tabs Y 坐标 | ⚠️ **不稳定**：Home Feed 时 Y=203，Friends 页 Y=109 → 不能 hardcode |
| Friend Requests Confirm/Delete | ✅ 标定可命中（首行 343×781 / 578×781，行高 234） |
| Messenger 入口 | ✅ Home 页右上 (672, 109) 可点；会弹"Select app"（u0/u999 双 Messenger 实例） |
| PYMK | ✅ Home 页中下部，单卡 (175, 1300) |

---

## 2. 屏幕基线（720×1600 物理像素）

### 2.1 Home Feed 顶部
```
┌────────────────────────────────────────────┐  Y
│ [≡]  facebook         [+]  [🔍] [↻ Msg]   │  ~110
├────────────────────────────────────────────┤
│ [🏠] [📺] [👥] [🏪] [🔔] [👤]             │  ~203 (Tabs)
├────────────────────────────────────────────┤
│ 头像  What's on your mind?            📷  │  ~308
├────────────────────────────────────────────┤
│ Stories ──────────────────────             │  ~594
├────────────────────────────────────────────┤
│ ...feed...                                 │  ~770+
├────────────────────────────────────────────┤
│ People you may know   ···  X              │  ~1010
│ ┌──────────┐ ┌──────────┐                  │
│ │  card 0  │ │  card 1  │   ←横向滚动      │  ~1300
│ │   Ynez   │ │  Bibi    │                 │  ~1500
│ └──────────┘ └──────────┘                  │
└────────────────────────────────────────────┘
```

### 2.2 Friends 页（点 Friends tab）
```
┌────────────────────────────────────────────┐  Y
│ [🏠] [📺] [👥] [🏪] [🔔] [👤]             │  ~109 (Tabs 上移!)
├────────────────────────────────────────────┤
│ [≡]  Friends                          🔍  │  ~203
├────────────────────────────────────────────┤
│ [🟢 32 online] [Suggestions] [Your fri...]│  ~281
├────────────────────────────────────────────┤
│  Jay Son and Lee Héros Katao               │  ~390
│  accepted your friend requests.            │
├────────────────────────────────────────────┤
│  Friend requests  9            See all     │  ~562
├────────────────────────────────────────────┤
│  ◉ Jean Canque                             │  ~660
│    1 mutual friend · 10w                   │
│    [   Confirm   ] [   Delete   ]          │  ~781
├────────────────────────────────────────────┤
│  ◉ ...                                     │  ~894
│    [   Confirm   ] [   Delete   ]          │  ~1015
└────────────────────────────────────────────┘
```

---

## 3. 已标定坐标（katana_coords.py）

```python
# 顶部导航（Home 页）
NF_HAMBURGER = (50, 110)
NF_LOGO = (160, 110)
NF_NEW_POST_BTN = (530, 110)
NF_SEARCH = (605, 110)
NF_MESSENGER_BTN = (671, 110)         # Messenger ↻ 入口

# Tabs（Home 页 Y=203，Friends 页 Y=109）
TAB_HOME, TAB_REELS, TAB_FRIENDS = (58, 203), (176, 203), (300, 203)
TAB_MARKETPLACE = (415, 203)
TAB_NOTIFICATIONS = (543, 203)
TAB_PROFILE = (664, 203)

# Stories 行
STORY_ROW_Y = 594, gap=175

# PYMK
PYMK_FIRST_CARD_CENTER = (175, 1300)
pymk_card(i) -> X = 175 + i*285

# Friends 页 Friend Requests
friend_request_confirm(i) -> (343, 781 + i*234)
friend_request_delete(i)  -> (578, 781 + i*234)
```

---

## 4. 关键洞察

### 4.1 Multi-User Messenger 选择器
点 Home 页右上 Messenger 入口会弹 **"Select app"** modal，列出所有可用的 Messenger 实例。
d113 上发现 **2 个 Messenger 实例**：
- 默认 Messenger（u0 user）
- 带橙色小图标的 Messenger（u999 XSpace user）

→ 这意味着 Katana 是**多账号统一入口**，但跳转到 Messenger 后会切到对应 user。

### 4.2 Tabs 浮动
Tabs 不是 absolute positioning，会随当前页滚动状态/页面 type 上下移。**所以 Tab 切换必须先 BACK 回到稳定的 Home Feed**，不能跨页 hardcode。

### 4.3 Friend Requests 是高 ROI 自动化场景
- 9 条挂着，每条都有 Confirm/Delete 大按钮
- 行高稳定 234 物理像素
- 名字 + 共同好友数可 vision 识别 → 可做"自动加好友过滤策略"

---

## 5. 下一阶段勘察清单（pending）

- [ ] 评论区进入路径（点帖子 → Comments）+ 评论输入框/发送按钮坐标
- [ ] 帖子详情页 like / share / save 按钮坐标
- [ ] Notifications 页布局 + 点击通知跳转
- [ ] Profile 页结构（自己 + 别人的）
- [ ] Stories 全屏播放页的 reply / reaction 入口
- [ ] Marketplace / Reels（如要做电商场景）

---

## 6. 推荐优先级（运维落地）

| 场景 | 优先级 | 难度 | 价值 |
|---|---|---|---|
| Friend Requests 自动接受+过滤 | ⭐⭐⭐ | 低 | 维护账号活跃度 |
| 评论自动回复 | ⭐⭐⭐ | 中 | 内容运营 |
| Story 反应自动回复 | ⭐⭐ | 中 | 私聊延伸 |
| PYMK 自动加好友 | ⭐ | 低 | 风险高（被风控） |
| 自动发帖/点赞 | ❌ 不推荐 | 中 | 严重违反 ToS |

**第一个落地建议**：基于已标定坐标的 `accept_friend_requests()` RPA 函数 — 只接受**有 1+ mutual friend** 的请求，其他默认忽略。
