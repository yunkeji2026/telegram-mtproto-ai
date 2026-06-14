export type Lang = "zh" | "en";

export interface PricingRow {
  plan: string;
  price: string;
  detail: string;
}

export interface Solution {
  id: string;
  tag: string;
  title: string;
  desc: string;
  features: string[];
  pricing: PricingRow[];
  highlight?: boolean;
}

export interface Plan {
  name: string;
  priceMonthly: string;
  priceYearly: string;
  desc: string;
  features: string[];
  highlight?: boolean;
}

export interface Dict {
  nav: {
    solutions: string;
    demo: string;
    autochat: string;
    cases: string;
    engage: string;
    pricing: string;
    about: string;
    contact: string;
    cta: string;
  };
  hero: {
    badge: string;
    title: string;
    titleAccent: string;
    rotating: string[];
    subtitle: string;
    trustline: string;
    ctaPrimary: string;
    ctaSecondary: string;
    stats: { value: string; label: string }[];
  };
  trust: {
    platformsLabel: string;
    platforms: string[];
    statsTitle: string;
    stats: { value: string; suffix: string; label: string }[];
    testimonialsTitle: string;
    testimonials: { quote: string; name: string; role: string }[];
    disclaimer: string;
  };
  plans: {
    title: string;
    subtitle: string;
    monthly: string;
    yearly: string;
    save: string;
    popular: string;
    perMonth: string;
    cta: string;
    items: Plan[];
  };
  orderSteps: {
    title: string;
    subtitle: string;
    steps: { title: string; desc: string }[];
  };
  faq: {
    title: string;
    subtitle: string;
    items: { q: string; a: string }[];
  };
  realtime: {
    badge: string;
    title: string;
    subtitle: string;
    videoNote: string;
    features: { icon: string; title: string; desc: string }[];
    stepsTitle: string;
    steps: { title: string; desc: string }[];
    hardwareTitle: string;
    hardwareNote: string;
    hardware: { tier: string; gpu: string; use: string }[];
    plansTitle: string;
    plansNote: string;
    plans: {
      name: string;
      tag?: string;
      price: string;
      unit: string;
      specs: string[];
      cta: string;
      highlight?: boolean;
    }[];
    extrasTitle: string;
    extras: string[];
    availability: string;
    capacityNote: string;
    cta: string;
  };
  faceswap: {
    badge: string;
    title: string;
    subtitle: string;
    tabCustom: string;
    tabTemplate: string;
    uploadFace: string;
    uploadFaceHint: string;
    uploadTarget: string;
    uploadTargetHint: string;
    pickTemplate: string;
    templates: { id: string; name: string; file: string }[];
    consent: string;
    button: string;
    processing: string;
    resultTitle: string;
    download: string;
    again: string;
    privacy: string;
    errConfig: string;
    errNoConsent: string;
    errNoImage: string;
    errSize: string;
    errGeneric: string;
  };
  solutionsSection: {
    title: string;
    subtitle: string;
  };
  solutions: Solution[];
  pricingSection: {
    title: string;
    subtitle: string;
    unit: string;
    note: string;
    planCol: string;
    priceCol: string;
    detailCol: string;
    allLabel: string;
  };
  about: {
    title: string;
    subtitle: string;
    points: { title: string; desc: string }[];
  };
  community: {
    badge: string;
    title: string;
    subtitle: string;
    perks: string[];
    cta: string;
    groupCta: string;
  };
  gate: {
    badge: string;
    title: string;
    subtitle: string;
    joinChannel: string;
    joinGroup: string;
    joinedChannel: string;
    joinedGroup: string;
    verify: string;
    checking: string;
    webNote: string;
    unlockedTitle: string;
    unlockedDesc: string;
    codeLabel: string;
    code: string;
    cta: string;
    notYet: string;
  };
  swap: {
    before: string;
    after: string;
    dragHint: string;
    liveTag: string;
    hudEngine: string;
    hudFps: string;
    hudLatency: string;
    callStatus: string;
    you: string;
    theySee: string;
    faceVoice: string;
    voiceCloning: string;
  };
  personas: {
    title: string;
    subtitle: string;
    items: { id: string; title: string; desc: string; cta: string; href: string }[];
  };
  showcaseSection: {
    title: string;
    subtitle: string;
  };
  chatDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    translatedTag: string;
    replyName: string;
    typing: string;
    messages: { name: string; flag: string; text: string; translated: string }[];
    reply: { text: string; translated: string };
  };
  voiceDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    original: string;
    cloned: string;
    langsLabel: string;
    langs: string[];
  };
  deployDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    cloudLabel: string;
    localLabel: string;
    rows: { label: string; cloud: boolean; local: boolean }[];
  };
  digitalDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    tags: string[];
  };
  compare: {
    badge: string;
    title: string;
    subtitle: string;
    cols: string[];
    rows: { label: string; us: string; them: string; manual: string }[];
  };
  autochat: {
    badge: string;
    title: string;
    subtitle: string;
    features: { icon: string; title: string; desc: string }[];
    compareTitle: string;
    compareNote: string;
    badLabel: string;
    goodLabel: string;
    compare: { src: string; bad: string; good: string }[];
    scenariosLabel: string;
    scenarios: string[];
    cta: string;
    demo: {
      inbox: string;
      personaName: string;
      translatedTag: string;
      autoTag: string;
      voiceTag: string;
      typing: string;
      incoming: { name: string; flag: string; text: string; translated: string };
      reply: { text: string; translated: string };
      voiceLen: string;
    };
  };
  engage: {
    badge: string;
    title: string;
    subtitle: string;
    selectorTitle: string;
    youLabel: string;
    weLabel: string;
    selector: { id: string; label: string }[];
    models: {
      id: string;
      badge: string;
      name: string;
      tagline: string;
      you: string;
      we: string;
      price: string;
      priceNote: string;
      points: string[];
      cta: string;
      highlight?: boolean;
    }[];
    serviceTiersLabel: string;
    extrasLabel: string;
    invest: {
      roiTitle: string;
      roiRows: { label: string; value: string }[];
      roiNote: string;
      flowTitle: string;
      flow: string[];
      compliance: string;
    };
    matrixTitle: string;
    matrixCols: string[];
    matrix: { label: string; a: string; b: string; c: string }[];
  };
  roi: {
    badge: string;
    title: string;
    subtitle: string;
    inputs: { agents: string; salary: string; leads: string; aov: string; conv: string };
    units: { agents: string; salary: string; leads: string; aov: string; conv: string };
    resultSaveLabel: string;
    resultRevenueLabel: string;
    resultNetLabel: string;
    resultRoiLabel: string;
    resultYearLabel: string;
    planLabel: string;
    perMonth: string;
    assumptionsTitle: string;
    assumptions: string[];
    disclaimer: string;
    cta: string;
  };
  cases: {
    badge: string;
    title: string;
    subtitle: string;
    items: { scene: string; metric: string; metricLabel: string; quote: string; name: string; role: string; img: string }[];
    galleryTitle: string;
    gallerySubtitle: string;
    translatedTag: string;
    replyTag: string;
    gallery: { lang: string; flag: string; incoming: string; translated: string; reply: string }[];
    disclaimer: string;
  };
  lead: {
    title: string;
    subtitle: string;
    name: string;
    contact: string;
    interest: string;
    message: string;
    namePh: string;
    contactPh: string;
    messagePh: string;
    interests: string[];
    submit: string;
    submitting: string;
    successTitle: string;
    successDesc: string;
    error: string;
    contactInvalid: string;
    privacy: string;
  };
  contact: {
    title: string;
    subtitle: string;
    telegram: string;
    telegramHandle: string;
    scanHint: string;
    usdt: string;
    usdtNote: string;
    networks: string;
    responseTime: string;
    compliance: string;
    complianceNote: string;
    cta: string;
  };
  footer: {
    rights: string;
    disclaimerTitle: string;
    disclaimer: string;
    links: string[];
  };
}

