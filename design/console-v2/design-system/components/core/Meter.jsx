import React from 'react';

/**
 * Meter — a labeled capacity bar (rate limit, context window). Fill color
 * is automatic by threshold: green < 60, amber 60–84, red ≥ 85.
 */
export function Meter({ label, value = 0, size = 'sm', note = null, style = {} }) {
  const fill = value >= 85 ? 'var(--danger)' : value >= 60 ? 'var(--warn)' : 'var(--ok)';
  const h = size === 'lg' ? 7 : 4;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: size === 'lg' ? 7 : 5, ...style }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontFamily: 'var(--font-mono)',
        fontSize: size === 'lg' ? '12px' : '9.5px',
        letterSpacing: size === 'lg' ? 0 : '.05em',
        color: size === 'lg' ? 'var(--ink-2)' : 'var(--ink-4)',
      }}>
        <span>{label}</span><span>{Math.round(value)}%</span>
      </div>
      <div style={{ height: h, background: 'var(--track)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${Math.max(2, value)}%`, background: fill, borderRadius: 3, transition: 'width var(--dur-slow) var(--ease-standard)' }} />
      </div>
      {note && <div style={{ fontSize: '11px', color: 'var(--ink-4)' }}>{note}</div>}
    </div>
  );
}
