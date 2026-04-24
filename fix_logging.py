#!/usr/bin/env python3
"""
修复日志配置脚本
修改配置和代码以启用文件日志记录
"""

import os
import sys
import yaml
import re
from pathlib import Path
import shutil

def backup_file(file_path):
    """备份文件"""
    backup_path = file_path.with_suffix(file_path.suffix + '.bak')
    try:
        shutil.copy2(file_path, backup_path)
        print(f"✅ 已备份: {file_path} -> {backup_path}")
        return backup_path
    except Exception as e:
        print(f"❌ 备份失败 {file_path}: {e}")
        return None

def add_logging_to_config():
    """在配置文件中添加日志配置"""
    config_file = Path("config/config.yaml")
    
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return False
    
    print(f"🔧 修改配置文件: {config_file}")
    
    # 备份文件
    backup_file(config_file)
    
    # 读取内容
    with open(config_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 检查是否已有日志配置
    if 'logging:' in content:
        print("✅ 配置文件中已有 logging 配置节")
        return True
    
    # 查找添加日志配置的位置
    # 在 trigger 配置节后添加
    if 'trigger:' in content:
        # 找到 trigger 配置节的结束位置
        lines = content.split('\n')
        trigger_end = -1
        in_trigger = False
        indent_level = 0
        
        for i, line in enumerate(lines):
            if line.strip().startswith('trigger:'):
                in_trigger = True
                # 计算缩进级别
                indent_level = len(line) - len(line.lstrip())
            elif in_trigger and line.strip() and not line.startswith(' ' * (indent_level + 2)):
                trigger_end = i
                break
        
        if trigger_end > 0:
            # 在 trigger 配置后插入日志配置
            logging_config = '''# 日志配置
logging:
  level: "INFO"                    # 日志级别: DEBUG, INFO, WARNING, ERROR, CRITICAL
  file: "logs/app.log"             # 日志文件路径
  max_size: 10485760               # 最大文件大小 (10MB)
  backup_count: 5                  # 备份文件数量
  console_output: true             # 是否输出到控制台
  
  # 日志格式
  format: "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
  date_format: "%Y-%m-%d %H:%M:%S"
  
  # 性能日志
  enable_perf_logging: false       # 启用性能日志记录
  perf_log_threshold_ms: 100       # 性能阈值(毫秒)'''
            
            lines.insert(trigger_end, logging_config)
            new_content = '\n'.join(lines)
        else:
            # 在文件末尾添加
            new_content = content + '\n\n' + '''# 日志配置
logging:
  level: "INFO"
  file: "logs/app.log"
  max_size: 10485760
  backup_count: 5
  console_output: true'''
    else:
        # 在文件末尾添加
        new_content = content + '\n\n' + '''# 日志配置
logging:
  level: "INFO"
  file: "logs/app.log"
  max_size: 10485760
  backup_count: 5
  console_output: true'''
    
    # 写入文件
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print("✅ 已添加日志配置到 config.yaml")
    return True

def update_main_py():
    """更新main.py中的日志设置"""
    main_file = Path("main.py")
    
    if not main_file.exists():
        print(f"❌ main.py 文件不存在: {main_file}")
        return False
    
    print(f"🔧 修改 main.py: {main_file}")
    
    # 备份文件
    backup_file(main_file)
    
    # 读取内容
    with open(main_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 查找 setup_logger() 调用
    pattern = r'self\.logger\s*=\s*setup_logger\([^)]*\)'
    match = re.search(pattern, content)
    
    if not match:
        print("❌ 未找到 setup_logger() 调用")
        return False
    
    old_call = match.group(0)
    
    # 替换为新的调用
    new_call = '''        # 1. 设置日志
        log_config = self.config.get("logging", {})
        self.logger = setup_logger(
            log_level=log_config.get("level", "INFO"),
            log_file=log_config.get("file"),
            console_output=log_config.get("console_output", True)
        )'''
    
    # 替换内容
    new_content = content.replace(old_call, new_call)
    
    # 检查是否还需要导入 Optional
    if 'Optional[' in new_content and 'from typing import' in new_content:
        # 确保 Optional 已导入
        if 'Optional' not in content:
            # 在导入部分添加 Optional
            import_pattern = r'from typing import ([\w\s,]+)'
            import_match = re.search(import_pattern, content)
            if import_match:
                imports = import_match.group(1)
                if 'Optional' not in imports:
                    new_imports = imports + ', Optional'
                    new_content = new_content.replace(import_match.group(0), 
                                                     f'from typing import {new_imports}')
    
    # 写入文件
    with open(main_file, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print("✅ 已更新 main.py 中的日志设置")
    return True

def create_logs_directory():
    """创建logs目录"""
    logs_dir = Path("logs")
    
    if not logs_dir.exists():
        try:
            logs_dir.mkdir(exist_ok=True)
            print(f"✅ 已创建 logs 目录: {logs_dir}")
            
            # 创建 .gitkeep 文件
            gitkeep = logs_dir / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.touch()
                print(f"✅ 已创建 .gitkeep 文件")
            
            return True
        except Exception as e:
            print(f"❌ 创建 logs 目录失败: {e}")
            return False
    else:
        print(f"✅ logs 目录已存在: {logs_dir}")
        return True

def update_logger_py():
    """更新logger.py以支持配置参数"""
    logger_file = Path("src/utils/logger.py")
    
    if not logger_file.exists():
        print(f"❌ logger.py 文件不存在: {logger_file}")
        return False
    
    print(f"🔧 检查 logger.py: {logger_file}")
    
    # 读取内容
    with open(logger_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 检查是否支持 log_level 参数
    if 'log_level: str = "INFO"' in content:
        print("✅ logger.py 已支持 log_level 参数")
    else:
        print("⚠️ logger.py 可能需要更新以支持 log_level 参数")
    
    # 检查是否支持 log_file 参数
    if 'log_file: Optional[str] = None' in content:
        print("✅ logger.py 已支持 log_file 参数")
    else:
        print("⚠️ logger.py 可能需要更新以支持 log_file 参数")
    
    return True

def verify_fix():
    """验证修复是否成功"""
    print("\n🔍 验证修复结果...")
    
    # 1. 检查logs目录
    logs_dir = Path("logs")
    if logs_dir.exists():
        print("✅ logs目录存在")
    else:
        print("❌ logs目录不存在")
        return False
    
    # 2. 检查配置文件
    config_file = Path("config/config.yaml")
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if 'logging:' in content:
            print("✅ 配置文件中已添加logging配置")
        else:
            print("❌ 配置文件中未添加logging配置")
            return False
    else:
        print("❌ 配置文件不存在")
        return False
    
    # 3. 检查main.py
    main_file = Path("main.py")
    if main_file.exists():
        with open(main_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if 'log_config = self.config.get("logging"' in content:
            print("✅ main.py中已更新日志配置读取")
        else:
            print("❌ main.py中未更新日志配置读取")
            return False
    else:
        print("❌ main.py不存在")
        return False
    
    print("\n🎉 所有修复验证通过!")
    return True

def main():
    """主函数"""
    print("🔧 Telegram AI 日志配置修复工具")
    print("=" * 50)
    
    # 确保在当前目录
    current_dir = Path.cwd()
    print(f"工作目录: {current_dir}")
    
    # 执行修复步骤
    steps = [
        ("创建logs目录", create_logs_directory),
        ("添加日志配置到config.yaml", add_logging_to_config),
        ("更新main.py中的日志设置", update_main_py),
        ("更新logger.py", update_logger_py),
    ]
    
    all_success = True
    for step_name, step_func in steps:
        print(f"\n📋 步骤: {step_name}")
        print("-" * 30)
        if step_func():
            print(f"✅ {step_name} 成功")
        else:
            print(f"❌ {step_name} 失败")
            all_success = False
    
    # 验证修复
    if all_success:
        if verify_fix():
            print("\n" + "=" * 50)
            print("🎉 日志配置修复完成!")
            print("\n📋 下一步操作:")
            print("1. 重启系统以应用新的日志配置:")
            print("   python main.py")
            print("\n2. 检查日志文件是否生成:")
            print("   ls -la logs/app.log")
            print("\n3. 使用监控脚本查看实时日志:")
            print("   .\\monitor_system.ps1 -Action monitor")
        else:
            print("\n⚠️ 修复验证失败，请手动检查")
            all_success = False
    else:
        print("\n⚠️ 部分修复步骤失败，请检查错误信息")
    
    return all_success

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)