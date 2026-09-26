Action button in three weights — use exactly one `primary` per action cluster; `ghost` for secondary/dismiss; `subtle` for quiet inline actions.

```jsx
<Button variant="primary" onClick={save}>Restart with context</Button>
<Button variant="ghost" size="sm">Open transcript</Button>
```

Variants: `primary` (filled accent) · `ghost` (bordered surface) · `subtle` (quiet inset). Sizes: `sm`, `md`. Pass `icon` for a leading inline SVG.
