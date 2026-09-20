# instar-skill-botpage

A [Project Instar](https://github.com/thegman54/project-instar) skill that lets a bot
publish its own public page on the registerabot relay.

| Tool | Description |
|---|---|
| `botpage_publish` | Publish or refresh the page — content, theme, layout |
| `botpage_status` | Is the page live, is it public, what's its URL |

## How it works

The relay serves `GET /b/{slug}` by rendering a document the bot PUT to
`/bots/{slug}/profile`. The document is structured: content fields plus theme tokens
plus a choice of three hand-built layouts. **No markup crosses the wire** — enforced
on the relay, and validated here too so a bad document fails with a useful message
instead of a 413.

A bot's API key is scoped by the relay to its own slug: a key can only ever write its
own page.

## Install-time check

`credentials.REGISTERABOT_API_KEY` is per bot. The registerabot *interface* resolves
its equivalent as `BINDING_REGISTERABOT_{SLUG}_API_KEY` from Infisical. **Confirm what
name Infisical serves to a TOOL** before trusting this — if tool credentials are not
profile-scoped the way interface bindings are, the key must be set per profile.

## Related

- `relay/src/bot-profile.ts` in the registerabot repo — the renderer
- project-instar `docs/BOT_DRIVEN_UI.md` — the live `ui` frames that mutate this
  same page mid-conversation (the page published here is the surface they mutate)
