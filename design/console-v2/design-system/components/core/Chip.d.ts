import * as React from 'react';

export type ChipTone =
  | 'neutral' | 'accent' | 'ok' | 'info' | 'warn'
  | 'attn' | 'danger' | 'violet' | 'teal' | 'gray';

export interface ChipProps {
  /** Semantic color. Default 'neutral'. */
  tone?: ChipTone;
  /** Filled pill with white text — use for source tags (ESCALATION, GATE HOLD). */
  solid?: boolean;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}

/**
 * Universal metadata pill: status labels, message kinds, severities,
 * thread states all render as a Chip with the matching tone.
 * @startingPoint section="Core" subtitle="Status / kind / severity pill" viewport="360x120"
 */
export function Chip(props: ChipProps): JSX.Element;
