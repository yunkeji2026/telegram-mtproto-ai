// 无界底座「实时数字人引擎」新能力 & 真实案例内容（独立于 content.ts，避免污染巨型 Dict）。
// 数据取自引擎侧 capability_matrix.json / 销售一页纸 实测口径：每条卖点都对应真实代码与实测数字，
// 营销措辞可以强，但底层能力必须真——这样「预约真机演示」时不翻车。
export type EngLang = "zh" | "en";

export interface CapItem {
  icon: string; // lucide 图标键，在组件里映射
  line: { zh: string; en: string }; // 所属产品线
  title: { zh: string; en: string };
  desc: { zh: string; en: string };
  proof: { zh: string; en: string }; // 实测/代码证据，鼠标可见的"硬指标"
  badge?: { zh: string; en: string }; // 全新 / 实测 / 独家
}

export interface ProofLayer {
  icon: string;
  title: { zh: string; en: string };
  desc: { zh: string; en: string };
}

export interface EngineDict {
  caps: {
    kicker: { zh: string; en: string };
    title: { zh: string; en: string };
    subtitle: { zh: string; en: string };
    items: CapItem[];
    footnote: { zh: string; en: string };
  };
  proof: {
    kicker: { zh: string; en: string };
    title: { zh: string; en: string };
    subtitle: { zh: string; en: string };
    layers: ProofLayer[];
    metricsTitle: { zh: string; en: string };
    metrics: { value: string; label: { zh: string; en: string } }[];
    metricsNote: { zh: string; en: string };
    // 真实成片画廊
    galleryTitle: { zh: string; en: string };
    gallerySubtitle: { zh: string; en: string };
    audioTitle: { zh: string; en: string };
    audioDesc: { zh: string; en: string };
    audioClips: { label: { zh: string; en: string }; src: string }[];
    videoTitle: { zh: string; en: string };
    videoDesc: { zh: string; en: string };
    videoSrc: string;
    videoPoster: string;
    swapTitle: { zh: string; en: string };
    swapDesc: { zh: string; en: string };
    swapBefore: string;
    swapAfter: string;
    beforeLabel: { zh: string; en: string };
    afterLabel: { zh: string; en: string };
    dragHint: { zh: string; en: string };
    mediaRealNote: { zh: string; en: string };
    mediaPending: { zh: string; en: string };
    // 一键验真
    verifyTitle: { zh: string; en: string };
    verifyDesc: { zh: string; en: string };
    verifyPoints: { zh: string; en: string }[];
    // 真机演示
    liveTitle: { zh: string; en: string };
    liveDesc: { zh: string; en: string };
    livePoints: { zh: string; en: string }[];
    liveCta: { zh: string; en: string };
    // 官方频道真实案例
    feedTitle: { zh: string; en: string };
    feedDesc: { zh: string; en: string };
    feedCta: { zh: string; en: string };
    disclaimer: { zh: string; en: string };
  };
}

