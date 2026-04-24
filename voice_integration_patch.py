#!/usr/bin/env python3
"""
语音识别集成补丁 - 展示如何修改telegram_client.py支持语音消息
"""

PATCH_CONTENT = '''
# ============================================================================
# 语音识别集成补丁 - 修改telegram_client.py
# ============================================================================

# 1. 首先在文件顶部添加导入
# 在现有导入之后添加：
try:
    from src.voice_transcriber import VoiceTranscriberFactory
    VOICE_RECOGNITION_AVAILABLE = True
except ImportError:
    VOICE_RECOGNITION_AVAILABLE = False
    # 创建模拟类以便代码可以运行
    class VoiceTranscriberFactory:
        @staticmethod
        def create_transcriber(config):
            return None

# 2. 修改TelegramClient类的__init__方法
# 在现有初始化代码中添加：
class TelegramClient:
    def __init__(self, config, skill_manager, logger=None):
        # ... 现有代码 ...
        
        # 添加语音识别支持
        self.voice_recognition_enabled = False
        self.voice_transcriber = None
        
        # 检查并初始化语音识别
        self._init_voice_recognition(config)
        
    def _init_voice_recognition(self, config):
        """初始化语音识别"""
        try:
            voice_config = config.get_voice_recognition_config()
            if voice_config and voice_config.get('enabled', False):
                if VOICE_RECOGNITION_AVAILABLE:
                    self.voice_transcriber = VoiceTranscriberFactory.create_transcriber(voice_config)
                    self.voice_recognition_enabled = True
                    self.logger.info("✅ 语音识别服务初始化成功")
                else:
                    self.logger.warning("⚠️  语音识别依赖未安装，语音消息将被忽略")
            else:
                self.logger.debug("语音识别未启用")
        except Exception as e:
            self.logger.error(f"语音识别初始化失败: {e}")

# 3. 修改消息处理器以支持语音消息
# 修改handle_private_message和handle_group_message函数：
# 在现有if message.text or message.caption:条件之前添加语音处理

@self.client.on_message(filters.private)
async def handle_private_message(client, message: Message):
    """处理私聊消息"""
    try:
        # 首先尝试语音消息处理
        processed = False
        if self.voice_recognition_enabled and (message.voice or message.audio):
            processed = await self._process_voice_message(message)
        
        # 然后处理文本消息（如果语音处理失败或没有语音）
        if not processed and (message.text or message.caption):
            await self._process_message(message)
        elif not processed:
            self.logger.debug(f"忽略非文本私聊消息: {message.chat.id}")
            
    except Exception as e:
        self.logger.error(f"处理私聊消息失败: {e}")

# 4. 添加语音消息处理方法
async def _process_voice_message(self, message: Message) -> bool:
    """处理语音消息
    
    Returns:
        bool: 是否成功处理
    """
    try:
        self.logger.info(f"收到语音消息，来自: {message.from_user.username}")
        
        # 下载语音文件
        voice_file = await self._download_voice_file(message)
        if not voice_file:
            self.logger.error("下载语音文件失败")
            return False
        
        # 转录语音为文本
        voice_config = self.config.get_voice_recognition_config()
        language = voice_config.get('language', 'zh')
        
        text = await self.voice_transcriber.transcribe_voice_message(
            str(voice_file), language
        )
        
        # 清理临时文件
        try:
            voice_file.unlink()
        except:
            pass
        
        if not text:
            self.logger.warning("语音转录失败或返回空文本")
            return False
        
        self.logger.info(f"语音转录成功: {text[:100]}...")
        
        # 将转录的文本作为普通消息处理
        await self._process_message_with_text(message, text)
        return True
        
    except Exception as e:
        self.logger.error(f"处理语音消息失败: {e}")
        return False

async def _download_voice_file(self, message: Message) -> Optional[Path]:
    """下载语音文件到临时目录"""
    try:
        # 确定文件扩展名
        if message.voice:
            file_attr = message.voice
            ext = ".ogg"  # Telegram语音通常是OGG格式
        elif message.audio:
            file_attr = message.audio
            ext = file_attr.mime_type.split('/')[-1] if file_attr.mime_type else ".mp3"
        else:
            return None
        
        # 创建临时文件路径
        temp_dir = Path(self.config.get_voice_recognition_config().get('temp_dir', './temp/voice'))
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        file_name = f"voice_{message.id}_{message.from_user.id}{ext}"
        file_path = temp_dir / file_name
        
        # 下载文件
        self.logger.info(f"下载语音文件: {file_path}")
        
        # Pyrogram的download方法
        await message.download(file_name=str(file_path))
        
        return file_path if file_path.exists() else None
        
    except Exception as e:
        self.logger.error(f"下载语音文件失败: {e}")
        return None

async def _process_message_with_text(self, message: Message, text: str):
    """使用转录的文本处理消息"""
    try:
        user_id = message.from_user.id if message.from_user else 0
        username = message.from_user.username if message.from_user else "unknown"
        chat_id = message.chat.id
        
        self.logger.info(f"处理转录文本 [{username}]: {text[:50]}...")
        
        # 放入消息队列（与文本消息相同）
        await self.message_queue.put({
            'message': message,
            'user_id': user_id,
            'username': username,
            'text': text,
            'chat_id': chat_id,
            'is_voice': True  # 标记来自语音消息
        })
        
    except Exception as e:
        self.logger.error(f"处理转录文本失败: {e}")

# 5. 添加配置支持
# 在config/config.yaml中添加：
'''
voice_recognition:
  enabled: true                # 启用语音识别
  provider: "whisper_local"    # 服务提供商: whisper_local, faster_whisper, openai
  
  # 本地Whisper配置
  whisper:
    model_size: "base"         # 模型大小: tiny, base, small, medium, large
    device: "cpu"             # 设备: cpu, cuda
    language: "zh"            # 语言代码: zh(中文), en(英文), auto(自动检测)
    download_root: "./models/whisper"
  
  # 临时文件配置
  temp_dir: "./temp/voice"
  max_file_size: 16777216     # 最大文件大小: 16MB
