# 真实样片投放目录 / Real sample drop-in

首页「真实案例 · 眼见为实」(`#proof`) 区块的成片画廊会自动读取本目录下的真实样片；
**文件存在即真实播放，缺失则优雅降级**为「样片按需提供 · 预约真机演示」提示（不会显示坏播放器）。

The homepage "Real Proof" (`#proof`) gallery auto-loads real samples from this folder.
When a file exists it plays for real; when missing it gracefully degrades to a
"samples on request" note (no broken players are ever shown).

## 当前文件 / Current files（2026-07-07 已上线真实样片）

| 文件 | 内容 | 生成来源（引擎） |
|---|---|---|
| `voice-zh.mp3` | 克隆音·中文朗读（Fish-Speech 实测产出，-16 LUFS 响度归一） | Hub `/api/tts_only` |
| `voice-en.mp3` | 克隆音·英文朗读 | 同上，`language=en` |
| `voice-ja.mp3` | 克隆音·日文朗读 | 同上，`language=ja` |
| `digital-human.mp4` | 活体数字人口播 8.4s（口型同步+眨眼摆头，H.264 528×768） | Hub `/avatar/speak` (`generate_lipsync=true`) |
| `digital-human-poster.png` | 视频海报帧（1.2s 处截帧） | ffmpeg |

## 重新生成 / Regenerate

引擎机（本机跑着 AvatarHub :9000）上执行：

```powershell
python scripts/gen-proof-samples.py $env:TEMP\samples   # 3 段多语种克隆音 WAV
python scripts/gen-proof-video.py   $env:TEMP\samples 刘德华  # 口播视频
# 之后用 ffmpeg 转 mp3(-16 LUFS)/H.264 faststart，替换本目录同名文件即可
```

> 说明：所有样片必须为**已获肖像/声音授权**的素材（自有形象或授权演示位）。
> 引擎产出默认带 C2PA 可验真水印，投放前无需去水印——「可验真」本身就是卖点。
