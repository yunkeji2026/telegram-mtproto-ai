// 产品线独立落地页内容（/voice /face /interpreting /asset-safe /nurture + /en/*）。
// 设计原则：一页一卖点，媒体证据前置（真实引擎产出），CTA 直达 Telegram。
// 指标口径与 engineContent.ts / 主仓真实配置保持一致——营销可以强，数字必须真。
// asset-safe / nurture 是「包装已有能力」页：只写已落地能力，路线图明说，不过度承诺。

export type LandingKey = "voice" | "face" | "interpreting" | "asset-safe" | "nurture";

interface L {
  zh: string;
  en: string;
}

export interface LandingDict {
  slug: string; // zh 路由；en 为 /en + slug
  productLine: L; // kicker 徽章
  seo: {
    title: L;
    description: L;
    keywords: string[];
  };
  hero: {
    title: L;
    accent: L;
    subtitle: L;
    points: L[]; // 3 个带勾要点
    demoCta?: L; // hero 次级按钮文案（缺省「先看真实样片」）
  };
  demo: {
    title: L;
    subtitle: L;
    realNote: L; // “真实产出”说明
  };
  // 无音视频样片的产品线（asset-safe / nurture）：demo 区改为逐条可当场验证的能力清单
  demoBullets?: L[];
  caps: { title: L; desc: L; proof: L }[];
  steps: { title: L; desc: L }[];
  faq: { q: L; a: L }[];
  finalCta: {
    title: L;
    desc: L;
  };
}

