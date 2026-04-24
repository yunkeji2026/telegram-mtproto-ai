#!/usr/bin/env python3
"""
群组回复控制诊断脚本
用于诊断@提及消息不回复的问题
"""

import os
import sys
import yaml
import asyncio
from pathlib import Path

def check_system_status():
    """检查系统状态"""
    print("=" * 60)
    print("📊 系统状态检查")
    print("=" * 60)
    
    # 1. 检查日志文件
    log_path = Path("logs/app.log")
    if log_path.exists():
        log_size = log_path.stat().st_size
        print(f"✅ 日志文件存在: {log_path} ({log_size} 字节)")
        
        # 读取最后几行日志
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()[-20:]  # 最后20行
            print(f"📝 最后日志片段:")
            for line in lines[-5:]:  # 显示最后5行
                print(f"  {line.strip()}")
        except Exception as e:
            print(f"⚠️ 读取日志失败: {e}")
    else:
        print(f"❌ 日志文件不存在: {log_path}")
        print("   可能原因: 系统未运行或配置错误")
    
    # 2. 检查session文件
    session_path = Path("sessions/639277356155.session")
    if session_path.exists():
        session_size = session_path.stat().st_size
        print(f"✅ Session文件存在: {session_path} ({session_size} 字节)")
    else:
        print(f"❌ Session文件不存在: {session_path}")
        print("   可能原因: 首次运行或session被删除")
    
    # 3. 检查配置文件
    config_path = Path("config/config.yaml")
    if config_path.exists():
        print(f"✅ 配置文件存在: {config_path}")
    else:
        print(f"❌ 配置文件不存在: {config_path}")
        return False
    
    return True

