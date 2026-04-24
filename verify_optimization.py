#!/usr/bin/env python3
"""
优化方案验证脚本
检查V3模型、上下文管理、情绪增强等功能是否正常
"""

import os
import sys
import yaml
import asyncio

def check_imports():
    """检查必要的模块导入"""
    print("🔍 检查模块导入...")
    
    modules = [
        ("src.context.context_manager", "ContextManager"),
        ("src.skills.emotion_enhancer", "EmotionEnhancer"),
        ("src.ai.ai_client", "AIClient"),
        ("src.skills.skill_manager", "SkillManager"),
    ]
    
    all_ok = True
    for module_path, class_name in modules:
        try:
            # 动态导入
            module = __import__(module_path, fromlist=[class_name])
            getattr(module, class_name)
            print(f"  ✅ {module_path}.{class_name}")
        except ImportError as e:
            print(f"  ❌ {module_path}.{class_name}: {e}")
            all_ok = False
        except AttributeError as e:
            print(f"  ❌ {module_path}.{class_name}: 类不存在 - {e}")
            all_ok = False
    
    return all_ok

def check_config():
    """检查配置文件"""
    print("\n🔍 检查配置文件...")
    
    config_path = "config/config.yaml"
    if not os.path.exists(config_path):
        print(f"  ❌ 配置文件不存在: {config_path}")
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 检查AI配置
        ai_config = config.get('ai', {})
        checks = [
            ("模型版本", ai_config.get('model'), "claude-4.6-oups-high-v3"),
            ("超时时间", ai_config.get('timeout'), 60),
            ("温度参数", ai_config.get('temperature'), 0.75),
            ("Token限制", ai_config.get('max_tokens'), 768),
        ]
        
        all_ok = True
        for name, actual, expected in checks:
            if actual == expected:
                print(f"  ✅ {name}: {actual}")
            else:
                print(f"  ⚠️  {name}: 当前={actual}, 预期={expected}")
                all_ok = False
        
        # 检查系统提示词
        system_prompt = ai_config.get('system_prompt', '')
        if "不要承认自己是AI" in system_prompt or "真人客服" in system_prompt:
            print(f"  ✅ 系统提示词包含真人化要求 ({len(system_prompt)}字符)")
        else:
            print(f"  ⚠️  系统提示词可能缺少真人化要求")
        
        # 检查新功能配置
        emoticons_enabled = config.get('emoticons', {}).get('enabled', False)
        context_enabled = config.get('context', {}).get('enabled', False)
        
        print(f"  {'✅' if emoticons_enabled else '❌'} 表情符号功能: {'启用' if emoticons_enabled else '禁用'}")
        print(f"  {'✅' if context_enabled else '❌'} 上下文管理功能: {'启用' if context_enabled else '禁用'}")
        
        return all_ok
        
    except Exception as e:
        print(f"  ❌ 读取配置文件失败: {e}")
        return False

def check_emoticons_config():
    """检查表情符号配置"""
    print("\n🔍 检查表情符号配置...")
    
    emoticons_path = "config/emoticons.yaml"
    if not os.path.exists(emoticons_path):
        print(f"  ⚠️  表情符号配置文件不存在: {emoticons_path}")
        return True  # 不是致命错误
    
    try:
        with open(emoticons_path, 'r', encoding='utf-8') as f:
            emoticons_config = yaml.safe_load(f)
        
        # 检查基本结构
        emoticons = emoticons_config.get('emoticons', {})
        emotion_types = ['positive', 'neutral', 'negative', 'business', 'customer_service']
        
        for emotion_type in emotion_types:
            if emotion_type in emoticons and emoticons[emotion_type]:
                print(f"  ✅ {emotion_type}表情: {len(emoticons[emotion_type])}个")
            else:
                print(f"  ⚠️  {emotion_type}表情: 未配置或为空")
        
        return True
        
    except Exception as e:
        print(f"  ⚠️  读取表情符号配置失败: {e}")
        return True  # 不是致命错误

