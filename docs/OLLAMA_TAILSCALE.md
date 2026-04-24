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
