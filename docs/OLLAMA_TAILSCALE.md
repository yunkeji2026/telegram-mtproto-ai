# Ollama + Tailscale：本机（跑 telegram-mtproto-ai）与 Ollama 主机分工

## 本机（当前这台跑 Bot 的电脑）

1. 在 `config/config.yaml` 的 `ai` 段已使用 `provider: openai_compatible`。
2. 将 `base_url` 里的主机名改为 **跑 Ollama 那台电脑** 的 Tailscale IPv4（在那台电脑上执行 `tailscale ip -4` 可得，一般为 `100.x.x.x`）。
3. 格式必须是：`http://<对方Tailscale-IP>:11434/v1`（末尾 **`/v1`** 不要漏）。
4. `model`、`embedding_model` 必须与 **Ollama 主机** 上 `ollama list` 里的名称一致（嵌入模型需单独 `ollama pull`，例如 `nomic-embed-text`）。
5. 安装依赖：`pip install -r requirements.txt`（含 `openai`）。
6. 连通性自测（PowerShell，把 IP 换成对方 Tailscale IP）：

```powershell
curl.exe http://100.x.x.x:11434/api/tags
```

---

## 给「另一台电脑」上 Cursor 用的一段指令（复制整段发给 Cursor）

把下面整段粘贴到另一台电脑（**安装并运行 Ollama 的那台**）的 Cursor 里执行/照做：

---

请在当前 Windows 电脑上完成 Ollama 对 Tailscale 局域网开放，步骤如下：

1. **确认 Tailscale 已登录**且状态为 Connected；在 **本机** PowerShell 执行 `tailscale ip -4`，记下输出的 `100.x.x.x`，把该地址发给跑 Bot 的同事/另一台机，用于填写 `config.yaml` 里的 `ai.base_url`（`http://该IP:11434/v1`）。

2. **让 Ollama 监听外网卡**：系统环境变量新增 `OLLAMA_HOST` = `0.0.0.0`，确定后**完全退出托盘里的 Ollama 再重新打开**（或重启电脑）。

3. **Windows 防火墙**：入站规则允许 **TCP 11434**（若另一台仍连不上，可暂时关防火墙测通后再收紧规则）。

4. **拉取与 Bot 配置一致的模型**（名称以对方 `config.yaml` 的 `ai.model` / `ai.embedding_model` 为准），例如：

```text
ollama pull qwen2.5:latest
ollama pull nomic-embed-text
```

5. **本机验证**：`curl http://127.0.0.1:11434/api/tags` 有 JSON；再在 **跑 Bot 的那台电脑** 上用对方 Tailscale IP 测：`curl http://100.x.x.x:11434/api/tags`。

---

## 常见问题

- **403 / 连不上**：多数是 Ollama 仍只绑在 `127.0.0.1`，检查 `OLLAMA_HOST` 是否生效并重启 Ollama。
- **嵌入失败**：在 Ollama 主机执行 `ollama pull <embedding_model>`，与 `config.yaml` 中 `embedding_model` 一致。
- **改回云端 Gemini**：把 `ai.provider` 设为 `gemini`，并按原方式填写 Google API 与 `google-genai` 相关配置。

---

## 当前生产实况：LAN bge-m3 嵌入主机 + SSH 远程拉起 watchdog（2026-07 上线）

> 上面 Tailscale/nomic 段是早期方案；当前嵌入走**同局域网直连**，模型统一为 **bge-m3(1024 维)**。

### 拓扑

- **嵌入主机 `.43`**（`192.168.1.43:11434`）：Ollama + `bge-m3:latest`。自带两层自愈：
  `OllamaServe`（崩溃重启计划任务）+ `OllamaHealth`（每分钟自检）。
- **客服机 `.44`**（`192.168.1.44`，跑 Bot 本体）：`config.local.yaml` 里
  `ai.embedding_base_url=http://192.168.1.43:11434`、`ai.embedding_model=bge-m3`。
  额外跑 `LanEmbedWatchdog` 计划任务作为**第三层兜底**——`.43` 自愈失败或客服机侧探测不到时，
  SSH 进 `.43` kill 卡死进程 + 触发 `OllamaServe`，等 12s 复检。

### SSH 免密（watchdog 远程拉起的前提）

- 客户端 `.44` 密钥：`~/.ssh/lan_embed_43`（**无密码**），`~/.ssh/config` 里 `Host lan-embed`
  指向 `.43` + `IdentitiesOnly yes`。
