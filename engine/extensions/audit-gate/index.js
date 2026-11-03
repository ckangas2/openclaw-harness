import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { canonUrl, urlsIn, looksLikeErrorPage, downloadedUrls, classifyLinks } from "./links.js";

// ───────────────────────────────────────────────────────────────────────────
// "audit-gate" — keeps the auditor honest about evidence.
//
//   Judgment (how to review) lives in the auditor's AGENTS.md. This plugin only
//   checks things that leave evidence:
//     1. No PASS without independent verification in THIS run
//        (a successful exec, web_search or web_fetch).
//     2. The reply starts with the verdict line ("AUDIT: PASS" / "AUDIT: REVISE").
//     3. Every cited link was opened (read) in this run, not just seen in a search.
//     4. A PASS does not contradict the report's own premise check.
//   One bounded revise if any check fails.
//
//   It also records each audit run's evidence in a shared in-process map
//   (globalThis.__auditEvidence: runId -> record) so the grounded plugin on the
//   requesting agent can tell a verified PASS from a rubber stamp, even when the
//   host does not honor the revise request.
// ───────────────────────────────────────────────────────────────────────────

const TTL_MS = 6 * 60 * 60 * 1000;
const VERDICT_LINE_RE = /^\s*(?:\*\*)?AUDIT:\s*(PASS|REVISE)\b/i;
// A finding line in the report, e.g. "[HIGH] ...", "**MEDIUM**", "Severity: LOW".
const FINDING_RE = /\[(HIGH|MEDIUM|LOW)\]|\*\*(HIGH|MEDIUM|LOW)\*\*|severity\s*[:=]\s*(HIGH|MEDIUM|LOW)|^\s*[-*\d.]*\s*(HIGH|MEDIUM)\s*[:\-—]/im;

function textOf(v) {
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v ?? "");
  } catch {
    return "";
  }
}

