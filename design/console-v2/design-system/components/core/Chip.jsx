import React from 'react';

const TONES = {
  neutral: ['var(--chip-neutral)', 'var(--chip-neutral-bg)'],
  accent:  ['var(--accent)', 'var(--accent-soft)'],
  ok:      ['var(--ok)', 'var(--ok-soft)'],
  info:    ['var(--info)', 'var(--info-soft)'],
  warn:    ['var(--warn)', 'var(--warn-soft)'],
  attn:    ['var(--attn)', 'var(--attn-soft)'],
  danger:  ['var(--danger)', 'var(--danger-soft)'],
  violet:  ['var(--violet)', 'var(--violet-soft)'],
  teal:    ['var(--teal)', 'var(--teal-soft)'],
  gray:    ['var(--gray)', 'var(--gray-soft)'],
};

/**
 * Chip — the console's universal pill. One component for status labels,
 * message kinds, severities, and thread states. Mono by default so it
 * reads as metadata. Use `solid` for a filled source tag.
 */
export function Chip({ tone = 'neutral', solid = false, children, style = {} }) {
  const [fg, bg] = TONES[tone] || TONES.neutral;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontFamily: 'var(--font-mono)',
        fontSize: '10px',
        fontWeight: 500,
        letterSpacing: '.02em',
        whiteSpace: 'nowrap',
        borderRadius: 'var(--radius-chip)',
        padding: '2px 7px',
        color: solid ? '#fff' : fg,
        background: solid ? fg : bg,
        ...style,
      }}
    >
      {children}
    </span>
  );
}
