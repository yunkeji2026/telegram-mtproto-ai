#!/usr/bin/env python3
"""
语法检查脚本
运行: python check_syntax.py
"""

import sys
import os
from pathlib import Path

def check_file_syntax(file_path):
    """检查单个文件的语法"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()
        
        # 尝试编译检查语法
        compile(source, file_path, 'exec')
        print(f"✅ {file_path} - 语法正确")
        return True
        
    except SyntaxError as e:
        print(f"❌ {file_path} - 语法错误")
        print(f"   行 {e.lineno}: {e.msg}")
        if e.text:
            print(f"   代码: {e.text.strip()}")
        return False
    except Exception as e:
        print(f"⚠️  {file_path} - 检查时出错: {e}")
        return False

def check_imports():
    """检查关键导入"""
    print("\n🔍 检查关键模块导入...")
    
    modules_to_check = [
        ("pyrogram", "Telegram客户端库"),
        ("openai", "AI API客户端"),
        ("aiohttp", "异步HTTP客户端"),
        ("yaml", "配置文件解析"),
    ]
    
    all_ok = True
    for module_name, description in modules_to_check:
        try:
            __import__(module_name)
            print(f"✅ {module_name} - {description} 可导入")
        except ImportError as e:
            print(f"❌ {module_name} - {description} 导入失败: {e}")
            all_ok = False
    
    # 检查Whisper
    try:
        import whisper
        print("✅ whisper - 语音识别库 可导入")
    except ImportError:
        print("⚠️  whisper - 语音识别库 未安装 (语音功能将受限)")
        print("   请运行: pip install openai-whisper")
    
    return all_ok

def main():
    """主函数"""
    print("🔧 Telegram MTProto AI 语法检查")
    print("=" * 60)
    
    # 切换到项目目录
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    # 检查关键文件
    files_to_check = [
        "src/client/telegram_client.py",
        "src/voice_transcriber.py",
        "src/config/config_manager.py",
        "main.py",
    ]
    
    print("📁 检查文件语法...")
    all_syntax_ok = True
    
    for file_path in files_to_check:
        if Path(file_path).exists():
            if not check_file_syntax(file_path):
                all_syntax_ok = False
        else:
            print(f"⚠️  {file_path} - 文件不存在")
    
    # 检查导入
    imports_ok = check_imports()
    
    # 检查配置文件
    print("\n📋 检查配置文件...")
    if Path("config/config.yaml").exists():
        print("✅ config/config.yaml - 配置文件存在")
    else:
        print("❌ config/config.yaml - 配置文件不存在")
        all_syntax_ok = False
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 检查结果汇总")
    print("=" * 60)
    
    if all_syntax_ok:
        print("✅ 所有文件语法检查通过")
    else:
        print("❌ 部分文件有语法问题")
    
    if imports_ok:
        print("✅ 关键模块导入检查通过")
    else:
        print("❌ 部分关键模块导入失败")
    
    print("\n🎯 下一步建议:")
    if all_syntax_ok and imports_ok:
        print("1. 启动系统测试: python main.py")
        print("2. 发送测试消息到 @ai_zkw")
        print("3. 测试语音识别功能")
    else:
        print("1. 修复语法错误")
        print("2. 安装缺失的依赖")
        print("3. 重新运行此检查")
    
    return 0 if (all_syntax_ok and imports_ok) else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n检查被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 检查脚本异常: {e}")
        sys.exit(1)