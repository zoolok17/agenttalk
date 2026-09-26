import * as React from 'react';

export type AttentionSource = 'escalation' | 'gate' | 'stuck' | 'deadletter' | 'supervisor';
export type Severity = 'high' | 'med' | 'low';

export interface AttentionAction {
  label: string;
  variant?: 'primary' | 'ghost' | 'subtle';
  onClick?: () => void;
}
export interface AttentionItemProps {
  source?: AttentionSource;
  severity?: Severity;
  title: React.ReactNode;
  /** Agent id chip. */
  agent?: string;
  /** One-line supporting detail. */
  detail?: React.ReactNode;
  /** Preformatted age (rendered as "<age> ago"). */
  age?: string;
  /** Action cluster; first defaults to primary, rest ghost. */
  actions?: AttentionAction[];
  style?: React.CSSProperties;
}

/**
 * One row of the "Needs a human" queue: severity spine, source tag, title,
 * agent + detail, and an action cluster.
 * @startingPoint section="Patterns" subtitle="Attention / escalation queue item" viewport="700x110"
 */
export function AttentionItem(props: AttentionItemProps): JSX.Element;
