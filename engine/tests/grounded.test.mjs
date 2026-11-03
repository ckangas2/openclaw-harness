// grounded: saved-answer warnings, post-PASS edit bounce, write-scope gate, verdict regex,
// read-before-cite links, narrowed echo rule, premise-aware PASS. Each rule is tested both ways.
import { readFileSync, rmSync } from "node:fs";
import plugin from "./grounded.js";

const LOG = "/tmp/harness-test-grounded.log";
rmSync(LOG, { force: true });
const H = {};
plugin.register({
  pluginConfig: {
    agents: ["gp"], auditAgents: ["gp"], logFile: LOG,
    checkPython: "/env/bin/python", writeRoots: ["/home/u/ws", "/tmp"], packFiles: ["./pack.json"],
    blockedPatterns: ["scbench", "~/\\.bench-src"],
  },
  on: (n, f) => (H[n] = f),
});
globalThis.__auditEvidence = new Map([["c1", { verifications: 3 }], ["c2", { verifications: 2 }]]);
let ok = 0, bad = 0;
const t = (name, cond) => { cond ? ok++ : bad++; console.log(`${cond ? "PASS" : "FAIL"} ${name}`); };

function scenario(sk, run) {
  const ctx = { agentId: "gp", runId: run, sessionKey: sk };
  H.before_prompt_build({ currentUserMessage: "Write scanpy code to flag high-mito cells" }, ctx);
  const call = (toolName, params, result, id) => {
    const pre = H.before_tool_call({ toolName, params }, ctx);
    if (pre?.block || pre?.requireApproval) return pre;
    H.after_tool_call({ toolName, params, result, toolCallId: id || Math.random().toString() }, ctx);
    return pre;
  };
  return { ctx, call };
}
const long = "Here is the code and the audit trail.\n" + "x".repeat(300);
const write = (ctx, content) => H.before_message_write({ message: { role: "assistant", content } }, ctx);

// A. PASS then a post-PASS edit: bounced once, second attempt goes through, warning written into saved answer
{
  const { ctx, call } = scenario("agent:gp:eval-a", "rA");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("write", { path: "/home/u/ws/mito.py", content: "x" }, "ok");
  call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-a" }, "ok");
  call("sessions_send", { sessionKey: "agent:auditor:eval-a", message: "audit" }, { status: "ok", runId: "c1", reply: "AUDIT: PASS\nVerification: ..." });
  const r1 = call("edit", { path: "/home/u/ws/mito.py", edits: [] }, "ok");
  t("A post-PASS edit bounced once", r1?.block === true && /PASSED audit \(round 1 of 3\)/.test(r1.blockReason));
  const r2 = call("edit", { path: "/home/u/ws/mito.py", edits: [] }, "ok");
  t("A repeated edit goes through", !r2?.block);
  const toolStep = write(ctx, [{ type: "text", text: long }, { type: "toolCall", name: "exec" }]);
  t("A mid-turn tool step untouched", toolStep === undefined);
  const w = write(ctx, [{ type: "text", text: long }]);
  t("A stale-audit warning written into saved answer", w?.message?.content?.[0]?.text?.includes("⚠️ Grounded:") && w.message.content[0].text.includes("changed after the last audit"));
  const again = write(ctx, [{ type: "text", text: w.message.content[0].text }]);
  t("A no double warning", again === undefined);
}

// B. Clean PASS, no edits after: nothing appended; edit to /tmp after PASS not bounced
{
  const { ctx, call } = scenario("agent:gp:eval-b", "rB");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("write", { path: "/home/u/ws/b.py", content: "x" }, "ok");
  call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-b" }, "ok");
  call("sessions_send", { sessionKey: "agent:auditor:eval-b", message: "audit" }, { status: "ok", runId: "c2", reply: "AUDIT: PASS" });
  const r = call("write", { path: "/tmp/scratch.py", content: "x" }, "ok");
  t("B /tmp edit after PASS not bounced", !r?.block);
  t("B clean PASS answer untouched", write(ctx, "Final verdict: AUDIT: PASS (round 1).\n" + "y".repeat(300)) === undefined);
}

