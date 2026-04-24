# 语音识别集成计划

**创建时间**: 2026-03-08 05:55 GMT+8  
**需求来源**: 用户要求"学习如何识别语音"  
**当前状态**: 系统只处理文本消息，语音消息被忽略

## 🎯 需求分析
### **用户期望**
- Telegram AI系统能够处理语音消息
- 语音转文字后由AI回复
- 保持当前系统架构

### **技术现状**
- **消息处理器**: 只检查`message.text`或`message.caption`
- **语音消息**: 有`message.voice`或`message.audio`属性
- **文件处理**: 需要下载语音文件后识别

## 🔧 技术方案选择

### **方案A: 本地Whisper模型** (推荐)
#### **优点**
- 完全免费，无API成本
- 离线工作，隐私保护
- 响应速度快（本地处理）

#### **缺点**
- 需要下载模型（~1.5GB）
- 需要GPU/CPU资源
- 安装配置复杂

#### **实现步骤**
1. 安装依赖：`pip install openai-whisper` 或 `pip install faster-whisper`
2. 下载模型：首次运行自动下载
3. 修改消息处理器：添加语音消息支持
4. 集成识别：语音→文字→AI处理→回复

#### **依赖**
```txt
openai-whisper>=20231117
# 或轻量版
faster-whisper>=0.9.0
torch>=2.0.0  # 可选，GPU加速
```

### **方案B: 在线API服务**
#### **选项1: OpenAI Whisper API**
- 成本：$0.006/分钟
- 准确率：高
- 实现简单

#### **选项2: Google Speech-to-Text**
- 成本：$0.009/分钟（标准）
- 功能丰富：多语言支持

#### **选项3: Azure Speech Services**
- 成本：$1/小时
- 企业级功能

### **方案C: 混合方案**
- 小型模型本地识别常用短语
- 复杂内容使用API
- 平衡成本与响应速度

## 🚀 实现计划

### **阶段1: 基础架构** (2小时)
1. **扩展消息处理器**
   ```python
   # 修改telegram_client.py
   async def _process_message(self, message: Message):
       # 现有文本处理
       text = message.text or message.caption
       
       # 新增语音处理
       if message.voice or message.audio:
           text = await self._transcribe_voice(message)
       
       if text:
           # 现有处理逻辑
   ```

2. **语音转文字服务接口**
   ```python
   class VoiceTranscriber:
       async def transcribe(self, voice_path: str) -> str:
           # 根据配置选择方案
           pass
   ```

3. **配置扩展**
   ```yaml
   voice_recognition:
     enabled: true
     provider: "whisper"  # whisper, google, azure, openai
     model_size: "base"   # tiny, base, small, medium, large
     language: "zh"       # 默认语言
   ```

### **阶段2: Whisper本地集成** (3小时)
1. **依赖安装脚本**
2. **模型下载管理**
3. **性能优化**
4. **错误处理**

### **阶段3: 测试验证** (1小时)
1. **单元测试**: 语音识别准确性
2. **集成测试**: 完整流程
3. **性能测试**: 响应时间
4. **兼容性测试**: 不同语音格式

## 📁 文件结构变化

### **新增文件**
```
src/voice/
├── __init__.py
├── transcriber.py        # 语音识别服务
├── whisper_local.py      # 本地Whisper实现
├── api_services.py       # 在线API服务
└── utils.py             # 工具函数
```

### **修改文件**
```
src/client/telegram_client.py   # 扩展消息处理
config/config.yaml              # 添加语音配置
requirements.txt                # 添加依赖
```

## ⚙️ 配置示例

```yaml
# config/config.yaml 新增部分
voice_recognition:
  enabled: true
  provider: "whisper_local"  # whisper_local, openai, google, azure
  
  # 本地Whisper配置
  whisper:
    model_size: "base"        # tiny, base, small, medium, large
    device: "cpu"            # cpu, cuda
    language: "zh"           # 语言代码，auto为自动检测
    download_root: "./models/whisper"
  
  # OpenAI API配置（如果使用）
  openai:
    api_key: "${OPENAI_API_KEY}"
    model: "whisper-1"
    
  # 文件处理
  temp_dir: "./temp/voice"
  max_file_size: 16777216    # 16MB限制
```

## 🧪 测试计划

### **功能测试**
1. **语音消息接收**: 系统能正确接收语音消息
2. **文件下载**: 语音文件正确下载到临时目录
3. **语音识别**: 准确转文字（中文测试）
4. **AI处理**: 转文字后正常进入AI处理流程
5. **回复发送**: 正常回复消息

### **性能要求**
- **识别准确率**: >85% (中文清晰语音)
- **响应时间**: <10秒（包括下载+识别+AI处理）
- **并发支持**: 同时处理多个语音消息
- **内存使用**: <2GB (含Whisper模型)

### **兼容性测试**
- **语音格式**: OGG, MP3, M4A, WAV
- **语音时长**: 5秒-5分钟
- **语言**: 中文普通话优先，支持多语言
- **网络**: 离线/在线模式切换

## 🚨 风险与挑战

### **技术风险**
1. **模型大小**: Whisper模型较大，部署困难
2. **性能问题**: CPU识别可能较慢
3. **依赖复杂**: PyTorch/Whisper依赖链复杂
4. **内存占用**: 可能超出服务器限制

### **缓解措施**
1. **使用轻量模型**: `tiny`或`base`版本
2. **GPU加速**: 有条件时启用CUDA
3. **渐进部署**: 先实现基础功能，再优化
4. **监控告警**: 添加资源使用监控

## 📅 时间预估

### **乐观估计**: 4-6小时
- 基础集成: 2小时
- 测试优化: 2小时
- 文档部署: 1小时

### **保守估计**: 8-12小时
- 技术调研: 2小时
- 集成开发: 4小时
- 测试调试: 4小时
- 部署优化: 2小时

## 🎯 优先级建议

### **立即行动** (高优先级)
1. ✅ 先解决当前系统无回复问题
2. 🔄 收集用户语音识别具体需求
3. 🧪 技术可行性验证

### **后续开发** (中优先级)
1. 🛠️ 实现基础语音识别
2. 📊 测试性能优化
3. 🚀 部署生产环境

### **未来扩展** (低优先级)
1. 🌐 多语言支持
2. 🔊 语音回复(TTS)
3. 📈 高级语音功能

## 📞 决策要点

### **需要用户确认**
1. **优先级**: 先修复当前问题 vs 立即实现语音识别
2. **预算**: 免费本地方案 vs 付费API方案
3. **时间**: 快速实现 vs 完整方案
4. **资源**: 服务器配置（内存/GPU）

### **技术建议**
**推荐方案**: 本地Whisper base模型
**原因**: 成本可控、隐私保护、响应快速
**起步**: 先实现基础功能，后续优化

---

**下一步**: 
1. 等待用户确认需求优先级
2. 修复当前系统无回复问题
3. 根据用户决定实施语音识别方案