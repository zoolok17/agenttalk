import * as React from 'react';

export type AgentState =
  | 'working_turn'
  | 'working_silent'
  | 'idle_waiting'
  | 'stuck_suspected'
  | 'rate_limited_or_outage'
  | 'degraded_output'
  | 'crashed_or_exited'
  | 'unknown';

export interface StatusDotProps {
  /** agenttalk health state. Drives color + whether it pulses. */
  state?: AgentState;
  /** Diameter in px. Default 10. */
  size?: number;
  /** Soft-color halo ring for header/hero use. */
  ring?: boolean;
  style?: React.CSSProperties;
}

/** Atomic agent-health indicator; `working_turn` breathes, others are steady. */
export function StatusDot(props: StatusDotProps): JSX.Element;
