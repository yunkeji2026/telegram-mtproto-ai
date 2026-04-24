#!/usr/bin/env python3
"""
搜索神秘文本的来源
查找"看到了看到了！您刚才发的订单截图..."这段文本的来源
"""

import os
import sys
import re
from pathlib import Path

# 要搜索的文本
MYSTERY_TEXT = """看到了看到了！您刚才发的订单截图我这边已经收到啦~ 📱 让我仔细看一下哈...嗯，您这个订单是今天下午3点42分下的单，订单号是#20231215-8742对吧？购买的是我们的经典款蓝牙耳机，数量1个，总金额是299元。 目前订单状态显示“待发货”，预计明天上午会安排寄出哦！您选的收货地址是北京市朝阳区那个，对吗？ 有什么特别想了解的地方吗？还是说您想修改订单信息？😊"""

# 简化的搜索片段
SEARCH_PATTERNS = [
    "看到了看到了",
    "订单截图",
    "蓝牙耳机",
    "订单号是#20231215",
    "北京市朝阳区",
    "待发货",
    "299元"
]

def search_in_file(file_path, patterns):
    """在文件中搜索模式"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        found_patterns = []
        for pattern in patterns:
            if pattern in content:
                found_patterns.append(pattern)
        
        if found_patterns:
            return found_patterns, content[:200]  # 返回前200个字符
    except Exception as e:
        # 忽略无法读取的文件（二进制文件等）
        pass
    
    return [], ""

def search_directory(root_dir):
    """搜索目录下的所有文件"""
    root_path = Path(root_dir)
    results = []
    
    # 支持的扩展名
    text_extensions = {'.py', '.yaml', '.yml', '.txt', '.md', '.json', '.ini', '.cfg', '.conf'}
    
    for file_path in root_path.rglob('*'):
        if file_path.is_file() and file_path.suffix in text_extensions:
            patterns_found, preview = search_in_file(file_path, SEARCH_PATTERNS)
            if patterns_found:
                results.append({
                    'file': str(file_path.relative_to(root_path)),
                    'patterns': patterns_found,
                    'preview': preview
                })
    
    return results

def main():
    print("🔍 搜索神秘文本来源")
    print("=" * 60)
    
    # 在当前目录搜索
    current_dir = Path.cwd()
    print(f"搜索目录: {current_dir}")
    
    results = search_directory(current_dir)
    
    if results:
        print(f"\n✅ 在 {len(results)} 个文件中找到匹配:")
        for result in results:
            print(f"\n📄 文件: {result['file']}")
            print(f"  匹配模式: {', '.join(result['patterns'])}")
            print(f"  内容预览: {result['preview']}...")
    else:
        print("\n❌ 未在任何文件中找到匹配的文本")
        
        # 检查是否是AI生成的回复
        print("\n🔍 可能性分析:")
        print("1. AI生成的回复: 文本可能是claude-4.6-oups-high API返回的回复")
        print("2. 外部数据源: 可能来自数据库或外部API")
        print("3. 动态生成: 代码动态组合生成的文本")
        print("4. 测试数据: 开发时留下的测试回复")
        
        # 检查最近的日志文件
        log_file = Path("logs/app.log")
        if log_file.exists():
            print(f"\n📝 检查日志文件: {log_file}")
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    # 读取最后100行
                    lines = f.readlines()
                    last_lines = lines[-100:] if len(lines) > 100 else lines
                    
                    # 搜索日志中的回复
                    print("最近日志中的回复记录:")
                    for line in last_lines:
                        if '已回复消息' in line or '回复:' in line or 'reply:' in line.lower():
                            print(f"  {line.strip()[:100]}...")
            except Exception as e:
                print(f"  读取日志失败: {e}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())