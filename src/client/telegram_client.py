"""
Telegram MTProto客户端
基于pyrogram库的Telegram用户客户端实现
"""

import asyncio
import logging
import os
import random
import re
import time
from html import escape as _html_escape
from pathlib import Path
from collections import OrderedDict
from typing import Dict, Any, Optional, List, Tuple
# 语音识别导入
try:
    from src.voice_transcriber import VoiceTranscriberFactory
    VOICE_RECOGNITION_AVAILABLE = True
except ImportError:
    VOICE_RECOGNITION_AVAILABLE = False
    VoiceTranscriberFactory = None

# 图片识别导入
try:
    from src.image_recognizer import ImageRecognizerFactory
    IMAGE_RECOGNITION_AVAILABLE = True
except ImportError:
    IMAGE_RECOGNITION_AVAILABLE = False
    ImageRecognizerFactory = None

# 图像理解（Vision）导入 - 智谱 GLM-4V 为主、OCR 兜底
try:
    from src.vision_client import VisionClient
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False
    VisionClient = None

# 尝试导入pyrogram，如果失败则使用模拟版本
try:
    from pyrogram import Client, filters
    from pyrogram.enums import ParseMode
    from pyrogram.types import Message, User
    from pyrogram.errors import (
        SessionPasswordNeeded, PhoneCodeInvalid,
        PhoneCodeExpired, FloodWait, Unauthorized
    )
    PYROGRAM_AVAILABLE = True
except ImportError:
    PYROGRAM_AVAILABLE = False
    ParseMode = None  # type: ignore
    # 无 pyrogram（如 CI requirements-ci）时也保留模块级占位，确保 import-safe 且可被测试 patch
    Client = None  # type: ignore
    filters = None  # type: ignore
    # 创建模拟类型以便代码可以运行
    class Message:
        """模拟Message类"""
        def __init__(self):
            self.text = ""
            self.from_user = None
            self.chat = None
            self.id = 0
    
    class User:
        """模拟User类"""
        def __init__(self):
            self.id = 0
            self.username = ""
            self.first_name = ""

from src.utils.logger import LoggerMixin
from src.skills.skill_manager import SkillManager
from src.client.trigger import TelegramTriggerMixin
from src.client.sender import TelegramSenderMixin

# 上下文管理和情绪增强导入
try:
    from src.context.context_manager import ContextManager
    from src.skills.emotion_enhancer import EmotionEnhancer
    CONTEXT_AND_EMOTION_AVAILABLE = True
except ImportError:
    CONTEXT_AND_EMOTION_AVAILABLE = False
    ContextManager = None
    EmotionEnhancer = None

# 四层触发决策器导入
try:
    from src.trigger.four_layer_trigger import FourLayerTrigger
    FOUR_LAYER_TRIGGER_AVAILABLE = True
except ImportError:
    FOUR_LAYER_TRIGGER_AVAILABLE = False
    FourLayerTrigger = None

def _metrics():
    try:
        from src.monitoring.metrics_store import get_metrics_store
        return get_metrics_store()
    except Exception:
        return None


