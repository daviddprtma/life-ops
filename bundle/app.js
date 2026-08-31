/**
 * LifeOps — AI Action Planner · bundle/app.js
 *
 * Responsibilities:
 *  1. Connect to the AnnaAppRuntime host (or degrade gracefully in standalone).
 *  2. Provide a rich input form (situation text, category, optional context).
 *  3. Call tools.invoke → plan on the Python Executa.
 *  4. Render the returned structured plan (summary, urgency, tasks, next action,
 *     resources, risks) as premium UI cards.
 *  5. Persist & reload the last 5 plans via anna.storage.
 */

import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

// ── Constants ────────────────────────────────────────────────────────────────

const TOOL_ID     = "tool-dev-lifeops";
const STORAGE_KEY = "lifeops:history";
const MAX_HISTORY = 5;

// ── DOM references ───────────────────────────────────────────────────────────

const $situation   = /** @type {HTMLTextAreaElement} */ (document.getElementById("situation"));
const $category    = /** @type {HTMLSelectElement}   */ (document.getElementById("category"));
const $context     = /** @type {HTMLInputElement}    */ (document.getElementById("context"));
const $charCount   = document.getElementById("char-count");
const $btnGenerate = document.getElementById("btn-generate");
const $btnIcon     = document.getElementById("btn-icon");
const $btnLabel    = document.getElementById("btn-label");

const $mainGrid        = document.getElementById("main-grid");
const $stateEmpty      = document.getElementById("state-empty");
const $stateLoading    = document.getElementById("state-loading");
const $stateError      = document.getElementById("state-error");
const $errorMessage    = document.getElementById("error-message");
const $statePlan       = document.getElementById("state-plan");
const $historySection  = document.getElementById("history-section");
const $historyList     = document.getElementById("history-list");
const $toast           = document.getElementById("toast");

// ── State ────────────────────────────────────────────────────────────────────

let anna = null;           // AnnaAppRuntime instance (null = standalone)
let isGenerating = false;
let history = [];          // [{situation, category, plan, timestamp}]

// ── Toast utility ────────────────────────────────────────────────────────────

let _toastTimer = null;

function showToast(msg, duration = 2800) {
  $toast.textContent = msg;
  $toast.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => $toast.classList.remove("show"), duration);
}

// ── Output state machine ─────────────────────────────────────────────────────

function setOutputState(state) {
  $stateEmpty.style.display   = state === "empty"   ? "" : "none";
  $stateLoading.style.display = state === "loading" ? "" : "none";
  $stateError.style.display   = state === "error"   ? "" : "none";
  $statePlan.style.display    = state === "plan"    ? "" : "none";

  // On small screens, show/hide panels
  if (state === "plan" || state === "loading" || state === "error") {
    $mainGrid.classList.add("has-output");
  } else {
    $mainGrid.classList.remove("has-output");
  }
}

// ── Character counter ─────────────────────────────────────────────────────────

$situation.addEventListener("input", () => {
  const len = $situation.value.length;
  $charCount.textContent = `${len} / 3000`;
  $charCount.classList.toggle("warn",  len > 2000 && len <= 2800);
  $charCount.classList.toggle("limit", len > 2800);
});

// ── Plan renderer ─────────────────────────────────────────────────────────────

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function urgencyIcon(u) {
  return u === "high" ? "🔴" : u === "medium" ? "🟠" : "🟢";
}

function priorityIcon(p) {
  return p === "critical" ? "🔴" : p === "high" ? "🟠" : p === "medium" ? "🟡" : "🟢";
}

function renderPlan(plan) {
  const {
    summary = "",
    urgency = "medium",
    tasks = [],
    next_action = "",
    resources_needed = [],
    risks = [],
  } = plan;

  const tasksHtml = tasks.map((t) => `
    <div class="task-card" role="listitem">
      <div class="task-num ${escapeHtml(t.priority)}">${escapeHtml(String(t.id))}</div>
      <div class="task-content">
        <div class="task-title">${escapeHtml(t.title)}</div>
        <div class="task-meta">
          <span class="priority-dot ${escapeHtml(t.priority)}"></span>
          <span class="tag-by">⏱ ${escapeHtml(t.by_when)}</span>
        </div>
      </div>
      <div class="task-why">${escapeHtml(t.why)}</div>
    </div>
  `).join("");

  const resourcesHtml = resources_needed.length
    ? resources_needed.map((r) => `<li>${escapeHtml(r)}</li>`).join("")
    : "<li>None identified</li>";

  const risksHtml = risks.length
    ? risks.map((r) => `<li>${escapeHtml(r)}</li>`).join("")
    : "<li>None identified</li>";

  $statePlan.innerHTML = `
    <!-- Summary -->
    <div class="summary-card">
      <div class="urgency-badge ${escapeHtml(urgency)}">
        ${urgencyIcon(urgency)} ${escapeHtml(urgency)} urgency
      </div>
      <p class="summary-text">${escapeHtml(summary)}</p>
    </div>

    <!-- Next action -->
    <div class="next-action-card">
      <div class="na-icon">⚡</div>
      <div>
        <div class="na-label">Do this first</div>
        <div class="na-text">${escapeHtml(next_action)}</div>
      </div>
    </div>

    <!-- Tasks -->
    <div class="section-label">Action tasks (${escapeHtml(String(tasks.length))})</div>
    <div class="task-list" role="list" aria-label="Action tasks">
      ${tasksHtml}
    </div>

    <!-- Resources & Risks -->
    <div class="info-grid">
      <div class="info-card">
        <h4>🛠 Resources needed</h4>
        <ul>${resourcesHtml}</ul>
      </div>
      <div class="info-card">
        <h4>⚠️ Risks &amp; blockers</h4>
        <ul>${risksHtml}</ul>
      </div>
    </div>
  `;

  setOutputState("plan");
}

