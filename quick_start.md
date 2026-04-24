# 🚀 Telegram MTProto AI聊天助手 - 快速启动指南

## 📋 前置条件
1. **Python 3.8+** - 确保已安装Python
2. **Telegram账号** - 需要手机号登录
3. **Telegram API凭证** - 从 https://my.telegram.org 获取
4. **claude-4.6-oups-high API密钥** - 从claude-4.6-oups-high控制台获取

## ⏱️ 5分钟快速部署

### 步骤1: 安装依赖
```bash
# 进入项目目录
cd telegram-mtproto-ai

# 安装Python依赖
pip install -r requirements.txt
```

### 步骤2: 配置API凭证
```bash
# 复制配置文件
cp config/config.example.yaml config/config.yaml

# 编辑配置文件
# Windows: notepad config\config.yaml
# Linux/Mac: nano config/config.yaml
```

**编辑以下配置项**:
```yaml
telegram:
  api_id: "YOUR_API_ID"           # ← 替换为你的API ID
  api_hash: "YOUR_API_HASH"       # ← 替换为你的API Hash
  phone_number: "+8612345678900"  # ← 替换为你的手机号

ai:
  api_key: "YOUR_claude-4.6-oups-high_API_KEY"  # ← 替换为你的claude-4.6-oups-high API密钥
```

### 步骤3: 运行AI助手
```bash
# 启动AI聊天助手
python main.py
```

### 步骤4: 手机验证（首次运行）
首次运行时会提示输入手机验证码：
1. 查看手机短信或Telegram应用获取验证码
2. 输入验证码继续
3. 如果启用两步验证，需要输入密码

## 🔧 配置说明

### Telegram API获取步骤
1. 访问 https://my.telegram.org
2. 使用手机号登录
3. 创建新应用
4. 获取 `api_id` 和 `api_hash`

### claude-4.6-oups-high API获取步骤
1. 访问 claude-4.6-oups-high 控制台
2. 创建API密钥
3. 复制密钥到配置文件中

## 📁 项目结构
```
telegram-mtproto-ai/
├── config/           # 配置文件
├── src/              # 源代码
├── logs/             # 日志文件
├── sessions/         # Telegram会话文件
├── main.py           # 主程序入口
├── requirements.txt  # Python依赖
└── setup.py          # 安装脚本
```

## 🎯 功能特性
- ✅ **用户身份聊天** - 以真实用户身份回复
- ✅ **智能意图识别** - 8种意图自动识别
- ✅ **多样化回复** - 防重复，自然流畅
- ✅ **上下文管理** - 用户对话历史跟踪
- ✅ **并发处理** - 同时处理多个用户
- ✅ **错误恢复** - 自动重试机制

## 🔄 工作流程
```
用户消息 → Telegram服务器 → MTProto客户端 → 意图识别 → 
Skill工作流 → claude-4.6-oups-high V3 → 回复生成 → Telegram服务器 → 用户
```

## 🐛 故障排除

### 常见问题1: 手机验证码问题
**症状**: 无法接收或输入验证码
**解决**:
1. 确保手机号正确（带国际区号）
2. 检查手机短信或Telegram应用
3. 可能需要使用`+country_code`格式

### 常见问题2: API凭证错误
**症状**: 登录失败或API调用错误
**解决**:
1. 确认api_id和api_hash正确
2. 确认claude-4.6-oups-high API密钥有效
3. 检查网络连接

### 常见问题3: Python依赖问题
**症状**: 导入错误或缺少模块
**解决**:
```bash
# 重新安装依赖
pip uninstall -r requirements.txt -y
pip install -r requirements.txt
```

## 📊 监控与日志
- **日志文件**: `logs/app.log`
- **实时监控**: 控制台输出
- **错误追踪**: 详细错误堆栈

## 🚀 高级配置

### 启用群组消息处理
编辑 `config/config.yaml`:
```yaml
telegram:
  process_groups: true  # 启用群组消息处理
```

### 自定义回复模板
编辑 `config/config.yaml` 中的 `templates` 部分:
```yaml
templates:
  greeting:
    - "您好！我是Camille，很高兴为您服务！"
    - "Hi~ 有什么可以帮您的吗？"
    - "欢迎咨询！请告诉我您需要什么帮助？"
```

### 调整冷却时间
```yaml
skills:
  cooldown:
    per_user: 60      # 同一用户冷却时间(秒)
    per_content: 120  # 相同内容冷却时间
    global: 30        # 全局冷却时间
```

## 🔧 开发扩展

### 添加新Skill
1. 在 `src/skills/` 中创建新skill文件
2. 继承 `Skill` 基类
3. 实现 `execute` 方法
4. 在 `skill_manager.py` 中注册新skill

### 自定义意图识别
编辑 `config/config.yaml` 中的 `intent` 部分:
```yaml
intent:
  keywords:
    new_intent: ["关键词1", "关键词2"]
  patterns:
    new_intent: ["正则表达式模式"]
```

## 📞 技术支持
如有问题，请：
1. 查看 `logs/app.log` 获取详细错误信息
2. 检查配置是否正确
3. 确保所有依赖已安装
4. 验证API凭证有效性

## 🎉 成功运行标志
1. ✅ 程序正常启动，无错误信息
2. ✅ 显示"Telegram客户端已启动，等待消息..."
3. ✅ 成功登录并显示用户信息
4. ✅ 可以正常接收和回复消息

**祝您使用愉快！** 🚀