async def test_context_manager():
    """测试上下文管理器"""
    print("\n🧪 测试上下文管理器...")
    
    try:
        from src.context.context_manager import ContextManager
        
        # 创建配置
        config = {
            'max_history_messages': 20,
            'analyze_window': 10,
            'emotion': {'enabled': True},
            'topic': {'enabled': True}
        }
        
        # 初始化管理器
        manager = ContextManager(config=config)
        
        # 添加测试消息
        chat_id = "test_chat_123"
        manager.add_message(chat_id, "user1", "user1", "你好，我想查订单", False)
        manager.add_message(chat_id, "user2", "user2", "有什么可以帮您？", True)
        manager.add_message(chat_id, "user1", "user1", "订单号是123456", False)
        
        # 分析上下文 - 传递当前消息文本
        current_message_text = "订单号是123456"
        analysis = manager.analyze_context(chat_id, current_message_text)
        
        if analysis:
            print(f"  ✅ 上下文分析成功")
            print(f"    情绪: {analysis.get('user_emotion', 'N/A')}")
            print(f"    主题: {analysis.get('conversation_topic', 'N/A')}")
            print(f"    建议回复: {analysis.get('should_reply', 'N/A')}")
            return True
        else:
            print(f"  ❌ 上下文分析失败")
            return False
            
    except Exception as e:
        print(f"  ❌ 测试上下文管理器失败: {e}")
        return False

async def test_emotion_enhancer():
    """测试情绪增强器"""
    print("\n🧪 测试情绪增强器...")
    
    try:
        from src.skills.emotion_enhancer import EmotionEnhancer
        
        # 创建模拟配置
        class MockConfig:
            def get(self, key, default=None):
                if key == 'emoticons':
                    return {
                        'emoticons': {
                            'positive': ['😊', '👍'],
                            'neutral': ['👉', '📝'],
                            'negative': ['😔', '🙁']
                        },
                        'rules': {
                            'max_emoticons_per_message': 3,
                            'min_message_length_for_emoticon': 10
                        }
                    }
                return default
        
        config = MockConfig()
        enhancer = EmotionEnhancer(config)
        
        # 测试情绪分析
        test_messages = [
            ("谢谢你的帮助！", "positive"),
            ("价格是多少？", "neutral"), 
            ("太生气了！", "negative")
        ]
        
        for text, expected_emotion in test_messages:
            analysis = enhancer.analyze_message_emotion(text)
            emotion = analysis.get('emotion', 'unknown')
            print(f"  📝 '{text}' → 情绪: {emotion} (预期: {expected_emotion})")
        
        # 测试回复增强
        original_reply = "好的，我帮您查询一下"
        enhanced = enhancer.enhance_reply(
            original_reply=original_reply,
            emotion="neutral",
            context_analysis={},
            message_text="价格是多少？"
        )
        
        if enhanced != original_reply:
            print(f"  ✅ 回复增强成功: {enhanced}")
        else:
            print(f"  ⚠️  回复未增强 (可能不需要)")
        
        return True
        
    except Exception as e:
        print(f"  ❌ 测试情绪增强器失败: {e}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("🤖 Telegram AI客服优化方案验证脚本")
    print("=" * 60)
    
    # 切换到项目目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # 执行检查
    results = []
    
    results.append(("模块导入", check_imports()))
    results.append(("配置文件", check_config()))
    results.append(("表情配置", check_emoticons_config()))
    
    # 异步测试
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    results.append(("上下文管理器", loop.run_until_complete(test_context_manager())))
    results.append(("情绪增强器", loop.run_until_complete(test_emotion_enhancer())))
    
    loop.close()
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 验证结果汇总")
    print("=" * 60)
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{status} - {test_name}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 所有检查通过！优化方案已正确实施。")
        print("   请运行 `python main.py` 启动系统并测试实际效果。")
    else:
        print("⚠️  部分检查失败，请根据上述输出修复问题。")
        print("   常见问题:")
        print("   1. 模块导入失败 → 检查Python路径和依赖")
        print("   2. 配置不正确 → 检查config/config.yaml")
        print("   3. 文件不存在 → 检查文件路径")
    
    print("=" * 60)
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())