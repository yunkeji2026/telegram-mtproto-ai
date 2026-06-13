import Navbar from "@/components/Navbar";
import SectionNav from "@/components/SectionNav";
import Hero from "@/components/Hero";
import TrustBar from "@/components/TrustBar";
import Personas from "@/components/Personas";
import Compare from "@/components/Compare";
import AutoChat from "@/components/AutoChat";
import RealtimeSwap from "@/components/RealtimeSwap";
import Showcase from "@/components/Showcase";
import Cases from "@/components/Cases";
import EngagementModels from "@/components/EngagementModels";
import Pricing from "@/components/Pricing";
import OrderSteps from "@/components/OrderSteps";
import About from "@/components/About";
import Faq from "@/components/Faq";
import Community from "@/components/Community";
import UnlockGate from "@/components/UnlockGate";
import Contact from "@/components/Contact";
import Footer from "@/components/Footer";

/** Shared marketing homepage tree, rendered at both `/` (zh) and `/en` (en).
 *  Locale is driven by the route via LanguageProvider, so this stays presentational. */
export default function SiteHome() {
  return (
    <main className="relative min-h-screen">
      <Navbar />
      <SectionNav />
      <Hero />
      <TrustBar />
      <Personas />
      <Compare />
      <AutoChat />
      <RealtimeSwap />
      <Showcase />
      <Cases />
      <EngagementModels />
      <Pricing />
      <OrderSteps />
      <About />
      <Faq />
      <Community />
      <UnlockGate />
      <Contact />
      <Footer />
    </main>
  );
}
