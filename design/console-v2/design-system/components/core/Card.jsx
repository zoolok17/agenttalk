import React from 'react';

/**
 * Card — the surface container. Optional status `spine` (a colored inset
 * left bar) and `hover` lift. Everything in the console sits on a Card.
 */
export function Card({ spine = null, hover = false, padding = 'var(--pad-card-lg)', style = {}, children, ...rest }) {
  const shadow = spine
    ? `inset 3px 0 0 ${spine}, var(--shadow-card)`
    : 'var(--shadow-card)';
  return (
    <div
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-card)',
        padding,
        boxShadow: shadow,
        transition: 'box-shadow var(--dur-fast), border-color var(--dur-fast)',
        ...(hover ? { cursor: 'pointer' } : {}),
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}
