import React from 'react';

/**
 * CliBadge — marks which coding-CLI runtime an agent runs. Claude = terracotta,
 * Codex = teal. Always uppercase mono.
 */
export function CliBadge({ cli = 'claude', style = {} }) {
  const map = {
    claude: ['var(--claude)', 'var(--claude-bg)', 'CLAUDE'],
    codex:  ['var(--codex)', 'var(--codex-bg)', 'CODEX'],
  };
  const [fg, bg, label] = map[cli] || map.claude;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontFamily: 'var(--font-mono)',
        fontSize: '10px',
        fontWeight: 500,
        letterSpacing: '.02em',
        borderRadius: 'var(--radius-chip)',
        padding: '2px 7px',
        color: fg,
        background: bg,
        ...style,
      }}
    >
      {label}
    </span>
  );
}
