#!/usr/bin/env python3
"""
API凭证验证工具
用于快速验证用户提供的API凭证格式
"""

import re
import sys


def validate_telegram_api_id(api_id: str) -> bool:
    """验证Telegram API ID格式"""
    try:
        # API ID应该是数字
        int(api_id)
        return True
    except ValueError:
        return False


def validate_telegram_api_hash(api_hash: str) -> bool:
    """验证Telegram API Hash格式"""
    # API Hash应该是32位的十六进制字符串
    pattern = r'^[a-fA-F0-9]{32}$'
    return bool(re.match(pattern, api_hash))


def validate_phone_number(phone_number: str) -> bool:
    """验证手机号格式"""
    # 简单验证：以+开头，包含数字
    pattern = r'^\+\d{10,15}$'
    return bool(re.match(pattern, phone_number))


def validate_claude-4.6-oups-high_api_key(api_key: str) -> bool:
    """验证claude-4.6-oups-high API密钥格式"""
    # claude-4.6-oups-high API密钥通常以'sk-'开头
    return api_key.startswith('sk-') and len(api_key) > 10


def validate_all_credentials(credentials: dict) -> dict:
    """
    验证所有凭证
    
    Args:
        credentials: 包含凭证的字典
        
    Returns:
        验证结果字典
    """
    results = {
        'valid': True,
        'errors': [],
        'details': {}
    }
    
    # 检查Telegram API ID
    if 'telegram_api_id' in credentials:
        is_valid = validate_telegram_api_id(credentials['telegram_api_id'])
        results['details']['telegram_api_id'] = is_valid
        if not is_valid:
            results['valid'] = False
            results['errors'].append('Telegram API ID格式错误，应为数字')
    
    # 检查Telegram API Hash
    if 'telegram_api_hash' in credentials:
        is_valid = validate_telegram_api_hash(credentials['telegram_api_hash'])
        results['details']['telegram_api_hash'] = is_valid
        if not is_valid:
            results['valid'] = False
            results['errors'].append('Telegram API Hash格式错误，应为32位十六进制字符串')
    
    # 检查手机号
    if 'phone_number' in credentials:
        is_valid = validate_phone_number(credentials['phone_number'])
        results['details']['phone_number'] = is_valid
        if not is_valid:
            results['valid'] = False
            results['errors'].append('手机号格式错误，应以+开头，如+8612345678900')
    
    # 检查claude-4.6-oups-high API密钥
    if 'claude-4.6-oups-high_api_key' in credentials:
        is_valid = validate_claude-4.6-oups-high_api_key(credentials['claude-4.6-oups-high_api_key'])
        results['details']['claude-4.6-oups-high_api_key'] = is_valid
        if not is_valid:
            results['valid'] = False
            results['errors'].append('claude-4.6-oups-high API密钥格式错误，应以sk-开头')
    
    return results


def parse_credentials_from_text(text: str) -> dict:
    """
    从文本中解析凭证
    
    Args:
        text: 用户提供的凭证文本
        
    Returns:
        解析后的凭证字典
    """
    credentials = {}
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 尝试解析键值对
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            
            # 映射不同的键名
            if 'api_id' in key or 'api id' in key:
                credentials['telegram_api_id'] = value
            elif 'api_hash' in key or 'api hash' in key:
                credentials['telegram_api_hash'] = value
            elif 'phone' in key or 'phone_number' in key:
                credentials['phone_number'] = value
            elif 'api_key' in key or 'apikey' in key:
                credentials['claude-4.6-oups-high_api_key'] = value
            elif 'claude-4.6-oups-high' in key:
                credentials['claude-4.6-oups-high_api_key'] = value
    
    return credentials


def main():
    """主函数：验证凭证"""
    print("🔐 API凭证验证工具")
    print("=" * 50)
    
    # 检查是否从命令行参数读取
    if len(sys.argv) > 1:
        # 从命令行参数读取
        if len(sys.argv) == 5:
            credentials = {
                'telegram_api_id': sys.argv[1],
                'telegram_api_hash': sys.argv[2],
                'phone_number': sys.argv[3],
                'claude-4.6-oups-high_api_key': sys.argv[4]
            }
        else:
            print("用法: python validate_credentials.py <api_id> <api_hash> <phone> <api_key>")
            sys.exit(1)
    else:
        # 从标准输入读取
        print("请输入API凭证 (每行一个，格式: 键: 值):")
        print("例如:")
        print("  api_id: 1234567")
        print("  api_hash: abcdef1234567890abcdef1234567890")
        print("  phone_number: +8612345678900")
        print("  api_key: sk-abcdef1234567890abcdef1234567890")
        print()
        
        text_lines = []
        try:
            while True:
                line = input()
                if line.lower() == 'done':
                    break
                text_lines.append(line)
        except EOFError:
            pass
        
        text = '\n'.join(text_lines)
        credentials = parse_credentials_from_text(text)
    
    # 验证凭证
    if not credentials:
        print("❌ 未找到有效的凭证信息")
        sys.exit(1)
    
    print("\n🔍 验证结果:")
    results = validate_all_credentials(credentials)
    
    # 显示验证结果
    for key, is_valid in results['details'].items():
        status = "✅" if is_valid else "❌"
        print(f"{status} {key}: {is_valid}")
    
    if results['valid']:
        print("\n🎉 所有凭证格式验证通过！")
        
        # 显示摘要
        print("\n📋 凭证摘要:")
        for key, value in credentials.items():
            # 隐藏敏感信息的部分内容
            if 'key' in key or 'hash' in key:
                masked = value[:8] + '...' + value[-4:] if len(value) > 12 else '***'
                print(f"  {key}: {masked}")
            elif 'phone' in key:
                print(f"  {key}: {value}")
            else:
                print(f"  {key}: {value}")
    else:
        print("\n❌ 验证失败:")
        for error in results['errors']:
            print(f"  - {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()