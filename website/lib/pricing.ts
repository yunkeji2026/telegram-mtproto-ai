// Canonical price figures, single source of truth.
//
// These are the headline SKU numbers surfaced in structured data (schema.org
// JSON-LD in app/layout.tsx) and in headline marketing copy. The localized
// display strings in lib/content.ts (e.g. "980 起", "198 / 月") are presentation
// formats of these same numbers — when a price changes, update it HERE and keep
// the content.ts display strings in sync.

export type PriceUnit = "one-time" | "month";

export interface PriceOffer {
  id: string;
  name: string;
  /** Numeric string (no currency / suffix) so it is valid for schema.org Offer.price. */
  price: string;
  currency: "USDT";
  unit: PriceUnit;
  description: string;
}

/** Real-time face & voice swap — private deployment. */
export const realtimeOffers: PriceOffer[] = [
  {
    id: "realtime-basic",
    name: "Basic deployment",
    price: "980",
    currency: "USDT",
    unit: "one-time",
    description:
      "One-time; real-time face swap OR voice clone, remote deploy + tuning + training + support.",
  },
  {
    id: "realtime-creator",
    name: "Creator all-in deployment",
    price: "2580",
    currency: "USDT",
    unit: "one-time",
    description:
      "One-time; face swap + voice + digital human, multi-scenario deep tuning, 30-day support.",
  },
];

/** AI auto-closing chat system — subscription. */
export const autochatOffers: PriceOffer[] = [
  {
    id: "autochat-team",
    name: "Team",
    price: "198",
    currency: "USDT",
    unit: "month",
    description: "Per month; 10 chat accounts, all platforms, AI auto-closing replies.",
  },
  {
    id: "autochat-flagship",
    name: "Flagship",
    price: "598",
    currency: "USDT",
    unit: "month",
    description: "Per month; 50 accounts, human handoff, dashboard, persona voice.",
  },
];

/** Map a PriceOffer to a schema.org Offer node. */
export function toSchemaOffer(o: PriceOffer) {
  return {
    "@type": "Offer",
    name: o.name,
    price: o.price,
    priceCurrency: o.currency,
    description: o.description,
  };
}
