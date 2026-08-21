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

Before publishing to the Anna App Store, you must:

1. **Mint a real tool ID** at https://anna.partners/executa → My Tools → Create → 🪪 Mint
2. Replace `"tool-dev-lifeops"` with your minted ID in:
   - `manifest.json` → `required_executas[0].tool_id` and `ui.host_api.tools[0]`
   - `bundle/app.js` → `const TOOL_ID = "..."`
   - `executas/lifeops/lifeops_plugin.py` → `TOOL_ID = "..."`
3. Fill in listing metadata in the Developer Console (logo, screenshots, homepage)
4. Run `anna-app validate` → green
5. `anna-app publish`

---

## File map

| File | Purpose |
|------|---------|
| `manifest.json` | Anna App manifest (schema 2, permissions, UI, dev config) |
| `app.json` | App store metadata (name, tagline, description, category) |
| `bundle/index.html` | Iframe entry point — two-column input/output layout |
| `bundle/style.css` | Complete design system (dark mode, glassmorphism, animations) |
| `bundle/app.js` | Frontend logic (AnnaAppRuntime, tools.invoke, plan renderer, storage) |
| `executas/lifeops/lifeops_plugin.py` | Python Executa — v2 protocol, sampling, plan tool |
| `executas/lifeops/pyproject.toml` | Python package config |
| `fixtures/sampling-mock.jsonl` | Mock LLM response for offline dev |
