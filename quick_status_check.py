#!/usr/bin/env python3
"""
快速状态检查 - 不停止系统检查Telegram MTProto AI状态
"""

import os
import sys
import time
import psutil
from pathlib import Path

def check_python_process():
    """检查Python进程"""
    print("🔍 检查Python进程...")
    
    python_processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if 'main.py' in cmdline or 'telegram-mtproto-ai' in cmdline:
                    python_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    if python_processes:
        print(f"✅ 发现 {len(python_processes)} 个相关Python进程:")
        for proc in python_processes:
            print(f"   PID: {proc.pid}, 命令行: {' '.join(proc.cmdline()[:3])}...")
        return True
    else:
        print("❌ 未发现相关Python进程")
        print("💡 系统可能未运行")
        return False

def check_session_file():
    """检查Session文件是否被访问"""
    print("\n🔍 检查Session文件活动...")
    
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        print("❌ 配置文件不存在")
        return False
    
    try:
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        session_name = config['telegram']['session_name']
        session_file = Path(f"sessions/{session_name}.session")
        
        if session_file.exists():
            # 检查文件最后访问时间
            stat = session_file.stat()
            access_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_atime))
            modify_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
            
            print(f"✅ Session文件: {session_file}")
            print(f"   最后访问: {access_time}")
            print(f"   最后修改: {modify_time}")
            print(f"   文件大小: {stat.st_size} bytes")
            
            # 检查是否最近被访问（5分钟内）
            if time.time() - stat.st_atime < 300:
                print("   📡 Session文件最近被访问（系统可能在工作）")
                return True
            else:
                print("   ⚠️  Session文件最近未被访问")
                return False
        else:
            print(f"❌ Session文件不存在: {session_file}")
            return False
            
    except Exception as e:
        print(f"❌ 检查Session失败: {e}")
        return False

def check_network_connections():
    """检查网络连接"""
    print("\n🔍 检查网络连接...")
    
    try:
        import socket
        
        # 测试claude-4.6-oups-high API连接
        print("测试claude-4.6-oups-high API连接...")
        try:
            sock = socket.create_connection(("api.claude-4.6-oups-high.com", 443), timeout=5)
            sock.close()
            print("✅ claude-4.6-oups-high API可达")
        except Exception as e:
            print(f"❌ claude-4.6-oups-high API连接失败: {e}")
        
        # 测试Telegram连接
        print("测试Telegram连接...")
        try:
            sock = socket.create_connection(("api.telegram.org", 443), timeout=5)
            sock.close()
            print("✅ Telegram API可达")
        except Exception as e:
            print(f"❌ Telegram API连接失败: {e}")
            
    except Exception as e:
        print(f"❌ 网络检查失败: {e}")

def check_recent_logs():
    """检查最近的日志"""
    print("\n🔍 检查日志文件...")
    
    log_dir = Path("logs")
    if log_dir.exists():
        log_files = list(log_dir.glob("*.log"))
        if log_files:
            latest_log = max(log_files, key=lambda f: f.stat().st_mtime)
            print(f"✅ 发现日志文件: {latest_log}")
            
            # 显示最后10行
            try:
                with open(latest_log, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()[-10:]
                    if lines:
                        print("最后10行日志:")
                        for line in lines:
                            print(f"  {line.strip()}")
                    else:
                        print("日志文件为空")
            except Exception as e:
                print(f"❌ 读取日志失败: {e}")
        else:
            print("❌ 日志目录中没有.log文件")
    else:
        print("❌ 日志目录不存在")

def main():
    """主检查函数"""
    print("=" * 50)
    print("Telegram MTProto AI 快速状态检查")
    print("=" * 50)
    print(f"检查时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 切换到脚本所在目录
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    print(f"工作目录: {script_dir}")
    print()
    
    # 运行检查
    process_running = check_python_process()
    session_active = check_session_file()
    check_network_connections()
    check_recent_logs()
    
    print("\n" + "=" * 50)
    print("状态总结:")
    print("=" * 50)
    
    if process_running:
        print("✅ 系统进程正在运行")
    else:
        print("❌ 系统进程未运行")
    
    if session_active:
        print("✅ Session文件活跃")
    else:
        print("❌ Session文件不活跃")
    
    print("\n💡 建议:")
    if not process_running:
        print("1. 启动系统: python main.py")
    elif not session_active:
        print("1. 系统可能已卡住，按Ctrl+C重启")
        print("2. 检查Session文件是否有效")
    else:
        print("1. 系统似乎在运行，发送测试消息验证")
        print("2. 检查PowerShell窗口是否有新日志")
    
    print("\n🔧 快速测试:")
    print("发送'测试'到 @ai_zkw，观察回复")
    print("=" * 50)

if __name__ == "__main__":
    try:
        import psutil
    except ImportError:
        print("❌ 需要psutil库: pip install psutil")
        sys.exit(1)
    
    main()