export const ENGINE: EngineDict = {
  caps: {
    kicker: { zh: "本周期新增能力", en: "New this cycle" },
    title: {
      zh: "数字人引擎 · 十二项硬核能力全面升级",
      en: "Digital-Human Engine · 12 hardcore upgrades",
    },
    subtitle: {
      zh: "全部本地部署、数据不出机房。每一条都对应真实引擎代码与实测数字——不是 PPT 参数，是能当场跑给你看的能力。",
      en: "All private-deployed, data stays in your racks. Every line maps to real engine code and measured numbers — not slideware, but capabilities we can run live for you.",
    },
    footnote: {
      zh: "指标随硬件浮动，以上为引擎实测区间（换脸机 RTX 4070 / 数字人主机 RTX 5090 32G）。完整能力↔证据对照见交付文档。",
      en: "Numbers scale with hardware; ranges above are measured on the engine (face-swap box RTX 4070 / digital-human host RTX 5090 32G). Full capability-to-evidence matrix ships with delivery docs.",
    },
    items: [
      {
        icon: "mic",
        line: { zh: "幻声 VoiceX", en: "VoiceX" },
        title: { zh: "三引擎克隆音 · 自动择优", en: "Tri-engine cloned voice · auto-pick" },
        desc: {
          zh: "几十秒样本零样本克隆你的音色。Fish 实时 / Qwen3 首包极速 10 语种 / VoxCPM 48kHz 可商用——克隆完引擎自动推荐最合适的引擎。",
          en: "Zero-shot clone from a few seconds of audio. Fish real-time / Qwen3 ultra-fast first-packet across 10 languages / VoxCPM 48kHz commercial — the engine auto-recommends the best one after cloning.",
        },
        proof: { zh: "Qwen3 首包 ≈97ms · 3 秒克隆 · 10 语种", en: "Qwen3 first-packet ≈97ms · 3s clone · 10 languages" },
        badge: { zh: "升级", en: "Upgraded" },
      },
      {
        icon: "scanface",
        line: { zh: "幻影 LiveX", en: "LiveX" },
        title: { zh: "高清活体数字人", en: "HD living digital human" },
        desc: {
          zh: "会眨眼、会摆头、有微表情的活体分身，不是死图对口型。克隆形象 + 克隆声 + 口型同步，一路直推 WebRTC / OBS。",
          en: "A living twin that blinks, turns its head and emotes — not a still photo lip-syncing. Cloned face + cloned voice + lip-sync, streamed straight to WebRTC / OBS.",
        },
        proof: { zh: "5090 上 25fps 高清 · 亚秒级首帧 ≈0.9s", en: "25fps HD on 5090 · sub-second first frame ≈0.9s" },
        badge: { zh: "旗舰", en: "Flagship" },
      },
      {
        icon: "sparkles",
        line: { zh: "幻影 LiveX", en: "LiveX" },
        title: { zh: "实时换脸 · 脸区原生通道", en: "Live face swap · native face channel" },
        desc: {
          zh: "脸部走原生高分辨率通道，清晰度实测 4.5× 提升。720p 高清脸默认，1080p 超清档随选；显卡吃紧自动降档，绝不卡成 PPT。",
          en: "Faces run through a native high-res channel — measured 4.5× sharper. 720p HD default, 1080p ultra on demand; auto-downshifts under GPU pressure so it never stutters.",
        },
        proof: { zh: "1080p 超清并发均值 ≈323ms · 脸区清晰度 4.5×", en: "1080p ultra concurrent avg ≈323ms · 4.5× face clarity" },
      },
      {
        icon: "palette",
        line: { zh: "幻影 LiveX", en: "LiveX" },
        title: { zh: "直播实时虚拟背景 / 绿幕", en: "Live virtual background / green screen" },
        desc: {
          zh: "不换房间就换场景：虚化 / 图片 / 绿幕一键切换，CPU 抠像不占显卡预算，直播中热切换毫无卡顿。",
          en: "Change the scene without changing rooms: blur / image / green-screen in one tap. CPU-side matting spends zero GPU budget and hot-swaps live without a hitch.",
        },
        proof: { zh: "抠像 ≈5ms/帧@720p · CPU 实现零显存占用", en: "Matting ≈5ms/frame@720p · CPU, zero VRAM" },
        badge: { zh: "全新", en: "New" },
      },
      {
        icon: "users",
        line: { zh: "幻影 LiveX", en: "LiveX" },
        title: { zh: "双人同框 · 各换各脸", en: "Two-in-frame · each swapped" },
        desc: {
          zh: "访谈、连麦、播客双人同框，左右槽位各换各脸，出现第三张脸自动回退不穿帮。",
          en: "Interviews, co-streams and podcasts with two people in frame — left/right slots each get their own swap, and a third face auto-falls back cleanly.",
        },
        proof: { zh: "face_map 双槽位映射 · 第三人自动回退", en: "face_map dual-slot mapping · third-face auto fallback" },
        badge: { zh: "全新", en: "New" },
      },
      {
        icon: "languages",
        line: { zh: "通译 LingoX", en: "LingoX" },
        title: { zh: "克隆音同传 · 术语锁定", en: "Cloned-voice interpreting · term lock" },
        desc: {
          zh: "用你自己的克隆声做双向同传，术语表锁定专有名词（美团永远 Meituan），支持抢话打断，一键开通话套餐。",
          en: "Two-way interpreting in your own cloned voice, a glossary that locks proper nouns (Meituan stays Meituan), barge-in interruption, and a one-tap call package.",
        },
        proof: { zh: "术语表 + TM 缓存 · barge-in 抢话打断", en: "Glossary + TM cache · barge-in interruption" },
      },
      {
        icon: "subtitles",
        line: { zh: "通译 LingoX", en: "LingoX" },
        title: { zh: "OBS 直播双语字幕", en: "OBS live bilingual subtitles" },
        desc: {
          zh: "OBS 浏览器源里拖一个链接，直播间即出实时双语字幕，散场一键导出 SRT——观众听不懂也跟得上。",
          en: "Drop one URL into an OBS Browser Source and your stream gets real-time bilingual subtitles, exportable to SRT afterwards — viewers keep up even across languages.",
        },
        proof: { zh: "SSE 实时推送 · 一键导出 SRT", en: "SSE live push · one-click SRT export" },
        badge: { zh: "全新", en: "New" },
      },
      {
        icon: "gauge",
        line: { zh: "平台能力", en: "Platform" },
        title: { zh: "开播前 3 秒设备体检", en: "3-second pre-broadcast device check" },
        desc: {
          zh: "开播前给麦克风信噪比、摄像头脸占比、虚拟声卡打 0–100 分，红灯先给一句话修法再放行——把直播事故挡在开播前。",
          en: "Before you go live it scores mic SNR, camera face-ratio and virtual audio 0–100; a red light shows a one-line fix before it lets you start — accidents blocked before broadcast.",
        },
        proof: { zh: "麦 SNR + 摄像头 + 虚拟声卡 0–100 分", en: "Mic SNR + camera + virtual audio, 0–100 score" },
        badge: { zh: "全新", en: "New" },
      },
      {
        icon: "brain",
        line: { zh: "数字人对话", en: "Conversation" },
        title: { zh: "有记忆 · 懂情绪的对话大脑", en: "A brain that remembers & reads emotion" },
        desc: {
          zh: "跨会话长期记忆记住每位客户，共情自适应读懂情绪调整语气，混合 RAG 检索带引用脚注，答得准、聊得像真人。",
          en: "Cross-session long-term memory that remembers each customer, empathy adaptation that reads mood and shifts tone, and hybrid RAG with cited footnotes — accurate answers that feel human.",
        },
        proof: { zh: "长期记忆 + 情绪轨迹 + BM25/语义 RRF 融合", en: "Long-term memory + mood trajectory + BM25/semantic RRF" },
      },
      {
        icon: "palette2",
        line: { zh: "幻颜 FaceX", en: "FaceX" },
        title: { zh: "开播前 AI 定妆 / 换发型", en: "Pre-broadcast AI hairstyle & makeup" },
        desc: {
          zh: "开播前换个发型生成一张定妆脸写入角色，整场直播生效，实时链路零额外开销——换风格不换算力。",
          en: "Restyle the hair before you start, bake a look into the character and it holds for the whole stream with zero real-time overhead — new style, same compute.",
        },
        proof: { zh: "HairFastGAN 离线定妆 · 实时链零开销", en: "HairFastGAN offline preset · zero real-time cost" },
      },
      {
        icon: "image",
        line: { zh: "幻颜 FaceX", en: "FaceX" },
        title: { zh: "图片 / 视频换脸精修", en: "Image / video face swap, refined" },
        desc: {
          zh: "成片级图片、视频换脸，inswapper + GFPGAN / CodeFormer 精修 + 光流时序平滑，三路并发池批量出片。",
          en: "Production-grade image and video swaps: inswapper + GFPGAN / CodeFormer refinement + optical-flow temporal smoothing, batched through a 3-way concurrency pool.",
        },
        proof: { zh: "并发聚合 GFPGAN 8.8fps / CodeFormer 5.5fps", en: "Concurrent GFPGAN 8.8fps / CodeFormer 5.5fps" },
      },
      {
        icon: "fingerprint",
        line: { zh: "平台能力", en: "Platform" },
        title: { zh: "合规可溯源 · 一键验真", en: "Compliant & verifiable output" },
        desc: {
          zh: "产出物默认嵌 C2PA 内容凭证 + Ed25519 签名 + 不可见水印，第三方可离线验真「是 AI 生成、由谁生成」。政企合规首选。",
          en: "Outputs carry C2PA content credentials + Ed25519 signatures + invisible watermark by default; any third party can verify offline that it's AI-generated and by whom — built for regulated buyers.",
        },
        proof: { zh: "C2PA + Ed25519 可对外验真 · 克隆伦理校验", en: "C2PA + Ed25519 externally verifiable · clone-ethics checks" },
      },
    ],
  },
  proof: {
    kicker: { zh: "眼见为实", en: "See it to believe it" },
    title: { zh: "五层「看得见」的真实证据", en: "Five layers of proof you can actually see" },
    subtitle: {
      zh: "别人放渲染图，我们给你能播、能验、能预约上手的真东西。想更狠？直接约一场真机演示，在你的硬件上跑给你看。",
      en: "Others show renders; we give you media you can play, output you can verify, and a live session you can book. Want the hard proof? Book a demo and we run it on your own hardware.",
    },
    layers: [
      { icon: "playcircle", title: { zh: "可交互 Demo", en: "Interactive demos" }, desc: { zh: "上方每块能力都配可点的实时演示", en: "Every capability above ships a clickable live demo" } },
      { icon: "waveform", title: { zh: "真实成片", en: "Real output" }, desc: { zh: "克隆音、换脸、口播成片可听可看", en: "Cloned audio, swaps and talking-heads to watch & hear" } },
      { icon: "badgecheck", title: { zh: "一键验真", en: "One-click verify" }, desc: { zh: "C2PA 凭证第三方可离线验证", en: "C2PA credentials any third party can verify" } },
      { icon: "calendar", title: { zh: "真机演示", en: "Live demo" }, desc: { zh: "预约在你的硬件上现场跑", en: "Book a session, run on your own hardware" } },
      { icon: "send", title: { zh: "每日案例", en: "Daily cases" }, desc: { zh: "官方频道持续更新真实成片", en: "Official channel posts real output daily" } },
    ],
    metricsTitle: { zh: "实测硬指标（引用区间，绝不编单点数）", en: "Measured numbers (ranges, never a single made-up figure)" },
    metrics: [
      { value: "25fps", label: { zh: "高清活体数字人 (5090)", en: "HD living human (5090)" } },
      { value: "≈0.9s", label: { zh: "数字人首帧", en: "Digital-human first frame" } },
      { value: "≈150ms", label: { zh: "对话首音 (命中缓存)", en: "First reply audio (cached)" } },
      { value: "4.5×", label: { zh: "换脸脸区清晰度", en: "Face-region clarity" } },
      { value: "≈97ms", label: { zh: "Qwen3 语音首包", en: "Qwen3 voice first-packet" } },
      { value: "10", label: { zh: "克隆音支持语种", en: "Cloning languages" } },
    ],
    metricsNote: {
      zh: "以上为引擎实测区间，随部署硬件浮动；运行时接口 /api/hardware/guide 会按你的显卡给出每功能推荐档位。",
      en: "Measured ranges that scale with hardware; the runtime /api/hardware/guide endpoint gives per-feature tiers for your exact GPU.",
    },
    galleryTitle: { zh: "真实成片画廊", en: "Real output gallery" },
    gallerySubtitle: {
      zh: "下面是引擎真实产出的样片。看得见的效果，加一枚看不见但可验证的水印。",
      en: "Below are real samples produced by the engine — visible quality, plus an invisible-but-verifiable watermark.",
    },
    audioTitle: { zh: "克隆音 · 多语种试听", en: "Cloned voice · multilingual" },
    audioDesc: { zh: "同一音色跨语种朗读，情感与语气自然连贯。点开即听——引擎真实产出、未剪辑。", en: "One voice across languages with natural prosody. Tap to listen — real engine output, unedited." },
    audioClips: [
      { label: { zh: "中文", en: "Chinese" }, src: "/showcase/real/voice-zh.mp3" },
      { label: { zh: "English", en: "English" }, src: "/showcase/real/voice-en.mp3" },
      { label: { zh: "日本語", en: "Japanese" }, src: "/showcase/real/voice-ja.mp3" },
    ],
    videoTitle: { zh: "高清活体数字人 · 口播成片", en: "HD digital human · talking head" },
    videoDesc: { zh: "克隆形象 + 克隆声 + 口型同步，会眨眼摆头的活体分身。本片为引擎真实产出。", en: "Cloned face + cloned voice + lip-sync — a living twin that blinks and moves. Real engine output." },
    videoSrc: "/showcase/real/digital-human.mp4",
    videoPoster: "/showcase/real/digital-human-poster.png",
    swapTitle: { zh: "实时换脸 · 前后对比", en: "Live face swap · before / after" },
    swapDesc: { zh: "拖动滑块看脸区原生通道带来的清晰度差异。", en: "Drag the slider to see the clarity the native face channel delivers." },
    swapBefore: "/showcase/live-before.png",
    swapAfter: "/showcase/live-after.png",
    beforeLabel: { zh: "原始", en: "Original" },
    afterLabel: { zh: "换脸后", en: "Swapped" },
    dragHint: { zh: "拖动查看前后", en: "Drag to compare" },
    mediaRealNote: {
      zh: "以上为引擎真实产出、未经剪辑；想用你自己的音色/形象试？点「预约真机演示」当场生成。",
      en: "Real, unedited engine output. Want it on your own voice / face? Book a live demo and we generate it on the spot.",
    },
    mediaPending: {
      zh: "样片按需提供 —— 点「预约真机演示」，我们用你的素材当场生成给你看。",
      en: "Samples on request — hit \u201cBook a live demo\u201d and we generate one from your material on the spot.",
    },
    verifyTitle: { zh: "不信？一键验真", en: "Don\u2019t trust us? Verify it" },
    verifyDesc: {
      zh: "我们产出的每一段音视频都嵌了 C2PA 内容凭证与 Ed25519 签名。别听我们说，你自己验——这是「合规可溯源」的底气。",
      en: "Every clip we produce embeds C2PA content credentials and an Ed25519 signature. Don\u2019t take our word — verify it yourself. That\u2019s what \u201ccompliant & traceable\u201d actually means.",
    },
    verifyPoints: [
      { zh: "C2PA 标准内容凭证嵌入", en: "C2PA standard content credentials" },
      { zh: "Ed25519 签名 · 第三方离线可验", en: "Ed25519 signature · verifiable offline" },
      { zh: "不可见水印 + AI 生成标识", en: "Invisible watermark + AI-made label" },
      { zh: "克隆伦理校验 · 拒绝未授权音色", en: "Clone-ethics checks · refuses unlicensed voices" },
    ],
    liveTitle: { zh: "最硬的证据：真机演示", en: "The hardest proof: a live demo" },
    liveDesc: {
      zh: "远程连你的机器、或用我们的样机，30 分钟把克隆音 / 换脸 / 数字人 / 同传当场跑一遍。可用你的素材、你的场景，数据全程不出你的机房。",
      en: "We connect to your machine or use ours and run cloned voice / face swap / digital human / interpreting live in 30 minutes — with your material, your scenario, data never leaving your racks.",
    },
    livePoints: [
      { zh: "用你的素材、你的场景现场跑", en: "Runs on your material and scenario, live" },
      { zh: "在你的硬件上验证真实帧率与延迟", en: "Verify real FPS and latency on your hardware" },
      { zh: "数据不出机房 · 演示后可留试用授权", en: "Data stays in-house · trial license after the demo" },
    ],
    liveCta: { zh: "预约真机演示", en: "Book a live demo" },
    feedTitle: { zh: "官方频道 · 每日真实案例", en: "Official channel · daily real cases" },
    feedDesc: {
      zh: "换脸直播、克隆音、数字人口播的真实成片与客户玩法，每天在官方频道更新。先看片，再决定。",
      en: "Real output and customer plays — live swaps, cloned voices, talking-heads — posted daily on the official channel. Watch first, decide later.",
    },
    feedCta: { zh: "进频道看真实成片", en: "See real output on the channel" },
    disclaimer: {
      zh: "样片与指标来自引擎实测与客户授权分享，随硬件与场景浮动，仅供参考，不构成对具体效果的承诺；所有演示素材均须合法授权。",
      en: "Samples and figures come from engine measurements and consented client shares; they vary by hardware and scenario, are for reference only, and are not a guarantee of specific results. All demo material must be lawfully licensed.",
    },
  },
};
