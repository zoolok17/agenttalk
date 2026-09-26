import * as React from 'react';

export interface MeterProps {
  /** Short label, e.g. "RATE", "Context window". */
  label: React.ReactNode;
  /** 0–100. Fill color is automatic by threshold. */
  value?: number;
  /** 'sm' = inline card meter, 'lg' = detail-panel meter. Default 'sm'. */
  size?: 'sm' | 'lg';
  /** Optional caption under the bar. */
  note?: React.ReactNode;
  style?: React.CSSProperties;
}

/** Labeled capacity bar (rate limit / context). Green <60, amber 60–84, red ≥85. */
export function Meter(props: MeterProps): JSX.Element;