- 服务端 `.43`：公钥写入 `C:\ProgramData\ssh\administrators_authorized_keys`，
  属主 `NT AUTHORITY\SYSTEM`，ACL 仅 `SYSTEM:F` + `Administrators:F`、禁继承。
- 自测：`ssh -o BatchMode=yes lan-embed "echo REMOTE_OK"` 应无密码返回。

### ⚠️ 坑 1：Windows 生成"无密码"密钥必须用 .bat，别在 PowerShell/cmd 内联传 `-N ""`

PowerShell 的 `-N '""'`/`-N ""`、`cmd /c "...-N ""..."`、甚至 `--%` 停止解析符，**都会把空值吞掉或
当成非空字符串**，结果生成一把**带密码**的私钥（曾把两个引号字符 `""` 当成 passphrase）。

**唯一可靠方式**——写一个真正的 `.bat` 再执行（批处理里 `""` 才是真空字符串）：

```powershell
$bat = "$env:TEMP\genkey.bat"
Set-Content -Path $bat -Value 'ssh-keygen -t ed25519 -f "%USERPROFILE%\.ssh\lan_embed_43" -N "" -C tg-lan-embed-43 -q' -Encoding Ascii
cmd /c $bat; Remove-Item $bat -Force
```

**确定性验证是否真无密码**（`ssh-keygen -y` 在 Windows 从控制台读密码、后台会卡，不可靠）——
直接解码私钥文件头看 cipher：

```powershell
$b64 = (Get-Content "$env:USERPROFILE\.ssh\lan_embed_43" | Where-Object {$_ -notmatch 'PRIVATE KEY'}) -join ''
$head = [System.Text.Encoding]::ASCII.GetString([Convert]::FromBase64String($b64)[0..40])
# 含 "none none" = 无密码 OK；含 "aes256-ctr / bcrypt" = 带密码，重来
```

### ⚠️ 坑 2：`Server accepts key` 之后立刻 `Permission denied` = 私钥带密码

`ssh -vvv` 里出现 `Server accepts key`（公钥在授权文件里，PK_OK 探测阶段**不需签名**能过）
但紧接着 `Permission denied`——典型症状就是**私钥带 passphrase**：watchdog 用 `BatchMode`
无法交互输密码去解密私钥签名。**不是 sshd_config、不是 ACL、不是防火墙/WiFi**。按坑 1 重建无密码密钥即可。

### 授权公钥轮换（服务端 .43，注意单元素塌陷 bug）

`$keep = Get-Content $ak | Where-Object {...}` 命中**单行时会塌陷成字符串**，`$keep + $new`
变字符串拼接把两把 key 粘到同一行损坏文件。务必强制数组：

```powershell
$ak = "$env:ProgramData\ssh\administrators_authorized_keys"
$new = 'ssh-ed25519 AAAA...tg-lan-embed-43'
$keep = @(Get-Content $ak | Where-Object { $_ -notmatch 'lan-embed-43' -and $_.Trim() -ne '' })
@($keep + $new) | Set-Content -Path $ak -Encoding Ascii
icacls $ak /inheritance:r /grant "SYSTEM:F" "Administrators:F" | Out-Null
```

### watchdog 计划任务（客服机 .44）

- 脚本：`scripts/lan_embed_watchdog.ps1`（**全 ASCII**，PS 5.1 会把 UTF-8-无-BOM 按 GBK 误解码而崩，勿加中文）。
- 注册（**当前登录用户**下跑，SYSTEM 无 `~/.ssh` 密钥）：

```powershell
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File D:\workspace\telegram-mtproto-ai\scripts\lan_embed_watchdog.ps1"
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "LanEmbedWatchdog" -Action $act -Trigger $trg -RunLevel Highest -User $env:USERNAME -Force
```

- 网络分类护栏：`-Preflight` 只诊断不动服务；`UNREACHABLE`（不同 WiFi/离线）时**不误 SSH 重启**，
  只有确认可达但服务死了才远程拉起。日志 `logs/lan_embed_watchdog.log`（自带轮转）。

### 迁移备忘（nomic→bge-m3）

- 维度从 768→1024。已给 `episodic_memory` 加 `embedding_model`/`embedding_dim` 列，
  cosine 相似度对**不等长向量直接返回 0**（防跨模型脏比较），780 条历史记忆已 backfill 统一到 bge-m3/1024。
- KB embed 端点也已改用 `ai.embedding_base_url`（与记忆同源）。
