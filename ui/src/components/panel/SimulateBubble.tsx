interface SimulateBubbleProps {
  label: string;
  onSimulate: () => void;
  className?: string;
}

export function SimulateBubble({ label, onSimulate, className }: SimulateBubbleProps) {
  return (
    <button
      type="button"
      className={className ? `simulate-bubble ${className}` : 'simulate-bubble'}
      onClick={onSimulate}
    >
      {label}
    </button>
  );
}
