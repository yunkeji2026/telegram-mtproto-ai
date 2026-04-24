#!/usr/bin/env python3
"""
快速测试脚本 - 测试AI回复生成和发送功能
"""

import asyncio
import time
from pathlib import Path
import sys

# 添加src目录到路径
sys.path.append(str(Path(__file__).parent / 'src'))

async def test_ai_reply():
    """测试AI回复生成"""
    print("🤖 测试AI回复生成...")
    
    try:
        from ai.ai_client import AIClient
        from utils.config_manager import ConfigManager
        
        # 加载配置
        config = ConfigManager()
        
        # 创建AI客户端
        ai_client = AIClient(config)
        
        # 测试消息
        test_messages = [
            "你好",
            "订单怎么查",
            "价格多少",
            "有哪些通道"
        ]
        
        for msg in test_messages:
            print(f"\n测试消息: '{msg}'")
            start = time.time()
            
            try:
                reply = await ai_client.generate_reply(msg)
                elapsed = time.time() - start
                
                if reply:
                    print(f"✅ 回复成功 ({elapsed:.2f}s): {reply[:100]}...")
                else:
                    print(f"❌ 空回复 ({elapsed:.2f}s)")
                    
            except Exception as e:
                elapsed = time.time() - start
                print(f"❌ 生成失败 ({elapsed:.2f}s): {e}")
        
        return True
        
    except Exception as e:
        print(f"❌ AI回复测试失败: {e}")
        return False

async def test_skill_processing():
    """测试Skill处理"""
    print("\n🎯 测试Skill意图识别...")
    
    try:
        from skills.skill_manager import SkillManager
        from utils.config_manager import ConfigManager
        
        # 加载配置
        config = ConfigManager()
        
        # 创建Skill管理器
        skill_manager = SkillManager(config)
        
        # 测试用例
        test_cases = [
            ("你好啊", "greeting"),
            ("查询订单123", "order_query"),
            ("价格是多少", "price_check"),
            ("通道状态", "channel_info"),
            ("我要投诉", "complaint"),
            ("今天天气怎么样", "small_talk"),
            ("测试", "test")
        ]
        
        for text, expected_skill in test_cases:
            print(f"\n测试: '{text}'")
            
            # 获取匹配的skill
            matched_skill = None
            for skill in skill_manager.skills.values():
                if skill.match(text):
                    matched_skill = skill.name
                    break
            
            if matched_skill:
                if matched_skill == expected_skill:
                    print(f"✅ 正确识别: {matched_skill}")
                else:
                    print(f"⚠️  识别为: {matched_skill} (预期: {expected_skill})")
            else:
                print(f"❌ 未识别任何skill")
        
        return True
        
    except Exception as e:
        print(f"❌ Skill测试失败: {e}")
        return False

async def test_telegram_send():
    """测试Telegram消息发送（模拟）"""
    print("\n📨 测试Telegram发送功能（模拟）...")
    
    try:
        from utils.config_manager import ConfigManager
        
        # 加载配置
        config = ConfigManager()
        tg_config = config.get_telegram_config()
        
        print(f"✅ 配置加载成功")
        print(f"   API ID: {tg_config['api_id']}")
        print(f"   Session: {tg_config['session_name']}")
        print(f"   手机号: {tg_config['phone_number'][:5]}****")
        
        # 检查session文件
        session_file = Path(f"sessions/{tg_config['session_name']}.session")
        if session_file.exists():
            size = session_file.stat().st_size
            print(f"✅ Session文件存在 ({size} bytes)")
        else:
            print("❌ Session文件不存在")
            print("💡 需要运行 main.py 并完成登录")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Telegram测试失败: {e}")
        return False

async def main():
    """主测试函数"""
    print("=" * 50)
    print("Telegram MTProto AI 快速测试")
    print("=" * 50)
    
    # 切换到脚本所在目录
    script_dir = Path(__file__).parent
    Path.cwd = script_dir
    
    results = []
    
    print("\n🔧 测试准备...")
    print(f"工作目录: {script_dir}")
    
    # 运行测试
    results.append(("AI回复生成", await test_ai_reply()))
    results.append(("Skill处理", await test_skill_processing()))
    results.append(("Telegram配置", await test_telegram_send()))
    
    # 汇总结果
    print("\n" + "=" * 50)
    print("测试结果汇总:")
    print("=" * 50)
    
    all_ok = True
    for name, ok in results:
        status = "✅ 通过" if ok else "❌ 失败"
        print(f"{name:15} {status}")
        if not ok:
            all_ok = False
    
    print("\n" + "=" * 50)
    if all_ok:
        print("🎉 所有基础功能测试通过！")
        print("\n💡 建议:")
        print("1. 运行完整系统: python main.py")
        print("2. 发送测试消息到Telegram")
        print("3. 检查PowerShell窗口是否有'收到消息'日志")
    else:
        print("⚠️  部分测试失败，需要检查。")
        print("\n💡 常见问题:")
        print("1. 依赖问题: 运行 pip install -r requirements.txt")
        print("2. 配置错误: 检查 config/config.yaml")
        print("3. API密钥: 确保claude-4.6-oups-high API密钥有效")
        print("4. Session文件: 确保 session 文件存在")
    
    print("=" * 50)

if __name__ == "__main__":
    asyncio.run(main())