#!/usr/bin/env python3
"""
claude-4.6-oups-high AI连接诊断工具
用于测试API连接、回复质量和性能
"""

import asyncio
import sys
import os
import time

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

async def diagnose_ai_connection():
    """诊断claude-4.6-oups-high AI连接状态"""
    print("=" * 60)
    print("🔍 claude-4.6-oups-high AI连接诊断工具")
    print("=" * 60)
    
    try:
        from src.ai.ai_client import AIClient
        from src.config.config import Config
    except ImportError as e:
        print(f"❌ 导入失败: {e}")
        print("请确保在项目根目录运行此脚本")
        return False
    
    # 初始化配置和客户端
    print("\n📦 初始化配置...")
    try:
        config = Config()
        client = AIClient(config)
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        return False
    
    print("✅ 配置初始化成功")
    
    # 测试AI客户端初始化
    print("\n🔌 测试claude-4.6-oups-high API连接...")
    start_time = time.time()
    try:
        success = await client.initialize()
        if not success:
            print("❌ AI客户端初始化失败")
            print("可能原因:")
            print("1. claude-4.6-oups-high API密钥未配置或无效")
            print("2. 网络连接问题")
            print("3. API服务不可用")
            return False
    except Exception as e:
        print(f"❌ 初始化异常: {e}")
        return False
    
    init_time = time.time() - start_time
    print(f"✅ AI客户端初始化成功 (耗时: {init_time:.2f}s)")
    
    # 测试用例定义
    test_cases = [
        {
            "desc": "简单问候",
            "message": "你好",
            "intent": "greeting",
            "expected": "问候回复，包含Camille身份"
        },
        {
            "desc": "业务咨询 - 价格",
            "message": "EP通道的费率是多少？",
            "intent": "price_check",
            "expected": "费率信息，专业回复"
        },
        {
            "desc": "订单查询",
            "message": "帮我查一下订单123456",
            "intent": "order_query",
            "expected": "订单查询相关回复"
        },
        {
            "desc": "通道咨询",
            "message": "有哪些可用的通道？",
            "intent": "channel_info",
            "expected": "通道列表信息"
        },
        {
            "desc": "上下文测试",
            "message": "刚才说的那个通道稳定吗？",
            "intent": "small_talk",
            "expected": "连贯性回复"
        }
    ]
    
    print(f"\n🧪 开始AI回复质量测试 ({len(test_cases)}个测试用例)")
    print("-" * 60)
    
    results = []
    total_time = 0
    
    for i, test in enumerate(test_cases, 1):
        print(f"\n📝 测试 {i}/{len(test_cases)}: {test['desc']}")
        print(f"   消息: {test['message']}")
        print(f"   预期: {test['expected']}")
        
        try:
            # 构建测试上下文
            context = {
                'user_id': 'test_user_123',
                'last_message': 'EP通道费率多少？' if '稳定' in test['message'] else '',
                'stage': 'testing'
            }
            
            # 调用AI生成回复
            start_time = time.time()
            if test['intent'] == 'greeting':
                reply = await client.generate_reply(test['message'], context)
            else:
                reply = await client.generate_reply_with_intent(
                    user_message=test['message'],
                    intent=test['intent'],
                    user_context=context
                )
            response_time = time.time() - start_time
            total_time += response_time
            
            # 分析回复质量
            if reply:
                # 检查回复长度
                length_ok = len(reply) >= 10
                
                # 检查是否包含关键词
                keywords_ok = True
                if test['intent'] == 'price_check':
                    keywords_ok = any(word in reply for word in ['费率', '价格', '%', '通道'])
                elif test['intent'] == 'order_query':
                    keywords_ok = any(word in reply for word in ['订单', '查询', '状态', '提供'])
                
                # 检查是否模板化
                is_template = any(template in reply for template in [
                    '您好！客服Camille为您服务。',
                    '当前可用通道',
                    '查询订单请提供订单号'
                ])
                
                # 输出结果
                print(f"   ⏱️  响应时间: {response_time:.2f}s")
                print(f"   📝 回复长度: {len(reply)}字符")
                print(f"   🔤 回复摘要: {reply[:80]}...")
                
                if length_ok and keywords_ok:
                    if is_template:
                        print(f"   ⚠️  结果: 成功但可能为模板回复")
                        results.append((test['desc'], True, response_time, 'template'))
                    else:
                        print(f"   ✅ 结果: 成功 - 智能回复")
                        results.append((test['desc'], True, response_time, 'ai'))
                else:
                    print(f"   ⚠️  结果: 回复质量可能不佳")
                    results.append((test['desc'], True, response_time, 'low_quality'))
            else:
                print(f"   ❌ 结果: 空回复")
                results.append((test['desc'], False, response_time, 'empty'))
                
        except asyncio.TimeoutError:
            print(f"   ⏱️  响应时间: >30s (超时)")
            print(f"   ❌ 结果: 请求超时")
            results.append((test['desc'], False, 30, 'timeout'))
        except Exception as e:
            print(f"   ⏱️  响应时间: N/A")
            print(f"   ❌ 结果: 错误 - {type(e).__name__}: {str(e)[:100]}")
            results.append((test['desc'], False, 0, f'error: {type(e).__name__}'))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 诊断结果汇总")
    print("=" * 60)
    
    success_count = sum(1 for r in results if r[1])
    total_count = len(results)
    success_rate = success_count / total_count * 100 if total_count > 0 else 0
    
    # 计算平均响应时间（仅成功请求）
    success_times = [r[2] for r in results if r[1] and r[2] > 0]
    avg_time = sum(success_times) / len(success_times) if success_times else 0
    
    # 分析回复类型
    reply_types = {}
    for r in results:
        if r[1]:  # 成功
            reply_types[r[3]] = reply_types.get(r[3], 0) + 1
    
    print(f"\n📈 总体统计:")
    print(f"   测试用例总数: {total_count}")
    print(f"   成功数量: {success_count} ({success_rate:.1f}%)")
    print(f"   失败数量: {total_count - success_count}")
    print(f"   平均响应时间: {avg_time:.2f}s (仅成功请求)")
    
    if reply_types:
        print(f"\n📊 回复类型分布:")
        for rtype, count in reply_types.items():
            percentage = count / success_count * 100 if success_count > 0 else 0
            print(f"   {rtype}: {count}次 ({percentage:.1f}%)")
    
    print(f"\n⏱️  性能评估:")
    if avg_time == 0:
        print("   ❌ 无成功请求，无法评估性能")
    elif avg_time < 3:
        print("   ✅ 优秀 - 响应迅速")
    elif avg_time < 8:
        print("   ⚠️  良好 - 响应时间可接受")
    elif avg_time < 15:
        print("   ⚠️  一般 - 响应较慢")
    else:
        print("   ❌ 差 - 响应时间过长")
    
    print(f"\n🎯 回复质量评估:")
    ai_count = reply_types.get('ai', 0)
    template_count = reply_types.get('template', 0)
    
    if success_count == 0:
        print("   ❌ 无成功回复")
    elif ai_count / success_count > 0.7:
        print("   ✅ 优秀 - 智能回复为主")
    elif ai_count / success_count > 0.4:
        print("   ⚠️  良好 - 混合回复")
    elif template_count / success_count > 0.7:
        print("   ❌ 差 - 模板回复为主")
    else:
        print("   ⚠️  一般 - 需要改进")
    
    print(f"\n🔧 建议:")
    if success_rate < 50:
        print("   1. 检查claude-4.6-oups-high API密钥配置")
        print("   2. 检查网络连接")
        print("   3. 验证API服务状态")
    elif avg_time > 8:
        print("   1. 调整API超时设置")
        print("   2. 添加重试机制")
        print("   3. 优化网络连接")
    elif template_count / success_count > 0.5:
        print("   1. 修改Skill逻辑，优先AI调用")
        print("   2. 优化系统提示")
        print("   3. 调整意图识别")
    else:
        print("   1. 系统运行正常，可进一步优化性能")
    
    print("\n" + "=" * 60)
    print("✅ 诊断完成")
    
    # 返回总体成功状态
    return success_rate > 50 and avg_time < 15

async def main():
    """主函数"""
    try:
        success = await diagnose_ai_connection()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  诊断被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 诊断过程出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())