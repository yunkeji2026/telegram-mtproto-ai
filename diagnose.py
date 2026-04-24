#!/usr/bin/env python3
"""
诊断脚本 - 检查Telegram MTProto AI系统状态
"""

import os
import sys
import yaml
import asyncio
from pathlib import Path

def check_config():
    """检查配置文件"""
    print("🔍 检查配置文件...")
    
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        print("❌ 配置文件不存在: config/config.yaml")
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 检查必要配置
        required = ['telegram', 'ai']
        for req in required:
            if req not in config:
                print(f"❌ 配置缺少必要部分: {req}")
                return False
        
        telegram_config = config['telegram']
        required_tg = ['api_id', 'api_hash', 'phone_number', 'session_name']
        for req in required_tg:
            if req not in telegram_config:
                print(f"❌ Telegram配置缺少: {req}")
                return False
        
        ai_config = config['ai']
        required_ai = ['api_key', 'model']
        for req in required_ai:
            if req not in ai_config:
                print(f"❌ AI配置缺少: {req}")
                return False
        
        print("✅ 配置文件检查通过")
        return True
        
    except Exception as e:
        print(f"❌ 配置文件解析失败: {e}")
        return False

def check_session():
    """检查Session文件"""
    print("\n🔍 检查Session文件...")
    
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        print("❌ 无法检查Session，配置文件不存在")
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        session_name = config['telegram']['session_name']
        session_file = Path(f"sessions/{session_name}.session")
        
        if session_file.exists():
            size = session_file.stat().st_size
            print(f"✅ Session文件存在: {session_file} ({size} bytes)")
            return True
        else:
            print(f"❌ Session文件不存在: {session_file}")
            print("💡 需要重新登录获取验证码")
            return False
            
    except Exception as e:
        print(f"❌ 检查Session失败: {e}")
        return False

def check_dependencies():
    """检查依赖"""
    print("\n🔍 检查依赖...")
    
    required = [
        'pyrogram',
        'openai',
        'aiohttp',
        'PyYAML',
        'colorama',
        'loguru'
    ]
    
    missing = []
    for dep in required:
        try:
            __import__(dep.replace('-', '_'))
            print(f"✅ {dep}")
        except ImportError:
            print(f"❌ {dep}")
            missing.append(dep)
    
    if missing:
        print(f"\n⚠️  缺少依赖: {', '.join(missing)}")
        print("💡 运行: pip install " + " ".join(missing))
        return False
    
    print("✅ 所有依赖已安装")
    return True

def check_ai_connection():
    """检查AI连接"""
    print("\n🔍 检查claude-4.6-oups-high API连接...")
    
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        api_key = config['ai']['api_key']
        base_url = config['ai'].get('base_url', 'https://api.claude-4.6-oups-high.com')
        
        import openai
        
        # 测试连接
        client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        
        # 简单调用测试
        response = client.chat.completions.create(
            model="claude-4.6-oups-high",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5,
            timeout=5
        )
        
        if response.choices:
            print("✅ claude-4.6-oups-high API连接成功")
            return True
        else:
            print("❌ claude-4.6-oups-high API返回空响应")
            return False
            
    except Exception as e:
        print(f"❌ claude-4.6-oups-high API连接失败: {e}")
        return False

async def test_simple_telegram():
    """简单测试Telegram连接"""
    print("\n🔍 测试Telegram连接...")
    
    try:
        from pyrogram import Client
        
        config_path = Path("config/config.yaml")
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        tg_config = config['telegram']
        
        # 创建客户端但不连接
        client = Client(
            name=tg_config['session_name'],
            api_id=int(tg_config['api_id']),
            api_hash=tg_config['api_hash'],
            workdir="sessions"
        )
        
        print("✅ Telegram客户端创建成功")
        
        # 检查是否已授权
        try:
            await client.connect()
            if await client.is_user_authorized():
                print("✅ 用户已授权")
                me = await client.get_me()
                print(f"✅ 登录用户: {me.first_name} (@{me.username})")
                await client.disconnect()
                return True
            else:
                print("❌ 用户未授权，需要重新登录")
                await client.disconnect()
                return False
        except Exception as e:
            print(f"❌ 连接测试失败: {e}")
            return False
            
    except Exception as e:
        print(f"❌ Telegram客户端创建失败: {e}")
        return False

def main():
    """主诊断函数"""
    print("=" * 50)
    print("Telegram MTProto AI 系统诊断")
    print("=" * 50)
    
    # 切换到脚本所在目录
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    results = []
    
    # 运行检查
    results.append(("配置文件", check_config()))
    results.append(("Session文件", check_session()))
    results.append(("依赖", check_dependencies()))
    results.append(("AI连接", check_ai_connection()))
    
    # 异步测试Telegram
    try:
        telegram_ok = asyncio.run(test_simple_telegram())
        results.append(("Telegram连接", telegram_ok))
    except Exception as e:
        print(f"❌ Telegram连接测试异常: {e}")
        results.append(("Telegram连接", False))
    
    # 汇总结果
    print("\n" + "=" * 50)
    print("诊断结果汇总:")
    print("=" * 50)
    
    all_ok = True
    for name, ok in results:
        status = "✅ 通过" if ok else "❌ 失败"
        print(f"{name:15} {status}")
        if not ok:
            all_ok = False
    
    print("\n" + "=" * 50)
    if all_ok:
        print("🎉 所有检查通过！系统应该可以正常运行。")
        print("\n💡 建议:")
        print("1. 运行: python main.py")
        print("2. 发送测试消息到Telegram")
        print("3. 检查PowerShell窗口输出")
    else:
        print("⚠️  发现问题，需要修复。")
        print("\n💡 常见问题解决:")
        print("1. 缺少依赖: pip install -r requirements.txt")
        print("2. Session过期: 删除sessions/目录下的.session文件")
        print("3. API密钥错误: 检查config/config.yaml")
        print("4. 网络问题: 检查代理设置")
    
    print("=" * 50)

if __name__ == "__main__":
    main()