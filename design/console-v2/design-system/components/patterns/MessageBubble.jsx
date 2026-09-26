import React from 'react';
import { CliBadge } from '../core/CliBadge.jsx';
import { Chip } from '../core/Chip.jsx';

/**
 * MessageBubble — one turn in a transcript or the lead chat. `mine` right-
 * aligns in accent; otherwise left-aligned on surface. Optional kind chip,
 * CLI badge, and mono meta footer. Bodies render pre-wrapped (not markdown).
 */
export function MessageBubble({
  from, body, mine = false, cli = null, kind = null, kindTone = 'neutral',
  age = null, meta = null, style = {},
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: mine ? 'flex-end' : 'flex-start', gap: 3, ...style }}>
      <div style={{
        maxWidth: '82%',
        background: mine ? 'var(--accent)' : 'var(--surface)',
        color: mine ? '#fff' : 'var(--ink)',
        border: mine ? 'none' : '1px solid var(--border)',
        borderRadius: 'var(--radius-card-lg)',
        padding: '11px 14px',
        boxShadow: 'var(--shadow-card)',
      }}>
        {(from || cli || kind) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 8 }}>
            {cli && <CliBadge cli={cli} />}
            {from && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 500, color: mine ? 'rgba(255,255,255,.85)' : 'var(--ink-2)' }}>{from}</span>}
            {kind && <Chip tone={kindTone}>{kind}</Chip>}
          </div>
        )}
        <div style={{ fontSize: 13.5, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{body}</div>
        {meta && (
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: mine ? 'rgba(255,255,255,.7)' : 'var(--ink-4)', marginTop: 8, paddingTop: 8, borderTop: `1px solid ${mine ? 'rgba(255,255,255,.25)' : 'var(--border-2)'}` }}>{meta}</div>
        )}
      </div>
      {age && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--ink-4)', padding: '0 4px' }}>{age}</span>}
    </div>
  );
}
