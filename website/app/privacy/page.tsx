import type { Metadata } from "next";
import LegalShell, { type LegalSection } from "@/components/LegalShell";

export const metadata: Metadata = {
  title: "隐私政策 Privacy Policy · 华灵科技 HuaLing Tech",
  description: "华灵科技 HuaLing Tech 隐私政策：我们收集哪些数据、如何使用、第三方与你的权利。",
  alternates: { canonical: "/privacy" },
  robots: { index: true, follow: true },
};

const UPDATED = "2026-06-13";

const sections: LegalSection[] = [
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

export default function PrivacyPage() {
  return (
    <LegalShell
      title={{ zh: "隐私政策", en: "Privacy Policy" }}
      updated={UPDATED}
      sections={sections}
    />
  );
}
