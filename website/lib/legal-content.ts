import type { LegalSection } from "@/components/LegalShell";

export const LEGAL_UPDATED = "2026-06-13";

export const privacyTitle = { zh: "隐私政策", en: "Privacy Policy" };
export const termsTitle = { zh: "服务条款", en: "Terms of Service" };

export const privacySections: LegalSection[] = [
  {
    h: { zh: "我们收集的信息", en: "Information we collect" },
    p: {
      zh: [
        "咨询/留资信息：你主动提交的称呼、联系方式（Telegram / WhatsApp / 邮箱等）、咨询意向与备注。",
        "使用数据：匿名的页面浏览、按钮点击等统计事件，用于了解站点使用情况并改进产品。",
        "技术数据：访问时的 IP（截断保存）、浏览器 User-Agent、来源页等，用于安全与防滥用。",
        "Telegram 信息：当你通过 Telegram Mini App 访问时，我们会读取 Telegram 提供的基础身份（如用户 ID、用户名）以完成验证与服务。",
      ],
      en: [
        "Inquiry/lead data: the name, contact (Telegram / WhatsApp / email, etc.), interest and notes you choose to submit.",
        "Usage data: anonymous events such as page views and button clicks, used to understand usage and improve the product.",
        "Technical data: truncated IP, browser user-agent and referrer at access time, for security and abuse prevention.",
        "Telegram data: when you access via the Telegram Mini App, we read basic identity (e.g. user ID, username) provided by Telegram to complete verification and service.",
      ],
    },
  },
  {
    h: { zh: "Cookie 与本地存储", en: "Cookies & local storage" },
    p: {
      zh: [
        "我们使用浏览器本地存储记住你的语言偏好与 Cookie 同意状态。",
        "管理后台使用一个仅限服务端读取（httpOnly）的会话 Cookie 用于登录，不用于追踪访客。",
        "我们不使用第三方广告 Cookie。",
      ],
      en: [
        "We use browser local storage to remember your language preference and cookie-consent choice.",
        "The admin dashboard uses one httpOnly session cookie for login only; it is not used to track visitors.",
        "We do not use third-party advertising cookies.",
      ],
    },
  },
  {
    h: { zh: "我们如何使用信息", en: "How we use information" },
    p: {
      zh: [
        "回应你的咨询、提供报价与方案、交付与支持你购买的服务。",
        "运营与改进网站、衡量内容与活动效果。",
        "保障安全、防止欺诈与滥用，遵守适用的法律义务。",
      ],
      en: [
        "Respond to inquiries, provide quotes and proposals, and deliver and support the services you purchase.",
        "Operate and improve the site, and measure content and campaign performance.",
        "Ensure security, prevent fraud and abuse, and comply with applicable legal obligations.",
      ],
    },
  },
  {
    h: { zh: "第三方服务", en: "Third-party services" },
    p: {
      zh: [
        "Telegram：用于客服沟通、机器人与 Mini App。你与我们的对话受 Telegram 隐私政策约束。",
        "AI 模型服务商：站内 AI 问答会将你的提问内容发送给第三方大模型 API 以生成回复，请勿在对话中提交敏感个人信息。",
        "我们不会将你的联系方式出售给第三方。",
      ],
      en: [
        "Telegram: used for support chat, the bot and the Mini App. Your conversations with us are subject to Telegram's privacy policy.",
        "AI model provider: the on-site AI assistant sends your questions to a third-party large-language-model API to generate replies; please do not submit sensitive personal data in the chat.",
        "We do not sell your contact details to third parties.",
      ],
    },
  },
  {
    h: { zh: "数据留存与你的权利", en: "Retention & your rights" },
    p: {
      zh: [
        "我们仅在为上述目的所必需的期间内保留你的数据。",
        "你可以请求查询、更正或删除你的个人信息——通过下方联系方式联系我们即可。",
      ],
      en: [
        "We retain your data only for as long as necessary for the purposes above.",
        "You may request access to, correction of, or deletion of your personal data — contact us via the details below.",
      ],
    },
  },
  {
    h: { zh: "联系我们", en: "Contact us" },
    p: {
      zh: ["隐私相关问题请通过 Telegram 客服 @ai_zkw 联系我们。"],
      en: ["For privacy questions, contact our Telegram support @ai_zkw."],
    },
  },
];

