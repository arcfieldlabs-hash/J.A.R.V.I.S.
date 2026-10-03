# Cloudflare interface preview

Cloudflare hosts the static orb interface from `web/index.html`. The hosted page
identifies itself as an interface preview and links to the local assistant at
`http://127.0.0.1:8765`. Chat, local model inference, memory, reminders, telemetry,
and Mac desktop tools run in the Python application on your Mac; this deployment
does not provide an online assistant backend.

## Fix the static-files detection error

The error `Could not detect a directory containing static files` means Wrangler
could not locate an asset directory. This repository's `wrangler.jsonc` explicitly
sets `assets.directory` to `./web`. No JavaScript Worker entry point or frontend
compilation is needed for this assets-only deployment.

Use the branch containing both `wrangler.jsonc` and `web/index.html`. These files
are in PR #1 on `feat/blueprint-capabilities`; `main` will contain them after the PR
is merged. Retrying an earlier commit without these files repeats the error.

In the correct Cloudflare account, open **Workers & Pages → j-a-r-v-i-s → Settings
→ Build** and set:

| Setting | Value |
| --- | --- |
| Connected repository | `arcfieldlabs-hash/J.A.R.V.I.S.` |
| Production branch | `feat/blueprint-capabilities` to test PR #1; `main` after merging |
| Root directory | Repository root (leave empty) |
| Build command | Leave empty |
| Deploy command | `npx wrangler deploy` |
| Build environment variable | `NODE_VERSION=22` |

Cloudflare installs the npm dependencies using the committed `package-lock.json`.
The page is plain HTML with inline CSS and JavaScript, so no separate build step
or output directory setting is required. Wrangler uses the configured `./web`
directory relative to the repository root.

After saving the settings, deploy the latest commit on that branch from the
deployment history. Confirm the build log detects `wrangler.jsonc`, uploads the
assets, and reports a successful deployment. Open the Worker URL and check that
the page says it is an interface preview. A successful deployment has not been
verified until Cloudflare reports success for the new commit.

If a later build reports an authentication error, check that the Git connection
can read this repository and that the build's deployment token belongs to the
correct Cloudflare account and has access to this Worker. Do not store account
tokens or credentials in the repository.

## Check locally

Use Node.js 22 or newer:

```bash
npm ci
npm run check
npm run dev
```

`npm run check` validates the configuration with a deployment dry run; it does
not publish. `npm run dev` serves the preview locally, normally at
`http://localhost:8787`. Neither command starts the Python assistant.

To use chat and the local assistant, start Jarvis on your Mac:

```bash
python3 -m jarvis --web --speak
```

Then open `http://127.0.0.1:8765`. Keep the Python server bound to loopback. A full
online assistant would need a separately designed backend for model inference,
authentication, persistence, and permissions; Workers does not run this Mac's
Ollama process, desktop tools, or SQLite database.

Reference: [Cloudflare Workers Static Assets](https://developers.cloudflare.com/workers/static-assets/).
