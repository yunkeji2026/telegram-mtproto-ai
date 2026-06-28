"""
配置管理器
负责加载、验证和管理配置文件
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging


class ConfigManager:
    """配置管理器类"""
    
    def __init__(self, config_path: str = None):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径，如果为None则使用默认路径
        """
        # logger 必须先于 _get_default_config_path()，后者在回退到 example 配置时
        # 会用 self.logger.warning（纯净 checkout 无 config.yaml 时即触发）。
        self.logger = logging.getLogger(__name__)
        self.config_path = Path(config_path) if config_path else self._get_default_config_path()
        self.config: Dict[str, Any] = {}
        self._quota_rules_cache: Optional[Dict[str, Any]] = None
        self._quota_rules_mtime: float = 0
        self._templates_cache: Optional[Dict[str, Any]] = None
        self._templates_mtime: float = 0
        self._exchange_rates_cache: Optional[Dict[str, Any]] = None
        self._exchange_rates_mtime: float = 0
        self._strategies_cache: Optional[Dict[str, Any]] = None
        self._strategies_mtime: float = 0
        self._config_mtime: float = 0
        self._hot_reload_interval: float = 30.0
        self._last_hot_reload_check: float = 0
        self._on_reload_callbacks: list = []
    
    def _get_default_config_path(self) -> Path:
        """获取默认配置文件路径。

        解析优先级：
        1. ``AITR_CONFIG_PATH`` 环境变量（显式指向 config.yaml）——打包/自包含部署用，
           桌面端把它指向用户**可写目录**，避免写进只读的安装包。
        2. ``AITR_DATA_DIR`` 环境变量（数据根）——config = ``$AITR_DATA_DIR/config/config.yaml``。
        3. 仓库默认 ``<repo>/config/config.yaml``（开发态，行为不变）。

        命中 1/2 时若目标不存在，会从**内置 example** 自播种到该可写路径（见 ``_ensure_seeded``），
        使首次运行即有可编辑的 config，无需 launcher 额外搬运。
        """
        env_path = os.environ.get("AITR_CONFIG_PATH")
        if env_path:
            target = Path(env_path).expanduser()
            self._ensure_seeded(target)
            return target

        env_dir = os.environ.get("AITR_DATA_DIR")
        if env_dir:
            target = Path(env_dir).expanduser() / "config" / "config.yaml"
            self._ensure_seeded(target)
            return target

        # 优先使用仓库 config/config.yaml
        current_dir = Path(__file__).parent.parent.parent
        config_file = current_dir / "config" / "config.yaml"

        # 如果不存在，使用config.example.yaml
        if not config_file.exists():
            example_file = current_dir / "config" / "config.example.yaml"
            if example_file.exists():
                self.logger.warning(f"配置文件 {config_file} 不存在，请复制 {example_file} 并编辑")
                return example_file

        return config_file

    def _bundled_example_path(self) -> Optional[Path]:
        """内置 example 配置路径（开发态=仓库 config/；打包态=冻结资源内 config/）。

        PyInstaller onedir 下 ``__file__`` 落在 ``_internal/src/utils/...``，故
        parent.parent.parent 即 ``_internal``，与 ``--add-data config/...`` 落点一致。
        """
        try:
            base = Path(__file__).resolve().parent.parent.parent
            ex = base / "config" / "config.example.yaml"
            return ex if ex.exists() else None
        except Exception:
            return None

    def _ensure_seeded(self, target: Path) -> None:
        """打包/自包含部署：目标 config 不存在时，从内置 example 播种到用户可写目录。

        失败永不抛（仅告警）；无内置 example 时不创建（load() 会报缺失并提示）。
        """
        try:
            if target.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            ex = self._bundled_example_path()
            if ex and ex.exists():
                import shutil
                shutil.copyfile(str(ex), str(target))
                self.logger.warning("配置不存在，已从内置示例播种到可写目录: %s", target)
            else:
                self.logger.warning("配置不存在且无内置 example，可写目录仍缺 config: %s", target)
        except Exception as exc:
            self.logger.warning("配置播种失败（忽略）: %s", exc)
    
    async def load(self) -> bool:
        """加载配置文件"""
        try:
            if not self.config_path.exists():
                self.logger.error(f"配置文件不存在: {self.config_path}")
                return False
            
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}

            # P1-1：凭证 overlay（config.local.yaml）深合并覆盖在主配置之上。
            # 接入向导只写这个小文件 → 主 config.yaml 的注释/结构永不被改写，
            # 且密钥与 git 跟踪文件分离（overlay 应进 .gitignore）。
            self._merge_overlay()

            # 打包/自包含部署：用 AITR_WEB_* 覆盖 web_admin.{host,port,auth_token}，
            # 使后端「serve 的端口/令牌」与桌面壳 renderer「talk 的 base_url/token」强一致，
            # 无需改随包 example（server 端口/令牌保持 canonical）。开发/server 态无 env→零影响。
            self._apply_env_overrides()

            # 验证配置
            if not self._validate_config():
                return False
            
            self._config_mtime = os.path.getmtime(self.config_path)
            self.logger.info(f"配置文件加载成功: {self.config_path}")
            # P0-1：非阻断启动自检 — 把 error/warn 摘要打到日志，引导修复错配，
            # 但不改变启动成败（严格 gate 走 `python main.py --check`）。
            self._run_startup_self_check()
            return True
            
        except yaml.YAMLError as e:
            self.logger.error(f"配置文件YAML格式错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"加载配置文件失败: {e}")
            return False
    
    @staticmethod
    def _env_truthy(name: str) -> bool:
        return str(os.environ.get(name) or "").strip().lower() in (
            "1", "true", "yes", "on")

    def _apply_env_overrides(self) -> None:
        """以 ``AITR_WEB_*`` 环境变量覆盖 ``web_admin.{host,port,auth_token}``（仅当设置时）。

        桌面/打包态由 launcher（``backend-launcher.js``）注入，保证后端 serve 与 renderer
        调用的 host/port/token 一致；server/开发态不设这些 env，行为不变。

        另：``AITR_DESKTOP_MODE`` 为真时**强制** ``web_admin.enabled=true``——统一收件箱 /
        翻译 / D1 选择器热更新 / D4 受控外发等路由都挂在 web 后台下，桌面壳没有它即不可用，
        故不依赖随包 example 是否显式写了 ``enabled``。
        """
        desktop = self._env_truthy("AITR_DESKTOP_MODE")
        host = os.environ.get("AITR_WEB_HOST")
        port = os.environ.get("AITR_WEB_PORT")
        token = os.environ.get("AITR_WEB_TOKEN")
        if not (desktop or host or port or token):
            return
        web = self.config.get("web_admin")
        if not isinstance(web, dict):
            web = {}
            self.config["web_admin"] = web
        if host:
            web["host"] = host
        if port:
            try:
                web["port"] = int(str(port).strip())
            except (TypeError, ValueError):
                self.logger.warning("AITR_WEB_PORT 非法（忽略）: %r", port)
        if token:
            web["auth_token"] = token
        if desktop:
            web["enabled"] = True

    def _overlay_path(self) -> Path:
        """凭证 overlay 路径：主配置同目录下的 config.local.yaml。"""
        return self.config_path.parent / "config.local.yaml"

    @staticmethod
    def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
        """递归合并 over 到 base（就地改 base）；dict 递归，其余以 over 覆盖。"""
        for k, v in (over or {}).items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def _merge_overlay(self) -> None:
        """若存在 config.local.yaml，深合并覆盖到 self.config（缺失/损坏则静默跳过）。"""
        path = self._overlay_path()
        try:
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                overlay = yaml.safe_load(f) or {}
            if isinstance(overlay, dict) and overlay:
                self._deep_merge(self.config, overlay)
                self.logger.info("已合并凭证 overlay: %s", path)
        except Exception as exc:
            self.logger.warning("凭证 overlay 合并失败（忽略）: %s", exc)

    def save_channel_credentials(
        self, channel: str, values: Dict[str, Any],
    ) -> tuple:
        """P1-1 接入向导：把某渠道的凭证字段写入 config.local.yaml 并即时生效。

        只接受 channel_setup 声明的已知字段（防注入任意键），写 overlay（保住主
        config 注释），随后深合并进 self.config 让本进程立即可见。
        返回 (成功?, 说明, issues:list)。
        """
        try:
            from src.utils.channel_setup import apply_channel_values
        except Exception as exc:
            return False, f"channel_setup 不可用: {exc}", []
        path = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            ok, msg = apply_channel_values(overlay, channel, values)
            if not ok:
                return False, msg, []
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 接入向导写入的凭证 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(path)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入凭证 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}", []
        issues = []
        try:
            from src.utils.config_check import check_config
            issues = check_config(self.config, config_path=self.config_path)
        except Exception:
            self.logger.debug("写入后自检失败（忽略）", exc_info=True)
        return True, "已保存", issues

    def save_branding(self, values: Dict[str, Any]) -> tuple:
        """C1-1 白标：把品牌字段写入 config.local.yaml 的 ``brand:`` 段并即时生效。

        只接受白名单字段（防注入任意键），写 overlay（保住主 config 注释），随后深合并
        进 self.config 让本进程立即可见。返回 (成功?, 说明)。
        """
        allowed = {
            "site_name", "site_name_short", "primary_color",
            "logo_url", "login_subtitle", "hide_powered_by",
        }
        clean: Dict[str, Any] = {}
        for k, v in (values or {}).items():
            if k not in allowed:
                continue
            if k == "hide_powered_by":
                clean[k] = bool(v)
            else:
                clean[k] = ("" if v is None else str(v)).strip()
        path = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            brand = dict(overlay.get("brand") or {})
            brand.update(clean)
            overlay["brand"] = brand
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 白标/凭证 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(path)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入品牌 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}"
        return True, "已保存"

    def set_overlay_flag(self, path: str, value: Any) -> tuple:
        """把单个开关写入 config.local.yaml overlay 并即时生效（陪伴能力分阶段开启用）。

        走 overlay 而非改写 config.yaml：保住主配置注释/结构、与凭证/白标同机制，重启后
        load() 再次深合并。``path`` 为点分隔嵌套键（如 ``companion.proactive_topic.enabled``）。
        返回 (成功?, 说明)。调用方（看板路由）已用能力注册表白名单约束 path，避免任意键注入。
        """
        keys = [k for k in str(path or "").split(".") if k]
        if not keys:
            return False, "空配置路径"
        p = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            if not isinstance(overlay, dict):
                overlay = {}
            node = overlay
            for k in keys[:-1]:
                nxt = node.get(k)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[k] = nxt
                node = nxt
            node[keys[-1]] = value
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 运营开关 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(p)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入开关 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}"
        self.logger.info("运营开关已更新: %s = %r", ".".join(keys), value)
        return True, "已保存"

    def _run_startup_self_check(self) -> None:
        """启动时跑配置自检并把 error/warn 摘要写日志（永不抛、永不阻断启动）。"""
        try:
            from src.utils.config_check import check_config
        except Exception:
            return
        try:
            issues = check_config(self.config, config_path=self.config_path)
        except Exception as exc:
            self.logger.debug("配置自检执行异常（忽略）: %s", exc)
            return
        errors = [i for i in issues if i.severity == "error"]
        warns = [i for i in issues if i.severity == "warn"]
        for i in errors:
            self.logger.error("配置自检[错误] %s: %s", i.path, i.message)
        for i in warns:
            self.logger.warning("配置自检[警告] %s: %s", i.path, i.message)
        if errors or warns:
            self.logger.warning(
                "配置自检发现 %d 错误 / %d 警告；详情运行 `python main.py --check`",
                len(errors), len(warns))

    def _validate_config(self) -> bool:
        """验证配置文件"""
        required_sections = ['telegram', 'ai', 'skills']
        
        # 检查必需的部分
        for section in required_sections:
            if section not in self.config:
                self.logger.error(f"配置缺少必需部分: {section}")
                return False
        
        # 验证Telegram配置
        telegram_config = self.config.get('telegram', {})
        required_telegram_keys = ['api_id', 'api_hash', 'phone_number']
        
        for key in required_telegram_keys:
            if key not in telegram_config:
                self.logger.error(f"Telegram配置缺少必需键: {key}")
                return False
            
            value = telegram_config[key]
            if value == f"YOUR_{key.upper()}" or not value:
                self.logger.error(f"请配置有效的Telegram {key}")
                return False
        
        # 验证AI配置
        ai_config = self.config.get('ai', {})
        if 'api_key' not in ai_config or ai_config['api_key'] == "YOUR_AI_API_KEY":
            self.logger.error("请配置有效的 AI API 密钥")
            return False
        
        return True
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点分隔的嵌套键"""
        keys = key.split('.')
        value = self.config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any) -> None:
        """设置配置值，支持点分隔的嵌套键"""
        keys = key.split('.')
        config = self.config
        
        # 遍历到倒数第二个键
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        # 设置最后一个键的值
        config[keys[-1]] = value
    
    def save(self) -> bool:
        """保存配置到文件"""
        try:
            # 确保配置目录存在
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
            self.logger.info(f"配置已保存到: {self.config_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")
            return False
    
    def get_telegram_config(self) -> Dict[str, Any]:
        """获取Telegram配置"""
        return self.config.get('telegram', {})
    
    def get_ai_config(self) -> Dict[str, Any]:
        """获取AI配置"""
        return self.config.get('ai', {})

    def get_line_rpa_config(self) -> Dict[str, Any]:
        """个人 LINE 客户端 RPA（ADB）；可选，默认空。"""
        return self.config.get("line_rpa") or {}

    def get_messenger_rpa_config(self) -> Dict[str, Any]:
        """Facebook Messenger RPA（ADB + Vision）；可选，默认空。"""
        return self.config.get("messenger_rpa") or {}

    def get_facebook_messenger_config(self) -> Dict[str, Any]:
        """Facebook Page Messenger Webhook（官方 Graph API）；可选，默认空。"""
        return self.config.get("facebook_messenger") or {}
    
    def get_skills_config(self) -> Dict[str, Any]:
        """获取Skills配置"""
        return self.config.get('skills', {})
    
    def get_intent_config(self) -> Dict[str, Any]:
        """获取意图识别配置"""
        return self.config.get('intent', {})
    
    def get_templates_config(self) -> Dict[str, Any]:
        """获取模板配置"""
        return self.config.get('templates', {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.config.get('logging', {})
    
    def get_dynamic_templates_config(self) -> Dict[str, Any]:
        """加载动态话术模板配置（config/templates.yaml），支持热更新（按文件 mtime 重载）"""
        templates_file = self.config_path.parent / "templates.yaml"
        if not templates_file.exists():
            # 回退：按 config_manager 所在包路径找 config/templates.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                templates_file = base / "config" / "templates.yaml"
            except Exception:
                pass
        if not templates_file.exists():
            self.logger.debug("动态话术模板文件不存在，将使用主配置中的模板: %s", getattr(templates_file, 'as_posix', str)(templates_file))
            return {}
        try:
            mtime = os.path.getmtime(templates_file)
            if self._templates_cache is not None and mtime <= self._templates_mtime:
                return self._templates_cache
            with open(templates_file, 'r', encoding='utf-8') as f:
                self._templates_cache = yaml.safe_load(f) or {}
            self._templates_mtime = mtime
            template_keys = list(self._templates_cache.keys())
            self.logger.info("动态话术模板已加载: 模板类别数=%s, 文件=%s", len(template_keys), templates_file.name)
            return self._templates_cache
        except Exception as e:
            self.logger.warning("加载动态话术模板失败: %s", e)
            return self._templates_cache if self._templates_cache is not None else {}
    
    def get_exchange_rates_config(self) -> Dict[str, Any]:
        """加载动态汇率配置（config/exchange_rates.yaml），支持热更新（按文件 mtime 重载）"""
        exchange_rates_file = self.config_path.parent / "exchange_rates.yaml"
        if not exchange_rates_file.exists():
            # 回退：按 config_manager 所在包路径找 config/exchange_rates.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                exchange_rates_file = base / "config" / "exchange_rates.yaml"
            except Exception:
                pass
        if not exchange_rates_file.exists():
            self.logger.debug("动态汇率配置文件不存在: %s", getattr(exchange_rates_file, 'as_posix', str)(exchange_rates_file))
            return {}
        try:
            mtime = os.path.getmtime(exchange_rates_file)
            if self._exchange_rates_cache is not None and mtime <= self._exchange_rates_mtime:
                return self._exchange_rates_cache
            with open(exchange_rates_file, 'r', encoding='utf-8') as f:
                self._exchange_rates_cache = yaml.safe_load(f) or {}
            self._exchange_rates_mtime = mtime
            channels = self._exchange_rates_cache.get("channels") or {}
            self.logger.info("动态汇率配置已加载: 通道数=%s, 文件=%s", len(channels), exchange_rates_file.name)
            return self._exchange_rates_cache
        except Exception as e:
            self.logger.warning("加载动态汇率配置失败: %s", e)
            return self._exchange_rates_cache if self._exchange_rates_cache is not None else {}
    
    async def reload(self) -> bool:
        """重新加载配置文件"""
        self.config = {}
        self._quota_rules_cache = None
        self._quota_rules_mtime = 0
        self._templates_cache = None
        self._templates_mtime = 0
        self._exchange_rates_cache = None
        self._exchange_rates_mtime = 0
        self._strategies_cache = None
        self._strategies_mtime = 0
        return await self.load()

    _HOT_RELOAD_PROTECTED_KEYS = {"api_id", "api_hash", "phone_number", "session_name"}

    @staticmethod
    def _validate_hot_reload_config(data) -> str:
        """热重载校验：检查配置结构完整性，返回拒绝原因（空字符串=通过）"""
        if not isinstance(data, dict):
            return "配置不是有效字典"
        if "telegram" not in data:
            return "缺少 telegram 节点"
        tg = data["telegram"]
        if not isinstance(tg, dict):
            return "telegram 节点不是字典"
        ai = data.get("ai", {})
        if ai and not isinstance(ai, dict):
            return "ai 节点不是字典"
        for key in ("max_tokens",):
            val = ai.get(key)
            if val is not None:
                try:
                    v = int(val)
                    if v <= 0:
                        return f"ai.{key} 必须为正整数，当前值: {val}"
                except (ValueError, TypeError):
                    return f"ai.{key} 不是有效数字: {val}"
        reply_cfg = data.get("reply", {})
        if reply_cfg and not isinstance(reply_cfg, dict):
            return "reply 节点不是字典"
        strategies = reply_cfg.get("strategies", {})
        if strategies and not isinstance(strategies, dict):
            return "reply.strategies 不是字典"
        return ""

    def on_reload(self, callback) -> None:
        """注册热重载回调。callback() 在配置成功重载后同步调用。"""
        self._on_reload_callbacks.append(callback)

    def check_and_hot_reload(self) -> bool:
        """检查 config.yaml 是否修改，若有则热重载（保护不可变字段）。
        返回 True 表示发生了重载。适合在消息处理主循环中定期调用。"""
        import time as _t
        now = _t.time()
        if now - self._last_hot_reload_check < self._hot_reload_interval:
            return False
        self._last_hot_reload_check = now
        try:
            if not self.config_path.exists():
                return False
            mtime = os.path.getmtime(self.config_path)
            if mtime <= self._config_mtime:
                return False
            with open(self.config_path, 'r', encoding='utf-8') as f:
                new_data = yaml.safe_load(f)
            reject_reason = self._validate_hot_reload_config(new_data)
            if reject_reason:
                self.logger.warning("热重载拒绝: %s", reject_reason)
                return False
            old_tg = self.config.get('telegram', {})
            new_tg = new_data.get('telegram', {})
            for key in self._HOT_RELOAD_PROTECTED_KEYS:
                if key in old_tg:
                    new_tg[key] = old_tg[key]
            new_data['telegram'] = new_tg
            self.config = new_data
            self._config_mtime = mtime
            self._quota_rules_cache = None
            self._quota_rules_mtime = 0
            self._templates_cache = None
            self._templates_mtime = 0
            self._exchange_rates_cache = None
            self._exchange_rates_mtime = 0
            self.logger.info("配置热重载完成 (config.yaml mtime=%s)", mtime)
            for cb in self._on_reload_callbacks:
                try:
                    cb()
                except Exception as cb_err:
                    self.logger.debug("热重载回调异常: %s", cb_err)
            return True
        except Exception as e:
            self.logger.warning("配置热重载失败: %s", e)
            return False

    def get_quota_rules(self) -> Dict[str, Any]:
        """加载额度规则配置（config/quota_rules.yaml），支持热更新（按文件 mtime 重载）"""
        quota_file = self.config_path.parent / "quota_rules.yaml"
        if not quota_file.exists():
            # 回退：按 config_manager 所在包路径找 config/quota_rules.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                quota_file = base / "config" / "quota_rules.yaml"
            except Exception:
                pass
        if not quota_file.exists():
            self.logger.debug("额度规则文件不存在，将使用 AI 回复额度类问题: %s", getattr(quota_file, 'as_posix', str)(quota_file))
            return {}
        try:
            mtime = os.path.getmtime(quota_file)
            if self._quota_rules_cache is not None and mtime <= self._quota_rules_mtime:
                return self._quota_rules_cache
            with open(quota_file, 'r', encoding='utf-8') as f:
                self._quota_rules_cache = yaml.safe_load(f) or {}
            self._quota_rules_mtime = mtime
            channels = self._quota_rules_cache.get("channels") or {}
            self.logger.info("额度规则已加载: 通道数=%s, 文件=%s", len(channels), quota_file.name)
            return self._quota_rules_cache
        except Exception as e:
            self.logger.warning("加载额度规则失败: %s", e)
            return self._quota_rules_cache if self._quota_rules_cache is not None else {}

    def get_quota_rules_file_path(self) -> Optional[Path]:
        """返回 quota_rules.yaml 的路径，供对话命令写回配置使用；不存在则返回 None"""
        quota_file = self.config_path.parent / "quota_rules.yaml"
        if not quota_file.exists():
            try:
                base = Path(__file__).resolve().parent.parent.parent
                quota_file = base / "config" / "quota_rules.yaml"
            except Exception:
                pass
        return quota_file if quota_file.exists() else None

    def invalidate_quota_rules_cache(self) -> None:
        """使额度规则缓存失效，下次 get_quota_rules 将重新从文件加载"""
        self._quota_rules_cache = None
        self._quota_rules_mtime = 0

    def update_quota_rules_special_groups(
        self, add: Optional[list] = None, remove: Optional[list] = None
    ) -> tuple:
        """
        增删特殊客户群名单并写回 quota_rules.yaml。
        add/remove 为群名字符串列表；返回 (成功?, 说明文案)。
        """
        path = self.get_quota_rules_file_path()
        if not path:
            return False, "未找到 quota_rules.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            groups = list(data.get("special_groups") or [])
            if add:
                for name in add:
                    name = (name or "").strip()
                    if name and name not in groups:
                        groups.append(name)
            if remove:
                for name in remove:
                    groups = [g for g in groups if (g or "").strip() != (name or "").strip()]
            data["special_groups"] = groups
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self.invalidate_quota_rules_cache()
            return True, f"已更新特殊群名单，当前共 {len(groups)} 个"
        except Exception as e:
            self.logger.warning("写回额度规则失败: %s", e)
            return False, f"写回配置失败: {e}"

    def update_quota_rules_blacklist(
        self,
        add_group: Optional[str] = None,
        ep_text: Optional[str] = None,
        jc_text: Optional[str] = None,
        remove_group: Optional[str] = None,
    ) -> tuple:
        """
        添加或删除黑名单群并写回 quota_rules.yaml。
        add_group 时 ep_text/jc_text 为可选话术；remove_group 为要删除的群名。返回 (成功?, 说明文案)。
        """
        path = self.get_quota_rules_file_path()
        if not path:
            return False, "未找到 quota_rules.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            blacklist = dict(data.get("blacklist_groups") or {})
            if remove_group:
                key = (remove_group or "").strip()
                if key in blacklist:
                    del blacklist[key]
                data["blacklist_groups"] = blacklist
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                self.invalidate_quota_rules_cache()
                return True, f"已删除黑名单群：{remove_group}"
            if add_group:
                key = (add_group or "").strip()
                if not key:
                    return False, "群名为空"
                ep_text = (ep_text or "当前EP渠道受限，请使用其他渠道或联系客服处理。").strip()
                jc_text = (jc_text or "当前JC额度请以实际提交为准。").strip()
                blacklist[key] = {"ep": ep_text, "jc": jc_text}
                data["blacklist_groups"] = blacklist
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                self.invalidate_quota_rules_cache()
                return True, f"已添加黑名单群：{key}"
            return False, "请指定 添加黑名单 或 删除黑名单 及群名"
        except Exception as e:
            self.logger.warning("写回额度规则失败: %s", e)
            return False, f"写回配置失败: {e}"

    def invalidate_templates_cache(self) -> None:
        """使动态话术模板缓存失效，下次 get_dynamic_templates_config 将重新从文件加载"""
        self._templates_cache = None
        self._templates_mtime = 0

    def invalidate_exchange_rates_cache(self) -> None:
        """使动态汇率配置缓存失效，下次 get_exchange_rates_config 将重新从文件加载"""
        self._exchange_rates_cache = None
        self._exchange_rates_mtime = 0

    def invalidate_strategies_cache(self) -> None:
        """使策略缓存失效，下次 get_strategies_config 将重新从文件加载"""
        self._strategies_cache = None
        self._strategies_mtime = 0

    def get_strategies_config(self) -> Dict[str, Any]:
        """加载 reply_strategies.yaml，mtime 驱动热更新；文件不存在时从 config.yaml 中提取并自动创建"""
        strategies_file = self.config_path.parent / "reply_strategies.yaml"
        if not strategies_file.exists():
            self._bootstrap_strategies_file(strategies_file)
        if not strategies_file.exists():
            return self.config.get("reply_strategies", {}) or {}
        try:
            mtime = os.path.getmtime(strategies_file)
            if self._strategies_cache is not None and mtime <= self._strategies_mtime:
                return self._strategies_cache
            with open(strategies_file, "r", encoding="utf-8") as f:
                self._strategies_cache = yaml.safe_load(f) or {}
            self._strategies_mtime = mtime
            n = len((self._strategies_cache.get("strategies") or {}))
            self.logger.info("回复策略已加载: %d 个策略, 文件=%s", n, strategies_file.name)
            return self._strategies_cache
        except Exception as e:
            self.logger.warning("加载回复策略失败: %s", e)
            return self._strategies_cache if self._strategies_cache is not None else {}

    def _bootstrap_strategies_file(self, path: Path) -> None:
        """从 config.yaml 的 reply_strategies 节提取数据，写入独立 YAML 文件"""
        rs = self.config.get("reply_strategies")
        if not rs or not isinstance(rs, dict):
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(rs, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self.logger.info("已从 config.yaml 迁移 reply_strategies 到 %s", path.name)
        except Exception as e:
            self.logger.warning("创建 reply_strategies.yaml 失败: %s", e)

    def save_strategies(self, data: Dict[str, Any]) -> tuple:
        """写入 reply_strategies.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self.config_path.parent / "reply_strategies.yaml"
        try:
            tmp = path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_strategies_cache()
            return True, "策略配置已保存"
        except Exception as e:
            self.logger.warning("写回策略配置失败: %s", e)
            if path.with_suffix(".yaml.tmp").exists():
                path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
            return False, f"保存失败: {e}"

    def _get_strategies_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "reply_strategies.yaml"
        return path if path.exists() else None

    def _get_templates_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "templates.yaml"
        return path if path.exists() else None

    def _get_exchange_rates_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "exchange_rates.yaml"
        return path if path.exists() else None

    def save_templates(self, data: Dict[str, Any]) -> tuple:
        """写入 templates.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self._get_templates_file_path()
        if not path:
            return False, "未找到 templates.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_templates_cache()
            return True, "话术模板已保存"
        except Exception as e:
            self.logger.warning("写回话术模板失败: %s", e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False, f"保存失败: {e}"

    # ── Personas canonical config (personas.yaml) ────────────────────────────

    _personas_cache: Optional[Dict[str, Any]] = None
    _personas_mtime: float = 0

    def get_personas_config(self) -> Dict[str, Any]:
        """加载 personas.yaml（运营人设规范定义），mtime 驱动热更新。
        不存在时返回空 dict（无报警，因为是可选文件）。
        """
        path = self.config_path.parent / "personas.yaml"
        if not path.exists():
            return {}
        try:
            mtime = os.path.getmtime(path)
            if self._personas_cache is not None and mtime <= self._personas_mtime:
                return self._personas_cache
            with open(path, "r", encoding="utf-8") as f:
                self._personas_cache = yaml.safe_load(f) or {}
            self._personas_mtime = mtime
            n = len((self._personas_cache.get("profiles") or {}))
            self.logger.info("personas.yaml 已加载: %d 个 profiles", n)
            return self._personas_cache
        except Exception as e:
            self.logger.warning("加载 personas.yaml 失败: %s", e)
            return self._personas_cache if self._personas_cache is not None else {}

    def save_personas(self, data: Dict[str, Any]) -> tuple:
        """将运营人设写入 personas.yaml（P5-C: atomic write, git-trackable canonical config）。
        data 格式: {"profiles": {pid: persona_dict, ...}, "updated_at": "..."}
        返回 (成功?, 说明)
        """
        path = self.config_path.parent / "personas.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)  # validate before replacing
            # P8-A: rotate previous version to .bak (one-step manual undo)
            bak = path.with_suffix(".yaml.bak")
            if path.exists():
                try:
                    path.replace(bak)
                except Exception:
                    pass
            tmp.replace(path)
            self._personas_cache = None
            self._personas_mtime = 0
            n = len((data.get("profiles") or {}))
            self.logger.info("personas.yaml 已保存: %d 个 profiles", n)
            return True, f"personas.yaml 已保存 ({n} 个 profiles)"
        except Exception as e:
            self.logger.warning("保存 personas.yaml 失败: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"保存失败: {e}"

    def get_personas_file_path(self) -> Optional[Path]:
        """返回 personas.yaml 的路径（不论是否存在）。"""
        return self.config_path.parent / "personas.yaml"

    def save_exchange_rates(self, data: Dict[str, Any]) -> tuple:
        """写入 exchange_rates.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self._get_exchange_rates_file_path()
        if not path:
            return False, "未找到 exchange_rates.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_exchange_rates_cache()
            return True, "通道配置已保存"
        except Exception as e:
            self.logger.warning("写回通道配置失败: %s", e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False, f"保存失败: {e}"