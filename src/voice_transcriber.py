"""
语音转录服务 - Telegram MTProto AI语音识别扩展
"""

import os
import asyncio
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
import logging

class VoiceTranscriber:
    """语音转录服务基类"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化语音转录服务"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # 临时目录配置
        temp_dir = config.get('temp_dir', './temp/voice')
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 最大文件大小（默认16MB）
        self.max_file_size = config.get('max_file_size', 16777216)
        
        self.logger.info(f"语音转录服务初始化，临时目录: {self.temp_dir}")
    
    async def transcribe_voice_message(self, voice_file_path: str, language: str = "zh") -> Optional[str]:
        """
        转录语音文件为文本
        
        Args:
            voice_file_path: 语音文件路径
            language: 语言代码（zh=中文，auto=自动检测）
            
        Returns:
            转录的文本，如果失败返回None
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(voice_file_path):
                self.logger.error(f"语音文件不存在: {voice_file_path}")
                return None
            
            # 检查文件大小
            file_size = os.path.getsize(voice_file_path)
            if file_size > self.max_file_size:
                self.logger.warning(f"语音文件过大: {file_size} bytes > {self.max_file_size} limit")
                return None
            
            # 调用具体实现
            text = await self._transcribe_impl(voice_file_path, language)
            
            if text:
                self.logger.info(f"语音转录成功: {text[:100]}...")
                return text.strip()
            else:
                self.logger.warning("语音转录返回空结果")
                return None
                
        except Exception as e:
            self.logger.error(f"语音转录失败: {e}")
            return None
    
    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """具体转录实现（由子类重写）"""
        raise NotImplementedError("子类必须实现此方法")
    
    def cleanup_temp_files(self):
        """清理临时文件"""
        try:
            import shutil
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
                self.logger.info(f"清理临时目录: {self.temp_dir}")
        except Exception as e:
            self.logger.warning(f"清理临时文件失败: {e}")

class WhisperLocalTranscriber(VoiceTranscriber):
    """本地Whisper模型转录服务"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化本地Whisper转录服务"""
        super().__init__(config)
        
        # Whisper配置
        whisper_config = config.get('whisper', {})
        self.model_size = whisper_config.get('model_size', 'base')
        self.device = whisper_config.get('device', 'cpu')
        self.download_root = Path(whisper_config.get('download_root', './models/whisper'))
        self.download_root.mkdir(parents=True, exist_ok=True)
        
        self.model = None
        self.logger.info(f"Whisper本地转录服务初始化，模型: {self.model_size}，设备: {self.device}")
    
    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用本地Whisper模型转录"""
        try:
            # 延迟导入，只在需要时加载
            import whisper
            
            # 加载模型（第一次运行会下载）
            if self.model is None:
                self.logger.info(f"加载Whisper模型: {self.model_size}")
                self.model = whisper.load_model(
                    name=self.model_size,
                    device=self.device,
                    download_root=str(self.download_root)
                )
            
            # 转录语音
            self.logger.info(f"开始转录: {voice_file_path}")
            result = self.model.transcribe(
                audio=voice_file_path,
                language=language if language != "auto" else None,
                fp16=False  # CPU模式关闭fp16
            )
            
            text = result.get("text", "").strip()
            return text
            
        except ImportError:
            self.logger.error("Whisper未安装，请运行: pip install openai-whisper")
            return None
        except Exception as e:
            self.logger.error(f"Whisper转录失败: {e}")
            return None

class FasterWhisperTranscriber(VoiceTranscriber):
    """Faster-Whisper转录服务（更快更轻量）"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化Faster-Whisper转录服务"""
        super().__init__(config)
        
        # Faster-Whisper配置
        whisper_config = config.get('whisper', {})
        self.model_size = whisper_config.get('model_size', 'base')
        self.device = whisper_config.get('device', 'cpu')
        self.compute_type = "int8" if self.device == "cpu" else "float16"
        self.download_root = Path(whisper_config.get('download_root', './models/faster-whisper'))
        
        self.model = None
        self.logger.info(f"Faster-Whisper转录服务初始化，模型: {self.model_size}")
    
    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用Faster-Whisper转录"""
        try:
            # 延迟导入
            from faster_whisper import WhisperModel
            
            # 加载模型
            if self.model is None:
                self.logger.info(f"加载Faster-Whisper模型: {self.model_size}")
                self.model = WhisperModel(
                    model_size_or_path=self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=str(self.download_root)
                )
            
            # 转录语音
            self.logger.info(f"开始转录: {voice_file_path}")
            segments, info = self.model.transcribe(
                audio=voice_file_path,
                language=language if language != "auto" else None,
                beam_size=5,
                vad_filter=True  # 语音活动检测
            )
            
            # 合并所有片段
            text = " ".join([segment.text for segment in segments])
            return text.strip()
            
        except ImportError:
            self.logger.error("Faster-Whisper未安装，请运行: pip install faster-whisper")
            return None
        except Exception as e:
            self.logger.error(f"Faster-Whisper转录失败: {e}")
            return None

class OpenAITranscriber(VoiceTranscriber):
    """OpenAI Whisper API转录服务"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化OpenAI API转录服务"""
        super().__init__(config)
        
        # OpenAI配置
        openai_config = config.get('openai', {})
        self.api_key = openai_config.get('api_key') or config.get('api_key')
        self.model = openai_config.get('model') or config.get('model') or 'whisper-1'
        self.base_url = openai_config.get('base_url') or config.get('base_url') or None

        if not self.api_key:
            self.logger.warning("OpenAI API密钥未配置")

        _ep = self.base_url or "https://api.openai.com/v1"
        self.logger.info(f"OpenAI Whisper API转录服务初始化 endpoint={_ep} model={self.model}")
    
    async def _transcribe_impl(self, voice_file_path: str, language: str) -> Optional[str]:
        """使用OpenAI Whisper API转录"""
        try:
            # 延迟导入
            import openai
            
            # 检查API密钥
            if not self.api_key:
                self.logger.error("OpenAI API密钥未配置")
                return None
            
            # 设置客户端（支持 OpenAI 兼容端点：Groq / SiliconFlow / 本地等）
            client_kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            client = openai.OpenAI(**client_kwargs)
            
            # 打开语音文件
            with open(voice_file_path, 'rb') as audio_file:
                self.logger.info(f"调用OpenAI Whisper API: {voice_file_path}")
                
                response = client.audio.transcriptions.create(
                    model=self.model,
                    file=audio_file,
                    language=language if language != "auto" else None,
                    response_format="text"
                )
                
                return response
            
        except ImportError:
            self.logger.error("OpenAI库未安装，请运行: pip install openai")
            return None
        except Exception as e:
            self.logger.error(f"OpenAI转录失败: {e}")
            return None

