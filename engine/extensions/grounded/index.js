import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { appendFileSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, resolve as resolvePath } from "node:path";
import { homedir } from "node:os";
import { canonUrl, urlsIn, looksLikeErrorPage, downloadedUrls, classifyLinks, citedLinks } from "./links.js";

// ───────────────────────────────────────────────────────────────────────────
// "grounded" v0.12.3 — real-time grounding for a scoped agent.
//
//   1. BEFORE the answer (before_prompt_build): classify the user's QUESTION.
//        facts mode → it depends on things that change (releases, recent
//                     papers, current recommendations) → "search first".
//        code mode  → it asks for code using a library → "check installed
//                     versions + version-matched docs/introspection first".
//      The agent writes its own searches; the hook only adds instructions.
//
//   2. DURING the run (after_tool_call): record what the agent ACTUALLY did —
//      web searches, fetches, the URLs those returned, and version/doc checks
//      run through exec. This is the evidence, not the wording of the draft.
//
//   3. AFTER the draft (before_agent_finalize): if a required step was
//      skipped, or the draft cites links that no search/fetch returned
//      (likely invented), send it back ONCE with a targeted instruction.
//      If it still fails, let it ship but queue a visible warning.
//
//   4. DELIVERY: write that warning into the saved answer (before_message_write),
//      and best-effort on channel delivery (message_sending).
//      agent_end logs how each run ended, so silent failures show up.
//
// Bounded: one revise per run (own guard + host retry key, maxAttempts 1).
// Fast: every handler is synchronous string work — no network in the hook.
// Safe: read-only; never throws into a turn.
// ───────────────────────────────────────────────────────────────────────────

const STATE_TTL_MS = 2 * 60 * 60 * 1000;

const TRIVIAL_RE = /^(hi|hey|hello|thanks|thank you|ok(ay)?|sure|got it|y(es)?|no|lol|haha+|👍|🙏)[\s!.]*$/i;

// Strong signals the QUESTION depends on current information.
const FRESH_STRONG_RE =
  /\b(latest|newest|current(ly)?|recent(ly)?|up[- ]to[- ]date|nowadays|these days|this year|as of|today|releases?|released|changelog|deprecated|state[- ]of[- ]the[- ]art|sota|best practices?|recommended|benchmarks?|papers?|preprints?|published|literature|guidelines?|now|(?<![=\w.-])20[12]\d)\b/i;
// Weaker signals: only trigger facts mode when the question isn't about code.
const FRESH_WEAK_RE = /\b(versions?|updated?|still|anymore|compare|comparison|vs\.?|versus|alternatives?|studies|evidence)\b/i;

const CODE_INTENT_RE =
  /\b(write|code|script|function|implement|refactor|debug|fix|error|exception|traceback|notebook|pipeline|plot|argument|parameter|import|how (do|can|should) i|how to|example|usage|use|snippet|show me)\b|```|Traceback \(most recent call last\)/i;
// A dotted call like sc.pp.scrublet() or pl.scan_parquet() is code talk even without other cues.
const CALL_RE = /\b[A-Za-z_]\w*(\.[A-Za-z_]\w*)+\(/;
const IMPORT_RE = /\bimport\s+[A-Za-z_]\w*|\bfrom\s+[A-Za-z_][\w.]*\s+import\b|\blibrary\([A-Za-z]/;

// General-purpose libraries. Domain libraries come from packs ("libraries" in pack.json).
const DEFAULT_LIBRARIES = [
  "numpy", "pandas", "scipy", "scikit-learn", "sklearn", "torch", "pytorch", "lightning", "jax",
  "matplotlib", "seaborn", "plotly", "snakemake", "nextflow",
];

// ── Packs ────────────────────────────────────────────────────────────────
// A pack is a JSON file with the domain knowledge for one kind of work (e.g. scRNA-seq QC).
// The engine holds the rules; packs hold the vocabulary, examples, and checklist items.
// Several packs can be active at once; their contents are merged in the order given.
const GENERIC_EXAMPLES = {
  versionCheck: 'python -c "import <library>; print(<library>.__version__)"',
  signatureCheck: 'python -c "import inspect, <library>; print(inspect.signature(<library>.<function>))"',
  nativeCheck: 'python -c "import <library>; print(dir(<library>))"',
};

function loadPacks(files, log) {
  const merged = { names: [], libraries: [], preferredSources: "", examples: {}, retestHints: [], skills: [] };
  for (const f of Array.isArray(files) ? files : []) {
    try {
      const p = JSON.parse(readFileSync(String(f), "utf8"));
      merged.names.push(`${p.name || f}@${p.version || "?"}`);
      if (Array.isArray(p.libraries)) merged.libraries.push(...p.libraries);
      if (!merged.preferredSources && typeof p.preferredSources === "string") merged.preferredSources = p.preferredSources;
      if (p.examples && typeof p.examples === "object") merged.examples = { ...p.examples, ...merged.examples };
      if (typeof p.retestHint === "string" && p.retestHint) merged.retestHints.push(p.retestHint);
      if (Array.isArray(p.skills)) merged.skills.push(...p.skills.filter((x) => typeof x === "string" && x));
    } catch (err) {
      log(`pack load ERROR ${f}: ${String(err)}`);
    }
  }
  return merged;
}

// Evidence that the agent checked versions or real API signatures.
const CODE_CHECK_RE =
  /__version__|\bpip3?\s+(show|list)\b|\b(conda|mamba|micromamba)\s+(list|run)\b|importlib\.metadata|inspect\.(signature|getdoc|getsource)|\bhelp\(|pydoc|--version|__doc__|packageVersion\(|sessionInfo\(/;
// Research-first gate: commands that run analysis code (an interpreter), and the ones that never count as analysis.
const INTERP_RE = /(^|[\s;&|(\/])(python[0-9.]*|ipython|Rscript|R\s+(-e|--vanilla|-f|<)|julia|jupyter)\b/;
const VERSION_ONLY_RE = /__version__|\bpip3?\s+(show|list)\b|importlib\.metadata|inspect\.(signature|getdoc|getsource)|\bhelp\(|--version|__doc__|packageVersion\(|sessionInfo\(/;
// Fetching a documentation page also counts for code mode.
const DOCS_URL_RE =
  /readthedocs\.io|rtfd\.io|\/docs?\/|\/\/docs\.|pypi\.org|github\.com|bioconductor\.org|cran\.r-project\.org|\/api\/|\/reference\//i;

// A turn that ends with a plan instead of an answer ("I'll keep this concise and tie it
// back to..."): the last sentence announces what the agent is about to write.
const PLAN_TAIL_RE =
  /\b(I'll(?! be happy)|I will|I'm going to|I am going to|let me(?! know)|let's|now I(?:'m| am)|next,? I|I need to|I should now)\b/i;
export function looksUnanswered(draft) {
  const t = String(draft || "").trim();
  if (!t) return true;
  // An answer has substance: a code block, a sources list, or links.
  if (t.includes("```") || /\bsources?\s*[:(]/i.test(t) || citedLinks(t).length) return false;
  const sentences = t.split(/(?<=[.!?])\s+|\n+/).map((x) => x.trim()).filter(Boolean);
  const last = sentences[sentences.length - 1] || "";
  return PLAN_TAIL_RE.test(last) && t.length < 3000;
}

