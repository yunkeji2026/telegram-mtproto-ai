#!/usr/bin/env python3
"""
测试日志配置修复
在系统重启前快速验证日志配置是否正确
"""

import os
import sys
import yaml
import logging
from pathlib import Path

def check_config_file():
    """检查配置文件中的日志设置"""
    config_path = Path("config/config.yaml")
    
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        logging_config = config.get("logging", {})
        if not logging_config:
            print("❌ 配置文件中没有 logging 配置节")
            return False
        
        log_file = logging_config.get("file")
        if not log_file:
            print("❌ logging.file 配置为空")
            return False
        
        print(f"✅ 配置文件检查通过:")
        print(f"   - 日志文件路径: {log_file}")
        print(f"   - 日志级别: {logging_config.get('level', 'INFO')}")
        print(f"   - 控制台输出: {logging_config.get('console_output', True)}")
        
        return True
        
    except Exception as e:
        print(f"❌ 配置文件读取失败: {e}")
        return False

def check_logs_directory():
    """检查logs目录"""
    logs_dir = Path("logs")
    
    if not logs_dir.exists():
        print(f"❌ logs目录不存在: {logs_dir}")
        print("⚠️  代码中的os.makedirs()会在运行时自动创建")
        return True  # 不是致命错误，代码会创建
    
    print(f"✅ logs目录存在: {logs_dir}")
    
    # 检查写入权限
    test_file = logs_dir / ".test_write"
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        print(f"✅ logs目录可写入")
    except Exception as e:
        print(f"❌ logs目录不可写入: {e}")
        return False
    
    return True

def check_main_py_modification():
    """检查main.py修改"""
    main_py = Path("main.py")
    
    if not main_py.exists():
        print(f"❌ main.py不存在: {main_py}")
        return False
    
    try:
        with open(main_py, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查关键修改
        checks = [
            ("os.makedirs", "os.makedirs调用存在"),
            ("log_config = self.config.config.get", "配置读取逻辑存在"),
            ("FileHandler(log_file", "文件处理器创建逻辑存在"),
        ]
        
        all_passed = True
        for keyword, description in checks:
            if keyword in content:
                print(f"✅ {description}")
            else:
                print(f"❌ {description} 缺失")
                all_passed = False
        
        return all_passed
        
    except Exception as e:
        print(f"❌ main.py读取失败: {e}")
        return False

def test_logging_directly():
    """直接测试日志功能"""
    print("\n🧪 直接测试日志功能...")
    
    # 读取配置
    config_path = Path("config/config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    logging_config = config.get("logging", {})
    log_file = logging_config.get("file", "logs/app.log")
    log_level = logging_config.get("level", "INFO")
    
    # 创建日志记录器
    logger = logging.getLogger("test_logger")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # 清除现有处理器
    logger.handlers.clear()
    
    # 添加文件处理器
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # 测试日志写入
    test_message = "🔥 日志配置测试消息 - 如果看到此消息，说明日志配置正确"
    logger.info(test_message)
    
    # 检查文件是否创建
    if os.path.exists(log_file):
        print(f"✅ 日志文件创建成功: {log_file}")
        
        # 读取最后一行验证
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    if test_message in last_line:
                        print(f"✅ 测试消息成功写入日志文件")
                    else:
                        print(f"⚠️  日志文件存在但测试消息未找到")
                        print(f"   最后一行: {last_line[:100]}...")
        except Exception as e:
            print(f"⚠️  无法读取日志文件: {e}")
    else:
        print(f"❌ 日志文件未创建: {log_file}")
        return False
    
    return True

def main():
    """主测试函数"""
    print("🔧 Telegram AI 日志配置修复测试")
    print("=" * 50)
    
    # 切换到脚本所在目录
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    print(f"工作目录: {os.getcwd()}")
    
    # 执行测试
    tests = [
        ("配置文件检查", check_config_file),
        ("logs目录检查", check_logs_directory),
        ("main.py修改检查", check_main_py_modification),
    ]
    
    all_passed = True
    for test_name, test_func in tests:
        print(f"\n📋 测试: {test_name}")
        print("-" * 30)
        if not test_func():
            all_passed = False
    
    # 直接测试日志功能（可选）
    if all_passed:
        print("\n" + "=" * 50)
        response = input("是否直接测试日志写入功能？(y/n): ").strip().lower()
        if response == 'y':
            if test_logging_directly():
                print("\n🎉 所有测试通过！日志配置修复成功。")
            else:
                print("\n⚠️  直接测试失败，请检查错误信息。")
                all_passed = False
        else:
            print("\n⏭️  跳过直接测试")
    else:
        print("\n❌ 基础测试失败，跳过直接测试")
    
    # 总结
    print("\n" + "=" * 50)
    if all_passed:
        print("✅ 所有检查通过！")
        print("\n📋 下一步操作:")
        print("1. 重启系统: python main.py")
        print("2. 检查日志文件: Get-ChildItem logs\\app.log")
        print("3. 启动监控: .\\monitor_system.ps1 -Action monitor")
    else:
        print("❌ 部分检查失败，需要修复")
        print("\n🔧 修复建议:")
        print("1. 确保 config/config.yaml 中有 logging 配置节")
        print("2. 确保 main.py 已正确修改")
        print("3. 手动创建 logs 目录: mkdir logs")
        print("4. 运行修复脚本: python fix_logging.py")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())