#!/usr/bin/env python3
"""
验证配置是否正确加载，特别是emoticons.enabled设置
"""

import yaml
import os
import sys

def load_yaml_config(filepath):
    """加载YAML配置文件"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"❌ 加载配置文件失败 {filepath}: {e}")
        return None

def verify_emoticons_config():
    """验证emoticons配置"""
    print("🔍 验证emoticons配置...")
    
    # 加载主配置文件
    config = load_yaml_config("config/config.yaml")
    if not config:
        return False
    
    # 检查emoticons配置
    emoticons_config = config.get('emoticons', {})
    print(f"emoticons配置: {emoticons_config}")
    
    # 检查enabled字段
    enabled = emoticons_config.get('enabled')
    print(f"emoticons.enabled 值: {enabled} (类型: {type(enabled)})")
    
    # 验证布尔值
    if enabled is None:
        print("❌ emoticons.enabled 不存在")
        return False
    elif isinstance(enabled, bool):
        if enabled == False:
            print("✅ emoticons.enabled 正确设置为布尔值 False")
            return True
        else:
            print("❌ emoticons.enabled 为布尔值 True (应为 False)")
            return False
    elif isinstance(enabled, str):
        enabled_lower = enabled.lower()
        if enabled_lower in ['false', 'no', '0', 'off']:
            print(f"⚠️  emoticons.enabled 为字符串 '{enabled}'，应转换为布尔值 False")
            # 建议修复
            print("💡 建议修复: 确保在代码中正确处理字符串值")
            return True  # 内容正确，但类型不对
        else:
            print(f"❌ emoticons.enabled 为字符串 '{enabled}'，无法识别为 False")
            return False
    else:
        print(f"❌ emoticons.enabled 为未知类型 {type(enabled)}: {enabled}")
        return False

def verify_trigger_config():
    """验证trigger配置"""
    print("\n🔍 验证trigger配置...")
    
    config = load_yaml_config("config/config.yaml")
    if not config:
        return False
    
    trigger_config = config.get('trigger', {})
    print(f"trigger配置: {trigger_config}")
    
    enabled = trigger_config.get('enabled')
    print(f"trigger.enabled 值: {enabled} (类型: {type(enabled)})")
    
    if enabled is None:
        print("❌ trigger.enabled 不存在")
        return False
    elif isinstance(enabled, bool):
        if enabled == True:
            print("✅ trigger.enabled 正确设置为布尔值 True")
            return True
        else:
            print("❌ trigger.enabled 为布尔值 False (应为 True)")
            return False
    else:
        print(f"⚠️  trigger.enabled 为类型 {type(enabled)}，可能需要转换为布尔值")
        return False

def verify_telegram_client_code():
    """验证telegram_client.py中的代码"""
    print("\n🔍 验证telegram_client.py代码...")
    
    try:
        with open("src/client/telegram_client.py", 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查初始化逻辑
        init_check = "if emoticons_config.get('enabled', True):" in content
        print(f"初始化检查 emoticons_config.get('enabled', True): {'✅ 存在' if init_check else '❌ 不存在'}")
        
        # 检查调用逻辑
        call_check = "if reply_text and self.emotion_enhancer and emoticons_config.get('enabled', True):" in content
        print(f"调用检查 emoticons_config.get('enabled', True): {'✅ 存在' if call_check else '❌ 不存在'}")
        
        # 检查日志
        log_check = "情绪增强器已禁用（配置: emoticons.enabled: false）" in content
        print(f"禁用日志: {'✅ 存在' if log_check else '❌ 不存在'}")
        
        return init_check and call_check
    except Exception as e:
        print(f"❌ 读取代码文件失败: {e}")
        return False

def check_running_system():
    """检查运行中的系统状态"""
    print("\n🔍 检查运行状态...")
    
    log_file = "logs/app.log"
    if os.path.exists(log_file):
        print(f"✅ 日志文件存在: {log_file}")
        
        # 读取最后50行
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                last_lines = lines[-50:] if len(lines) > 50 else lines
            
            print("📄 最后相关日志:")
            for line in last_lines:
                if any(keyword in line for keyword in ['情绪增强', 'emoticons', 'trigger', '四层触发']):
                    print(f"  {line.strip()}")
            
            # 检查是否有情绪增强日志
            emotion_logs = [line for line in last_lines if '情绪增强' in line]
            if emotion_logs:
                print(f"⚠️  发现情绪增强日志: {len(emotion_logs)} 条")
                for log in emotion_logs[-3:]:
                    print(f"  示例: {log.strip()}")
            else:
                print("✅ 未发现情绪增强相关日志")
                
        except Exception as e:
            print(f"❌ 读取日志失败: {e}")
    else:
        print(f"⚠️  日志文件不存在: {log_file}")

def fix_config_if_needed():
    """如果需要，修复配置文件"""
    print("\n🔧 检查是否需要修复配置...")
    
    config = load_yaml_config("config/config.yaml")
    if not config:
        return False
    
    needs_fix = False
    emoticons_config = config.get('emoticons', {})
    
    # 检查emoticons.enabled
    enabled = emoticons_config.get('enabled')
    if isinstance(enabled, str):
        enabled_lower = enabled.lower()
        if enabled_lower in ['false', 'no', '0', 'off']:
            print(f"⚠️  发现字符串值 '{enabled}'，建议修复为布尔值")
            # 询问是否修复
            response = input("是否修复为布尔值 False? (y/n): ")
            if response.lower() == 'y':
                config['emoticons']['enabled'] = False
                needs_fix = True
    
    # 检查trigger配置
    if 'trigger' not in config:
        print("⚠️  trigger配置不存在，建议添加")
        response = input("是否添加trigger配置? (y/n): ")
        if response.lower() == 'y':
            config['trigger'] = {
                'enabled': True,
                'config_file': 'config/trigger_rules.yaml',
                'debug': {'enabled': False}
            }
            needs_fix = True
    
    # 保存修复
    if needs_fix:
        try:
            with open("config/config.yaml", 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            print("✅ 配置文件已修复")
        except Exception as e:
            print(f"❌ 保存配置文件失败: {e}")
            return False
    
    return needs_fix

def main():
    """主验证函数"""
    print("=" * 60)
    print("Telegram AI 配置验证工具")
    print("=" * 60)
    
    # 确保在正确目录
    if not os.path.exists("config/config.yaml"):
        print("❌ 不在项目根目录，请切换到 telegram-mtproto-ai 目录")
        return
    
    # 验证配置
    emoticons_ok = verify_emoticons_config()
    trigger_ok = verify_trigger_config()
    code_ok = verify_telegram_client_code()
    
    # 检查运行状态
    check_running_system()
    
    # 总结
    print("\n" + "=" * 60)
    print("验证结果总结")
    print("=" * 60)
    
    results = {
        "emoticons配置": "✅ 通过" if emoticons_ok else "❌ 失败",
        "trigger配置": "✅ 通过" if trigger_ok else "❌ 失败",
        "代码修改": "✅ 通过" if code_ok else "❌ 失败",
    }
    
    for item, status in results.items():
        print(f"{item}: {status}")
    
    # 问题诊断
    if not emoticons_ok:
        print("\n🔴 emoticons配置问题:")
        print("   1. 检查 config/config.yaml 中 emoticons.enabled 应为 false")
        print("   2. 确保值为布尔值 False，不是字符串 'false'")
        print("   3. 重启系统应用配置更改")
    
    if not trigger_ok:
        print("\n🔴 trigger配置问题:")
        print("   1. 检查 config/config.yaml 中 trigger.enabled 应为 true")
        print("   2. 确保 trigger 配置节存在")
        print("   3. 重启系统应用配置更改")
    
    if not code_ok:
        print("\n🔴 代码修改问题:")
        print("   1. 检查 src/client/telegram_client.py 中的修改是否生效")
        print("   2. 确保情绪增强器初始化和调用都检查 emoticons.enabled")
        print("   3. 重新应用代码修改并重启系统")
    
    # 建议
    print("\n💡 建议操作:")
    print("   1. 运行 full_auto_optimize.bat 进行全自动修复和测试")
    print("   2. 或手动修复上述问题后重启系统")
    print("   3. 发送测试消息验证修复效果")
    
    # 询问是否修复
    print("\n🛠️  是否自动修复配置问题?")
    response = input("运行自动修复? (y/n): ")
    if response.lower() == 'y':
        fix_config_if_needed()
        print("\n✅ 修复完成，请重启系统: python main.py")

if __name__ == "__main__":
    main()