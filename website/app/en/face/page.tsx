import ProductLanding from "@/components/ProductLanding";
import { landingMetadata, landingJsonLd, landingFaqJsonLd } from "@/lib/landingMeta";

export const metadata = landingMetadata("face", "en");

export default function FaceLandingEn() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingJsonLd("face", "en")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(landingFaqJsonLd("face", "en")) }}
      />
      <ProductLanding product="face" />
    </>
  );
}
