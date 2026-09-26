# 05 · Phone (board 3c)

380×800 in the prototype (use for widths < 1024). One team per screen.

Top → bottom:
1. Status bar (system).
2. Header: logo 22 · "Main team" 15/600 + freshness (mono 11: ok "live · updated 3s ago" / warn "offline · last seen 3h 12m ago") · **"2 teams" button, 44px tall**, with a warn badge counting the other team's needs.
3. Offline banner (if offline): "**Can't see the team.** win-ws01 stopped reporting 3h 12m ago. Everything here is from 18:22."
4. Scroll area (padding 2/16/16, gap 14):
   - "NEEDS YOU · n" (mono 11). Offline: "n · frozen".
   - Cards (max 2 visible, same order as desktop): kind + age · serif 20 title · evidence mono 11.5 · **two buttons, 48px tall**: primary (flex 2, first option) + Later (flex 1). Offline: "Can't send", disabled.
   - Quiet: calm card "Nothing needs you." serif 30 + "9 of 10 agents are idle — that's normal…".
   - Qwen spend card: label, mono 18 value "/ 100 €", 10px bar, line "alert 80 · cutoff 95 · bill 61.80 € (14:00)".
   - Lead card: avatar + "Lead" + age · latest message 14.5 · reply field 48px.
5. Tab bar 66px: Needs you (badge) · Team · Lead; each target ≥ 48px.

Answers on the phone update the same store as desktop (prototype does this).