class VoiceTranscriberFactory:
    """语音转录服务工厂"""
    
    @staticmethod
    def create_transcriber(config: Dict[str, Any]) -> VoiceTranscriber:
        """
        创建语音转录服务实例
        
        Args:
            config: 语音识别配置
            
        Returns:
            语音转录服务实例
        """
        provider = config.get('provider', 'whisper_local')
        
        if provider == 'whisper_local':
            return WhisperLocalTranscriber(config)
        elif provider == 'faster_whisper':
            return FasterWhisperTranscriber(config)
        elif provider == 'openai':
            return OpenAITranscriber(config)
        else:
            # 默认使用本地Whisper
            return WhisperLocalTranscriber(config)

# 简易测试函数
async def test_voice_transcription():
    """测试语音转录服务"""
    print("🔊 语音转录服务测试")
    print("=" * 50)
    
    # 测试配置
    test_config = {
        'enabled': True,
        'provider': 'whisper_local',
        'temp_dir': './temp/test_voice',
        'whisper': {
            'model_size': 'base',
            'device': 'cpu',
            'download_root': './models/test'
        }
    }
    
    try:
        # 创建转录服务
        transcriber = VoiceTranscriberFactory.create_transcriber(test_config)
        
        # 创建测试语音文件（模拟）
        test_file = Path(test_config['temp_dir']) / "test_voice.wav"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 写入模拟数据（实际使用时是真实语音文件）
        test_file.write_bytes(b"fake voice data for testing")
        
        print(f"✅ 转录服务创建成功: {transcriber.__class__.__name__}")
        print(f"✅ 测试文件创建: {test_file}")
        
        # 尝试转录（会失败，因为不是真实语音文件）
        print("\n⚠️  注意: 测试文件不是真实语音，转录会失败")
        print("   实际使用时需要真实语音文件")
        
        # 清理
        transcriber.cleanup_temp_files()
        
        print("\n" + "=" * 50)
        print("🎯 实际使用步骤:")
        print("1. 安装依赖: pip install openai-whisper")
        print("2. 准备真实语音文件")
        print("3. 调用transcribe_voice_message()方法")
        print("4. 处理返回的文本")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")

if __name__ == "__main__":
    # 运行测试
    asyncio.run(test_voice_transcription())