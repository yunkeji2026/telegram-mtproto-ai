#!/usr/bin/env python3
"""
测试上下文管理器修复
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from src.context.context_manager import ContextManager
    
    print("✅ 成功导入ContextManager")
    
    # 创建配置
    config = {
        'max_history_messages': 20,
        'analyze_window': 10,
        'emotion': {'enabled': True},
        'topic': {'enabled': True}
    }
    
    # 初始化管理器
    manager = ContextManager(config=config)
    print("✅ 成功初始化ContextManager")
    
    # 测试add_message两种方式
    chat_id = "test_chat_123"
    
    # 方式1: 字典参数
    manager.add_message(chat_id, {
        'user_id': 'user1',
        'username': 'user1',
        'text': '你好，我想查订单',
        'is_bot': False
    })
    print("✅ add_message方式1成功")
    
    # 方式2: 多个参数 (telegram_client.py使用的方式)
    manager.add_message(chat_id, "user2", "user2", "有什么可以帮您？", True)
    print("✅ add_message方式2成功")
    
    # 再添加一条消息
    manager.add_message(chat_id, "user1", "user1", "订单号是123456", False)
    print("✅ add_message方式2再次成功")
    
    # 测试analyze_context
    current_message = "订单号是123456"
    analysis = manager.analyze_context(chat_id, current_message)
    
    if analysis:
        print("✅ analyze_context成功")
        print(f"   情绪: {analysis.get('user_emotion', 'N/A')}")
        print(f"   主题: {analysis.get('conversation_topic', 'N/A')}")
        print(f"   建议回复: {analysis.get('should_reply', 'N/A')}")
        print(f"   优先级: {analysis.get('priority', 'N/A')}")
    else:
        print("❌ analyze_context失败")
    
    # 测试无消息文本的情况
    analysis2 = manager.analyze_context(chat_id, None)
    print(f"✅ 无消息文本分析: {analysis2.get('should_reply', 'N/A')}")
    
    print("\n🎉 所有测试通过！")
    
except ImportError as e:
    print(f"❌ 导入失败: {e}")
    print("请检查Python路径或模块依赖")
except Exception as e:
    print(f"❌ 测试失败: {e}")
    import traceback
    traceback.print_exc()