export const termsSections: LegalSection[] = [
  {
    h: { zh: "服务说明", en: "Service description" },
    p: {
      zh: [
        "华灵科技 HuaLing Tech 提供 AI 技术服务，旗下华影 LiveAvatar（实时换脸换声、声音克隆、数字人、视频翻译配音）与灵犀 SoulSync（AI 自动成交聊天、多语种拟人翻译、AI 伴侣），以及华灵 Engine 私有化部署等的选型、部署、定制与支持。",
        "具体交付内容、规格与时效以双方在下单沟通中确认的方案为准。",
      ],
      en: [
        "HuaLing Tech provides AI technical services across HuaYing LiveAvatar (real-time face & voice swap, voice cloning, digital humans, video dubbing) and LingXi SoulSync (AI auto-closing chat, human-like multi-language translation, AI companion), plus HuaLing Engine private deployment — including selection, deployment, customization and support.",
        "The exact deliverables, specifications and timelines are those confirmed between both parties during the ordering conversation.",
      ],
    },
  },
  {
    h: { zh: "结算与付款", en: "Billing & payment" },
    p: {
      zh: [
        "服务以 USDT 结算（支持 TRC20 / ERC20）。具体金额、周期与里程碑在下单时确认。",
        "除另有书面约定外，已开始交付或已部署的定制服务不予退款。",
      ],
      en: [
        "Services are settled in USDT (TRC20 / ERC20 supported). Amounts, cycles and milestones are confirmed at ordering.",
        "Unless otherwise agreed in writing, customized services that have begun delivery or been deployed are non-refundable.",
      ],
    },
  },
  {
    h: { zh: "可接受使用", en: "Acceptable use" },
    p: {
      zh: [
        "你须确保对所提供的素材（人脸、声音、数据等）拥有合法授权，并对其使用承担责任。",
        "你不得将我们的服务用于违反所在地法律法规的用途，包括但不限于诈骗、冒充、侵犯他人肖像/声音/隐私权等。",
        "因你违规使用导致的一切后果由你自行承担。",
      ],
      en: [
        "You must ensure you hold lawful authorization for any material you provide (faces, voices, data, etc.) and are responsible for its use.",
        "You must not use our services for purposes that violate the laws of your jurisdiction, including but not limited to fraud, impersonation, or infringement of others' likeness/voice/privacy rights.",
        "You bear all consequences arising from your non-compliant use.",
      ],
    },
  },
  {
    h: { zh: "免责声明", en: "Disclaimer" },
    p: {
      zh: [
        "服务按“现状”提供。在适用法律允许的最大范围内，我们不对因使用或无法使用服务而产生的间接或后果性损失承担责任。",
        "示例数据、案例与 ROI 测算仅供参考，不构成对具体业务结果的承诺或保证。",
      ],
      en: [
        "Services are provided “as is”. To the maximum extent permitted by law, we are not liable for indirect or consequential losses arising from use or inability to use the services.",
        "Sample data, cases and ROI estimates are for reference only and do not constitute a promise or guarantee of specific business results.",
      ],
    },
  },
  {
    h: { zh: "条款变更与联系", en: "Changes & contact" },
    p: {
      zh: [
        "我们可能不时更新本条款，更新后在本页公布即生效。",
        "如有疑问，请通过 Telegram 客服 @ai_zkw 联系我们。",
      ],
      en: [
        "We may update these terms from time to time; updates take effect once posted on this page.",
        "For questions, contact our Telegram support @ai_zkw.",
      ],
    },
  },
];
