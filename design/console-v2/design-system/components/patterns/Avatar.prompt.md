An agent's circular portrait with a live status-dot badge (bottom-right, surface ring, breathes when working). Use it everywhere an agent is shown — cards, detail header, graph nodes, chat header — for one consistent identity.

```jsx
<Avatar src="avatars/claude-lead.png" alt="claude-lead" state="working_turn" size={40} />
<Avatar src={`avatars/${agent.id}.png`} state={agent.state} size={56} />
```

Filenames follow the agent id. A missing/broken image falls back to a neutral disc with a status dot, so a new agent without an avatar still renders cleanly. Pass `badge={false}` for decorative use.
