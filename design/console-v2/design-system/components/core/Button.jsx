import React from 'react';

/**
 * Button — the console's action control. Primary (filled accent), ghost
 * (bordered surface), and subtle (quiet) variants; two sizes.
 */
export function Button({
  variant = 'primary',
  size = 'md',
  disabled = false,
  icon = null,
  children,
  style = {},
  ...rest
}) {
  const pad = size === 'sm' ? '7px 13px' : '9px 15px';
  const font = size === 'sm' ? '12px' : '12.5px';

  const variants = {
    primary: { background: 'var(--accent)', color: '#fff', border: '1px solid transparent' },
    ghost:   { background: 'var(--surface)', color: 'var(--ink-2)', border: '1px solid var(--border)' },
    subtle:  { background: 'var(--inset)', color: 'var(--ink-2)', border: '1px solid transparent' },
  };

  const base = {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '7px',
    padding: pad,
    fontSize: font,
    fontWeight: 500,
    fontFamily: 'var(--font-body)',
    borderRadius: 'var(--radius-btn)',
    cursor: disabled ? 'default' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    whiteSpace: 'nowrap',
    transition: 'filter var(--dur-fast), background var(--dur-fast), border-color var(--dur-fast)',
    ...variants[variant],
    ...style,
  };

  return (
    <button type="button" disabled={disabled} style={base} {...rest}>
      {icon && <span style={{ display: 'inline-flex', width: 15, height: 15 }}>{icon}</span>}
      {children}
    </button>
  );
}