// What the report's "Premise check:" section answers: "yes" (a premise problem), "no", or "".
export function premiseAnswer(text) {
  const m = String(text || "").match(/premise\s+check\s*:?\s*(?:\*\*|__)?\s*:?\s*([\s\S]{0,160})/i);
  if (!m) return "";
  const head = m[1].replace(/^[\s*_>#:\-–—]+/, "");
  if (/^(yes|y\b|flagged|partly|partially)/i.test(head)) return "yes";
  if (/^(no|n\b|none)/i.test(head)) return "no";
  return "";
}

export default definePluginEntry({
  id: "audit-gate",
  name: "Audit Gate",
  description: "No PASS without verification evidence; verdict-first replies; only links it opened; PASS must agree with its premise check. Shares evidence with the grounded plugin.",
  register(api) {
    const cfg = api.pluginConfig || {};
    const agents = Array.isArray(cfg.agents) ? cfg.agents : ["auditor"];
    const searchTools = new Set(cfg.searchTools || ["web_search"]);
    const fetchTools = new Set(cfg.fetchTools || ["web_fetch"]);
    const execTools = new Set(cfg.execTools || ["exec", "bash"]);
    const wrapperTools = new Set(cfg.wrapperTools || ["tool_call"]);
    const checkPython = typeof cfg.checkPython === "string" ? cfg.checkPython : "";
    const AUDIT_REQ_RE = /\baudit\b|```/i;
    // Sessions that exist only to receive audit requests. In these, an inter-session
    // message that is not an audit request is an agent-to-agent follow-up step.
    const AUDIT_SESSION_RE = new RegExp(typeof cfg.auditSessionPattern === "string" && cfg.auditSessionPattern ? cfg.auditSessionPattern : "^agent:[^:]+:(audits|eval-)");
    const INTER_RE = /^\s*\[Inter-session message\]/;
    const PREMISE_RE = /premise\s+check\s*:/i;
    const VERIF_RE = /verification\s*:/i;

    // Packs add domain checklist items and the unit that data-driven values are derived per.
    const packNames = [];
    const packItems = [];
    let premiseHint = "";
    for (const f of Array.isArray(cfg.packFiles) ? cfg.packFiles : []) {
      try {
        const p = JSON.parse(readFileSync(String(f), "utf8"));
        packNames.push(`${p.name || f}@${p.version || "?"}`);
        if (Array.isArray(p.auditChecklist)) packItems.push(...p.auditChecklist.filter((x) => typeof x === "string" && x));
        if (!premiseHint && typeof p.premiseHint === "string") premiseHint = p.premiseHint;
      } catch (err) {
        log(`pack load ERROR ${f}: ${String(err)}`);
      }
    }
    log(`packs loaded: ${packNames.join(", ") || "(none)"}`);
    const userName = typeof cfg.userName === "string" && cfg.userName ? cfg.userName : "the user";
    const userPoss = userName === "the user" ? "the user's" : `${userName}'s`;
    const premiseUnit = premiseHint ? ` (${premiseHint})` : "";

    // Just-in-time checklist, appended to each new audit request.
    const items = [
      `Premise: should any value or choice in this request be derived from the data${premiseUnit} instead of fixed? Is the request asking the right question at all?`,
      "Falsification: what result would prove this code wrong? Was that actually tested, on data where it could fail?",
      "The whole answer: if a draft answer is included, review its prose and code comments too. Every function, parameter, number, and data-state claim is a claim to check, and open every link it cites (a link you cannot retrieve is a finding).",
      "Verify yourself: run at least one check" +
        (checkPython ? ` with ${checkPython} (${userPoss} analysis environment)` : "") +
        " — introspection, or the key function on small toy data with a known answer. Search snippets, failed fetches, and the author's description are not checks.",
      ...packItems,
    ];
    const CHECKLIST = [
      "[AUDIT CHECKLIST — work through these BEFORE answering the author's own questions]",
      ...items.map((x, i) => `${i + 1}. ${x}`),
      "Your report must include a 'Premise check:' section (yes/no, and why) and a 'Verification:' section listing what you ran.",
    ].join("\n");

    globalThis.__auditEvidence ??= new Map();
    const evidence = globalThis.__auditEvidence;
    const runs = new Map(); // runId -> working state
    const lastRunBySession = new Map(); // sessionKey -> runId
    const stampUnverified = cfg.stampUnverified !== false;

    function log(msg) {
      try {
        const file = cfg.logFile || `${process.env.OPENCLAW_STATE_DIR || "/tmp"}/logs/audit-gate.log`;
        mkdirSync(dirname(file), { recursive: true });
        appendFileSync(file, `[${new Date().toISOString()}] ${msg}\n`);
      } catch { /* never break a turn over logging */ }
    }

    function prune() {
      const now = Date.now();
      for (const [k, r] of runs) if (now - r.at > TTL_MS) runs.delete(k);
      for (const [k, r] of evidence) if (now - r.at > TTL_MS) evidence.delete(k);
    }

    function stateFor(ctx, event, create) {
      prune();
      const runId = ctx?.runId || event?.runId;
      if (!runId) return undefined;
      let r = runs.get(runId);
      if (!r && create) {
        r = { runId, verifications: 0, strong: 0, checks: [], read: new Set(), seenUrls: new Set(), passes: 0, revised: false, seen: new Set(), at: Date.now() };
        runs.set(runId, r);
      }
      if (r) {
        r.at = Date.now();
        if (ctx?.sessionKey) {
          lastRunBySession.set(ctx.sessionKey, runId);
          r.sessionKey = ctx.sessionKey;
        }
      }
      return r;
    }

    function resolveTool(event) {
      const name = String(event?.toolName || "");
      const params = event?.params ?? {};
      if (!wrapperTools.has(name)) return { name, args: params };
      const id = params.id ?? params.name ?? params.tool ?? params.toolId ?? "";
      const args = params.args ?? params.arguments ?? params.input ?? params.params ?? {};
      return { name: String(id).split(":").pop(), args: typeof args === "object" && args ? args : {} };
    }

    function record(r, verdict, premise, premiseConflict) {
      evidence.set(r.runId, {
        runId: r.runId,
        sessionKey: r.sessionKey,
        verdict,
        verifications: r.strong,
        premise: Boolean(premise),
        premiseConflict: Boolean(premiseConflict),
        searches: r.verifications - r.strong,
        checks: r.checks.slice(0, 20),
        at: Date.now(),
      });
    }

    // Start a run's state (a revision pass reuses the existing one).
    api.on("before_prompt_build", (event, ctx) => {
      try {
        if (!agents.includes(ctx?.agentId)) return;
        const existed = Boolean(ctx?.runId && runs.has(ctx.runId));
        stateFor(ctx, event, true);
        const q =
          typeof event?.currentUserMessage === "string"
            ? event.currentUserMessage
            : typeof event?.prompt === "string"
              ? event.prompt
              : "";
        // OpenClaw's agent-to-agent reply-back / announce steps are not new audits.
        const isA2AControl = /REPLY_SKIP|ANNOUNCE_SKIP|\bannounce\b/i.test(q);
        const isAudit = !existed && !isA2AControl && AUDIT_REQ_RE.test(q);
        const inter = ctx?.inputProvenance?.kind === "inter_session" || INTER_RE.test(q);
        if (!existed && !isAudit && inter && AUDIT_SESSION_RE.test(String(ctx?.sessionKey || ""))) {
          const r = stateFor(ctx, event, false);
          if (r) r.control = true;
          log(`prompt start (A2A follow-up → asked to REPLY_SKIP) run=${ctx?.runId || "?"}`);
          return {
            toolsAllow: [],
            appendContext:
              "[AGENT-TO-AGENT] This is a follow-up step from the agent you audited, not a new audit request. " +
              "Your audit is already delivered. Do not review anything again and do not call tools. Reply with exactly REPLY_SKIP.",
          };
        }
        log(`prompt ${existed ? "(revision pass)" : "start"}${isAudit ? " + checklist" : ""} run=${ctx?.runId || "?"}`);
        if (isAudit) return { appendContext: CHECKLIST };
      } catch (err) {
        log(`prompt ERROR ${String(err)}`);
      }
    });

    // Count independent verification actually performed.
    api.on("after_tool_call", (event, ctx) => {
      try {
        if (ctx?.agentId && !agents.includes(ctx.agentId)) return;
        const r = stateFor(ctx, event, false);
        if (!r) return;
        const { name, args } = resolveTool(event);
        // The deferred-tool wrapper and the inner tool both report the same call,
        // with different call ids. Treat identical tool+arguments within a few
        // seconds as one call.
        let argKey = "";
        try {
          argKey = JSON.stringify(args ?? {});
        } catch { /* ignore */ }
        const sig = `${name}:${argKey}`;
        const now = Date.now();
        r.recent ??= new Map();
        for (const [k, t] of r.recent) if (now - t > 10000) r.recent.delete(k);
        if (r.recent.has(sig)) return;
        r.recent.set(sig, now);
        // OpenClaw wraps fetched web content in a "SECURITY NOTICE" marker; that is not a failure.
        const errText = event?.error ? String(event.error) : "";
        const failed = Boolean(errText) && !/^\s*SECURITY NOTICE/i.test(errText);
        let what = "";
        let ok = !failed;
        if (!failed) urlsIn(event?.result).forEach((u) => r.seenUrls.add(u));
        if (searchTools.has(name)) {
          what = `search "${String(args?.query ?? "").slice(0, 60)}"`;
        } else if (fetchTools.has(name)) {
          what = `fetch ${String(args?.url ?? "").slice(0, 80)}`;
          // An error or "not found" page is not a source and not a check.
          if (ok && looksLikeErrorPage(event?.result)) { ok = false; what += " (error page)"; }
          const c = canonUrl(args?.url);
          if (ok && c) { r.read.add(c); r.seenUrls.add(c); }
        } else if (execTools.has(name)) {
          const cmd = String(args?.command ?? args?.cmd ?? "");
          what = `exec ${cmd.replace(/\s+/g, " ").slice(0, 80)}`;
          if (ok) {
            const dl = downloadedUrls(cmd);
            if (dl.length && !looksLikeErrorPage(event?.result)) dl.forEach((u) => { r.read.add(u); r.seenUrls.add(u); });
          }
        } else {
          return;
        }
        if (ok) {
          r.verifications += 1;
          // Searches only return snippets (leads). Running something or reading a fetched
          // primary source is what counts as checking a claim.
          if (!searchTools.has(name)) r.strong += 1;
          r.checks.push(what);
        }
        log(`tool ${what} failed=${!ok} verifications=${r.verifications} run=${r.runId}`);
      } catch (err) {
        log(`tool ERROR ${String(err)}`);
      }
    });

    // Check the reply against the evidence.
    api.on("before_agent_finalize", (event, ctx) => {
      try {
        if (!agents.includes(ctx?.agentId)) return;
        const r = stateFor(ctx, event, true);
        const draft = typeof event?.lastAssistantMessage === "string" ? event.lastAssistantMessage : "";
        r.passes += 1;

        const m = draft.match(VERDICT_LINE_RE);
        const anywhere = draft.match(/AUDIT:\s*(PASS|REVISE)/i);
        const verdict = (m || anywhere || [])[1]?.toUpperCase() || "";

        // Agent-to-agent control replies (stop ping-pong / stay silent): leave alone.
        if (/^\s*(REPLY_SKIP|ANNOUNCE_SKIP|NO_REPLY)\s*$/.test(draft)) {
          log(`finalize: control reply ${draft.trim()} → ship run=${r.runId}`);
          return;
        }
        // A2A follow-up step: never push it into a full report, whatever it says.
        if (r.control) {
          log(`finalize: A2A follow-up (not an audit) → ship run=${r.runId}`);
          return;
        }
        // Not an audit reply at all (e.g. the user chatting with the auditor): leave it alone.
        if (!verdict && !/\bAUDIT\b/i.test(draft)) {
          log(`finalize: not an audit reply → ship run=${r.runId}`);
          return;
        }

        const problems = [];
        if (!m) problems.push("format");
        const missing = [];
        if (!PREMISE_RE.test(draft)) missing.push("Premise check");
        if (!VERIF_RE.test(draft)) missing.push("Verification");
        if (missing.length) problems.push("sections");
        if (verdict === "PASS" && r.strong === 0) problems.push("unverified");
        const links = classifyLinks(draft, r.read, r.seenUrls);
        const unbacked = [...links.unknown, ...links.seenOnly];
        if (unbacked.length) problems.push("links");
        // The premise check is binding: "yes" is a finding, so a PASS with no findings contradicts it.
        const premise = premiseAnswer(draft);
        const premiseConflict = verdict === "PASS" && premise === "yes" && !FINDING_RE.test(draft);
        if (premiseConflict) problems.push("premise");
        r.premiseConflict = premiseConflict;

        record(r, verdict, PREMISE_RE.test(draft), premiseConflict);
        log(`finalize pass=${r.passes} verdict=${verdict || "?"} checks=${r.strong} searches=${r.verifications - r.strong} premise=${premise || "?"} links=${links.read.length}/${links.read.length + unbacked.length + links.marked.length} problems=[${problems.join(",")}] run=${r.runId}`);
        if (!problems.length || r.revised) return;

        r.revised = true;
        const steps = [];
        if (problems.includes("unverified")) {
          steps.push(
            "You returned PASS without running any independent check in this audit (search snippets and failed fetches do not count). " +
              "Before passing, verify the claims that matter most yourself: " +
              "introspect the functions used in the installed version (existence, signatures, defaults), run a small snippet, " +
              "and fetch the primary source behind any number or formula. List what you ran under 'Verification:'. " +
              "If you cannot verify something important, return REVISE and say what must be checked.",
          );
        }
        if (problems.includes("sections")) {
          if (missing.includes("Premise check")) {
            steps.push(
              `Add a 'Premise check:' section: should any value or choice in this request be derived from the data${premiseUnit} instead of fixed, ` +
                "or is the request asking the wrong question? Answer yes or no, and why. If yes, that is a finding.",
            );
          }
          if (missing.includes("Verification")) {
            steps.push("Add a 'Verification:' section listing each check you actually ran in this audit and what it showed.");
          }
        }
        if (problems.includes("premise")) {
          steps.push(
            "Your premise check answers yes, but your verdict is PASS with no findings. A premise problem is a finding: " +
              "either list it with a severity and return REVISE, or explain why it does not apply and answer the premise check 'No'.",
          );
        }
        if (problems.includes("format")) {
          steps.push('Begin your reply with the verdict line: "AUDIT: PASS" or "AUDIT: REVISE".');
        }
        if (problems.includes("links")) {
          steps.push(
            `You cite pages you did not open in this audit: ${unbacked.slice(0, 5).join(", ")}. ` +
              "A link counts only if you fetched it and took the point from its content. Open them, or remove them, or mark them [UNVERIFIED].",
          );
        }
        return {
          action: "revise",
          reason: "Audit evidence check failed: " + problems.join(", "),
          retry: { instruction: steps.join(" "), idempotencyKey: "audit-gate:v1", maxAttempts: 1 },
        };
      } catch (err) {
        log(`finalize ERROR ${String(err)}`);
      }
    });

    // Stamp an unverified PASS into the auditor's saved reply, so whoever reads
    // the reply back (sessions_send) sees that it does not count. Synchronous hook.
    api.on("before_message_write", (event, ctx) => {
      try {
        if (!stampUnverified || !agents.includes(ctx?.agentId)) return;
        const msg = event?.message;
        if (!msg || msg.role !== "assistant") return;
        const runId = ctx?.runId || event?.runId || lastRunBySession.get(ctx?.sessionKey);
        const r = runId ? runs.get(runId) : undefined;
        if (!r || (r.strong > 0 && !r.premiseConflict)) return;
        const STAMP = r.strong > 0
          ? "AUDIT: PASS (UNVERIFIED: the premise check flagged a problem that this PASS ignores, so it does not count; re-audit)"
          : "AUDIT: PASS (UNVERIFIED: this audit ran no checks, so this PASS does not count; re-audit with real checks)";
        const fix = (t) => (typeof t === "string" && /AUDIT:\s*PASS\b(?!\s*\(UNVERIFIED)/i.test(t) ? t.replace(/AUDIT:\s*PASS\b/i, STAMP) : t);
        let changed = false;
        let next;
        if (typeof msg.content === "string") {
          const t = fix(msg.content);
          changed = t !== msg.content;
          next = { ...msg, content: t };
        } else if (Array.isArray(msg.content)) {
          const parts = msg.content.map((p) => {
            if (p && p.type === "text") {
              const t = fix(p.text);
              if (t !== p.text) changed = true;
              return { ...p, text: t };
            }
            return p;
          });
          next = { ...msg, content: parts };
        }
        if (!changed) return;
        log(`stamped unverified PASS into saved reply (${r.strong > 0 ? "premise conflict" : "no checks"}) run=${r.runId}`);
        return { message: next };
      } catch (err) {
        log(`stamp ERROR ${String(err)}`);
      }
    });

    api.on("agent_end", (event, ctx) => {
      try {
        if (!agents.includes(ctx?.agentId)) return;
        const r = stateFor(ctx, event, false);
        if (!r) return;
        const rec = evidence.get(r.runId);
        if (r.revised && r.passes < 2) log(`agent_end: REVISE NOT HONORED run=${r.runId}`);
        log(
          `agent_end run=${r.runId} success=${event?.success} passes=${r.passes} verdict=${rec?.verdict || "-"} ` +
            `checks=${r.strong} searches=${r.verifications - r.strong} list=[${r.checks.slice(0, 5).join(" | ")}]`,
        );
      } catch { /* observation only */ }
    });
  },
});
