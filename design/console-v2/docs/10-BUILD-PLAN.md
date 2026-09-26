# 10 · Build plan

1. **View-model adapter** over the existing snapshot (07): add `as_of` everywhere, freshness, stuck-vs-busy evidence, queue order, optional deadline, name shortening.
2. **Shell + theme engine**: CSS variables from `tokens/themes.json`; Terminal flips font and radius variables.
3. **Stream**: greeting, lead latest, needs cards, deferred line, side block, chat, composer.
4. **Rail**: usage windows, roster with avatars and badges.
5. **States**: quiet and offline treatments (08), Retry.
6. **Keyboard**: map + overlay.
7. **Phone layout** (05) at < 1024px.
8. **Action gating** with `--enable-actions` + server re-checks.
9. **Flagged — three backend pieces**: (a) gateway spend per machine + account bill in the console; (b) a list of team sources; (c) process evidence for stuck agents. Until (c), stuck detection uses heartbeat + progress counter + last message with Wait first.
10. **Inherited surfaces** (gates, lanes, ownership, risk, tasks, lessons, onboarding) as stream items + detail drawer, using 2a–2d as reference.

Not planned: mission progress/ETA, program stages, signed export (no data).