def _normalize_message_text(raw: Any) -> str:
    """P2 编码防护：将消息文本统一为可安全处理的 str，避免 utf-16-le 等解码异常导致整次处理失败。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    s = str(raw)
    try:
        # 去除 surrogate 与非法码点，避免下游编码报错
        return s.encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        return "".join(c if ord(c) < 0x10000 else "\ufffd" for c in s)


class TelegramClient(TelegramTriggerMixin, TelegramSenderMixin, LoggerMixin):
    """Telegram MTProto客户端"""
    
    def __init__(self, config, skill_manager: SkillManager, ai_client=None,
                 account_cfg: Optional[Dict[str, Any]] = None):
        """
        初始化Telegram客户端
        
        Args:
            config: 配置管理器实例
            skill_manager: Skill管理器实例
            ai_client: AI 客户端（可选），用于「前一条+当前消息」上下文判断是否回复
            account_cfg: 多账号覆盖字典（可选）；包含 api_id/api_hash/phone_number/
                         session_name/account_id/persona_ids 等，优先于 config 中的值
        """
        self.config = config
        self.skill_manager = skill_manager
        self.ai_client = ai_client
        self.client: Optional[Client] = None
        self.running = False
        _tg_cfg = config.get('telegram', {}) if hasattr(config, 'get') else {}
        _q_size = int(_tg_cfg.get('message_queue_size', 200) or 200)
        self.message_queue = asyncio.Queue(maxsize=_q_size)
        self._max_concurrent = int(_tg_cfg.get('max_workers', 10) or 10)
        self._process_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._active_tasks: int = 0
        self.user_info: Optional[User] = None
        self._session_reply_ts: Dict[str, float] = {}  # (chat_id:user_id) -> 我们最后回复该用户的时间戳
        self._last_send_wallclock: float = 0.0  # 全局上次 send_message 时间，用于 min_interval
        self._boot_timestamp: float = time.time()  # 启动时间戳，用于跳过启动前的旧消息
        self._processed_msg_ids: OrderedDict = OrderedDict()  # message_id -> timestamp, LRU 去重
        self._dedup_max_size: int = 2000
        self._dedup_ttl: float = 600.0
        self._gxp_pending: Dict[int, list] = {}  # chat_id -> [{cmd, ts, user_id, user_msg_id}, ...]

        from src.utils.i18n import I18n
        from src.utils.event_tracker import EventTracker
        cfg_dir = Path(config.config_path).parent if hasattr(config, "config_path") else Path("config")
        self._cfg_dir = cfg_dir
        self.i18n = I18n(db_path=cfg_dir / "bot.db")
        self.event_tracker = EventTracker(db_path=cfg_dir / "bot.db")
        from src.utils.multi_bot import BotRouter
        self._bot_router = BotRouter(config.config if hasattr(config, 'config') else {})
        from src.utils.rate_limiter import RateLimiter
        self._rate_limiter = RateLimiter(config.config if hasattr(config, 'config') else {})

        # 从配置获取Telegram设置（account_cfg overlay 优先）
        _ov: Dict[str, Any] = dict(account_cfg or {})
        telegram_config = config.get_telegram_config()
        self.api_id = _ov.get('api_id') or telegram_config.get('api_id')
        self.api_hash = _ov.get('api_hash') or telegram_config.get('api_hash')
        self.phone_number = _ov.get('phone_number') or telegram_config.get('phone_number')
        self.session_name = (
            _ov.get('session_name') or telegram_config.get('session_name', 'camille_bot')
        )
        # 多账号元信息（供日志/persona 路由使用）
        self.account_id: str = str(_ov.get('account_id') or 'default')
        self.account_label: str = str(_ov.get('account_label') or self.account_id)
        self.account_persona_ids: List[str] = list(_ov.get('persona_ids') or [])
        # N 线 核心2：每号独立代理（反封号命门）。proxy_id 指向 proxy_pool 条目；
        # 与 B 线协议 worker 复用同一份 proxy_pool + _to_pyrogram_proxy，不另造代理逻辑。
        self.proxy_id: str = str(
            _ov.get('proxy_id') or telegram_config.get('proxy_id') or ''
        ).strip()
        # N 线 核心4（统一运行时）：session_string 直接喂已授权 session（扫码/手机登录产物），
        # 让协议号无需 phone 即可拉起 A 线"有灵魂"client。空则回落 session 文件 / phone 登录。
        self.session_string: str = str(_ov.get('session_string') or '').strip()
        # N 线 N4b（入站镜像）：开启后把本号收/发的消息镜像进统一收件箱（坐席台可见）。
        # 默认关 → standalone main.py 行为不变；companion worker 拉起协议号时置 True。
        self._mirror_inbox: bool = bool(_ov.get('mirror_inbox', False))
        
        # 初始化语音转录服务
        self.voice_transcriber = None
        voice_config = self.config.get('voice_recognition', {})
        
        # 临时目录设置
        self.temp_dir = Path(voice_config.get('temp_dir', './temp/voice'))
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_size = voice_config.get('max_file_size', 16777216)  # 16MB
        
        if voice_config.get('enabled', False) and VOICE_RECOGNITION_AVAILABLE:
            try:
                self.voice_transcriber = VoiceTranscriberFactory.create_transcriber(voice_config)
                self.logger.info("语音转录服务初始化成功")
            except Exception as e:
                self.logger.warning(f"语音转录服务初始化失败: {e}")
        
        # 初始化图片识别服务
        self.image_recognizer = None
        image_config = self.config.get('image_recognition', {})
        
        if image_config.get('enabled', False) and IMAGE_RECOGNITION_AVAILABLE:
            try:
                self.image_recognizer = ImageRecognizerFactory.create_recognizer(image_config)
                self.logger.info("图片识别服务初始化成功")
            except Exception as e:
                self.logger.warning(f"图片识别服务初始化失败: {e}")
        
        # 图像理解（Vision）：请求时 Ollama→智谱链（见 VisionClient.describe_image_with_ollama_zhipu_fallback）
        self.vision_client = None
        vision_config = self.config.get('vision', {})
        if vision_config.get('enabled', False) and VISION_AVAILABLE and VisionClient:
            try:
                from src.vision_client import has_any_vision_backend
                if has_any_vision_backend(vision_config, vision_config):
                    self.logger.info(
                        "图像理解（Vision）已启用：优先本地 Ollama，不可用时使用智谱"
                    )
                else:
                    self.logger.warning(
                        "vision.enabled=true 但未配置可用的 Ollama(base_url) 或智谱(api_key/zhipu_api_key)"
                    )
            except Exception as e:
                self.logger.warning(f"Vision 配置检查失败: {e}")
        
        # 初始化上下文管理器和情绪增强器
        self.context_manager = None
        self.emotion_enhancer = None
        context_config = self.config.get('context', {})
        emoticons_config = self.config.get('emoticons', {})
        
        # 检查上下文功能是否启用
        if context_config.get('enabled', False) and CONTEXT_AND_EMOTION_AVAILABLE:
            try:
                # 初始化上下文管理器
                self.context_manager = ContextManager(config=context_config)
                self.logger.info("上下文管理器初始化成功")
                
                # 初始化情绪增强器（仅在启用时）
                if emoticons_config.get('enabled', True):  # 默认为true
                    self.emotion_enhancer = EmotionEnhancer(config=self.config)
                    self.logger.info("情绪增强器初始化成功")
                else:
                    self.logger.info("情绪增强器已禁用（配置: emoticons.enabled: false）")
            except Exception as e:
                self.logger.warning(f"上下文或情绪增强器初始化失败: {e}")
        else:
            if CONTEXT_AND_EMOTION_AVAILABLE:
                self.logger.info("上下文或情绪增强功能未启用")
            else:
                self.logger.info("上下文或情绪增强模块未安装，功能不可用")
        
        # 初始化四层触发决策器
        self.four_layer_trigger = None
        trigger_config = self.config.get('trigger', {})
        
        if trigger_config.get('enabled', False) and FOUR_LAYER_TRIGGER_AVAILABLE:
            try:
                from src.ai.ai_client import AIClient
                # 注意：这里需要AI客户端，但AI客户端可能在skill_manager中初始化
                # 暂时先不传递ai_client，后续在initialize中设置
                self.four_layer_trigger = FourLayerTrigger(
                    config=self.config,
                    context_manager=self.context_manager,
                    ai_client=self.ai_client
                )
                self.logger.info("四层触发决策器初始化成功")
            except Exception as e:
                self.logger.warning(f"四层触发决策器初始化失败: {e}")
        else:
            if FOUR_LAYER_TRIGGER_AVAILABLE:
                self.logger.info("四层触发功能未启用")
            else:
                self.logger.info("四层触发模块未安装，功能不可用")
        
        self._human_escalation = None
        self._human_escalation_store = None
        try:
            from src.utils.human_escalation_store import HumanEscalationStore
            from src.utils.human_escalation import HumanEscalationHelper
            self._human_escalation_store = HumanEscalationStore(cfg_dir / "human_escalation.db")
            self._human_escalation = HumanEscalationHelper(
                self.config.config if hasattr(self.config, "config") else {},
                self._human_escalation_store,
            )
            self.logger.info("人工转接（重复问句）模块已加载")
        except Exception as e:
            self.logger.warning("人工转接模块初始化失败: %s", e)
        
        self.logger.info(f"Telegram客户端初始化: {self.session_name}")
    
    async def initialize(self) -> bool:
        """初始化Telegram客户端"""
        try:
            if not PYROGRAM_AVAILABLE:
                self.logger.error("pyrogram库未安装，请运行: pip install pyrogram")
                return False
            
            # 检查 API 凭证：api_id/api_hash 必需；phone 仅在"无既有 session"时必需。
            # N 线 核心4：有 session_string 或已落盘 session 文件 → 视为已授权，免 phone 拉起。
            if not (self.api_id and self.api_hash):
                self.logger.error("Telegram API凭证不完整")
                self.logger.error("请从 https://my.telegram.org 获取api_id和api_hash")
                return False
            _has_session = bool(self.session_string) or os.path.exists(self._session_file_path())
            if not _has_session and not self.phone_number:
                self.logger.error("Telegram 登录信息不完整：需 phone_number 或已有 session（session_string/会话文件）")
                return False
            
            # 创建客户端
            _client_kwargs: Dict[str, Any] = dict(
                name=self.session_name,
                api_id=int(self.api_id),
                api_hash=self.api_hash,
                workdir="sessions",  # 会话文件保存目录
            )
            if self.phone_number:
                _client_kwargs["phone_number"] = self.phone_number
            # N 线 核心4：session_string 优先（in-memory 已授权 session，协议多开/云端拉起常用）
            if self.session_string:
                _client_kwargs["session_string"] = self.session_string
            # N 线 核心2：注入每号独立代理（复用 B 线 proxy_pool + _to_pyrogram_proxy）
            _proxy = self._resolve_proxy()
            if _proxy:
                _client_kwargs["proxy"] = _proxy
                self.logger.info(
                    "Telegram 客户端绑定代理 proxy_id=%s (%s)",
                    self.proxy_id, _proxy.get("hostname"),
                )
            self.client = Client(**_client_kwargs)
            
            self.logger.info("Telegram客户端创建成功")
            return True
            
        except Exception as e:
            self.logger.error(f"初始化Telegram客户端失败: {e}")
            return False
    
    def _resolve_proxy(self) -> Optional[Dict[str, Any]]:
        """解析本账号绑定的代理 → pyrogram proxy 配置（无 / 失败 → None）。

        N 线 核心2：复用 B 线 ``proxy_pool`` + ``_to_pyrogram_proxy``，A/B 同一套代理源，
        不重复造代理逻辑。``proxy_id`` 为空或解析失败时静默返回 None（保持直连旧行为）。
        """
        if not self.proxy_id:
            return None
        try:
            from src.integrations.proxy_pool import get_proxy_pool
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy
            entry = get_proxy_pool().get(self.proxy_id, mask=False)
            return _to_pyrogram_proxy(entry)
        except Exception as ex:
            self.logger.warning("代理解析失败 proxy_id=%s: %s", self.proxy_id, ex)
            return None

    def _session_file_path(self) -> str:
        """会话文件路径：workdir 为 sessions 时，文件为 sessions/{name}.session"""
        import os
        return os.path.join("sessions", f"{self.session_name}.session")

    async def _handle_authorization(self) -> bool:
        """处理授权和登录。有 session 文件时不 connect()，让后面 start() 负责连接并启动收消息；无 session 时先 connect() 再登录。"""
        try:
            import os
            session_path = self._session_file_path()
            if os.path.exists(session_path):
                # 已有 session：不 connect()，直接返回，让 start() 里执行 client.start() 以启动更新循环（才能收消息）
                self.logger.info("检测到已有会话文件，将使用 start() 连接并接收消息")
                return True

            # 无 session：必须先 connect() 再检查/执行登录，否则会报 Client has not been started yet
            is_authorized = await self.client.connect()
            if not is_authorized:
                self.logger.info("需要重新授权登录")
                
                # 发送验证码
                sent_code = await self.client.send_code(self.phone_number)
                self.logger.info(f"验证码已发送到 {self.phone_number}")
                
                # 这里需要用户输入验证码
                # 在实际使用中，可以通过其他方式获取验证码
                phone_code = await self._request_phone_code()
                
                if not phone_code:
                    self.logger.error("未收到验证码，登录失败")
                    return False
                
                try:
                    # 使用验证码登录
                    await self.client.sign_in(
                        phone_number=self.phone_number,
                        phone_code_hash=sent_code.phone_code_hash,
                        phone_code=phone_code
                    )
                    self.logger.info("登录成功")
                    
                except SessionPasswordNeeded:
                    # 需要两步验证密码
                    password = await self._request_2fa_password()
                    if password:
                        await self.client.check_password(password)
                        self.logger.info("两步验证通过")
                    else:
                        self.logger.error("未提供两步验证密码")
                        return False
                        
                except (PhoneCodeInvalid, PhoneCodeExpired) as e:
                    self.logger.error(f"验证码错误: {e}")
                    return False
                    
                except FloodWait as e:
                    self.logger.error(f"触发洪水等待: 需要等待 {e.value} 秒")
                    return False
                    
                except Unauthorized as e:
                    self.logger.error(f"未授权错误: {e}")
                    return False
            
            # 获取用户信息
            self.user_info = await self.client.get_me()
            self.logger.info(f"登录用户: {self.user_info.first_name} (@{self.user_info.username})")
            
            return True
            
        except Exception as e:
            self.logger.error(f"授权处理失败: {e}")
            return False
    
    async def _request_phone_code(self) -> Optional[str]:
        """
        请求用户输入手机验证码
        
        注意: 验证码有效期只有5分钟，需要快速处理
        """
        self.logger.warning("⚠️ 需要手机验证码，请在Telegram应用中查看")
        self.logger.warning("📱 请检查您的手机短信或Telegram应用")
        self.logger.warning("⏰ 验证码有效期: 5分钟，请快速处理")
        
        # 尝试从文件读取验证码（快速方法）
        import asyncio
        code_file = "code.txt"
        
        # 等待用户输入验证码
        for i in range(30):  # 等待最多30秒
            try:
                # 检查code.txt文件
                import os
                if os.path.exists(code_file):
                    with open(code_file, 'r') as f:
                        phone_code = f.read().strip()
                    if phone_code and phone_code.isdigit():
                        self.logger.info(f"从文件读取到验证码: {phone_code}")
                        # 删除文件避免重复使用
                        os.remove(code_file)
                        return phone_code
            except Exception:
                pass
            
            # 等待1秒后重试
            await asyncio.sleep(1)
        
        self.logger.error("未收到验证码，登录失败")
        return None
    
    async def _request_2fa_password(self) -> Optional[str]:
        """
        获取两步验证密码。
        按优先级从三个来源读取：环境变量 > 配置文件 > 本地文件 2fa_password.txt
        """
        self.logger.warning("🔐 需要两步验证密码，正在查找...")

        password = os.environ.get("TG_2FA_PASSWORD", "").strip()
        if password:
            self.logger.info("从环境变量 TG_2FA_PASSWORD 读取到两步验证密码")
            return password

        try:
            tg_cfg = self.config.get('telegram', {}) if hasattr(self.config, 'get') else {}
            password = (tg_cfg.get("two_fa_password") or "").strip()
            if password:
                self.logger.info("从配置文件 telegram.two_fa_password 读取到两步验证密码")
                return password
        except Exception:
            pass

        fa_file = "2fa_password.txt"
        try:
            if os.path.exists(fa_file):
                with open(fa_file, "r") as f:
                    password = f.read().strip()
                if password:
                    self.logger.info("从文件 %s 读取到两步验证密码", fa_file)
                    return password
        except Exception:
            pass

        self.logger.error(
            "未找到两步验证密码。请通过以下任一方式提供：\n"
            "  1. 设置环境变量 TG_2FA_PASSWORD\n"
            "  2. 在 config.yaml 的 telegram 段添加 two_fa_password 字段\n"
            "  3. 在项目根目录创建 2fa_password.txt 文件"
        )
        return None
    
    async def start(self, block: bool = True):
        """启动Telegram客户端。

        block=True（默认，main.py 独立运行）：末尾进入 ``idle()`` 常驻；
        block=False（N 线 核心4，编排器托管）：连接+装处理器+起消息处理任务后即返回，
        由外部事件循环（编排器监督循环）保活，便于按账号生命周期 start/stop。
        """
        try:
            if not self.client:
                self.logger.error("Telegram客户端未初始化")
                return
            
            self.logger.info("启动Telegram客户端...")
            
            # 处理授权
            if not await self._handle_authorization():
                self.logger.error("授权失败，无法启动")
                return
            
            # 设置消息处理器
            self._setup_handlers()
            
            # 启动客户端（含 database is locked 重试）
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    await self.client.start()
                    break
                except Exception as e:
                    err_msg = str(e).lower()
                    if "already connected" in err_msg or (type(e).__name__ == "ConnectionError"):
                        self.logger.info("客户端已连接（首次登录），跳过 start()；若收不到消息请重启程序一次")
                        break
                    elif "database is locked" in err_msg and attempt < max_retries:
                        wait = attempt * 2
                        self.logger.warning("会话数据库锁定 (尝试 %d/%d)，%d 秒后重试...", attempt, max_retries, wait)
                        await asyncio.sleep(wait)
                    else:
                        raise
            if self.user_info is None:
                self.user_info = await self.client.get_me()
                self.logger.info(f"登录用户: {self.user_info.first_name} (@{self.user_info.username})")
            self.running = True
            
            asyncio.create_task(self._message_processor())
            # 轮询兜底：当实时 MTProto 推送通道失效（如某些 session/运行时收不到 updateNewMessage）时，
            # 用 RPC（get_dialogs，正常可用）定时拉新进站私聊喂给同一条 _process_message，
            # 保证全自动回复不因实时通道静默挂掉而失效。与实时 handler 共用去重，互不重复。
            asyncio.create_task(self._poll_inbound_loop())
            self._register_reload_notifier()
            self._start_scheduler()
            # P2：情绪增强配置可观测（便于部署核对）
            ec = self.config.get('emoticons', {})
            self.logger.info("情绪增强: %s", "已启用" if ec.get('enabled', True) else "已禁用（emoticons.enabled: false）")
            self.logger.info("✅ Telegram客户端已启动，等待消息...")

            # N 线 核心4：编排器托管模式不进入 idle()，直接返回让监督循环保活
            if not block:
                return
            # 保持客户端运行（可用 pyrogram.idle() 替代，此处用简单循环）
            try:
                from pyrogram import idle
                await idle()
            except ImportError:
                while self.running:
                    await asyncio.sleep(1)
                
        except Exception as e:
            self.logger.error(f"启动Telegram客户端失败: {e}")
    
    async def stop(self):
        """停止Telegram客户端"""
        if self.running and self.client:
            self.logger.info("正在停止Telegram客户端...")
            self.running = False
            
            try:
                await self.client.stop()
                self.logger.info("Telegram客户端已停止")
            except Exception as e:
                self.logger.error(f"停止Telegram客户端时出错: {e}")

    # ── 触发决策方法: TelegramTriggerMixin (src/client/trigger.py) ──
    # ── 发送方法: TelegramSenderMixin (src/client/sender.py) ──


    def _setup_handlers(self):
        """设置消息处理器"""
        if not self.client:
            return

        _reject_cooldowns: dict = {}

        @self.client.on_message(filters.private)
        async def handle_private_message(client, message: Message):
            """处理私聊消息（动态读配置，无需重启即可切换行为）"""
            try:
                uid = str(getattr(getattr(message, 'from_user', None), 'id', 0))

                # ── 限流：私聊也走令牌桶 ──
                if self._rate_limiter.enabled:
                    allowed, reason = self._rate_limiter.allow(uid, message.chat.id)
                    if not allowed:
                        if self._rate_limiter.check_auto_ban(uid):
                            self.logger.warning("[私聊] 用户 %s 触发自动封禁", uid)
                        else:
                            self.logger.info("[私聊] 限流丢弃 user=%s reason=%s", uid, reason)
                        return

                # ── 自动封禁检查 ──
                if self._rate_limiter.is_banned(uid):
                    self.logger.debug("[私聊] 用户 %s 在封禁名单中，静默忽略", uid)
                    return

                # ── 动态读取最新配置 ──
                tg_cfg = self.config.get_telegram_config()
                process_private = tg_cfg.get("process_private", True)

                if process_private:
                    if message.text or message.caption or message.voice or message.audio or message.photo or message.document:
                        await self._process_message(message)
                    else:
                        self.logger.debug("忽略非文本/语音/图片私聊消息: %s", message.chat.id)
                else:
                    reject_msg = tg_cfg.get(
                        "private_reject_message",
                        "亲，抱歉，我们不接受私聊处理问题，请在群内联系我或者@我，我将竭诚为您服务。"
                    )
                    now = __import__('time').time()
                    cooldown = int(tg_cfg.get("private_reject_cooldown", 60))
                    last_sent = _reject_cooldowns.get(uid, 0)
                    if now - last_sent < cooldown:
                        self.logger.debug("[私聊] 引导语冷却中 user=%s (%.0fs)", uid, now - last_sent)
                        return
                    _reject_cooldowns[uid] = now
                    try:
                        await message.reply_text(reject_msg)
                        self.logger.info("[私聊] 已发送引导语 → user=%s", uid)
                    except Exception as e:
                        self.logger.warning("[私聊] 发送引导语失败: %s", e)

                    if len(_reject_cooldowns) > 5000:
                        cutoff = now - 3600
                        _reject_cooldowns.clear()
                        # 大量缓存时整体清理即可，下次再发自然重新计时
            except Exception as e:
                self.logger.error("处理私聊消息失败: %s", e)

        # 处理群组消息
        @self.client.on_message(filters.group)
        async def handle_group_message(client, message: Message):
            """处理群组消息"""
            try:
                # ── 动态开关：实时读取配置，无需重启 ──
                if not self.config.get_telegram_config().get("process_groups", True):
                    return

                chat_id = getattr(message.chat, 'id', 0)
                self.logger.info("[群消息] 收到一条群消息 chat_id=%s", chat_id)
                # P-1: 跳过 bot 启动之前的旧消息（防止重启后处理历史队列导致循环）
                msg_date = getattr(message, 'date', None)
                if msg_date:
                    msg_ts = msg_date.timestamp() if hasattr(msg_date, 'timestamp') else 0
                    if msg_ts < self._boot_timestamp - 5:
                        self.logger.info("[群消息] 跳过: 启动前旧消息 chat_id=%s", chat_id)
                        return

                # P-0.5: 消息去重 — 防止网络重传导致同一条消息被处理两次
                mid = getattr(message, 'id', 0) or getattr(message, 'message_id', 0)
                if mid:
                    if mid in self._processed_msg_ids:
                        self.logger.debug("[群消息] 跳过: 去重 mid=%s", mid)
                        return
                    now = time.time()
                    self._processed_msg_ids[mid] = now
                    self._processed_msg_ids.move_to_end(mid)
                    while self._processed_msg_ids:
                        oldest_k, oldest_v = next(iter(self._processed_msg_ids.items()))
                        if now - oldest_v > self._dedup_ttl or len(self._processed_msg_ids) > self._dedup_max_size:
                            self._processed_msg_ids.popitem(last=False)
                        else:
                            break

                # 多 Bot 路由
                if hasattr(self, '_bot_router') and self._bot_router.enabled:
                    if not self._bot_router.should_handle(message.chat.id, self.session_name):
                        self.logger.info("[群消息] 跳过: 多 Bot 路由(该群由其他 session 处理) chat_id=%s", message.chat.id)
                        return

                # 令牌桶限流
                if self._rate_limiter.enabled:
                    uid = str(getattr(getattr(message, 'from_user', None), 'id', 0))
                    allowed, reason = self._rate_limiter.allow(uid, message.chat.id)
                    if not allowed:
                        self.logger.info("[群消息] 跳过: 限流 user=%s chat=%s reason=%s", uid, message.chat.id, reason)
                        return

                # 检查是否需要处理此消息类型
                if not (message.text or message.caption or message.voice or message.audio or message.photo or message.document):
                    self.logger.info("[群消息] 跳过: 无文本/图/语音 chat_id=%s title=%s", chat_id, getattr(message.chat, 'title', ''))
                    return
                
                # P0: 不处理自己发送的消息（避免自我循环）
                from_user = getattr(message, 'from_user', None)
                if from_user and self.user_info and getattr(from_user, 'id', None) == self.user_info.id:
                    self.logger.info("[群消息] 跳过: 自身发送的消息 (from_user.id=%s)", getattr(from_user, 'id', None))
                    return
                if getattr(message, 'outgoing', False):
                    self.logger.info("[群消息] 跳过: outgoing 自身消息")
                    return
                # GXP 结果追踪：在过滤 bot 消息前，先检查是否为 gxp_notify_bot 的回复
                gxp_bot_username = (self.config.get('telegram', {}).get('gxp_commands', {}).get('bot_username') or 'gxp_notify_bot').lower()
                if from_user:
                    sender_uname = (getattr(from_user, 'username', '') or '').strip().lower()
                    if sender_uname == gxp_bot_username:
                        await self._handle_gxp_bot_reply(message)

                # P0: 不回复机器人账号或配置中的「不回复」发送者（避免对 gxp_notify_bot 等误回）
                if from_user and getattr(from_user, 'is_bot', False):
                    self.logger.info("[群消息] 跳过: 机器人发送者 username=%s", getattr(from_user, 'username', ''))
                    return
                no_reply = self.config.get('telegram', {}).get('no_reply_sender_usernames') or []
                if no_reply and from_user:
                    uname = (getattr(from_user, 'username') or '').strip().lower()
                    if uname and uname in [u.strip().lower().lstrip('@') for u in no_reply]:
                        self.logger.info("[群消息] 跳过: 配置的不回复发送者 username=%s", uname)
                        return
                # 📊 强制记录所有群组消息（监控完整性）- 新增
                text = message.text or message.caption or ""
                chat_title = message.chat.title if message.chat.title else "Unknown"
                username = from_user.username if from_user else "unknown"
                self.logger.info(f"[群组监控] 收到消息 [{chat_title}/{username}]: {text[:100]}...")
                
                # 检查是否需要回复（使用异步方法）
                if not await self._should_reply_to_group_message(message):
                    mode = self.config.get('telegram', {}).get('group_reply', {}).get('mode', 'always')
                    trigger_on = self.config.get('trigger', {}).get('enabled', False)
                    self.logger.info(
                        "[群组监控] 跳过未触发消息: %s... (模式=%s, 四层=%s). "
                        "触发方式: 回复我们的消息 / @本账号 / 关键词或图片+文字 / 追问或会话窗口内L2",
                        text[:50] if text else "(无文本)", mode, "开" if trigger_on else "关"
                    )
                    return
                
                # 满足条件，处理消息
                await self._process_message(message)
                
            except Exception as e:
                self.logger.error(f"处理群组消息失败: {e}")
        
        _tg = self.config.get_telegram_config()
        self.logger.info(
            "消息处理器已设置 - 私聊=%s, 群组=%s (动态开关，保存即生效)",
            "处理" if _tg.get("process_private", True) else "引导语",
            "开" if _tg.get("process_groups", True) else "关",
        )
    
    async def _download_voice_file(self, message: Message) -> Optional[Path]:
        """
        下载语音消息文件到临时目录
        
        Args:
            message: Telegram消息对象，包含voice或audio属性
            
        Returns:
            下载的文件路径，如果失败返回None
        """
        try:
            # 确定文件ID和文件类型
            if message.voice:
                file_id = message.voice.file_id
                file_extension = ".ogg"  # Telegram语音通常是OGG格式
            elif message.audio:
                file_id = message.audio.file_id
                file_extension = ".mp3"  # 或根据实际情况
            else:
                self.logger.error("消息不是语音或音频类型")
                return None
            
            # 生成临时文件名
            import time
            import uuid
            timestamp = int(time.time())
            unique_id = str(uuid.uuid4())[:8]
            temp_filename = f"voice_{timestamp}_{unique_id}{file_extension}"
            temp_file_path = self.temp_dir / temp_filename
            
            self.logger.info(f"下载语音文件: {file_id} -> {temp_file_path}")
            
            # 下载文件
            await message.download(file_name=str(temp_file_path))
            
            # 检查文件是否下载成功
            if temp_file_path.exists() and temp_file_path.stat().st_size > 0:
                file_size = temp_file_path.stat().st_size
                if file_size > self.max_file_size:
                    self.logger.warning(f"文件过大: {file_size} bytes > {self.max_file_size} limit")
                    temp_file_path.unlink(missing_ok=True)
                    return None
                
                self.logger.info(f"语音文件下载成功: {temp_file_path} ({file_size} bytes)")
                return temp_file_path
            else:
                self.logger.error("文件下载失败或文件为空")
                return None
                
        except Exception as e:
            self.logger.error(f"下载语音文件失败: {e}")
            return None
    
    async def _download_image_file(self, message: Message) -> Optional[Path]:
        """
        下载图片消息文件到临时目录
        
        Args:
            message: Telegram消息对象，包含photo或document属性
            
        Returns:
            下载的文件路径，如果失败返回None
        """
        try:
            # 确定文件ID和文件类型
            file_id = None
            file_extension = ".jpg"  # 默认扩展名
            
            if message.photo:
                # 照片消息：Pyrogram 2.x 中 photo 可能是单个 Photo 对象或 PhotoSize 列表，先按单对象取 file_id
                photo_obj = message.photo
                file_id = getattr(photo_obj, 'file_id', None)
                if file_id is None and isinstance(photo_obj, (list, tuple)):
                    try:
                        largest_photo = max(photo_obj, key=lambda p: getattr(p, 'file_size', 0) or 0)
                        file_id = getattr(largest_photo, 'file_id', None)
                    except (TypeError, ValueError):
                        file_id = getattr(photo_obj[0], 'file_id', None) if photo_obj else None
                file_extension = ".jpg"
                
            elif message.document:
                # 文档消息，检查是否是图片
                document = message.document
                mime_type = document.mime_type or ""
                
                # 检查是否是图片文件
                if mime_type.startswith('image/'):
                    file_id = document.file_id
                    # 根据MIME类型确定扩展名
                    if 'jpeg' in mime_type or 'jpg' in mime_type:
                        file_extension = ".jpg"
                    elif 'png' in mime_type:
                        file_extension = ".png"
                    elif 'gif' in mime_type:
                        file_extension = ".gif"
                    elif 'bmp' in mime_type:
                        file_extension = ".bmp"
                    else:
                        file_extension = ".jpg"  # 默认
                else:
                    self.logger.warning(f"不是图片文件: {mime_type}")
                    return None
            else:
                self.logger.error("消息不是图片或文档类型")
                return None
            
            if not file_id:
                self.logger.error("无法获取文件ID")
                return None
            
            # 生成临时文件名
            import time
            import uuid
            timestamp = int(time.time())
            unique_id = str(uuid.uuid4())[:8]
            temp_filename = f"image_{timestamp}_{unique_id}{file_extension}"
            temp_file_path = self.temp_dir / temp_filename
            
            self.logger.info(f"下载图片文件: {file_id} -> {temp_file_path}")
            
            # 下载文件
            await message.download(file_name=str(temp_file_path))
            
            # 检查文件是否下载成功
            if temp_file_path.exists() and temp_file_path.stat().st_size > 0:
                file_size = temp_file_path.stat().st_size
                if file_size > self.max_file_size:
                    self.logger.warning(f"文件过大: {file_size} bytes > {self.max_file_size} limit")
                    temp_file_path.unlink(missing_ok=True)
                    return None
                
                self.logger.info(f"图片文件下载成功: {temp_file_path} ({file_size} bytes)")
                return temp_file_path
            else:
                self.logger.error("文件下载失败或文件为空")
                return None
                
        except Exception as e:
            self.logger.error(f"下载图片文件失败: {e}")
            return None
    
    def _vision_usable(self) -> bool:
        """是否可走 Vision（含 Ollama→智谱回退链）。"""
        v = self.config.get("vision", {})
        if not v.get("enabled") or not VISION_AVAILABLE or not VisionClient:
            return False
        try:
            from src.vision_client import has_any_vision_backend
            return has_any_vision_backend(v, v)
        except Exception:
            return False

    async def _get_image_content(self, image_path: str) -> Optional[str]:
        """
        方案 A：优先 Vision（Ollama→智谱链）解析图片，失败则兜底 OCR。
        返回图中内容的文字描述，供下游 AI 使用。
        """
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return None
        vision_config = self.config.get("vision", {})
        if self._vision_usable():
            try:
                text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
                    vision_config,
                    vision_config,
                    str(path),
                    prompt=vision_config.get("prompt"),
                )
                if text and text.strip():
                    self.logger.info(
                        f"Vision 解析成功 ({tag})，长度 {len(text)} 字符"
                    )
                    return text.strip()[:2000]
            except Exception as e:
                self.logger.warning(f"Vision 解析失败，回退 OCR: {e}")
        # 2. 兜底 OCR
        if self.image_recognizer:
            try:
                lang = self.config.get('image_recognition', {}).get('language', 'zh')
                recognized = await self.image_recognizer.recognize_image(str(path), lang)
                if not (recognized and recognized.strip()) and lang == 'zh':
                    recognized = await self.image_recognizer.recognize_image(str(path), 'en')
                if recognized and recognized.strip():
                    self.logger.info(f"OCR 兜底成功，长度 {len(recognized)} 字符")
                    return recognized.strip()[:2000]
            except Exception as e:
                self.logger.warning(f"OCR 兜底失败: {e}")
        return None
    
    async def _get_recent_bot_messages(self, chat_id: int) -> List[Dict[str, str]]:
        """拉取当前群内近期机器人/通知号消息，供 AI 参考（订单、通道通知等）。"""
        cfg = self.config.get('context', {}).get('bot_sources', {})
        if not cfg.get('enabled', False) or not self.client:
            return []
        limit = min(int(cfg.get('limit', 25)), 50)
        include_any_bot = cfg.get('include_any_bot', False)
        usernames = [u.strip().lower().lstrip('@') for u in cfg.get('usernames', [])]
        out = []
        try:
            async for msg in self.client.get_chat_history(chat_id, limit=limit):
                if not getattr(msg, 'text', None):
                    continue
                from_user = getattr(msg, 'from_user', None)
                if not from_user:
                    continue
                uname = (getattr(from_user, 'username') or '').lower()
                is_bot = getattr(from_user, 'is_bot', False)
                if include_any_bot and is_bot:
                    out.append({"from": uname or str(getattr(from_user, 'id', '')), "text": (msg.text or "")[:800]})
                elif usernames and uname in usernames:
                    out.append({"from": uname, "text": (msg.text or "")[:800]})
            if out:
                self.logger.debug(f"拉取到 {len(out)} 条机器人/通知消息供 AI 参考")
        except Exception as e:
            self.logger.warning(f"获取群内机器人消息失败: {e}")
        return out
    
    async def _get_recent_chat_image_ocr(self, chat_id: int, limit: int = 20) -> Optional[str]:
        """
        当用户问「看到订单图了吗」但当前消息无图时：拉取群内最近一条带图消息并 OCR，
        结果供 AI 使用，从而能回复「看到了」并概括图中内容。
        历史按时间倒序（最新在前），会跳过无图消息，对带图消息逐个尝试 OCR，最多试 3 条带图消息。
        """
        if not self.client or not (self._vision_usable() or self.image_recognizer):
            self.logger.debug("群内最近图 跳过: 未初始化 client 或 Vision/OCR")
            return None
        try:
            tried = 0
            max_photo_messages = 3
            async for msg in self.client.get_chat_history(chat_id, limit=limit):
                has_photo = bool(getattr(msg, 'photo', None))
                has_img_doc = False
                if getattr(msg, 'document', None) and getattr(msg.document, 'mime_type', None):
                    has_img_doc = (msg.document.mime_type or "").startswith("image/")
                if not (has_photo or has_img_doc):
                    continue
                tried += 1
                msg_id = getattr(msg, 'id', None)
                sender = getattr(getattr(msg, 'from_user', None), 'username', None) or getattr(msg, 'sender_id', None)
                self.logger.info(f"群内最近图: 尝试第 {tried} 条带图消息 id={msg_id} 发送者={sender}（Vision/OCR）")
                image_file = await self._download_image_file(msg)
                if not image_file or not image_file.exists():
                    self.logger.warning(f"群内最近图: 下载失败 msg_id={msg_id}")
                    continue
                try:
                    content = await self._get_image_content(str(image_file))
                    if content:
                        self.logger.info(f"群内最近一张图解析成功 msg_id={msg_id}，长度 {len(content)} 字符")
                        return content[:2000]
                    self.logger.warning(f"群内最近图: 结果为空 msg_id={msg_id}，尝试下一条带图消息")
                except Exception as err:
                    self.logger.warning(f"群内最近图解析异常 msg_id={msg_id}: {err}")
                finally:
                    try:
                        image_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                if tried >= max_photo_messages:
                    break
            if tried == 0:
                self.logger.info("群内最近图: 未找到带图消息")
            return None
        except Exception as e:
            self.logger.warning(f"拉取群内最近图片并 OCR 失败: {e}")
            return None
    
    async def _poll_inbound_loop(self):
        """轮询兜底主循环：定时拉取新进站私聊消息（补实时推送缺失）。

        config-gated：``telegram.poll_fallback.enabled``（默认开，dedup 保护下对实时正常的
        部署也安全——实时先处理、轮询命中去重即跳过）。``interval_seconds`` / ``dialogs_limit``
        可调。任何异常不退出循环、不影响实时路径。
        """
        try:
            tg_cfg = self.config.get_telegram_config()
        except Exception:
            tg_cfg = {}
        pf = (tg_cfg.get("poll_fallback") or {}) if isinstance(tg_cfg, dict) else {}
        if not pf.get("enabled", True):
            self.logger.info("[轮询兜底] 已禁用（telegram.poll_fallback.enabled=false）")
            return
        interval = float(pf.get("interval_seconds", 12) or 12)
        dlimit = int(pf.get("dialogs_limit", 30) or 30)
        catchup = float(pf.get("catchup_seconds", 600) or 0)
        self.logger.info(
            "[轮询兜底] 已启动 interval=%.0fs dialogs=%d catchup=%.0fs"
            "（RPC 拉新进站私聊，补实时推送缺失）",
            interval, dlimit, catchup,
        )
        await asyncio.sleep(interval)  # 首轮延迟，避开启动风暴
        _cycle = 0
        while self.running:
            try:
                scanned = await self._poll_inbound_once(dlimit, catchup)
                if _cycle == 0:
                    # 首轮确认：证明 get_dialogs 在 app 内可正常完成（不挂起）
                    self.logger.info("[轮询兜底] 首轮扫描完成 scanned=%d 会话", scanned)
                elif _cycle % 25 == 0:  # 约每 5 分钟一次心跳，确认循环存活
                    self.logger.info("[轮询兜底] 心跳 cycle=%d scanned=%d 会话", _cycle, scanned)
            except Exception as e:
                self.logger.warning("[轮询兜底] 本轮异常（已忽略，下轮继续）: %s", e)
            _cycle += 1
            await asyncio.sleep(interval)

    async def _poll_inbound_once(self, dlimit: int, catchup: float = 0.0) -> int:
        """单轮：扫描最近会话，挑出未处理过的新进站私聊消息并处理。返回扫描的会话数。

        时间闸门：消息时间在 boot 之后**或**距今 ``catchup`` 秒内（覆盖宕机/重启期间到达的
        未读消息），既能补回最近未读、又不回灌远古历史。已回复的会话 top_message 变 outgoing
        会被跳过，故不会重复回复。
        """
        if not self.client:
            return 0
        try:
            tg_cfg = self.config.get_telegram_config()
        except Exception:
            tg_cfg = {}
        if not (tg_cfg.get("process_private", True) if isinstance(tg_cfg, dict) else True):
            return 0
        processed = 0
        scanned = 0
        async for dialog in self.client.get_dialogs(limit=dlimit):
            scanned += 1
            try:
                chat = getattr(dialog, "chat", None)
                if chat is None:
                    continue
                ctype = getattr(chat, "type", None)
                ctype_name = (getattr(ctype, "name", None) or str(ctype or "")).upper()
                if "PRIVATE" not in ctype_name:  # 兜底只覆盖私聊（群有四层触发，避免误回）
                    continue
                msg = getattr(dialog, "top_message", None)
                if msg is None:
                    continue
                if getattr(msg, "outgoing", False):  # 我们自己发的（含已回复）→ 跳过
                    continue
                from_user = getattr(msg, "from_user", None)
                if from_user and self.user_info and \
                        getattr(from_user, "id", None) == self.user_info.id:
                    continue
                if getattr(from_user, "is_bot", False):
                    continue
                if not (getattr(msg, "text", None) or getattr(msg, "caption", None)
                        or getattr(msg, "voice", None) or getattr(msg, "audio", None)
                        or getattr(msg, "photo", None) or getattr(msg, "document", None)):
                    continue
                mdate = getattr(msg, "date", None)
                mts = mdate.timestamp() if (mdate and hasattr(mdate, "timestamp")) else 0
                if mts:
                    after_boot = mts >= self._boot_timestamp - 5
                    within_catchup = catchup > 0 and (time.time() - mts) <= catchup
                    if not (after_boot or within_catchup):  # 远古历史，防重启回灌
                        continue
                mid = getattr(msg, "id", 0) or getattr(msg, "message_id", 0)
                if mid and mid in self._processed_msg_ids:  # 与实时 handler 共用去重
                    continue
                uid = str(getattr(from_user, "id", 0))
                if self._rate_limiter.enabled:
                    if self._rate_limiter.is_banned(uid):
                        continue
                    allowed, _reason = self._rate_limiter.allow(uid, getattr(chat, "id", 0))
                    if not allowed:
                        continue
                # 处理前先登记去重（防并发/下一轮在回复落地前重入造成重复回复）
                if mid:
                    now = time.time()
                    self._processed_msg_ids[mid] = now
                    self._processed_msg_ids.move_to_end(mid)
                    while self._processed_msg_ids:
                        _ok, _ov = next(iter(self._processed_msg_ids.items()))
                        if now - _ov > self._dedup_ttl or \
                                len(self._processed_msg_ids) > self._dedup_max_size:
                            self._processed_msg_ids.popitem(last=False)
                        else:
                            break
                self.logger.info(
                    "[轮询兜底] 发现新进站私聊 chat=%s mid=%s text=%r",
                    getattr(chat, "id", ""), mid,
                    (getattr(msg, "text", None) or getattr(msg, "caption", None) or "")[:50],
                )
                await self._process_message(msg)
                processed += 1
            except Exception as e:
                self.logger.debug("[轮询兜底] 单会话处理失败（已忽略）: %s", e, exc_info=True)
        if processed:
            self.logger.info("[轮询兜底] 本轮处理 %d 条新进站私聊", processed)
        return scanned

    async def _process_message(self, message: Message):
        """处理接收到的消息"""
        try:
            # 获取消息信息
            user_id = message.from_user.id if message.from_user else 0
            username = message.from_user.username if message.from_user else "unknown"
            chat_title = message.chat.title if hasattr(message.chat, 'title') and message.chat.title else "私聊"
            
            # 获取消息文本（包括caption）；P2 编码防护：统一归一化为安全 str
            raw_text = getattr(message, "text", None) or getattr(message, "caption", None)
            text = _normalize_message_text(raw_text) if raw_text else ""
            image_ocr_text = None  # 带图时的 OCR 结果，会传给 AI 以保持话术与图一致
            
            # 处理语音消息
            if not text and (message.voice or message.audio):
                if self.voice_transcriber:
                    try:
                        self.logger.info(f"收到语音消息 [{chat_title}/{username}]，开始转录...")
                        
                        # 1. 下载语音文件
                        voice_file = await self._download_voice_file(message)
                        if not voice_file:
                            text = "[语音消息 - 下载失败]"
                            self.logger.error("语音文件下载失败")
                        else:
                            try:
                                # 2. 调用转录服务
                                language = self.config.get('voice_recognition', {}).get('language', 'zh')
                                transcribed_text = await self.voice_transcriber.transcribe_voice_message(
                                    str(voice_file), language
                                )
                                
                                if transcribed_text:
                                    text = f"[语音转录] {transcribed_text}"
                                    self.logger.info(f"语音转录成功: {transcribed_text[:100]}...")
                                else:
                                    text = "[语音消息 - 转录失败或无内容]"
                                    self.logger.warning("语音转录返回空结果")
                                
                                # 3. 清理临时文件
                                try:
                                    voice_file.unlink(missing_ok=True)
                                    self.logger.debug(f"已清理临时文件: {voice_file}")
                                except Exception as cleanup_error:
                                    self.logger.warning(f"清理临时文件失败: {cleanup_error}")
                                    
                            except Exception as transcribe_error:
                                self.logger.error(f"语音转录过程失败: {transcribe_error}")
                                text = "[语音消息 - 转录过程错误]"
                                # 清理临时文件
                                try:
                                    voice_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        
                    except Exception as e:
                        self.logger.error(f"处理语音消息失败: {e}")
                        text = "[语音消息 - 处理异常]"
                else:
                    self.logger.info(f"收到语音消息 [{chat_title}/{username}]（语音识别未启用或依赖未安装）")
                    text = "[语音消息 - 识别功能未启用]"
            
            # 有说明文字但带图时：Vision 为主、OCR 兜底，把图内容给 AI
            has_image = bool(message.photo or (message.document and message.document.mime_type and
                               message.document.mime_type.startswith('image/')))
            if text and has_image and (self._vision_usable() or self.image_recognizer):
                try:
                    self.logger.info(f"收到带图消息 [{chat_title}/{username}]，解析图中内容（Vision/OCR）...")
                    image_file = await self._download_image_file(message)
                    if image_file:
                        try:
                            image_ocr_text = await self._get_image_content(str(image_file))
                            if image_ocr_text:
                                image_ocr_text = _normalize_message_text(image_ocr_text)
                                self.logger.info(f"带图消息解析成功: {image_ocr_text[:80]}...")
                            try:
                                image_file.unlink(missing_ok=True)
                            except Exception:
                                pass
                        except Exception as e:
                            self.logger.warning(f"带图消息解析异常: {e}")
                except Exception as e:
                    self.logger.warning(f"带图消息下载/解析异常: {e}")
            
            # 处理图片消息（如果没有文本且不是语音消息）— Vision 为主、OCR 兜底
            if not text and (message.photo or message.document):
                if self._vision_usable() or self.image_recognizer:
                    try:
                        self.logger.info(f"收到图片消息 [{chat_title}/{username}]，解析图中内容（Vision/OCR）...")
                        image_file = await self._download_image_file(message)
                        if not image_file:
                            text = "[图片消息 - 下载失败]"
                            self.logger.error("图片文件下载失败")
                        else:
                            try:
                                content = await self._get_image_content(str(image_file))
                                if content:
                                    content = _normalize_message_text(content)
                                    text = f"[图片内容] {content}"
                                    image_ocr_text = content
                                    self.logger.info(f"图片解析成功: {content[:100]}...")
                                else:
                                    text = "[图片消息 - 解析失败或无文字]"
                                    self.logger.warning("图片解析返回空结果")
                                try:
                                    image_file.unlink(missing_ok=True)
                                except Exception as cleanup_error:
                                    self.logger.warning(f"清理临时文件失败: {cleanup_error}")
                            except Exception as recognize_error:
                                self.logger.error(f"图片解析过程失败: {recognize_error}")
                                text = "[图片消息 - 解析过程错误]"
                                try:
                                    image_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                    except Exception as e:
                        self.logger.error(f"处理图片消息失败: {e}")
                        text = "[图片消息 - 处理异常]"
                else:
                    self.logger.info(f"收到图片消息 [{chat_title}/{username}]（Vision/OCR 均未启用）")
                    text = "[图片消息 - 识别功能未启用]"
            
            if text:
                self.logger.info(f"收到消息 [{chat_title}/{username}]: {self._log_safe_text(text)}")
                m = _metrics()
                if m:
                    m.record_message_received()
                    m.set_queue_size(self.message_queue.qsize() + 1)
                msg_data = {
                    'message': message,
                    'user_id': user_id,
                    'username': username,
                    'text': _normalize_message_text(text),
                    'chat_id': message.chat.id,
                    'image_ocr_text': image_ocr_text,
                    '_trigger_path': getattr(message, '_trigger_path', None),
                    '_is_voice_msg': bool(message.voice or message.audio),
                }
                try:
                    self.message_queue.put_nowait(msg_data)
                except asyncio.QueueFull:
                    self.logger.warning("消息队列已满 (%d)，丢弃消息: %s/%s",
                                        self.message_queue.maxsize, chat_title, username)
                    m2 = _metrics()
                    if m2:
                        m2.record_queue_drop()
            else:
                self.logger.debug(f"忽略非文本/语音消息: {chat_title}/{username}")
            
        except Exception as e:
            self.logger.error(f"处理消息失败: {e}")
            m = _metrics()
            if m:
                m.record_error()

    async def _message_processor(self):
        """消息处理任务"""
        self.logger.info("消息处理器已启动")
        
        while self.running:
            try:
                message_data = await self.message_queue.get()
                self.config.check_and_hot_reload()
                m = _metrics()
                if m:
                    m.set_queue_size(self.message_queue.qsize())
                asyncio.create_task(self._guarded_process(message_data))
                self.message_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"消息处理器错误: {e}")
                m = _metrics()
                if m:
                    m.record_error()
    
    async def _guarded_process(self, message_data: Dict[str, Any]):
        """使用 Semaphore 控制并发的消息处理包装器"""
        async with self._process_semaphore:
            self._active_tasks += 1
            m = _metrics()
            if m:
                m.set_active_tasks(self._active_tasks, self._max_concurrent)
            try:
                await self._process_message_async(message_data)
            finally:
                self._active_tasks -= 1
                if m:
                    m.set_active_tasks(self._active_tasks, self._max_concurrent)

    def _emit_inbox(self, *, chat_id: Any, text: str, direction: str,
                    name: str = "", msg_id: str = "",
                    media_type: str = "", media_ref: str = "") -> None:
        """N4b：companion 运行时把 A 线收/发的消息镜像进统一收件箱（坐席台可见）。

        默认关（``self._mirror_inbox`` False）→ standalone main.py 零影响。仅 emit 到
        收件箱 sink（不触发 B 线 autoreply，避免与 A 线自身回复重复）。best-effort，
        绝不影响主消息流。
        """
        if not getattr(self, "_mirror_inbox", False):
            return
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            emit_incoming(make_message(
                platform="telegram",
                account_id=getattr(self, "account_id", "default"),
                chat_key=str(chat_id),
                name=name or "",
                text=text or "",
                ts=time.time(),
                msg_id=str(msg_id or ""),
                direction=direction,
                media_type=media_type,
                media_ref=media_ref,
            ))
        except Exception:
            try:
                self.logger.debug("[mirror] 收件箱镜像失败", exc_info=True)
            except Exception:
                pass

    async def _process_message_async(self, message_data: Dict[str, Any]):
        """异步处理消息"""
        import time
        start_time = time.time()
        
        try:
            message = message_data['message']
            user_id = message_data['user_id']
            text = message_data['text']
            chat_id = message_data['chat_id']

            # N4b：入站镜像（companion 模式才生效）→ 坐席台/统一收件箱可见用户原话
            self._emit_inbox(
                chat_id=chat_id, text=text, direction="in",
                name=str(message_data.get('username') or ''),
                msg_id=str(getattr(message, 'id', '') or ''),
            )

            _es_cnt, _es_key = 0, ""
            if text and self._human_escalation:
                try:
                    self._human_escalation.reload_config(
                        self.config.config if hasattr(self.config, "config") else {}
                    )
                    _es_cnt, _es_key = self._human_escalation.record_streak(
                        chat_id, user_id, text
                    )
                except Exception as ex:
                    self.logger.warning("人工转接计数失败: %s", ex)
            
            # 记录接收时间
            receive_time = time.time()
            
            # 上下文分析（如果启用）
            context_analysis = None
            should_reply = True  # 默认回复
            
            if self.context_manager:
                try:
                    # 添加上下文消息
                    self.context_manager.add_message(
                        chat_id=chat_id,
                        user_id=user_id,
                        username=message_data.get('username', 'unknown'),
                        text=text,
                        is_ai=False
                    )
                    
                    # 分析上下文
                    context_analysis = self.context_manager.analyze_context(
                        chat_id=chat_id,
                        current_context=text
                    )
                    
                    # 根据上下文分析决定是否需要回复
                    should_reply = context_analysis.get('should_reply', True)
                    
                    if not should_reply:
                        self.logger.info(f"根据上下文分析，不回复此消息: {text[:50]}...")
                        return
                    
                    self.logger.info(
                        f"上下文分析结果 - 情绪: {context_analysis.get('user_emotion', 'unknown')}, "
                        f"主题: {context_analysis.get('conversation_topic', 'unknown')}, "
                        f"优先级: {context_analysis.get('priority', 'normal')}"
                    )
                    
                except Exception as e:
                    self.logger.warning(f"上下文分析失败: {e}")
            
            # 群内：拉取近期机器人/通知消息，与 OCR 一起供 AI 使用（群聊 id 为负）
            recent_bot_messages = []
            image_ocr_text = message_data.get('image_ocr_text')
            if isinstance(chat_id, (int, float)) and int(chat_id) < 0:
                recent_bot_messages = await self._get_recent_bot_messages(int(chat_id))
                # 当前消息无图但与订单/查单相关时，拉取群内最近一张图作为查单依据（SOP：有凭证即按凭证确认）
                if not image_ocr_text and text:
                    t = text.strip()
                    order_ask = (
                        ("订单" in t and any(k in t for k in ("看", "图", "发", "了", "吗")))
                        or ("订单" in t and "吗" in t)
                        or ("查" in t and any(k in t for k in ("订单", "单", "到了", "凭证")))
                    )
                    if order_ask:
                        self.logger.info("当前消息无图且为订单相关问句，拉取群内最近一张图做 OCR")
                        recent_ocr = await self._get_recent_chat_image_ocr(int(chat_id))
                        if recent_ocr:
                            image_ocr_text = _normalize_message_text(recent_ocr)
                            self.logger.info("已使用群内最近一张图 OCR 作为订单上下文")
                        else:
                            self.logger.info("群内最近图 OCR 无结果，AI 将无图上下文回复")
            
            # 群名（用于额度规则：特殊客户/黑名单按群名识别）
            msg = message_data.get('message')
            chat_title = (getattr(msg.chat, 'title', None) or '').strip() if msg and getattr(msg, 'chat', None) else ''
            # chat_type 用于 3-tier persona 路由和下游决策
            _chat_type_str = str(getattr(getattr(msg, 'chat', None), 'type', '') or '').lower()
            _is_group = _chat_type_str in ('group', 'supergroup', 'channel')

            # request_id 串联整条链路，便于日志与排错
            request_id = f"{chat_id}_{getattr(message, 'id', 0)}"
            # 是否因 @ 本账号而触发回复（用于 S5 静默策略：被 @ 时不再按概率跳过）
            triggered_by_mention = False
            if self.user_info and getattr(self.user_info, 'username', None):
                uname = (self.user_info.username or "").strip().lower().lstrip("@")
                if uname and text:
                    t_lower = text.strip().lower()
                    if f"@{uname}" in t_lower or f"@{uname}\u200b" in t_lower:
                        triggered_by_mention = True
            # 情绪粗判传入 AI prompt（在情绪增强之前，与 enhance_reply 独立）
            # N 线 核心1：复用共享 companion_context（A/B 两线同一套情绪/人设逻辑）
            from src.utils.companion_context import (
                emotion_hint as _companion_emotion_hint,
                record_relationship_message as _record_relationship_message,
                resolve_funnel_stage as _resolve_funnel_stage,
                resolve_intimacy_score as _resolve_intimacy_score,
                route_persona_id as _route_persona_id,
            )
            user_emotion_hint = _companion_emotion_hint(text, self.emotion_enhancer)
            # Q3：先把本条入站记入 contacts（recorder 未开则 no-op）→ 刷新 journey 的
            # intimacy_score，再读出，保证融合用到的是"含本轮"的最新分（与 RPA 各线同序）。
            _record_relationship_message(
                self.account_id, chat_id, "in",
                text_preview=text or "",
                display_name=str(message_data.get('username') or ''),
            )
            # Q3：注入统一关系事实源（contacts.IntimacyEngine）→ companion_relationship
            # 双信号融合（沉默衰减自动降阶 + reunion 提示）。provider 未注册时返回 None，
            # 行为完全等同旧版（A 线此前从不传 intimacy_score → 融合恒跳过）。
            _intimacy_score = _resolve_intimacy_score(self.account_id, chat_id)
            _funnel_stage = _resolve_funnel_stage(self.account_id, chat_id)
            # 语音转录：AI 只需看纯文本，剥掉 [语音转录] 前缀标记
            _VOICE_PREFIX = "[语音转录] "
            ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text

            # 调用Skill管理器处理消息，传递上下文分析结果、图片 OCR、机器人消息、群名、request_id、情绪、发群消息回调（供 gxp 代发命令等）
            _sm_context = {
                'chat_id': chat_id,
                'chat_title': chat_title,
                'context_analysis': context_analysis,
                'image_ocr_text': image_ocr_text,
                'recent_bot_messages': recent_bot_messages,
                'request_id': request_id,
                'user_emotion_hint': user_emotion_hint,
                'triggered_by_mention': triggered_by_mention,
                '_trigger_path': message_data.get('_trigger_path'),
                '_send_to_chat': self.send_message,
                '_send_photo_to_chat': self.send_photo,
                '_record_gxp_cmd': self.record_gxp_command,
                '_i18n': self.i18n,
                '_event_tracker': self.event_tracker,
                'user_id': user_id,
                'user_msg_id': getattr(message, 'id', 0),
                # N 线 核心1：复用共享 companion_context.route_persona_id（A/B 同一套 3-tier 路由）
                'account_persona_id': _route_persona_id(
                    getattr(self, 'account_persona_ids', None), _chat_type_str
                ),
                'is_group': _is_group,
                'chat_type': _chat_type_str or 'private',
                'platform': 'telegram',  # S5: CrossPlatformIdentity
                # 供 skill_manager 剧情收场把 story_complete 镜像进 contacts journey
                # （与 resolve_intimacy_score 用同一 account_id 寻址同一 journey）。
                'account_id': self.account_id,
            }
            # Q3：仅在有值时注入，None 不写键 → 与 RPA 各线一致、向后兼容
            if _intimacy_score is not None:
                _sm_context['intimacy_score'] = _intimacy_score
            if _funnel_stage:
                _sm_context['funnel_stage'] = _funnel_stage
            reply_text = await self.skill_manager.process_message(
                text=ai_text,
                user_id=user_id,
                context=_sm_context,
            )
            
            # 情绪增强（仅在启用时）
            enhanced_reply = reply_text
            emoticons_config = self.config.get('emoticons', {})
            
            # 检查情绪增强器是否启用且实例存在
            if reply_text and self.emotion_enhancer and emoticons_config.get('enabled', True):
                try:
                    # 分析消息情绪（独立于上下文）
                    emotion_analysis = self.emotion_enhancer.analyze_message_emotion(text)
                    
                    # 增强回复
                    enhanced_reply = self.emotion_enhancer.enhance_reply(
                        original_reply=reply_text,
                        emotion=emotion_analysis.get('emotion', 'neutral'),
                        context_analysis=context_analysis or {},
                        message_text=text,
                        chat_id=str(chat_id),
                    )
                    
                    if enhanced_reply != reply_text:
                        self.logger.info(f"情绪增强应用成功: {enhanced_reply[:80]}...")
                    else:
                        self.logger.debug("情绪增强未修改回复内容")
                        
                except Exception as e:
                    self.logger.warning(f"情绪增强失败: {e}")
                    enhanced_reply = reply_text

            # conversion 域：短探询「在吗」等去掉客服台套话（在情绪增强之后）
            if enhanced_reply:
                enhanced_reply = self._rewrite_companion_helpdesk_ping(
                    enhanced_reply, text
                )
            
            # 记录处理完成时间
            process_time = time.time()
            
            # 术语后处理：按 config ai.terminology 统一表述后再发送
            reply_final = self._apply_terminology(enhanced_reply) if enhanced_reply else ""
            suffix = ""
            he_forward_spec = None
            if reply_final and self._human_escalation and _es_key:
                try:
                    self._human_escalation.reload_config(
                        self.config.config if hasattr(self.config, "config") else {}
                    )
                    _chat_un = None
                    try:
                        if message and getattr(message, "chat", None):
                            _chat_un = getattr(message.chat, "username", None)
                    except Exception:
                        _chat_un = None
                    _umid = getattr(message, "id", None)
                    he_out = self._human_escalation.format_suffix_if_needed(
                        chat_id,
                        user_id,
                        _es_cnt,
                        _es_key,
                        user_message_id=int(_umid) if _umid is not None else None,
                        user_text=text,
                        chat_username=_chat_un,
                        chat_title=chat_title or None,
                    )
                    if he_out.suffix:
                        suffix = he_out.suffix
                    he_forward_spec = he_out.forward_spec
                except Exception as ex:
                    self.logger.warning("人工转接追加文案失败: %s", ex)
            suffix_html = bool(suffix and ("<a href" in suffix))

            def _apply_suffix_chunks(chunks_in: List[str]) -> List[str]:
                if not suffix or not chunks_in:
                    return chunks_in
                out = list(chunks_in)
                if suffix_html:
                    if len(out) > 1:
                        for i in range(len(out) - 1):
                            out[i] = _html_escape(out[i])
                        out[-1] = _html_escape(out[-1]) + suffix
                    else:
                        out[-1] = _html_escape(out[-1]) + suffix
                else:
                    out[-1] = out[-1] + suffix
                return out

            _parse_mode = ParseMode.HTML if (PYROGRAM_AVAILABLE and suffix_html) else None

            # 语音回复：如果启用且触发条件满足，先尝试发语音；成功则跳过文字
            _is_voice_msg = bool(message_data.get('_is_voice_msg'))
            _voice_sent = False
            if reply_final:
                try:
                    _voice_sent = await self._maybe_send_voice_reply(
                        message, reply_final, is_peer_voice=_is_voice_msg
                    )
                except Exception as _ve:
                    self.logger.warning("[voice_reply] probe failed: %s", _ve)

            # 如果有回复，发送消息（言简意赅：长回复可分条发送）
            if reply_final and not _voice_sent:
                sent_text_for_context = reply_final
                split_cfg = self.config.get("reply", {}).get("split_send", {})
                if split_cfg.get("enabled", False):
                    max_chars = int(split_cfg.get("max_chars_per_message", 120))
                    min_seg = int(split_cfg.get("min_segments_to_split", 2))
                    delay = float(split_cfg.get("delay_between_seconds", 0.35))
                    chunks = self._split_reply_for_send(reply_final, max_chars, min_seg)
                    chunks = _apply_suffix_chunks(chunks)
                    sent_text_for_context = "\n\n".join(chunks)
                    if len(chunks) > 1:
                        for i, chunk in enumerate(chunks):
                            await self._send_reply(message, chunk, parse_mode=_parse_mode)
                            if i < len(chunks) - 1 and delay >= 0:
                                jitter = float(split_cfg.get("delay_jitter_seconds", 0) or 0)
                                await asyncio.sleep(delay + (random.uniform(0, jitter) if jitter > 0 else 0))
                    else:
                        await self._send_reply(message, chunks[0], parse_mode=_parse_mode)
                else:
                    if suffix:
                        if suffix_html:
                            sent_text_for_context = _html_escape(reply_final) + suffix
                        else:
                            sent_text_for_context = reply_final + suffix
                    else:
                        sent_text_for_context = reply_final
                    await self._send_reply(
                        message, sent_text_for_context, parse_mode=_parse_mode
                    )
                send_time = time.time()
                total_time_ms = (send_time - start_time) * 1000
                m = _metrics()
                if m:
                    m.record_reply()
                    m.record_response_time_ms(total_time_ms)
                    m.set_queue_size(self.message_queue.qsize())
                
                # 如果启用了上下文管理器，记录AI回复（存术语校正后的内容）
                if self.context_manager:
                    try:
                        self.context_manager.add_message(
                            chat_id=chat_id,
                            user_id=0,  # AI用户
                            username="小灵",
                            text=sent_text_for_context,
                            is_ai=True
                        )
                    except Exception as e:
                        self.logger.warning(f"记录AI回复到上下文失败: {e}")
                
                # 记录响应时间统计
                total_time = send_time - start_time
                process_duration = process_time - receive_time
                send_duration = send_time - process_time
                self.logger.info(
                    f"响应时间统计 - 总计: {total_time:.2f}s, "
                    f"处理: {process_duration:.2f}s, "
                    f"发送: {send_duration:.2f}s"
                )

                if he_forward_spec:
                    try:
                        await self._forward_escalation_user_to_agents(he_forward_spec)
                    except Exception as fwd_ex:
                        self.logger.warning(
                            "人工转接: 向客服私聊转发用户原话失败: %s", fwd_ex
                        )

            elif reply_final and _voice_sent:
                # Voice was sent — still record metrics & context
                send_time = time.time()
                total_time_ms = (send_time - start_time) * 1000
                m = _metrics()
                if m:
                    m.record_reply()
                    m.record_response_time_ms(total_time_ms)
                    m.set_queue_size(self.message_queue.qsize())
                if self.context_manager:
                    try:
                        self.context_manager.add_message(
                            chat_id=chat_id,
                            user_id=0,
                            username="小灵",
                            text=reply_final,
                            is_ai=True,
                        )
                    except Exception as _e:
                        self.logger.warning("记录语音回复到上下文失败: %s", _e)

                if he_forward_spec:
                    try:
                        await self._forward_escalation_user_to_agents(he_forward_spec)
                    except Exception as fwd_ex:
                        self.logger.warning(
                            "人工转接: 向客服私聊转发用户原话失败: %s", fwd_ex
                        )
                
        except Exception as e:
            self.logger.error(f"异步处理消息失败: {e}")
            m = _metrics()
            if m:
                m.record_error()

    # ── GXP 命令结果追踪 + 超时提醒（队列化，支持同群并发） ────

    _GXP_TIMEOUT_SEC = 60
    _GXP_MAX_QUEUE = 10

    _CMD_HINT_MAP = {
        "/cxye": r"余额|balance",
        "/hl": r"汇率|exchange|rate",
        "/cgl": r"成功率|success",
        "/cxds": r"代收|collection|deposit",
        "/cxdf": r"提现|withdraw|payout",
        "/utr": r"utr|UTR|补单",
        "/htds": r"回调.*代收|callback.*deposit",
        "/htdf": r"回调.*提现|callback.*withdraw",
    }

    def record_gxp_command(self, chat_id: int, cmd: str, user_id: int = 0, user_msg_id: int = 0):
        ts = time.time()
        entry = {"cmd": cmd, "ts": ts, "user_id": user_id, "user_msg_id": user_msg_id}
        queue = self._gxp_pending.setdefault(chat_id, [])
        queue.append(entry)
        if len(queue) > self._GXP_MAX_QUEUE:
            queue[:] = queue[-self._GXP_MAX_QUEUE:]
        cutoff = ts - 120
        for cid in [c for c, q in self._gxp_pending.items() if not q or q[-1]["ts"] < cutoff]:
            del self._gxp_pending[cid]
        try:
            asyncio.get_running_loop().create_task(self._gxp_timeout_check(chat_id, ts, cmd))
        except RuntimeError:
            pass

    async def _gxp_timeout_check(self, chat_id: int, original_ts: float, cmd: str):
        await asyncio.sleep(self._GXP_TIMEOUT_SEC + 2)
        queue = self._gxp_pending.get(chat_id, [])
        idx = next((i for i, e in enumerate(queue) if e["ts"] == original_ts), -1)
        if idx < 0:
            return
        entry = queue.pop(idx)
        if not queue:
            self._gxp_pending.pop(chat_id, None)
        msg_id = entry.get("user_msg_id") or None
        try:
            text = self.i18n.t("gxp_timeout", chat_id, sec=self._GXP_TIMEOUT_SEC, cmd=cmd)
            await self.client.send_message(chat_id=chat_id, text=text, reply_to_message_id=msg_id)
            self.logger.info("[GXP追踪] 超时提醒已发送: %s", cmd)
        except Exception as e:
            self.logger.warning("[GXP追踪] 超时提醒发送失败: %s", e)

    def _match_pending_by_hint(self, queue: list, bot_text: str) -> int:
        """尝试根据 bot 回复内容精确匹配队列中的命令，返回 index；-1 表示无匹配"""
        for i, entry in enumerate(queue):
            cmd_prefix = entry["cmd"].split()[0] if entry["cmd"] else ""
            pattern = self._CMD_HINT_MAP.get(cmd_prefix)
            if pattern and re.search(pattern, bot_text, re.IGNORECASE):
                return i
        return -1

    async def _handle_gxp_bot_reply(self, message):
        chat_id = message.chat.id
        queue = self._gxp_pending.get(chat_id, [])
        if not queue:
            return
        bot_text = (message.text or message.caption or "").strip()
        if not bot_text:
            return
        idx = self._match_pending_by_hint(queue, bot_text)
        if idx < 0:
            idx = 0
        entry = queue.pop(idx)
        if not queue:
            self._gxp_pending.pop(chat_id, None)
        if time.time() - entry["ts"] > self._GXP_TIMEOUT_SEC:
            return
        if re.search(r"查询失败|不存在|无此订单|已过期|failed|not found", bot_text):
            relay = self.i18n.t("gxp_result_fail", chat_id, text=bot_text[:200])
        elif re.search(r"查询成功|操作成功|success", bot_text):
            relay = self.i18n.t("gxp_result_ok", chat_id, text=bot_text[:500])
        else:
            relay = self.i18n.t("gxp_result_other", chat_id, text=bot_text[:500])
        try:
            await self.client.send_message(
                chat_id=chat_id,
                text=relay,
                reply_to_message_id=entry.get("user_msg_id") or None,
            )
            self.logger.info("[GXP追踪] 已转告结果 (queue_remaining=%d): %s...", len(queue), relay[:80])
        except Exception as e:
            self.logger.warning("[GXP追踪] 转告失败: %s", e)

        await self._check_success_rate_alert(chat_id, bot_text, entry.get("cmd", ""))

    # ── 通道成功率告警 ──────────────────────────────────────────

    async def _check_success_rate_alert(self, chat_id: int, bot_text: str, cmd: str):
        if "/cgl" not in cmd:
            return
        alert_cfg = self.config.get("channel_alerts", {})
        if not alert_cfg.get("enabled"):
            return
        threshold = float(alert_cfg.get("success_rate_threshold", 80))
        m = re.search(r"(\d+\.?\d*)%", bot_text)
        if not m:
            return
        rate = float(m.group(1))
        if rate >= threshold:
            return
        admin_chat = self.config.get("telegram", {}).get("admin_chat_id")
        alert_text = (
            f"⚠️ 成功率告警\n"
            f"当前成功率: {rate}%（阈值: {threshold}%）\n"
            f"来源: {bot_text[:150]}"
        )
        self.logger.warning("[告警] 成功率 %.1f%% < 阈值 %.1f%%", rate, threshold)
        if admin_chat:
            try:
                await self.client.send_message(chat_id=int(admin_chat), text=alert_text)
            except Exception as e:
                self.logger.warning("[告警] 发送失败: %s", e)
        self.event_tracker.track("alert_success_rate", chat_id, detail=f"{rate}%<{threshold}%")

    # ── 配置热重载通知 ──────────────────────────────────────────

    def _register_reload_notifier(self):
        admin_chat_id = self.config.get('telegram', {}).get('admin_chat_id')
        if not admin_chat_id:
            self.logger.debug("未配置 admin_chat_id，热重载通知已跳过")
            return

        def _on_reload():
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._send_reload_notification(int(admin_chat_id)))
            except Exception:
                pass

        self.config.on_reload(_on_reload)
        self.logger.info("热重载通知已注册 → chat_id=%s", admin_chat_id)

    async def _send_reload_notification(self, chat_id: int):
        try:
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            text = self.i18n.t("reload_notify", chat_id, ts=ts)
            await self.client.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            self.logger.warning("热重载通知发送失败: %s", e)

    # ── 定时任务调度 ────────────────────────────────────────────

    def _start_scheduler(self):
        from src.utils.scheduler import TaskScheduler
        cfg = self.config.config if hasattr(self.config, 'config') else {}
        self._scheduler = TaskScheduler.from_config(cfg, self._scheduled_send)
        if self._scheduler._tasks:
            self._scheduler.start()

    async def _scheduled_send(self, chat_id: int, command: str):
        try:
            await self.client.send_message(chat_id=chat_id, text=command)
            self.logger.info("[定时任务] 已发送: chat=%s cmd=%s", chat_id, command)
        except Exception as e:
            self.logger.warning("[定时任务] 发送失败: %s", e)
