#!/usr/bin/env python3
"""
安装脚本
用于设置Telegram MTProto AI聊天助手
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path


def print_header(text):
    """打印标题"""
    print("\n" + "=" * 60)
    print(f" {text}")
    print("=" * 60)


def print_step(step_num, text):
    """打印步骤"""
    print(f"\n[{step_num}] {text}")


def print_success(text):
    """打印成功信息"""
    print(f"✅ {text}")


def print_warning(text):
    """打印警告信息"""
    print(f"⚠️  {text}")


def print_error(text):
    """打印错误信息"""
    print(f"❌ {text}")


def check_python_version():
    """检查Python版本"""
    print_step(1, "检查Python版本")
    
    if sys.version_info < (3, 8):
        print_error("需要Python 3.8或更高版本")
        print(f"当前版本: {sys.version}")
        return False
    
    print_success(f"Python版本 {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} 符合要求")
    return True


def install_dependencies():
    """安装依赖"""
    print_step(2, "安装Python依赖")
    
    requirements_file = "requirements.txt"
    if not os.path.exists(requirements_file):
        print_error(f"依赖文件 {requirements_file} 不存在")
        return False
    
    try:
        # 使用pip安装依赖
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", requirements_file])
        print_success("依赖安装完成")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"安装依赖失败: {e}")
        return False


def setup_configuration():
    """设置配置"""
    print_step(3, "设置配置文件")
    
    config_dir = Path("config")
    example_config = config_dir / "config.example.yaml"
    actual_config = config_dir / "config.yaml"
    
    if not config_dir.exists():
        print_error(f"配置目录不存在: {config_dir}")
        return False
    
    if not example_config.exists():
        print_error(f"示例配置文件不存在: {example_config}")
        return False
    
    # 检查是否已有配置文件
    if actual_config.exists():
        print_warning(f"配置文件已存在: {actual_config}")
        choice = input("是否覆盖现有配置? (y/N): ").strip().lower()
        if choice != 'y':
            print_success("保留现有配置文件")
            return True
    
    try:
        # 复制示例配置文件
        shutil.copy2(example_config, actual_config)
        print_success(f"配置文件已创建: {actual_config}")
        
        # 显示配置说明
        print("\n📋 配置文件说明:")
        print("请编辑以下文件并填写您的API凭证:")
        print(f"  {actual_config}")
        print("\n需要填写的配置项:")
        print("  1. Telegram API凭证 (从 https://my.telegram.org 获取)")
        print("     - api_id: 您的Telegram API ID")
        print("     - api_hash: 您的Telegram API Hash")
        print("     - phone_number: 您的手机号（带国际区号）")
        print("  2. claude-4.6-oups-high API密钥 (从claude-4.6-oups-high控制台获取)")
        print("     - api_key: 您的claude-4.6-oups-high API密钥")
        
        return True
        
    except Exception as e:
        print_error(f"创建配置文件失败: {e}")
        return False


def create_directories():
    """创建必要的目录"""
    print_step(4, "创建项目目录")
    
    directories = [
        "logs",
        "sessions",  # Telegram会话文件目录
        "data"       # 数据目录
    ]
    
    try:
        for dir_name in directories:
            dir_path = Path(dir_name)
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                print_success(f"创建目录: {dir_name}")
            else:
                print_success(f"目录已存在: {dir_name}")
        
        return True
    except Exception as e:
        print_error(f"创建目录失败: {e}")
        return False


def setup_logging():
    """设置日志系统"""
    print_step(5, "设置日志系统")
    
    log_dir = Path("logs")
    log_file = log_dir / "app.log"
    
    try:
        if not log_dir.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建日志文件
        if not log_file.exists():
            log_file.touch()
            print_success(f"日志文件已创建: {log_file}")
        else:
            print_success(f"日志文件已存在: {log_file}")
        
        return True
    except Exception as e:
        print_error(f"设置日志系统失败: {e}")
        return False


def verify_installation():
    """验证安装"""
    print_step(6, "验证安装")
    
    # 检查关键文件
    required_files = [
        "main.py",
        "requirements.txt",
        "config/config.yaml",
        "src/client/telegram_client.py",
        "src/ai/ai_client.py",
        "src/skills/skill_manager.py"
    ]
    
    try:
        all_files_exist = True
        for file_path in required_files:
            if os.path.exists(file_path):
                print_success(f"文件存在: {file_path}")
            else:
                print_error(f"文件不存在: {file_path}")
                all_files_exist = False
        
        if not all_files_exist:
            return False
        
        # 尝试导入关键模块
        print("\n🔧 测试模块导入...")
        try:
            import src.utils.logger
            import src.utils.config_manager
            print_success("模块导入测试通过")
        except ImportError as e:
            print_error(f"模块导入失败: {e}")
            return False
        
        return True
    except Exception as e:
        print_error(f"验证安装失败: {e}")
        return False


def show_next_steps():
    """显示下一步操作"""
    print_header("安装完成")
    
    print("🎉 恭喜！Telegram MTProto AI聊天助手已安装完成。")
    print("\n📋 下一步操作:")
    print("1. 编辑配置文件:")
    print("   nano config/config.yaml")
    print("   或")
    print("   notepad config\\config.yaml (Windows)")
    
    print("\n2. 填写必要的API凭证:")
    print("   - Telegram API: 从 https://my.telegram.org 获取")
    print("   - claude-4.6-oups-high API: 从claude-4.6-oups-high控制台获取")
    
    print("\n3. 运行AI聊天助手:")
    print("   python main.py")
    
    print("\n4. 首次运行会提示输入手机验证码")
    print("   请查看您的手机短信或Telegram应用获取验证码")
    
    print("\n📞 如果需要帮助:")
    print("   1. 查看README.md获取详细说明")
    print("   2. 检查logs/app.log查看运行日志")
    print("   3. 确保所有依赖已正确安装")
    
    print("\n🚀 开始使用:")
    print("   cd telegram-mtproto-ai")
    print("   python main.py")


def main():
    """主安装函数"""
    print_header("Telegram MTProto AI聊天助手安装程序")
    
    # 检查是否在项目根目录
    current_dir = Path.cwd()
    if current_dir.name != "telegram-mtproto-ai":
        print_warning(f"当前目录: {current_dir}")
        print_warning("建议在项目根目录 (telegram-mtproto-ai) 中运行此脚本")
        choice = input("是否继续? (y/N): ").strip().lower()
        if choice != 'y':
            print("安装已取消")
            return
    
    steps = [
        ("检查Python版本", check_python_version),
        ("创建项目目录", create_directories),
        ("设置日志系统", setup_logging),
        ("安装依赖", install_dependencies),
        ("设置配置", setup_configuration),
        ("验证安装", verify_installation)
    ]
    
    for step_name, step_func in steps:
        if not step_func():
            print_error(f"安装步骤失败: {step_name}")
            print("请检查错误信息并重试")
            return
    
    show_next_steps()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n安装已取消")
        sys.exit(1)
    except Exception as e:
        print_error(f"安装过程中出现错误: {e}")
        sys.exit(1)