export const LANDINGS: Record<LandingKey, LandingDict> = {
  voice: {
    slug: "/voice",
    productLine: { zh: "幻声 VoiceX · AI 声音克隆", en: "VoiceX · AI voice cloning" },
    seo: {
      title: {
        zh: "AI 声音克隆 · 十几秒复刻你的音色 | 幻声 VoiceX — 无界科技",
        en: "AI Voice Cloning · Clone any voice in seconds | VoiceX — BOUNDLESS",
      },
      description: {
        zh: "十几秒参考音零样本克隆音色：三引擎自动择优，10 语种同一音色，情感语气自然连贯，48kHz 可商用。本地部署声音不出机房，产出带 C2PA 可验真水印。",
        en: "Zero-shot voice cloning from seconds of audio: tri-engine auto-pick, 10 languages in one voice, natural emotion, commercial-grade 48kHz. Private deployment, C2PA-verifiable output.",
      },
      keywords: ["AI声音克隆", "声音克隆软件", "voice cloning", "AI配音", "克隆音色", "TTS", "语音合成", "多语种配音"],
    },
    hero: {
      title: { zh: "十几秒参考音，", en: "A few seconds of audio," },
      accent: { zh: "克隆出一模一样的你", en: "and your voice is cloned" },
      subtitle: {
        zh: "三引擎（Fish / Qwen3 / VoxCPM）自动择优：实时对话、极速首包、48kHz 可商用各取所长。10 语种共用同一音色，情感语气自然连贯——全部本地部署，声音数据不出你的机器。",
        en: "Three engines (Fish / Qwen3 / VoxCPM) auto-picked per job: real-time chat, ultra-fast first packet, commercial 48kHz. Ten languages in one voice with natural emotion — fully private, audio never leaves your racks.",
      },
      points: [
        { zh: "Qwen3 首包 ≈97ms · 3 秒克隆 · 实时对话级", en: "≈97ms first packet · 3s cloning · real-time grade" },
        { zh: "中 / 英 / 日 / 韩等 10 语种 · 同一音色", en: "10 languages · one identical voice" },
        { zh: "C2PA 可验真水印 · 克隆伦理校验", en: "C2PA-verifiable watermark · clone-ethics checks" },
      ],
    },
    demo: {
      title: { zh: "先听，再谈", en: "Listen first, talk later" },
      subtitle: {
        zh: "同一克隆音色朗读三种语言——点开即听。",
        en: "One cloned voice reading three languages — tap to play.",
      },
      realNote: {
        zh: "以上为引擎真实产出、未经剪辑。想听你自己的音色？发 10~40 秒样本，当场克隆给你听。",
        en: "Real, unedited engine output. Want your own voice? Send a 10-40s sample and we clone it on the spot.",
      },
    },
    caps: [
      {
        title: { zh: "三引擎克隆音 · 自动择优", en: "Tri-engine cloning · auto-pick" },
        desc: {
          zh: "几十秒样本零样本克隆。Fish 实时 / Qwen3 首包极速 10 语种 / VoxCPM 48kHz 可商用，克隆完引擎自动推荐最合适的一路。",
          en: "Zero-shot cloning from seconds of audio. Fish real-time / Qwen3 ultra-fast across 10 languages / VoxCPM commercial 48kHz — auto-recommended per use case.",
        },
        proof: { zh: "Qwen3 首包 ≈97ms · 3 秒克隆 · 10 语种", en: "≈97ms first packet · 3s clone · 10 languages" },
      },
      {
        title: { zh: "情感与语气 · 像真人一样说话", en: "Emotion & prosody that feel human" },
        desc: {
          zh: "情感标签 + 自然语言指令双模式：开心、安抚、兴奋、耳语随点随换，长文朗读语气连贯不出戏。",
          en: "Emotion tags plus natural-language style prompts: happy, soothing, excited, whisper on demand — consistent prosody across long reads.",
        },
        proof: { zh: "情感引擎 + 指令模式 · 长文语气连贯", en: "Emotion engine + instruct mode" },
      },
      {
        title: { zh: "接直播 · 接电话 · 接对话大脑", en: "Plugs into live, calls and chat" },
        desc: {
          zh: "克隆音直通数字人直播、电话桥接与 AI 对话大脑：有记忆、懂情绪，答得准、聊得像真人。",
          en: "Cloned voice flows straight into digital-human streams, phone bridges and the AI conversation brain — memory, mood-awareness, human-grade replies.",
        },
        proof: { zh: "直播 / 电话 / 对话一条链路", en: "One pipeline: live / calls / chat" },
      },
      {
        title: { zh: "合规可溯源 · 一键验真", en: "Compliant & verifiable" },
        desc: {
          zh: "产出默认嵌 C2PA 内容凭证 + Ed25519 签名 + 不可见水印，第三方可离线验真；未授权音色直接拒绝克隆。",
          en: "C2PA credentials + Ed25519 signature + invisible watermark by default; unlicensed voices are refused outright.",
        },
        proof: { zh: "C2PA + Ed25519 · 克隆伦理校验", en: "C2PA + Ed25519 · ethics checks" },
      },
    ],
    steps: [
      {
        title: { zh: "发一段 10~40 秒干净人声", en: "Send 10-40s of clean speech" },
        desc: { zh: "手机录音即可，越干净越像；支持多段融合提升相似度。", en: "A phone recording works; cleaner audio clones better. Multi-clip fusion boosts similarity." },
      },
      {
        title: { zh: "引擎克隆 + 自动择优", en: "Engine clones & auto-picks" },
        desc: { zh: "约 3 秒完成克隆，引擎按你的场景自动推荐最合适的合成引擎。", en: "Cloning takes ~3 seconds; the engine recommends the best synth route for your scenario." },
      },
      {
        title: { zh: "输入文本，随处可用", en: "Type text, use it anywhere" },
        desc: { zh: "配音出片、直播开麦、电话客服、多语种内容矩阵——同一音色全场景复用。", en: "Dubbing, live streams, phone support, multilingual content — one voice everywhere." },
      },
    ],
    faq: [
      {
        q: { zh: "需要多长的声音样本？", en: "How much sample audio do I need?" },
        a: {
          zh: "10~40 秒干净人声即可克隆；样本越干净相似度越高，支持多段样本融合进一步提升。",
          en: "10-40 seconds of clean speech is enough; cleaner samples clone closer, and multi-clip fusion pushes similarity further.",
        },
      },
      {
        q: { zh: "支持哪些语言？中文克隆的音色能说英语吗？", en: "Which languages? Can a Chinese-cloned voice speak English?" },
        a: {
          zh: "支持中、英、日、韩等 10 语种，而且是同一音色跨语种——中文克隆完直接说英语、日语，听感还是同一个人。",
          en: "Ten languages including EN/ZH/JA/KO — with the same voice across all of them. Clone once in Chinese, speak English and Japanese as the same person.",
        },
      },
      {
        q: { zh: "声音数据安全吗？", en: "Is my voice data safe?" },
        a: {
          zh: "全部本地部署，样本和产出都不出你的机器；产出默认带 C2PA 可验真水印，未授权音色引擎会直接拒绝克隆。",
          en: "Everything runs on your own hardware — samples and output never leave it. Output carries C2PA verification, and unlicensed voices are refused.",
        },
      },
    ],
    finalCta: {
      title: { zh: "用你的声音，当场克隆给你听", en: "We clone your voice, live" },
      desc: {
        zh: "30 分钟真机演示：远程连你的机器或用我们的样机，你发样本我们当场克隆，跨语种朗读给你验货。",
        en: "30-minute live demo: on your machine or ours — send a sample, hear it cloned and reading across languages on the spot.",
      },
    },
  },

  face: {
    slug: "/face",
    productLine: { zh: "幻颜 FaceX × 幻影 LiveX · AI 换脸", en: "FaceX × LiveX · AI face swap" },
    seo: {
      title: {
        zh: "AI 实时换脸直播 · 高清活体数字人 | 幻颜 FaceX — 无界科技",
        en: "Real-time AI Face Swap for Live · HD Digital Human | FaceX — BOUNDLESS",
      },
      description: {
        zh: "直播实时换脸：脸区原生通道清晰度 4.5×，25fps 高清，双人同框各换各脸；高清活体数字人会眨眼摆头；图片视频成片级精修。本地部署，平台无感。",
        en: "Live face swap with a native face channel (4.5× sharper, 25fps HD), dual-face frames, living digital humans that blink and move, production-grade image/video refinement. Fully private.",
      },
      keywords: ["AI换脸", "实时换脸", "直播换脸", "face swap", "数字人直播", "虚拟主播", "换脸软件", "AI数字人"],
    },
    hero: {
      title: { zh: "直播里换一张脸，", en: "Swap your face on a live stream —" },
      accent: { zh: "清晰到看不出破绽", en: "sharp enough to fool anyone" },
      subtitle: {
        zh: "脸部走原生高分辨率通道，清晰度实测 4.5× 提升：720p 高清默认、1080p 超清随选，显卡吃紧自动降档不卡顿。双人同框各换各脸，活体数字人会眨眼摆头——全部本机运行，平台无感。",
        en: "Faces run through a native high-res channel — measured 4.5× sharper. 720p default, 1080p ultra on demand, auto-downshift under GPU pressure. Dual-face frames, living digital humans that blink and move — all on your own box.",
      },
      points: [
        { zh: "实时 25fps · 1080p 超清档 · 亚秒级首帧", en: "25fps live · 1080p ultra · sub-second first frame" },
        { zh: "双人同框各换各脸 · 第三张脸自动回退", en: "Two faces per frame · third face auto-fallback" },
        { zh: "开播前 3 秒设备体检 · 事故挡在开播前", en: "3-second pre-flight device check" },
      ],
    },
    demo: {
      title: { zh: "拖一下，眼见为实", en: "Drag the slider — see for yourself" },
      subtitle: {
        zh: "左原始右换脸，看脸区原生通道带来的清晰度差异；下方是活体数字人口播真实成片。",
        en: "Original vs swapped — see what the native face channel does. Below: a real living digital-human clip.",
      },
      realNote: {
        zh: "以上为引擎真实产出。想看你自己的脸？预约真机演示，一张正脸照当场生成。",
        en: "Real engine output. Want your own face? Book a live demo — one portrait photo is enough.",
      },
    },
    caps: [
      {
        title: { zh: "实时换脸 · 脸区原生通道", en: "Live swap · native face channel" },
        desc: {
          zh: "脸部原生高分辨率通道，实测 4.5× 清晰度；1080p 超清档并发均值 ≈323ms，显卡吃紧自动降档保流畅。",
          en: "Native high-res face channel, 4.5× sharper; 1080p ultra averages ≈323ms under concurrency and auto-downshifts to stay smooth.",
        },
        proof: { zh: "4.5× 清晰度 · 1080p ≈323ms", en: "4.5× clarity · 1080p ≈323ms" },
      },
      {
        title: { zh: "高清活体数字人", en: "HD living digital human" },
        desc: {
          zh: "会眨眼、摆头、有微表情的活体分身，不是死图对口型：克隆形象 + 克隆声 + 口型同步，直推 WebRTC / OBS。",
          en: "Blinks, head turns, micro-expressions — not a lip-synced still. Cloned face + voice + lip-sync, streamed to WebRTC / OBS.",
        },
        proof: { zh: "5090 上 25fps · 首帧 ≈0.9s", en: "25fps on a 5090 · ≈0.9s first frame" },
      },
      {
        title: { zh: "图片 / 视频换脸精修", en: "Image / video swap, refined" },
        desc: {
          zh: "成片级精修：inswapper + GFPGAN / CodeFormer + 光流时序平滑，三路并发池批量出片。",
          en: "Production pipeline: inswapper + GFPGAN / CodeFormer + optical-flow smoothing, batched via a 3-way pool.",
        },
        proof: { zh: "GFPGAN 8.8fps / CodeFormer 5.5fps", en: "GFPGAN 8.8fps / CodeFormer 5.5fps" },
      },
      {
        title: { zh: "直播虚拟背景 / 绿幕", en: "Live virtual background" },
        desc: {
          zh: "虚化 / 图片 / 绿幕一键热切换，CPU 抠像零显存占用，直播中切换毫无卡顿。",
          en: "Blur / image / green-screen hot-swap, CPU matting with zero VRAM cost — switch mid-stream without a hitch.",
        },
        proof: { zh: "抠像 ≈5ms/帧@720p · 零显存", en: "≈5ms/frame matting · zero VRAM" },
      },
    ],
    steps: [
      {
        title: { zh: "一张正脸照生成角色", en: "One portrait creates the character" },
        desc: { zh: "上传照片即建角色，开播前还能 AI 定妆换发型，整场直播生效。", en: "Upload a photo to build the character; optionally restyle hair/makeup before going live." },
      },
      {
        title: { zh: "本机开播，OBS 一键接入", en: "Go live from your own box" },
        desc: { zh: "虚拟摄像头 / OBS 通道即插即用，开播前 3 秒设备体检给你兜底。", en: "Virtual camera / OBS plug-and-play, with a 3-second pre-flight check before you start." },
      },
      {
        title: { zh: "平台无感直播", en: "Stream, platform-agnostic" },
        desc: { zh: "输出就是普通摄像头画面；显卡吃紧自动降档，绝不卡成 PPT。", en: "Output looks like any normal camera; auto-downshift keeps it smooth under load." },
      },
    ],
    faq: [
      {
        q: { zh: "需要什么显卡？", en: "What GPU do I need?" },
        a: {
          zh: "实时换脸入门 RTX 5070 Ti / 4080 16G 起；换脸 + 数字人专业档推荐 RTX 4090 24G / 5080；全功能旗舰推荐 RTX 5090 32G（可双卡）。",
          en: "Entry live swap: RTX 5070 Ti / 4080 16G. Pro (swap + digital human): RTX 4090 24G / 5080. Flagship everything-on: RTX 5090 32G (dual-ready).",
        },
      },
      {
        q: { zh: "直播平台会检测到吗？", en: "Will platforms detect it?" },
        a: {
          zh: "引擎在你本机把画面合成好，走虚拟摄像头输出——平台看到的就是一路普通摄像头信号。",
          en: "Everything is composited locally and delivered through a virtual camera — the platform just sees a normal camera feed.",
        },
      },
      {
        q: { zh: "两个人同框能各换各的脸吗？", en: "Two people in frame?" },
        a: {
          zh: "可以。左右槽位各绑定一张目标脸，出现第三张脸时自动回退不穿帮，访谈连麦都稳。",
          en: "Yes — left/right slots each bind a target face, and a third face triggers a clean auto-fallback. Solid for interviews and co-streams.",
        },
      },
    ],
    finalCta: {
      title: { zh: "用你的脸，当场换给你看", en: "Your face, swapped live" },
      desc: {
        zh: "30 分钟真机演示：在你的硬件上验证真实帧率与清晰度，用你的素材当场生成，数据不出你的机房。",
        en: "30-minute live demo on your hardware: verify real FPS and clarity with your own material — data never leaves your racks.",
      },
    },
  },

  interpreting: {
    slug: "/interpreting",
    productLine: { zh: "通译 LingoX · 克隆音实时同传", en: "LingoX · cloned-voice interpreting" },
    seo: {
      title: {
        zh: "AI 实时同传 · 用你自己的声音说外语 | 通译 LingoX — 无界科技",
        en: "Real-time AI Interpreting in Your Own Voice | LingoX — BOUNDLESS",
      },
      description: {
        zh: "克隆音双向同传：对方听到的还是你的音色。术语表锁定专有名词零翻车，支持抢话打断，OBS 直播双语字幕一条链接接入，SRT 一键导出。10 语种互译，本地部署。",
        en: "Two-way interpreting in your own cloned voice. Glossary-locked terms, barge-in interruption, OBS bilingual subtitles via one URL with SRT export. Ten languages, fully private.",
      },
      keywords: ["AI同传", "实时翻译", "同声传译", "AI interpreting", "直播翻译", "双语字幕", "克隆音翻译", "会议同传"],
    },
    hero: {
      title: { zh: "用你自己的声音，", en: "Speak languages you don't know —" },
      accent: { zh: "说你不会的语言", en: "in your own voice" },
      subtitle: {
        zh: "克隆音双向同传：你说中文，对方听到你的音色在说英语。术语表锁定专有名词（美团永远是 Meituan），支持抢话打断像真人对话；OBS 拖一条链接，直播间即出实时双语字幕。",
        en: "Two-way interpreting in your cloned voice: you speak Chinese, they hear you speaking English. A glossary locks proper nouns, barge-in keeps it conversational, and one URL adds live bilingual subtitles to OBS.",
      },
      points: [
        { zh: "克隆音同传 · 对方听到的还是你", en: "Cloned-voice output · it's still you they hear" },
        { zh: "术语表锁定 · 专有名词零翻车", en: "Glossary lock · zero term errors" },
        { zh: "OBS 双语字幕 · SRT 一键导出", en: "OBS bilingual subtitles · SRT export" },
      ],
    },
    demo: {
      title: { zh: "同一个声音，两种语言", en: "One voice, two languages" },
      subtitle: {
        zh: "先听中文原声，再听引擎用同一克隆音色输出的英文同传。",
        en: "Hear the Chinese source, then the English interpretation — same cloned voice.",
      },
      realNote: {
        zh: "以上为引擎真实产出、未经剪辑。预约真机演示，用你的声音、你的术语表现场跑一遍。",
        en: "Real, unedited engine output. Book a live demo and run it with your voice and your glossary.",
      },
    },
    caps: [
      {
        title: { zh: "克隆音同传 · 术语锁定", en: "Cloned-voice interpreting · term lock" },
        desc: {
          zh: "双向同传全程保留你的音色；术语表锁定行业专有名词，TM 缓存越用越快；支持抢话打断。",
          en: "Two-way interpreting that keeps your voice; glossary-locked terminology with a TM cache that speeds up over time; barge-in supported.",
        },
        proof: { zh: "术语表 + TM 缓存 · barge-in 打断", en: "Glossary + TM cache · barge-in" },
      },
      {
        title: { zh: "OBS 直播双语字幕", en: "OBS live bilingual subtitles" },
        desc: {
          zh: "OBS 浏览器源拖一条链接，直播间即出实时双语字幕；散场一键导出 SRT 直接投稿。",
          en: "One URL in an OBS Browser Source adds live bilingual subtitles; export SRT afterwards for publishing.",
        },
        proof: { zh: "SSE 实时推送 · 一键导出 SRT", en: "SSE live push · one-click SRT" },
      },
      {
        title: { zh: "三引擎克隆音底座", en: "Tri-engine voice foundation" },
        desc: {
          zh: "同传的声音底座即幻声 VoiceX：10 语种同一音色、情感语气自然，首包亚秒级不抢拍。",
          en: "Built on VoiceX: ten languages in one voice, natural prosody, sub-second first packet that keeps the conversation flowing.",
        },
        proof: { zh: "10 语种 · 首包亚秒级", en: "10 languages · sub-second first packet" },
      },
      {
        title: { zh: "会议 / 直播 / 电话全场景", en: "Meetings, streams and calls" },
        desc: {
          zh: "视频会议、跨境直播、电话桥接一条链路全覆盖，一键开通话套餐。",
          en: "Video meetings, cross-border streams and phone bridges on one pipeline, with a one-tap call package.",
        },
        proof: { zh: "会议 · 直播 · 电话一条链路", en: "One pipeline for all three" },
      },
    ],
    steps: [
      {
        title: { zh: "克隆你的音色", en: "Clone your voice" },
        desc: { zh: "10~40 秒样本一次克隆，中外双向同传共用同一音色。", en: "One 10-40s sample powers both directions of interpretation." },
      },
      {
        title: { zh: "选语向 + 导入术语表", en: "Pick languages + import glossary" },
        desc: { zh: "中英日韩等 10 语种互译；把行业词表交给引擎，专有名词从此零翻车。", en: "Ten languages; hand the engine your term list and proper nouns never break." },
      },
      {
        title: { zh: "开会 / 开播 / 通话", en: "Meet, stream or call" },
        desc: { zh: "实时同传自动跟话，支持抢话打断；直播加字幕只需一条 OBS 链接。", en: "Real-time interpretation with barge-in; live subtitles are one OBS URL away." },
      },
    ],
    faq: [
      {
        q: { zh: "延迟有多大？会不会抢拍？", en: "How much latency?" },
        a: {
          zh: "首包亚秒级，正常语速对话自然跟话；支持抢话打断（barge-in），插话时引擎立刻让位，更像真人翻译。",
          en: "Sub-second first packet keeps a natural pace, and barge-in support means the engine yields instantly when someone cuts in — like a human interpreter.",
        },
      },
      {
        q: { zh: "支持哪些语言？", en: "Which languages?" },
        a: {
          zh: "中、英、日、韩等 10 语种双向互译，同一克隆音色跨语种输出。",
          en: "Ten languages including ZH/EN/JA/KO, both directions, all in your one cloned voice.",
        },
      },
      {
        q: { zh: "能用在电话和会议软件里吗？", en: "Does it work with calls and meeting apps?" },
        a: {
          zh: "可以。支持电话桥接与通话套餐，会议软件走虚拟声卡接入；直播场景还能同步输出双语字幕。",
          en: "Yes — phone bridging with a call package, meeting apps via virtual audio, and live streams get bilingual subtitles on top.",
        },
      },
    ],
    finalCta: {
      title: { zh: "带上你的术语表来试", en: "Bring your glossary and try it" },
      desc: {
        zh: "30 分钟真机演示：用你的声音、你的行业词表现场双向同传，数据全程不出机房。",
        en: "30-minute live demo: two-way interpreting with your voice and your term list — data never leaves the room.",
      },
    },
  },

  "asset-safe": {
    slug: "/asset-safe",
    productLine: { zh: "客户资产保险箱 · 数据主权", en: "Customer asset vault · data sovereignty" },
    seo: {
      title: {
        zh: "客户资产保险箱 · 跨渠道客户档案本地化 | 无界科技",
        en: "Customer Asset Vault · Cross-channel CRM, fully on-prem | BOUNDLESS",
      },
      description: {
        zh: "Telegram / WhatsApp / LINE / Messenger 客户资产沉到你自己的机器：Contact360 跨渠道全景（消息 + 互动时间轴 + 关系演进），多渠道身份合并/拆分，联系人一键 CSV 导出。本地 SQLite 存储，账号没了客户档案还在。",
        en: "Your Telegram / WhatsApp / LINE / Messenger customer assets live on your own hardware: Contact360 cross-channel view (messages + interaction timeline + relationship stages), identity merge/split, one-click contacts CSV export. Local SQLite — lose an account, keep the customer file.",
      },
      keywords: ["客户资产", "私域流量", "客户数据本地化", "跨渠道CRM", "客户档案", "Contact360", "数据主权", "customer data ownership"],
    },
    hero: {
      title: { zh: "平台账号会没，", en: "Platform accounts come and go —" },
      accent: { zh: "客户资产必须是你的", en: "your customer assets must stay yours" },
      subtitle: {
        zh: "每一条消息、每一次互动、每一段关系演进，都归集成跨渠道客户档案，落在你自己机器的本地库里（SQLite，随时可备份可直读）。账号受限或换设备，客户是谁、聊到哪、答应过什么——档案都在。",
        en: "Every message, interaction and relationship stage rolls up into one cross-channel customer file, stored in a local database on your own machine (SQLite — back it up or read it directly, any time). If an account gets restricted or you switch devices, who the customer is, where the conversation stood and what was promised — the file survives.",
      },
      points: [
        { zh: "Contact360 跨渠道全景 · 消息/互动/关系一屏", en: "Contact360 cross-channel view · messages / timeline / stages" },
        { zh: "本地 SQLite 存储 · 不经第三方服务器", en: "Local SQLite storage · no third-party servers" },
        { zh: "联系人一键 CSV 导出 · 合并/拆分有审计", en: "One-click contacts CSV export · audited merge/split" },
      ],
      demoCta: { zh: "看它到底存了什么", en: "See what it actually stores" },
    },
    demo: {
      title: { zh: "不是概念，后台里现成的", en: "Not a concept — it's in the console today" },
      subtitle: {
        zh: "以下每一条都能在 30 分钟真机演示里当场点给你看。",
        en: "Every line below can be shown live in a 30-minute demo.",
      },
      realNote: {
        zh: "以上为已上线能力的如实清单；标注「路线图」的功能以交付沟通为准，不含糊承诺。",
        en: "An honest list of shipped capabilities; anything marked roadmap is agreed at delivery, never hand-waved.",
      },
    },
    demoBullets: [
      {
        zh: "打开任一客户：跨渠道消息、互动时间轴、关系阶段演进一屏看全（Contact360）",
        en: "Open any customer: cross-channel messages, interaction timeline and relationship stages on one screen (Contact360)",
      },
      {
        zh: "同一客户在多平台的身份可合并成一份档案，合并错了可拆分回滚",
        en: "Multi-platform identities merge into one file — and can be split back if merged wrong",
      },
      {
        zh: "联系人列表一键导出 CSV，Excel 直接打开",
        en: "Contacts list exports to CSV in one click, opens straight in Excel",
      },
      {
        zh: "数据落在你机器的本地 SQLite 库（contacts / inbox），标准格式，随时整库备份",
        en: "Data lives in local SQLite files (contacts / inbox) on your machine — standard format, back up the whole thing any time",
      },
    ],
    caps: [
      {
        title: { zh: "Contact360 跨渠道客户全景", en: "Contact360 cross-channel view" },
        desc: {
          zh: "以客户为中心归集各渠道消息与互动：时间轴、关系阶段演进、备注画像一处看全，接手的人 5 分钟进入状态。",
          en: "Customer-centric rollup of every channel: timeline, relationship-stage history and profile notes in one place — a new agent is up to speed in five minutes.",
        },
        proof: { zh: "消息 + 互动 + 关系演进 一屏", en: "Messages + timeline + stages, one screen" },
      },
      {
        title: { zh: "数据主权：本地库存储", en: "Data sovereignty: local storage" },
        desc: {
          zh: "客户档案与会话落在你自己部署的 SQLite 本地库，不经我们服务器；标准格式可直读、可整库备份迁移。",
          en: "Customer files and conversations live in SQLite on your own deployment — never on our servers. Standard format: read it directly, back it up, move it.",
        },
        proof: { zh: "本地 SQLite · 可直读可备份", en: "Local SQLite · readable & backupable" },
      },
      {
        title: { zh: "多渠道身份合并 / 拆分", en: "Identity merge / split" },
        desc: {
          zh: "同一个客户的 Telegram、WhatsApp、LINE 身份合成一份档案；合并有审计、可拆分，不怕手滑。",
          en: "One customer's Telegram, WhatsApp and LINE identities become a single file; merges are audited and reversible.",
        },
        proof: { zh: "合并可回退 · 操作有审计", en: "Reversible merges · full audit" },
      },
      {
        title: { zh: "资产可带走：CSV 导出", en: "Take it with you: CSV export" },
        desc: {
          zh: "联系人清单一键导出 CSV。单客户完整档案 JSON 与消息批量导出在路线图上；交付即有的本地库可用标准 SQLite 工具直读。",
          en: "Contacts export to CSV in one click. Per-customer JSON and bulk message export are on the roadmap; the local database is readable with standard SQLite tools from day one.",
        },
        proof: { zh: "CSV 现成 · 库文件标准格式", en: "CSV today · standard DB format" },
      },
    ],
    steps: [
      {
        title: { zh: "接入你的渠道", en: "Connect your channels" },
        desc: { zh: "Telegram / WhatsApp / LINE / Messenger 接入后，收发消息自动进本地库。", en: "Once Telegram / WhatsApp / LINE / Messenger are connected, every message lands in the local DB automatically." },
      },
      {
        title: { zh: "档案自动归集", en: "Files build themselves" },
        desc: { zh: "消息、互动、关系阶段自动归到客户名下；多渠道身份可合并成一人。", en: "Messages, interactions and stages roll up per customer; multi-channel identities merge into one person." },
      },
      {
        title: { zh: "随时导出 / 备份", en: "Export / back up any time" },
        desc: { zh: "联系人 CSV 一键导出；本地库文件整库备份，资产始终在你手里。", en: "One-click contacts CSV; back up the database files wholesale — the assets never leave your hands." },
      },
    ],
    faq: [
      {
        q: { zh: "数据到底存在哪里？你们能看到吗？", en: "Where exactly is the data? Can you see it?" },
        a: {
          zh: "存在你自己部署环境的本地 SQLite 库里（客户档案与收件箱各一个库文件）。系统本地部署时数据不经我们的服务器，我们也没有访问通道。",
          en: "In local SQLite files inside your own deployment (one for customer files, one for the inbox). With on-prem deployment the data never touches our servers, and we have no access path to it.",
        },
      },
      {
        q: { zh: "平台账号被封，客户资产还能保住吗？", en: "If a platform account gets banned, do I keep the assets?" },
        a: {
          zh: "能。档案、消息记录、关系阶段都在你的本地库里，不随平台账号消失。新账号接回后可以人工对照档案续联——目前不提供自动「换号续聊」，这点我们如实说。",
          en: "Yes. Files, message history and relationship stages live in your local database — they don't vanish with the account. A new account can pick up the relationship manually using the file. We don't currently offer automatic account-switch continuation, and we say so plainly.",
        },
      },
      {
        q: { zh: "能导出哪些东西？", en: "What can I export?" },
        a: {
          zh: "联系人清单 CSV 一键导出已内置。单客户完整档案 JSON、消息批量导出在路线图上；等不及的话，本地 SQLite 库是标准格式，任何 SQLite 工具都能直读。",
          en: "One-click contacts CSV is built in. Per-customer JSON and bulk message export are on the roadmap; if you can't wait, the local SQLite database is a standard format any SQLite tool can read.",
        },
      },
    ],
    finalCta: {
      title: { zh: "让客户资产落到你自己的硬盘上", en: "Put your customer assets on your own disk" },
      desc: {
        zh: "30 分钟真机演示：现场看 Contact360 全景、合并拆分与 CSV 导出，指给你看数据库文件就在你机器上。",
        en: "30-minute live demo: Contact360 in action, merge/split, CSV export — and we point at the database files sitting on your machine.",
      },
    },
  },

  nurture: {
    slug: "/nurture",
    productLine: { zh: "养号模式 · 反封号工程", en: "Nurture mode · anti-ban engineering" },
    seo: {
      title: {
        zh: "养号模式 · 预热爬坡 / 健康红绿灯 / 一键急停 | 无界科技",
        en: "Account Nurture Mode · Warm-up ramp / health lights / kill-switch | BOUNDLESS",
      },
      description: {
        zh: "把养号做成工程：新号预热爬坡（默认第 0 天日发 2 条、14 天爬到 15 条）、账号健康红绿灯、红灯直接拒发、三级一键急停（TTL 自动恢复）、金丝雀白名单放量（默认关）、代理与指纹注入。一键切「养号模式」预设档。",
        en: "Account nurturing as engineering: warm-up ramp for new accounts (default 2 sends on day 0, ramping to 15 over 14 days), health traffic lights with hard-stop on red, a three-level kill-switch with TTL auto-recovery, canary whitelist rollout (off by default), proxy & fingerprint injection. One click switches the whole preset.",
      },
      keywords: ["养号", "防封号", "账号预热", "风控", "金丝雀发布", "账号健康", "anti-ban", "account warmup"],
    },
    hero: {
      title: { zh: "别再靠感觉养号，", en: "Stop nurturing accounts by gut feel —" },
      accent: { zh: "把风控做成工程", en: "make risk control an engineering system" },
      subtitle: {
        zh: "新号自动从每天 2 条起步、14 天爬到目标量；账号健康算成红黄绿灯，红灯直接拒发；出事一键急停（全局/平台/单号三级，TTL 到点自动恢复）；放量走金丝雀白名单，绿灯稳定才扩面。每一条都是线上跑着的真实闸门，不是 PPT。",
        en: "New accounts start at 2 sends a day and ramp to target over 14 days; account health becomes a red/amber/green light, and red refuses to send; one click freezes everything (global / platform / account, with TTL auto-recovery); volume ramps through a canary whitelist that only expands on stable green. Every one of these is a real gate running in production — not a slide.",
      },
      points: [
        { zh: "预热爬坡：默认 2 条/天 → 14 天到 15 条/天（可配）", en: "Warm-up ramp: default 2/day → 15/day over 14 days (configurable)" },
        { zh: "健康红绿灯 · 红灯拒发 · 封号信号自动停发", en: "Health lights · red refuses to send · ban signals auto-freeze" },
        { zh: "三级一键急停 + 金丝雀白名单放量（默认关）", en: "3-level kill-switch + canary whitelist rollout (off by default)" },
      ],
      demoCta: { zh: "看看闸门长什么样", en: "See the actual gates" },
    },
    demo: {
      title: { zh: "闸门是真的，后台里点得到", en: "Real gates you can click in the console" },
      subtitle: {
        zh: "以下全部来自主仓真实配置与运营面板，演示时逐条点给你看。",
        en: "Everything below comes from real configuration and live ops panels — we click through each in the demo.",
      },
      realNote: {
        zh: "参数为出厂默认值（target_cap=15 / warmup_start_cap=2 / warmup_ramp_days=14），均可按你的盘子调整；金丝雀默认关闭，由运营显式开启。",
        en: "Figures are factory defaults (target_cap=15 / warmup_start_cap=2 / warmup_ramp_days=14), all tunable to your fleet; canary is off by default and enabled explicitly by ops.",
      },
    },
    demoBullets: [
      {
        zh: "新号第 0 天日发上限 2 条，14 天线性爬到 15 条——超限当天直接拒发，不靠自觉",
        en: "A new account caps at 2 sends on day 0 and ramps to 15 over 14 days — over the cap, sending is refused outright",
      },
      {
        zh: "机群健康看板：活跃 / 预热中 / 受限封禁 红黄绿灯一屏，取最差账号亮总灯",
        en: "Fleet health board: active / warming / restricted at a glance, overall light = worst account",
      },
      {
        zh: "一键急停面板：global / platform / account 三级冻结，可带 TTL 到点自动恢复",
        en: "Kill-switch panel: freeze at global / platform / account level, optional TTL auto-recovery",
      },
      {
        zh: "金丝雀放量：启用后白名单外账号一律 hold，绿灯稳定才逐步扩面（默认关闭）",
        en: "Canary rollout: when enabled, accounts outside the whitelist are held; expansion only on stable green (off by default)",
      },
      {
        zh: "「养号模式」一键预设：预热闸 + 金丝雀白名单 + 只演练不真发，随时可回滚",
        en: "One-click \"nurture mode\" preset: warm-up gate + canary whitelist + dry-run only, fully reversible",
      },
    ],
    caps: [
      {
        title: { zh: "预热爬坡 · 新号不猛冲", en: "Warm-up ramp · no day-one blasting" },
        desc: {
          zh: "按账号天龄自动算当日上限：默认从 2 条/天线性爬到 15 条/天（14 天），全部可配；超限拒发而不是提醒。",
          en: "Daily caps computed from account age: default ramp from 2/day to 15/day over 14 days, fully configurable — and over-cap means refusal, not a reminder.",
        },
        proof: { zh: "2 → 15 条/天 · 14 天 · 超限拒发", en: "2 → 15/day · 14 days · hard cap" },
      },
      {
        title: { zh: "健康红绿灯 · 红灯拒发", en: "Health lights · red refuses" },
        desc: {
          zh: "天龄、代理、当日发送量、熔断信号算成健康分与红黄绿灯；红灯账号直接拒发，封号信号触发自动停发。",
          en: "Account age, proxy, daily volume and circuit-breaker signals roll into a health score and light; red accounts are refused, and ban signals trigger an automatic freeze.",
        },
        proof: { zh: "红灯 = 拒发 · 封号信号自动停", en: "Red = refuse · ban signal auto-freeze" },
      },
      {
        title: { zh: "三级一键急停 Kill-Switch", en: "Three-level kill-switch" },
        desc: {
          zh: "出事毫秒级冻结自动发送：全局 / 单平台 / 单账号三级作用域，重启不丢，可设 TTL 到点自动恢复，防「停了忘开」。",
          en: "Freeze automated sending in milliseconds: global / platform / account scopes, survives restarts, optional TTL auto-recovery so a stop is never forgotten.",
        },
        proof: { zh: "三级作用域 · TTL 自动恢复", en: "3 scopes · TTL auto-recovery" },
      },
      {
        title: { zh: "金丝雀放量 + 一键预设档", en: "Canary rollout + one-click presets" },
        desc: {
          zh: "放量先走白名单：启用金丝雀后白名单外一律 hold，绿灯稳定再扩面（默认关闭，运营显式开启）。「养号模式」预设一键把预热闸、金丝雀、只演练不真发整套摆好，可回滚。",
          en: "Ramp through a whitelist: with canary on, everything outside it is held until green light holds (off by default, explicitly enabled by ops). The \"nurture mode\" preset arms warm-up gate + canary + dry-run in one click, reversibly.",
        },
        proof: { zh: "白名单放量 · 预设可回滚", en: "Whitelist rollout · reversible preset" },
      },
    ],
    steps: [
      {
        title: { zh: "新号进预热档", en: "New accounts enter warm-up" },
        desc: { zh: "一键切「养号模式」：预热闸开、金丝雀白名单武装好、主动触达只演练不真发。", en: "One click into nurture mode: warm-up gate on, canary whitelist armed, outreach in dry-run only." },
      },
      {
        title: { zh: "看灯养号", en: "Nurture by the lights" },
        desc: { zh: "机群看板盯红黄绿灯与预热进度；红灯自动拒发，异常一键急停。", en: "Watch the fleet board: lights and ramp progress. Red refuses automatically; anomalies get the kill-switch." },
      },
      {
        title: { zh: "绿灯稳定再放量", en: "Ramp on stable green" },
        desc: { zh: "把养熟的号加进金丝雀白名单逐步放量，爆炸半径始终受控。", en: "Add matured accounts to the canary whitelist and expand gradually — the blast radius stays contained." },
      },
    ],
    faq: [
      {
        q: { zh: "金丝雀放量默认就开吗？", en: "Is canary rollout on by default?" },
        a: {
          zh: "默认关闭。启用后白名单外账号一律 hold（白名单为空时最保守=全部 hold），所以要由运营显式开启并指定白名单。「养号模式」预设会替你把它武装到 manual 白名单模式。",
          en: "No — off by default. Once enabled, accounts outside the whitelist are held (an empty whitelist means everything is held, the most conservative stance), so ops enables it explicitly and pins the whitelist. The nurture preset arms it in manual whitelist mode for you.",
        },
      },
      {
        q: { zh: "开了这套就保证不封号吗？", en: "Does this guarantee no bans?" },
        a: {
          zh: "不保证，也没人能保证。这套是把「猛冲、红灯硬发、出事停不下来」这类高危动作工程化地挡住，把风险显著压低；号的内容与经营方式仍然是第一位的。",
          en: "No — and nobody can honestly guarantee that. What this does is engineer away the high-risk behaviors (day-one blasting, sending through red lights, no e-stop when things go wrong) and cut the risk substantially; what you send and how you operate still matter most.",
        },
      },
      {
        q: { zh: "预热参数是死的吗？", en: "Are the warm-up numbers fixed?" },
        a: {
          zh: "不是。日发目标、起步上限、爬坡天数、红灯是否硬拒全部可配；出厂默认 2 条/天起步、14 天爬到 15 条/天，是我们实际在用的保守档。",
          en: "No. Target volume, starting cap, ramp days and hard-stop-on-red are all configurable; the factory default (start at 2/day, ramp to 15/day over 14 days) is the conservative profile we run ourselves.",
        },
      },
      {
        q: { zh: "代理和设备指纹也管吗？", en: "What about proxies and fingerprints?" },
        a: {
          zh: "管。账号注册表按号绑定代理，桌面端注入设备指纹，健康分里代理是否配置也计分；这些同样是已上线能力。",
          en: "Yes. The account registry binds a proxy per account, the desktop shell injects device fingerprints, and proxy hygiene feeds the health score — all shipped capabilities.",
        },
      },
    ],
    finalCta: {
      title: { zh: "带一批新号来，现场进预热档", en: "Bring a batch of fresh accounts — we arm them live" },
      desc: {
        zh: "30 分钟真机演示：一键切养号模式、看板看灯、试一次急停与恢复，全程在你的部署上操作。",
        en: "30-minute live demo on your deployment: switch to nurture mode, read the fleet lights, fire and lift a kill-switch — all hands-on.",
      },
    },
  },
};

// 落地页真实媒体（与 public/showcase/real/ 对应）
export const LANDING_MEDIA = {
  voiceClips: [
    { label: { zh: "中文", en: "Chinese" }, src: "/showcase/real/voice-zh.mp3" },
    { label: { zh: "English", en: "English" }, src: "/showcase/real/voice-en.mp3" },
    { label: { zh: "日本語", en: "Japanese" }, src: "/showcase/real/voice-ja.mp3" },
  ],
  interpPair: {
    src: { label: { zh: "中文原声（克隆音）", en: "Chinese source (cloned voice)" }, file: "/showcase/real/interp-src-zh.mp3" },
    out: { label: { zh: "英文同传（同一音色）", en: "English output (same voice)" }, file: "/showcase/real/interp-out-en.mp3" },
  },
  faceSwap: {
    before: "/showcase/live-before.png",
    after: "/showcase/live-after.png",
  },
  dhVideoZh: { src: "/showcase/real/digital-human.mp4", poster: "/showcase/real/digital-human-poster.png" },
  dhVideoEn: { src: "/showcase/real/digital-human-en.mp4", poster: "/showcase/real/digital-human-en-poster.jpg" },
};
