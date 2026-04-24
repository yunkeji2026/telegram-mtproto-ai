#!/usr/bin/env python3
"""
检查日志配置脚本
诊断为什么没有生成 logs/app.log 文件
"""

import os
import sys
import yaml
from pathlib import Path

def check_log_config():
    """检查日志配置"""
    print("🔍 检查日志配置...")
    print("=" * 50)
    
    # 1. 检查当前目录
    current_dir = Path.cwd()
    print(f"当前目录: {current_dir}")
    
    # 2. 检查 logs 目录
    logs_dir = current_dir / "logs"
    if logs_dir.exists():
        print(f"✅ logs 目录存在: {logs_dir}")
        # 列出目录内容
        for item in logs_dir.iterdir():
            print(f"   - {item.name}")
    else:
        print(f"❌ logs 目录不存在: {logs_dir}")
    
    # 3. 检查 app.log 文件
    log_file = logs_dir / "app.log"
    if log_file.exists():
        print(f"✅ 日志文件存在: {log_file}")
        # 显示文件大小
        size = log_file.stat().st_size
        print(f"   文件大小: {size} 字节 ({size/1024:.2f} KB)")
    else:
        print(f"❌ 日志文件不存在: {log_file}")
    
    # 4. 检查配置文件
    config_file = current_dir / "config" / "config.yaml"
    if config_file.exists():
        print(f"✅ 配置文件存在: {config_file}")
        
        # 读取配置文件
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 检查日志配置
        if 'logging' in config:
            print(f"✅ 配置文件中有 logging 配置节")
            logging_config = config['logging']
            for key, value in logging_config.items():
                print(f"   - {key}: {value}")
        else:
            print(f"⚠️ 配置文件中没有 logging 配置节")
            
        # 检查是否有 log_file 配置
        found_log_config = False
        for key in config:
            if 'log' in key.lower():
                print(f"⚠️ 找到可能的日志配置: {key} = {config[key]}")
                found_log_config = True
        
        if not found_log_config:
            print("❌ 配置文件中没有找到日志文件路径配置")
    else:
        print(f"❌ 配置文件不存在: {config_file}")
    
    # 5. 检查 main.py 中的日志设置
    main_file = current_dir / "main.py"
    if main_file.exists():
        print(f"✅ main.py 文件存在: {main_file}")
        
        with open(main_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 查找 setup_logger 调用
        if 'setup_logger' in content:
            print("✅ main.py 中调用了 setup_logger()")
            
            # 尝试提取调用参数
            import re
            pattern = r'setup_logger\(([^)]+)\)'
            matches = re.findall(pattern, content)
            
            if matches:
                print(f"✅ setup_logger 调用参数: {matches[0]}")
            else:
                print("⚠️ setup_logger 调用无参数或使用默认参数")
                
            # 检查是否有 log_file 参数
            if 'log_file' in content:
                print("✅ setup_logger 调用中包含 log_file 参数")
            else:
                print("❌ setup_logger 调用中不包含 log_file 参数，日志不会保存到文件")
        else:
            print("❌ main.py 中没有调用 setup_logger")
    else:
        print(f"❌ main.py 文件不存在: {main_file}")
    
    # 6. 检查 logger.py 模块
    logger_file = current_dir / "src" / "utils" / "logger.py"
    if logger_file.exists():
        print(f"✅ logger.py 文件存在: {logger_file}")
        
        with open(logger_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 检查 setup_logger 函数定义
        if 'def setup_logger' in content:
            print("✅ logger.py 中包含 setup_logger 函数定义")
            
            # 检查默认参数
            if 'log_file: Optional[str] = None' in content:
                print("❌ setup_logger 默认参数: log_file=None (不会保存到文件)")
            else:
                print("✅ setup_logger 有 log_file 参数配置")
    else:
        print(f"❌ logger.py 文件不存在: {logger_file}")
    
    # 7. 检查是否有Python进程在运行
    print("\n🔍 检查Python进程...")
    try:
        import psutil
        python_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' in proc.info['name'].lower():
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if 'main.py' in cmdline:
                        python_processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        if python_processes:
            print(f"✅ 找到 {len(python_processes)} 个运行中的Python进程:")
            for proc in python_processes:
                print(f"   - PID: {proc.pid}, 命令: {' '.join(proc.info['cmdline'] or [])}")
        else:
            print("❌ 没有找到运行中的Python进程 (系统可能未启动)")
    except ImportError:
        print("⚠️ 无法导入psutil模块，跳过进程检查")
    
    print("\n" + "=" * 50)
    print("📋 诊断总结:")
    
    # 生成建议
    issues = []
    solutions = []
    
    if not logs_dir.exists():
        issues.append("logs目录不存在")
        solutions.append("创建logs目录: mkdir logs 或 程序启动时自动创建")
    
    if not log_file.exists():
        issues.append("app.log文件不存在")
        solutions.append("修改代码启用文件日志记录")
    
    # 检查配置
    config_file = current_dir / "config" / "config.yaml"
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config_content = f.read()
        
        if 'log_file' not in config_content and 'logging' not in config_content:
            issues.append("配置文件中没有日志配置")
            solutions.append("在config.yaml中添加logging配置节")
    
    if issues:
        print("❌ 发现问题:")
        for issue in issues:
            print(f"   - {issue}")
        
        print("\n💡 建议解决方案:")
        for solution in solutions:
            print(f"   - {solution}")
        
        # 提供具体修复命令
        print("\n🔧 立即修复:")
        print("1. 创建logs目录:")
        print("   mkdir logs")
        print("\n2. 在config.yaml中添加日志配置:")
        print('   添加以下内容到config.yaml:')
        print('   logging:')
        print('     level: "INFO"')
        print('     file: "logs/app.log"')
        print('     max_size: 10485760  # 10MB')
        print('     backup_count: 5')
        print('\n3. 修改main.py中的setup_logger调用:')
        print('   将 self.logger = setup_logger() 改为:')
        print('   log_config = self.config.get("logging", {})')
        print('   self.logger = setup_logger(')
        print('       log_level=log_config.get("level", "INFO"),')
        print('       log_file=log_config.get("file"),')
        print('       console_output=True')
        print('   )')
    else:
        print("✅ 所有检查通过，日志系统配置正确")
    
    return len(issues) == 0

if __name__ == "__main__":
    success = check_log_config()
    sys.exit(0 if success else 1)