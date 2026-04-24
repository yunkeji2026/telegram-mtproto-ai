# AI智能客服系统

基于 Telegram User API（MTProto）+ 大模型 + Skill 工作流的智能客服与人工协同系统（技术仓库名：`telegram-mtproto-ai`）。

## 🎯 项目目标
实现一个本机运行的Python脚本，能够：
1. 通过MTProto API登录Telegram用户帐号
2. 自动处理用户私聊消息
3. 使用claude-4.6-oups-high V3生成智能回复
4. 执行完整的Skill工作流
5. 以用户身份自然聊天

## 🏗️ 技术架构
```
用户消息 → Telegram服务器 → MTProto客户端 → 意图识别 → Skill工作流 → claude-4.6-oups-high V3 → 回复 → Telegram服务器 → 用户
```

## 📁 项目结构
```
telegram-mtproto-ai/
├── config/                    # 配置文件
│   ├── config.example.yaml   # 配置示例
│   └── skills.example.yaml   # Skill配置示例
├── src/                      # 源代码
│   ├── client/              # MTProto客户端
│   ├── skills/              # Skill工作流
│   ├── ai/                  # AI集成
│   └── utils/               # 工具函数
├── logs/                    # 日志文件
├── tests/                   # 测试代码
├── requirements.txt         # Python依赖
├── setup.py                 # 安装脚本
├── main.py                  # 主程序入口
└── README.md               # 项目说明
```

## 🔧 技术栈
- **Telegram API**: `pyrogram` (MTProto客户端)
- **AI模型**: claude-4.6-oups-high V3 (通过OpenAI兼容API)
- **工作流**: 自定义Skill引擎，可扩展
- **异步处理**: `asyncio` + `aiohttp`
- **配置管理**: `PyYAML`
- **日志**: `logging` + 文件输出

## 🚀 快速开始
1. 安装依赖: `pip install -r requirements.txt`
2. 复制配置文件: `cp config/config.example.yaml config/config.yaml`
3. 编辑配置文件: 填入你的API凭证
4. 运行: `python main.py`

## ⚙️ 配置说明
### Telegram API凭证
从 https://my.telegram.org 获取：
- `api_id`: 你的API ID
- `api_hash`: 你的API Hash
- `phone_number`: 你的手机号（带国际区号）

### claude-4.6-oups-high V3 API
从claude-4.6-oups-high控制台获取：
- `claude-4.6-oups-high_api_key`: 你的API密钥
- `model`: `claude-4.6-oups-high` (默认)

### Skill工作流配置
定义不同的技能和回复策略，支持：
- 意图识别
- 多轮对话
- 上下文管理
- 条件分支

## 💡 功能特性
- ✅ **用户身份聊天**: 以真实用户身份回复
- ✅ **智能意图识别**: 8种意图类型（复用Camille系统）
- ✅ **多样化回复**: 防重复，自然流畅
- ✅ **上下文管理**: 用户对话历史跟踪
- ✅ **并发处理**: 异步处理多个用户消息
- ✅ **错误恢复**: 自动重试和降级
- ✅ **日志监控**: 详细运行状态记录
- ✅ **可扩展**: 轻松添加新Skill

## 🔄 工作流程
1. **登录Telegram**: 使用MTProto API登录用户帐号
2. **监听消息**: 监控私聊和群组消息
3. **意图识别**: 分析消息内容识别意图
4. **Skill路由**: 根据意图选择对应Skill
5. **AI处理**: 调用claude-4.6-oups-high V3生成回复
6. **发送回复**: 通过MTProto发送消息
7. **状态更新**: 更新用户上下文和日志

## 📋 实施计划
### 阶段1: 基础框架 (1-2天)
- [ ] 项目结构搭建
- [ ] 配置管理系统
- [ ] 日志系统
- [ ] 错误处理框架

### 阶段2: MTProto集成 (2-3天)
- [ ] Pyrogram客户端实现
- [ ] 消息监听和处理
- [ ] 手机验证码处理
- [ ] 会话管理和持久化

### 阶段3: AI集成 (1-2天)
- [ ] claude-4.6-oups-high V3 API封装
- [ ] 回复生成逻辑
- [ ] 上下文管理
- [ ] 防重复机制

### 阶段4: Skill工作流 (2-3天)
- [ ] Skill引擎设计
- [ ] 意图识别模块
- [ ] 工作流执行器
- [ ] 可扩展插件系统

### 阶段5: 测试优化 (1-2天)
- [ ] 单元测试
- [ ] 集成测试
- [ ] 性能优化
- [ ] 文档完善

## 🐛 故障排除
### 常见问题
1. **手机验证码**: 首次登录需要短信验证码
2. **API限制**: 注意API调用频率限制
3. **网络问题**: 确保网络连接稳定
4. **会话过期**: 定期重新登录

### 调试工具
- 详细日志输出
- 调试模式开关
- 消息跟踪功能

## 🤝 贡献指南
1. Fork项目
2. 创建功能分支
3. 提交更改
4. 发起Pull Request

## 📄 许可证
MIT License

## 📞 支持
如有问题，请参考文档或提交Issue