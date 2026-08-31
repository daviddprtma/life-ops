# LifeOps — AI Action Planner

> **Turn any messy real-world situation into a structured, prioritised action plan — powered by Anna's LLM via reverse Sampling.**

Scaffolded with `anna-app init` · App ID: 188 · Slug: `life-ops`

---

## What it does

The user drops a messy real-world situation into the app — text, notes, half-formed ideas — and clicks **Generate Action Plan**. The Python Executa receives the text, calls Anna's LLM via `sampling/createMessage` (reverse JSON-RPC), and returns a structured plan with:

- **Summary** + **urgency level** (high / medium / low)
- **Prioritised task list** (critical → low) with reason and timeframe
- **Single next action** (the most important thing to do right now)
- **Resources needed** + **risks/blockers**

Plans are stored in Anna Persistent Storage (APS) and the last 5 are shown in the history panel.

---

## Architecture

```
bundle/index.html + app.js   (Anna iframe UI)
        │
        │  anna.tools.invoke({method:"plan", args:{situation, category, context}})
        ▼
Anna UI Runtime dispatcher
        │  stdio JSON-RPC
        ▼
executas/lifeops/lifeops_plugin.py   (Python Executa, v2 protocol)
        │
        │  sampling/createMessage (reverse RPC over stdout)
        ▼
Anna LLM (user's preferred provider — billing handled by Anna)
        │
        └─► structured JSON plan → back to iframe → rendered as cards
```

---

## Local development

### Prerequisites

- **Node 22+**: `node --version`
- **uv** (Astral): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **anna-app CLI**: `npm i -g @anna-ai/cli`

### Run the dev harness

```bash
# From the project root:
anna-app dev
# → opens http://127.0.0.1:5180/dev/<wid>?t=<dev-token>
```

### Validate the manifest

```bash
anna-app validate            # JSON Schema + UI static checks
anna-app validate --strict   # + bundle ACL coverage check
```

### Test the plugin in isolation

```bash
cd executas/lifeops

# Check describe() returns the correct manifest
echo '{"jsonrpc":"2.0","method":"describe","id":1}' | uv run python lifeops_plugin.py 2>/dev/null

# Check initialize() returns v2 capability (sampling:{})
echo '{"jsonrpc":"2.0","method":"initialize","id":0,"params":{"protocolVersion":"2.0","clientInfo":{"name":"test","version":"1.0"},"capabilities":{"sampling":{"modalities":["text"],"maxTokensPerCall":8192}}}}' | uv run python lifeops_plugin.py 2>/dev/null

# Check ping()
echo '{"jsonrpc":"2.0","method":"invoke","id":2,"params":{"tool":"ping","arguments":{}}}' | uv run python lifeops_plugin.py 2>/dev/null
```

### Test with mock LLM

For offline development (no LLM API key needed):

```bash
anna-app dev --mock-llm ./fixtures/sampling-mock.jsonl
```

---

## Sampling grant (required in production)

The `plan` tool calls Anna's LLM via reverse sampling. In the **local dev harness**, this is mocked automatically. When deployed to a real Anna instance, the user must enable sampling:

1. Anna Settings → Apps → LifeOps → **Enable LLM sampling**
2. Or: Anna Admin panel → Executas → tool-dev-lifeops → **Sampling grant: enabled**

Without this grant, `plan` will fail with `-32001 SAMPLING_NOT_GRANTED`. The UI shows a helpful error message with instructions in this case.

---

## Publishing

The Executa is **bundled**: `app.json` declares it under `bundled_executas` with the handle
`lifeops`, and `manifest.json` references it as `bundled:lifeops`. You never mint or paste a
tool ID by hand.

```bash
anna-app validate --strict   # must be green first
anna-app apps publish
```

`anna-app apps publish` does the wiring for you, in order:

1. Publishes `executas/lifeops` (using the `distribution.active` profile — currently `binary`).
2. Mints the real platform `tool_id` for it.
3. Rewrites every `bundled:lifeops` reference in `manifest.json` **in memory only** — the file on
   disk is left untouched.
4. Regenerates `bundle/anna-tool-ids.js` so the frontend resolves the minted ID at runtime via
   `window.__ANNA_TOOL_IDS__["lifeops"]`.

Then fill in listing metadata in the Developer Console (logo, screenshots, homepage) and submit for
review with `anna-app apps submit-review`.

### The three IDs, kept separate

| ID | Lives in | Purpose |
|----|----------|---------|
| `tool-dev-lifeops` | `executas/lifeops/executa.json` → `tool_id` | **Local dev discovery only.** How `anna-app dev` spawns and routes to the stdio process. Never published. |
| `tool-<handle>-<hash>` | Minted by the platform at publish | The real catalogue ID. Never written into a source file by hand. |
| `lifeops` | `app.json`, `manifest.json`, `bundle/app.js` → `EXECUTA_HANDLE` | **The bundled handle.** The stable name everything references. |

> Do **not** hand-edit `bundle/anna-tool-ids.js`, and do not replace `bundled:lifeops` with a
> concrete ID — a pinned ID drifts on the next publish.

---

## File map

| File | Purpose |
|------|---------|
| `manifest.json` | Anna App manifest (schema 2, permissions, UI, dev config) |
| `app.json` | App store metadata + `bundled_executas` handle → path map |
| `bundle/index.html` | Iframe entry point — two-column input/output layout |
| `bundle/style.css` | Complete design system (dark mode, glassmorphism, animations) |
| `bundle/anna-tool-ids.js` | **Auto-generated** handle → minted `tool_id` map; loaded before `app.js` |
| `bundle/app.js` | Frontend logic (AnnaAppRuntime, tools.invoke, plan renderer, storage) |
| `executas/lifeops/executa.json` | Executa config — local dev `tool_id`, spawn `type`, distribution profiles |
| `executas/lifeops/lifeops_plugin.py` | Python Executa — v2 protocol, sampling, plan tool |
| `executas/lifeops/pyproject.toml` | Python package config |
| `fixtures/sampling-mock.jsonl` | Mock LLM response for offline dev |
