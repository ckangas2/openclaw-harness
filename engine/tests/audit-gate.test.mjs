import plugin from "./auditgate.js";
const H = {}; let ok=0,bad=0; const t=(n,c)=>{c?ok++:bad++;console.log((c?"PASS ":"FAIL ")+n)};
plugin.register({ pluginConfig: { agents: ["auditor"], logFile: "/dev/null" }, on: (n, f) => (H[n] = f) });
const inter = (sk, run, text) => ({ ctx: { agentId: "auditor", runId: run, sessionKey: sk, inputProvenance: { kind: "inter_session" } }, ev: { currentUserMessage: text } });
// real audit request in eval session → checklist
let a = inter("agent:auditor:eval-x", "a1", "Please audit this code:\n```py\nx=1\n```");
let r = H.before_prompt_build(a.ev, a.ctx);
t("audit request gets checklist", /AUDIT CHECKLIST/.test(r?.appendContext || ""));
// follow-up without audit request → REPLY_SKIP + no tools
let b = inter("agent:auditor:eval-x", "a2", "[Inter-session message] Thanks, delivered to the user.");
r = H.before_prompt_build(b.ev, b.ctx);
t("follow-up gets REPLY_SKIP + toolsAllow []", Array.isArray(r?.toolsAllow) && r.toolsAllow.length === 0 && /REPLY_SKIP/.test(r.appendContext));
t("follow-up full report not forced into sections", H.before_agent_finalize({ lastAssistantMessage: "AUDIT: REVISE\nstuff" }, b.ctx) === undefined);
// same message in the auditor's main session → untouched
let c = inter("agent:auditor:main", "a3", "hey auditor what do you think of this approach?");
t("main session untouched", H.before_prompt_build(c.ev, c.ctx) === undefined);
// direct (non inter-session) chat in audits session → untouched
t("direct chat untouched", H.before_prompt_build({ currentUserMessage: "Reply with OK" }, { agentId: "auditor", runId: "a4", sessionKey: "agent:auditor:eval-y" }) === undefined);

// Links in the auditor's report: opened → fine; only seen in a search → finding
const aud = (run) => {
  const ctx = { agentId: "auditor", runId: run, sessionKey: "agent:auditor:eval-" + run };
  H.before_prompt_build({ currentUserMessage: "Please audit this code:\n```py\nx=1\n```" }, ctx);
  const call = (toolName, params, result) => H.after_tool_call({ toolName, params, result, toolCallId: Math.random() + "" }, ctx);
  const fin = (text) => H.before_agent_finalize({ lastAssistantMessage: text }, ctx);
  return { ctx, call, fin };
};
const report = (extra, premise = "No, fixed values are fine here.") => `AUDIT: PASS\nPremise check: ${premise}\nVerification: ran sc.pp.calculate_qc_metrics on toy data.\n${extra}`;
{
  const { call, fin } = aud("l1");
  call("exec", { command: "python -c 'print(1)'" }, "1");
  call("web_search", { query: "q" }, "https://docs.example.org/api https://docs.example.org/other");
  call("web_fetch", { url: "https://docs.example.org/api" }, "API reference ".repeat(400));
  t("opened link in report → no problem", fin(report("Docs: https://docs.example.org/api")) === undefined);
}
{
  const { call, fin } = aud("l2");
  call("exec", { command: "python -c 'print(1)'" }, "1");
  call("web_search", { query: "q" }, "https://docs.example.org/other");
  t("search-only link in report → links problem", /links/.test(fin(report("See https://docs.example.org/other"))?.reason || ""));
}
{
  const { call, fin } = aud("l3");
  call("web_fetch", { url: "https://docs.example.org/gone" }, '{"status":404}');
  t("404 fetch is not a check → unverified PASS", /unverified/.test(fin(report(""))?.reason || ""));
}
// Premise check is binding
{
  const { call, fin, ctx } = aud("p1");
  call("exec", { command: "python -c 'print(1)'" }, "1");
  const r = fin(report("No findings.", "Yes — cells from the same mouse are not independent; DE should be per mouse."));
  t("premise yes + PASS + no findings → premise problem", /premise/.test(r?.reason || ""));
  t("premise conflict recorded in evidence", globalThis.__auditEvidence.get("p1")?.premiseConflict === true);
  const w = H.before_message_write({ message: { role: "assistant", content: "AUDIT: PASS\nPremise check: Yes ..." } }, ctx);
  t("premise-conflicted PASS stamped in saved reply", /premise check flagged/.test(w?.message?.content || ""));
}
{
  const { call, fin } = aud("p2");
  call("exec", { command: "python -c 'print(1)'" }, "1");
  t("premise yes + listed finding → no premise problem", !/premise/.test(fin(report("[MEDIUM] Use pseudobulk per mouse.", "**Yes** — per-mouse."))?.reason || ""));
}
{
  const { call, fin, ctx } = aud("p3");
  call("exec", { command: "python -c 'print(1)'" }, "1");
  t("premise no + PASS → no problem", fin(report("")) === undefined);
  t("consistent verified PASS not stamped", H.before_message_write({ message: { role: "assistant", content: "AUDIT: PASS" } }, ctx) === undefined);
}
console.log(`${ok} passed, ${bad} failed`); process.exit(bad?1:0);
