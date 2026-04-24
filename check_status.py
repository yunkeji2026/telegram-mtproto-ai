#!/usr/bin/env python3
"""
简化状态检查工具
纯Python实现，避免批处理语法问题
"""

import os
import sys
import subprocess
import time

def print_header(title):
    """打印标题"""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)

def check_python():
    """检查Python环境"""
    print_header("Python环境检查")
    try:
        result = subprocess.run([sys.executable, "--version"], 
                              capture_output=True, text=True)
        print(f"Python版本: {result.stdout.strip()}")
        return True
    except Exception as e:
        print(f"❌ Python检查失败: {e}")
        return False

def check_directory():
    """检查当前目录"""
    print_header("目录结构检查")
    cwd = os.getcwd()
    print(f"当前目录: {cwd}")
    
    required_items = [
        "config/config.yaml",
        "src/client/telegram_client.py", 
        "main.py",
        "requirements.txt"
    ]
    
    all_ok = True
    for item in required_items:
        if os.path.exists(item):
            print(f"✅ {item}")
        else:
            print(f"❌ {item} (不存在)")
            all_ok = False
    
    return all_ok

def check_config():
    """检查配置文件"""
    print_header("配置文件检查")
    
    config_file = "config/config.yaml"
    if not os.path.exists(config_file):
        print(f"❌ 配置文件不存在: {config_file}")
        return False
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查关键配置
        checks = [
            ("emoticons.enabled: false", "情绪增强器禁用"),
            ("trigger.enabled: true", "触发系统启用"),
            ("model: \"claude-4.6-oups-high\"", "AI模型配置"),
        ]
        
        for pattern, description in checks:
            if pattern in content:
                print(f"✅ {description}: {pattern}")
            else:
                # 尝试查找类似内容
                lines = content.split('\n')
                found = False
                for line in lines:
                    if pattern.split(':')[0] in line:
                        print(f"⚠️  {description}: 找到类似配置 '{line.strip()}'")
                        found = True
                        break
                if not found:
                    print(f"❌ {description}: 未找到配置")
        
        return True
    except Exception as e:
        print(f"❌ 读取配置文件失败: {e}")
        return False

def check_running_processes():
    """检查运行中的进程"""
    print_header("进程状态检查")
    
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
                continue
        
        if python_processes:
            print(f"🔍 找到 {len(python_processes)} 个相关Python进程:")
            for proc in python_processes:
                print(f"  PID {proc.info['pid']}: {proc.info['cmdline']}")
            
            response = input("是否停止这些进程? (y/n): ")
            if response.lower() == 'y':
                for proc in python_processes:
                    try:
                        proc.terminate()
                        print(f"✅ 已停止进程 {proc.info['pid']}")
                    except Exception as e:
                        print(f"❌ 停止进程失败 {proc.info['pid']}: {e}")
                time.sleep(2)
        else:
            print("✅ 未找到运行的Telegram AI进程")
        
        return True
    except ImportError:
        print("⚠️  psutil模块未安装，跳过进程检查")
        print("   安装: pip install psutil")
        return True
    except Exception as e:
        print(f"❌ 进程检查失败: {e}")
        return False

def check_logs():
    """检查日志文件"""
    print_header("日志文件检查")
    
    log_file = "logs/app.log"
    if os.path.exists(log_file):
        print(f"✅ 日志文件存在: {log_file}")
        
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            if lines:
                print(f"📄 日志行数: {len(lines)}")
                print("最后5条相关日志:")
                
                # 查找相关日志
                keywords = ['情绪增强', '触发分析', '群组监控', 'Telegram客户端已启动']
                relevant_lines = []
                for line in lines[-50:]:  # 检查最后50行
                    if any(keyword in line for keyword in keywords):
                        relevant_lines.append(line.strip())
                
                if relevant_lines:
                    for line in relevant_lines[-5:]:  # 显示最后5条相关日志
                        print(f"  {line}")
                else:
                    print("  ℹ️ 未找到相关日志")
            else:
                print("  ℹ️ 日志文件为空")
        except Exception as e:
            print(f"❌ 读取日志失败: {e}")
    else:
        print(f"⚠️ 日志文件不存在: {log_file}")
    
    return True