const zh: Dict = {
  nav: {
    solutions: "业务能力",
    demo: "实时换脸",
    autochat: "AI 成交聊天",
    cases: "案例",
    engage: "合作方式",
    pricing: "价格",
    about: "关于我们",
    contact: "联系下单",
    cta: "立即咨询",
  },
  hero: {
    badge: "无界科技 · AI 自动成交聊天 · 多语种拟人翻译 · USDT 结算",
    title: "AI 自动成交",
    titleAccent: "聊天系统",
    rotating: ["AI 全自动接客成交", "多语种拟人翻译", "人设语音聊天", "多平台号聚合", "实时换脸换声", "无禁区私有 AI"],
    subtitle:
      "以你的人设 7×24 全自动接客、答疑、跟进、促单——多语种拟人翻译让对方看不出你是外国人，AI 主动转化客户、文字转人设语音聊天，人工随时一键接管。同时支持直播 / 视频通话级实时换脸换声。私有部署、数据不出网，全程 USDT 结算。",
    trustline: "已服务出海团队 · 全程 USDT 结算 · 私有部署数据不出网",
    ctaPrimary: "咨询 AI 成交方案",
    ctaSecondary: "查看套餐与价格",
    stats: [
      { value: "7×24", label: "AI 不漏客在线" },
      { value: "30+", label: "支持语种" },
      { value: "100%", label: "私有可控不出网" },
      { value: "300+", label: "服务场景" },
    ],
  },
  solutionsSection: {
    title: "五条产品线 · 一个无界底座",
    subtitle: "幻颜换脸、幻声克隆、幻影直播分身、通译实时互译、智聊自动成交——五条产品线共享无界底座，私有部署、数据不出网，按需单独选用或组合。",
  },
  solutions: [
    {
      id: "voice",
      tag: "幻声 VoiceX",
      title: "声音克隆 VoiceClone",
      desc: "几十秒样本即可克隆任意音色，支持多语种 TTS 与实时变声。",
      features: ["秒级音色克隆", "多语种合成", "实时变声 API", "可接入语音客服"],
      pricing: [
        { plan: "体验", price: "18 / 月", detail: "1 音色，1 万字符 TTS" },
        { plan: "标准", price: "78 / 月", detail: "5 音色，10 万字符，多语种" },
        { plan: "专业", price: "198 / 月", detail: "20 音色，50 万字符，变声 API" },
        { plan: "按量加购", price: "10 / 万字符", detail: "实时变声 0.04 / 分钟" },
      ],
    },
    {
      id: "faceswap",
      tag: "幻颜 FaceX",
      title: "AI 换脸 FaceSwap",
      desc: "图片 / 视频换脸与数字人形象定制；实时换脸见上方旗舰服务（部署到你自己的设备）。",
      features: ["图片视频换脸", "实时换脸部署", "数字人形象", "高清无痕"],
      pricing: [
        { plan: "图片换脸", price: "1 / 张", detail: "包月 38 = 100 张" },
        { plan: "视频换脸", price: "4 / 分钟", detail: "成片计费" },
        { plan: "实时换脸部署", price: "980 起", detail: "远程部署调试，见旗舰" },
        { plan: "数字人形象定制", price: "398 起", detail: "形象买断" },
      ],
    },
    {
      id: "translate",
      tag: "通译 LingoX",
      title: "聊天聚合 + 实时翻译",
      desc: "TG / LINE / WhatsApp / Messenger 多号聚合，收发即时双向翻译，AI 自动回复 + 人工接管。",
      features: ["多平台多号聚合", "双向实时翻译", "AI 自动回复", "人工接管 + 知识库"],
      highlight: true,
      pricing: [
        { plan: "入门", price: "58 / 月", detail: "3 账号，双向翻译，1 平台" },
        { plan: "团队", price: "198 / 月", detail: "10 账号，全平台，AI 自动回复" },
        { plan: "旗舰", price: "598 / 月", detail: "50 账号，人工接管，数据看板" },
        { plan: "私有化部署", price: "3000 起", detail: "买断 + 600 / 年维护" },
      ],
    },
    {
      id: "private-ai",
      tag: "无界底座",
      title: "无审查 · 无禁区 AI 私有部署",
      desc: "私有化部署无内容审查、无话题禁区的大模型，不上传公网、数据不出本地，可定制微调。",
      features: ["无内容审查", "无话题禁区", "私有部署不出网", "模型微调定制"],
      pricing: [
        { plan: "云端 API", price: "≈1.2x token", detail: "充值制，无审查中转" },
        { plan: "单机私有部署", price: "1600 起", detail: "一次性，含部署调试" },
        { plan: "企业私有集群", price: "6000 起", detail: "按规模报价" },
        { plan: "模型微调 / 定制", price: "1000 起", detail: "按任务" },
      ],
    },
    {
      id: "digital-human",
      tag: "幻影 LiveX",
      title: "数字人 / 虚拟主播",
      desc: "声音克隆 + 换脸 + 口型同步，一键克隆你自己的数字分身。",
      features: ["克隆形象 + 声音", "口型同步", "虚拟主播直播", "口播视频生成"],
      pricing: [
        { plan: "订阅", price: "198 起 / 月", detail: "数字人口播套餐" },
        { plan: "形象买断", price: "798", detail: "永久数字人形象" },
      ],
    },
    {
      id: "video-dubbing",
      tag: "幻影 LiveX",
      title: "AI 视频翻译配音",
      desc: "上传视频自动翻译、克隆原声配音、对口型，出海短视频刚需。",
      features: ["自动字幕翻译", "原声克隆配音", "口型对齐", "批量处理"],
      pricing: [
        { plan: "视频翻译配音", price: "6 / 分钟", detail: "成片计费" },
        { plan: "短视频矩阵", price: "398 / 月", detail: "30 条；100 条 998 / 月" },
      ],
    },
  ],
  pricingSection: {
    title: "私有定制 · 一切皆可实现",
    subtitle: "克隆声音、克隆人脸、实时视频通话与直播——只要你想要的功能，我们都能为你私有定制落地，适用于任何场景。",
    unit: "单位：USDT · 可私有定制",
    note: "下方为标准能力的挂牌建议价；超出清单的需求一律支持私有定制开发，按场景与规模报价。把你的想法告诉客服，我们把它变成现实。",
    planCol: "套餐",
    priceCol: "价格 (USDT)",
    detailCol: "说明",
    allLabel: "全部",
  },
  trust: {
    platformsLabel: "已聚合主流平台",
    platforms: ["Telegram", "LINE", "WhatsApp", "Messenger", "Discord", "Instagram"],
    statsTitle: "用数据说话",
    stats: [
      { value: "2000", suffix: "万+", label: "累计处理消息" },
      { value: "98", suffix: "%", label: "翻译准确满意度" },
      { value: "300", suffix: "+", label: "服务出海团队" },
      { value: "24", suffix: "/7", label: "全天候运行" },
    ],
    testimonialsTitle: "客户怎么说",
    testimonials: [
      {
        quote: "多号聚合 + 实时翻译直接把我们的跨境客服效率拉满，回复速度翻了一倍。",
        name: "Leo",
        role: "跨境电商 · 运营负责人",
      },
      {
        quote: "数字人口播视频批量产出，一个人顶一个小团队，内容矩阵铺得飞快。",
        name: "Mia",
        role: "MCN 工作室 · 主理人",
      },
      {
        quote: "私有化部署数据不出本地，合规上完全放心，模型还能按我们业务微调。",
        name: "陈工",
        role: "企业客户 · 技术负责人",
      },
    ],
    disclaimer: "以上数据为平台累计估算、客户评价为部分客户授权分享的示例展示，不代表对具体效果的承诺。",
  },
  plans: {
    title: "AI 成交聊天 · 套餐",
    subtitle: "聚合 + AI 拟人翻译 + AI 自动成交 + 人设语音，按账号规模选档，年付更划算。",
    monthly: "月付",
    yearly: "年付",
    save: "省 15%",
    popular: "最受欢迎",
    perMonth: "/ 月",
    cta: "选择此套餐",
    items: [
      {
        name: "入门",
        priceMonthly: "58",
        priceYearly: "50",
        desc: "小团队 / 个人起步",
        features: ["3 个聊天账号", "AI 拟人翻译", "1 个平台", "基础声音克隆体验"],
      },
      {
        name: "团队",
        priceMonthly: "198",
        priceYearly: "168",
        desc: "成长型团队首选",
        features: ["10 个聊天账号", "全平台聚合", "AI 自动成交回复", "人设语音消息", "优先客服"],
        highlight: true,
      },
      {
        name: "旗舰",
        priceMonthly: "598",
        priceYearly: "508",
        desc: "规模化 / 企业级",
        features: ["50 个聊天账号", "AI 自动成交 + 人设语音", "人工接管 + 知识库", "数据看板", "可选私有化部署"],
      },
    ],
  },
  orderSteps: {
    title: "三步即可开通",
    subtitle: "流程透明，全程 USDT 结算，开通即用。",
    steps: [
      { title: "选择服务", desc: "在业务与价格中挑选适合的套餐或组合。" },
      { title: "Telegram 沟通确认", desc: "添加客服，确认需求、用量与最终报价。" },
      { title: "USDT 付款开通", desc: "核对收款地址后付款，快速开通账号与权限。" },
    ],
  },
  faq: {
    title: "常见问题",
    subtitle: "还有疑问？直接联系 Telegram 客服。",
    items: [
      { q: "为什么只支持 USDT 结算？", a: "面向出海与跨境客户，USDT 无需绑卡、到账快、跨境无障碍。大额合作可联系客服商定其它方式。" },
      { q: "支持私有化部署吗？", a: "支持。聊天聚合与无审查 AI 均可部署到你的服务器，数据本地化、无云端上报。" },
      { q: "你们的 AI 翻译和谷歌翻译有什么不同？", a: "我们用 AI 翻译 + 对话技术，输出地道口语、地方俚语与文化语气，对方看不出你是外国人；不同于市面软件直接套谷歌等 API 的生硬直译。" },
      { q: "AI 能自动跟客户成交吗？人工能接管吗？", a: "可以。AI 以你的人设 7×24 自动接洽、答疑、跟进、促单转化，遇到关键节点人工可随时一键接管。" },
      { q: "声音克隆 / 换脸需要什么素材？", a: "声音克隆仅需几十秒清晰人声样本；换脸需要清晰的人脸图片或视频。请确保你拥有相应授权。" },
      { q: "无审查 AI 是什么意思？", a: "指私有部署、无云端内容审查上报的大模型，输出不受平台内容过滤限制，数据完全本地可控。" },
      { q: "可以按量付费吗？", a: "可以。多数业务同时提供订阅与按量加购，用多少付多少，灵活组合。" },
      { q: "如何防止收款诈骗？", a: "所有付款前请通过官方 Telegram 客服核对最新收款地址，切勿轻信第三方转发的地址。" },
    ],
  },
  realtime: {
    badge: "旗舰服务 · 技术落地",
    title: "实时换脸 + 换声 · 私有化部署服务",
    subtitle: "我们把直播 / 视频通话级的实时换脸 + 声音克隆，部署到你自己的设备上，并按你的场景深度定制——硬件你自备，我们负责选型建议、部署落地、调试培训与长期支持。数据全程私有、不出网，适用于任何场景。",
    videoNote: "实时换脸演示 · 视频即将上线",
    features: [
      { icon: "cpu", title: "私有可控", desc: "部署在你自己的设备，数据不出本地、不上传公网，完全自主可控。" },
      { icon: "zap", title: "真·实时", desc: "实时换脸 + 同步换声，适配直播、视频通话、多人会议，低延迟流畅。" },
      { icon: "monitor", title: "按需定制", desc: "按你的平台与玩法深度调试，配置、效果、流程全部为你量身定制。" },
      { icon: "shield", title: "长期支持", desc: "交付文档 + 上手培训，提供运维、升级与技术支持，随时远程协助。" },
    ],
    stepsTitle: "服务流程",
    steps: [
      { title: "需求沟通 & 选型", desc: "确认你的场景，给出硬件配置清单，你照单自行采购。" },
      { title: "远程部署", desc: "在你的设备上部署换脸 / 换声 / 数字人 / 私有大模型。" },
      { title: "场景定制调试", desc: "按你的平台与玩法调到最佳效果，并远程培训上手。" },
      { title: "验收 & 持续支持", desc: "交付文档与运维，提供后续升级和技术支持。" },
    ],
    hardwareTitle: "推荐硬件配置（你自购）",
    hardwareNote: "硬件由你采购、完全归你所有；我们只提供配置建议与部署服务，不转售算力。",
    hardware: [
      { tier: "入门", gpu: "RTX 4060Ti 16G / 4070", use: "单人脸 · 1080P 直播 / 视频通话" },
      { tier: "专业", gpu: "RTX 4090 24G", use: "高清高帧 · 多场景 · 数字人" },
      { tier: "旗舰", gpu: "双卡 / 48G+（A6000、4090×2）", use: "多人脸 · 专业大屏 · 私有大模型" },
    ],
    plansTitle: "服务套餐与价格",
    plansNote: "一次性部署费 · 全程 USDT 结算 · 含部署调试与技术支持",
    availability: "本周可接 3 个部署排期 · 预约制（先约先得）",
    plans: [
      {
        name: "基础部署",
        tag: "单能力",
        price: "980 USDT 起",
        unit: "一次性 · 含部署调试",
        specs: ["实时换脸 或 换声 任选其一", "远程部署 + 基础调试", "上手培训", "7 天技术支持"],
        cta: "Telegram 咨询",
      },
      {
        name: "创作者全能",
        tag: "推荐",
        price: "2580 USDT",
        unit: "一次性 · 含部署调试",
        specs: ["实时换脸 + 换声 + 数字人", "多场景深度调试", "上手培训 + 文档", "30 天技术支持"],
        cta: "Telegram 咨询",
        highlight: true,
      },
      {
        name: "全家桶",
        tag: "全能力",
        price: "3980 USDT",
        unit: "一次性 · 含部署调试",
        specs: ["换脸 + 换声 + 数字人", "无禁区私有大模型", "全场景定制调试", "30 天支持 + 1 月运维"],
        cta: "Telegram 咨询",
      },
    ],
    extrasTitle: "更多服务",
    extras: [
      "场景深度定制开发 · 报价制 from 1600",
      "上门 / 驻场部署 · from 3000 + 差旅",
      "运维订阅 · 198 / 月 或 1998 / 年",
      "按次远程协助 · 160 / 小时",
    ],
    capacityNote: "硬件归你所有、数据私有不出网；我们提供从选型到部署、定制与运维的全流程技术服务，适用于任何场景。",
    cta: "Telegram 咨询定制",
  },
  faceswap: {
    badge: "免费体验 · 图片版",
    title: "图片换脸 · 免费体验",
    subtitle: "想先感受效果？上传照片，秒变职业照、超级英雄、宇航员。（实时换脸请看上方旗舰服务）",
    tabCustom: "自定义目标",
    tabTemplate: "模板换脸",
    uploadFace: "你的照片",
    uploadFaceHint: "正脸清晰、光线充足效果最佳",
    uploadTarget: "目标图片",
    uploadTargetHint: "你的脸会被换到这张图里",
    pickTemplate: "选择一个模板",
    templates: [
      { id: "business", name: "职业商务", file: "/templates/tpl-business.png" },
      { id: "hero", name: "超级英雄", file: "/templates/tpl-hero.png" },
      { id: "astronaut", name: "宇航员", file: "/templates/tpl-astronaut.png" },
      { id: "fashion", name: "时尚大片", file: "/templates/tpl-fashion.png" },
    ],
    consent: "我确认对上传的照片拥有合法肖像授权，且不用于任何违法用途。",
    button: "开始换脸",
    processing: "AI 换脸中…通常需 30~60 秒，请稍候",
    resultTitle: "换脸结果",
    download: "下载图片",
    again: "再换一张",
    privacy: "图片仅用于本次换脸处理，不长期存储。",
    errConfig: "换脸服务正在配置中，敬请期待。",
    errNoConsent: "请先勾选肖像授权确认。",
    errNoImage: "请先上传照片。",
    errSize: "图片过大，请上传小于 8MB 的图片。",
    errGeneric: "换脸失败，请换一张更清晰的正脸照片重试。",
  },
  about: {
    title: "为什么选择无界科技",
    subtitle: "形象看得见、对话听得懂——技术自研、出海友好、私有可控。",
    points: [
      { title: "技术自研", desc: "声音 / 形象 / 对话 / 部署四大引擎自研可控，非简单套壳。" },
      { title: "出海友好", desc: "全程 USDT 结算，无需绑卡，跨境团队即买即用。" },
      { title: "私有可控", desc: "支持私有化部署，数据不出本地，无云端上报。" },
      { title: "快速交付", desc: "标准业务即开即用，定制需求专属对接。" },
    ],
  },
  community: {
    badge: "官方社群",
    title: "关注频道 · 加入交流群",
    subtitle: "频道第一时间发真实案例、新功能与限时折扣；进群和同行交流、领 AI 自动成交试用与专属优惠。",
    perks: ["真实案例与成果展示", "新功能 + 限时折扣", "进群领试用 · 同行交流"],
    cta: "关注频道",
    groupCta: "加入交流群",
  },
  gate: {
    badge: "专属解锁",
    title: "关注频道 + 进群，解锁专属优惠",
    subtitle: "关注官方频道并加入交流群，即可解锁专属折扣码与免费试用名额。",
    joinChannel: "① 关注频道",
    joinGroup: "② 加入交流群",
    joinedChannel: "✅ 已关注频道",
    joinedGroup: "✅ 已加入群",
    verify: "我已完成，校验解锁",
    checking: "校验中…",
    webNote: "在 Telegram 内打开本页（小程序）即可自动校验并解锁。",
    unlockedTitle: "🎉 已解锁专属权益",
    unlockedDesc: "凭专属折扣码联系客服，享报价优惠并锁定免费试用名额。",
    codeLabel: "你的专属折扣码",
    code: "HL-VIP",
    cta: "锁定试用名额 · 联系客服",
    notYet: "尚未检测到关注/进群，请先完成上面两步再校验。",
  },
  swap: {
    before: "原始",
    after: "换脸后",
    dragHint: "拖动滑块查看换脸前后",
    liveTag: "实时换脸中",
    hudEngine: "FACE SWAP ENGINE",
    hudFps: "32 FPS",
    hudLatency: "38ms",
    callStatus: "通话中",
    you: "你 · 真实",
    theySee: "对方看到",
    faceVoice: "换脸 + 换声",
    voiceCloning: "声音克隆中",
  },
  personas: {
    title: "你是谁？看这里",
    subtitle: "不同场景的玩法不一样，先找到属于你的那一个。",
    items: [
      { id: "streamer", title: "主播 / 直播", desc: "直播、视频通话实时换脸换声，部署到你自己的设备，连麦即开播。", cta: "看实时换脸", href: "#realtime" },
      { id: "ecom", title: "出海电商 / 客服", desc: "多平台多号聚合 + AI 拟人翻译 + AI 自动成交，转化客户不漏单。", cta: "看 AI 成交聊天", href: "#autochat" },
      { id: "creator", title: "内容创作者 / MCN", desc: "数字人口播 + 声音克隆 + 视频翻译配音，批量起号。", cta: "看数字人", href: "#showcase" },
      { id: "enterprise", title: "企业 / 开发者", desc: "无审查大模型私有部署，数据不出本地，可微调可定制。", cta: "看私有部署", href: "#showcase" },
    ],
  },
  showcaseSection: {
    title: "看得见的能力",
    subtitle: "不只是说说——每项核心能力都给你一个可交互的真实演示。",
  },
  chatDemo: {
    badge: "对话 · 实时翻译",
    title: "聊天聚合 + 实时翻译",
    desc: "TG / LINE / WhatsApp / Messenger 多号聚合到一个收件箱，收发即时双向翻译，AI 自动回复、人工随时接管。",
    features: ["多平台多号聚合", "双向实时翻译", "AI 自动回复", "人工接管 + 知识库"],
    translatedTag: "已翻译",
    replyName: "AI 助手",
    typing: "对方正在输入…",
    messages: [
      { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México?", translated: "你们发货到墨西哥吗？" },
      { name: "あやか", flag: "🇯🇵", text: "在庫はまだありますか？", translated: "还有库存吗？" },
    ],
    reply: { text: "Yes! We ship worldwide, 3–7 days.", translated: "可以！全球发货，3–7 天送达。" },
  },
  voiceDemo: {
    badge: "声音 · 克隆",
    title: "声音克隆 VoiceClone",
    desc: "几十秒清晰人声样本，即可克隆任意音色，支持多语种 TTS 与实时变声，可直接接入语音客服。",
    features: ["秒级音色克隆", "多语种合成", "实时变声 API", "语音客服接入"],
    original: "原声样本",
    cloned: "克隆结果",
    langsLabel: "支持多语种",
    langs: ["中文", "English", "日本語", "한국어", "Español", "العربية"],
  },
  deployDemo: {
    badge: "部署 · 无禁区私有",
    title: "无审查 · 无禁区 AI · 私有部署",
    desc: "无内容审查、无话题禁区的大模型，部署在你自己的服务器：不上传公网、数据不出本地，输出不受平台过滤限制，可按业务自由微调。",
    features: ["无内容审查", "无话题禁区", "私有部署不出网", "自由微调定制"],
    cloudLabel: "公有云 API",
    localLabel: "你的私有部署",
    rows: [
      { label: "数据全程留在本地", cloud: false, local: true },
      { label: "无内容审查 / 不拦截", cloud: false, local: true },
      { label: "无云端日志上报", cloud: false, local: true },
      { label: "可按业务微调", cloud: false, local: true },
      { label: "完全自主可控", cloud: false, local: true },
    ],
  },
  digitalDemo: {
    badge: "组合 · 数字分身",
    title: "数字人 / 虚拟主播",
    desc: "声音克隆 + 换脸 + 口型同步，一键克隆你自己的数字分身，7×24 直播带货、批量产出口播视频。",
    features: ["克隆形象 + 声音", "口型同步", "虚拟主播直播", "口播视频生成"],
    tags: ["口型同步", "声音克隆", "多语配音", "永久形象"],
  },
  compare: {
    badge: "为什么选我们",
    title: "我们 vs 市面方案",
    subtitle: "同样是聚合聊天，差距在“会不会成交”。",
    cols: ["无界 AI", "普通聚合翻译软件", "纯人工团队"],
    rows: [
      { label: "翻译质量", us: "AI 拟人 · 地道口语/俚语", them: "谷歌式直译 · 生硬易错", manual: "看人，水平不一" },
      { label: "自动成交", us: "AI 主动促单 · 转化客户", them: "不支持", manual: "靠经验 · 易漏单" },
      { label: "人设语音", us: "文字转人设声音", them: "无", manual: "无" },
      { label: "7×24 在线", us: "全天候不漏客", them: "部分", manual: "受工时限制" },
      { label: "多平台聚合", us: "统一收件箱", them: "支持", manual: "手动切换" },
      { label: "私有部署 · 数据不出网", us: "支持", them: "多为云端", manual: "—" },
      { label: "规模化成本", us: "低 · 一人顶一队", them: "中", manual: "高 · 随人头涨" },
    ],
  },
  autochat: {
    badge: "旗舰业务 · AI 自动成交",
    title: "AI 自动成交聊天系统",
    subtitle:
      "不止聚合与翻译——AI 以你的人设，7×24 全自动接客、答疑、跟进、促单、转化客户，跨语言零障碍、真人级体验。市面软件多接谷歌等翻译 API，生硬易错、一眼穿帮；我们用 AI 翻译 + 对话技术，地道、拟人、会成交。",
    features: [
      { icon: "languages", title: "AI 拟人翻译", desc: "地道口语 + 地方俚语 + 文化语气，对方看不出你是外国人——告别谷歌式生硬直译。" },
      { icon: "bot", title: "AI 自动成交", desc: "懂上下文，主动答疑、跟进、引导下单、转化客户，7×24 不漏客，人工可随时接管。" },
      { icon: "mic", title: "人设语音聊天", desc: "AI 文字回复转成你的人设声音，发语音消息 / 语音聊天，更真实、更信任、更易成交。" },
      { icon: "inbox", title: "多平台聚合", desc: "TG / LINE / WhatsApp / Messenger 多号统一收件箱，规模化批量运营。" },
    ],
    compareTitle: "AI 拟人翻译 vs 普通翻译",
    compareNote: "同一句话，差距一眼可见——普通翻译让对方瞬间识破，AI 拟人翻译像本地人。",
    badLabel: "普通翻译 · 谷歌式",
    goodLabel: "我们 · AI 拟人翻译",
    compare: [
      {
        src: "¿Está disponible? lo quiero ya jaja",
        bad: "它可用吗？我现在要它哈哈",
        good: "还有货吗？我现在就想要哈哈～",
      },
      {
        src: "我们给你包邮，今天下单还送小礼物",
        bad: "We give you free shipping, today order also send small gift",
        good: "Free shipping on us — order today and grab a free gift 🎁",
      },
    ],
    scenariosLabel: "适用场景",
    scenarios: ["出海电商", "私域客服", "约单获客", "跨境社群"],
    cta: "Telegram 咨询 AI 成交方案",
    demo: {
      inbox: "统一收件箱 · AI 自动成交",
      personaName: "你的人设 AI",
      translatedTag: "AI 拟人翻译",
      autoTag: "自动成交",
      voiceTag: "人设语音",
      typing: "AI 正在以人设回复…",
      incoming: { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México? precio?", translated: "发货到墨西哥吗？多少钱？" },
      reply: {
        text: "¡Claro! Envío a México en 5-7 días 🚀 Hoy con 10% OFF, ¿te lo aparto?",
        translated: "当然！墨西哥 5-7 天到 🚀 今天还有 9 折，要不要我先帮你留一份？",
      },
      voiceLen: "0:08 · 人设语音",
    },
  },
  engage: {
    badge: "合作方式 · 灵活共赢",
    title: "三种合作方式，总有一种适合你",
    subtitle: "无论你已有硬件、想省心全包，还是想投资共赢——我们都能落地。硬件归你所有，数据私有不出网，全程 USDT 结算。",
    selectorTitle: "你的情况是？",
    youLabel: "你负责",
    weLabel: "我们负责",
    selector: [
      { id: "service", label: "我有硬件 · 找技术落地" },
      { id: "managed", label: "我要省心 · 全包托管" },
      { id: "invest", label: "我想投资 · 合作分红" },
    ],
    models: [
      {
        id: "service",
        badge: "最自主",
        name: "私有部署服务",
        tagline: "你的设备，我们负责落地",
        you: "自购硬件 · 提供场地",
        we: "选型建议 + 部署 + 定制 + 培训 + 支持",
        price: "一次性 980 USDT 起",
        priceNote: "含三档部署套餐 · 可加运维 198 / 月",
        points: ["数据 100% 私有、不出网", "按你的场景深度定制", "交付文档 + 上手培训", "7~30 天技术支持"],
        cta: "Telegram 咨询",
      },
      {
        id: "managed",
        badge: "最省心",
        name: "全托管 · 交钥匙",
        tagline: "硬件 + 机房 + 运维我们包，你只管用",
        you: "出需求 · 按月付费",
        we: "硬件代采 + 机房托管 + 部署 + 7×24 运维 + 升级",
        price: "from 1980 USDT / 月",
        priceNote: "含机房 + 运维 · 硬件代采按成本另计",
        points: ["免运维、稳定在线", "7×24 监控与升级", "按需弹性扩容", "一价全包、省心无忧"],
        cta: "Telegram 咨询",
        highlight: true,
      },
      {
        id: "invest",
        badge: "最高回报",
        name: "机房投资合作分红",
        tagline: "你出资，我们专业运营，按月分红",
        you: "出资建节点 / 买卡",
        we: "技术落地 + 运营 + 获客 + 7×24 运维",
        price: "分红合作 · 起投 from 20,000 USDT",
        priceNote: "净利分成 70 / 30（投资方占多数）",
        points: ["我们全程专业运营", "净利分成、你占多数", "风险共担、透明月结对账", "签约合作、权责清晰"],
        cta: "Telegram 洽谈合作",
      },
    ],
    serviceTiersLabel: "三档部署套餐（一次性）",
    extrasLabel: "更多可选服务",
    invest: {
      roiTitle: "示例测算 · 标准档 50,000 USDT（满载估算）",
      roiRows: [
        { label: "预估月营收", value: "8,000 – 12,000 USDT" },
        { label: "扣成本预估净利", value: "6,000 – 9,000 USDT" },
        { label: "投资方月分红（70%）", value: "4,200 – 6,300 USDT" },
        { label: "预估回本周期", value: "约 9 – 13 个月" },
      ],
      roiNote: "以上为满载理想估算，实际受利用率、市场与汇率波动影响，不构成任何收益承诺。",
      flowTitle: "合作流程",
      flow: ["洽谈评估", "签约 · 明确分成权责", "出资建节点（可代采）", "部署运营 · 月度分红对账"],
      compliance: "硬件归投资方所有；仅服务合法合规业务，遵守当地法律法规。",
    },
    matrixTitle: "三种方式对比",
    matrixCols: ["私有部署服务", "全托管交钥匙", "投资合作分红"],
    matrix: [
      { label: "硬件采购", a: "你自购", b: "我们代采", c: "你出资" },
      { label: "机房 / 场地", a: "你提供", b: "我们提供", c: "共建" },
      { label: "部署调试", a: "我们", b: "我们", c: "我们" },
      { label: "日常运维", a: "可选", b: "我们 7×24", c: "我们" },
      { label: "获客 / 接单", a: "—", b: "—", c: "我们" },
      { label: "计费方式", a: "一次性", b: "月费", c: "分红" },
      { label: "适合", a: "技术自主", b: "省心稳定", c: "投资收益" },
    ],
  },
  roi: {
    badge: "算一算 · 你能多赚多少",
    title: "AI 成交收益试算",
    subtitle: "拖动几个数字，估算 AI 自动成交每月能帮你省下的人力、多赚的营收。",
    inputs: { agents: "当前客服人数", salary: "单客服月薪", leads: "日均新咨询", aov: "客单价", conv: "当前转化率" },
    units: { agents: "人", salary: "USDT", leads: "条/天", aov: "USDT", conv: "%" },
    resultSaveLabel: "人力成本优化 / 月",
    resultRevenueLabel: "转化提升增收 / 月",
    resultNetLabel: "净增收益 / 月",
    resultRoiLabel: "投入产出比",
    resultYearLabel: "年化净增（估）",
    planLabel: "推荐套餐",
    perMonth: "/ 月",
    assumptionsTitle: "测算假设（可与客服按你的实际调整）",
    assumptions: [
      "AI 自动成交可优化约 60% 重复性人力成本",
      "拟人翻译 + 7×24 不漏客，转化率平均相对提升约 35%",
      "按每月 30 天、你输入的客单价与转化率估算",
    ],
    disclaimer: "以上为基于行业经验的估算模型，实际效果因行业、流量与运营而异，不构成任何收益承诺。",
    cta: "按我的数据要方案",
  },
  cases: {
    badge: "真实成果 · 看得见",
    title: "客户成果 & 多语种实战",
    subtitle: "从换脸直播到 AI 多语种自动成交——下面是不同场景的真实玩法与对话实录。",
    items: [
      { scene: "出海电商", metric: "+38%", metricLabel: "转化率", quote: "多语种 AI 自动成交后，夜间和不同时区的订单也不漏了。", name: "Leo", role: "跨境电商 · 运营负责人", img: "/showcase/digital-human.png" },
      { scene: "直播 / 连麦", metric: "0 延迟", metricLabel: "实时换脸换声", quote: "连麦直播稳定流畅，观众完全看不出是换的脸和声音。", name: "Aya", role: "全职主播", img: "/showcase/live-after.png" },
      { scene: "私域客服", metric: "1 人顶 6", metricLabel: "人力效率", quote: "一个人管六个号，回复比真人还地道，再也不用熬夜守屏。", name: "Mia", role: "私域客服 · 负责人", img: "/showcase/live-before.png" },
    ],
    galleryTitle: "多语种 · 拟人成交实录",
    gallerySubtitle: "同一套 AI，地道接洽各国客户——翻译看不出外国人，回复主动促单。",
    translatedTag: "AI 拟人翻译",
    replyTag: "AI 自动成交",
    gallery: [
      { lang: "Español", flag: "🇪🇸", incoming: "¿Tienen envío a Chile? 🇨🇱", translated: "发货到智利吗？", reply: "¡Sí! Llega en 7-10 días 🚀 Hoy 10% OFF, ¿te lo aparto?" },
      { lang: "Português", flag: "🇧🇷", incoming: "Quanto custa? quero comprar agora", translated: "多少钱？我现在就想买", reply: "Sai por R$199 com frete grátis hoje 🎁 Reservo pra você?" },
      { lang: "العربية", flag: "🇸🇦", incoming: "هل المنتج متوفر؟", translated: "产品有货吗？", reply: "نعم متوفر ✅ شحن خلال ٥ أيام، وخصم ١٠٪ اليوم" },
      { lang: "ไทย", flag: "🇹🇭", incoming: "สนใจค่ะ ราคาเท่าไหร่", translated: "有兴趣，多少钱？", reply: "ราคา 990 บาท ส่งฟรีวันนี้ 🎉 รับเลยไหมคะ" },
    ],
    disclaimer: "案例数据来自客户反馈与内部统计，仅供参考；对话为多语种能力实录示意。",
  },
  lead: {
    title: "不方便现在开 Telegram？留个联系方式",
    subtitle: "留下需求，我们 5 分钟内主动联系你，按你的场景出方案与报价。",
    name: "称呼",
    contact: "联系方式",
    interest: "想了解",
    message: "需求备注（选填）",
    namePh: "怎么称呼你",
    contactPh: "Telegram / WhatsApp / 邮箱 / 微信",
    messagePh: "简单说说你的场景、平台或目标……",
    interests: ["实时换脸换声", "AI 自动成交聊天", "私有部署", "整体托管（我们配硬件机房）", "投资合作分红", "其他"],
    submit: "提交，等客服联系我",
    submitting: "提交中…",
    successTitle: "已收到，马上联系你 ✅",
    successDesc: "我们会在 5 分钟内通过你留的方式联系你；急可直接点上方 Telegram。",
    error: "提交失败，请重试或直接联系 Telegram。",
    contactInvalid: "请填写有效的联系方式（Telegram / WhatsApp / 邮箱等）",
    privacy: "你的信息仅用于本次咨询，不对外共享。",
  },
  contact: {
    title: "联系我们 · 下单",
    subtitle: "添加 Telegram 客服，确认需求后以 USDT 结算。",
    telegram: "Telegram 客服",
    telegramHandle: "@ai_zkw",
    scanHint: "手机扫码直达客服",
    usdt: "USDT 收款",
    usdtNote: "下单前请向客服核对最新收款地址，谨防诈骗。",
    networks: "支持 TRC20 / ERC20（推荐 TRC20，手续费低）。收款地址以客服当面确认为准。",
    responseTime: "客服响应 ≈ 5 分钟 · 7×24 在线",
    compliance: "合规与隐私",
    complianceNote: "仅限本人合法授权用途；禁止用于冒充、诈骗或侵犯他人权益。素材会话结束即删除，不长期留存。",
    cta: "联系 Telegram 客服",
  },
  footer: {
    rights: "无界科技 BOUNDLESS · 保留所有权利",
    disclaimerTitle: "授权与免责声明",
    disclaimer:
      "本平台服务仅供合法用途。用户须确保对所使用的肖像、声音及内容拥有完整授权，严禁用于诈骗、伪造、侵权或任何违法活动。使用即表示同意自行承担相应法律责任。",
    links: ["业务能力", "价格", "关于我们", "联系下单"],
  },
};

const en: Dict = {
  nav: {
    solutions: "Solutions",
    demo: "Live Swap",
    autochat: "AI Closing",
    cases: "Cases",
    engage: "Engagement",
    pricing: "Pricing",
    about: "About",
    contact: "Contact",
    cta: "Get Started",
  },
  hero: {
    badge: "BOUNDLESS · AI Auto-Closing Chat · human-like multi-language · USDT",
    title: "AI Auto-Closing",
    titleAccent: "Chat System",
    rotating: ["AI closes deals 24/7", "Human-like translation", "Persona voice chat", "Multi-platform inbox", "Real-time face & voice swap", "No-limit private AI"],
    subtitle:
      "Your persona engages, answers, follows up and closes 24/7 — human-like multi-language translation hides your origin, AI actively converts customers, turns text into persona voice, and humans can take over anytime. Plus live-stream / video-call grade real-time face & voice swap. Privately deployed, off the net, settled in USDT.",
    trustline: "Trusted by cross-border teams · settled in USDT · private, off the net",
    ctaPrimary: "Get an AI closing plan",
    ctaSecondary: "View plans & pricing",
    stats: [
      { value: "7×24", label: "AI always online" },
      { value: "30+", label: "Languages" },
      { value: "100%", label: "Private, off the net" },
      { value: "300+", label: "Scenarios served" },
    ],
  },
  solutionsSection: {
    title: "Five Product Lines · One Boundless Core",
    subtitle: "FaceX swaps faces, VoiceX clones voices, LiveX powers live digital twins, LingoX translates in real time, ChatX closes deals — five lines on one BOUNDLESS core, privately deployed and composable on demand.",
  },
  solutions: [
    {
      id: "voice",
      tag: "VoiceX",
      title: "Voice Cloning",
      desc: "Clone any voice from a short sample, with multilingual TTS and real-time voice changing.",
      features: ["Instant voice cloning", "Multilingual TTS", "Real-time voice API", "Voice agent ready"],
      pricing: [
        { plan: "Starter", price: "18 / mo", detail: "1 voice, 10K chars TTS" },
        { plan: "Standard", price: "78 / mo", detail: "5 voices, 100K chars, multilingual" },
        { plan: "Pro", price: "198 / mo", detail: "20 voices, 500K chars, voice API" },
        { plan: "Add-on", price: "10 / 10K chars", detail: "Real-time 0.04 / min" },
      ],
    },
    {
      id: "faceswap",
      tag: "FaceX",
      title: "AI Face Swap",
      desc: "Image / video face swap and digital avatars; real-time swap is the flagship above (deployed on your own device).",
      features: ["Image & video swap", "Live swap deploy", "Digital avatars", "HD seamless"],
      pricing: [
        { plan: "Image swap", price: "1 / image", detail: "Monthly 38 = 100 images" },
        { plan: "Video swap", price: "4 / min", detail: "Per output minute" },
        { plan: "Live swap deploy", price: "from 980", detail: "Remote deploy, see flagship" },
        { plan: "Custom avatar", price: "from 398", detail: "One-time buyout" },
      ],
    },
    {
      id: "translate",
      tag: "LingoX",
      title: "Chat Aggregation + Live Translation",
      desc: "Unify TG / LINE / WhatsApp / Messenger accounts with instant two-way translation, AI auto-reply and human handoff.",
      features: ["Multi-platform inbox", "Two-way translation", "AI auto-reply", "Handoff + knowledge base"],
      highlight: true,
      pricing: [
        { plan: "Entry", price: "58 / mo", detail: "3 accounts, translation, 1 platform" },
        { plan: "Team", price: "198 / mo", detail: "10 accounts, all platforms, AI reply" },
        { plan: "Flagship", price: "598 / mo", detail: "50 accounts, handoff, dashboard" },
        { plan: "Self-hosted", price: "from 3000", detail: "Buyout + 600 / yr support" },
      ],
    },
    {
      id: "private-ai",
      tag: "BOUNDLESS Engine",
      title: "Uncensored · No-limit AI Deploy",
      desc: "Privately deploy uncensored, unrestricted LLMs. Off the public internet, data stays local, fine-tuning available.",
      features: ["No content filtering", "No topic limits", "Off the public net", "Custom fine-tuning"],
      pricing: [
        { plan: "Cloud API", price: "≈1.2x token", detail: "Prepaid uncensored relay" },
        { plan: "Single-node", price: "from 1600", detail: "One-time, incl. setup" },
        { plan: "Enterprise cluster", price: "from 6000", detail: "Quote by scale" },
        { plan: "Fine-tuning", price: "from 1000", detail: "Per task" },
      ],
    },
    {
      id: "digital-human",
      tag: "LiveX",
      title: "Digital Human / Virtual Streamer",
      desc: "Voice cloning + face swap + lip sync to spin up your own digital twin in one click.",
      features: ["Cloned face + voice", "Lip sync", "Live streaming avatar", "Talking-head videos"],
      pricing: [
        { plan: "Subscription", price: "from 198 / mo", detail: "Talking-head package" },
        { plan: "Avatar buyout", price: "798", detail: "Permanent avatar" },
      ],
    },
    {
      id: "video-dubbing",
      tag: "LiveX",
      title: "AI Video Translation & Dubbing",
      desc: "Auto-translate videos, dub with cloned original voice, and align lip movements — built for global short video.",
      features: ["Auto subtitle translation", "Cloned voice dubbing", "Lip alignment", "Batch processing"],
      pricing: [
        { plan: "Translate & dub", price: "6 / min", detail: "Per output minute" },
        { plan: "Video matrix", price: "398 / mo", detail: "30 clips; 100 clips 998 / mo" },
      ],
    },
  ],
  pricingSection: {
    title: "Private & Custom · Anything is possible",
    subtitle: "Clone voices, clone faces, real-time video calls and live streams — whatever feature you imagine, we build it for you, privately, for any scenario.",
    unit: "Unit: USDT · custom available",
    note: "Below are suggested prices for standard capabilities; anything beyond the list is delivered as private custom development, quoted by scenario and scale. Tell us your idea — we make it real.",
    planCol: "Plan",
    priceCol: "Price (USDT)",
    detailCol: "Details",
    allLabel: "All",
  },
  trust: {
    platformsLabel: "Platforms unified",
    platforms: ["Telegram", "LINE", "WhatsApp", "Messenger", "Discord", "Instagram"],
    statsTitle: "Numbers that speak",
    stats: [
      { value: "20", suffix: "M+", label: "Messages processed" },
      { value: "98", suffix: "%", label: "Translation satisfaction" },
      { value: "300", suffix: "+", label: "Global teams served" },
      { value: "24", suffix: "/7", label: "Always-on uptime" },
    ],
    testimonialsTitle: "What clients say",
    testimonials: [
      {
        quote: "Multi-account inbox plus live translation maxed out our cross-border support — replies got twice as fast.",
        name: "Leo",
        role: "Cross-border e-commerce · Ops Lead",
      },
      {
        quote: "Batch digital-human videos let one person do the work of a whole team. Our content matrix scaled fast.",
        name: "Mia",
        role: "MCN Studio · Founder",
      },
      {
        quote: "Private deployment keeps data local — fully compliant, and the model fine-tunes to our business.",
        name: "Mr. Chen",
        role: "Enterprise client · Tech Lead",
      },
    ],
    disclaimer: "Figures are cumulative platform estimates; testimonials are illustrative examples shared with client consent and are not a guarantee of specific results.",
  },
  plans: {
    title: "AI Auto-Closing Chat · Plans",
    subtitle: "Aggregation + human-like AI translation + AI auto-closing + persona voice. Pick by account scale; save more annually.",
    monthly: "Monthly",
    yearly: "Yearly",
    save: "Save 15%",
    popular: "Most popular",
    perMonth: "/ mo",
    cta: "Choose plan",
    items: [
      {
        name: "Entry",
        priceMonthly: "58",
        priceYearly: "50",
        desc: "Small teams / individuals",
        features: ["3 chat accounts", "Human-like AI translation", "1 platform", "Basic voice cloning trial"],
      },
      {
        name: "Team",
        priceMonthly: "198",
        priceYearly: "168",
        desc: "Best for growing teams",
        features: ["10 chat accounts", "All platforms unified", "AI auto-closing replies", "Persona voice messages", "Priority support"],
        highlight: true,
      },
      {
        name: "Flagship",
        priceMonthly: "598",
        priceYearly: "508",
        desc: "Scale / enterprise",
        features: ["50 chat accounts", "AI auto-closing + persona voice", "Human handoff + knowledge base", "Analytics dashboard", "Optional private deployment"],
      },
    ],
  },
  orderSteps: {
    title: "Get started in 3 steps",
    subtitle: "Transparent process, settled in USDT, ready to use instantly.",
    steps: [
      { title: "Pick a service", desc: "Choose the plan or combination that fits in solutions & pricing." },
      { title: "Confirm on Telegram", desc: "Add support to confirm needs, usage and the final quote." },
      { title: "Pay in USDT & go live", desc: "Verify the address, pay, and get your account provisioned fast." },
    ],
  },
  faq: {
    title: "FAQ",
    subtitle: "Still have questions? Reach our Telegram support directly.",
    items: [
      { q: "Why USDT only?", a: "Built for global and cross-border clients — USDT needs no cards, settles fast and works across borders. Large deals can arrange other methods with support." },
      { q: "Do you support private deployment?", a: "Yes. Chat aggregation and uncensored AI can both deploy to your own servers — data stays local with no cloud reporting." },
      { q: "How is your AI translation different from Google Translate?", a: "We use AI translation + chat tech that outputs native slang, local idioms and cultural tone — they can't tell you're foreign — unlike tools that wire up Google-style APIs and read stiff and literal." },
      { q: "Can AI close deals automatically? Can humans take over?", a: "Yes. AI works your persona 24/7 to engage, answer, follow up and convert; at key moments a human can take over in one click." },
      { q: "What input do voice cloning / face swap need?", a: "Voice cloning needs a short clear voice sample; face swap needs clear face images or video. Ensure you hold the rights." },
      { q: "What does uncensored AI mean?", a: "A privately deployed LLM with no cloud content-review reporting — outputs are not restricted by platform filters and data stays fully local." },
      { q: "Can I pay per usage?", a: "Yes. Most services offer both subscriptions and usage-based add-ons — pay for what you use, mix freely." },
      { q: "How do I avoid payment scams?", a: "Always verify the latest receiving address via our official Telegram support before paying. Never trust addresses forwarded by third parties." },
    ],
  },
  realtime: {
    badge: "Flagship · Deployment service",
    title: "Real-time Face + Voice Swap · Private Deployment Service",
    subtitle: "We deploy live-stream / video-call grade real-time face swap + voice cloning onto your own hardware and customize it to your scenario — you own the hardware, we handle spec advice, deployment, tuning, training and long-term support. Data stays fully private, off the public net, for any scenario.",
    videoNote: "Real-time swap demo · video coming soon",
    features: [
      { icon: "cpu", title: "Private & owned", desc: "Runs on your own hardware — data stays local, off the public net, fully yours." },
      { icon: "zap", title: "True real-time", desc: "Real-time face swap + synced voice change for live, calls and meetings, low latency." },
      { icon: "monitor", title: "Custom-fit", desc: "Deep-tuned to your platform and workflow — config, results and process all tailored." },
      { icon: "shield", title: "Long-term support", desc: "Docs + training, plus maintenance, upgrades and remote assistance anytime." },
    ],
    stepsTitle: "How we deliver",
    steps: [
      { title: "Consult & spec", desc: "Confirm your scenario, get a hardware spec list to purchase yourself." },
      { title: "Remote deploy", desc: "We install face swap / voice / digital human / private LLM on your device." },
      { title: "Tune to scenario", desc: "Optimize for your platform and workflow, with hands-on training." },
      { title: "Handover & support", desc: "Deliver docs and maintenance, with upgrades and tech support." },
    ],
    hardwareTitle: "Recommended hardware (you buy)",
    hardwareNote: "You purchase and fully own the hardware; we only provide spec advice and deployment service — no compute resale.",
    hardware: [
      { tier: "Entry", gpu: "RTX 4060Ti 16G / 4070", use: "Single face · 1080P live / video call" },
      { tier: "Pro", gpu: "RTX 4090 24G", use: "High-res / high FPS · multi-scenario · digital human" },
      { tier: "Flagship", gpu: "Dual-GPU / 48G+ (A6000, 4090×2)", use: "Multi-face · pro big screen · private LLM" },
    ],
    plansTitle: "Service packages & pricing",
    plansNote: "One-time deployment fee · settled in USDT · incl. setup, tuning & support",
    availability: "3 deployment slots open this week · by reservation",
    plans: [
      {
        name: "Basic deploy",
        tag: "Single",
        price: "from 980 USDT",
        unit: "one-time · incl. setup",
        specs: ["Face swap OR voice, your pick", "Remote deploy + basic tuning", "Hands-on training", "7-day support"],
        cta: "Ask on Telegram",
      },
      {
        name: "Creator all-in",
        tag: "Popular",
        price: "2580 USDT",
        unit: "one-time · incl. setup",
        specs: ["Face swap + voice + digital human", "Multi-scenario deep tuning", "Training + docs", "30-day support"],
        cta: "Ask on Telegram",
        highlight: true,
      },
      {
        name: "Everything",
        tag: "Full",
        price: "3980 USDT",
        unit: "one-time · incl. setup",
        specs: ["Face + voice + digital human", "No-limit private LLM", "Full scenario tuning", "30-day support + 1mo ops"],
        cta: "Ask on Telegram",
      },
    ],
    extrasTitle: "More services",
    extras: [
      "Custom development · quote from 1600",
      "On-site deployment · from 3000 + travel",
      "Maintenance subscription · 198/mo or 1998/yr",
      "Per-session remote help · 160/hour",
    ],
    capacityNote: "You own the hardware and your data stays private, off the public net; we provide end-to-end service from selection to deployment, customization and ops — for any scenario.",
    cta: "Customize on Telegram",
  },
  faceswap: {
    badge: "Free demo · Image",
    title: "Image Face Swap · Free Demo",
    subtitle: "Want a taste first? Upload a photo and turn into a pro headshot, superhero, or astronaut. (For real-time, see the flagship service above.)",
    tabCustom: "Custom Target",
    tabTemplate: "Templates",
    uploadFace: "Your Photo",
    uploadFaceHint: "A clear, well-lit front-facing photo works best",
    uploadTarget: "Target Image",
    uploadTargetHint: "Your face will be swapped into this image",
    pickTemplate: "Pick a template",
    templates: [
      { id: "business", name: "Business", file: "/templates/tpl-business.png" },
      { id: "hero", name: "Superhero", file: "/templates/tpl-hero.png" },
      { id: "astronaut", name: "Astronaut", file: "/templates/tpl-astronaut.png" },
      { id: "fashion", name: "Fashion", file: "/templates/tpl-fashion.png" },
    ],
    consent: "I confirm I have the legal rights to the uploaded photo and will not use it for any unlawful purpose.",
    button: "Swap Face",
    processing: "Swapping… usually 30–60s, please wait",
    resultTitle: "Result",
    download: "Download",
    again: "Try another",
    privacy: "Images are used only for this swap and not stored long-term.",
    errConfig: "The face swap service is being configured. Stay tuned.",
    errNoConsent: "Please confirm the portrait authorization first.",
    errNoImage: "Please upload a photo first.",
    errSize: "Image too large. Please upload an image under 8MB.",
    errGeneric: "Swap failed. Please try a clearer front-facing photo.",
  },
  about: {
    title: "Why BOUNDLESS",
    subtitle: "Faces you can see, conversations you can feel — self-built tech, global-friendly, privately controllable.",
    points: [
      { title: "Self-built tech", desc: "Voice / face / chat / deployment engines built in-house, not thin wrappers." },
      { title: "Global-friendly", desc: "Settle fully in USDT — no cards, instant onboarding for cross-border teams." },
      { title: "Private control", desc: "Private deployment supported, data stays local with no cloud reporting." },
      { title: "Fast delivery", desc: "Standard services run out of the box; custom needs get dedicated support." },
    ],
  },
  community: {
    badge: "Community",
    title: "Follow the channel · Join the group",
    subtitle: "The channel posts real cases, new features and limited-time deals first; join the group to chat with peers and grab an AI auto-closing trial and exclusive perks.",
    perks: ["Real cases & results", "New features + discounts", "Group trial · peer chat"],
    cta: "Follow channel",
    groupCta: "Join group",
  },
  gate: {
    badge: "Members only",
    title: "Follow + join to unlock perks",
    subtitle: "Follow the official channel and join the group to unlock an exclusive discount code and a free trial slot.",
    joinChannel: "① Follow channel",
    joinGroup: "② Join group",
    joinedChannel: "✅ Channel followed",
    joinedGroup: "✅ Group joined",
    verify: "I'm done — verify & unlock",
    checking: "Checking…",
    webNote: "Open this page inside Telegram (Mini App) to auto-verify and unlock.",
    unlockedTitle: "🎉 Perks unlocked",
    unlockedDesc: "Show your code to support for a discounted quote and a reserved free-trial slot.",
    codeLabel: "Your exclusive code",
    code: "HL-VIP",
    cta: "Reserve trial · contact support",
    notYet: "No follow/join detected yet — please complete both steps, then verify.",
  },
  swap: {
    before: "Original",
    after: "Swapped",
    dragHint: "Drag to compare before / after",
    liveTag: "Live swapping",
    hudEngine: "FACE SWAP ENGINE",
    hudFps: "32 FPS",
    hudLatency: "38ms",
    callStatus: "On call",
    you: "You · real",
    theySee: "They see",
    faceVoice: "Face + Voice",
    voiceCloning: "Cloning voice",
  },
  personas: {
    title: "Who are you? Start here",
    subtitle: "Every use case plays differently — find the one that fits you.",
    items: [
      { id: "streamer", title: "Streamer / Live", desc: "Real-time face & voice swap for live streams & video calls — deployed on your own device, go live in minutes.", cta: "See live swap", href: "#realtime" },
      { id: "ecom", title: "Cross-border / Support", desc: "Multi-account inbox + human-like AI translation + AI auto-closing that converts customers.", cta: "See AI closing", href: "#autochat" },
      { id: "creator", title: "Creator / MCN", desc: "Digital humans + voice cloning + video dubbing to scale content in batches.", cta: "See digital human", href: "#showcase" },
      { id: "enterprise", title: "Enterprise / Dev", desc: "Uncensored LLMs deployed privately — data stays local, fine-tunable and customizable.", cta: "See private deploy", href: "#showcase" },
    ],
  },
  showcaseSection: {
    title: "Capabilities you can see",
    subtitle: "Not just claims — every core capability comes with a real, interactive demo.",
  },
  chatDemo: {
    badge: "Chat · Live translation",
    title: "Chat Aggregation + Live Translation",
    desc: "Unify TG / LINE / WhatsApp / Messenger accounts in one inbox with instant two-way translation, AI auto-reply and human handoff anytime.",
    features: ["Multi-platform inbox", "Two-way translation", "AI auto-reply", "Handoff + knowledge base"],
    translatedTag: "Translated",
    replyName: "AI Assistant",
    typing: "typing…",
    messages: [
      { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México?", translated: "Do you ship to Mexico?" },
      { name: "Ayaka", flag: "🇯🇵", text: "在庫はまだありますか？", translated: "Is it still in stock?" },
    ],
    reply: { text: "Yes! We ship worldwide, 3–7 days.", translated: "Sent in the customer's language automatically." },
  },
  voiceDemo: {
    badge: "Voice · Cloning",
    title: "Voice Cloning",
    desc: "Clone any voice from a short clear sample, with multilingual TTS, real-time voice changing and voice-agent integration.",
    features: ["Instant cloning", "Multilingual TTS", "Real-time voice API", "Voice agent ready"],
    original: "Source sample",
    cloned: "Cloned result",
    langsLabel: "Multilingual",
    langs: ["中文", "English", "日本語", "한국어", "Español", "العربية"],
  },
  deployDemo: {
    badge: "Deploy · No-limit private",
    title: "Uncensored · No-limit AI · Private Deploy",
    desc: "An LLM with no content censorship and no topic restrictions, deployed on your own servers: never uploaded to the public internet, data never leaves, outputs unrestricted by platform filters, freely fine-tunable.",
    features: ["No content filtering", "No topic limits", "Private, off the public net", "Free fine-tuning"],
    cloudLabel: "Public Cloud API",
    localLabel: "Your private deploy",
    rows: [
      { label: "Data stays fully local", cloud: false, local: true },
      { label: "No content review / blocking", cloud: false, local: true },
      { label: "No cloud-side logging", cloud: false, local: true },
      { label: "Fine-tune to your business", cloud: false, local: true },
      { label: "Fully self-controlled", cloud: false, local: true },
    ],
  },
  digitalDemo: {
    badge: "Combo · Digital twin",
    title: "Digital Human / Virtual Streamer",
    desc: "Voice cloning + face swap + lip sync to spin up your own digital twin — 24/7 live selling and batch talking-head videos.",
    features: ["Cloned face + voice", "Lip sync", "Live streaming avatar", "Talking-head videos"],
    tags: ["Lip sync", "Voice clone", "Multi-lang dub", "Permanent avatar"],
  },
  compare: {
    badge: "Why us",
    title: "Us vs the rest",
    subtitle: "Same chat aggregation — the difference is whether it closes.",
    cols: ["BOUNDLESS AI", "Ordinary aggregator", "Pure human team"],
    rows: [
      { label: "Translation quality", us: "Human-like · slang & idioms", them: "Google-style literal · stiff", manual: "Varies by person" },
      { label: "Auto-closing", us: "AI pushes the sale & converts", them: "Not supported", manual: "Skill-based · misses leads" },
      { label: "Persona voice", us: "Text → persona voice", them: "None", manual: "None" },
      { label: "24/7 online", us: "Never misses a lead", them: "Partial", manual: "Limited by hours" },
      { label: "Multi-platform inbox", us: "Unified inbox", them: "Supported", manual: "Manual switching" },
      { label: "Private deploy · off the net", us: "Supported", them: "Mostly cloud", manual: "—" },
      { label: "Scaling cost", us: "Low · 1 person = a team", them: "Medium", manual: "High · grows with headcount" },
    ],
  },
  autochat: {
    badge: "Flagship · AI auto-closing",
    title: "AI Auto-Closing Chat System",
    subtitle:
      "Beyond aggregation and translation — AI works your persona to greet, answer, follow up, push the sale and convert customers 24/7, across languages, at human-level quality. Most tools wire up Google-style translation APIs that read stiff and blow your cover; we use AI translation + chat that sounds native and actually closes.",
    features: [
      { icon: "languages", title: "Human-like AI translation", desc: "Native slang, local idioms and cultural tone — they can't tell you're foreign. No more stiff, literal Google output." },
      { icon: "bot", title: "AI that closes", desc: "Context-aware: answers, follows up, guides the order and converts — 24/7, never misses a lead, humans can take over anytime." },
      { icon: "mic", title: "Persona voice chat", desc: "Turn AI replies into your persona's voice — voice messages and voice chat that feel real, build trust and close faster." },
      { icon: "inbox", title: "Multi-platform inbox", desc: "TG / LINE / WhatsApp / Messenger, many accounts in one inbox — operate at scale." },
    ],
    compareTitle: "Human-like AI translation vs ordinary translation",
    compareNote: "Same sentence, obvious gap — ordinary translation gives you away; ours reads like a local.",
    badLabel: "Ordinary · Google-style",
    goodLabel: "Ours · AI human-like",
    compare: [
      {
        src: "¿Está disponible? lo quiero ya jaja",
        bad: "Is it available? I want it now haha",
        good: "Still in stock? I want one right now lol",
      },
      {
        src: "我们给你包邮，今天下单还送小礼物",
        bad: "We give you free shipping, today order also send small gift",
        good: "Free shipping on us — order today and grab a free gift 🎁",
      },
    ],
    scenariosLabel: "Use cases",
    scenarios: ["Cross-border e-com", "Private-domain support", "Lead closing", "Global communities"],
    cta: "Ask about AI closing on Telegram",
    demo: {
      inbox: "Unified inbox · AI auto-closing",
      personaName: "Your persona AI",
      translatedTag: "AI human-like",
      autoTag: "Auto-close",
      voiceTag: "Persona voice",
      typing: "AI replying in persona…",
      incoming: { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México? precio?", translated: "Do you ship to Mexico? Price?" },
      reply: {
        text: "¡Claro! Envío a México en 5-7 días 🚀 Hoy con 10% OFF, ¿te lo aparto?",
        translated: "Sure! 5-7 days to Mexico 🚀 10% off today — want me to reserve one for you?",
      },
      voiceLen: "0:08 · persona voice",
    },
  },
  engage: {
    badge: "Engagement · flexible & win-win",
    title: "Three ways to work with us",
    subtitle: "Whether you already own hardware, want a fully managed setup, or want to invest and share returns — we deliver. You own the hardware, data stays private off the net, settled in USDT.",
    selectorTitle: "Which fits you?",
    youLabel: "You handle",
    weLabel: "We handle",
    selector: [
      { id: "service", label: "I have hardware · need delivery" },
      { id: "managed", label: "I want it fully managed" },
      { id: "invest", label: "I want to invest · share returns" },
    ],
    models: [
      {
        id: "service",
        badge: "Most control",
        name: "Private Deployment Service",
        tagline: "Your hardware, we make it work",
        you: "Buy hardware · provide space",
        we: "Spec advice + deploy + customize + train + support",
        price: "one-time from 980 USDT",
        priceNote: "incl. 3 deploy tiers · ops add-on 198 / mo",
        points: ["100% private, off the net", "Deeply tailored to your scenario", "Docs + hands-on training", "7–30 days tech support"],
        cta: "Ask on Telegram",
      },
      {
        id: "managed",
        badge: "Most hassle-free",
        name: "Turnkey · Fully Managed",
        tagline: "Hardware + datacenter + ops on us, you just use it",
        you: "Bring needs · pay monthly",
        we: "Hardware procurement + hosting + deploy + 24/7 ops + upgrades",
        price: "from 1980 USDT / mo",
        priceNote: "incl. hosting + ops · hardware at cost",
        points: ["Zero ops, always online", "24/7 monitoring & upgrades", "Elastic scaling on demand", "All-in price, worry-free"],
        cta: "Ask on Telegram",
        highlight: true,
      },
      {
        id: "invest",
        badge: "Highest return",
        name: "Datacenter Investment & Revenue Share",
        tagline: "You invest, we operate, monthly dividends",
        you: "Fund nodes / buy GPUs",
        we: "Delivery + operations + client acquisition + 24/7 ops",
        price: "revenue share · from 20,000 USDT",
        priceNote: "net profit split 70 / 30 (investor majority)",
        points: ["We operate it end-to-end", "Net profit split in your favor", "Shared risk, transparent monthly settlement", "Contract-based, clear responsibilities"],
        cta: "Discuss on Telegram",
      },
    ],
    serviceTiersLabel: "Three deploy packages (one-time)",
    extrasLabel: "More optional services",
    invest: {
      roiTitle: "Example · standard 50,000 USDT (full-load estimate)",
      roiRows: [
        { label: "Est. monthly revenue", value: "8,000 – 12,000 USDT" },
        { label: "Est. net profit after cost", value: "6,000 – 9,000 USDT" },
        { label: "Investor monthly share (70%)", value: "4,200 – 6,300 USDT" },
        { label: "Est. payback period", value: "~9 – 13 months" },
      ],
      roiNote: "Ideal full-load estimate; actual results depend on utilization, market and FX — not a guarantee of returns.",
      flowTitle: "How it works",
      flow: ["Discuss & assess", "Sign · define split & duties", "Fund & build nodes (we can procure)", "Operate · monthly dividend settlement"],
      compliance: "Hardware belongs to the investor; lawful, compliant use only, per local regulations.",
    },
    matrixTitle: "Compare the three",
    matrixCols: ["Deployment Service", "Turnkey Managed", "Investment Share"],
    matrix: [
      { label: "Hardware purchase", a: "You", b: "We procure", c: "You fund" },
      { label: "Datacenter / space", a: "You", b: "We", c: "Co-built" },
      { label: "Deploy & tune", a: "We", b: "We", c: "We" },
      { label: "Day-to-day ops", a: "Optional", b: "We 24/7", c: "We" },
      { label: "Client acquisition", a: "—", b: "—", c: "We" },
      { label: "Billing", a: "One-time", b: "Monthly", c: "Revenue share" },
      { label: "Best for", a: "Self-reliant", b: "Hassle-free", c: "Investment return" },
    ],
  },
  roi: {
    badge: "Calculate · how much more you'd earn",
    title: "AI Closing ROI Calculator",
    subtitle: "Drag a few numbers to estimate the labor you save and revenue you gain each month with AI auto-closing.",
    inputs: { agents: "Current support agents", salary: "Salary per agent", leads: "Daily new inquiries", aov: "Avg order value", conv: "Current conversion" },
    units: { agents: "ppl", salary: "USDT", leads: "/day", aov: "USDT", conv: "%" },
    resultSaveLabel: "Labor cost saved / mo",
    resultRevenueLabel: "Conversion uplift / mo",
    resultNetLabel: "Net gain / mo",
    resultRoiLabel: "Return on spend",
    resultYearLabel: "Annualized net (est.)",
    planLabel: "Suggested plan",
    perMonth: "/ mo",
    assumptionsTitle: "Assumptions (tune with support to your reality)",
    assumptions: [
      "AI auto-closing optimizes ~60% of repetitive labor cost",
      "Human-like translation + 24/7 lifts conversion by ~35% relative",
      "Estimated over 30 days using your AOV and conversion",
    ],
    disclaimer: "An estimate model based on industry experience; actual results vary by industry, traffic and operations — not a guarantee of returns.",
    cta: "Get a plan with my numbers",
  },
  cases: {
    badge: "Real results · see it",
    title: "Client results & multi-language in action",
    subtitle: "From live-stream face swap to AI multi-language auto-closing — real plays and chat logs across scenarios.",
    items: [
      { scene: "Cross-border e-com", metric: "+38%", metricLabel: "Conversion", quote: "With multi-language AI auto-closing, we stopped missing night and cross-timezone orders.", name: "Leo", role: "Cross-border e-com · Ops Lead", img: "/showcase/digital-human.png" },
      { scene: "Live / video call", metric: "0 lag", metricLabel: "Real-time face & voice", quote: "Rock-solid live calls — viewers can't tell the face and voice are swapped.", name: "Aya", role: "Full-time streamer", img: "/showcase/live-after.png" },
      { scene: "Private-domain support", metric: "1 = 6", metricLabel: "Labor efficiency", quote: "One person runs six accounts, replies sound more native than a human — no more late nights.", name: "Mia", role: "Private-domain support · Lead", img: "/showcase/live-before.png" },
    ],
    galleryTitle: "Multi-language · human-like closing logs",
    gallerySubtitle: "One AI engaging customers worldwide — translation that hides your origin, replies that push the sale.",
    translatedTag: "AI human-like",
    replyTag: "AI auto-close",
    gallery: [
      { lang: "Español", flag: "🇪🇸", incoming: "¿Tienen envío a Chile? 🇨🇱", translated: "Do you ship to Chile?", reply: "¡Sí! Llega en 7-10 días 🚀 Hoy 10% OFF, ¿te lo aparto?" },
      { lang: "Português", flag: "🇧🇷", incoming: "Quanto custa? quero comprar agora", translated: "How much? I want to buy now", reply: "Sai por R$199 com frete grátis hoje 🎁 Reservo pra você?" },
      { lang: "العربية", flag: "🇸🇦", incoming: "هل المنتج متوفر؟", translated: "Is the product available?", reply: "نعم متوفر ✅ شحن خلال ٥ أيام، وخصم ١٠٪ اليوم" },
      { lang: "ไทย", flag: "🇹🇭", incoming: "สนใจค่ะ ราคาเท่าไหร่", translated: "Interested, how much?", reply: "ราคา 990 บาท ส่งฟรีวันนี้ 🎉 รับเลยไหมคะ" },
    ],
    disclaimer: "Case figures are from client feedback and internal stats, for reference; chats are illustrative of multi-language capability.",
  },
  lead: {
    title: "Not ready to open Telegram? Leave your contact",
    subtitle: "Drop your needs and we'll reach out within 5 minutes with a plan and quote for your scenario.",
    name: "Name",
    contact: "Contact",
    interest: "Interested in",
    message: "Notes (optional)",
    namePh: "What should we call you",
    contactPh: "Telegram / WhatsApp / email",
    messagePh: "Briefly describe your scenario, platform or goal…",
    interests: ["Real-time face & voice swap", "AI auto-closing chat", "Private deployment", "Turnkey (we supply hardware & DC)", "Investment / revenue share", "Other"],
    submit: "Submit — have support reach me",
    submitting: "Submitting…",
    successTitle: "Got it — reaching out shortly ✅",
    successDesc: "We'll contact you within 5 minutes via what you left; for urgent needs tap Telegram above.",
    error: "Submit failed, please retry or contact us on Telegram.",
    contactInvalid: "Please enter a valid contact (Telegram / WhatsApp / email).",
    privacy: "Your info is used only for this inquiry and never shared.",
  },
  contact: {
    title: "Contact & Order",
    subtitle: "Add our Telegram support, confirm your needs, settle in USDT.",
    telegram: "Telegram Support",
    telegramHandle: "@ai_zkw",
    scanHint: "Scan to chat on mobile",
    usdt: "USDT Payment",
    usdtNote: "Always verify the latest receiving address with support before paying. Beware of scams.",
    networks: "TRC20 / ERC20 supported (TRC20 recommended, low fees). Confirm the address with support before paying.",
    responseTime: "Support replies in ~5 min · online 24/7",
    compliance: "Compliance & Privacy",
    complianceNote: "For your own lawfully authorized use only; no impersonation, fraud or infringement. Materials are deleted after the session, not stored long-term.",
    cta: "Contact on Telegram",
  },
  footer: {
    rights: "BOUNDLESS · All rights reserved",
    disclaimerTitle: "Authorization & Disclaimer",
    disclaimer:
      "Services are for lawful use only. Users must hold full rights to any likeness, voice or content used, and must not use the services for fraud, forgery, infringement or any illegal activity. Use implies acceptance of full legal responsibility.",
    links: ["Solutions", "Pricing", "About", "Contact"],
  },
};

export const content: Record<Lang, Dict> = { zh, en };
