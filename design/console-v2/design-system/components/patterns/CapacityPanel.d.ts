import * as React from 'react';

export interface CapacityPanelProps {
  /** 5-hour rate-limit usage, 0–100. */
  rate?: number;
  /** Context-window usage, 0–100. */
  context?: number;
  /** Override the auto note under the rate meter. */
  rateNote?: React.ReactNode;
  /** Override the auto note under the context meter. */
  contextNote?: React.ReactNode;
  style?: React.CSSProperties;
}

/**
 * Agent capacity card: rate-limit + context-window meters with advisory notes.
 * @startingPoint section="Patterns" subtitle="Agent capacity meters" viewport="306x180"
 */
export function CapacityPanel(props: CapacityPanelProps): JSX.Element;