// C. Round cap with REVISE: cap warning written, and finalize does not delete it
{
  const { ctx, call } = scenario("agent:gp:eval-c", "rC");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("write", { path: "/home/u/ws/c.py", content: "x" }, "ok");
  for (const c of ["d1", "d2", "d3"]) {
    call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-c" }, "ok");
    call("sessions_send", { sessionKey: "agent:auditor:eval-c", message: "audit" }, { status: "ok", runId: c, reply: "AUDIT: REVISE" });
  }
  const w = write(ctx, long);
  t("C round-cap warning written", typeof w?.message?.content === "string" && w.message.content.includes("Audit not closed after 3 rounds"));
  const f = H.before_agent_finalize({ lastAssistantMessage: long }, ctx);
  t("C finalize ships (no revise at cap)", f === undefined);
}

// D. Verdict regex: "without a verified PASS" is not a PASS claim; "AUDIT: PASS" after REVISE is
{
  const { ctx, call } = scenario("agent:gp:eval-d", "rD");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("write", { path: "/home/u/ws/d.py", content: "x" }, "ok");
  call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-d" }, "ok");
  call("sessions_send", { sessionKey: "agent:auditor:eval-d", message: "audit" }, { status: "ok", runId: "e1", reply: "AUDIT: REVISE" });
  const f1 = H.before_agent_finalize({ lastAssistantMessage: "Round 1 verdict: `AUDIT: REVISE`. Delivered without a final verified PASS." }, ctx);
  t("D honest REVISE report → audit-open only", f1?.action === "revise" && /audit-open/.test(f1.reason) && !/verdict/.test(f1.reason));
}
{
  const { ctx, call } = scenario("agent:gp:eval-e", "rE");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("write", { path: "/home/u/ws/e.py", content: "x" }, "ok");
  call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-e" }, "ok");
  call("sessions_send", { sessionKey: "agent:auditor:eval-e", message: "audit" }, { status: "ok", runId: "e2", reply: "AUDIT: REVISE" });
  const f = H.before_agent_finalize({ lastAssistantMessage: "Verdict: AUDIT: PASS" }, ctx);
  t("D false PASS claim → verdict problem", /verdict/.test(f?.reason || ""));
}

// F. Write-scope gate
{
  const { call } = scenario("agent:gp:eval-f", "rF");
  const out = call("write", { path: "/home/u/.openclaw/workspace/qc.py", content: "x" }, "ok");
  t("F write to another workspace needs approval", Boolean(out?.requireApproval));
  t("F write inside workspace allowed", !call("write", { path: "/home/u/ws/sub/qc.py", content: "x" }, "ok")?.requireApproval);
  t("F relative path allowed", !call("write", { path: "scripts/qc.py", content: "x" }, "ok")?.requireApproval);
  t("F relative path climbing out needs approval", Boolean(call("write", { path: "../other/qc.py", content: "x" }, "ok")?.requireApproval));
  t("F sibling prefix is not inside (ws2)", Boolean(call("write", { path: "/home/u/ws2/qc.py", content: "x" }, "ok")?.requireApproval));
}

// K. Off-limits patterns (benchmark answer keys) are blocked for every tool, in every mode
{
  const { call } = scenario("agent:gp:eval-k", "rK");
  t("K fetch of the benchmark repo blocked", call("web_fetch", { url: "https://github.com/latchbio/scbench/tree/main/evals" }, "x")?.block === true);
  t("K search for it blocked", call("web_search", { query: "SCBench pseudobulk answer" }, "x")?.block === true);
  t("K read of the key folder blocked (~ expanded)", call("exec", { command: `cat ${process.env.HOME}/.bench-src/evals/qc.json` }, "x")?.block === true);
  t("K ordinary fetch allowed", !call("web_fetch", { url: "https://scanpy.readthedocs.io/en/stable/" }, "x")?.block);
}

