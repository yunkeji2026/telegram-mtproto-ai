#!/usr/bin/env python3
"""
测试空格问题根源
检测情绪增强器是否在每个字符间添加空格
"""

import re

def test_cleanup_format():
    """测试 _cleanup_format 方法"""
    from src.skills.emotion_enhancer import EmotionEnhancer
    
    # 创建模拟配置
    config = {
        'emoticons': {
            'emoticons': {
                'positive': ['😊'],
                'neutral': ['👉'],
                'negative': ['😔'],
                'business': ['💰'],
                'customer_service': ['💬']
            },
            'rules': {
                'max_emoticons_per_message': 3,
                'min_message_length_for_emoticon': 10,
                'avoid_emoticons_in': []
            }
        }
    }
    
    enhancer = EmotionEnhancer(config)
    
    # 测试用例
    test_cases = [
        ("Hi~ 有什么可以帮您？", "正常问候"),
        ("好的，我帮您查一下订单信息！", "中文回复"),
        ("当前可用通道：- EP通道", "业务回复"),
        ("非常抱歉给您带来不便，", "道歉回复"),
    ]
    
    print("=== 测试 _cleanup_format 方法 ===")
    for original, description in test_cases:
        # 直接调用 _cleanup_format（私有方法，需要特殊访问）
        try:
            cleaned = enhancer._EmotionEnhancer__cleanup_format(original)
            # 或者通过反射调用
            # cleaned = enhancer._cleanup_format(original)
        except:
            # 如果无法直接调用，模拟逻辑
            reply = original
            # 模拟 _cleanup_format 逻辑
            reply = re.sub(r'\s+', ' ', reply.strip())
            reply = re.sub(r'([。！？])\s*', r'\1', reply)
            reply = re.sub(r'([\w\d])([\u263a-\U0001f9ff])', r'\1 \2', reply)
            reply = re.sub(r'([\u263a-\U0001f9ff])([\w\d])', r'\1 \2', reply)
            cleaned = reply
        
        # 检查是否添加了空格
        original_chars = list(original.replace(' ', ''))
        cleaned_chars = list(cleaned.replace(' ', ''))
        
        added_spaces = False
        if len(cleaned) > len(original):
            # 检查字符间是否有新增空格
            for i in range(min(len(original), len(cleaned))):
                if i < len(original) and i < len(cleaned):
                    if original[i] != ' ' and cleaned[i] == ' ':
                        added_spaces = True
                        break
        
        print(f"\n测试: {description}")
        print(f"原始: '{original}'")
        print(f"清理后: '{cleaned}'")
        print(f"长度变化: {len(original)} -> {len(cleaned)}")
        print(f"是否添加空格: {'是' if added_spaces else '否'}")
        
        # 检查中文字符间是否有空格
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', cleaned)
        for i in range(len(chinese_chars) - 1):
            # 检查两个中文字符之间是否有空格
            pattern = f"{re.escape(chinese_chars[i])}\\s+{re.escape(chinese_chars[i+1])}"
            if re.search(pattern, cleaned):
                print(f"⚠️  发现中文字符间空格: '{chinese_chars[i]}' 和 '{chinese_chars[i+1]}'")
    
    # 测试增强整个回复
    print("\n=== 测试完整 enhance_reply 方法 ===")
    
    test_reply = "Hi~ 有什么可以帮您？"
    test_emotion = "neutral"
    test_context = {'suggested_emoticons': ['😊']}
    
    try:
        enhanced = enhancer.enhance_reply(
            original_reply=test_reply,
            emotion=test_emotion,
            context_analysis=test_context,
            message_text="你好"
        )
        
        print(f"原始回复: '{test_reply}'")
        print(f"增强后: '{enhanced}'")
        
        # 检查空格模式
        if enhanced != test_reply:
            print("🔍 差异分析:")
            # 逐字符比较
            for i, (orig_char, enh_char) in enumerate(zip(test_reply.ljust(len(enhanced)), enhanced.ljust(len(test_reply)))):
                if orig_char != enh_char:
                    print(f"  位置 {i}: '{orig_char}' -> '{enh_char}'")
    
    except Exception as e:
        print(f"测试失败: {e}")

def test_regex_patterns():
    """测试可能导致空格问题的正则表达式"""
    print("\n=== 测试正则表达式模式 ===")
    
    test_strings = [
        "Hi~有什么可以帮您？",
        "Hi~ 有什么可以帮您？",
        "好的我帮您查一下",
        "好的 我 帮 您 查 一下",
    ]
    
    # 测试 _cleanup_format 中的正则
    patterns = [
        (r'\s+', ' ', "合并多个空格"),
        (r'([。！？])\s*', r'\1', "移除标点后空格"),
        (r'([\w\d])([\u263a-\U0001f9ff])', r'\1 \2', "文字后表情加空格"),
        (r'([\u263a-\U0001f9ff])([\w\d])', r'\1 \2', "表情后文字加空格"),
    ]
    
    for test_str in test_strings:
        print(f"\n测试字符串: '{test_str}'")
        result = test_str
        for pattern, replacement, description in patterns:
            before = result
            result = re.sub(pattern, replacement, result)
            if before != result:
                print(f"  {description}: '{before}' -> '{result}'")

def test_character_iteration():
    """测试是否有代码在遍历字符时添加空格"""
    print("\n=== 测试字符遍历可能的问题 ===")
    
    test_string = "Hi~有什么可以帮您？"
    
    # 模拟可能的错误代码
    print(f"原始字符串: '{test_string}'")
    
    # 错误示例1: 遍历字符并在每个字符后添加空格
    chars = list(test_string)
    wrong_result1 = ' '.join(chars)
    print(f"错误示例1 (join空格): '{wrong_result1}'")
    
    # 错误示例2: 在特定字符后添加空格
    wrong_result2 = ''
    for char in test_string:
        wrong_result2 += char + ' '
    wrong_result2 = wrong_result2.strip()
    print(f"错误示例2 (每个字符后加空格): '{wrong_result2}'")
    
    # 错误示例3: 在中文和英文间添加空格
    wrong_result3 = re.sub(r'([a-zA-Z])([\u4e00-\u9fff])', r'\1 \2', test_string)
    wrong_result3 = re.sub(r'([\u4e00-\u9fff])([a-zA-Z])', r'\1 \2', wrong_result3)
    print(f"错误示例3 (中英文间加空格): '{wrong_result3}'")

def main():
    """主测试函数"""
    print("🔍 空格问题诊断测试")
    print("=" * 50)
    
    try:
        test_cleanup_format()
    except Exception as e:
        print(f"清理格式测试失败: {e}")
    
    test_regex_patterns()
    test_character_iteration()
    
    print("\n" + "=" * 50)
    print("📋 测试总结")
    print("=" * 50)
    print("""
可能的问题根源:
1. ❌ 情绪增强器 _cleanup_format 方法中的正则表达式
2. ❌ 遍历字符时错误添加空格
3. ❌ AI模型返回的回复本身就有空格
4. ❌ 其他处理逻辑中的字符串操作

诊断建议:
1. 检查实际日志中的"情绪增强应用成功"前后的回复内容
2. 临时完全禁用情绪增强器（已实施）
3. 检查AI原始回复是否有空格问题
4. 检查是否有其他字符串处理逻辑
    """)

if __name__ == "__main__":
    main()