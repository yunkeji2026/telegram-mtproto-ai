import ProductLanding from "@/components/ProductLanding";
import { landingMetadata, landingJsonLd, landingFaqJsonLd } from "@/lib/landingMeta";

export const metadata = landingMetadata("voice", "zh");

export default function VoiceLanding() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingJsonLd("voice", "zh")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingFaqJsonLd("voice", "zh")) }}
      />
      <ProductLanding product="voice" />
    </>
  );
}
