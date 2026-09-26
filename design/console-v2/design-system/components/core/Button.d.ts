import * as React from 'react';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Default 'primary'. */
  variant?: 'primary' | 'ghost' | 'subtle';
  /** Default 'md'. */
  size?: 'sm' | 'md';
  disabled?: boolean;
  /** Optional leading icon (an inline SVG element). */
  icon?: React.ReactNode;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}

/**
 * Primary action button. One primary per action cluster; ghost for
 * secondary/dismiss actions; subtle for low-emphasis inline actions.
 * @startingPoint section="Core" subtitle="Filled / ghost / subtle action button" viewport="360x120"
 */
export function Button(props: ButtonProps): JSX.Element;
