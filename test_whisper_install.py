#!/usr/bin/env python3
"""
Whisper安装测试脚本
运行: python test_whisper_install.py
"""

import sys
import subprocess
import importlib.util
import platform

def print_header(text):
    """打印标题"""
    print("\n" + "=" * 60)
    print(f" {text}")
    print("=" * 60)

def check_python_version():
    """检查Python版本"""
    print_header("Python环境检查")
    print(f"Python版本: {sys.version}")
    print(f"平台: {platform.platform()}")
    
    if sys.version_info < (3, 8):
        print("❌ 需要Python 3.8或更高版本")
        return False
    else:
        print("✅ Python版本符合要求")
        return True

def check_module(module_name, pip_name=None):
    """检查模块是否安装"""
    if pip_name is None:
        pip_name = module_name
    
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            print(f"❌ {module_name} 未安装")
            print(f"   请运行: pip install {pip_name}")
            return False
        else:
            print(f"✅ {module_name} 已安装")
            return True
    except Exception as e:
        print(f"❌ 检查{module_name}时出错: {e}")
        return False

def test_whisper_import():
    """测试Whisper导入"""
    print_header("Whisper导入测试")
    
    try:
        import whisper
        print("✅ whisper 模块导入成功")
        
        # 检查版本
        if hasattr(whisper, '__version__'):
            print(f"   Whisper版本: {whisper.__version__}")
        else:
            print("   Whisper版本信息不可用")
        
        # 尝试列出可用模型
        print("\n📦 可用模型大小:")
        model_sizes = ["tiny", "base", "small", "medium", "large"]
        for size in model_sizes:
            print(f"   - {size}")
        
        return True
        
    except ImportError as e:
        print("❌ whisper 导入失败")
        print(f"   错误: {e}")
        print("\n💡 解决方案:")
        print("   1. 安装openai-whisper: pip install openai-whisper")
        print("   2. 或安装faster-whisper: pip install faster-whisper")
        return False
    except Exception as e:
        print(f"❌ whisper 测试失败: {e}")
        return False

def test_whisper_download():
    """测试Whisper模型下载（可选）"""
    print_header("Whisper模型下载测试（可选）")
    
    choice = input("是否测试模型下载？(y/n, 建议第一次安装时测试): ")
    if choice.lower() != 'y':
        print("跳过模型下载测试")
        return True
    
    try:
        import whisper
        print("正在下载base模型（约150MB）...")
        print("首次下载可能需要几分钟，请耐心等待...")
        
        # 尝试下载base模型
        model = whisper.load_model("base")
        print("✅ base模型下载成功")
        
        # 测试转录功能（需要音频文件，跳过实际转录）
        print("📝 转录功能就绪（需要音频文件测试）")
        return True
        
    except Exception as e:
        print(f"❌ 模型下载失败: {e}")
        print("\n💡 可能的原因:")
        print("   1. 网络连接问题")
        print("   2. 磁盘空间不足")
        print("   3. 权限问题")
        print("\n🔄 解决方案:")
        print("   1. 检查网络连接")
        print("   2. 手动下载模型到 ~/.cache/whisper/")
        print("   3. 使用代理或国内镜像")
        return False

def check_telegram_deps():
    """检查Telegram相关依赖"""
    print_header("Telegram依赖检查")
    
    modules = [
        ("pyrogram", "pyrogram"),
        ("tgcrypto", "tgcrypto"),
        ("aiohttp", "aiohttp"),
        ("openai", "openai"),
        ("yaml", "PyYAML"),
        ("colorama", "colorama"),
        ("loguru", "loguru"),
    ]
    
    results = []
    for module_name, pip_name in modules:
        result = check_module(module_name, pip_name)
        results.append(result)
    
    return all(results)

def check_config_files():
    """检查配置文件"""
    print_header("配置文件检查")
    
    import os
    
    required_files = [
        "config/config.yaml",
        "requirements.txt",
        "src/client/telegram_client.py",
        "src/voice_transcriber.py",
    ]
    
    all_exist = True
    for file_path in required_files:
        if os.path.exists(file_path):
            print(f"✅ {file_path} 存在")
        else:
            print(f"❌ {file_path} 不存在")
            all_exist = False
    
    return all_exist

def run_pip_check():
    """运行pip检查"""
    print_header("pip包检查")
    
    try:
        # 获取已安装的包
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            print("✅ pip list 执行成功")
            
            # 检查关键包
            packages_to_check = ["pyrogram", "openai", "whisper", "aiohttp"]
            output = result.stdout.lower()
            
            for package in packages_to_check:
                if package in output:
                    print(f"   ✅ {package} 在已安装列表中")
                else:
                    print(f"   ⚠️  {package} 未在已安装列表中找到")
            
            return True
        else:
            print(f"❌ pip list 失败: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        print("❌ pip检查超时")
        return False
    except Exception as e:
        print(f"❌ pip检查异常: {e}")
        return False

def main():
    """主函数"""
    print("🔊 Whisper语音识别安装测试")
    print("=" * 60)
    
    # 检查Python版本
    if not check_python_version():
        return 1
    
    # 检查配置文件
    check_config_files()
    
    # 检查Telegram依赖
    telegram_ok = check_telegram_deps()
    
    # 检查Whisper
    whisper_import_ok = test_whisper_import()
    
    # 运行pip检查
    pip_ok = run_pip_check()
    
    # 汇总结果
    print_header("安装测试结果汇总")
    
    if telegram_ok:
        print("✅ Telegram依赖: 正常")
    else:
        print("❌ Telegram依赖: 有问题")
    
    if whisper_import_ok:
        print("✅ Whisper导入: 正常")
    else:
        print("❌ Whisper导入: 失败")
    
    if pip_ok:
        print("✅ pip包管理: 正常")
    else:
        print("❌ pip包管理: 有问题")
    
    print("\n🎯 下一步建议:")
    
    if not whisper_import_ok:
        print("1. 安装Whisper: pip install openai-whisper")
        print("2. 或使用国内镜像: pip install -i https://pypi.tuna.tsinghua.edu.cn/simple openai-whisper")
    
    if not telegram_ok:
        print("1. 安装核心依赖: pip install pyrogram tgcrypto openai aiohttp PyYAML colorama loguru")
    
    print("\n🚀 启动系统测试:")
    print("1. 停止现有进程: Ctrl+C")
    print("2. 启动系统: python main.py")
    print("3. 发送测试消息到 @ai_zkw")
    
    print("\n📞 如需帮助，请提供:")
    print("- 此脚本的输出")
    print("- python main.py 的错误信息")
    print("- pip install 的输出")
    
    return 0 if (telegram_ok and whisper_import_ok) else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试脚本异常: {e}")
        sys.exit(1)