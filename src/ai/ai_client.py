"""
AI 大模型 API 客户端
负责对话生成、上下文构建与熔断等。
支持：Google Gemini 原生 API (google-genai)，或 OpenAI 兼容 HTTP API（Ollama 等）。
"""

import asyncio
import json
import re
import time
from collections import deque
from typing import Dict, Any, Optional, List
import logging

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    from openai import AsyncOpenAI
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    AsyncOpenAI = None  # type: ignore
    OPENAI_SDK_AVAILABLE = False

from src.utils.logger import LoggerMixin
from src.utils.domain_policy import effective_domain_name


class AIClient(LoggerMixin):
    """AI 大模型 API 客户端（Gemini 或 OpenAI 兼容 / Ollama）"""

    def __init__(self, config):
        """
        初始化 AI 客户端

        Args:
            config: 配置管理器实例
        """
        self.config = config
        self.client = None  # genai.Client
        self._oa_client = None  # AsyncOpenAI（Ollama / OpenAI 兼容）
        self._oa_embed_client = None  # 可选：仅用于 embedding（如 DeepSeek 对话 + Ollama 向量）
        self._use_openai_compat = False
        self._provider = "gemini"
        self.system_prompt = ""
        self.model = "gemini-2.5-flash"
        self.temperature = 0.7
        self.max_tokens = 1024
        self.timeout = 30
        
        # 性能跟踪
        self.total_calls = 0
        self.total_tokens = 0
        self.last_call_time = 0

        from src.utils.quality_tracker import QualityTracker
        self._quality_tracker = QualityTracker(config.config if hasattr(config, "config") else {})
        self._config_path = getattr(config, 'config_path', None)
        
        self.max_conversation_history = 10
        # 熔断器：closed → open → half-open → closed/open 三态
        self._cb_window: deque = deque(maxlen=20)
        self._cb_open_until: float = 0.0
        self._cb_half_open: bool = False
        self.logger.info("AI 客户端初始化")
    
    async def initialize(self) -> bool:
        """初始化 AI 客户端"""
        try:
            ai_config = self.config.get_ai_config()
            self._provider = (ai_config.get("provider") or "gemini").strip().lower()

            api_key = ai_config.get('api_key')
            self.model = ai_config.get('model', 'gemini-2.5-flash')
            self.temperature = float(ai_config.get('temperature', 0.7))
            self.max_tokens = int(ai_config.get('max_tokens', 1024))
            self.timeout = int(ai_config.get('timeout', 30))
            self.system_prompt = ai_config.get('system_prompt', '').strip()
            self._domain_system_prompt = ""
            self._domain_terminology = {}
            self._domain_context_supplements = {}
            self.max_conversation_history = int(ai_config.get('max_conversation_history', 10) or 10)
            self._embedding_model = ai_config.get('embedding_model', 'gemini-embedding-001')
            # ★ P5-4：对话分级路由 — 配置示例
            # ai.tiers:
            #   enabled: true
            #   default: normal
            #   premium: {model: gpt-4o, temperature: 0.6, max_tokens: 1200}
            #   normal:  {model: deepseek-chat, temperature: 0.7, max_tokens: 800}
            #   low:     {model: deepseek-chat, temperature: 0.8, max_tokens: 400}
            tiers_cfg = ai_config.get("tiers") or {}
            self._tiers_enabled = bool(tiers_cfg.get("enabled", False))
            self._tiers_default = str(tiers_cfg.get("default") or "normal")
            self._tiers: Dict[str, Dict[str, Any]] = {}
            for k, v in tiers_cfg.items():
                if k in ("enabled", "default"):
                    continue
                if isinstance(v, dict):
                    self._tiers[str(k)] = dict(v)
            if self._tiers_enabled:
                self.logger.info(
                    f"AI 分级路由启用，已加载 {len(self._tiers)} 档：{list(self._tiers.keys())}"
                )
            # ★ P6-4：加载 LLM 价格表 + 初始化 cost tracker
            try:
                from src.ai.llm_cost import get_llm_cost
                pricing = ai_config.get("pricing") or {}
                if pricing:
                    get_llm_cost().set_pricing(pricing)
                    self.logger.info(
                        f"LLM 成本追踪：已加载 {len(pricing)} 个模型的价格表"
                    )
            except Exception:
                self.logger.debug("LLM 成本追踪初始化失败", exc_info=True)
            _cb = ai_config.get('circuit_breaker') or {}
            self._cb_enabled = bool(_cb.get('enabled', False))
            self._cb_window_size = int(_cb.get('window_size', 20) or 20)
            self._cb_fail_threshold = float(_cb.get('fail_threshold', 0.5) or 0.5)
            self._cb_open_seconds = int(_cb.get('open_seconds', 60) or 60)
            self._cb_window = deque(maxlen=self._cb_window_size)

            if self._provider == "openai_compatible":
                return await self._initialize_openai_compatible(ai_config, api_key)

            if not GENAI_AVAILABLE:
                self.logger.error("google-genai 库未安装，请运行: pip install google-genai")
                return False

            if not api_key or api_key == "YOUR_AI_API_KEY":
                self.logger.error("AI API 密钥未配置")
                self.logger.error("请在配置文件中填写 ai.api_key")
                return False

            self.client = genai.Client(api_key=api_key)

            test_result = await self._test_connection()
            if not test_result:
                self.logger.error("AI API 连接测试失败")
                return False

            self.logger.info(f"✅ AI 客户端初始化成功 — 原生 Gemini API (模型: {self.model})")
            return True

        except Exception as e:
            self.logger.error(f"初始化 AI 客户端失败: {e}")
            return False

    async def _initialize_openai_compatible(self, ai_config: Dict[str, Any], api_key: Optional[str]) -> bool:
        """Ollama / vLLM 等 OpenAI 兼容接口（base_url 形如 http://host:11434/v1）。"""
        self._oa_embed_client = None
        if not OPENAI_SDK_AVAILABLE:
            self.logger.error("openai 库未安装，请运行: pip install openai")
            return False
        raw_base = (ai_config.get("base_url") or "").strip().rstrip("/")
        if not raw_base:
            self.logger.error("openai_compatible 需要配置 ai.base_url，例如 http://100.x.x.x:11434/v1")
            return False
        if not raw_base.endswith("/v1"):
            raw_base = raw_base + "/v1"
        key = api_key if api_key and api_key != "YOUR_AI_API_KEY" else "ollama"
        self._oa_client = AsyncOpenAI(api_key=key, base_url=raw_base, timeout=float(self.timeout))
        self._use_openai_compat = True
        self.client = None

        emb_raw = (ai_config.get("embedding_base_url") or "").strip().rstrip("/")
        if emb_raw:
            if not emb_raw.endswith("/v1"):
                emb_raw = emb_raw + "/v1"
            emb_key = (ai_config.get("embedding_api_key") or key or "ollama").strip()
            if emb_key == "YOUR_AI_API_KEY":
                emb_key = "ollama"
            self._oa_embed_client = AsyncOpenAI(api_key=emb_key, base_url=emb_raw, timeout=float(self.timeout))
            self.logger.info("Embedding 使用独立 base_url: %s", emb_raw)

        test_result = await self._test_openai_connection()
        if not test_result:
            self.logger.error(
                "OpenAI 兼容 API 连接测试失败（请检查 base_url、密钥、网络及模型名）"
            )
            return False

        self.logger.info("✅ AI 客户端初始化成功 — OpenAI 兼容 API (模型: %s, base: %s)", self.model, raw_base)
        return True

    async def _test_openai_connection(self) -> bool:
        try:
            if not self._oa_client:
                return False
            response = await self._oa_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Say hi in one word."}],
                max_tokens=32,
                temperature=0.3,
            )
            if response and response.choices:
                c0 = response.choices[0].message
                if c0 and (c0.content or "").strip():
                    self.logger.info("AI API 连接测试成功")
                    return True
            self.logger.error("OpenAI 兼容 API 返回空 choices")
            return False
        except Exception as e:
            self.logger.error(f"AI API 连接测试失败: {e}")
            return False

    async def _test_connection(self) -> bool:
        """测试API连接"""
        try:
            if not self.client:
                return False
            
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents="Say hi in one word.",
                config=types.GenerateContentConfig(
                    max_output_tokens=50,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            
            if response and response.candidates:
                text = None
                try:
                    text = response.text
                except (ValueError, IndexError):
                    pass
                if text:
                    self.logger.info("AI API 连接测试成功")
                    return True
                self.logger.warning(
                    "AI API 测试返回 candidates 但无文本, finish_reason=%s",
                    response.candidates[0].finish_reason if response.candidates else "N/A"
                )
                return True
            else:
                self.logger.error("AI API 返回空响应 (无 candidates)")
                return False
        except Exception as e:
            self.logger.error(f"AI API 连接测试失败: {e}")
            return False

    async def _generate_reply_openai_compat(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        strategy_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """OpenAI 兼容（Ollama）对话生成，与 generate_reply 行为对齐（熔断、重试、兜底）。"""
        _fb_lang = (context or {}).get("reply_lang", "zh")
        if not self._oa_client:
            self.logger.error("AI 客户端未初始化")
            return self._fallback_reply(_fb_lang)
        if context is not None:
            context["_current_user_message_for_lang"] = user_message
        if self._cb_enabled and self._cb_open_until > 0:
            now = time.time()
            if now < self._cb_open_until:
                self.logger.warning("AI 熔断开路中，跳过 API 调用 request_id=%s", (context or {}).get("request_id") or "n/a")
                return self._fallback_reply(_fb_lang)
            if not self._cb_half_open:
                self._cb_half_open = True
                self.logger.info("AI 熔断进入半开状态，允许一次探测请求")
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().set_circuit_breaker_state("half-open", self._cb_open_until)
                except Exception:
                    pass

        so = strategy_overrides or {}
        use_temperature = float(so["temperature"]) if "temperature" in so else self.temperature
        use_max_tokens = int(so["max_tokens"]) if "max_tokens" in so else self.max_tokens
        if use_max_tokens < 256:
            use_max_tokens = 256
        use_context_rounds = int(so["context_rounds"]) if "context_rounds" in so else None
        use_model = str(so["model"]) if so.get("model") else self.model

        max_hist = use_context_rounds if use_context_rounds is not None else max(1, int(self.max_conversation_history or 10))
        system_instruction = self._build_system_instruction(context)
        # ★ companion debug：可热开 ai.debug.dump_system_prompt 把完整 system prompt 打到 INFO 日志
        # 用法：在 config 里设 ai.debug.dump_system_prompt: true，看 logs/app.log 验证 4 层记忆是否注入
        try:
            _ai_cfg = (self.config.config.get("ai") or {}) if (self.config and getattr(self.config, "config", None)) else {}
            _dbg = (_ai_cfg.get("debug") or {})
            if _dbg.get("dump_system_prompt", False) and system_instruction:
                _rid = (context or {}).get("request_id", "n/a")
                _layers = []
                if "【对话伙伴画像" in system_instruction: _layers.append("portrait")
                if "【陪伴关系" in system_instruction or "relationship" in system_instruction.lower(): _layers.append("intimacy")
                if "【用户长期记忆要点" in system_instruction: _layers.append("episodic")
                if "【Messenger 人设补充" in system_instruction or "【LINE 补充说明" in system_instruction: _layers.append("style_hint")
                self.logger.info(
                    "[prompt-dump rid=%s] layers=%s len=%d\n--- BEGIN system_prompt ---\n%s\n--- END ---",
                    _rid, ",".join(_layers) or "none", len(system_instruction), system_instruction,
                )
        except Exception:
            pass
        messages: List[Dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        if conversation_history:
            _lim = max(0, int(max_hist))
            hist = [] if _lim == 0 else conversation_history[-_lim:]
            for msg in hist:
                role = (msg.get("role") or "user").lower()
                content = (msg.get("content") or "").strip()
                if not content or role == "system":
                    continue
                if role == "assistant":
                    messages.append({"role": "assistant", "content": content})
                else:
                    messages.append({"role": "user", "content": content})
        messages.append({"role": "user", "content": user_message})

        request_id = (context or {}).get("request_id", "")
        last_error = None
        start_time = time.time()
        for attempt in range(2):
            try:
                response = await self._oa_client.chat.completions.create(
                    model=use_model,
                    messages=messages,
                    temperature=use_temperature,
                    max_tokens=use_max_tokens,
                )
                elapsed_time = time.time() - start_time
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_api_call(elapsed_time * 1000)
                except Exception:
                    pass

                reply = None
                if response and response.choices:
                    reply = (response.choices[0].message.content or "").strip()
                if reply:
                    self.total_calls += 1
                    pt = ct = 0
                    try:
                        u = response.usage
                        if u:
                            pt = getattr(u, "prompt_tokens", 0) or 0
                            ct = getattr(u, "completion_tokens", 0) or 0
                            self.total_tokens += pt + ct
                    except Exception:
                        pass
                    self.last_call_time = time.time()
                    self._quality_tracker.record_call(
                        prompt_tokens=pt, completion_tokens=ct,
                        elapsed_ms=int(elapsed_time * 1000),
                        reply=reply, request_id=request_id,
                    )
                    # ★ P6-4：按 (model, tier, account) 累积 tokens + cost
                    try:
                        from src.ai.llm_cost import get_llm_cost
                        _ctx = context or {}
                        get_llm_cost().record(
                            model=str(use_model),
                            prompt_tokens=pt,
                            completion_tokens=ct,
                            tier=str(_ctx.get("ai_tier") or "default"),
                            account_id=str(_ctx.get("account_id") or "default"),
                            latency_ms=int(elapsed_time * 1000),
                        )
                    except Exception:
                        self.logger.debug("llm_cost.record 失败", exc_info=True)
                    if self._cb_enabled:
                        self._cb_window.append(True)
                        if self._cb_half_open:
                            self._cb_half_open = False
                            self._cb_open_until = 0.0
                            self._cb_window.clear()
                            self.logger.info("AI 半开探测成功，熔断器关闭")
                            try:
                                from src.monitoring.metrics_store import get_metrics_store
                                get_metrics_store().set_circuit_breaker_state("closed")
                            except Exception:
                                pass
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        _ms = get_metrics_store()
                        _ms.record_reply_length(len(reply))
                        _ms.record_ai_success()
                    except Exception:
                        pass
                    reply = await self._guard_reply_language(reply, context)
                    return reply
                self.logger.warning("AI 返回空响应")
                if self._cb_enabled:
                    self._cb_window.append(False)
                    self._maybe_trip_circuit()
            except Exception as e:
                last_error = e
                self.logger.warning("AI 调用失败(attempt=%s): %s", attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(1.5)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _ms = get_metrics_store()
            _ms.record_error()
            _ms.record_ai_error()
        except Exception:
            pass
        if self._cb_enabled:
            self._cb_window.append(False)
            self._maybe_trip_circuit()
        self.logger.error("AI 两次调用均失败, request_id=%s: %s", request_id or "n/a", last_error)
        return self._fallback_reply(_fb_lang)

    def _apply_tier_overrides(
        self,
        strategy_overrides: Optional[Dict[str, Any]],
        context: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """★ P5-4：按 context.ai_tier 合并 tier overrides 到 strategy_overrides。

        优先级：strategy_overrides（调用者显式给的） > tier > 实例默认
        → 所以 tier overrides 只填充 strategy_overrides 里**未指定的**字段。
        """
        if not self._tiers_enabled or not self._tiers:
            return strategy_overrides
        tier = str((context or {}).get("ai_tier") or "").strip()
        if not tier:
            tier = self._tiers_default
        spec = self._tiers.get(tier) or self._tiers.get(self._tiers_default) or {}
        if not spec:
            return strategy_overrides
        merged = dict(strategy_overrides or {})
        for k, v in spec.items():
            if k in ("enabled", "default"):
                continue
            merged.setdefault(k, v)
        # 记录一下（方便排查）
        merged.setdefault("_ai_tier", tier)
        return merged

    async def generate_reply(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        strategy_overrides: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        生成回复（含重试与兜底：失败时返回友好提示而非 None）

        strategy_overrides 可包含 temperature / max_tokens / context_rounds
        以覆盖实例默认值，实现按策略差异化调用。
        """
        strategy_overrides = self._apply_tier_overrides(strategy_overrides, context)
        if self._use_openai_compat:
            return await self._generate_reply_openai_compat(
                user_message, context, conversation_history, strategy_overrides
            )
        _fb_lang = (context or {}).get("reply_lang", "zh")
        if not self.client:
            self.logger.error("AI 客户端未初始化")
            return self._fallback_reply(_fb_lang)
        if context is not None:
            context["_current_user_message_for_lang"] = user_message
        if self._cb_enabled and self._cb_open_until > 0:
            now = time.time()
            if now < self._cb_open_until:
                self.logger.warning("AI 熔断开路中，跳过 API 调用 request_id=%s", (context or {}).get("request_id") or "n/a")
                return self._fallback_reply(_fb_lang)
            if not self._cb_half_open:
                self._cb_half_open = True
                self.logger.info("AI 熔断进入半开状态，允许一次探测请求")
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().set_circuit_breaker_state("half-open", self._cb_open_until)
                except Exception:
                    pass

        so = strategy_overrides or {}
        use_temperature = float(so["temperature"]) if "temperature" in so else self.temperature
        use_max_tokens = int(so["max_tokens"]) if "max_tokens" in so else self.max_tokens
        if use_max_tokens < 256:
            use_max_tokens = 256
        use_context_rounds = int(so["context_rounds"]) if "context_rounds" in so else None
        use_model = str(so["model"]) if so.get("model") else self.model

        start_time = time.time()
        system_instruction = self._build_system_instruction(context)
        contents = self._build_contents(user_message, context, conversation_history,
                                        context_rounds_override=use_context_rounds)
        request_id = (context or {}).get("request_id", "")
        last_error = None
        for attempt in range(2):
            try:
                use_thinking = int(so.get("thinking_budget", 0))
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction if system_instruction else None,
                    temperature=use_temperature,
                    max_output_tokens=use_max_tokens,
                    thinking_config=types.ThinkingConfig(thinking_budget=use_thinking),
                )

                response = await self.client.aio.models.generate_content(
                    model=use_model,
                    contents=contents,
                    config=config,
                )

                elapsed_time = time.time() - start_time
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_api_call(elapsed_time * 1000)
                except Exception:
                    pass

                if response and response.candidates:
                    candidate = response.candidates[0]
                    reply = None
                    try:
                        reply = response.text
                    except (ValueError, IndexError):
                        pass
                    finish = str(candidate.finish_reason) if candidate.finish_reason else None

                    if reply:
                        self.total_calls += 1
                        pt = ct = 0
                        um = response.usage_metadata
                        if um:
                            pt = getattr(um, "prompt_token_count", 0) or 0
                            ct = getattr(um, "candidates_token_count", 0) or 0
                            self.total_tokens += pt + ct
                        self.last_call_time = time.time()
                        if finish and "MAX_TOKENS" in finish.upper():
                            self.logger.warning(
                                "AI 回复因 max_output_tokens 截断 (finish_reason=%s, "
                                "max_output_tokens=%d, completion=%d, request_id=%s)",
                                finish, use_max_tokens, ct, request_id or "n/a",
                            )
                        self._quality_tracker.record_call(
                            prompt_tokens=pt, completion_tokens=ct,
                            elapsed_ms=int(elapsed_time * 1000),
                            reply=reply, request_id=request_id,
                        )
                        # ★ P6-4：Gemini 分支 cost tracking
                        try:
                            from src.ai.llm_cost import get_llm_cost
                            _ctx = context or {}
                            get_llm_cost().record(
                                model=str(self.model),
                                prompt_tokens=pt,
                                completion_tokens=ct,
                                tier=str(_ctx.get("ai_tier") or "default"),
                                account_id=str(_ctx.get("account_id") or "default"),
                                latency_ms=int(elapsed_time * 1000),
                            )
                        except Exception:
                            self.logger.debug("llm_cost.record 失败", exc_info=True)
                        self.logger.debug(
                            "AI回复生成: %.2fs, tokens=%d, finish=%s, request_id=%s",
                            elapsed_time, pt + ct, finish, request_id or "n/a"
                        )
                        if self._cb_enabled:
                            self._cb_window.append(True)
                            if self._cb_half_open:
                                self._cb_half_open = False
                                self._cb_open_until = 0.0
                                self._cb_window.clear()
                                self.logger.info("AI 半开探测成功，熔断器关闭")
                                try:
                                    from src.monitoring.metrics_store import get_metrics_store
                                    get_metrics_store().set_circuit_breaker_state("closed")
                                except Exception:
                                    pass
                        stripped = reply.strip()
                        try:
                            from src.monitoring.metrics_store import get_metrics_store
                            _ms = get_metrics_store()
                            _ms.record_reply_length(len(stripped))
                            _ms.record_ai_success()
                            if finish and "MAX_TOKENS" in finish.upper():
                                _ms.record_truncated_reply()
                        except Exception:
                            pass
                        stripped = await self._guard_reply_language(stripped, context)
                        return stripped
                self.logger.warning("AI 返回空响应")
                if self._cb_enabled:
                    self._cb_window.append(False)
                    self._maybe_trip_circuit()
            except Exception as e:
                last_error = e
                self.logger.warning("AI 调用失败(attempt=%s): %s", attempt + 1, e)
                if attempt == 0:
                    await asyncio.sleep(1.5)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _ms = get_metrics_store()
            _ms.record_error()
            _ms.record_ai_error()
        except Exception:
            pass
        if self._cb_enabled:
            self._cb_window.append(False)
            self._maybe_trip_circuit()
        self.logger.error("AI 两次调用均失败, request_id=%s: %s", request_id or "n/a", last_error)
        return self._fallback_reply(_fb_lang)

    def _maybe_trip_circuit(self):
        """窗口内失败比例超阈值则开路；半开探测失败则加倍 open 时长"""
        if self._cb_half_open:
            backoff = min(self._cb_open_seconds * 2, 600)
            self._cb_open_until = time.time() + backoff
            self._cb_half_open = False
            self.logger.error("AI 半开探测失败，重新开路 %.0fs", backoff)
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().set_circuit_breaker_state("open", self._cb_open_until)
            except Exception:
                pass
            return
        if len(self._cb_window) < self._cb_window_size:
            return
        fails = sum(1 for x in self._cb_window if not x)
        rate = fails / len(self._cb_window)
        if rate >= self._cb_fail_threshold:
            self._cb_open_until = time.time() + self._cb_open_seconds
            self.logger.error(
                "AI 熔断开路 %.0fs（窗口失败率 %.0f%%）",
                self._cb_open_seconds, rate * 100
            )
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().set_circuit_breaker_state("open", self._cb_open_until)
            except Exception:
                pass

    _FALLBACK_REPLIES = [
        "在的，请您稍等一下～",
        "收到，马上为您处理。",
        "好的亲，稍等我看一下～",
        "您好，请稍等片刻～",
        "收到啦，这就帮您查看。",
    ]
    _FALLBACK_REPLIES_EN = [
        "Got it, please hold on a moment~",
        "Received, let me check for you right away.",
        "Sure, one moment please~",
        "Hello, please wait a moment~",
        "Noted, checking on it now.",
    ]

    @staticmethod
    def _reply_lang_mismatch(reply: str, expected_lang: str) -> bool:
        """Return True when reply language clearly mismatches expected_lang."""
        if not reply or not expected_lang:
            return False
        cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
        latin = len(re.findall(r"[A-Za-z]", reply))
        total = cjk + latin
        if total < 8:
            return False
        if expected_lang == "zh":
            return cjk / total < 0.2
        else:
            return cjk / total > 0.35

    async def _guard_reply_language(
        self, reply: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """Post-generation safety net: if reply language clearly mismatches
        reply_lang, attempt a single lightweight translation correction."""
        if not reply or not context:
            return reply
        _rl = context.get("reply_lang")
        if not _rl or _rl in ("zh", "ja", "ko") or context.get("_skip_lang_guard"):
            return reply
        _cfg_ctx = (self.config.config or {}) if self.config else {}
        if isinstance(_cfg_ctx, dict) and effective_domain_name(_cfg_ctx) == "conversion":
            return reply
        if not self._reply_lang_mismatch(reply, _rl):
            return reply
        _lang_name = self._LANG_NAMES.get(_rl, _rl)
        self.logger.warning(
            "Language guard triggered: expected=%s, reply=%s...",
            _rl, reply[:60],
        )
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_lang_mismatch()
        except Exception:
            pass
        try:
            _fix_ctx = {
                "_skip_lang_guard": True,
                "_intent_supplement": (
                    f"Translate the following customer-service reply into {_lang_name}. "
                    f"Keep channel names (EP/JC/EasyPaisa/JazzCash) and commands unchanged. "
                    f"Output ONLY the translation, no explanation."
                ),
                "_current_user_message_for_lang": "x" * 5,
            }
            corrected = await self.generate_reply(
                f"Translate to {_lang_name}:\n\n{reply}",
                _fix_ctx,
            )
            if corrected and len(corrected) > 5:
                if not self._reply_lang_mismatch(corrected, _rl):
                    self.logger.info("Language guard corrected reply successfully")
                    return corrected
                self.logger.warning("Language guard correction still mismatched, using original")
        except Exception as e:
            self.logger.warning("Language guard correction failed: %s", e)
        return reply

    def _fallback_reply(self, lang: str = "zh") -> str:
        import random
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_fallback_reply()
        except Exception:
            pass
        if lang and lang != "zh":
            return random.choice(self._FALLBACK_REPLIES_EN)
        try:
            from src.utils.kb_store import KnowledgeBaseStore
            from pathlib import Path
            if hasattr(self, '_config_path') and self._config_path:
                _kb_db = Path(self._config_path).parent / "knowledge_base.db"
                if _kb_db.exists():
                    kb = KnowledgeBaseStore(_kb_db)
                    reply = kb.get_fallback("global")
                    if reply:
                        return reply
        except Exception:
            pass
        return random.choice(self._FALLBACK_REPLIES)

    def set_domain_pack(self, system_prompt: str = "", terminology: dict = None, context_supplements: dict = None):
        """Apply domain pack overrides for prompts, terminology, and context supplements.
        If no system_prompt is set in config.yaml, the domain pack's prompt is used instead.
        """
        self._domain_system_prompt = system_prompt or ""
        self._domain_terminology = terminology or {}
        self._domain_context_supplements = context_supplements or {}
        if not self.system_prompt and self._domain_system_prompt:
            self.system_prompt = self._domain_system_prompt
            self.logger.info("System prompt loaded from domain pack (%d chars)", len(self.system_prompt))

    def _primary_system_prompt_text(self) -> str:
        """主系统提示：优先读当前 config 中的 ai.system_prompt，避免进程内 self.system_prompt 与后台保存不一致。"""
        if not self.config or not getattr(self.config, "config", None):
            return (self.system_prompt or "").strip()
        ai_cfg = (self.config.config or {}).get("ai") or {}
        sp = (ai_cfg.get("system_prompt") or "").strip()
        if sp:
            return sp
        return (self.system_prompt or "").strip()

    def _build_system_instruction(self, context: Optional[Dict[str, Any]] = None) -> str:
        """将主系统提示词 + 快速设置 + 上下文提示合并为单一 system_instruction"""
        parts = []
        context = context or {}
        _suppress_global_identity = bool(context.get("suppress_global_ai_identity"))
        _primary = "" if _suppress_global_identity else self._primary_system_prompt_text()
        if _primary:
            parts.append(_primary)

        # 后台人设（Web「默认人设」/ persona_runtime.yaml / 域包）：与静态 system_prompt 叠加
        ai_cfg_pre = (self.config.config or {}).get("ai", {}) if self.config else {}
        _pbd = (ai_cfg_pre.get("persona_block_detail") or "full").strip().lower()
        if _pbd not in ("none", "full", "compact"):
            _pbd = "full"
        _name_ov = "" if _suppress_global_identity else (ai_cfg_pre.get("ai_name") or "").strip()
        try:
            from src.utils.persona_manager import PersonaManager

            _pm = PersonaManager.get_instance()
            _p_cid = str((context or {}).get("chat_id", "") or "") if context else ""
            _p_block = _pm.format_persona_block(
                _p_cid, detail=_pbd, name_override=_name_ov
            )
            if _p_block:
                parts.append("【后台人设定位 · 须遵守】\n" + _p_block)
        except Exception:
            pass

        ai_cfg = (self.config.config or {}).get("ai", {}) if self.config else {}
        ai_name = "" if _suppress_global_identity else (ai_cfg.get("ai_name") or "").strip()
        reply_style = (ai_cfg.get("reply_style") or "").strip()
        overrides = []
        if ai_name:
            overrides.append(
                f"你的名字是「{ai_name}」，用户问你叫什么、是谁、怎么称呼你，都必须用这个名字；"
                "若上文（含人设块、知识库）出现其他称呼，一律以本句为准。"
            )
        _STYLE_MAP = {
            "concise": (
                "回复风格：简洁干练，少废话，直接给结论和动作。"
                "**禁止**在句首使用填充语气词：如「嗯」「嗯嗯」「呃」「那个」等；"
                "可直入主题，或以「好的」「收到」等短承接开头（视语境）。"
                "若与上文系统提示中「开头多样化可选池」冲突，**以本段为准**。"
            ),
            "warm": (
                "回复风格：温暖、活泼、恋爱向腻聊；可用撒娇/反问/昵称感语气词，避免油腻刷屏。"
                "少用「报告体」和机械分条；非必要不用 Markdown 大标题；不要主动推销查单/通道/支付。"
            ),
            "professional": "回复风格：正式专业，措辞严谨，适合商务场景。",
        }
        if reply_style in _STYLE_MAP:
            overrides.append(_STYLE_MAP[reply_style])
        if overrides:
            parts.append("【快速设置覆盖】\n" + "\n".join(overrides))

        _reply_lang = (context or {}).get("reply_lang", "zh") if context else "zh"
        _lang_name = self._LANG_NAMES.get(_reply_lang, "")
        _cfg_ins = (self.config.config or {}) if self.config and hasattr(self.config, "config") else {}
        _instr_companion = isinstance(_cfg_ins, dict) and effective_domain_name(_cfg_ins) == "conversion"
        if _reply_lang != "zh" and _lang_name:
            _no_zh_extra = (
                ""
                if _instr_companion
                else " DO NOT output any Chinese characters (except channel names like EP/JC/EasyPaisa/JazzCash)."
            )
            parts.append(
                f"【LANGUAGE RULE — TOP PRIORITY — MANDATORY】\n"
                f"DETECTED USER LANGUAGE: {_lang_name}.\n"
                f"You MUST reply ENTIRELY in {_lang_name}.{_no_zh_extra}\n"
                f"Any Chinese templates or knowledge base content MUST be translated to {_lang_name} before output.\n"
                f"Commands (/cxds etc) stay as-is.\n"
                f"Violating this rule is MORE SERIOUS than giving wrong content."
            )
        else:
            _zh_tail = (
                "中文模板和知识库内容必须翻译为用户使用的语言后输出。"
                if _instr_companion
                else (
                    "中文模板和知识库内容必须翻译为用户使用的语言后输出。"
                    "通道名(EP/JC/EasyPaisa/JazzCash)、命令(/cxds等)保持原样不翻译。"
                )
            )
            parts.append(
                "【多语言回复规则 — MANDATORY】"
                "ALWAYS reply in the SAME language as the user's message. This is the #1 priority rule. "
                "If the user writes in English, you MUST reply entirely in English. "
                "If the user writes in Urdu/Arabic, reply in that language. "
                f"{_zh_tail}"
                "违反此规则比回复错误的内容更严重。"
            )

        context_prompt = self._build_context_prompt(context)
        if context_prompt:
            parts.append(context_prompt)

        if context and context.get("_intent_supplement"):
            parts.append(context["_intent_supplement"])

        return "\n\n".join(parts)

    _MAX_HISTORY_CHARS = 12000

    def _build_contents(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        context_rounds_override: Optional[int] = None
    ) -> list:
        """
        构建 Gemini 原生 contents 数组（仅 user/model 角色）。
        双重截断：先按轮数裁剪，再按字符总量从最早消息开始丢弃，
        防止超长历史导致 prompt 过大。
        """
        contents = []
        
        max_hist = context_rounds_override if context_rounds_override is not None else \
            max(1, getattr(self, "max_conversation_history", 10) or 10)
        if conversation_history:
            _lim = max(0, int(max_hist))
            hist = [] if _lim == 0 else conversation_history[-_lim:]
            if len(conversation_history) > _lim and _lim > 0:
                self.logger.debug(
                    "conversation_history 已截断: %s -> %s 条",
                    len(conversation_history), _lim
                )
            total_chars = sum(len(m.get("content", "")) for m in hist)
            if total_chars > self._MAX_HISTORY_CHARS:
                trimmed = []
                budget = self._MAX_HISTORY_CHARS
                for msg in reversed(hist):
                    c_len = len(msg.get("content", ""))
                    if budget >= c_len:
                        trimmed.append(msg)
                        budget -= c_len
                    else:
                        break
                trimmed.reverse()
                dropped = len(hist) - len(trimmed)
                if dropped > 0:
                    self.logger.debug(
                        "历史按字符截断: 丢弃最早 %d 条 (%d chars -> %d chars)",
                        dropped, total_chars, total_chars - budget
                    )
                    trimmed.insert(0, {
                        "role": "user",
                        "content": f"[...此前有 {dropped} 条对话已省略...]"
                    })
                hist = trimmed
            for msg in hist:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "assistant":
                    role = "model"
                elif role == "system":
                    continue
                if not content:
                    continue
                if contents and contents[-1].role == role:
                    prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
                    contents[-1] = types.Content(
                        role=role,
                        parts=[types.Part(text=prev_text + "\n" + content)]
                    )
                else:
                    contents.append(types.Content(
                        role=role,
                        parts=[types.Part(text=content)]
                    ))
        
        # 添加当前用户消息
        if contents and contents[-1].role == "user":
            prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
            contents[-1] = types.Content(
                role="user",
                parts=[types.Part(text=prev_text + "\n" + user_message)]
            )
        else:
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=user_message)]
            ))
        
        return contents
    
    def _build_context_prompt(self, context: Optional[Dict[str, Any]]) -> str:
        """
        构建上下文提示（含关键信息锚定，确保订单号/额度结论不被窗口滚动丢弃）
        """
        if not context:
            return ""
        prompt_parts = []
        # Phase 1：用户画像注入 — runner 已渲染好 markdown 块塞 _contact_portrait_block
        _portrait_block = (context.get("_contact_portrait_block") or "").strip()
        if _portrait_block:
            prompt_parts.append(_portrait_block)
        _cfg_ctx = (self.config.config or {}) if self.config else {}
        _is_companion = isinstance(_cfg_ctx, dict) and effective_domain_name(_cfg_ctx) == "conversion"
        _wa = (_cfg_ctx.get("web_admin") or {}) if isinstance(_cfg_ctx, dict) else {}
        _site = (_wa.get("site_name") or "").strip()
        # LINE 个人号 RPA：人设以全文「后台人设」为准；此处仅渠道与系统名提示
        if context.get("channel") == "line_rpa":
            if _site:
                prompt_parts.append(
                    f"【系统】你是在「{_site}」中协助客户转化类对话；语气须与上文「后台人设定位」一致。"
                )
            _lh = (context.get("line_rpa_style_hint") or "").strip()
            if _lh:
                prompt_parts.append("【LINE 补充说明】\n" + _lh)
            else:
                prompt_parts.append(
                    "【LINE 渠道】当前为手机 LINE 一对一私聊；回复简短自然，避免与上文人设冲突的客服套话。"
                )
            # P7-1：Vision / 结构化读屏得到的「伪用户消息」以 [标签] 开头
            _um_line = (context.get("_current_user_message_for_lang") or "").strip()
            if _um_line.startswith("[LINE贴图]"):
                prompt_parts.append(
                    "【LINE 多模态·贴图】对方发来贴图；你看到的是系统代述的标签+简短描述，并非逐像素识图。"
                    "回复宜口语化、一两句即可，可带少量 emoji；不要编造画面里不存在的细节。"
                )
            elif _um_line.startswith("[图片消息]"):
                prompt_parts.append(
                    "【LINE 多模态·图片】对方发来图片/截图；描述来自模型归纳，可能不完整。"
                    "可自然评论或简短追问，避免假装看清了全部文字或 UI。"
                )
            elif _um_line.startswith("[语音消息]"):
                prompt_parts.append(
                    "【LINE 多模态·语音】对方发来语音；若仅见时长等占位描述，回复宜简短确认或表示稍后方便文字沟通。"
                )
            elif _um_line.startswith("[文件消息]"):
                prompt_parts.append(
                    "【LINE 多模态·文件】对方发来文件；不知具体内容时勿臆测，可简短询问用途或表示收到。"
                )
            if context.get("vision_room"):
                prompt_parts.append(
                    "【LINE 读屏模式】本条上下文来自截图+多模态识别，可能存在遗漏；回复保持容错、简短。"
                )
        # Messenger RPA：渠道说明 + 可选 style_hint（从 config 读）
        if context.get("channel") == "messenger_rpa":
            _mh = (context.get("messenger_rpa_style_hint") or "").strip()
            if _mh:
                prompt_parts.append("【Messenger 人设补充】\n" + _mh)
            else:
                prompt_parts.append(
                    "【Messenger 渠道】当前为 Facebook Messenger 一对一私聊；"
                    "回复风格偏朋友式聊天，简短自然（建议 1-2 句，可含少量 emoji），"
                    "避免长段客服套话；称呼用对方在 Messenger 上的名字，不主动提及其他平台。"
                )
            _peer_kind = (context.get("messenger_rpa_peer_kind") or "").strip().lower()
            if _peer_kind == "image":
                prompt_parts.append(
                    "【Messenger 多模态·图片】对方发来图片；你看到的是系统代述的描述，"
                    "不要假装看清了画面的所有细节，可自然评论或简短追问。"
                )
            elif _peer_kind == "voice":
                prompt_parts.append(
                    "【Messenger 多模态·语音】对方发来语音；回复宜简短确认或表示稍后方便文字沟通。"
                )
            elif _peer_kind == "sticker":
                prompt_parts.append(
                    "【Messenger 多模态·贴纸】对方发来贴纸；回复口语化，一两句即可，贴纸氛围偏轻松。"
                )
        # 关键信息锚定：置顶，避免长对话截断后丢失（陪聊域不注入通道/订单锚点）
        key_anchor = []
        last_reply = (context.get("last_reply") or "").strip()
        if not _is_companion and last_reply and (
            "当前额度如下" in last_reply or ("EP" in last_reply and "100" in last_reply)
        ):
            key_anchor.append("上条回复已包含通道/额度说明，若用户追问可简短确认勿重复贴长段。")
        if context.get("image_ocr_text"):
            key_anchor.append("本会话含识图/凭证内容（见下方），订单回复请严格依据此信息。")
        if key_anchor:
            prompt_parts.append("【本会话关键信息】\n" + "\n".join(key_anchor))
        # 情绪感知写入 prompt：引导语气，不改变情绪增强器后处理逻辑
        emotion_hint = (context.get("user_emotion_hint") or "").strip().lower()
        _um = context.get("_current_user_message_for_lang") or context.get("last_message") or ""
        lang_hint = self._detect_message_language(_um)
        if lang_hint:
            lang_name = self._LANG_NAMES.get(lang_hint, lang_hint) or "中文"
            if _is_companion:
                prompt_parts.append(
                    f"【输出语言】用户当前消息语言为「{lang_name}」，你必须用该语言回复全部内容。"
                    f"即使系统提供的模板或知识库内容是中文，你也必须翻译为「{lang_name}」后再输出。"
                )
            else:
                if lang_hint != "zh":
                    prompt_parts.append(
                        f"【输出语言】用户当前消息语言为「{lang_name}」，你必须用该语言回复全部内容。"
                        f"即使系统提供的模板或知识库内容是中文，你也必须翻译为「{lang_name}」后再输出。"
                        f"术语如 EP/JC/EasyPaisa/JazzCash 等通道名保持原样不翻译。"
                    )

        if emotion_hint and emotion_hint != "neutral":
            emotion_guide = {
                "urgent": "用户语气偏着急，请先简短安抚并给出明确动作或时间点，避免空话。",
                "frustrated": "用户可能不满，先认同再说明处理进度，不要争辩。",
                "angry": "用户情绪激动，保持冷静礼貌，少emoji，多事实与解决方案。",
                "happy": "用户情绪积极，可顺势简短热情，但不要过度冗长。",
                "positive": "用户情绪偏积极，可顺势简短热情，但不要过度冗长。",
                "negative": "用户情绪偏消极或不满，先认同再说明处理进度，避免争辩与机械道歉堆砌。",
            }
            if emotion_hint in emotion_guide:
                prompt_parts.append("【用户情绪倾向】" + emotion_guide[emotion_hint])
            else:
                prompt_parts.append(f"【用户情绪倾向】粗判为 {emotion_hint}，回复语气可适当贴合。")
        # Telegram 侧上下文分析（近期消息主题/摘要），补全「聊天连贯」所需线索
        _ca = context.get("context_analysis")
        if isinstance(_ca, dict):
            _csum = (_ca.get("context_summary") or "").strip()
            _topic = (_ca.get("conversation_topic") or "").strip()
            if _csum and _csum != "无上下文消息":
                _line = _csum[:600]
                if _topic and _topic != "general":
                    _line = f"主题倾向: {_topic}。{_line}"
                prompt_parts.append(
                    "【近期聊天脉络（助手侧参考，请自然承接、勿复述标签）】\n" + _line
                )
        _rp = (context.get("_relationship_prompt_block") or "").strip()
        if _rp and _is_companion:
            prompt_parts.append(_rp)
        _epi = (context.get("_episodic_memory_text") or "").strip()
        if _epi:
            prompt_parts.append(
                "【用户长期记忆要点（简要事实，自然承接即可；不要机械复述「我记得你说过」）】\n"
                + _epi
            )
        _slo = (context.get("_slow_think_outline") or "").strip()
        if _slo:
            prompt_parts.append(
                "【慢思考规划（内部策略，请自然融入回复，勿逐条复读）】\n" + _slo[:2800]
            )
        # 添加用户信息
        user_id = context.get('user_id')
        if user_id:
            prompt_parts.append(f"用户ID: {user_id}")
        
        # 添加上次对话时间
        last_message_time = context.get('last_message_time')
        if last_message_time:
            prompt_parts.append(f"上次消息时间: {last_message_time}")
        
        # 添加意图信息
        intent = context.get('intent')
        if intent:
            prompt_parts.append(f"用户意图: {intent}")
        
        # 添加上一条用户消息（便于理解「之前说过/给过」等）
        last_message = context.get('last_message')
        if last_message:
            prompt_parts.append(f"用户本条消息（当前轮）: {last_message}")
        
        topic_switch = context.get('_topic_switch_hint')
        if topic_switch:
            prompt_parts.append(f"【话题切换——注意】\n{topic_switch}")

        if context.get("_channel_followup_brief") and not _is_companion:
            prompt_parts.append(
                "【追问简短回复 —— 最高优先级】\n"
                "用户刚看过你上一条关于通道成功率/状态的回复；本条是短追问或确认。\n"
                "禁止复述上一条里各通道成功率数字与「都正常/都可用」等整段；用一两句话即可："
                "可答「和刚才一致」、或只回应追问点（例如问波动则只谈波动与建议）。"
            )

        last_reply = context.get('last_reply')
        if last_reply:
            anti_repeat = context.get('_anti_repeat_hint')
            if anti_repeat:
                _who = "你不是复读机，要像真人一样自然换个说法。" if _is_companion else "你是一个真人客服，不是复读机。"
                prompt_parts.append(
                    f"【角度切换指令——必须遵守】\n"
                    f"你上一条回复是：「{last_reply[:200]}」\n"
                    f"用户又问了类似问题。{_who}\n"
                    f"具体要求：{anti_repeat}\n"
                    f"禁止与上条回复使用相同的开头词和句式。"
                )
            else:
                prompt_parts.append(f"上次回复: {last_reply}")
        
        # 添加对话阶段
        stage = context.get('stage')
        if stage:
            prompt_parts.append(f"对话阶段: {stage}")
        
        # 用户刚发的图片/截图内容（Vision 或 OCR），仅根据此真实内容回复
        image_ocr_text = context.get('image_ocr_text')
        if image_ocr_text:
            prompt_parts.append(f"用户刚发的图片/截图内容（Vision/OCR）:\n{image_ocr_text[:2000]}")
        
        # 近期群内机器人/通知消息（支付域可参考订单/通道；陪聊域不注入以免模型接工作话）
        recent_bot = context.get('recent_bot_messages')
        if recent_bot and not _is_companion:
            lines = []
            for item in (recent_bot[:15] if isinstance(recent_bot, list) else []):
                who = item.get('from', '') if isinstance(item, dict) else ''
                txt = item.get('text', '') if isinstance(item, dict) else str(item)
                if txt:
                    lines.append(f"[{who}]: {txt[:600]}")
            if lines:
                prompt_parts.append("近期群内机器人/通知消息（可参考）:\n" + "\n".join(lines))
        
        # 通道实时状态（仅业务域；陪聊域不注入）
        channel_status = ("" if _is_companion else context.get("channel_status_info", "") or "").strip()
        _live_metrics = bool(context.get("_channel_metrics_live_only")) and not _is_companion
        if channel_status:
            _fee_block = ""
            if _live_metrics:
                _fee_block = (
                    "- 本条为成功率或「费率/手续费」类咨询：只使用上方数据中的**状态**与**成功率百分比**作答；"
                    "**禁止**说出任何手续费/费率的具体数值或比例（含 x%、千分之几）；"
                    "若用户追问费率，引导联系**业务主管**或**人工客服**对接；"
                    "禁止引导去商户后台查费率、禁止说「客服无权限查费率」类话术。\n"
                    "- 若用户一句话里同时提到成功率和费率：只回答成功率与各通道状态；费率不报价。\n"
                )
            else:
                _fee_block = (
                    "- 客户问单笔限额/额度时，用上面的「单笔限额」数值；"
                    "对话中**禁止**报手续费/费率的具体数值或比例；费率请咨询业务主管或人工客服。\n"
                )
            _tail_kb = (
                "本类咨询**不要**引用知识库中含具体费率数字的话术；通道状态与成功率以上方实时数据为准；"
                "手续费/费率数值不对客宣读。"
                if _live_metrics
                else "知识库模板仅供话术风格参考，通道的具体状态必须以上面的实时数据为准；"
                "勿在回复中写出具体费率数值或比例。"
            )
            prompt_parts.append(
                f"【当前通道实时数据 ★★★ 唯一数据源 ★★★】\n{channel_status}\n"
                f"⚠️⚠️⚠️ 以下规则必须严格遵守：\n"
                f"- 回复中的所有数字（成功率、限额等）必须且只能来自上方实时数据，禁止使用其他任何来源的数字\n"
                f"- 如果代收和代付的成功率不同，必须分别列出，不能只报一个笼统数字\n"
                f"- **禁止**在回复中出现「PIX」「pix」或巴西 PIX 相关内容（该业务已永久下架，对客视同不存在）。\n"
                f"- 每个通道的状态以上面的实时数据为准，忽略知识库示例中的旧状态描述\n"
                f"- 只能提及上面列出的可用通道，不要编造不存在的通道信息\n"
                f"- 如果上面列出了'已禁用通道'，当客户问到这些通道时，回复'该通道目前已下线/不可用'，不要说它正常\n"
                f"- 状态=正常 → 通道正常，可以正常提交\n"
                f"- 状态=维护中 → 暂不可用，恢复后通知\n"
                f"- 状态=波动 → 通道存在波动，建议控制提交量或稍后再试\n"
                f"- 客户问成功率时，用上面的实际成功率数值回复\n"
                f"{_fee_block}"
                f"- 如果之前对话中你说过某通道'维护中'或'正常'但实时数据显示不同状态，以实时数据为准，可以说'刚更新了'\n"
                f"{_tail_kb}"
            )

        # 成功率/费率类咨询但暂无实时行时：仍注入硬性约束（陪聊域跳过）
        if _live_metrics and not channel_status and not _is_companion:
            prompt_parts.append(
                "【成功率/费率类咨询 —— 硬性约束】\n"
                "当前未注入通道实时数据行；不要编造费率数值。"
                "禁止在对话中说出任何手续费/费率的具体数值或比例；"
                "费率请咨询业务主管或人工客服。"
                "禁止引导用户去商户后台或后台查看费率、禁止「无权限查费率」。"
            )

        # K1: 早期对话摘要注入（来自规则引擎压缩）
        _conv_summary = (context.get("_conversation_summary") or "").strip()
        if _conv_summary:
            prompt_parts.append(
                f"【早期对话摘要】{_conv_summary}\n"
                f"（以上为之前对话的关键信息压缩，请结合当前对话和上条消息回答。）"
            )

        kb_ctx = context.get("kb_context", "").strip()
        if kb_ctx and not _live_metrics:
            if _is_companion:
                prompt_parts.append(
                    "\n【参考片段（语气用，非工作指令）】\n"
                    f"{kb_ctx}\n"
                    "不要主动提起查单、通道状态、支付、费率等工作话题；除非用户先说到这些词。"
                )
            else:
                _kb_cleaned = kb_ctx
                if channel_status:
                    import re as _re
                    _kb_cleaned = _re.sub(
                        r'成功率[：:]\s*\d+[\.\d]*%?',
                        '成功率：见上方实时数据',
                        _kb_cleaned
                    )
                prompt_parts.append(
                    f"\n【知识库参考（仅供话术风格参考）】\n"
                    f"⚠️ 重要：知识库中的任何成功率数值、通道状态均已过期，严禁使用！\n"
                    f"通道的成功率、状态、限额等数据必须且只能使用上方【当前通道实时数据】中的数值。\n"
                    f"知识库仅用于参考回复的语气和格式：\n{_kb_cleaned}"
                )
        # _live_metrics 时主流程已跳过 KB 检索；若仍带有 kb_context，不注入，避免「商户后台」等模板进入模型

        # L4: 用户画像注入
        _profile = context.get("_user_profile")
        if isinstance(_profile, dict) and _profile.get("type"):
            _P_MAP = {
                "new": "新用户：需耐心引导，语言简单明了，主动提供操作说明。",
                "regular": "老用户：有基本了解，可省略基础说明，直奔主题。",
                "veteran": "资深用户：非常熟悉流程，用专业简洁的方式沟通。",
                "vip": "高价值客户：高频业务用户，优先响应，提供 VIP 级别的详细服务。",
            }
            _T_MAP = {
                "impatient": "用户偏急躁，请简短高效回复，先给结论再解释。",
                "frustrated": "用户多次投诉，需特别注意语气安抚和问题解决。",
                "friendly": "用户态度友善，可稍作寒暄但不啰嗦。",
            }
            _hints = []
            _p_hint = _P_MAP.get(_profile["type"])
            if _p_hint:
                _hints.append(_p_hint)
            _t_hint = _T_MAP.get(_profile.get("tone", "standard"))
            if _t_hint:
                _hints.append(_t_hint)
            if _hints:
                prompt_parts.append("【用户画像】" + " ".join(_hints))
            # K3: 满意度预警
            if _profile.get("at_risk"):
                prompt_parts.append(
                    "【⚠ 满意度预警】该用户满意度评分极低，可能即将流失。"
                    "务必：1) 先真诚道歉/共情；2) 给出明确解决方案和时间；"
                    "3) 提供人工客服升级入口（如「如需进一步帮助可联系专属客服」）。"
                )

        # H3: 意图链模式注入
        _chain_pat = context.get("_chain_pattern")
        if isinstance(_chain_pat, dict) and _chain_pat.get("hint"):
            _case_id = context.get("_case_id", "")
            _chain_label = f"（案例 {_case_id}）" if _case_id else ""
            prompt_parts.append(
                f"【对话升级模式{_chain_label}】{_chain_pat['hint']}"
            )

        if prompt_parts:
            return "上下文信息:\n" + "\n".join([f"- {part}" for part in prompt_parts])
        
        return ""

    _LANG_PATTERNS = [
        (r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", "ar_ur"),
        (r"[\u0900-\u097F]", "hi"),
        (r"[\u0400-\u04FF]", "ru"),
        (r"[\u3040-\u309F\u30A0-\u30FF]", "ja"),
        (r"[\uAC00-\uD7AF\u1100-\u11FF]", "ko"),
        (r"[\u0E00-\u0E7F]", "th"),
        (r"[\u0A00-\u0A7F]", "pa"),
        (r"[\u0980-\u09FF]", "bn"),
    ]

    _LANG_NAMES = {
        "zh": "中文", "en": "English", "ar_ur": "Arabic/Urdu",
        "hi": "Hindi", "ru": "Русский", "ja": "日本語",
        "ko": "한국어", "th": "ภาษาไทย", "pa": "ਪੰਜਾਬੀ",
        "bn": "বাংলা", "pt": "Português", "es": "Español",
        "fr": "Français", "de": "Deutsch", "it": "Italiano",
        "tr": "Türkçe", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
    }

    _LATIN_LANG_HINTS = {
        "pt": ["obrigado", "pagamento", "consulta", "transferência", "reembolso", "ajuda", "saldo", "taxa"],
        "es": ["gracias", "pago", "consulta", "transferencia", "reembolso", "ayuda", "saldo", "tasa", "monto"],
        "fr": ["merci", "paiement", "virement", "remboursement", "aide", "solde", "taux", "montant", "frais"],
        "de": ["danke", "zahlung", "überweisung", "rückerstattung", "hilfe", "guthaben", "kurs", "betrag", "gebühr"],
        "it": ["grazie", "pagamento", "trasferimento", "rimborso", "aiuto", "saldo", "tasso", "importo", "tariffa"],
        "tr": ["teşekkür", "ödeme", "transfer", "iade", "yardım", "bakiye", "oran", "tutar", "ücret", "sipariş"],
        "vi": ["cảm ơn", "thanh toán", "chuyển khoản", "hoàn tiền", "hỗ trợ", "số dư", "tỷ giá", "đơn hàng"],
        "id": ["terima kasih", "pembayaran", "pemindahan", "bayaran", "bantuan", "saldo", "pesanan"],
    }

    _SHORT_EN_WORDS = frozenset({
        "ok", "hi", "no", "yes", "hey", "bye", "thx", "ty", "gm", "gn",
        "good", "fine", "done", "help", "how", "why", "what", "who",
        "pls", "plz", "brb", "omg", "wtf", "lol", "asap",
    })

    def _detect_message_language(self, text: str) -> str:
        """粗判用户消息主语言，支持中英日韩阿拉伯乌尔都印地俄语等15+种语言。"""
        if not text or not isinstance(text, str):
            return "zh"
        t = text.strip()
        if not t:
            return "zh"
        t = re.sub(r"@\w+", "", t).strip()
        if not t:
            return "zh"

        cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
        letters = len(re.findall(r"[A-Za-z]", t))

        # \u2605 \u975e CJK \u7684 script \u4f18\u5148\uff08hiragana/katakana/hangul/arabic/...\uff09
        # \u542b\u6c49\u5b57 + \u5047\u540d\u65f6\uff08\u5982\u300c\u4eca\u65e5\u306f\u826f\u3044\u5929\u6c17\u300d\uff09\u4e5f\u5e94\u5224 ja\uff0c\u56e0\u4e3a\u5047\u540d\u662f\u65e5\u6587\u4e13\u5c5e
        for pattern, lang in self._LANG_PATTERNS:
            if re.search(pattern, t):
                return lang

        # \u542b\u6c49\u5b57\u4f46\u65e0\u4efb\u4f55 script \u2192 \u5224 zh\uff08\u4e2d\u6587\uff09
        if cjk > 0:
            if letters > 0 and letters > cjk * 3:
                pass
            else:
                return "zh"

        if letters >= 3:
            t_lower = t.lower()
            for lang, hints in self._LATIN_LANG_HINTS.items():
                if any(h in t_lower for h in hints):
                    return lang
            return "en"

        t_lower = t.lower().strip("?？!！.。, ")
        if t_lower in self._SHORT_EN_WORDS:
            return "en"

        if letters > 0 and cjk == 0:
            digits = len(re.findall(r"\d", t))
            if letters + digits >= len(t.replace(" ", "")) * 0.8:
                return "en"

        return "zh"

    def _parse_memory_facts_json(self, raw: str) -> List[str]:
        """Parse model output {\"facts\": [\"...\"]}."""
        if not raw or not isinstance(raw, str):
            return []
        t = raw.strip()
        if t.startswith("```"):
            t = re.sub(r"^```\w*\s*", "", t)
            t = re.sub(r"\s*```\s*$", "", t).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            return []
        facts = obj.get("facts") if isinstance(obj, dict) else None
        if not isinstance(facts, list):
            return []
        out: List[str] = []
        for x in facts:
            s = str(x).strip()
            if 2 <= len(s) <= 500:
                out.append(s)
            if len(out) >= 6:
                break
        return out

    async def extract_memory_bullets(self, user_msg: str, assistant_msg: str) -> List[str]:
        """
        One cheap LLM call: extract 0–4 durable user-specific facts from this turn.
        Skips when circuit breaker is open (caller may still use heuristics).
        """
        u = (user_msg or "").strip()
        a = (assistant_msg or "").strip()
        if len(u) < 2 or len(a) < 2:
            return []
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            self.logger.debug("extract_memory_bullets skipped: circuit open")
            return []

        sys_inst = (
            "你是对话记忆抽取器。根据本轮用户消息与助手回复，抽取值得后续聊天记住的客观信息"
            "（称呼、偏好、用户刚透露的重要事实、简单约定）。不要抽取通道费率、订单号等业务敏感数字"
            "除非用户明确说这是 TA 自己的。输出严格为一行 JSON，不要 markdown："
            '{"facts":["..."]} facts 为 0～4 条中文短句，无则 []。'
        )
        usr = f"USER:\n{u[:2000]}\n\nASSISTANT:\n{a[:2000]}"

        try:
            if self._use_openai_compat and self._oa_client:
                async def _call():
                    return await self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": usr},
                        ],
                        temperature=0.15,
                        max_tokens=320,
                    )

                response = await asyncio.wait_for(_call(), timeout=14.0)
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                return self._parse_memory_facts_json(raw)

            if GENAI_AVAILABLE and self.client:
                use_model = self.model
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.15,
                    max_output_tokens=320,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=use_model,
                        contents=[types.Content(role="user", parts=[types.Part(text=usr)])],
                        config=config,
                    ),
                    timeout=14.0,
                )
                raw = ""
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                    except (ValueError, AttributeError, IndexError):
                        raw = ""
                return self._parse_memory_facts_json(raw)
        except asyncio.TimeoutError:
            self.logger.debug("extract_memory_bullets timeout")
        except Exception as e:
            self.logger.debug("extract_memory_bullets failed: %s", e)
        return []

    # ── P3-6：LLM 语义摘要（会话长尾压缩） ─────────────
    async def summarize_conversation(
        self,
        history: List[Dict[str, str]],
        *,
        max_chars: int = 200,
        timeout_sec: float = 14.0,
    ) -> str:
        """把 conversation_history（[{role,content},...]）压成一段≤max_chars 的中文摘要。

        失败返回空串（caller 可回退规则引擎）。
        """
        if not history or not isinstance(history, list):
            return ""
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return ""
        # 预处理：截断 + 拼文本
        lines: List[str] = []
        for m in history[-40:]:  # 最多送最近 40 条
            role = "用户" if m.get("role") == "user" else "助手"
            txt = str(m.get("content") or "")[:240].replace("\n", " ")
            if txt:
                lines.append(f"{role}: {txt}")
        if len(lines) < 4:
            return ""
        joined = "\n".join(lines)[:6000]

        sys_inst = (
            "你是对话摘要器。把以下对话压缩成一段中文摘要，抓住：\n"
            "1) 用户的关键诉求/话题演进；2) 已达成的共识；3) 用户透露的稳定事实（称呼、偏好、约定）。\n"
            f"输出纯中文（≤{int(max_chars)} 字），不要 markdown、不要 json、不要换行，"
            "一段话写完。"
        )
        try:
            if self._use_openai_compat and self._oa_client:
                async def _call():
                    return await self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": joined},
                        ],
                        temperature=0.2,
                        max_tokens=400,
                    )
                response = await asyncio.wait_for(_call(), timeout=timeout_sec)
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                return raw[:max_chars]

            if GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.2,
                    max_output_tokens=400,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=joined,
                        config=config,
                    ),
                    timeout=timeout_sec,
                )
                if response and response.candidates:
                    try:
                        return (response.text or "").strip()[:max_chars]
                    except (ValueError, AttributeError, IndexError):
                        return ""
        except asyncio.TimeoutError:
            self.logger.debug("summarize_conversation timeout")
        except Exception as e:
            self.logger.debug("summarize_conversation failed: %s", e)
        return ""

    # ── P7-4：长期记忆蒸馏（从 working summary + 历史 → 稳定 facts） ──
    async def extract_long_term_facts(
        self,
        *,
        working_summary: str,
        recent_history: List[Dict[str, str]],
        existing_facts: Optional[List[str]] = None,
        max_facts: int = 15,
        timeout_sec: float = 15.0,
    ) -> List[str]:
        """从 working_summary + 最近历史 + 已有 facts 蒸馏出"不易变"的长期事实。

        返回新的 facts 列表（已合并、去重、限长）。失败返回 existing_facts 副本。

        事实类型：称呼/自称姓名、常驻地区、长期偏好、已承诺的约定、
        已明确的拒绝事项、产品/服务意向、家庭/职业信息。
        **不**包含：一次性情绪、当下聊的话题、临时问题。
        """
        existing = [s for s in (existing_facts or []) if isinstance(s, str) and s.strip()]
        if not working_summary and len(recent_history) < 4:
            return existing[:max_facts]
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return existing[:max_facts]

        lines: List[str] = []
        for m in (recent_history or [])[-20:]:
            role = "用户" if m.get("role") == "user" else "助手"
            txt = str(m.get("content") or "")[:200].replace("\n", " ")
            if txt:
                lines.append(f"{role}: {txt}")
        hist_block = "\n".join(lines)[:3500]

        existing_block = ""
        if existing:
            existing_block = "已记录的事实（尽量保留，除非被明确否定/更新）：\n- " + \
                "\n- ".join(existing[:max_facts])

        sys_inst = (
            "你是对话长期记忆管理器。从以下对话摘要 + 最近片段里，抽取"
            "稳定的、跨话题依然成立的事实（姓名/地区/长期偏好/已承诺约定/"
            "明确拒绝/产品意向/家庭或职业信息）。\n"
            "规则：\n"
            "1) 只保留'跨天仍有效'的信息，不记录一次性话题或即时情绪。\n"
            "2) 每条 ≤40 字中文；单行 bullet。\n"
            f"3) 输出 JSON 数组，最多 {int(max_facts)} 项；若无新信息就回已有。\n"
            "4) 若发现已有事实被新内容否定/更正，用新版覆盖；否则合并保留。\n"
            "5) 严格输出 JSON 数组，不要任何解释文本、不要 markdown。"
        )
        user_block = (
            f"工作摘要：\n{working_summary or '(暂无)'}\n\n"
            f"最近对话：\n{hist_block or '(暂无)'}\n\n"
            f"{existing_block}"
        )

        def _parse(raw: str) -> List[str]:
            s = (raw or "").strip()
            if s.startswith("```"):
                s = re.sub(r"^```\w*\s*", "", s)
                s = re.sub(r"\s*```\s*$", "", s).strip()
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                # 兜底：按行拆
                items = [
                    re.sub(r"^[-*\d.\s)]+", "", ln).strip()
                    for ln in s.splitlines() if ln.strip()
                ]
                return [x[:80] for x in items if x][:max_facts]
            if isinstance(obj, list):
                out: List[str] = []
                for x in obj:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip()[:80])
                    elif isinstance(x, dict):
                        v = str(x.get("fact") or x.get("text") or "").strip()
                        if v:
                            out.append(v[:80])
                return out[:max_facts]
            return []

        try:
            if self._use_openai_compat and self._oa_client:
                # DeepSeek 不支持 array 顶层的 json_object —— 包一层
                sys_inst_wrap = sys_inst + "\n输出格式示例：{\"facts\": [\"事实1\", \"事实2\"]}"
                async def _call_wrap():
                    return await self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst_wrap},
                            {"role": "user", "content": user_block},
                        ],
                        temperature=0.1,
                        max_tokens=800,
                        response_format={"type": "json_object"},
                    )
                try:
                    response = await asyncio.wait_for(_call_wrap(), timeout=timeout_sec)
                except Exception:
                    # 某些 provider 不支持 response_format → 降级不带
                    response = await asyncio.wait_for(
                        self._oa_client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "system", "content": sys_inst},
                                {"role": "user", "content": user_block},
                            ],
                            temperature=0.1,
                            max_tokens=800,
                        ),
                        timeout=timeout_sec,
                    )
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                # 尝试 parse 成 {"facts":[...]}
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        facts = obj.get("facts") or obj.get("items") or []
                        if isinstance(facts, list):
                            parsed = [str(x).strip()[:80] for x in facts if str(x).strip()][:max_facts]
                            return parsed or existing[:max_facts]
                except json.JSONDecodeError:
                    pass
                parsed = _parse(raw)
                return parsed or existing[:max_facts]

            if GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.1,
                    max_output_tokens=800,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=user_block,
                        config=config,
                    ),
                    timeout=timeout_sec,
                )
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                        parsed = _parse(raw)
                        return parsed or existing[:max_facts]
                    except (ValueError, AttributeError, IndexError):
                        return existing[:max_facts]
        except asyncio.TimeoutError:
            self.logger.debug("extract_long_term_facts timeout")
        except Exception as e:
            self.logger.debug("extract_long_term_facts failed: %s", e)
        return existing[:max_facts]

    def _format_slow_think_raw(self, raw: str) -> str:
        """Parse planning JSON to compact text for stage-2 context."""
        if not raw or not isinstance(raw, str):
            return ""
        t = raw.strip()
        if t.startswith("```"):
            t = re.sub(r"^```\w*\s*", "", t)
            t = re.sub(r"\s*```\s*$", "", t).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            return t[:2000]
        if not isinstance(obj, dict):
            return t[:2000]
        lines: List[str] = []
        ang = obj.get("angles")
        if isinstance(ang, list):
            for i, x in enumerate(ang[:8]):
                s = str(x).strip()
                if s:
                    lines.append(f"{i + 1}. {s}")
        rk = obj.get("risks")
        if isinstance(rk, list) and rk:
            lines.append("风险/遗漏:")
            for x in rk[:5]:
                s = str(x).strip()
                if s:
                    lines.append(f"- {s}")
        return "\n".join(lines)[:2800]

    async def slow_think_outline(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        stage1_max_tokens: int = 400,
    ) -> str:
        """
        Stage-1: internal planning only (not shown to end user verbatim).
        Returns compact bullet text for injection into stage-2 system context.
        """
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return ""
        u = (user_message or "").strip()
        if len(u) < 2:
            return ""
        ctx = context or {}
        parts: List[str] = []
        ep = (ctx.get("_episodic_memory_text") or "").strip()
        if ep:
            parts.append("【用户记忆要点】\n" + ep[:1000])
        kb = (ctx.get("kb_context") or "").strip()
        if kb:
            parts.append("【知识库参考片段】\n" + kb[:600])
        ca = ctx.get("context_analysis")
        if isinstance(ca, dict):
            em = (ca.get("user_emotion") or "").strip()
            if em:
                parts.append(f"【情绪粗判】{em}")
        pack = "\n\n".join(parts)
        sys_inst = (
            "你是对话策略助理，只做内部规划，不直接对用户输出。"
            "根据用户消息与下列材料，输出严格一行 JSON（不要 markdown）："
            '{"angles":["角度1","角度2"],"risks":["可选风险"]}'
            "angles 为 2～5 条中文短句，覆盖回应重点；risks 0～3 条。"
        )
        usr = f"用户消息：\n{u[:1800]}\n\n---\n{pack}" if pack else f"用户消息：\n{u[:1800]}"
        mt = max(120, min(int(stage1_max_tokens or 400), 800))
        try:
            raw = ""
            if self._use_openai_compat and self._oa_client:
                response = await asyncio.wait_for(
                    self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": usr},
                        ],
                        temperature=0.2,
                        max_tokens=mt,
                    ),
                    timeout=18.0,
                )
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
            elif GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.2,
                    max_output_tokens=mt,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=[types.Content(role="user", parts=[types.Part(text=usr)])],
                        config=config,
                    ),
                    timeout=18.0,
                )
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                    except (ValueError, AttributeError, IndexError):
                        raw = ""
            return self._format_slow_think_raw(raw)
        except asyncio.TimeoutError:
            self.logger.debug("slow_think_outline timeout")
        except Exception as e:
            self.logger.debug("slow_think_outline failed: %s", e)
        return ""
    
    async def generate_reply_with_intent(
        self,
        user_message: str,
        intent: str,
        user_context: Dict[str, Any],
        strategy_overrides: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        基于意图生成回复，支持策略参数覆盖 + 自动注入对话历史。
        """
        enhanced_context = user_context.copy()
        enhanced_context['intent'] = intent

        # L2: 从 user_context 提取对话历史，传递给 generate_reply
        _conv_hist = user_context.get('_conversation_history')
        if isinstance(_conv_hist, list) and _conv_hist:
            conversation_history = _conv_hist
        else:
            conversation_history = None

        intent_supplement = self._get_intent_prompt(intent)
        if intent_supplement:
            enhanced_context["_intent_supplement"] = intent_supplement

        if not (enhanced_context.get("_current_user_message_for_lang") or "").strip():
            enhanced_context["_current_user_message_for_lang"] = user_message
        reply = await self.generate_reply(
            user_message, enhanced_context,
            conversation_history=conversation_history,
            strategy_overrides=strategy_overrides)
        
        return reply
    
    _INTENT_SUPPLEMENTS = {
        "complaint": (
            "【当前意图：投诉/不满 / Intent: Complaint】用户正在投诉或表达不满。"
            "请先认同情绪、表达理解（如'确实给您添麻烦了'），然后给出明确的处理方案和时间预期。"
            "不要敷衍推脱，也不要过度道歉。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "order_query": (
            "【当前意图：订单查询 / Intent: Order Query】用户在查询订单。"
            "优先引用上下文中已有的订单号/交易号/识图结果。"
            "有凭证则确认收到+处理中，无凭证则引导提供。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "channel_info": (
            "【当前意图：通道/额度咨询 / Intent: Channel Info】用户在问通道状态、额度或成功率。"
            "只客观告知实时数据中列出的通道状态，不推荐特定通道。分条列出，简洁明了。"
            "严禁提及实时数据中未列出的通道名称。"
            "禁止出现「PIX」「pix」或巴西 PIX 相关内容（已永久下架）。"
            "若用户问成功率、或问手续费/费率、或两者同时问：只答各通道成功率与运行状态；"
            "禁止说出任何费率/手续费的具体数值或比例；费率引导联系业务主管或人工客服，不要引导去后台查费率。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "greeting": (
            "【当前意图：打招呼 / Intent: Greeting】用户在问候或开场。"
            "简短、活泼、像朋友接话即可，不要长篇大论。"
            "Match the user's language - if they greet in English, respond in English."
        ),
    }

    def _get_intent_prompt(self, intent: str) -> Optional[str]:
        """获取意图特定的补充提示（追加到系统提示末尾，非替换）"""
        try:
            _cfg = self.config.config if self.config and hasattr(self.config, "config") else {}
            _conv = isinstance(_cfg, dict) and effective_domain_name(_cfg) == "conversion"
        except Exception:
            _conv = False
        if intent == "greeting" and _conv:
            return (
                "【当前意图：打招呼 / Intent: Greeting】对方在问你在不在、或轻轻开场。"
                "你们是**情感陪伴/恋人向**私聊，不是客服台或工单系统。"
                "**严禁**使用「有什么可以帮您/帮您的吗」「需要什么服务」「请问有什么可以」"
                "等柜台话术。用一两句像女友/好友微信：如「在呀」「嗯嗯我在～」「找我呀？」「怎么啦」；"
                "用户只发「在」「在吗」时要**短、自然、不重复同一句**，不要接业务办理暗示。"
                "Match the user's language."
            )
        return self._INTENT_SUPPLEMENTS.get(intent)
    
    async def should_reply_by_context(
        self,
        previous_message: str,
        previous_time: str,
        current_message: str,
    ) -> tuple[bool, str]:
        """
        根据「前一条消息 + 当前消息」让 AI 判断是否应回复。
        conversion 域：陪聊/情绪延续；payment 等业务域：是否与订单/通道等工作相关。
        """
        try:
            _ai_cfg = (self.config.config or {}).get("ai", {}) if self.config else {}
            _cfg = self.config.config if self.config and hasattr(self.config, "config") else {}
            _companion = isinstance(_cfg, dict) and effective_domain_name(_cfg) == "conversion"
            _disp_name = (_ai_cfg.get("ai_name") or ("小桃" if _companion else "小优")).strip() or (
                "小桃" if _companion else "小优"
            )
            if _companion:
                _role_intro = (
                    f"你是「{_disp_name}」，主打轻松陪聊和情绪陪伴，像朋友在线上打字聊天，"
                    "不负责查单、通道、支付等业务办理。"
                )
                _gate_hint = (
                    "根据「前一条消息」和「当前消息」判断：对方是否在延续对话、找你闲聊、倾诉情绪、"
                    "提问、回应你，或明显在对你说话——需要接话、安慰、陪聊、回答时回答 YES；"
                    "若是群内其他人彼此对话、纯噪音或与对话完全无关则回答 NO。"
                )
            else:
                _role_intro = f"你是智能客服{_disp_name}，负责订单查询、通道状态、代收代付等。"
                _gate_hint = (
                    "根据「前一条消息」和「当前消息」判断：当前这条是否在跟你说话或与你的工作相关"
                    "（订单、查单、通道、支付、回调、咨询等）。"
                    "仅当与工作相关且应回复时回答 YES，否则回答 NO。第二行用一句话说明原因。"
                )
            _user_q = (
                "当前消息是否需要你接话、陪聊或回复？回答 YES 或 NO，第二行写原因。"
                if _companion
                else "当前消息是否与你的工作相关、是否需要你回复？回答 YES 或 NO，第二行写原因。"
            )
            if self._use_openai_compat:
                if not self._oa_client:
                    return False, "AI客户端未初始化"
                sys_content = (
                    f"{_role_intro}\n{_gate_hint}\n"
                    "格式严格为：第一行 YES 或 NO，第二行原因。"
                )
                user_content = (
                    f"前一条消息（时间: {previous_time}）:\n{previous_message[:500]}\n\n"
                    f"当前消息:\n{current_message[:500]}\n\n"
                    f"{_user_q}"
                )
                response = await self._oa_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": sys_content},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=150,
                    temperature=0.3,
                )
                raw = (response.choices[0].message.content or "").strip() if response and response.choices else ""
                lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
                first = (lines[0] or "").upper()
                reason = " ".join(lines[1:3]) if len(lines) > 1 else first
                if first.startswith("YES"):
                    return True, reason
                if first.startswith("NO"):
                    return False, reason
                if "YES" in raw.upper():
                    return True, raw[:120]
                return False, raw[:120]
            if not self.client:
                return False, "AI客户端未初始化"
            sys_content = (
                f"{_role_intro}\n{_gate_hint}\n"
                "格式严格为：第一行 YES 或 NO，第二行原因。"
            )
            user_content = (
                f"前一条消息（时间: {previous_time}）:\n{previous_message[:500]}\n\n"
                f"当前消息:\n{current_message[:500]}\n\n"
                f"{_user_q}"
            )
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=sys_content,
                    max_output_tokens=150,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    temperature=0.3,
                ),
            )
            if not response or not response.text:
                return False, "AI返回空"
            raw = response.text.strip()
            lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
            first = (lines[0] or "").upper()
            reason = " ".join(lines[1:3]) if len(lines) > 1 else first
            if first.startswith("YES"):
                return True, reason
            if first.startswith("NO"):
                return False, reason
            if "YES" in raw.upper():
                return True, raw[:120]
            return False, raw[:120]
        except Exception as e:
            self.logger.debug("should_reply_by_context 异常: %s", e)
            return False, str(e)

    async def chat(
        self,
        prompt: str,
        strategy_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """短提示词调用（如 L2 触发置信度），走与 generate_reply 相同的提供方。"""
        return await self.generate_reply(
            prompt,
            context=None,
            conversation_history=None,
            strategy_overrides=strategy_overrides,
        )

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "last_call_time": self.last_call_time,
            "model": self.model,
            "temperature": self.temperature,
            "provider": self._provider,
        }
    
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """
        调用 Gemini Embedding API 获取文本向量。
        支持批量输入，返回顺序对应输入顺序。
        失败时返回空列表，调用方应做降级处理。
        """
        if not texts:
            return []
        if self._use_openai_compat:
            # 未配置独立 embedding 端点且未指定模型时，跳过（避免向仅支持 chat 的 API 误请求 embeddings）
            _em = (self._embedding_model or "").strip()
            if not _em or _em.lower() in ("none", "off", "disabled"):
                return []
            _emb_cli = self._oa_embed_client or self._oa_client
            if not _emb_cli:
                return []
            try:
                result = await _emb_cli.embeddings.create(
                    model=self._embedding_model,
                    input=texts,
                )
                if result and result.data:
                    return [list(d.embedding) for d in result.data]
                return []
            except Exception as _e:
                self.logger.warning("Embedding API 调用失败: %s", _e)
                return []
        if not self.client:
            return []
        try:
            result = await self.client.aio.models.embed_content(
                model=self._embedding_model,
                contents=texts,
            )
            if result and result.embeddings:
                return [emb.values for emb in result.embeddings]
            return []
        except Exception as _e:
            self.logger.warning("Embedding API 调用失败: %s", _e)
            return []

    async def embed_with_fallback(self, texts: List[str]) -> List[List[float]]:
        """
        批量嵌入；若 API 返回向量数与输入不一致，则逐条请求（兼容部分批处理行为）。
        返回列表长度与 texts 一致，失败位置为空列表。
        """
        if not texts:
            return []
        out = await self.embed(texts)
        if out and len(out) == len(texts):
            return out
        if len(texts) == 1:
            return out if out else [[]]
        self.logger.warning(
            "Embedding 批量返回数量=%s 与输入=%s 不一致，改为逐条请求",
            len(out) if out else 0, len(texts),
        )
        result: List[List[float]] = []
        for t in texts:
            one = await self.embed([t])
            result.append(one[0] if one else [])
            await asyncio.sleep(0.05)
        return result

    async def cleanup(self):
        """清理资源"""
        self.logger.info("AI 客户端清理完成")
