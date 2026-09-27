import React from 'react';
import { StatusDot } from '../core/StatusDot.jsx';
import { CliBadge } from '../core/CliBadge.jsx';
import { Chip } from '../core/Chip.jsx';
import { Meter } from '../core/Meter.jsx';

const STATE_LABEL = {
  working_turn: ['Working', 'var(--ok)'],
  working_silent: ['Working · quiet', 'var(--info)'],
  idle_waiting: ['Idle · waiting', 'var(--warn)'],
  stuck_suspected: ['Stuck?', 'var(--attn)'],
  rate_limited_or_outage: ['Rate-limited', 'var(--danger)'],
  degraded_output: ['Degraded', 'var(--danger)'],
  crashed_or_exited: ['Exited', 'var(--chip-neutral)'],
  unknown: ['Unknown', 'var(--gray)'],
};
const TONE = {
  working_turn: 'ok', working_silent: 'info', idle_waiting: 'warn',
  stuck_suspected: 'attn', rate_limited_or_outage: 'danger',
  degraded_output: 'danger', crashed_or_exited: 'neutral', unknown: 'gray',
};

/**
 * AgentCard — the core roster tile. Shows one agent's identity, current
 * task, health chip, heartbeat age, and rate/context meters, with a
 * colored status spine. Clickable to open detail.
 */
export function AgentCard({ agent, onClick, style = {} }) {
  const a = agent || {};
  const [label, color] = STATE_LABEL[a.state] || STATE_LABEL.unknown;
  return (
    <div
      onClick={onClick}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-card)',
        padding: 'var(--pad-card)',
        boxShadow: `inset 3px 0 0 ${color}, var(--shadow-card)`,
        cursor: onClick ? 'pointer' : 'default',
        display: 'flex', flexDirection: 'column', gap: 9,
        transition: 'box-shadow var(--dur-fast), border-color var(--dur-fast)',
        ...style,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <StatusDot state={a.state} />
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 500, letterSpacing: '-.01em', color: 'var(--ink)' }}>{a.id}</span>
        <CliBadge cli={a.cli} />
        <span style={{ flex: 1 }} />
        {a.wrapped && (
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--ink-5)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2" /></svg>
        )}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--ink-3)' }}>{a.role} · {a.group}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.42, color: 'var(--ink-2)', minHeight: 36 }}>{a.task}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Chip tone={TONE[a.state] || 'gray'}>{label}</Chip>
        <span style={{ flex: 1 }} />
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ink-4)' }}>{a.age || ''}</span>
      </div>
      <div style={{ display: 'flex', gap: 14, paddingTop: 10, borderTop: '1px solid var(--hairline)' }}>
        <Meter label="RATE" value={a.rate || 0} style={{ flex: 1 }} />
        <Meter label="CTX" value={a.ctx || 0} style={{ flex: 1 }} />
      </div>
    </div>
  );
}
