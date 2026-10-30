# ACR Agent drawer

The masthead's **Ask ACR Agent** button opens the published Aomi widget beside
the dashboard. Desktop users can drag the panel edge or focus it and use arrow
keys. On screens up to 760px the panel fills the screen, traps keyboard focus,
and makes the underlying page inert. Escape or the close button returns focus
to the opener. Reduced-motion preferences disable the slide transition.

The shared root layout keeps the widget mounted after its first opening,
including across client-side page navigation. Closing it preserves the draft
and conversation. The widget also owns its app/account-scoped thread storage.
Page-specific question hints do not submit messages or send page data.

## Deployment

Merge and deploy with the existing Vercel setup: no new environment variable,
secret, backend, or contract deployment is required for this chat drawer. It
uses the author's existing public ACR application (`2938642`) by default.

`ACR_AOMI_APPLICATION_ID` is an optional server-side override for forks or a
replacement hosted ACR app. The public identifier is passed to both the widget's
application scope and its sole Direct routing target. Invalid explicit overrides
(including an empty string) show an unavailable state rather than silently
falling back. There is no Auto routing or URL-selected target.

The widget uses its official `https://chat.aomi.dev` service and browser-wallet
authentication. Private app permissions remain enforced by Aomi; embedding
does not grant access. Never place an owner's API credential or signing key in
this frontend. Opening the drawer does not request a signature or send a chat.
The site's MAINNET/TESTNET toggle controls the site's data, not wallet authority:
the widget's network selector and transaction review remain independent.

`@aomi-labs/widget` is pinned to 3.1.4. Its compiled stylesheet is scoped to the
widget root; ACR overrides its theme tokens. Generic swap/bridge starter chips
are hidden for this compute-specific entry point. The widget currently retains
its built-in composer wording and model/safety controls. The model selector's
“Auto” label selects a model, not app routing.

The local `.npmrc` uses legacy peer resolution because 3.1.4's optional Para and
Privy peer constraints conflict in npm's resolver. This integration uses neither
provider. The lockfile and production build cover the browser-wallet path.
Recheck this workaround when upgrading the widget.

## Verification

- Production Next build, TypeScript, targeted ESLint, and 395 frontend tests.
- Browser: drawer open/close, desktop resizing, draft preservation across close
  and navigation, mobile full-screen layout, focus handling, and invalid app configuration.
- Live read-only smoke used the original public ACR application (2938642), with the
  default configuration. It returned an ACR-specific explanation. This does not
  prove private-fork access.
- No wallet signatures or payments were made. Treasury payment acceptance and
  deployment of the private app remain separate from this frontend PR.

Screenshots in `docs/evidence/agent-drawer/` show the local production build.

## Safeguards added at merge (#67)