// G. skill-workshop writes not gated; FILES nudge present
{
  const ctx = { agentId: "gp", runId: "skill-workshop-review:xyz", sessionKey: "agent:gp:eval-g" };
  H.before_prompt_build({ currentUserMessage: "review" }, { ...ctx, inputProvenance: { kind: "internal_system" } });
  const r = H.before_tool_call({ toolName: "write", params: { path: "/home/u/.openclaw/agents/gp/agent/workshop-skills/x/SKILL.md" } }, ctx);
  t("G workshop write gated", Boolean(r?.requireApproval));
  const p = H.before_prompt_build({ currentUserMessage: "Write scanpy code to normalize" }, { agentId: "gp", runId: "rG2", sessionKey: "agent:gp:eval-g2" });
  t("G FILES nudge names the workspace", /FILES: save scripts and outputs under \/home\/u\/ws/.test(p?.appendContext || ""));
}
// H. Any unmet check (not just audits) is written into the saved answer: an invented link
{
  const { ctx, call } = scenario("agent:gp:eval-h", "rH");
  call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
  call("sessions", { action: "reset", sessionKey: "agent:auditor:eval-h" }, "ok");
  call("sessions_send", { sessionKey: "agent:auditor:eval-h", message: "audit" }, { status: "ok", runId: "c1", reply: "AUDIT: PASS" });
  const ans = "sc.pp.scrublet wraps the [scrublet package](https://github.com/fangsong/scrublet). " + "z".repeat(300);
  const w = write(ctx, [{ type: "text", text: ans }]);
  const txt = w?.message?.content?.[0]?.text || "";
  t("H invented link flagged in saved answer", /Unverified links \(not found in any search or opened page\): https:\/\/github\.com\/fangsong\/scrublet\./.test(txt));
  t("H clean answer with no links untouched", write(ctx, [{ type: "text", text: "All checked. " + "y".repeat(300) }]) === undefined);
}

// L. Links: a cited page counts only if it was opened this session
const factsRun = (sk, run) => {
  const ctx = { agentId: "gp", runId: run, sessionKey: sk };
  H.before_prompt_build({ currentUserMessage: "What are the latest best practices for doublet detection?" }, ctx);
  const call = (toolName, params, result, error) => H.after_tool_call({ toolName, params, result, error, toolCallId: Math.random().toString() }, ctx);
  const ans = (body) => write(ctx, [{ type: "text", text: body + "\n" + "w".repeat(300) }])?.message?.content?.[0]?.text || "";
  return { ctx, call, ans };
};
{
  const { call, ans } = factsRun("agent:gp:eval-l1", "rL1");
  call("web_search", { query: "doublet detection" }, "Results: https://example.org/paper-a https://github.com/owner/tool");
  call("web_fetch", { url: "https://example.org/paper-a" }, "Long article ".repeat(500));
  t("L opened page cited → clean", ans("Per https://example.org/paper-a, use two methods.") === "");
  t("L search-only page cited → flagged as never opened", /Cited but never opened: https:\/\/github\.com\/owner\/tool/.test(ans("See https://github.com/owner/tool.")));
  t("L search-only page marked [UNVERIFIED] → accepted", ans("Maybe https://github.com/owner/tool [UNVERIFIED].") === "");
  t("L marker with trailing note accepted ([UNVERIFIED — snippet only])", ans("See https://github.com/owner/tool [UNVERIFIED — search snippet only, didn't open]") === "");
  t("L 'didn't open' on the line accepted", ans("Directory: https://github.com/owner/tool (listed, didn't open)") === "");
  t("L subpage of an opened page is not the opened page", /Unverified links/.test(ans("See https://example.org/paper-a/supplement/table3.")));
  t("L parent of an opened page is not the opened page", /Unverified links/.test(ans("See https://example.org/.")));
}
{
  const { call, ans } = factsRun("agent:gp:eval-l2", "rL2");
  call("web_search", { query: "x" }, "https://marmelab.com/blog/missing-post.html");
  call("web_fetch", { url: "https://marmelab.com/blog/missing-post.html" }, '{"status":404,"text":"Page not found"}');
  t("L 404 fetch does not count as opened", /Cited but never opened/.test(ans("Source: https://marmelab.com/blog/missing-post.html")));
  call("web_fetch", { url: "https://arxiv.org/pdf/2406.10279v2" }, "Abstract ".repeat(800));
  t("L arXiv pdf opened, abs cited → clean", ans("Paper: https://arxiv.org/abs/2406.10279") === "");
  t("L bare-domain link never seen → flagged", /Unverified links.*openai\.com\/index\/harness-engineering/.test(ans("OpenAI: openai.com/index/harness-engineering/")));
  t("L bare names and emails are not links", ans("Use scanpy.io or write to a@b.com.") === "");
  call("exec", { command: "curl -sL https://raw.githubusercontent.com/owner/repo/main/README.md" }, "# Repo\n" + "text ".repeat(900));
  t("L curl download of README counts as opening the repo", ans("Repo: https://github.com/owner/repo") === "");
  call("web_fetch", { url: "https://escholarship.org/content/qt6gr648np/qt6gr648np_noSplash_32134c.pdf" }, "Benchmark text ".repeat(500));
  t("L abbreviated link to an opened page accepted (host/path/...pdf)", ans("Accepted manuscript: escholarship.org/content/qt6gr648np/...pdf") === "");
  t("L abbreviated domain-only link not accepted (host/...)", /Unverified links/.test(ans("See escholarship.org/... for more.")));
}
{
  // links are checked even when the question triggered no grounding mode
  const ctx = { agentId: "gp", runId: "rL3", sessionKey: "agent:gp:eval-l3" };
  H.before_prompt_build({ currentUserMessage: "tell me about harness engineering" }, ctx);
  const w = write(ctx, [{ type: "text", text: "Read https://invented.example/post " + "v".repeat(300) }]);
  t("L no-mode answer with invented link → flagged", /Unverified links/.test(w?.message?.content?.[0]?.text || ""));
  t("L no-mode answer without links → untouched", write(ctx, [{ type: "text", text: "Plain answer " + "v".repeat(300) }]) === undefined);
}

