import React from 'react';
import { Meter } from '../core/Meter.jsx';

/**
 * CapacityPanel — an agent's headroom at a glance: the 5-hour rate-limit
 * and context-window meters with advisory notes. Thresholds are inherited
 * from Meter (green <60, amber 60–84, red ≥85).
 */
export function CapacityPanel({
  rate = 0, context = 0, rateNote, contextNote, style = {},
}) {
  const rNote = rateNote ?? (rate >= 85 ? 'Near cap — steer long work elsewhere' : 'Headroom for new work');
  const cNote = contextNote ?? (context >= 85 ? 'Compaction risk — avoid heavy context' : 'Comfortable context budget');
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-card)', boxShadow: 'var(--shadow-card)', padding: 'var(--pad-card-lg)', ...style }}>
      <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--ink-4)', marginBottom: 16 }}>Capacity</div>
      <Meter label="5-hour rate limit" value={rate} size="lg" note={rNote} style={{ marginBottom: 16 }} />
      <Meter label="Context window" value={context} size="lg" note={cNote} />
    </div>
  );
}
