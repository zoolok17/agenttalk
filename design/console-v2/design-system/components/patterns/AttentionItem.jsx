import React from 'react';
import { Chip } from '../core/Chip.jsx';
import { Button } from '../core/Button.jsx';

const SOURCE = {
  escalation: ['ESCALATION', 'var(--danger)'],
  gate:       ['GATE HOLD', 'var(--danger)'],
  stuck:      ['STUCK', 'var(--attn)'],
  deadletter: ['DEAD LETTER', 'var(--violet)'],
  supervisor: ['SUPERVISOR', 'var(--chip-neutral)'],
};
const SEV = {
  high: ['HIGH', 'danger', 'var(--danger)'],
  med:  ['MED', 'warn', 'var(--warn)'],
  low:  ['LOW', 'neutral', 'var(--chip-neutral)'],
};

/**
 * AttentionItem — one row in the "Needs a human" queue. A colored severity
 * spine, a solid source tag, the blocker title, the agent + detail, and a
 * cluster of actions (first primary, rest ghost).
 */
export function AttentionItem({
  source = 'escalation', severity = 'med', title, agent, detail, age,
  actions = [], style = {},
}) {
  const [srcLabel, srcColor] = SOURCE[source] || SOURCE.escalation;
  const [sevLabel, sevTone, sevColor] = SEV[severity] || SEV.med;

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-card)',
      padding: '16px 18px', display: 'flex', alignItems: 'center', gap: 18,
      boxShadow: `inset 3px 0 0 ${sevColor}, var(--shadow-card)`, ...style,
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 9, flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          <Chip solid style={{ background: srcColor }}>{srcLabel}</Chip>
          <Chip tone={sevTone}>{sevLabel}</Chip>
          <span style={{ flex: 1 }} />
          {age && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ink-4)' }}>{age} ago</span>}
        </div>
        <div style={{ fontSize: 14.5, fontWeight: 500, color: 'var(--ink)', letterSpacing: '-.005em' }}>{title}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {agent && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, background: 'var(--inset)', border: '1px solid var(--border)', borderRadius: 'var(--radius-chip)', padding: '2px 7px', color: 'var(--ink-2)' }}>{agent}</span>}
          {detail && <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>{detail}</span>}
        </div>
      </div>
      {actions.length > 0 && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flex: 'none' }}>
          {actions.map((a, i) => (
            <Button key={i} size="sm" variant={a.variant || (i === 0 ? 'primary' : 'ghost')} onClick={a.onClick}>{a.label}</Button>
          ))}
        </div>
      )}
    </div>
  );
}