// E. Echo rule applies only to messages from this agent's own auditor
{
  const fromAud = { agentId: "gp", runId: "rE1", sessionKey: "agent:gp:main", inputProvenance: { kind: "inter_session", sourceSessionKey: "agent:auditor:audits" } };
  const r1 = H.before_prompt_build({ currentUserMessage: "[Inter-session message] AUDIT: PASS ..." }, fromAud);
  t("E message from own auditor → REPLY_SKIP, no tools", Array.isArray(r1?.toolsAllow) && r1.toolsAllow.length === 0);
  t("E own-auditor echo claimed without a model call", H.before_agent_reply({ prompt: "x" }, fromAud)?.reply?.text === "REPLY_SKIP");
  const fromOther = { agentId: "gp", runId: "rE2", sessionKey: "agent:gp:main", inputProvenance: { kind: "inter_session", sourceSessionKey: "agent:planner:main" } };
  const r2 = H.before_prompt_build({ currentUserMessage: "Write scanpy code to flag high-mito cells" }, fromOther);
  t("E message from another agent keeps its tools", r2?.toolsAllow === undefined && /GROUNDING REQUIRED/.test(r2?.appendContext || ""));
  t("E other-agent message not claimed", H.before_agent_reply({ prompt: "x" }, fromOther) === undefined);
  const r3 = H.before_prompt_build({ currentUserMessage: "[Inter-session message] sourceSession=agent:auditor:eval-z ... AUDIT: REVISE" }, { agentId: "gp", runId: "rE3", sessionKey: "agent:gp:x" });
  t("E text-only provenance naming the auditor → echo", Array.isArray(r3?.toolsAllow));
}

// P. A PASS whose premise check flagged a problem it then ignored does not count
{
  globalThis.__auditEvidence.set("p1", { verifications: 2, premiseConflict: true, at: Date.now() });
  globalThis.__auditEvidence.set("p2", { verifications: 2, premiseConflict: false, at: Date.now() });
  for (const [id, expectWarn] of [["p1", true], ["p2", false]]) {
    const { ctx, call } = scenario(`agent:gp:eval-${id}`, `r${id}`);
    call("exec", { command: "/env/bin/python -c 'import scanpy; print(scanpy.__version__)'" }, "1.11.5");
    call("write", { path: `/home/u/ws/${id}.py`, content: "x" }, "ok");
    call("sessions", { action: "reset", sessionKey: `agent:auditor:eval-${id}` }, "ok");
    call("sessions_send", { sessionKey: `agent:auditor:eval-${id}`, message: "audit" }, { status: "ok", runId: id, reply: "AUDIT: PASS" });
    const w = write(ctx, "Verdict: AUDIT: PASS (round 1). " + "y".repeat(300));
    const warned = /PASS had no verification evidence/.test(w?.message?.content || "");
    t(`P ${expectWarn ? "premise-conflicted PASS → warning" : "consistent PASS → no warning"}`, warned === expectWarn);
  }
}

