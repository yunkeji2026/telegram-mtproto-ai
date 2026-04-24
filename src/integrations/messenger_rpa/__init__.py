"""Facebook Messenger 个人 App RPA（ADB + Vision/OCR）。

设计要点（与 line_rpa 的差别）：
- Messenger UI 大量使用 Meta Bloks (React Native)，UI 树复杂、uiautomator dump 易 OOM；
  → 主路径走 **Vision + 截图坐标**，UI dump 仅作降级辅助
- 已登录单账户态稳定；多账户 Profile picker / 引导守卫屏会拒绝部分 input → 必须先手动选号
- 复用 line_rpa.adb_helpers（ADB/AdbKeyboard/screencap）和 vision_client
- 复用主进程 SkillManager / AIClient（共享人设、知识库、模板、reply_strategy）

模块概览：
- service.py        长期后台服务（start/stop/pause/trigger_once/status，自适应轮询）
- runner.py         单次 run_once：foreground → inbox_scan → enter_chat → read_peer → AI → reply
- state_store.py    SQLite：per-chat 去重、近期 run 历史、审批队列
- inbox_scanner.py  Vision 扫 Inbox 取未读会话列表
- chat_reader.py    Vision 扫会话页取对方最后一条
- bloks_navigator.py 处理 Note onboarding / Previews modal / Profile picker 等守卫屏
- coords.py         屏幕坐标常量（720×1600 标定 + 比例换算）
"""

from src.integrations.messenger_rpa.runner import MessengerRpaRunner

__all__ = ["MessengerRpaRunner"]