// ── History ───────────────────────────────────────────────────────────────────

function renderHistory() {
  if (!history.length) {
    $historySection.style.display = "none";
    return;
  }
  $historySection.style.display = "";
  $historyList.innerHTML = history.map((h, i) => {
    const date = new Date(h.timestamp);
    const timeStr = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const preview = h.situation.slice(0, 60).replace(/\n/g, " ");
    return `
      <div class="history-item" data-index="${i}" role="button" tabindex="0"
           aria-label="Load previous plan: ${escapeHtml(preview)}">
        <span class="h-cat">${escapeHtml(h.category)}</span>
        <span class="h-text">${escapeHtml(preview)}</span>
        <span class="h-time">${escapeHtml(timeStr)}</span>
      </div>
    `;
  }).join("");

  // Click to reload a previous plan
  $historyList.querySelectorAll(".history-item").forEach((el) => {
    const load = () => {
      const idx = parseInt(el.dataset.index, 10);
      const h = history[idx];
      if (!h) return;
      $situation.value = h.situation;
      $category.value  = h.category;
      $situation.dispatchEvent(new Event("input"));
      if (h.plan) renderPlan(h.plan);
    };
    el.addEventListener("click", load);
    el.addEventListener("keydown", (e) => e.key === "Enter" && load());
  });
}

async function loadHistory() {
  if (!anna) return;
  try {
    const raw = await anna.storage.get({ key: STORAGE_KEY });
    if (raw?.value) {
      history = JSON.parse(raw.value);
      renderHistory();
    }
  } catch {
    // storage not available in standalone or first run
  }
}

async function saveHistory(entry) {
  history.unshift(entry);
  if (history.length > MAX_HISTORY) history = history.slice(0, MAX_HISTORY);
  renderHistory();
  if (!anna) return;
  try {
    await anna.storage.set({ key: STORAGE_KEY, value: JSON.stringify(history) });
  } catch {
    // non-fatal
  }
}

// ── Generate flow ─────────────────────────────────────────────────────────────

function setGenerating(on) {
  isGenerating = on;
  $btnGenerate.disabled = on;
  $btnIcon.textContent  = on ? "⏳" : "✦";
  $btnLabel.textContent = on ? "Generating…" : "Generate Action Plan";
}

async function generate() {
  const situation = $situation.value.trim();
  if (!situation) {
    $situation.focus();
    showToast("Please describe your situation first.");
    return;
  }
  if (isGenerating) return;

  const category = $category.value;
  const ctx      = $context.value.trim();

  setGenerating(true);
  setOutputState("loading");

  try {
    let plan;

    if (anna) {
      // ── Real path: call the Python Executa via Anna host ──
      const result = await anna.tools.invokeAsyncAwait({
        tool_id: TOOL_ID,
        method:  "plan",
        args:    { situation, category, context: ctx || undefined },
      }, {
        // Runtime options belong in the second argument.  Putting this in
        // the request payload silently retains the SDK's 70-second default.
        timeoutMs: 120_000,
      });

      if (!result?.success) {
        throw new Error(result?.error || "The planner returned an empty response.");
      }
      plan = result.data?.plan;
      if (!plan) throw new Error("Plan data was missing from the response.");

    } else {
      // ── Standalone preview (no host) — use demo data ──
      await new Promise((r) => setTimeout(r, 1200));
      plan = _demoPlan(situation, category);
    }

    renderPlan(plan);
    await saveHistory({ situation, category, plan, timestamp: Date.now() });
    showToast("Plan generated ✓");

  } catch (err) {
    setOutputState("error");
    $errorMessage.textContent = err?.message || String(err);

    // Special hint for sampling grant error
    if (String(err).includes("SAMPLING_NOT_GRANTED") || String(err).includes("-32001")) {
      $errorMessage.textContent +=
        "\n\n💡 To fix this: go to Anna Settings → Apps → LifeOps → enable LLM sampling.";
    }
  } finally {
    setGenerating(false);
  }
}

// ── Demo plan (standalone / offline) ─────────────────────────────────────────

function _demoPlan(situation, category) {
  return {
    summary: `[Preview mode] Your ${category} situation has been analysed. This is a demo plan — connect the Anna host to get a real AI-generated plan.`,
    urgency: "medium",
    tasks: [
      {
        id: 1, priority: "critical",
        title: "Identify your single most urgent item",
        why: "Clarity on priority number one cuts overwhelm immediately.",
        by_when: "Right now",
      },
      {
        id: 2, priority: "high",
        title: "Block 30 minutes to plan the next 24 hours",
        why: "Short-horizon planning beats long-term ambition when you're overwhelmed.",
        by_when: "Today",
      },
      {
        id: 3, priority: "medium",
        title: "Delegate or defer two low-priority items",
        why: "Freeing mental bandwidth is as valuable as completing tasks.",
        by_when: "This week",
      },
    ],
    next_action: "Write down the one thing you MUST do today — then do only that until it's done.",
    resources_needed: ["Pen and paper or notes app", "15 minutes of quiet time"],
    risks: ["Trying to do everything at once", "Not asking for help when needed"],
  };
}

// ── Keyboard shortcut ─────────────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
  // Ctrl/Cmd + Enter to generate
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    generate();
  }
});

$btnGenerate.addEventListener("click", generate);

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function main() {
  setOutputState("empty");

  try {
    anna = await AnnaAppRuntime.connect();
    await anna.window.set_title({ title: "LifeOps — AI Action Planner" });
    await anna.window.ready();
    await loadHistory();
  } catch {
    // Running standalone (no Anna host) — degrade gracefully
    showToast("Preview mode (no host connected)", 4000);
  }
}

main();