// N. The turn must end with an answer, not a plan to write one (live chat ddf712b8, 8 Oct)
{
  const plan = "I've verified the four main sources—Martin Fowler's guide, Addy Osmani's post, LangChain's architecture breakdown, and OpenAI's recent engineering article—so I have enough grounding to answer confidently.\n\nNow I'm connecting these findings to your own harness setup: the grounded plugin, audit gate, and evaluation framework. The key patterns that map directly are the feedback-forward loop, ratcheting improvements with each failure, and using hooks as the enforcement mechanism. I'll keep this concise and tie it back to what you're already doing.";
  const { ctx, call } = factsRun("agent:gp:eval-n1", "rN1");
  call("web_search", { query: "harness engineering" }, "https://martinfowler.com/articles/harness-engineering.html");
  call("web_fetch", { url: "https://martinfowler.com/articles/harness-engineering.html" }, "Article ".repeat(600));
  const f = H.before_agent_finalize({ lastAssistantMessage: plan }, ctx);
  t("N plan-only final message → sent back (no-answer)", f?.action === "revise" && /no-answer/.test(f.reason));
  const w = write(ctx, [{ type: "text", text: plan }]);
  t("N plan-only saved message gets a no-answer warning", /no answer was delivered/.test(w?.message?.content?.[0]?.text || ""));
  const w2 = write(ctx, [{ type: "text", text: "I'll write the answer now." }]);
  t("N short plan-only message also warned", /no answer was delivered/.test(w2?.message?.content?.[0]?.text || ""));
  const real = "1. Keep AGENTS.md short.\n2. Enforce rules with hooks.\n\nSources: martinfowler.com/articles/harness-engineering.html\n\nLet me know if you want more. " + "a".repeat(200);
  t("N real answer ending 'Let me know' → untouched", write(ctx, [{ type: "text", text: real }]) === undefined);
  const real2 = "Use pseudobulk per mouse; cell-level tests inflate significance because cells are not independent. " + "b".repeat(300) + " I'll keep digging if useful.";
  t("N substantive answer is not judged by tone alone (has sources) → no no-answer", !/no answer was delivered/.test(write(ctx, [{ type: "text", text: real2 + "\nSources: martinfowler.com/articles/harness-engineering.html" }])?.message?.content?.[0]?.text || ""));
}
{
  const ctx = { agentId: "gp", runId: "rN2", sessionKey: "agent:gp:eval-n2" };
  H.before_prompt_build({ currentUserMessage: "okay sounds good" }, ctx);
  t("N no-mode chat turn that ends with a plan → untouched", write(ctx, [{ type: "text", text: "Sounds good. I'll wait for your next question." }]) === undefined);
}

// M. Arithmetic written out in an answer is recomputed (live run 9 Oct: "(5 × 25) / 10 = 1.25 mL stock")
{
  const ctx = { agentId: "gp", runId: "rM1", sessionKey: "agent:gp:eval-m1" };
  H.before_prompt_build({ currentUserMessage: "I have a 10 mg/mL BSA stock. I need 25 mL of 0.5% BSA." }, ctx);
  const wrong = "- V₁ = (C₂ × V₂) / C₁ = (5 × 25) / 10 = **1.25 mL stock**\n- PBS = V₂ − V₁ = 25 − 1.25 = **23.75 mL**\n" + "x".repeat(200);
  const w = write(ctx, [{ type: "text", text: wrong }]);
  t("M wrong arithmetic flagged, even with no grounding mode", /Arithmetic check: \(5 × 25\) \/ 10 = 12\.5, not 1\.25/.test(w?.message?.content?.[0]?.text || ""));
  const right = "- V₁ = (5 × 25) / 10 = **12.5 mL** stock\n- PBS = 25 − 12.5 = 12.5 mL\n" + "x".repeat(200);
  t("M correct arithmetic untouched", write(ctx, [{ type: "text", text: right }]) === undefined);
  t("M rounding within 2% accepted", write(ctx, [{ type: "text", text: "Rate = 100 / 3 = 33.3%; share = 1/6 = 16.67%. " + "x".repeat(200) }]) === undefined);
  t("M year ranges and dates ignored", write(ctx, [{ type: "text", text: "Published 2021-2024; see 2024-10-09. Total = 3 + 4 = 7. " + "x".repeat(200) }]) === undefined);
}
console.log(`\n${ok} passed, ${bad} failed`);
console.log(readFileSync(LOG, "utf8").split("\n").filter((l) => /gate|warning/.test(l)).map((l) => l.slice(27, 170)).join("\n"));
process.exit(bad ? 1 : 0);
