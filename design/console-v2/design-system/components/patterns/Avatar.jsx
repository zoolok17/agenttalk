import React from 'react';

const STATE_COLOR = {
  working_turn: 'var(--ok)', working_silent: 'var(--info)', idle_waiting: 'var(--warn)',
  stuck_suspected: 'var(--attn)', rate_limited_or_outage: 'var(--danger)',
  degraded_output: 'var(--danger)', crashed_or_exited: 'var(--chip-neutral)', unknown: 'var(--gray)',
};

/**
 * Avatar — an agent's circular portrait with a live status-dot badge at the
 * bottom-right (surface ring; breathes when working). Falls back to a plain
 * status dot on a neutral disc if the image is missing.
 */
export function Avatar({ src, alt = '', state = 'unknown', size = 40, badge = true, style = {} }) {
  const color = STATE_COLOR[state] || STATE_COLOR.unknown;
  const bs = Math.max(9, Math.round(size * 0.28));
  const [broken, setBroken] = React.useState(false);
  return (
    <div style={{ position: 'relative', flex: 'none', width: size, height: size, ...style }}>
      {src && !broken ? (
        <img src={src} alt={alt} onError={() => setBroken(true)}
          style={{ width: size, height: size, borderRadius: '50%', objectFit: 'cover', display: 'block', border: '2px solid var(--surface)', boxShadow: '0 1px 3px var(--shadow)', background: 'var(--surface-2)' }} />
      ) : (
        <div style={{ width: size, height: size, borderRadius: '50%', background: 'var(--surface-2)', border: '2px solid var(--surface)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ width: size * 0.34, height: size * 0.34, borderRadius: '50%', background: color }} />
        </div>
      )}
      {badge && (
        <span style={{
          position: 'absolute', right: 0, bottom: 0, width: bs, height: bs, borderRadius: '50%',
          background: color, border: '2px solid var(--surface)', boxSizing: 'border-box',
          animation: state === 'working_turn' ? 'ds-liveDot 1.8s ease-in-out infinite' : 'none',
        }} />
      )}
    </div>
  );
}
