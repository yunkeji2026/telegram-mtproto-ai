#!/usr/bin/env python3
"""
测试项目结构完整性
"""

import os
import sys
from pathlib import Path


def check_file_exists(file_path, required=True):
    """检查文件是否存在"""
    exists = os.path.exists(file_path)
    status = "✅" if exists else "❌"
    print(f"{status} {file_path}")
    
    if required and not exists:
        print(f"   错误: 必需文件不存在: {file_path}")
        return False
    return True


def check_directory_exists(dir_path, required=True):
    """检查目录是否存在"""
    exists = os.path.exists(dir_path) and os.path.isdir(dir_path)
    status = "✅" if exists else "❌"
    print(f"{status} {dir_path}/")
    
    if required and not exists:
        print(f"   错误: 必需目录不存在: {dir_path}")
        return False
    return True


def main():
    """主测试函数"""
    print("🔍 检查Telegram MTProto AI项目结构完整性")
    print("=" * 60)
    
    all_passed = True
    
    # 检查根目录文件
    print("\n📄 根目录文件:")
    root_files = [
        ("main.py", True),
        ("setup.py", True),
        ("requirements.txt", True),
        ("quick_start.md", True),
        (".gitignore", True),
        ("test_structure.py", False)
    ]
    
    for file_name, required in root_files:
        all_passed &= check_file_exists(file_name, required)
    
    # 检查目录
    print("\n📁 项目目录:")
    directories = [
        ("config", True),
        ("src", True),
        ("logs", True),
        ("sessions", True),
        ("data", False)
    ]
    
    for dir_name, required in directories:
        all_passed &= check_directory_exists(dir_name, required)
    
    # 检查config目录
    print("\n⚙️ 配置文件:")
    config_files = [
        ("config/config.example.yaml", True),
        ("config/config.yaml", False)  # 实际配置文件可能不存在
    ]
    
    for file_name, required in config_files:
        all_passed &= check_file_exists(file_name, required)
    
    # 检查src目录结构
    print("\n💻 源代码目录:")
    src_dirs = [
        ("src/client", True),
        ("src/ai", True),
        ("src/skills", True),
        ("src/utils", True)
    ]
    
    for dir_name, required in src_dirs:
        all_passed &= check_directory_exists(dir_name, required)
    
    # 检查核心源码文件
    print("\n🔧 核心源文件:")
    src_files = [
        ("src/client/telegram_client.py", True),
        ("src/ai/ai_client.py", True),
        ("src/skills/skill_manager.py", True),
        ("src/utils/config_manager.py", True),
        ("src/utils/logger.py", True)
    ]
    
    for file_name, required in src_files:
        all_passed &= check_file_exists(file_name, required)
    
    # 检查Python模块导入
    print("\n🐍 Python模块导入测试:")
    try:
        # 将项目根目录添加到Python路径
        sys.path.insert(0, str(Path(__file__).parent))
        
        import src.utils.logger
        print("✅ src.utils.logger 导入成功")
        
        import src.utils.config_manager
        print("✅ src.utils.config_manager 导入成功")
        
        # 尝试导入其他模块（不执行，只检查语法）
        import src.client.telegram_client
        print("✅ src.client.telegram_client 导入成功")
        
        import src.ai.ai_client
        print("✅ src.ai.ai_client 导入成功")
        
        import src.skills.skill_manager
        print("✅ src.skills.skill_manager 导入成功")
        
        print("✅ 所有模块导入测试通过")
        
    except ImportError as e:
        print(f"❌ 模块导入失败: {e}")
        all_passed = False
    except Exception as e:
        print(f"❌ 导入测试异常: {e}")
        all_passed = False
    
    # 检查依赖文件
    print("\n📦 依赖检查:")
    if os.path.exists("requirements.txt"):
        with open("requirements.txt", "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            print(f"✅ requirements.txt 包含 {len(lines)} 个依赖项")
    else:
        print("❌ requirements.txt 不存在")
        all_passed = False
    
    # 最终结果
    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 项目结构完整性测试通过！")
        print("✅ 所有必需文件和目录都存在")
        print("✅ 模块导入测试成功")
        print("✅ 项目结构完整，可以正常运行")
    else:
        print("⚠️  项目结构测试失败")
        print("❌ 部分文件或目录缺失")
        print("💡 请检查缺失的文件并重新运行测试")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())