The widget is the vendor's npm code plus roughly 775 transitive wallet-SDK packages, running in this
origin rather than in an iframe. sessionStorage holds two credentials that must never share a JavaScript
context with it: `acr-ops-key` (the operator console on `/ops` and `/spend`) and `acr-reader-card` (an
owner's agent card on `/spend`). #67 kept them apart inside one origin: no drawer on those pages, no widget in
a tab already holding a key, and a full reload when entering one. That guard has been **replaced** by
separate origins (next section): a guard inside one origin is only as strong as "the vendor's code has not
run in this tab yet".

- **On phones (≤640px) the launcher floats bottom right as the ✦ mark.** In the masthead it pushed the
  EXPERT | PLAIN toggle onto a third header row.

Two things are not restricted, and both are noted on the PR:

- **Networks.** The widget's "All networks" selector includes testnets, and its public props
  (`features`: settings, library, theme, activity) cannot hide it.
- **The `@aomi-labs` packages carry no license field.** This is accepted because the vendor authored the PR.

`.npmrc`'s `legacy-peer-deps` applies to every install in `apps/terminal`, not just the widget. Its comment
lists what it suppresses (measured: react-native 0.87 and @solana/kit 7 as wallet-SDK peers, and valtio's
React ≤18 peer). When adding or upgrading a dependency, read `npm ls` yourself.

## Credential pages on their own origin

`/ops`, `/spend` and `/developers` are served from their own origin (`ops.arccomputerate.in`), and the widget
is never mounted there. sessionStorage, the DOM and window handles are all per origin, so the browser itself
keeps `acr-ops-key` and `acr-reader-card` away from the vendor's code, whatever that code does.

| Variable | Example | Meaning |
| --- | --- | --- |
| `NEXT_PUBLIC_ACR_OPS_ORIGIN` | `https://ops.arccomputerate.in` | The origin serving the credential pages. **Required for production builds** (`next.config.mjs` fails the build without it). |
| `NEXT_PUBLIC_ACR_SITE_ORIGIN` | `https://arccomputerate.in` | Optional. The main site. Defaults to the ops host minus its first label. |

These are `NEXT_PUBLIC_`, so they are inlined at build time: change one, and you must redeploy. A single
value drives the proxy, the layout and every link, so they cannot disagree. `lib/opsOrigin.ts`
(`hostSplit()`) reads them.

- **`proxy.ts` routes by host.** The main host 307s `/ops`, `/spend` and `/developers` (and their subpaths)
  to the ops host. The ops host 307s every other page back. Both hosts serve `/api/*`, `/og/*` and static
  assets, because the credential pages fetch their own `/api` proxies same-origin.
- **The ops host never mounts the drawer.** `app/layout.tsx` compares the request's `Host` header with the ops
  host and renders the page without `AgentDrawer`, so no launcher and no widget bundle exist on that host.
- **Links name the serving host** (`hostHref`): the masthead nav, the footer's systems ledger, and the links on
  `/loop`, `/traction`, `/developers`, `/spend` and the home page's tape teaser. A relative link would still
  work through the redirect, but the router prefetches it with a `fetch`, and a fetch refuses to follow a
  redirect to another origin.
- **The MAINNET/TESTNET cookie is set on the parent domain** (`domain=arccomputerate.in`), so an operator
  does not land on the default chain with a key in hand. The chain is not a credential. Readers who switched
  chains before this keep a host-only cookie on the main host until they next switch.

**Without the variable (local dev, Vercel previews) the site is one origin and nothing separates the widget
from the keys.** Do not paste a real operator key into a preview.

### Turning it on

1. Vercel → the terminal project → Domains: add `ops.arccomputerate.in`. It is the same deployment, so it has
   no project of its own.
2. DNS: add `CNAME ops → cname.vercel-dns.com` (or the record Vercel shows for the domain).
3. Vercel → Environment Variables, Production: `NEXT_PUBLIC_ACR_OPS_ORIGIN=https://ops.arccomputerate.in`.
   Also set `NEXT_PUBLIC_ACR_SITE_ORIGIN` if the main site is not the ops host minus `ops.`. Leave it unset for
   Preview, or previews will link to production hosts. Then redeploy.
4. Add `https://ops.arccomputerate.in` to `ACR_CORS_ORIGINS` on the mainnet presses in `render.yaml`.
   Pay-with-wallet on `/developers` calls the seller cross-origin, and it is now called from the ops origin.

This needs the real domain: a `*.vercel.app` alias cannot have a subdomain. Two sibling aliases would work
as two origins (set `NEXT_PUBLIC_ACR_SITE_ORIGIN`), but they share no parent domain, so the chain cookie
would stay per host.

### Not covered

- `/traction`'s ledger download reads the agent card from sessionStorage. On the main origin there is never
  one now.
- `acr-desk-user` in **localStorage** on `/curve` (`components/chain/PublicDesk.tsx`) is a bearer: whoever
  knows it can open that desk session. It still lives on the main origin beside the widget, and this change
  does not move it.