'''

# 6. 在ConfigManager中添加获取语音配置的方法
# 在src/utils/config_manager.py中添加：

'''
class ConfigManager:
    # ... 现有代码 ...
    
    def get_voice_recognition_config(self) -> Optional[Dict[str, Any]]:
        """获取语音识别配置"""
        return self.config.get('voice_recognition')
'''

# ============================================================================
# 总结：需要修改的文件
# ============================================================================
# 1. src/client/telegram_client.py - 主消息处理器
# 2. config/config.yaml - 添加语音识别配置
# 3. src/utils/config_manager.py - 添加配置获取方法
# 4. 新增: src/voice_transcriber.py - 语音转录服务
# 5. 新增: requirements.txt - 添加语音识别依赖
'''

def main():
    """主函数：展示集成步骤"""
    print("=" * 70)
    print("Telegram MTProto AI 语音识别集成指南")
    print("=" * 70)
    print()
    
    print("🎯 集成目标：")
    print("   使系统能够接收、转录和处理语音消息")
    print()
    
    print("📋 需要修改的文件：")
    files = [
        ("src/client/telegram_client.py", "主消息处理器，添加语音处理逻辑"),
        ("config/config.yaml", "添加语音识别配置项"),
        ("src/utils/config_manager.py", "添加语音配置获取方法"),
        ("src/voice_transcriber.py", "新增：语音转录服务"),
        ("requirements.txt", "添加语音识别依赖"),
    ]
    
    for file, desc in files:
        print(f"   📄 {file:40} {desc}")
    
    print()
    print("🔧 安装依赖：")
    print("   # 选项1：标准Whisper（推荐）")
    print("   pip install openai-whisper")
    print()
    print("   # 选项2：轻量快速版")
    print("   pip install faster-whisper")
    print()
    print("   # 选项3：GPU加速版")
    print("   pip install torch torchaudio")
    print("   pip install openai-whisper")
    
    print()
    print("⚙️ 配置示例（config/config.yaml）：")
    config_example = '''
voice_recognition:
  enabled: true
  provider: "whisper_local"
  whisper:
    model_size: "base"
    device: "cpu"
    language: "zh"
    download_root: "./models/whisper"
  temp_dir: "./temp/voice"
  max_file_size: 16777216
'''
    print(config_example)
    
    print()
    print("🧪 测试步骤：")
    steps = [
        ("1", "安装语音识别依赖"),
        ("2", "应用代码补丁"),
        ("3", "更新配置文件"),
        ("4", "重启系统"),
        ("5", "发送语音消息测试"),
        ("6", "检查日志确认识别结果"),
    ]
    
    for num, desc in steps:
        print(f"   {num}. {desc}")
    
    print()
    print("⏰ 预计时间：")
    print("   - 基础集成：2-3小时")
    print("   - 测试优化：1-2小时")
    print("   - 总计：3-5小时")
    
    print()
    print("⚠️  注意事项：")
    notes = [
        "首次运行会下载Whisper模型（~150MB）",
        "语音识别需要CPU/GPU资源",
        "响应时间比文本消息慢（+5-10秒）",
        "中文识别准确率约85-90%",
        "嘈杂环境可能影响识别效果",
    ]
    
    for note in notes:
        print(f"   • {note}")
    
    print()
    print("=" * 70)
    print("📞 下一步：")
    print("   1. 确认是否需要语音识别功能")
    print("   2. 决定使用哪个方案（本地/API）")
    print("   3. 分配开发时间")
    print("   4. 开始实施")
    print("=" * 70)

if __name__ == "__main__":
    main()