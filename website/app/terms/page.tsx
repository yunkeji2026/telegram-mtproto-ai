import type { Metadata } from "next";
import LegalShell, { type LegalSection } from "@/components/LegalShell";

export const metadata: Metadata = {
  title: "服务条款 Terms of Service · 华灵科技 HuaLing Tech",
  description: "华灵科技 HuaLing Tech 服务条款：服务范围、USDT 结算、使用规范与免责声明。",
  alternates: { canonical: "/terms" },
  robots: { index: true, follow: true },
};

const UPDATED = "2026-06-13";

const sections: LegalSection[] = [
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

export default function TermsPage() {
  return (
    <LegalShell
      title={{ zh: "服务条款", en: "Terms of Service" }}
      updated={UPDATED}
      sections={sections}
    />
  );
}
