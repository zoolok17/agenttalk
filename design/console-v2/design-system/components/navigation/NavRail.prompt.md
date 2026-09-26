The console's left sidebar — a titled view list with active state and count badges, plus a pinned status legend.

```jsx
<NavRail
  items={[
    { label: 'Team overview', icon: 'M3 3h7v7H3z…', active: true, onClick: goOverview },
    { label: 'Attention', icon: 'M10.29 3.86…', badge: 5, onClick: goAttention },
  ]}
  legend={[
    { label: 'Working', color: 'var(--ok)', count: 5 },
    { label: 'Idle · waiting', color: 'var(--warn)', count: 2 },
  ]}
/>
```

`icon` is a Lucide-style SVG path-`d` string (or a node). Keep exactly one item `active`.
