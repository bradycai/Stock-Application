import ReviewsSection from "../components/performance/ReviewsSection";
import MetricsSection from "../components/performance/MetricsSection";
import BacktestSection from "../components/performance/BacktestSection";

export default function PerformancePage() {
  return (
    <div className="page-stack">
      <h2>Performance</h2>
      <p className="muted section-note">
        Read-only reporting from the journal. Nothing on this page places orders or changes settings.
      </p>
      <ReviewsSection />
      <MetricsSection />
      <BacktestSection />
    </div>
  );
}
