import * as React from 'react';
import type { AgentState } from '../core/StatusDot';

export interface Agent {
  id: string;
  cli?: 'claude' | 'codex';
  role?: string;
  group?: string;
  state?: AgentState;
  task?: string;
  /** Preformatted heartbeat age, e.g. "6m 12s" or "no hb". */
  age?: string;
  rate?: number;
  ctx?: number;
  wrapped?: boolean;
}

export interface AgentCardProps {
  agent: Agent;
  onClick?: () => void;
  style?: React.CSSProperties;
}

/**
 * Roster tile: identity + current task + health chip + heartbeat age +
 * rate/context meters, with a colored status spine.
 * @startingPoint section="Patterns" subtitle="Live agent roster tile" viewport="280x220"
 */
export function AgentCard(props: AgentCardProps): JSX.Element;
