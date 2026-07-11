// 三条产品线独立落地页内容（/voice /face /interpreting + /en/*）。
// 设计原则：一页一卖点，媒体证据前置（真实引擎产出），CTA 直达 Telegram。
// 指标口径与 engineContent.ts 保持一致——营销可以强，数字必须真。

export type LandingKey = "voice" | "face" | "interpreting";

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
  };
  demo: {
    title: L;
    subtitle: L;
    realNote: L; // “真实产出”说明
  };
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