def check_configuration():
    """检查配置是否正确"""
    print("\n" + "=" * 60)
    print("⚙️ 配置检查")
    print("=" * 60)
    
    try:
        with open("config/config.yaml", 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 检查Telegram配置
        telegram_config = config.get('telegram', {})
        print(f"📱 Telegram配置:")
        print(f"  api_id: {telegram_config.get('api_id', '未设置')}")
        print(f"  api_hash: {'已设置' if telegram_config.get('api_hash') else '未设置'}")
        print(f"  phone_number: {telegram_config.get('phone_number', '未设置')}")
        print(f"  process_groups: {telegram_config.get('process_groups', False)}")
        
        # 检查群组回复控制配置
        group_reply = telegram_config.get('group_reply', {})
        if group_reply:
            print(f"\n🎯 群组回复控制配置:")
            print(f"  mode: {group_reply.get('mode', '未设置')}")
            print(f"  keywords: {group_reply.get('keywords', [])}")
            print(f"  mention_usernames: {group_reply.get('mention_usernames', [])}")
            print(f"  case_sensitive: {group_reply.get('case_sensitive', False)}")
            print(f"  require_exact_match: {group_reply.get('require_exact_match', False)}")
            
            # 验证配置
            mode = group_reply.get('mode', 'always')
            if mode not in ['always', 'mention_only', 'keyword_only', 'mention_or_keyword']:
                print(f"❌ 无效的mode值: {mode}")
                return False
            
            keywords = group_reply.get('keywords', [])
            if not keywords and mode in ['keyword_only', 'mention_or_keyword']:
                print("⚠️ 警告: 关键词模式已启用但关键词列表为空")
            
            mentions = group_reply.get('mention_usernames', [])
            if not mentions and mode in ['mention_only', 'mention_or_keyword']:
                print("⚠️ 警告: @提及模式已启用但@用户名列表为空")
            
            print(f"✅ 群组回复控制配置有效")
        else:
            print(f"❌ 群组回复控制配置未找到")
            print("   可能原因: 配置未更新或格式错误")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ 读取配置失败: {e}")
        return False

def check_code_integration():
    """检查代码集成"""
    print("\n" + "=" * 60)
    print("💻 代码集成检查")
    print("=" * 60)
    
    client_path = Path("src/client/telegram_client.py")
    if not client_path.exists():
        print(f"❌ 客户端文件不存在: {client_path}")
        return False
    
    print(f"✅ 客户端文件存在: {client_path}")
    
    # 检查关键函数是否存在
    try:
        with open(client_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        required_functions = [
            '_should_reply_to_group_message',
            '_contains_mention', 
            '_contains_keyword'
        ]
        
        for func in required_functions:
            if func in content:
                print(f"✅ 函数 {func}() 存在")
            else:
                print(f"❌ 函数 {func}() 不存在")
                return False
        
        # 检查群组消息处理器
        if 'handle_group_message' in content and 'if not self._should_reply_to_group_message(message):' in content:
            print("✅ 群组消息处理器已集成过滤逻辑")
        else:
            print("❌ 群组消息处理器未集成过滤逻辑")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ 读取代码失败: {e}")
        return False

def check_dependencies():
    """检查依赖"""
    print("\n" + "=" * 60)
    print("📦 依赖检查")
    print("=" * 60)
    
    try:
        import pyrogram
        print(f"✅ Pyrogram 已安装: {pyrogram.__version__}")
    except ImportError:
        print("❌ Pyrogram 未安装")
        return False
    
    try:
        import openai
        print(f"✅ OpenAI 已安装: {openai.__version__}")
    except ImportError:
        print("❌ OpenAI 未安装")
        return False
    
    try:
        import yaml
        print(f"✅ PyYAML 已安装")
    except ImportError:
        print("❌ PyYAML 未安装")
        return False
    
    try:
        import loguru
        print(f"✅ Loguru 已安装")
    except ImportError:
        print("❌ Loguru 未安装")
        return False
    
    return True

def provide_solutions():
    """提供解决方案"""
    print("\n" + "=" * 60)
    print("🔧 解决方案建议")
    print("=" * 60)
    
    print("""
根据诊断结果，建议按以下步骤解决问题:

1. 🔄 **重启系统** (如果未运行)
   ```
   python main.py
   ```

2. 📝 **检查启动输出**
   - 确认看到"Telegram客户端已启动，等待消息..."
   - 确认看到"消息处理器已设置 - 处理所有私聊和群组消息 (群组回复控制已启用)"

3. 🧪 **测试功能**
   - 私聊发送"测试" → 应回复
   - 群组发送"客服在吗" → 应回复  
   - 群组发送"@ai_zkw 你好" → 应回复
   - 群组发送"大家好" → 不应回复

4. 📋 **检查日志**
   ```
   tail -f logs/app.log
   ```
   - 查看是否有"忽略未触发条件的群组消息"日志
   - 查看是否有消息处理日志

5. ⚙️ **验证配置**
   - 确认 config/config.yaml 中的 group_reply 配置正确
   - 确认 mode 为 "mention_or_keyword"
   - 确认 mention_usernames 包含 "@ai_zkw"

6. 🔑 **权限检查**
   - 确认账号已加入目标群组
   - 确认账号在群组中有发送消息权限
   - 尝试退出重新加入群组

7. 🔄 **重建session** (如果问题持续)
   ```
   rm -rf sessions/639277356155.session
   python main.py
   ```
   - 重新登录获取验证码

8. 📞 **如果问题仍然存在**
   - 提供具体的错误信息
   - 提供 logs/app.log 完整内容
   - 提供测试消息的截图
""")

def main():
    """主函数"""
    print("🚀 Telegram MTProto AI 群组回复控制诊断工具")
    print("=" * 60)
    
    # 切换到项目目录
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    print(f"📁 工作目录: {os.getcwd()}")
    
    # 执行检查
    checks = [
        ("系统状态", check_system_status),
        ("配置检查", check_configuration),
        ("代码集成", check_code_integration),
        ("依赖检查", check_dependencies),
    ]
    
    results = []
    for name, check_func in checks:
        print(f"\n▶️ 执行: {name}")
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print(f"❌ 检查失败: {e}")
            results.append((name, False))
    
    # 总结
    print("\n" + "=" * 60)
    print("📈 诊断总结")
    print("=" * 60)
    
    all_passed = True
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{name}: {status}")
        if not result:
            all_passed = False
    
    if all_passed:
        print("\n🎉 所有检查通过! 系统配置正常。")
        print("问题可能在于:")
        print("1. 系统未运行 - 请重启系统")
        print("2. 网络问题 - 检查网络连接")
        print("3. 权限问题 - 确认账号在群组中")
    else:
        print("\n⚠️ 发现配置问题，请根据上面的错误信息修复。")
    
    # 提供解决方案
    provide_solutions()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 诊断中断")
    except Exception as e:
        print(f"\n❌ 诊断过程出错: {e}")
        import traceback
        traceback.print_exc()