// Arithmetic written out in an answer ("(5 × 25) / 10 = 1.25") is recomputed. Only plain numeric
// expressions with at least one operator are checked; results may be rounded (2% tolerance).
const ARITH_RE = /([\d.\s()×*\/÷+\-−]{3,})=\s*\**\s*(-?[\d,]*\.?\d+)(\s*%)?/g;
export function arithmeticErrors(text) {
  const out = [];
  for (const m of String(text || "").matchAll(ARITH_RE)) {
    const lhs = m[1].trim();
    if (!/\d\s*\)?\s*[×*\/÷+\-−]\s*\(?\s*\d/.test(lhs) || (lhs.match(/\d+(\.\d+)?/g) || []).length < 2) continue;
    if (/^\d{4}\s*[-−]\s*\d{2,4}$/.test(lhs)) continue; // year ranges like 2021-2024
    const expr = lhs.replace(/×/g, "*").replace(/÷/g, "/").replace(/−/g, "-");
    if (!/^[\d.\s()+\-*/]+$/.test(expr)) continue;
    let val;
    try { val = Function(`"use strict"; return (${expr});`)(); } catch { continue; }
    const stated = Number(m[2].replace(/,/g, ""));
    if (m[3] && Number.isFinite(val) && Math.abs(val * 100 - stated) < Math.abs(val - stated)) val *= 100; // "1/6 = 16.7%"
    if (!Number.isFinite(val) || !Number.isFinite(stated)) continue;
    const tol = Math.max(Math.abs(val) * 0.02, 1e-9);
    if (Math.abs(val - stated) > tol) out.push({ expr: lhs, stated, actual: Number(val.toPrecision(6)) });
  }
  return out;
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function newState() {
  return {
    modes: new Set(),
    searches: 0,
    codeChecks: 0,
    execs: 0,
    editSeq: 0,
    echo: false,
    resetSinceAudit: false,
    auditedAtEdit: 0,
    gated: false,
    audits: 0,
    verdict: "",
    seenCalls: new Set(),
    auditSpawns: 0,
    auditRuns: new Set(),
    verifiedPass: false,
    lastAuditUnverified: false,
    read: new Set(), // pages actually opened this session (canonical URLs)
    seen: new Set(), // URLs that appeared in search results or tool output
    passes: 0,
    revised: false,
    touched: Date.now(),
  };
}

export default definePluginEntry({
  id: "grounded",
  name: "Grounded",
  description:
    "Real-time grounding: search-first for current facts, version-matched docs for code, verified against what the agent actually did.",
  register(api) {
    const cfg = api.pluginConfig || {};
    const agents = Array.isArray(cfg.agents) ? cfg.agents : [];
    const enableFacts = cfg.enableFacts !== false;
    const enableCode = cfg.enableCode !== false;
    // Audit gate: agents whose code must pass an independent auditor agent.
    const auditAgents = Array.isArray(cfg.auditAgents) ? cfg.auditAgents : [];
    const auditorId = typeof cfg.auditorId === "string" && cfg.auditorId ? cfg.auditorId : "auditor";
    // Dedicated audit session (kept separate from the auditor's main chat); reset before every round.
    const auditSessionKey =
      typeof cfg.auditSessionKey === "string" && cfg.auditSessionKey ? cfg.auditSessionKey : `agent:${auditorId}:audits`;
    const requireFreshAudit = cfg.requireFreshAudit !== false;
    const gateInstalls = cfg.gateInstalls !== false;
    const autoSkipEcho = cfg.autoSkipEcho !== false;
    const ECHO_TEXT_RE = /^\s*\[Inter-session message\]/;
    // Echo handling applies only to messages from this agent's own auditor. Messages from
    // any other session (another agent, a relay) are ordinary turns with all tools.
    function fromOwnAuditor(ctx, text) {
      const src = String(ctx?.inputProvenance?.sourceSessionKey || "");
      if (src) return src.startsWith(`agent:${auditorId}:`);
      return new RegExp(`sourceSession(?:Key)?=agent:${escapeRe(auditorId)}:`).test(String(text || ""));
    }
    const INSTALL_RE =
      /\b(pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(install|uninstall)\b|\b(conda|mamba|micromamba)\s+(install|create|update|remove|uninstall|env\s+(create|update|remove))\b|\buv\s+(add|remove|sync)\b|\bnpm\s+(i|install)\s+(-g|--global)\b|\bapt(-get)?\s+(install|remove|purge)\b|install\.packages\(|BiocManager::install\(|remotes::install_|devtools::install_/;
    const auditorSessionKey =
      typeof cfg.auditorSessionKey === "string" && cfg.auditorSessionKey ? cfg.auditorSessionKey : `agent:${auditorId}:main`;
    // Eval runs: a requester session ending in ":eval-<tag>" talks to a dedicated auditor
    // session "agent:<auditor>:eval-<tag>" (created by the eval runner), so the auditor's
    // main session is never touched.
    const EVAL_KEY_RE = /:eval-([^:\s]+)$/;
    function auditorTarget(sessionKey) {
      const m = String(sessionKey || "").match(EVAL_KEY_RE);
      if (m) {
        const key = `agent:${auditorId}:eval-${m[1]}`;
        return { call: `sessionKey: "${key}"`, history: key };
      }
      return { call: `sessionKey: "${auditSessionKey}"`, history: auditSessionKey };
    }
    function auditKeyFor(sessionKey) {
      const m = String(sessionKey || "").match(EVAL_KEY_RE);
      return m ? `agent:${auditorId}:eval-${m[1]}` : auditSessionKey;
    }
    const SESSIONS_TOOL_RE = /^sessions$/i;
    const SEND_RE = /^sessions?_send$/i;
    const HISTORY_RE = /^sessions?_history$/i;
    const SPAWN_RE = /^sessions?_spawn$/i;
    const WAIT_RE = /^agents?_wait$/i;
    const requirePass = cfg.requirePassVerdict !== false;
    // A PASS only counts if the audit-gate plugin recorded real verification in that auditor run.
    const requireVerifiedPass = cfg.requireVerifiedPass !== false;
    const minAuditLines = Number.isFinite(cfg.minAuditLines) ? cfg.minAuditLines : 10;
    const maxAuditRounds = Number.isFinite(cfg.maxAuditRounds) ? Math.max(1, cfg.maxAuditRounds) : 3;
    // Shared, in-process record written by the audit-gate plugin: runId -> { verdict, verifications, checks }.
    const auditEvidence = () => globalThis.__auditEvidence;
    // A PASS counts if the auditor ran real checks and its own premise check did not flag a
    // problem that the PASS then ignored (audit-gate records that as premiseConflict).
    const passCounts = (rec) => rec.verifications > 0 && !rec.premiseConflict;
    // Agents that get facts grounding only (e.g. the auditor reviews code but shouldn't be forced to re-run version checks).
    const factsOnlyAgents = Array.isArray(cfg.factsOnlyAgents) ? cfg.factsOnlyAgents : [];
    const auditTimeout = Number.isFinite(cfg.auditTimeoutSeconds) ? cfg.auditTimeoutSeconds : 1200;
    // Structured result the auditor must return (enforced by sessions_spawn outputSchema).
    const AUDIT_SCHEMA = {
      type: "object",
      required: ["verdict", "findings"],
      properties: {
        verdict: { enum: ["PASS", "REVISE"] },
        summary: { type: "string" },
        findings: {
          type: "array",
          items: {
            type: "object",
            required: ["severity", "title", "problem", "fix"],
            properties: {
              severity: { enum: ["HIGH", "MEDIUM", "LOW"] },
              title: { type: "string" },
              where: { type: "string" },
              principle: { type: "string" },
              problem: { type: "string" },
              evidence: { type: "string" },
              fix: { type: "string" },
            },
          },
        },
      },
    };
    const SCHEMA_TEXT = JSON.stringify(AUDIT_SCHEMA);

    function textOf(v) {
      if (typeof v === "string") return v;
      try { return JSON.stringify(v ?? ""); } catch { return ""; }
    }
    // Pull a verdict out of whatever shape the tool result has (structured or text).
    function findVerdict(v) {
      const t = textOf(v);
      const structured = [...t.matchAll(/\\?"verdict\\?"\s*:\s*\\?"(PASS|REVISE)/gi)];
      if (structured.length) return structured[structured.length - 1][1].toUpperCase();
      const plain = [...t.matchAll(/AUDIT:\s*(PASS|REVISE)/gi)];
      return plain.length ? plain[plain.length - 1][1].toUpperCase() : "";
    }
    const defaultEnv = typeof cfg.defaultEnv === "string" ? cfg.defaultEnv : "";
    // The user's analysis interpreter. When set, all checks must use it.
    const checkPython = typeof cfg.checkPython === "string" ? cfg.checkPython : "";
    const editTools = new Set(cfg.editTools || ["write", "edit", "apply_patch"]);
    // Where the agent may write without asking. Empty = no write-scope gate.
    const expandHome = (p) => String(p || "").replace(/^~(?=\/|$)/, homedir()).replace(/^\$HOME(?=\/|$)/, homedir());
    const writeRoots = (Array.isArray(cfg.writeRoots) ? cfg.writeRoots : [])
      .map((p) => resolvePath(expandHome(p)).replace(/\/+$/, "") + "/");
    // After a PASS, the first edit to a code file is bounced once with a reminder.
    const nudgePostPassEdits = cfg.nudgePostPassEdits !== false;
    // Baseline for experiments: keep only the safety gates (installs, writes outside the
    // workspace); no grounding reminders, no audit loop, no checks on the answer.
    const gatesOnly = cfg.gatesOnly === true;
    // Experiment: research brief first. Every non-trivial task starts with a short research
    // pass from primary sources, written to a brief file the agent then works from.
    const briefFirst = cfg.briefFirst === true;
    // Experiment: claim check. On the first finished draft of a brief task, pig lists the factual
    // claims its answer relies on, matches each to a line of its brief, looks up the ones the brief
    // does not cover, appends them to the brief, and revises. Shares the plugin's single revise pass.
    const claimCheck = briefFirst && cfg.claimCheck === true;
    // Experiment: map first. The brief starts with a map of the task and data (what is unusual,
    // which decisions the result depends on), then research aimed at those decisions.
    const mapFirst = briefFirst && cfg.mapFirst === true;
    // Experiment: research first. After a few inspection runs (for the map), commands that run analysis
    // code are held until the brief cites >=2 pages opened this run and a search has happened.
    const researchFirst = briefFirst && cfg.researchFirst === true;
    const inspectRuns = Number.isInteger(cfg.inspectRuns) ? cfg.inspectRuns : 3;
    // Answer files (e.g. a benchmark's analysis_output.json). With claimCheck, the first tool call that
    // writes one is blocked once with the claim-check instruction, because OpenClaw does not honor
    // finalize revises in CLI runs (0 of 25 since 8 Oct). Chat-only answers get the instruction up front.
    const answerFiles = Array.isArray(cfg.answerFiles) ? cfg.answerFiles.filter((x) => typeof x === "string" && x) : [];
    function claimText(path) {
      return `CLAIM CHECK against your brief (${path}). ` +
        "List every factual claim your answer relies on (what a gene or marker indicates, what a method or default does, expected values or ranges, what a source says), " +
        "including claims you made during the analysis. For each decision in your Map, include the judgment itself as a claim " +
        "('X is the right method/parameter for this data because Y'), not only facts about the tools; judgments are where errors hide. " +
        "Write each as a short standalone sentence (e.g. 'Sox17 is a pericyte marker', not 'Sox17 is up here, a classic marker'). " +
        "Table: claim | brief line | SUPPORTED (the brief says this) or CONTRADICTED (the brief says otherwise) or NOT COVERED | load-bearing (would the answer change if this claim were wrong?) yes/no. " +
        "For every CONTRADICTED or NOT COVERED claim that is load-bearing, search for it now and open 1-2 primary sources; append what you find to the brief under a heading '## Added by claim check' with the URL. " +
        "If a source contradicts a claim, redo the affected part of the analysis. Mark claims that are unsupported but not load-bearing [UNVERIFIED]. " +
        "Include the table in your final answer.";
    }
    const briefDir = (typeof cfg.briefDir === "string" && cfg.briefDir
      ? expandHome(cfg.briefDir)
      : (writeRoots[0] || "") + "briefs").replace(/\/+$/, "");
    // Experiment: a research-method guide (e.g. the research-101 skill) the agent reads before searching.
    const researchGuide = typeof cfg.researchGuide === "string" && cfg.researchGuide ? expandHome(cfg.researchGuide) : "";
    // Benchmarks: tool calls whose arguments match any of these patterns are blocked (answer
    // keys, benchmark repos, other models' published answers). Matched case-insensitively.
    const blockedPatterns = (Array.isArray(cfg.blockedPatterns) ? cfg.blockedPatterns : [])
      .map((p) => { try { return new RegExp(expandHome(p), "i"); } catch { log(`config: bad blockedPatterns entry ${p}`); return null; } })
      .filter(Boolean);
    const BRIEF_PATH_RE = /(?<![\w.-])briefs\/[^\s"'<>]+\.md\b/;
    const CODE_FILE_RE = /\.(py|r|R|sh|ipynb|qmd|rmd|Rmd|smk|nf)\b/;
    // exec commands that write a code file (heredoc, redirect, tee, sed -i)
    const EXEC_WRITE_RE = /(cat|tee|printf|echo)\b[^|]*>\s*\S+\.(py|R|r|sh|ipynb)\b|\btee\s+(-a\s+)?\S+\.(py|R|r|sh)\b|\bsed\s+-i\b[^\n]*\.(py|R|r|sh)\b/;
    // "I checked / verified / tested ..." style claims
    const CLAIM_CHECKED_RE =
      /\b(I (have |also )?(checked|verified|confirmed|tested|ran|introspected|inspected|reproduced)|(verified|checked|tested|confirmed) (it |this )?(against|in|on|via|with)|introspected|(ran|run) (it|the code|the test))\b/i;
    const pack = loadPacks(cfg.packFiles, (m) => log(m));
    log(`packs loaded: ${pack.names.join(", ") || "(none)"}`);
    const userName = typeof cfg.userName === "string" && cfg.userName ? cfg.userName : "the user";
    const userPoss = userName === "the user" ? "the user's" : `${userName}'s`;
    const sources = pack.preferredSources || "official documentation, release notes, and primary literature";
    const ex = { ...GENERIC_EXAMPLES, ...pack.examples };
    const retestHint = pack.retestHints.length ? `, ${pack.retestHints.join(", ")}` : "";
    const libraries = [...DEFAULT_LIBRARIES, ...pack.libraries, ...(Array.isArray(cfg.extraLibraries) ? cfg.extraLibraries : [])];
    const LIB_RE = new RegExp(`(?<![\\w-])(${libraries.map(escapeRe).join("|")})(?![\\w-])`, "i");

    const searchTools = new Set(cfg.searchTools || ["web_search"]);
    const fetchTools = new Set(cfg.fetchTools || ["web_fetch"]);
    const execTools = new Set(cfg.execTools || ["exec", "bash"]);
    // Deferred tools are invoked through a wrapper, e.g.
    //   tool_call(id="openclaw:core:web_search", args={...})
    // so unwrap to the real tool name and its args.
    const wrapperTools = new Set(cfg.wrapperTools || ["tool_call"]);

    function resolveTool(event) {
      const name = String(event?.toolName || "");
      const params = event?.params ?? {};
      if (!wrapperTools.has(name)) return { name, args: params };
      const id = params.id ?? params.name ?? params.tool ?? params.toolId ?? "";
      const args = params.args ?? params.arguments ?? params.input ?? params.params ?? {};
      return { name: String(id).split(":").pop(), args: typeof args === "object" && args ? args : {} };
    }

    const byRun = new Map();
    const bySession = new Map();
    const pendingWarnings = new Map();

    function log(msg) {
      try {
        const file = cfg.logFile || `${process.env.OPENCLAW_STATE_DIR || "/tmp"}/logs/grounded-audit.log`;
        mkdirSync(dirname(file), { recursive: true });
        appendFileSync(file, `[${new Date().toISOString()}] ${msg}\n`);
      } catch { /* never break a turn over logging */ }
    }

    function describe(problems) {
      return problems
        .map((p) =>
          p === "facts"
            ? "no successful web search for current facts"
            : p === "code"
              ? "no version/docs check for code"
              : p === "audit"
                ? "no independent audit of the code"
                : p === "audit-open"
                  ? "audit verdict was REVISE and no passing re-audit followed"
                  : p === "verdict"
                    ? "the answer misstates the auditor's verdict"
                    : p === "audit-unverified"
                      ? "the auditor's PASS had no verification evidence"
                      : p === "audit-stale"
                        ? "code was changed after the last audit"
                        : p === "unbacked-claim"
                          ? "the answer claims checks that were never run"
                          : p === "math"
                            ? "written-out arithmetic does not compute"
                          : p === "brief"
                            ? "no research brief was written before the work"
                          : p === "no-answer"
                            ? "the turn ended with a plan, not an answer"
                          : p === "links-unread"
                            ? "it cites pages that were never opened, so their content was not the source"
                    : "it cites links that appear in no search result or opened page",
        )
        .join("; ");
    }

    function researchState(s) {
      const bp = s.brief || s.ledger?.brief || "";
      const path = bp ? (isAbsolute(bp) ? bp : resolvePath(dirname(briefDir), bp)) : "";
      let txt = "";
      if (path) { try { txt = readFileSync(path, "utf8"); } catch { /* not written yet */ } }
      const opened = [...new Set(txt.match(/https?:\/\/[^\s)>\]"']+/g) || [])].filter((u) => s.read.has(canonUrl(u))).length;
      return { path, txt, opened, ok: !!txt && opened >= 2 && s.searches > 0 };
    }
    function fileSize(p) {
      try { return statSync(p).size; } catch { return -1; }
    }
    function norm(t) {
      return String(t || "").replace(/\s+/g, " ").trim();
    }

    function queueWarning(s, problems) {
      const key = s.sessionKey || s.runId;
      if (!key) return;
      pendingWarnings.set(key, {
        at: Date.now(),
        text: `\n\n⚠️ Grounded: this answer could not be fully verified (${describe(problems)}). Treat it with caution.`,
      });
      log(`warning queued (${problems.join(",")}) session=${key}`);
    }

    // Audit status of the code this turn is delivering. Shared by finalize and by the
    // saved-message writer, so the same rules decide both the revise and the warning.
    function auditStatus(s, draft) {
      const problems = [];
      let capped = false;
      const longestBlock = Math.max(0, ...[...String(draft || "").matchAll(/```[^\n]*\n([\s\S]*?)```/g)].map((m) => m[1].split("\n").length));
      const deliversCode = s.editSeq > 0 || longestBlock >= minAuditLines;
      if (s.modes.has("code") && s.gated && deliversCode) {
        capped = s.audits >= maxAuditRounds;
        if (s.audits === 0) problems.push("audit");
        else if (requirePass && s.verdict === "REVISE" && !capped) problems.push("audit-open");
        else if (requireVerifiedPass && s.verdict === "PASS" && s.lastAuditUnverified && !capped) problems.push("audit-unverified");
        else if (s.editSeq > s.auditedAtEdit && !capped) problems.push("audit-stale");
        // Only an explicit verdict line counts as a claim ("without a verified PASS" is not one).
        const claims = [...String(draft || "").matchAll(/\b(?:AUDIT|verdict)\s*[:=]\s*[*_`"']*\s*(?:AUDIT:\s*)?(PASS|REVISE)\b/gi)].map((m) => m[1].toUpperCase());
        if (claims.length && claims[claims.length - 1] === "PASS" && s.verdict !== "PASS") problems.push("verdict");
      }
      const cappedOpen = capped && (s.verdict === "REVISE" || s.lastAuditUnverified);
      return { problems, capped, cappedOpen, deliversCode };
    }

    function warningText(s, st) {
      if (st.cappedOpen) {
        return `\n\n⚠️ Audit not closed after ${s.audits} rounds (latest verdict: ${s.verdict}${s.lastAuditUnverified ? ", unverified" : ""}). Review the unresolved findings before relying on this code.`;
      }
      if (st.problems.length) {
        return `\n\n⚠️ Grounded: this answer could not be fully verified (${describe(st.problems)}). Treat it with caution.`;
      }
      return "";
    }

    // Every check run on a finished answer. Shared by finalize (revise once) and the
    // saved-message writer (warning in the answer), so both apply the same rules.
    // Returns null when the turn has nothing to check.
    function checkDraft(s, draft) {
      const problems = [];
      // Claims of checking with nothing in the log to back them.
      if (s.modes.size && CLAIM_CHECKED_RE.test(draft) && s.searches === 0 && s.codeChecks === 0 && s.execs === 0) problems.push("unbacked-claim");
      // Links are checked on every answer, classified or not.
      const links = classifyLinks(draft, s.read, s.seen);
      if (links.unknown.length) problems.push("links");
      if (links.seenOnly.length) problems.push("links-unread");
      // Written-out arithmetic must be right (checked on every answer, classified or not).
      const math = arithmeticErrors(draft);
      if (math.length) problems.push("math");
      s.mathErrors = math;
      if (!s.modes.size && !problems.length) return null;
      if (s.modes.has("facts") && s.searches === 0) problems.push("facts");
      if (s.modes.has("code") && s.codeChecks === 0) problems.push("code");
      if (briefFirst && !s.brief && !s.ledger?.brief) problems.push("brief");
      if ((s.answerGateMissing || s.researchFirstViolated) && !problems.includes("brief")) problems.push("brief");
      // The turn must end with the answer, not with a plan to write it.
      if (looksUnanswered(draft)) problems.push("no-answer");
      // Audits are for delivered code: files written this turn, or a substantial code block in the answer.
      const st = auditStatus(s, draft);
      problems.push(...st.problems);
      const unbacked = links.unknown;
      return { problems, unbacked, links, st };
    }

    function fullWarning(s, chk) {
      const parts = [];
      if (chk.st.cappedOpen) parts.push(warningText(s, chk.st).trim());
      if (chk.problems.includes("no-answer")) {
        parts.push("⚠️ Grounded: no answer was delivered. The turn ended with a plan instead of the answer; reply \"answer\" to get it.");
        return "\n\n" + parts.join("\n\n");
      }
      if (chk.problems.length) {
        let t = `⚠️ Grounded: this answer could not be fully verified (${describe(chk.problems)}).`;
        if (chk.links?.unknown.length) t += ` Unverified links (not found in any search or opened page): ${chk.links.unknown.slice(0, 5).join(", ")}.`;
        if (chk.links?.seenOnly.length) t += ` Cited but never opened: ${chk.links.seenOnly.slice(0, 5).join(", ")}.`;
        if (s.mathErrors?.length) t += ` Arithmetic check: ${s.mathErrors.slice(0, 3).map((e) => `${e.expr} = ${e.actual}, not ${e.stated}`).join("; ")}.`;
        parts.push(t + " Treat it with caution.");
      }
      return parts.length ? "\n\n" + parts.join("\n\n") : "";
    }

    function isPathAllowed(target, ctx) {
      if (!writeRoots.length || !target) return true;
      const raw = expandHome(target);
      if (!raw.startsWith("/")) return !raw.split("/").includes(".."); // relative: inside the workspace unless it climbs out
      const abs = resolvePath(raw);
      const roots = [...writeRoots];
      if (typeof ctx?.workspaceDir === "string" && ctx.workspaceDir) roots.push(resolvePath(ctx.workspaceDir).replace(/\/+$/, "") + "/");
      return roots.some((r) => (abs + "/").startsWith(r));
    }

    function isScoped(ctx) {
      return agents.includes(ctx?.agentId);
    }

    function prune() {
      const now = Date.now();
      for (const m of [byRun, bySession]) {
        for (const [k, s] of m) if (now - s.touched > STATE_TTL_MS) m.delete(k);
      }
      for (const [k, w] of pendingWarnings) if (now - w.at > STATE_TTL_MS) pendingWarnings.delete(k);
      for (const [k, l] of ledgers) if (now - l.at > STATE_TTL_MS) ledgers.delete(k);
    }

    // Look up this turn's state by runId first, then sessionKey, and link both.
    // Links read or seen anywhere in a session stay valid for later turns of that session.
    const ledgers = new Map();
    function ledgerFor(sk) {
      if (!sk) return null;
      let l = ledgers.get(sk);
      if (!l) ledgers.set(sk, (l = { read: new Set(), seen: new Set(), brief: "", at: Date.now() }));
      l.at = Date.now();
      return l;
    }

    function getState(ctx, event, fresh) {
      prune();
      const runId = ctx?.runId || event?.runId;
      const sk = ctx?.sessionKey;
      let s = fresh ? newState() : (runId && byRun.get(runId)) || (sk && bySession.get(sk));
      if (!s) return undefined;
      if (fresh) {
        const l = ledgerFor(sk);
        if (l) { s.read = l.read; s.seen = l.seen; s.ledger = l; }
      }
      if (runId) byRun.set(runId, s);
      if (sk) bySession.set(sk, s);
      s.runId ??= runId;
      s.sessionKey ??= sk;
      s.touched = Date.now();
      return s;
    }

    // ── 1. BEFORE: classify the question and nudge ──────────────────────
    api.on("before_prompt_build", (event, ctx) => {
      try {
        if (!isScoped(ctx)) return;
        if (gatesOnly) {
          log(`prompt: gates-only baseline (no grounding, no audit) run=${ctx?.runId || "(none)"}`);
          return;
        }
        const runId = String(ctx?.runId || "");
        // A revision pass re-runs prompt building for the SAME run: keep its state
        // (passes, revised flag, evidence) instead of starting over.
        if (runId && byRun.has(runId)) {
          log(`prompt: revision pass of an existing run (state kept) run=${runId}`);
          return;
        }
        // System-triggered continuation runs (exec-completion notices, heartbeats) can carry
        // on an unfinished task in the same session under a new run id. They inherit the open
        // task's state, so the work cannot slip out of the harness.
        const sk = ctx?.sessionKey;
        const prev = sk ? bySession.get(sk) : undefined;
        const internal = ctx?.inputProvenance?.kind === "internal_system";
        if (internal && !runId.startsWith("skill-workshop") && prev && prev.modes.size && !prev.closed && !prev.echo) {
          if (runId) byRun.set(runId, prev);
          prev.runId = runId || prev.runId;
          prev.touched = Date.now();
          prev.revised = false; // the continuation gets its own chance to be sent back
          log(`prompt: continuation run inherits open task modes=[${[...prev.modes].join(",")}] audits=${prev.audits} run=${runId || "(none)"}`);
          return {
            appendContext:
              "[GROUNDING STILL REQUIRED] You are continuing the user's unfinished task. Everything from the original instructions still applies: " +
              "check versions in the analysis environment, test the code, and if you deliver code, get it audited " +
              "(reset the audit session, then sessions_send) before your final answer. Report audit verdicts exactly.",
          };
        }
        // Otherwise start a fresh state so a skipped turn never inherits the previous turn's.
        const s = getState(ctx, event, true);
        s.gated = auditAgents.includes(ctx?.agentId);
        if (internal || runId.startsWith("skill-workshop")) {
          log(`prompt skip (internal run) run=${runId || "(none)"}`);
          return;
        }
        // For audit-gated agents, a message relayed from another session (e.g. the
        // auditor's reply being echoed into the chat) is not a new task.
        const interSession = ctx?.inputProvenance?.kind === "inter_session" || ECHO_TEXT_RE.test(String(event?.currentUserMessage ?? event?.prompt ?? ""));
        if (s.gated && interSession && !fromOwnAuditor(ctx, event?.currentUserMessage ?? event?.prompt)) {
          log(`prompt: inter-session message not from ${auditorId} → normal turn run=${runId || "(none)"}`);
        }
        if (s.gated && interSession && fromOwnAuditor(ctx, event?.currentUserMessage ?? event?.prompt)) {
          // Agent-to-agent continuation or a re-delivered copy of a reply already handled:
          // end the exchange instead of answering it (stops OpenClaw's reply-back loop).
          s.echo = true;
          log(`prompt: inter-session message → asked to REPLY_SKIP run=${runId || "(none)"}`);
          return {
            toolsAllow: [],
            appendContext:
              `[AGENT-TO-AGENT] This message came from another agent session, not from ${userName}. ` +
              "If it continues an exchange you have already finished, or repeats a reply you already received and acted on, " +
              "reply with exactly REPLY_SKIP and nothing else. Only reply normally if it contains something new you still need to act on.",
          };
        }

        // Prefer the explicit current request; "" means no textual request.
        const q =
          typeof event?.currentUserMessage === "string"
            ? event.currentUserMessage
            : typeof event?.prompt === "string"
              ? event.prompt
              : "";
        if (!q.trim() || TRIVIAL_RE.test(q.trim())) {
          log(`prompt: trivial/empty → no grounding run=${runId || "(none)"}`);
          return;
        }

        const code = enableCode && !factsOnlyAgents.includes(ctx?.agentId) && ((CODE_INTENT_RE.test(q) && (LIB_RE.test(q) || IMPORT_RE.test(q) || q.includes("```"))) || CALL_RE.test(q));
        const facts = enableFacts && (FRESH_STRONG_RE.test(q) || (!code && FRESH_WEAK_RE.test(q)));
        if (facts || briefFirst) s.modes.add("facts");
        if (code) s.modes.add("code");
        log(`prompt: modes=[${[...s.modes].join(",") || "none"}] run=${runId || "(none)"} q="${q.slice(0, 80).replace(/\s+/g, " ")}"`);
        if (!s.modes.size) return;

        // Bolt-on: these reminders add checks to the agent's own way of working (its skills,
        // tools, and memory); they never tell it to stop using them.
        const parts = ["[GROUNDING REQUIRED] These checks add to your usual way of working; keep using your skills and tools."];
        parts.push(
          "LINKS: a source counts only if you opened its page in this session (web_fetch, or curl/wget) and took the claim from what the page says. " +
            "Search results and links found inside other pages are leads: open them before citing, or mark them [UNVERIFIED]. " +
            "Never write a URL from memory; name the package or paper instead. Leaving a link out is always fine.",
        );
        if (briefFirst) {
          const prior = s.ledger?.brief;
          parts.push(
            prior
              ? `BRIEF: your research brief for this conversation is ${prior}. Reread it and work from it; add to it only if this turn needs information it lacks.`
              : mapFirst
                ? "MAP, THEN RESEARCH, THEN WORK. " +
                  `1) Map: before searching, look at what you are working with. Read the task closely and, if there is data, inspect it cheaply (shape, metadata columns, layers, value ranges, sparsity; no heavy compute). ` +
                  `Start the brief at ${briefDir}/<short-topic>.md with a '## Map' section: what the data and problem are, what is unusual about them, and the 3-5 decisions the result depends on, each with how it could go wrong for this data. ` +
                  "2) Research: for each decision, find how it should be done today, using up to 5-10 primary sources (official docs for the installed versions, method papers, reviews, benchmarks, protocols). " +
                  "Two independent primary sources settle a decision; one is a lead, so say so. If a search finds nothing, rephrase or follow citations instead of stopping. " +
                  "Add a '## Research' section: per decision, the recommended choice, its pitfalls, and whether it should come from the data, each point with the URL of the page it came from. Keep the brief to about a page; stop when new sources stop changing the picture. " +
                  (researchFirst ? `Analysis runs are held until the brief cites at least 2 pages you opened; your first ${inspectRuns} runs are for inspecting the data for the map. ` : "") +
                  "3) Work from the brief. If the task prescribes a method or parameters, follow them exactly and use the research to interpret the result, not to change the method. Say where and why you depart from the brief."
                : "BRIEF FIRST: before doing the task, do a short research pass and write a brief, then work from it. " +
                  "Search for and open 2-4 primary sources on how this kind of task should be done today: the official docs or API reference of the libraries involved " +
                  `(look for the library's own function first) and a current paper, review, or guideline on the method (${sources}). ` +
                  `Write the brief to ${briefDir}/<short-topic>.md, under one page: the recommended approach, the library functions for it, common pitfalls, ` +
                  "and which choices should come from the data instead of fixed defaults, each point with the URL of the page it came from. " +
                  "Then do the task from the brief, and say where and why you depart from it.",
          );
        }
        if (claimCheck) {
          parts.push("Before your final answer" + (answerFiles.length ? " (and before writing the answer file)" : "") + ", run this check. " + claimText(`${briefDir}/<your brief>.md`));
        }
        if (researchGuide && s.modes.has("facts") && !s.ledger?.guideRead) {
          parts.push(`RESEARCH METHOD: before you search, read ${researchGuide} and follow it for choosing and citing sources.`);
        }
        if (pack.skills.length) {
          parts.push(`SKILLS: if one of your skills covers this task (e.g. ${pack.skills.slice(0, 4).join(", ")}), read it first and follow it.`);
        }
        if (facts) {
          parts.push(
            "FACTS: this depends on information that may have changed since your training. Before answering, web_search the specific facts " +
              `(versions, releases, recent papers, current recommendations), then open the most relevant primary sources (${sources}) and answer from what they say. ` +
              "Blogs and tutorials are leads; when a claim comes from a paper, open the paper. " +
              "If web_search/web_fetch are not directly callable, use tool_search and tool_call (openclaw:core:web_search, openclaw:core:web_fetch); " +
              "web_fetch returns static HTML only, so use an API, PyPI, or a static mirror for JS pages. " +
              "End with a 'Sources:' list of pages you opened. Mark anything you could not verify [UNVERIFIED]. " +
              `${userPoss} own project files and data stay authoritative for their own results.`,
          );
        }
        if (code) {
          if (writeRoots.length) {
            parts.push(`FILES: save scripts and outputs under ${writeRoots[0].replace(/\/$/, "")} or the project folder you were given (scratch in /tmp). Writing elsewhere needs ${userPoss} approval.`);
          }
          const interp = checkPython
            ? `${checkPython} (${userPoss} analysis environment), unless the project folder has a .harness-python file naming another interpreter; then use that one`
            : defaultEnv
              ? `the project's environment (a .harness-python file in the project folder names it if present; otherwise conda env "${defaultEnv}")`
              : "the project's environment (a .harness-python file in the project folder names it if present; if unclear, ask)";
          parts.push(
            `CODE: run every version check, introspection, and test with ${interp}. ` +
              `Before writing code, check the installed versions of the libraries you use (e.g. ${ex.versionCheck}) and confirm each function's ` +
              `signature and defaults for that version (e.g. ${ex.signatureCheck}). ` +
              `If the user asks how to do something in a specific library, look for that library's own function first (e.g. ${ex.nativeCheck}). ` +
              "Ask before installing packages or creating environments. If feasible, run the code on a tiny subset. State which versions you checked.",
          );
          if (s.gated) {
            parts.push(
              "AUDIT REQUIRED: once the code is written and tested, write your full draft answer and get it reviewed by the auditor before delivering it " +
                "(if a tool is not directly callable, use tool_call). Each audit round:\n" +
                `  1. sessions tool, action "reset", key "${auditKeyFor(ctx?.sessionKey)}" (reset only, never delete), so the reviewer starts fresh.\n` +
                `  2. sessions_send({${auditorTarget(ctx?.sessionKey).call}, timeoutSeconds: ${auditTimeout}, message: "<audit request>"}) and wait. ` +
                `With status "ok" the report is in \`reply\`; on "timeout" or "accepted", read it with sessions_history ("${auditorTarget(ctx?.sessionKey).history}") instead of guessing.\n` +
                "The request contains: the user's question, your FULL DRAFT ANSWER exactly as you will deliver it (prose, code, every link), versions checked, test results, open doubts. " +
                "For a re-audit, send the current draft plus a numbered list of the fixes you claim, and ask the reviewer to verify each one by running something; do not paste the old review. " +
                `Fix every HIGH and MEDIUM finding (or explain why not) and re-audit, for at most ${maxAuditRounds} rounds. ` +
                "A PASS counts only if the report has a 'Verification:' section of checks the auditor ran; otherwise send it back asking for real checks. " +
                "If you change the substance after a PASS (code, claims, links), re-audit. " +
                `After ${maxAuditRounds} rounds without a verified PASS, deliver the best version and list the unresolved findings. ` +
                "In the final answer, quote each verdict exactly as returned (\"AUDIT: PASS\" / \"AUDIT: REVISE\") with its round and what you changed per finding. " +
                "To claim something doesn't exist or can't run, try it. A repeated auditor message is a duplicate: acknowledge it in one line.",
            );
          }
        }
        return { appendContext: parts.join("\n") };
      } catch (err) {
        log(`prompt ERROR ${String(err)}`);
      }
    });

    // ── 1a. ECHO TURNS: answer agent-to-agent echoes without calling the model ──
    api.on("before_agent_reply", (event, ctx) => {
      try {
        if (!autoSkipEcho || !auditAgents.includes(ctx?.agentId)) return;
        const text = [event?.prompt, event?.currentUserMessage, event?.message?.text, event?.text, event?.body]
          .find((v) => typeof v === "string") || "";
        const interSession = ctx?.inputProvenance?.kind === "inter_session" || ECHO_TEXT_RE.test(text);
        if (!interSession || !fromOwnAuditor(ctx, text)) return;
        log(`echo: claimed agent-to-agent turn with REPLY_SKIP (no model call) run=${ctx?.runId || event?.runId || "(none)"}`);
        return { handled: true, reply: { text: "REPLY_SKIP" } };
      } catch (err) {
        log(`echo ERROR ${String(err)}`);
      }
    });

    // ── 1b. GATES: block or ask before certain tool calls ──────────────
    // A blocked call's reason goes back to the model, which makes this the one
    // in-loop channel a plugin has in this OpenClaw version.
    api.on("before_tool_call", (event, ctx) => {
      try {
        if (!isScoped(ctx)) return;
        const s = getState(ctx, event, false);
        const { name, args } = resolveTool(event);
        let blob = "";
        try { blob = JSON.stringify(args ?? {}); } catch { /* ignore */ }
        const runId = ctx?.runId || event?.runId || "(none)";

        // Echo turns (agent-to-agent continuations) must not start new work.
        if (s?.echo) {
          log(`gate: blocked ${name} in echo turn run=${runId}`);
          return {
            block: true,
            blockReason: "This turn is an agent-to-agent continuation of work you already finished. Do not call tools; reply with exactly REPLY_SKIP.",
          };
        }

        // Off-limits resources (benchmark answer keys): blocked in every mode, baseline included.
        const hit = blockedPatterns.find((re) => re.test(blob));
        if (hit) {
          log(`gate: blocked ${name} matching off-limits pattern ${hit.source} run=${runId}`);
          return {
            block: true,
            blockReason: "That resource is off-limits for this task. Work from the data and tools you have, and do not look for this task's published solution.",
          };
        }

        // Research-first gate: analysis runs wait for the research (inspection runs for the map are allowed).
        if (researchFirst && s && execTools.has(name) && !s.researchOpen) {
          const cmd = String(args?.command ?? args?.cmd ?? blob);
          const analysis = INTERP_RE.test(cmd) && !VERSION_ONLY_RE.test(cmd) && !/https?:\/\//.test(cmd);
          if (analysis) {
            const r = (s.researchGate ||= { inspect: 0, blocks: 0 });
            const st = researchState(s);
            if (st.ok) {
              s.researchOpen = true;
              log(`research-gate: open (opened=${st.opened} searches=${s.searches}) after ${r.inspect} inspection run(s), ${r.blocks} block(s) run=${runId}`);
            } else if (r.inspect < inspectRuns) {
              r.inspect += 1;
              log(`research-gate: inspection run ${r.inspect}/${inspectRuns} run=${runId}`);
            } else if (r.blocks < 3) {
              r.blocks += 1;
              const why = !st.txt ? "no brief" : `brief cites ${st.opened} opened source(s), searches=${s.searches}`;
              log(`research-gate: blocked analysis run (${why}) ${r.blocks}/3 run=${runId}`);
              return {
                block: true,
                blockReason:
                  `Not yet: research comes before analysis (${why}). You have used your ${inspectRuns} inspection runs for the map. ` +
                  `Now research the decisions in your map: search, open primary sources, and write them into your brief (${st.path || briefDir + "/<short-topic>.md"}) with the URL of each page you opened. ` +
                  "Analysis runs open once the brief cites at least 2 opened sources. Shell commands that only read files or fetch pages are not held.",
              };
            } else {
              s.researchOpen = true;
              s.researchFirstViolated = true;
              log(`research-gate: cap reached → analysis allowed, research-first violated (opened=${st.opened} searches=${s.searches}) run=${runId}`);
            }
          }
        }

        // Answer gate (brief arms): writing the answer file requires the research to exist first.
        // Blocked calls reach the agent mid-run; finalize revises do not (OpenClaw drops them in CLI runs).
        //   1. no brief, or a brief with fewer than 2 opened sources → research first (at most 2 blocks)
        //   2. then, with claimCheck, the claim check (1 block)
        // After that the write goes through; anything still missing is logged and warned in the answer.
        if (briefFirst && s && answerFiles.length && answerFiles.some((f) => blob.includes(f)) && !s.answerGateDone) {
          const g = (s.answerGate ||= { researchBlocks: 0, claimBlocked: false });
          const { path, txt, opened, ok: researchOk } = researchState(s);
          if (!researchOk && g.researchBlocks < 2) {
            g.researchBlocks += 1;
            const why = !txt ? "no brief" : `brief has ${opened} opened source(s), searches=${s.searches}`;
            log(`answer-gate: blocked (${why}) ${g.researchBlocks}/2 on ${name} run=${runId}`);
            return {
              block: true,
              blockReason:
                (!txt
                  ? `Not yet: research comes before the answer. Write your brief to ${briefDir}/<short-topic>.md first: `
                  : `Not yet: your brief (${path}) has ${opened} source(s) you actually opened. `) +
                "map the decisions this result depends on, search for how each should be done, open at least 2 primary sources, and cite each point with the URL of the page you opened. " +
                "Check your analysis against the brief and redo anything it contradicts. Then write the answer file again.",
            };
          }
          if (claimCheck && researchOk && !g.claimBlocked) {
            g.claimBlocked = true;
            g.size0 = fileSize(path); g.searches0 = s.searches; g.path = path;
            log(`answer-gate: claim check on ${name} brief=${path} run=${runId}`);
            return { block: true, blockReason: "Not yet: check your claims before writing the answer. " + claimText(path) + " Then write the answer file again." };
          }
          s.answerGateDone = true;
          s.claimGate = g.claimBlocked ? { done: true } : s.claimGate;
          const grew = g.claimBlocked ? ` brief+${fileSize(g.path) - g.size0}B searches ${g.searches0}->${s.searches}` : "";
          log(`answer-gate: allowed after ${g.researchBlocks + (g.claimBlocked ? 1 : 0)} block(s) research=${researchOk ? "ok" : "MISSING"} opened=${opened}${grew} run=${runId}`);
          if (!researchOk) s.answerGateMissing = true;
        }

        // Environment changes need the user's approval.
        if (gateInstalls && execTools.has(name)) {
          const cmd = String(args?.command ?? args?.cmd ?? blob);
          if (INSTALL_RE.test(cmd)) {
            log(`gate: approval requested for install cmd="${cmd.replace(/\s+/g, " ").slice(0, 100)}" run=${runId}`);
            return {
              requireApproval: {
                title: "Package/environment change requested",
                description: `The agent wants to run:\n${cmd.slice(0, 500)}\n\nThis changes an environment. Approve only if you want this installed or removed.`,
                severity: "warning",
                timeoutMs: 120000,
                allowedDecisions: ["allow-once", "deny"],
              },
            };
          }
        }

        if (editTools.has(name)) {
          const target = String(args?.path ?? args?.file_path ?? args?.file ?? args?.filePath ?? "");
          // Writing outside the agent's workspace (another agent's workspace, home, config) needs approval.
          // Includes OpenClaw's skill-workshop runs: a skill written from a session changes how the
          // agent behaves later, so it goes to the user (in CLI/eval runs the approval fails closed).
          if (target && !isPathAllowed(target, ctx)) {
            log(`gate: approval requested for write outside workspace path=${target.slice(-120)} run=${runId}`);
            return {
              requireApproval: {
                title: "Write outside the agent's workspace",
                description: `The agent wants to ${name} a file outside its workspace:\n${target}\n\nApprove only if this location is intended.`,
                severity: "warning",
                timeoutMs: 120000,
                allowedDecisions: ["allow-once", "deny"],
              },
            };
          }
          // The first code edit after a PASS gets bounced once with the consequences spelled out.
          if (nudgePostPassEdits && s?.gated && s.audits > 0 && s.verdict === "PASS" && s.editSeq === s.auditedAtEdit &&
              !target.startsWith("/tmp/") && CODE_FILE_RE.test(target)) {
            s.postPassNudged ??= new Set();
            if (!s.postPassNudged.has(s.audits)) {
              s.postPassNudged.add(s.audits);
              const left = Math.max(0, maxAuditRounds - s.audits);
              log(`gate: post-PASS edit bounced once (rounds used ${s.audits}/${maxAuditRounds}) run=${runId}`);
              return {
                block: true,
                blockReason:
                  `This code already PASSED audit (round ${s.audits} of ${maxAuditRounds}). Any change now makes that audit stale: ` +
                  (left > 0
                    ? `if you make it, you must re-audit (reset + sessions_send; ${left} round(s) left) or the answer ships with a stale-audit warning. `
                    : "no audit rounds are left, so the answer would ship with a stale-audit warning. ") +
                  "If the change is not needed for correctness, deliver the audited version as it is and mention optional improvements in your answer instead. " +
                  "If it is needed, repeat this edit (it will go through) and then re-audit.",
              };
            }
          }
        }

        // Every audit round must go to a freshly reset audit session.
        if (requireFreshAudit && s?.gated && SEND_RE.test(name) && blob.toLowerCase().includes(auditorId.toLowerCase()) && !s.resetSinceAudit) {
          const key = auditKeyFor(s.sessionKey);
          log(`gate: blocked audit send without reset (${key}) run=${runId}`);
          return {
            block: true,
            blockReason:
              `Reset the audit session first so the reviewer starts fresh: use the sessions tool with action "reset" on "${key}" ` +
              "(reset only, do not delete), then send the audit again. For a re-audit, send the current code plus the list of claimed fixes, not the previous review.",
          };
        }
      } catch (err) {
        log(`gate ERROR ${String(err)}`);
      }
    });

    // ── 2. DURING: record what the agent actually did ───────────────────
    api.on("after_tool_call", (event, ctx) => {
      try {
        const s = getState(ctx, event, false);
        if (!s) return;
        const { name, args } = resolveTool(event);
        // OpenClaw wraps fetched web content in a "SECURITY NOTICE" marker; that is not a failure.
        const errText = event?.error ? String(event.error) : "";
        const failed = Boolean(errText) && !/^\s*SECURITY NOTICE/i.test(errText);
        // The wrapper and the inner tool can both report the same call; count it once.
        const callId = event?.toolCallId;
        if (callId) {
          const k = `${callId}:${name}`;
          if (s.seenCalls.has(k)) return;
          s.seenCalls.add(k);
        }
        const runId = ctx?.runId || event?.runId || "(none)";

        // Reading the research guide (any tool whose arguments name its path) is logged once per session.
        if (!failed && researchGuide && s.ledger && !s.ledger.guideRead) {
          let blob = "";
          try { blob = JSON.stringify(args ?? {}); } catch { /* ignore */ }
          if (blob.includes(researchGuide)) {
            s.ledger.guideRead = true;
            log(`tool ${name} research guide read run=${runId}`);
          }
        }
        // Any tool output can legitimately surface a link (source code, command output).
        if (!failed && !searchTools.has(name) && !fetchTools.has(name)) {
          urlsIn(event?.result).forEach((u) => s.seen.add(u));
        }

        if (SESSIONS_TOOL_RE.test(name)) {
          let blob = "";
          try { blob = JSON.stringify(args ?? {}); } catch { /* ignore */ }
          if (!failed && /reset/i.test(blob) && blob.includes(auditKeyFor(s.sessionKey))) {
            s.resetSinceAudit = true;
            log(`tool sessions reset ${auditKeyFor(s.sessionKey)} run=${runId}`);
          }
        } else if (searchTools.has(name)) {
          if (!failed) s.searches += 1;
          const urls = urlsIn(event?.result);
          urls.forEach((u) => s.seen.add(u));
          log(`tool ${name} failed=${failed} q="${String(args?.query ?? "").slice(0, 60)}" urls=${urls.length} run=${runId}` +
            (failed ? ` err="${String(event.error).slice(0, 160).replace(/\s+/g, " ")}"` : ""));
        } else if (fetchTools.has(name)) {
          const target = String(args?.url ?? "");
          const errorPage = !failed && looksLikeErrorPage(event?.result);
          const ok = !failed && !errorPage;
          if (ok) {
            s.searches += 1;
            if (DOCS_URL_RE.test(target)) s.codeChecks += 1;
            const c = canonUrl(target);
            if (c) { s.read.add(c); s.seen.add(c); }
          }
          if (!failed) urlsIn(event?.result).forEach((u) => s.seen.add(u));
          log(`tool ${name} failed=${!ok}${errorPage ? " (error page)" : ""} url=${target.slice(0, 120)} run=${runId}` +
            (failed ? ` err="${String(event.error).slice(0, 160).replace(/\s+/g, " ")}"` : ""));
        } else if (SPAWN_RE.test(name)) {
          if (String(args?.agentId ?? "").toLowerCase() === auditorId.toLowerCase()) {
            if (!failed) s.auditSpawns += 1;
            const m = textOf(event?.result).match(/\\?"runId\\?"\s*:\s*\\?"([^"\\]+)/);
            log(`tool ${name} → ${auditorId} failed=${failed} collect=${Boolean(args?.collect)} childRun=${m ? m[1] : "?"} run=${runId}` +
              (failed ? ` err="${String(event.error).slice(0, 160).replace(/\s+/g, " ")}"` : ""));
          }
        } else if (WAIT_RE.test(name)) {
          if (s.auditSpawns > 0) {
            const v = findVerdict(event?.result);
            const t = textOf(event?.result);
            const status = (t.match(/\\?"status\\?"\s*:\s*\\?"(done|failed|killed|timeout)/i) || [])[1] || "?";
            if (!failed && v) {
              s.audits += 1;
              s.verdict = v;
              s.auditedAtEdit = s.editSeq;
            }
            log(`tool ${name} ← ${auditorId} failed=${failed} status=${status} verdict=${v || "?"} audits=${s.audits} run=${runId}`);
          }
        } else if (HISTORY_RE.test(name)) {
          // The agent may poll the auditor's session for a long-running audit.
          let blob = "";
          try {
            blob = JSON.stringify(args ?? {});
          } catch { /* ignore */ }
          if (!failed && blob.toLowerCase().includes(auditorId.toLowerCase())) {
            const resText = typeof event?.result === "string" ? event.result : (() => { try { return JSON.stringify(event?.result ?? ""); } catch { return ""; } })();
            const all = [...resText.matchAll(/AUDIT:\s*(PASS|REVISE)/gi)];
            if (all.length) {
              s.verdict = all[all.length - 1][1].toUpperCase();
              s.audits = Math.max(s.audits, 1);
              s.auditedAtEdit = s.editSeq;
              s.resetSinceAudit = false;
              // Evidence for the newest auditor run in that session (recorded by audit-gate).
              const akey = auditKeyFor(s.sessionKey);
              let rec;
              for (const v of auditEvidence()?.values?.() || []) if (v.sessionKey === akey && (!rec || v.at > rec.at)) rec = v;
              if (s.verdict === "PASS") {
                s.verifiedPass = rec ? passCounts(rec) : false;
                s.lastAuditUnverified = !s.verifiedPass;
              } else {
                s.verifiedPass = false;
                s.lastAuditUnverified = false;
              }
              log(`tool ${name} ← ${auditorId} verdict=${s.verdict}${rec ? ` verifications=${rec.verifications}` : " verifications=none-found"} audits=${s.audits} run=${runId}`);
            }
          }
        } else if (SEND_RE.test(name)) {
          let blob = "";
          try {
            blob = JSON.stringify(args ?? {});
          } catch { /* ignore */ }
          if (blob.toLowerCase().includes(auditorId.toLowerCase())) {
            const t = textOf(event?.result);
            const status = (t.match(/\\?"status\\?"\s*:\s*\\?"(ok|accepted|timeout|no_reply|error|forbidden|queued)/i) || [])[1] || "?";
            const v = findVerdict(event?.result);
            const childRun = (t.match(/\\?"runId\\?"\s*:\s*\\?"([^"\\]+)/) || [])[1] || "";
            const dup = childRun && s.auditRuns.has(childRun);
            let evid = "";
            if (!failed && v && !dup) {
              s.audits += 1;
              s.verdict = v;
              s.auditedAtEdit = s.editSeq;
              s.resetSinceAudit = false;
              if (childRun) s.auditRuns.add(childRun);
              const rec = childRun ? auditEvidence()?.get?.(childRun) : undefined;
              if (v === "PASS") {
                if (rec) {
                  s.verifiedPass = passCounts(rec);
                  evid = ` verifications=${rec.verifications}${rec.premiseConflict ? " premise-flagged" : ""}`;
                } else {
                  // No record (audit-gate plugin absent or ids differ): don't deadlock; accept but log it.
                  s.verifiedPass = true;
                  evid = " verifications=unknown";
                }
                s.lastAuditUnverified = !s.verifiedPass;
              } else {
                s.verifiedPass = false;
                s.lastAuditUnverified = false;
                if (rec) evid = ` verifications=${rec.verifications}`;
              }
            }
            if (dup) return;
            log(`tool ${name} → ${auditorId} failed=${failed} status=${status} verdict=${v || "?"}${evid} audits=${s.audits} child=${childRun || "?"} run=${runId}` +
              (failed ? ` err="${String(event.error).slice(0, 160).replace(/\s+/g, " ")}"` : ""));
          }
        } else if (editTools.has(name)) {
          const target = String(args?.path ?? args?.file_path ?? args?.file ?? args?.filePath ?? "");
          const patchText = typeof args?.patch === "string" ? args.patch : typeof args?.input === "string" ? args.input : "";
          if (!failed && BRIEF_PATH_RE.test(target)) {
            s.brief = target;
            if (s.ledger) s.ledger.brief = target;
            log(`tool ${name} brief ${target.slice(-160)} run=${runId}`);
          }
          if (!failed && !target.startsWith("/tmp/") && (CODE_FILE_RE.test(target) || CODE_FILE_RE.test(patchText.slice(0, 2000)))) {
            s.editSeq += 1;
            log(`tool ${name} edit ${target.slice(-200) || "(patch)"} editSeq=${s.editSeq} run=${runId}`);
          }
        } else if (execTools.has(name)) {
          const cmd = String(args?.command ?? args?.cmd ?? JSON.stringify(args ?? {}));
          if (!failed) s.execs += 1;
          const bm = !failed && /(>|\btee\b)/.test(cmd) ? cmd.match(BRIEF_PATH_RE) : null;
          if (bm) {
            s.brief = bm[0];
            if (s.ledger) s.ledger.brief = bm[0];
            log(`tool ${name} brief ${bm[0].slice(-160)} (via shell) run=${runId}`);
          }
          // curl/wget downloads count as reading that page, unless the output is an error page
          const dl = !failed ? downloadedUrls(cmd) : [];
          if (dl.length && !looksLikeErrorPage(event?.result)) {
            dl.forEach((u) => { s.read.add(u); s.seen.add(u); });
            s.searches += 1;
            log(`tool ${name} download read=${dl.slice(0, 3).join(" ")} run=${runId}`);
          }
          if (!failed && EXEC_WRITE_RE.test(cmd) && !/>\s*\/tmp\/|tee\s+(-a\s+)?\/tmp\/|cd\s+\/tmp\b/.test(cmd)) {
            s.editSeq += 1;
            log(`tool ${name} exec-write editSeq=${s.editSeq} cmd="${cmd.replace(/\s+/g, " ").slice(0, 80)}" run=${runId}`);
          }
          if (!failed && CODE_CHECK_RE.test(cmd)) {
            s.codeChecks += 1;
            log(`tool ${name} code-check cmd="${cmd.replace(/\s+/g, " ").slice(0, 100)}" run=${runId}`);
          }
        }
      } catch (err) {
        log(`tool ERROR ${String(err)}`);
      }
    });

    // ── 3. AFTER: verify against the evidence, revise once ──────────────
    api.on("before_agent_finalize", (event, ctx) => {
      try {
        if (!isScoped(ctx) || gatesOnly) return;
        const runId = ctx?.runId || event?.runId || "(none)";
        const draft = typeof event?.lastAssistantMessage === "string" ? event.lastAssistantMessage : "";
        // OpenClaw's own background reviews (skill workshop) are not the agent's answers.
        if (String(runId).startsWith("skill-workshop")) return;
        const s = getState(ctx, event, false);
        if (!s) {
          log(`finalize: no turn state → ship run=${runId} draftLen=${draft.length}`);
          return;
        }
        s.passes += 1;
        // Claim check: what the answer's claims table says, and whether the answer-file gate fired.
        if (claimCheck) {
          let briefTxt = "";
          const bp0 = s.brief || s.ledger?.brief || "";
          if (bp0) { try { briefTxt = readFileSync(isAbsolute(bp0) ? bp0 : resolvePath(dirname(briefDir), bp0), "utf8"); } catch { /* none */ } }
          // Table rows only: a label in a table cell (| LABEL |), in the brief or the answer.
          const rows = (re) => ((briefTxt + "\n" + draft).match(re) || []).length;
          const sup = rows(/\|\s*SUPPORTED\b[^|]*\|/g), con = rows(/\|\s*CONTRADICTED\b[^|]*\|/g), nc = rows(/\|\s*NOT COVERED\b[^|]*\|/g);
          const gate = !answerFiles.length ? "n/a" : !s.answerGate ? "not-reached" : s.answerGate.claimBlocked ? "claim-checked" : s.answerGateDone ? "research-only" : "blocked";
          log(`claim-check: table supported=${sup} contradicted=${con} not-covered=${nc} ${sup + con + nc ? "" : "(no table) "}gate=${gate} run=${runId}`);
        }
        if (briefFirst) {
          const bp = s.brief || s.ledger?.brief || "";
          if (bp) {
            let txt = "";
            try { txt = readFileSync(isAbsolute(bp) ? bp : resolvePath(dirname(briefDir), bp), "utf8"); } catch { /* unreadable */ }
            const urls = new Set(txt.match(/https?:\/\/[^\s)>\]"']+/g) || []).size;
            const has = (re) => (re.test(txt) ? "yes" : "no");
            log(`brief-shape: map=${has(/^#+\s*Map\b/im)} research=${has(/^#+\s*Research\b/im)} urls=${urls} added-by-check=${has(/^#+\s*Added by claim check/im)} lines=${txt ? txt.split("\n").length : 0} run=${runId}`);
          }
        }
        log(`finalize entry run=${runId} pass=${s.passes} modes=[${[...s.modes].join(",")}] searches=${s.searches} codeChecks=${s.codeChecks} execs=${s.execs} draftLen=${draft.length}${briefFirst ? ` brief=${s.brief || s.ledger?.brief || "none"}` : ""}`);

        const chk = checkDraft(s, draft);
        if (!chk) return;
        const { problems, unbacked, links, st } = chk;
        const nCited = links.read.length + links.seenOnly.length + links.unknown.length + links.marked.length;
        if (nCited) log(`links: cited=${nCited} read=${links.read.length} seen-only=${links.seenOnly.length} unknown=${links.unknown.length} marked-unverified=${links.marked.length} run=${runId}`);
        const capWarning = st.cappedOpen ? warningText(s, st) : "";
        if (st.cappedOpen) {
          s.closed = true;
          log(`finalize: audit round cap reached (${s.audits}) without a verified PASS → warning queued run=${runId}`);
        }

        if (!problems.length) {
          const key = s.sessionKey || s.runId;
          if (key) {
            // A successful revision clears a pre-queued warning, but never the round-cap warning.
            if (capWarning) pendingWarnings.set(key, { at: Date.now(), text: capWarning });
            else pendingWarnings.delete(key);
          }
          s.closed = true;
          log(`finalize: grounded → ship run=${runId}`);
          return;
        }

        if (s.revised) {
          // Already had our one pass. Let it ship (return nothing, so we never
          // override other plugins' decisions) with a visible warning.
          queueWarning(s, problems);
          log(`finalize: STILL ungrounded after revise (${problems.join(",")}) → ship with warning run=${runId}`);
          return;
        }

        s.revised = true;
        // Pre-queue the warning in case the host never runs the revision pass.
        queueWarning(s, problems);
        const steps = [];
        if (problems.includes("math")) {
          steps.push(
            `Your arithmetic is wrong: ${(s.mathErrors || []).slice(0, 3).map((e) => `${e.expr} = ${e.actual}, not ${e.stated}`).join("; ")}. ` +
              "Compute every calculation with code (python) instead of by hand, then correct the answer and anything derived from it.",
          );
        }
        if (problems.includes("brief")) {
          steps.push(
            `You skipped the research brief. Open 2-4 primary sources on how this task should be done, write the brief to ${briefDir}/<short-topic>.md ` +
              "(approach, library functions, pitfalls, data-driven choices, a URL per point), then check your answer against it and revise.",
          );
        }
        if (problems.includes("no-answer")) {
          steps.push(
            "Your last message describes what you are about to write, but you have not answered the question yet. " +
              "Write the full answer now, from the pages you opened, ending with a 'Sources:' list.",
          );
        }
        if (problems.includes("facts")) {
          steps.push(
            "You answered without searching. Run web_search for the specific facts that may have changed, in the correct domain " +
              `(${sources}), and rewrite the answer from those results.`,
          );
        }
        if (problems.includes("code")) {
          steps.push(
            "You wrote code without checking the installed library versions or the real function signatures. " +
              "Check versions in the project's conda environment and confirm each function you used (inspect.signature or version-matched docs), then fix the code.",
          );
        }
        if (problems.includes("audit")) {
          steps.push(
            `Your code has not been independently audited (no audit verdict was received). ` +
              `Send it with sessions_send({${auditorTarget(s.sessionKey).call}, timeoutSeconds: ${auditTimeout}, message: ...}) and use the reply. ` +
              "Address every HIGH and MEDIUM finding, and report the verdict exactly as returned.",
          );
        }
        if (problems.includes("audit-open")) {
          steps.push(
            "The auditor's latest verdict is REVISE. Fix the findings, then send one re-audit (sessions_send with the same long timeout) before finalizing.",
          );
        }
        if (problems.includes("audit-unverified")) {
          steps.push(
            "The auditor's PASS had no verification evidence (it ran no independent checks). Send it back with sessions_send: " +
              "'Your PASS had no verification evidence. Independently verify <the specific claims that matter most: function existence and signatures, " +
              "numbers and formulas, what the data actually is> before passing, and list what you ran under Verification:'.",
          );
        }
        if (problems.includes("audit-stale")) {
          steps.push(
            "You changed code files after the auditor's last verdict, so the audited version is not the version you are delivering. " +
              `Re-test the changed code (in the analysis environment, on data where it could fail${retestHint}) ` +
              "and send the changes for a re-audit before finalizing.",
          );
        }
        if (problems.includes("unbacked-claim")) {
          steps.push(
            "Your answer says you checked, verified, tested, or ran something, but no checks were run in this turn. " +
              `Either actually run the checks now${checkPython ? ` with ${checkPython}` : ""} (introspection or a small test), ` +
              "or remove those claims and mark anything unverified [UNVERIFIED].",
          );
        }
        if (problems.includes("verdict")) {
          steps.push(
            `Your answer reports a PASS verdict, but the auditor's recorded verdict is ${s.verdict || "none"}. ` +
              "Report the verdict exactly as the auditor returned it.",
          );
        }
        if (problems.includes("links")) {
          steps.push(
            `These links appear in no search result or opened page and may be invented: ${unbacked.slice(0, 5).join(", ")}. ` +
              "Remove them, or find and open the real page.",
          );
        }
        if (problems.includes("links-unread")) {
          steps.push(
            `You cite pages you never opened: ${links.seenOnly.slice(0, 5).join(", ")}. A source only counts if you read it: ` +
              "open each with web_fetch and check your claim against its content, or mark that claim [UNVERIFIED].",
          );
        }
        steps.push("If your searches or fetches fail, say so plainly; do not present an answer from memory as verified. Mark anything you still cannot verify [UNVERIFIED].");
        log(`finalize: ungrounded (${problems.join(",")}) → ONE revise run=${runId}`);
        return {
          action: "revise",
          reason: "Grounding check failed: " + problems.join(", "),
          retry: {
            instruction: steps.join(" "),
            idempotencyKey: "grounded:v2",
            maxAttempts: 1,
          },
        };
      } catch (err) {
        log(`finalize ERROR ${String(err)}`);
      }
    });

    // ── 4a. Log how each run ended (diagnoses revisions that never return) ──
    api.on("agent_end", (event, ctx) => {
      try {
        if (!isScoped(ctx)) return;
        const s = getState(ctx, event, false);
        if (!s || !s.modes.size) return;
        if (s.revised && s.passes < 2) {
          log(`agent_end: REVISE NOT HONORED — run ended without a second pass run=${ctx?.runId || event?.runId || "(none)"}`);
        }
        log(
          `agent_end run=${ctx?.runId || event?.runId || "(none)"} success=${event?.success} ` +
            `durationMs=${event?.durationMs ?? "?"} passes=${s.passes} revised=${s.revised} audits=${s.audits} verdict=${s.verdict || "-"}${s.verdict === "PASS" ? (s.verifiedPass ? " (verified)" : " (UNVERIFIED)") : ""}` +
            (event?.error ? ` error="${String(event.error).slice(0, 120)}"` : ""),
        );
      } catch { /* observation only */ }
    });

    // ── 4a'. Write the warning into the SAVED answer (synchronous hook) ─────
    // message_sending only fires for channel deliveries, so CLI and session-history
    // readers never saw the warning. The saved assistant message is the record.
    api.on("before_message_write", (event, ctx) => {
      try {
        if (!isScoped(ctx) || gatesOnly) return;
        const msg = event?.message;
        if (!msg || msg.role !== "assistant") return;
        const parts = Array.isArray(msg.content) ? msg.content : null;
        if (parts && parts.some((p) => p && /tool_?(call|use)/i.test(String(p.type || "")))) return; // mid-turn tool step
        const text = typeof msg.content === "string" ? msg.content : (parts || []).filter((p) => p?.type === "text").map((p) => p.text || "").join("\n");
        // Short texts are skipped, except a short final message that is only a plan (no answer).
        if ((text.trim().length < 200 && !looksUnanswered(text)) || /^\s*(REPLY_SKIP|ANNOUNCE_SKIP)\s*$/.test(text) || text.includes("⚠️ Grounded:") || text.includes("⚠️ Audit not closed")) return;
        if (String(ctx?.runId || "").startsWith("skill-workshop")) return; // OpenClaw's own background review
        const s = getState(ctx, event, false);
        if (!s || s.echo) return;
        const chk = checkDraft(s, text);
        if (!chk) return;
        const w = fullWarning(s, chk);
        if (!w) return;
        let next;
        if (typeof msg.content === "string") next = { ...msg, content: msg.content + w };
        else {
          const idx = parts.map((p) => p?.type).lastIndexOf("text");
          next = { ...msg, content: parts.map((p, i) => (i === idx ? { ...p, text: (p.text || "") + w } : p)) };
        }
        const key = s.sessionKey || s.runId;
        if (key) pendingWarnings.delete(key); // already in the answer; don't append twice on delivery
        log(`warning written into saved answer (${[chk.st.cappedOpen ? "round-cap" : "", ...chk.problems].filter(Boolean).join(",")}) run=${ctx?.runId || s.runId || "(none)"}`);
        return { message: next };
      } catch (err) {
        log(`write-warning ERROR ${String(err)}`);
      }
    });

    // ── 4b. Best-effort visible warning on delivery ─────────────────────
    api.on("message_sending", (event, ctx) => {
      try {
        const key = ctx?.sessionKey || ctx?.runId;
        if (!key) return;
        const w = pendingWarnings.get(key);
        if (!w) return;
        if (typeof event?.content !== "string" || !event.content.trim()) return; // empty streaming chunk: wait
        pendingWarnings.delete(key);
        log(`warning appended session=${key}`);
        return { content: event.content + w.text };
      } catch { /* never block delivery */ }
    });
  },
});
