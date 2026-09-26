import React from 'react';

const STATE = {
  working_turn:            { color: 'var(--ok)',     pulse: true  },
  working_silent:          { color: 'var(--info)',   pulse: false },
  idle_waiting:            { color: 'var(--warn)',   pulse: false },
  stuck_suspected:         { color: 'var(--attn)',   pulse: false },
  rate_limited_or_outage:  { color: 'var(--danger)', pulse: false },
  degraded_output:         { color: 'var(--danger)', pulse: false },
  crashed_or_exited:       { color: 'var(--chip-neutral)', pulse: false },
  unknown:                 { color: 'var(--gray)',   pulse: false },
};

/**
 * StatusDot — the atomic health indicator. A working agent breathes;
 * everything else is steady. Optional soft-color ring for emphasis.
 */
export function StatusDot({ state = 'unknown', size = 10, ring = false, style = {} }) {
  const s = STATE[state] || STATE.unknown;
  return (
    <span
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        borderRadius: '50%',
        flex: 'none',
        background: s.color,
        boxShadow: ring ? `0 0 0 ${Math.round(size / 2.5)}px color-mix(in srgb, ${s.color} 16%, transparent)` : 'none',
        animation: s.pulse ? 'ds-liveDot 1.8s ease-in-out infinite' : 'none',
        ...style,
      }}
    />
  );
}
