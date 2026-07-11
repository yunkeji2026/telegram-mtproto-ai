import ProductLanding from "@/components/ProductLanding";
import { landingMetadata, landingJsonLd, landingFaqJsonLd } from "@/lib/landingMeta";

export const metadata = landingMetadata("asset-safe", "en");

export default function AssetSafeLandingEn() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingJsonLd("asset-safe", "en")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingFaqJsonLd("asset-safe", "en")) }}
      />
      <ProductLanding product="asset-safe" />
    </>
  );
}
