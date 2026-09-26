import React from 'react';

/**
 * NavRail — the console's left sidebar: a titled view list with active
 * state + optional count badges, and a status legend pinned to the bottom.
 * Icons are passed as SVG path-`d` strings (Lucide-style) or nodes.
 */
export function NavRail({ items = [], legend = [], width = 238, style = {} }) {
  const Icon = (icon) =>
    typeof icon === 'string'
      ? <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d={icon} /></svg>
      : icon;

  return (
    <aside style={{ width, flex: 'none', background: 'var(--surface)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', padding: '14px 12px', ...style }}>
      <div style={{ fontSize: 10, fontWeight: 600, letterSpacing: '.09em', color: 'var(--ink-4)', textTransform: 'uppercase', padding: '6px 10px 8px' }}>Views</div>
      <nav style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {items.map((it, i) => (
          <a key={i} onClick={it.onClick}
            style={{
              display: 'flex', alignItems: 'center', gap: 11, padding: '9px 11px',
              borderRadius: 'var(--radius-btn)', cursor: 'pointer', fontSize: 13, textDecoration: 'none',
              color: it.active ? 'var(--accent)' : 'var(--ink-2)',
              background: it.active ? 'var(--accent-soft)' : 'transparent',
              fontWeight: it.active ? 500 : 400,
            }}>
            {Icon(it.icon)}
            <span style={{ flex: 1, fontWeight: 500 }}>{it.label}</span>
            {it.badge ? (
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, fontWeight: 500, minWidth: 18, textAlign: 'center', color: '#fff', background: 'var(--danger)', borderRadius: 9, padding: '1px 5px' }}>{it.badge}</span>
            ) : null}
          </a>
        ))}
      </nav>
      {legend.length > 0 && (
        <div style={{ borderTop: '1px solid var(--border-2)', padding: '14px 10px 4px', marginTop: 'auto' }}>
          <div style={{ fontSize: 10, fontWeight: 600, letterSpacing: '.09em', color: 'var(--ink-4)', textTransform: 'uppercase', marginBottom: 10 }}>Status legend</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
            {legend.map((l, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', flex: 'none', background: l.color }} />
                <span style={{ fontSize: 12, color: 'var(--ink-2)', flex: 1 }}>{l.label}</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ink-3)' }}>{l.count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