def check_code_modifications():
    """检查代码修改"""
    print_header("代码修改检查")
    
    files_to_check = [
        ("src/client/telegram_client.py", [
            "emoticons_config.get('enabled', True)",
            "情绪增强器已禁用"
        ]),
        ("src/trigger/four_layer_trigger.py", [
            "\\[触发分析\\]",
            "\\[L1规则\\]"
        ]),
    ]
    
    all_ok = True
    for filepath, patterns in files_to_check:
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                print(f"\n检查: {filepath}")
                found_patterns = []
                for pattern in patterns:
                    if pattern in content:
                        found_patterns.append(pattern)
                
                if found_patterns:
                    print(f"✅ 找到 {len(found_patterns)}/{len(patterns)} 个关键模式")
                    for pattern in found_patterns:
                        print(f"  ✓ {pattern}")
                else:
                    print(f"❌ 未找到关键修改模式")
                    all_ok = False
            except Exception as e:
                print(f"❌ 读取文件失败 {filepath}: {e}")
                all_ok = False
        else:
            print(f"❌ 文件不存在: {filepath}")
            all_ok = False
    
    return all_ok

def start_system():
    """启动系统"""
    print_header("启动系统")
    
    print("正在启动Telegram AI系统...")
    print("注意: 系统将在新终端窗口启动")
    
    try:
        # 根据不同平台使用不同的启动命令
        if sys.platform == "win32":
            subprocess.Popen(["start", "cmd", "/k", "python main.py"], 
                           shell=True)
            print("✅ 已启动新窗口")
        else:
            # Linux/Mac
            subprocess.Popen(["xterm", "-e", "python main.py &"])
            print("✅ 已启动终端")
        
        print("\n📢 请在新窗口中:")
        print("1. 等待显示 'Telegram客户端已启动'")
        print("2. 发送测试消息")
        print("3. 检查日志输出")
        
        return True
    except Exception as e:
        print(f"❌ 启动系统失败: {e}")
        return False

def provide_test_instructions():
    """提供测试指导"""
    print_header("测试指南")
    
    print("📋 测试消息清单:")
    print("1. '测试状态检查001' → 应记录但不回复")
    print("2. '查询订单进度' → 应智能回复，无空格")
    print("3. '@ai_zkw 费率多少' → 应触发回复")
    print("4. '今天心情不错' → 应记录但不回复")
    print()
    print("🎯 验证要点:")
    print("1. 所有消息是否记录? (检查 logs/app.log 中的 [群组监控])")
    print("2. 回复是否有空格问题? (应为'您好'而不是'您 好')")
    print("3. 是否有详细触发日志? ([触发分析], [L1规则]等)")
    print("4. 情绪增强器是否禁用? (无'情绪增强应用成功'日志)")
    print()
    print("⏱️ 时间安排:")
    print("1. 发送测试消息: 1分钟")
    print("2. 等待处理: 1分钟")
    print("3. 检查结果: 1分钟")
    
    return True

def main():
    """主函数"""
    print("=" * 60)
    print("Telegram AI 状态检查工具")
    print("=" * 60)
    
    # 检查环境
    if not check_python():
        return
    
    if not check_directory():
        print("\n❌ 请确保在 telegram-mtproto-ai 目录中运行")
        return
    
    # 执行检查
    check_config()
    check_code_modifications()
    check_running_processes()
    check_logs()
    
    # 提供选项
    print_header("操作选项")
    print("1. 启动系统并测试")
    print("2. 仅显示测试指南")
    print("3. 退出")
    
    choice = input("\n请选择 (1-3): ").strip()
    
    if choice == "1":
        start_system()
        provide_test_instructions()
    elif choice == "2":
        provide_test_instructions()
    else:
        print("退出检查工具")
    
    print_header("检查完成")
    print("📞 如需帮助，请提供:")
    print("1. 此工具的完整输出")
    print("2. logs/app.log 内容")
    print("3. 具体遇到的问题")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n检查被用户中断")
    except Exception as e:
        print(f"\n❌ 检查过程中出错: {e}")
        import traceback
        traceback.print_exc()