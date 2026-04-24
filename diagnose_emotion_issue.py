#!/usr/bin/env python3
"""
诊断情绪增强器空格问题
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 模拟配置
config = {
    'emoticons': {
        'emoticons': {
            'positive': ['😊', '👍', '🙏', '🎉'],
            'neutral': ['👉', '📝', '🔍', '⏰'],
            'negative': ['😔', '🙁', '😥', '⚠️'],
            'business': ['💰', '📦', '🔄', '✅'],
            'customer_service': ['👋', '🙋', '💬', '📞']
        },
        'rules': {
            'max_emoticons_per_message': 3,
            'min_message_length_for_emoticon': 10,
            'avoid_emoticons_in': ['serious_complaint', 'legal_issue'],
            'keyword_triggers': [
                {
                    'keywords': ['订单', '查单', '单号'],
                    'emoticons': ['📦', '🔍']
                }
            ]
        }
    }
}

# 导入情绪增强器
try:
    from src.skills.emotion_enhancer import EmotionEnhancer
    print("✅ 成功导入EmotionEnhancer")
except Exception as e:
    print(f"❌ 导入失败: {e}")
    sys.exit(1)

# 创建实例
enhancer = EmotionEnhancer(config)

# 测试用例
test_cases = [
    {
        'name': 'AI生成的订单查询回复',
        'original_reply': '好的，我帮您查一下订单信息！😊 不过我需要您提供一些信息才能准确查询呢～您方便告诉我订单号吗？',
        'emotion': 'neutral',
        'context_analysis': {
            'conversation_topic': 'order',
            'suggested_emoticons': ['📦']
        },
        'message_text': '查订单'
    },
    {
        'name': '模板订单回复',
        'original_reply': '查询订单请提供订单号。',
        'emotion': 'neutral',
        'context_analysis': {},
        'message_text': '订单'
    },
    {
        'name': '简短回复',
        'original_reply': '好的',
        'emotion': 'neutral',
        'context_analysis': {},
        'message_text': '好的'
    }
]

print("\n🧪 测试情绪增强器处理:")
print("=" * 60)

for test_case in test_cases:
    print(f"\n📋 测试: {test_case['name']}")
    print(f"原始回复: '{test_case['original_reply']}'")
    print(f"长度: {len(test_case['original_reply'])} 字符")
    
    try:
        # 增强回复
        enhanced = enhancer.enhance_reply(
            original_reply=test_case['original_reply'],
            emotion=test_case['emotion'],
            context_analysis=test_case['context_analysis'],
            message_text=test_case['message_text']
        )
        
        print(f"增强回复: '{enhanced}'")
        print(f"长度: {len(enhanced)} 字符")
        print(f"是否修改: {'是' if enhanced != test_case['original_reply'] else '否'}")
        
        # 检查空格问题
        if '  ' in enhanced:
            print("⚠️  警告: 包含双空格")
        if enhanced.count(' ') > len(enhanced) * 0.3:  # 超过30%字符是空格
            print("⚠️  警告: 空格过多")
        
        # 检查每个字符
        if len(enhanced) > 0:
            print("字符分析 (前20个字符):")
            for i, char in enumerate(enhanced[:20]):
                print(f"  [{i}] '{char}' (U+{ord(char):04X}) - {'空格' if char == ' ' else '文字'}")
                
    except Exception as e:
        print(f"❌ 处理失败: {e}")

# 测试 _cleanup_format 方法
print("\n🧪 测试 _cleanup_format 方法:")
print("=" * 60)

test_strings = [
    "好的，我帮您查一下订单信息！😊",
    "好 的， 我 帮 您 查 一 下 订 单 信 息！😊",
    "查询订单请提供订单号。",
    "查 询 订 单 请 提 供 订 单 号 。",
    "测试😊表情符号",
    "测试 😊 表情 符号"
]

for s in test_strings:
    cleaned = enhancer._cleanup_format(s)
    print(f"'{s}'")
    print(f"  -> '{cleaned}'")
    print(f"  空格数: {s.count(' ')} -> {cleaned.count(' ')}")
    print()

# 测试情绪分析
print("\n🧪 测试情绪分析:")
print("=" * 60)

messages = ["查订单", "谢谢", "投诉", "今天天气不错"]
for msg in messages:
    emotion_result = enhancer.analyze_message_emotion(msg)
    print(f"'{msg}' -> 情绪: {emotion_result['emotion']}, 置信度: {emotion_result['confidence']}")

print("\n✅ 诊断完成")