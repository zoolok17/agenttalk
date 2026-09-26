The core roster tile — one per agent in the overview grid. Composes StatusDot, CliBadge, Chip, and Meter, and paints a status spine matching the agent's health.

```jsx
<AgentCard
  agent={{ id: 'claude-dev', cli: 'claude', role: 'implementer', group: 'devs',
           state: 'working_turn', task: 'Implementing WP-07…', age: '8s',
           rate: 58, ctx: 74, wrapped: true }}
  onClick={() => openAgent('claude-dev')}
/>
```

Pass a preformatted `age` string (the component doesn't tick clocks itself). `wrapped` shows the supervised-frame glyph.
