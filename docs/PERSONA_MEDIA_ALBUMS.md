# 每人设「相册 / 媒体」——图 / 视频备货 + 触发词自动发

> 让每个人设有一个可运营的相册：后台上传**图片/视频**、配**触发词**与**配文**，客户在对话里
> 说到就自动发出。发的是**已上传的现成媒体**（秒发、零出图成本、可含视频），优先于 AI 现场出图。

## 一句话架构

```
上传（后台 UI / 迁移 CLI）
   └─► persona_media 注册表（SQLite: config/persona_media.db，元数据）
        + 文件落 src/web/static/persona_albums/<pid>/（/static 直服）
              ▲
              │ 同一份 store 读
   ┌──────────┴───────────────────────────────┐
   │ 回复链 Stage 0（命中即发，优先于生成）      │
   │  · autosend：image_autosend.run_autosend_image
   │  · skill_manager：_handle_persona_media_request
   └───────────────────────────────────────────┘
```

## 关键文件

| 层 | 文件 | 职责 |
|----|------|------|
| 存储 | `src/companion/persona_media_store.py` | SQLite 注册表（线程安全单例），CRUD / 去重 / 命中计数 / 聚合 |
| 匹配（纯函数） | `src/companion/persona_media.py` | `pick_media` / `select_media`（触发词/通用池/加权轮播+避重）、`explain_match`（试触发）、`caption_for`（多语配文） |
| 探针（软失败） | `src/companion/media_probe.py` | 视频时长/宽高（ffprobe）+ 抽帧封面（ffmpeg）+ 图片宽高（PIL），缺工具只是拿不到元数据 |
| 迁移 | `src/companion/persona_media_import.py` + `scripts/import_persona_albums.py` | 旧文件相册 → 新注册表（幂等） |
| 回复链 · autosend | `src/inbox/image_autosend.py` | `run_autosend_image`：注册相册优先，否则回落生成 |
| 回复链 · skill | `src/skills/skill_manager.py::_handle_persona_media_request` | Stage 0：命中即发（图/视频），先于自拍/物体图生成 |
| 后台 API | `src/web/routes/persona_media_routes.py` | `/api/personas/{pid}/media*`（列/传/改/删/试触发） |
| 后台 UI | `src/web/templates/personas.html` | 「人设工作室 → 相册/媒体」面板 |

## 触发语义（`pick_media`）

1. **关键词池**：条目带触发词、且客户文本命中任一触发词 → 精确发。**独立于自拍/物体意图**
   （给某视频配 `跳舞`，客户说「给我跳个舞」就命中，无需它被判成"要照片"）。
2. **通用池**：仅当客户是**泛化「要照片/自拍」请求**（`detect_selfie_request`）时，额外放开
   **无触发词**的条目（对齐老"随机挑一张自拍"）。
3. **加权轮播 + 避重**：命中多条时按 `weight` 加权随机；同一会话不连发同一条（`avoid_id`）。
4. **关系闸门**：`min_bond_level` > 当前亲密度的条目不进候选（分层内容）。
5. 命中即 `record_hit`（命中计数，供轮播避重 + 观测看板）。

启用总开关：`companion.selfie.enabled`（与形象照同口径；关＝两条链都不查相册）。

## 后台操作（人设工作室 → 相册/媒体）

- **上传**：选图/视频（可多选），可选填一行统一触发词；文件即时可用（instant，无需审核）。
- **每条目可改**：触发词、配文、**多语配文**（每行 `lang:文案`，如 `en:Hi`）、权重、关系门槛、启停。
- **试触发**：输入一句话，看会命中哪个池（关键词/通用/无）+ 全部候选（不随机，纯 dry-run）。
- **命中看板**：面板顶部显示 图/视频数 + 累计命中；全局看板见 ops-overview「🖼️ 人设相册」卡。

## 护栏（治理）

| 项 | 规则 |
|----|------|
| 扩展名白名单 | 图 `jpg/jpeg/png/webp/gif`；视频 `mp4/mov/webm/m4v`（其它 400 拒） |
| 体积上限 | 图 10MB / 视频 50MB（超 413） |
| 视频时长 | 默认上限 3 分钟；**仅当 ffprobe 可探时**才拦（探不到不拦，软失败） |
| 去重 | 按 `(persona_id, sha256)` 去重，重复上传返回已存在条目（`deduped: true`） |
| 路径安全 | `persona_id` 消毒为目录名（挡 `..`/分隔符）；删除只删相册根目录内的文件 |
| 权限 | 写操作（传/改/删）viewer 只读拦截（403） |
| 审计 | `pmedia_upload` / `pmedia_update` / `pmedia_delete` 落 audit_store |

## 视频发送与封面

- 发送链：编排器 `send_media(media_type="video")` —— Telegram `send_video`、WhatsApp/Messenger
  透传给 Node 微服务。skill_manager 侧若无受管媒体 worker（A 线纯客户端）则需 `_send_video_to_chat` 回调。
- 封面：上传视频时 ffmpeg 抽一帧（约 1s 处，缩到 320 宽）落 `*.thumb.jpg`，UI 网格用海报图 + ▶ 秒开
  （不拉全片）；缺 ffmpeg 则回落 `<video preload=metadata>`。

## 从旧文件相册迁移

旧机制（`companion_selfie` backend=album）从 `config/persona_albums/<key>/` 随机挑图，无触发词/命中。
用迁移 CLI 导入新注册表（文件复制进 static，元数据后续在后台补）：

```bash
python -m scripts.import_persona_albums                       # dry-run 全扫，看清单
python -m scripts.import_persona_albums --apply               # 真导入（全部子目录）
python -m scripts.import_persona_albums --persona lin --apply # 只导 lin（根目录散图也归 lin）
python -m scripts.import_persona_albums --triggers "自拍,selfie" --apply   # 导入项带触发词
python -m scripts.import_persona_albums --json                # JSON 输出
```

- 子目录名 = `persona_id`；根目录散图仅在 `--persona` 指定时才归给那个人设（否则跳过，不臆测）。
- **幂等**：按 `(persona_id, sha256)` 去重，可反复跑（已导入算 `dup` 跳过）。默认 **dry-run**，`--apply` 才真导。

## 观测

- JSON：`GET /api/workspace/metrics` → `persona_media = {total, enabled, photo, video, total_hits, top:[...]}`（主管专属）。
- Prometheus：`?format=prometheus` → `ws_persona_media_items` / `..._enabled` / `..._hits_total` / `..._by_type{type=...}`。
- 看板：ops-overview「🖼️ 人设相册」卡（备货/命中 + 命中 Top 8）。**备货多但命中 0** ⇒ 多半没配触发词或触发词太窄。

## 回归

```bash
python -m pytest tests/test_persona_media.py tests/test_persona_media_routes.py \
  tests/test_persona_media_import.py tests/test_media_probe.py \
  tests/test_selfie_wiring.py tests/test_image_autosend.py -q --tb=line
```

预期全绿：store CRUD/去重/命中/聚合 · 匹配器触发词/通用池/关系闸门/避重/多语配文 · 路由传改删/护栏/试触发/审计/metrics ·
迁移器发现/去重幂等/dry-run · 探针软失败 · 两条回复链 Stage 0 命中即发（图/视频）。
