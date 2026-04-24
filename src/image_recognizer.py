"""
图片识别服务 - Telegram MTProto AI图片识别扩展
"""

import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
import logging

class ImageRecognizer:
    """图片识别服务基类"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化图片识别服务"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # 临时目录配置
        temp_dir = config.get('temp_dir', './temp/images')
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 最大文件大小（默认10MB）
        self.max_file_size = config.get('max_file_size', 10485760)
        
        # 支持的图片格式
        self.supported_formats = config.get('supported_formats', ['jpg', 'jpeg', 'png', 'gif', 'bmp'])
        
        self.logger.info(f"图片识别服务初始化，临时目录: {self.temp_dir}")
    
    async def recognize_image(self, image_file_path: str, language: str = "zh") -> Optional[str]:
        """
        识别图片中的文字
        
        Args:
            image_file_path: 图片文件路径
            language: 语言代码（zh=中文，en=英文等）
            
        Returns:
            识别的文本，如果失败返回None
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(image_file_path):
                self.logger.error(f"图片文件不存在: {image_file_path}")
                return None
            
            # 检查文件大小
            file_size = os.path.getsize(image_file_path)
            if file_size > self.max_file_size:
                self.logger.warning(f"图片文件过大: {file_size} bytes > {self.max_file_size} limit")
                return None
            
            # 检查文件格式
            file_ext = Path(image_file_path).suffix.lower().lstrip('.')
            if file_ext not in self.supported_formats:
                self.logger.warning(f"不支持的图片格式: {file_ext}")
                return None
            
            # 调用具体实现
            text = await self._recognize_impl(image_file_path, language)
            
            if text:
                self.logger.info(f"图片识别成功: {text[:100]}...")
                return text.strip()
            else:
                self.logger.warning("图片识别返回空结果")
                return None
                
        except Exception as e:
            self.logger.error(f"图片识别失败: {e}")
            return None
    
    async def _recognize_impl(self, image_file_path: str, language: str) -> Optional[str]:
        """具体识别实现（由子类重写）"""
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

class TesseractOCRRecognizer(ImageRecognizer):
    """Tesseract OCR识别服务"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化Tesseract OCR识别服务"""
        super().__init__(config)
        
        # Tesseract配置
        ocr_config = config.get('ocr', {})
        self.languages = ocr_config.get('languages', ['zh', 'eng'])
        self.psm = ocr_config.get('psm', 3)  # 页面分割模式
        self.oem = ocr_config.get('oem', 3)  # OCR引擎模式
        
        self.logger.info(f"Tesseract OCR识别服务初始化，语言: {self.languages}")
    
    async def _recognize_impl(self, image_file_path: str, language: str) -> Optional[str]:
        """使用Tesseract OCR识别"""
        try:
            # 延迟导入
            import pytesseract
            from PIL import Image
            
            # 打开图片
            self.logger.info(f"使用Tesseract OCR识别: {image_file_path}")
            image = Image.open(image_file_path)
            
            # 配置Tesseract参数
            custom_config = f'--psm {self.psm} --oem {self.oem}'
            
            # 构建语言参数
            lang_param = '+'.join(self.languages)
            
            # 执行OCR
            text = pytesseract.image_to_string(
                image, 
                lang=lang_param,
                config=custom_config
            )
            
            return text
            
        except ImportError as e:
            self.logger.error(f"Tesseract依赖未安装: {e}")
            self.logger.error("请安装: pip install pytesseract pillow")
            self.logger.error("并安装系统Tesseract: https://github.com/tesseract-ocr/tesseract")
            return None
        except Exception as e:
            self.logger.error(f"Tesseract OCR识别失败: {e}")
            return None

class EasyOCRRecognizer(ImageRecognizer):
    """EasyOCR识别服务（更准确但较重）"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化EasyOCR识别服务"""
        super().__init__(config)
        
        # EasyOCR配置
        ocr_config = config.get('ocr', {})
        self.languages = ocr_config.get('languages', ['ch_sim', 'en'])
        self.gpu = ocr_config.get('gpu', False)
        self.model_storage_directory = Path(ocr_config.get('model_storage_directory', './models/easyocr'))
        self.model_storage_directory.mkdir(parents=True, exist_ok=True)
        
        self.reader = None
        self.logger.info(f"EasyOCR识别服务初始化，语言: {self.languages}")
    
    async def _recognize_impl(self, image_file_path: str, language: str) -> Optional[str]:
        """使用EasyOCR识别"""
        try:
            # 延迟导入
            import easyocr
            
            # 初始化Reader（第一次会下载模型）
            if self.reader is None:
                self.logger.info(f"初始化EasyOCR Reader，语言: {self.languages}")
                self.reader = easyocr.Reader(
                    self.languages,
                    gpu=self.gpu,
                    model_storage_directory=str(self.model_storage_directory)
                )
            
            # 执行OCR
            self.logger.info(f"使用EasyOCR识别: {image_file_path}")
            results = self.reader.readtext(image_file_path)
            
            # 提取文本
            texts = [result[1] for result in results]
            text = ' '.join(texts)
            
            return text
            
        except ImportError:
            self.logger.error("EasyOCR未安装，请运行: pip install easyocr")
            return None
        except Exception as e:
            self.logger.error(f"EasyOCR识别失败: {e}")
            return None

class ImageRecognizerFactory:
    """图片识别服务工厂"""
    
    @staticmethod
    def create_recognizer(config: Dict[str, Any]) -> ImageRecognizer:
        """
        创建图片识别服务实例
        
        Args:
            config: 图片识别配置
            
        Returns:
            图片识别服务实例
        """
        provider = config.get('provider', 'tesseract')
        
        if provider == 'tesseract':
            return TesseractOCRRecognizer(config)
        elif provider == 'easyocr':
            return EasyOCRRecognizer(config)
        else:
            # 默认使用Tesseract
            return TesseractOCRRecognizer(config)

# 简易测试函数
async def test_image_recognition():
    """测试图片识别服务"""
    print("📸 图片识别服务测试")
    print("=" * 50)
    
    # 测试配置
    test_config = {
        'enabled': True,
        'provider': 'tesseract',
        'temp_dir': './temp/test_images',
        'max_file_size': 10485760,
        'supported_formats': ['jpg', 'jpeg', 'png'],
        'ocr': {
            'languages': ['zh', 'eng'],
            'psm': 3,
            'oem': 3
        }
    }
    
    try:
        # 创建识别服务
        recognizer = ImageRecognizerFactory.create_recognizer(test_config)
        
        # 创建测试图片文件（模拟）
        test_file = Path(test_config['temp_dir']) / "test_image.jpg"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 写入模拟数据（实际使用时是真实图片文件）
        test_file.write_bytes(b"fake image data for testing")
        
        print(f"✅ 识别服务创建成功: {recognizer.__class__.__name__}")
        print(f"✅ 测试文件创建: {test_file}")
        
        # 尝试识别（会失败，因为不是真实图片）
        print("\n⚠️  注意: 测试文件不是真实图片，识别会失败")
        print("   实际使用时需要真实图片文件")
        
        # 清理
        recognizer.cleanup_temp_files()
        
        print("\n" + "=" * 50)
        print("🎯 实际使用步骤:")
        print("1. 安装依赖:")
        print("   - Tesseract: pip install pytesseract pillow")
        print("   - 系统安装Tesseract OCR")
        print("   - 或 EasyOCR: pip install easyocr")
        print("2. 准备真实图片文件")
        print("3. 调用recognize_image()方法")
        print("4. 处理返回的文本")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")

if __name__ == "__main__":
    # 运行测试
    asyncio.run(test_image_recognition())