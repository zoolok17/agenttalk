import * as React from 'react';
import type { ChipTone } from '../core/Chip';

export interface MessageBubbleProps {
  /** Sender id shown in the bubble header. */
  from?: string;
  body: React.ReactNode;
  /** Right-align in accent (the operator's own messages). */
  mine?: boolean;
  cli?: 'claude' | 'codex' | null;
  /** Message-kind label (review-request, note, wake…). */
  kind?: string | null;
  kindTone?: ChipTone;
  /** Preformatted relative age. */
  age?: string | null;
  /** Mono footer, e.g. "status=approved · head 7b2d9c1". */
  meta?: React.ReactNode;
  style?: React.CSSProperties;
}

/** One transcript/chat turn. Bodies render pre-wrapped plain text, never parsed markdown. */
export function MessageBubble(props: MessageBubbleProps): JSX.Element;
