interface BrandLockupProps {
  compact?: boolean;
}

export default function BrandLockup({ compact = false }: BrandLockupProps) {
  return (
    <div className={`brand-lockup${compact ? " brand-lockup--compact" : ""}`}>
      <img src="/foodlog-mark.svg" alt="" aria-hidden="true" />
      <span>Gemini FoodLog</span>
    </div>
  );
}
