import * as React from 'react';

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** A status color (e.g. 'var(--ok)') to paint the inset left spine. */
  spine?: string | null;
  /** Enables pointer cursor; pair with a style-hover for the lift. */
  hover?: boolean;
  /** CSS padding. Default var(--pad-card-lg). */
  padding?: string;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}

/** Surface container with optional colored status spine and hover lift. */
export function Card(props: CardProps): JSX.Element;
