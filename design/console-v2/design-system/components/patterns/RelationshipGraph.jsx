import React from 'react';
import { StatusDot } from '../core/StatusDot.jsx';

const STATE_COLOR = {
  working_turn: 'var(--ok)', working_silent: 'var(--info)', idle_waiting: 'var(--warn)',
  stuck_suspected: 'var(--attn)', rate_limited_or_outage: 'var(--danger)',
  degraded_output: 'var(--danger)', crashed_or_exited: 'var(--chip-neutral)', unknown: 'var(--gray)',
};

/**
 * RelationshipGraph — the "who's talking to whom" view. Agents are laid out
 * on a circle; edges are weighted by message volume, and an active-review
 * edge is drawn dashed + animated in the accent color.
 */
export function RelationshipGraph({
  nodes = [], edges = [], width = 640, height = 480, radius, onNodeClick, style = {},
}) {
  const cx = width / 2, cy = height / 2;
  const r = radius || Math.min(width, height) / 2 - 62;
  const pos = {};
  nodes.forEach((n, i) => {
    if (n.x != null && n.y != null) { pos[n.id] = { x: n.x, y: n.y }; return; }
    const ang = (-90 + i * (360 / nodes.length)) * Math.PI / 180;
    pos[n.id] = { x: Math.round(cx + r * Math.cos(ang)), y: Math.round(cy + r * Math.sin(ang)) };
  });

  return (
    <div style={{ position: 'relative', width, height, margin: '0 auto', ...style }}>
      <svg width={width} height={height} style={{ display: 'block', position: 'absolute', inset: 0 }}>
        {edges.map((e, i) => {
          const a = pos[e.from], b = pos[e.to];
          if (!a || !b) return null;
          const w = (e.weight || 1) * 1.4 + 0.6;
          return (
            <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={e.hot ? 'var(--accent)' : 'var(--edge)'}
              strokeWidth={w} strokeLinecap="round"
              opacity={e.hot ? 0.9 : 0.55}
              strokeDasharray={e.hot ? '5 7' : undefined}
              style={e.hot ? { animation: 'ds-dashmove 1s linear infinite' } : undefined} />
          );
        })}
      </svg>
      <div style={{ position: 'absolute', inset: 0 }}>
        {nodes.map((n) => {
          const p = pos[n.id];
          return (
            <div key={n.id} onClick={onNodeClick ? () => onNodeClick(n.id) : undefined}
              style={{ position: 'absolute', left: p.x, top: p.y, transform: 'translate(-50%,-50%)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5, cursor: onNodeClick ? 'pointer' : 'default' }}>
              <span style={{ width: 13, height: 13, borderRadius: '50%', background: STATE_COLOR[n.state] || 'var(--gray)', boxShadow: '0 0 0 3px var(--surface)', animation: n.state === 'working_turn' ? 'ds-liveDot 1.8s ease-in-out infinite' : 'none' }} />
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--ink-2)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-chip)', padding: '2px 7px', whiteSpace: 'nowrap', boxShadow: 'var(--shadow-card)' }}>{n.id}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
