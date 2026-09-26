import * as React from 'react';
import type { AgentState } from '../core/StatusDot';

export interface AvatarProps {
  /** Image URL (e.g. `avatars/claude-lead.png`). Missing/broken → status-dot fallback. */
  src?: string;
  alt?: string;
  /** Agent health — drives the badge color + pulse. */
  state?: AgentState;
  /** Diameter in px. Default 40. */
  size?: number;
  /** Show the status-dot badge. Default true. */
  badge?: boolean;
  style?: React.CSSProperties;
}

/**
 * Agent portrait + live status badge. Use everywhere an agent appears
 * (cards, detail header, graph nodes, chat header).
 * @startingPoint section="Patterns" subtitle="Agent avatar + status badge" viewport="120x120"
 */
export function Avatar(props: AvatarProps): JSX.Element;
