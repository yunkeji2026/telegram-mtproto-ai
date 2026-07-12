# website/public/downloads/

桌面客户端安装包的**站内托管目录**。下载页（`/download`、`/en/download`）默认按
`/downloads/ChatX-Setup-<version>.exe` 引用（见 `website/lib/site.ts::DOWNLOAD_WIN_URL`）。

## 机制

- 安装包（`ChatX-Setup-*.exe`）由 `desktop/` 打包产出，**手动复制到此目录**：
  ```powershell
  # 在 desktop/ 打出安装包（见 desktop/build/build_backend.py + electron-builder）
  npm --prefix desktop run build:backend
  npm --prefix desktop run dist:win           # 产出 desktop/dist/智聊-<ver>-setup.exe
  Copy-Item desktop/dist/*setup.exe website/public/downloads/ChatX-Setup-0.1.0.exe
  ```
- 部署时 `website/scripts/deploy.ps1` 打包整个 `website/`（**不排除 public/**），
  安装包随 tarball 上传，服务器 `rsync` 落位后由 Next 直接 serve `/downloads/...`。
- **`.exe` 被根 `.gitignore`（`downloads/` 规则）忽略，不入库**——大二进制不进 git，
  只有本 README 受跟踪（`git add -f`）以自文档化本机制。

## 迁 CDN / 对象存储（可选优化）

安装包变大或下载量上来后，改由 CDN/对象存储托管：设环境变量
`NEXT_PUBLIC_DOWNLOAD_WIN_URL=https://cdn.example.com/ChatX-Setup-0.1.0.exe`
（绝对 URL），下载页自动切换为该链接、新开标签页下载，无需改代码。

## nginx 直发（可选优化）

生产走 nginx 反代时，建议给 `/downloads/` 配 `location` 直发静态文件、
绕开 Node（pm2）进程流式传输大文件，减轻应用进程压力：

```nginx
location /downloads/ {
    alias /home/ubuntu/yuntech/public/downloads/;
    add_header Cache-Control "public, max-age=3600";
}
```
