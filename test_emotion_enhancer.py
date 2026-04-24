#!/usr/bin/env python3
"""
测试情绪增强器，检查为什么回复会有空格
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.skills.emotion_enhancer import EmotionEnhancer

# 创建模拟配置
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
            'avoid_emoticons_in': ['serious_complaint', 'legal_issue']
        }
    }
}

# 初始化情绪增强器
enhancer = EmotionEnhancer(config)

# 测试用例：模板回复
test_cases = [
    {
        'original_reply': '查询订单请提供订单号。',
        'emotion': 'neutral',
        'context_analysis': {},
        'message_text': '订单'
    },
    {
        'original_reply': '请告诉我订单号。',
        'emotion': 'neutral',
        'context_analysis': {},
        'message_text': '订单'
    },
    {
        'original_reply': '订单查询需要订单号。',
        'emotion': 'neutral',
        'context_analysis': {},
        'message_text': '订单'
    }
]

print("测试情绪增强器处理模板回复:")
print("=" * 60)

for i, test_case in enumerate(test_cases, 1):
    original = test_case['original_reply']
    enhanced = enhancer.enhance_reply(
        original_reply=original,
        emotion=test_case['emotion'],
        context_analysis=test_case['context_analysis'],
        message_text=test_case['message_text']
    )
    
    print(f"\n测试用例 {i}:")
    print(f"原始回复: '{original}'")
    print(f"增强回复: '{enhanced}'")
    print(f"长度变化: {len(original)} -> {len(enhanced)} 字符")
    print(f"是否有空格分隔: {'是' if ' ' in enhanced else '否'}")
    
    # 检查每个字符
    if ' ' in enhanced:
        print("空格位置:", [i for i, char in enumerate(enhanced) if char == ' '])
    
    # 检查是否有奇怪的空格模式
    if enhanced.count(' ') > 2:  # 超过2个空格可能有问题
        print("警告: 回复中有多个空格")

print("\n" + "=" * 60)
print("测试完成")

# 测试情绪分析
print("\n测试情绪分析:")
test_messages = ["订单", "谢谢", "投诉", "今天天气不错"]
for msg in test_messages:
    emotion_result = enhancer.analyze_message_emotion(msg)
    print(f"'{msg}' -> 情绪: {emotion_result['emotion']}, 置信度: {emotion_result['confidence']}")

# 测试 _cleanup_format 方法
print("\n测试 _cleanup_format 方法:")
test_strings = [
    "订单查询需要订单号。",
    "订 单 查 询 需 要 订 单 号 。",
    "你好  啊    世界！",
    "测试😊表情",
    "订单📦查询"
]

for s in test_strings:
    cleaned = enhancer._cleanup_format(s)
    print(f"'{s}' -> '{cleaned}'")