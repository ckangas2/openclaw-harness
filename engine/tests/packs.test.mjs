// The engine knows no domain on its own; a pack supplies it.
import g from "./grounded.js";
import a from "./auditgate.js";
let ok = 0, bad = 0;
const t = (name, cond) => { cond ? ok++ : bad++; console.log(`${cond ? "PASS" : "FAIL"} ${name}`); };
const mk = (plugin, cfg) => { const H = {}; plugin.register({ pluginConfig: { logFile: "/dev/null", ...cfg }, on: (n, f) => (H[n] = f) }); return H; };
const ask = (H, q, k) => H.before_prompt_build({ currentUserMessage: q }, { agentId: "gp", runId: k, sessionKey: "agent:gp:" + k })?.appendContext || "";

const bare = mk(g, { agents: ["gp"] });
const scrna = mk(g, { agents: ["gp"], packFiles: ["./pack.json"], userName: "Alex" });
t("no pack: scanpy request is not code mode", !/CODE: run every/.test(ask(bare, "Write scanpy code to filter cells", "b1")));
t("no pack: numpy request is code mode", /CODE: run every/.test(ask(bare, "Write numpy code to normalize rows", "b2")));
t("no pack: generic examples, generic user", /<library>/.test(ask(bare, "Write numpy code to normalize rows", "b3")));
const s = ask(scrna, "Write scanpy code to filter cells", "s1");
t("scrna pack: scanpy request is code mode", /CODE: run every/.test(s));
t("scrna pack: scanpy examples", /sc\.pp\.filter_cells/.test(s));
t("scrna pack: preferred sources", /PubMed, bioRxiv/.test(ask(scrna, "What's the latest scanpy release?", "s2")));
t("missing pack file does not crash", typeof mk(g, { agents: ["gp"], packFiles: ["/nope.json"] }).before_prompt_build === "function");

const chk = (H) => H.before_prompt_build({ currentUserMessage: "please audit ```x```" }, { agentId: "auditor", runId: Math.random() + "", sessionKey: "agent:auditor:audits" })?.appendContext || "";
const c0 = chk(mk(a, { agents: ["auditor"] })), c1 = chk(mk(a, { agents: ["auditor"], packFiles: ["./pack.json"] }));
t("no pack: 4 engine checklist items, no 'per sample'", /\n3\. The whole answer/.test(c0) && /\n4\. Verify yourself/.test(c0) && !/\n5\. /.test(c0) && !/per sample/.test(c0));
t("scrna pack: 5 items incl. units, 'per sample'", /\n5\. Form and units/.test(c1) && /per sample/.test(c1));
t("link rule in a code-only prompt", /LINKS: a source counts only if you opened its page/.test(ask(scrna, "Write scanpy code to filter cells", "s9")));
t("audit request asks for the full draft", /FULL DRAFT ANSWER/.test(ask(mk(g, { agents: ["gp"], auditAgents: ["gp"], packFiles: ["./pack.json"] }), "Write scanpy code to filter cells", "s10")));
const base = mk(g, { agents: ["gp"], auditAgents: ["gp"], packFiles: ["./pack.json"], gatesOnly: true, writeRoots: ["/home/u/ws", "/tmp"] });
t("gates-only: no grounding prompt", ask(base, "Write scanpy code to filter cells", "b9") === "");
const bctx = { agentId: "gp", runId: "b10", sessionKey: "agent:gp:b10" };
t("gates-only: install still needs approval", Boolean(base.before_tool_call({ toolName: "exec", params: { command: "pip install polars" } }, bctx)?.requireApproval));
t("gates-only: outside write still needs approval", Boolean(base.before_tool_call({ toolName: "write", params: { path: "/home/u/other/x.py" } }, bctx)?.requireApproval));
t("gates-only: no answer rewrite", base.before_message_write({ message: { role: "assistant", content: "see https://made.up/x " + "q".repeat(300) } }, bctx) === undefined);
t("n_top_genes=2000 is not a year", !/FACTS:/.test(ask(scrna, "Review this scanpy code: sc.pp.highly_variable_genes(adata, n_top_genes=2000, subset=True)", "y1")));
t("'is X on by default now' asks for a search", /FACTS:/.test(ask(scrna, "In pandas, is Copy-on-Write on by default now?", "y3")));
t("'since 2025' still counts as a date", /FACTS:/.test(ask(scrna, "What changed in scanpy since 2025?", "y4")));
t("scrna pack: skills-first line names pack skills", /SKILLS: .*scanpy, bulk-rnaseq/.test(s));
t("no pack: no skills line", !/SKILLS:/.test(ask(bare, "Write numpy code to normalize rows", "b4")));
t("project interpreter file mentioned", /\.harness-python/.test(ask(mk(g, { agents: ["gp"], packFiles: ["./pack.json"], checkPython: "/env/bin/python" }), "Write scanpy code to filter cells", "s11")));
t("reminders say they add to the agent's own way of working", /keep using your skills and tools/.test(s));

