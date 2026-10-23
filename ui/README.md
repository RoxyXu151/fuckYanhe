# shadcn/ui frontend

The dashboard uses React, the official shadcn/ui New York Button, Card, Badge,
Tabs and Alert sources, Radix primitives, Lucide icons and Tailwind CSS.

```sh
cd ui
npm ci
npm run build
npx tsc --noEmit
```

Generated production assets are committed under `src/yanhe_sentinel/web/static/ui`.
End users need only Python; Node is required only when changing the frontend.
The Python package includes the generated assets, without runtime CDN requests.

The `fuckYanheUI` bridge receives read-only view data and callbacks from the
existing controller. Navigation unmounts the React root before the original
course/report/study renderer takes over. Non-dashboard pages retain their
existing controls and behavior and share the theme tokens. Chinese/English
changes rerender the dashboard; theme changes use CSS variables.

The palette uses white, green (#006b4f), near-black (#181c1b) and red (#b52c38).
These are interface choices inspired by the requested BIT colors, not a claim
of official color-standard compliance or school endorsement.

Sources: https://ui.shadcn.com/docs/installation/manual and
https://ui.shadcn.com/r/styles/new-york/{button,card,badge,tabs,alert}.json
