import * as React from 'react';

export interface CliBadgeProps {
  /** Which coding-CLI the agent runs. Default 'claude'. */
  cli?: 'claude' | 'codex';
  style?: React.CSSProperties;
}

/** Uppercase mono badge marking an agent's CLI runtime (Claude / Codex). */
export function CliBadge(props: CliBadgeProps): JSX.Element;