// Experiment arms: no auditor; research brief first
const noaud = mk(g, { agents: ["gp"], auditAgents: [], packFiles: ["./pack.json"], writeRoots: ["/home/u/ws", "/tmp"] });
t("noaudit arm: code prompt has no audit step", !/AUDIT REQUIRED/.test(ask(noaud, "Write scanpy code to filter cells", "n1")));
const br = mk(g, { agents: ["gp"], auditAgents: [], packFiles: ["./pack.json"], writeRoots: ["/home/u/ws", "/tmp"], briefFirst: true });
const b1 = ask(br, "how should I think about choosing clustering resolution?", "br1");
t("brief arm: conceptual question gets research + brief", /BRIEF FIRST/.test(b1) && /\/home\/u\/ws\/briefs\/<short-topic>\.md/.test(b1));
t("brief off: same question gets no brief", !/BRIEF/.test(ask(noaud, "how should I think about choosing clustering resolution?", "br0")));
t("brief arm: trivial message untouched", ask(br, "thanks!", "br2") === "");
{
  const ctx = { agentId: "gp", runId: "br3", sessionKey: "agent:gp:br3" };
  br.before_prompt_build({ currentUserMessage: "Write scanpy code to flag doublets" }, ctx);
  br.after_tool_call({ toolName: "web_search", params: { query: "x" }, result: "https://scanpy.readthedocs.io/en/stable/api" }, ctx);
  const f = br.before_agent_finalize({ lastAssistantMessage: "Done.\n```py\nimport scanpy as sc\n```\nSources: scanpy docs" }, ctx);
  t("brief arm: no brief written → sent back", /brief/.test(f?.reason || ""));
}
{
  const ctx = { agentId: "gp", runId: "br4", sessionKey: "agent:gp:br4" };
  br.before_prompt_build({ currentUserMessage: "Write scanpy code to flag doublets" }, ctx);
  br.after_tool_call({ toolName: "exec", params: { command: "python -c 'import scanpy; print(scanpy.__version__)'" }, result: "1.11" }, ctx);
  br.after_tool_call({ toolName: "web_search", params: { query: "x" }, result: "ok" }, ctx);
  br.after_tool_call({ toolName: "write", params: { path: "/home/u/ws/briefs/doublets.md", content: "x" }, result: "ok" }, ctx);
  const f = br.before_agent_finalize({ lastAssistantMessage: "Done.\n```py\nimport scanpy as sc\n```\nSources: scanpy docs" }, ctx);
  t("brief arm: brief written → no brief problem", !/brief/.test(f?.reason || ""));
  const next = br.before_prompt_build({ currentUserMessage: "Now also write scanpy code to plot the doublet scores" }, { ...ctx, runId: "br5" })?.appendContext || "";
  t("brief arm: follow-up turn reuses the brief", /your research brief for this conversation is \/home\/u\/ws\/briefs\/doublets\.md/.test(next) && !/BRIEF FIRST/.test(next));
}
{
  const ctx = { agentId: "gp", runId: "br6", sessionKey: "agent:gp:br6" };
  br.before_prompt_build({ currentUserMessage: "Write scanpy code to flag doublets" }, ctx);
  br.after_tool_call({ toolName: "exec", params: { command: "cat > briefs/doublets.md <<'EOF'\n# brief\nEOF" }, result: "" }, ctx);
  br.after_tool_call({ toolName: "web_search", params: { query: "x" }, result: "ok" }, ctx);
  const f = br.before_agent_finalize({ lastAssistantMessage: "Done.\n```py\nimport scanpy as sc\n```\nSources: scanpy docs" }, ctx);
  t("brief arm: brief written via shell heredoc counts", !/brief/.test(f?.reason || ""));
}

// research-101 guide: named in facts-mode prompts until read, then dropped; absent when not configured
{
  const GUIDE = "/home/u/.openclaw/harness-skills/research-101/SKILL.md";
  const rg = mk(g, { agents: ["gp"], auditAgents: [], packFiles: ["./pack.json"], writeRoots: ["/home/u/ws", "/tmp"], briefFirst: true, researchGuide: GUIDE });
  const ctx = { agentId: "gp", runId: "rg1", sessionKey: "agent:gp:rg1" };
  const p1 = rg.before_prompt_build({ currentUserMessage: "What does the literature say about doublet detection benchmarks?" }, ctx)?.appendContext || "";
  t("guide: named before first search", p1.includes(`RESEARCH METHOD: before you search, read ${GUIDE}`));
  rg.after_tool_call({ toolName: "read", params: { path: GUIDE }, result: "# research-101", toolCallId: "x1" }, ctx);
  const p2 = rg.before_prompt_build({ currentUserMessage: "And what about recent preprints on it?" }, { ...ctx, runId: "rg2" })?.appendContext || "";
  t("guide: not repeated once read in this session", !/RESEARCH METHOD/.test(p2));
  t("guide: absent when not configured", !/RESEARCH METHOD/.test(ask(br, "What does the literature say about doublet detection benchmarks?", "rg3")));
  t("guide: absent in brief-free arm without facts cue", !/RESEARCH METHOD/.test(ask(mk(g, { agents: ["gp"], researchGuide: GUIDE }), "I have a 10 mg/mL BSA stock and need 25 mL of 0.5% BSA", "rg4")));
}
console.log(`${ok} passed, ${bad} failed`); process.exit(bad ? 1 : 0);
