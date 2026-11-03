// grounded: map step and claim check (answer-file gate + up-front instruction). Tested both ways.
import { rmSync, mkdirSync, writeFileSync, appendFileSync, readFileSync } from "node:fs";
import plugin from "./grounded.js";

const LOG = "/tmp/harness-test-grounded-claims.log";
const WS = "/tmp/harness-test-claims-ws";
rmSync(LOG, { force: true }); rmSync(WS, { recursive: true, force: true }); mkdirSync(`${WS}/briefs`, { recursive: true });
function load(extra) {
  const H = {};
  plugin.register({
    pluginConfig: { agents: ["gp"], logFile: LOG, checkPython: "/env/bin/python", writeRoots: [WS, "/tmp"], packFiles: ["./pack.json"],
      briefFirst: true, briefDir: `${WS}/briefs`, ...extra },
    on: (n, f) => (H[n] = f),
  });
  return H;
}
let ok = 0, bad = 0;
const t = (name, cond) => { cond ? ok++ : bad++; console.log(`${cond ? "PASS" : "FAIL"} ${name}`); };
let n = 0;
const logFor = (ctx) => readFileSync(LOG, "utf8").split("\n").filter((l) => l.includes(`run=${ctx.runId}`));
function run(H) {
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  const prompt = H.before_prompt_build({ currentUserMessage: "Count the mural nuclei in this snRNA dataset." }, ctx)?.appendContext || "";
  const call = (toolName, params, result) => {
    const pre = H.before_tool_call({ toolName, params }, ctx);
    if (pre?.block) return pre;
    H.after_tool_call({ toolName, params, result, toolCallId: `${n}-${Math.random()}` }, ctx);
    return pre;
  };
  call("web_search", { query: "pericyte markers snRNA" }, "results");
  const brief = `${WS}/briefs/mural-${n}.md`;
  writeFileSync(brief, "# Brief\n## Map\n- sparse\n## Research\n- Pericyte markers: Pdgfrb, Rgs5 https://a.org/x\n- b https://b.org/y\n");
  call("write", { path: `briefs/mural-${n}.md`, content: "x" }, "ok");
  return { ctx, call, brief, prompt };
}
const ANS = "/tmp/harness-test-claims-ws/bench/agent_workspace/analysis_output.json";
const table = "| claim | brief line | label | lb |\n| Sox17 is a pericyte marker | - | NOT COVERED | yes |\n| Pdgfrb marks pericytes | 4 | SUPPORTED | yes |\nAnswer: 0 (no CONTRADICTED claims).\n" + "y".repeat(200);

{ // Research first: no brief → blocked; brief without opened sources → blocked; cap 2, then allowed with a warning.
  const H = load({ claimCheck: true, answerFiles: ["analysis_output.json"] });
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  H.before_prompt_build({ currentUserMessage: "Count the mural nuclei in this snRNA dataset." }, ctx);
  const call = (toolName, params, result) => { const pre = H.before_tool_call({ toolName, params }, ctx); if (!pre?.block) H.after_tool_call({ toolName, params, result, toolCallId: Math.random().toString() }, ctx); return pre; };
  const b1 = call("exec", { command: `python -c "open('${ANS}','w')"` }, "ok");
  t("no brief: answer write blocked, told to research first", b1?.block === true && /research comes before the answer/.test(b1.blockReason));
  const brief = `${WS}/briefs/memory-${n}.md`;
  writeFileSync(brief, "# Brief\n## Map\n- sparse\n## Research\n- from memory https://never-opened.org/a\n");
  call("write", { path: `briefs/memory-${n}.md`, content: "x" }, "ok");
  const b2 = call("write", { path: ANS, content: "{}" }, "ok");
  t("brief with 0 opened sources: blocked again", b2?.block === true && /0 source\(s\) you actually opened/.test(b2.blockReason));
  const b3 = call("write", { path: ANS, content: "{}" }, "ok");
  t("after 2 research blocks: write allowed (cap)", !b3?.block);
  t("cap logged with research MISSING", logFor(ctx).some((l) => /answer-gate: allowed after 2 block\(s\) research=MISSING/.test(l)));
  H.before_agent_finalize({ lastAssistantMessage: "Answer 92.\n" + "x".repeat(200) }, ctx);
  t("missing research warned in the saved answer", logFor(ctx).some((l) => /warning queued \(.*brief/.test(l) || /STILL ungrounded.*brief|ungrounded \(.*brief/.test(l)));
}
{ // Research done (opened sources) + claim check: one claim-check block, then allowed and logged.
  const H = load({ mapFirst: true, claimCheck: true, answerFiles: ["analysis_output.json"] });
  const { ctx, call, brief, prompt } = run(H);
  t("prompt carries the claim check up front", /CLAIM CHECK/.test(prompt) && /before writing the answer file/.test(prompt));
  call("web_fetch", { url: "https://a.org/x" }, "Pericyte markers page with enough real content to not look like an error page. ".repeat(5));
  call("web_fetch", { url: "https://b.org/y" }, "Second source page with enough real content to not look like an error page. ".repeat(5));
  const b1 = call("write", { path: ANS, content: '{"mural_nuclei": 92}' }, "ok");
  t("research ok: blocked once for the claim check", b1?.block === true && /CLAIM CHECK/.test(b1.blockReason) && /load-bearing/.test(b1.blockReason));
  call("web_search", { query: "Sox17 endothelial" }, "results");
  appendFileSync(brief, "## Added by claim check\n- Sox17: arterial endothelial TF https://c.org/z\n");
  const b2 = call("exec", { command: `python -c "open('${ANS}','w').write('{}')"` }, "ok");
  t("then the answer write is allowed", !b2?.block);
  t("gate outcome logged (1 block, research ok, brief grew)", logFor(ctx).some((l) => /answer-gate: allowed after 1 block\(s\) research=ok opened=2 brief\+\d+B searches \d+->\d+/.test(l) && !/brief\+0B/.test(l)));
  t("finalize: no revise; table and gate state logged", H.before_agent_finalize({ lastAssistantMessage: table }, ctx) === undefined &&
    logFor(ctx).some((l) => /claim-check: table supported=1 contradicted=0 not-covered=1 gate=claim-checked/.test(l)));
}
{ // No answer files (chat task): no gate; missing table is logged.
  const H = load({ claimCheck: true });
  const { ctx, call } = run(H);
  t("chat task: writes are never gated", !call("write", { path: "/tmp/harness-test-claims-ws/notes.md", content: "x" }, "ok")?.block);
  H.before_agent_finalize({ lastAssistantMessage: "Answer: 92.\n" + "x".repeat(200) }, ctx);
  t("chat task: missing table logged, gate n/a", logFor(ctx).some((l) => /claim-check: table supported=0 contradicted=0 not-covered=0 \(no table\) gate=n\/a/.test(l)));
}
{ // Claim check off: answer writes untouched, no claim-check instruction.
  const H = load({ answerFiles: ["analysis_output.json"] });
  const { call, prompt } = run(H);
  t("off: no claim-check instruction", !/CLAIM CHECK/.test(prompt));
  const ob = call("write", { path: ANS, content: "{}" }, "ok");
  t("claim check off: research gate still applies, no claim-check block", ob?.block === true && !/CLAIM CHECK/.test(ob.blockReason));
}
{ // Map first on/off.
  const on = load({ mapFirst: true }), off = load({});
  const pa = on.before_prompt_build({ currentUserMessage: "Count the mural nuclei." }, { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:m-${n}` })?.appendContext || "";
  const pb = off.before_prompt_build({ currentUserMessage: "Count the mural nuclei." }, { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:m-${n}` })?.appendContext || "";
  t("map on: Map section, 5-10 sources, prescribed methods followed", /## Map/.test(pa) && /5-10 primary/.test(pa) && /follow them exactly/.test(pa) && !/BRIEF FIRST/.test(pa));
  t("map off: plain brief prompt", /BRIEF FIRST/.test(pb) && !/## Map/.test(pb));
}
{ // Table counted from the brief too, and prose mentions of a label are not rows; judgments requested.
  const H = load({ mapFirst: true, claimCheck: true, answerFiles: ["analysis_output.json"] });
  const { ctx, call, brief } = run(H);
  call("web_fetch", { url: "https://a.org/x" }, "Real page content, long enough to not be an error page at all. ".repeat(5));
  call("web_fetch", { url: "https://b.org/y" }, "Another real page, long enough to not be an error page at all. ".repeat(5));
  const b = call("write", { path: ANS, content: "{}" }, "ok");
  t("claim check asks for judgment claims per Map decision", /judgment itself as a claim/.test(b?.blockReason || ""));
  appendFileSync(brief, "## Claim check table\n| # | claim | line | status | lb |\n|---|---|---|---|---|\n| 1 | a | x | SUPPORTED | yes |\n| 2 | b | x | SUPPORTED | no |\n| 3 | c | - | CONTRADICTED | yes |\n");
  call("write", { path: ANS, content: "{}" }, "ok");
  H.before_agent_finalize({ lastAssistantMessage: "Done; table is in the brief; no CONTRADICTED claims needed rework.\n" + "x".repeat(200) }, ctx);
  t("rows counted from the brief, prose mention ignored", logFor(ctx).some((l) => /claim-check: table supported=2 contradicted=1 not-covered=0 gate=claim-checked/.test(l)));
}
{ // Research first: 3 inspection runs, then analysis held until the brief cites 2 opened pages.
  const H = load({ mapFirst: true, researchFirst: true });
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  const p = H.before_prompt_build({ currentUserMessage: "Find the top 2000 HVGs in this dataset." }, ctx)?.appendContext || "";
  t("prompt tells the agent about the hold up front", /Analysis runs are held until the brief cites at least 2 pages you opened/.test(p));
  const call = (toolName, params, result) => { const pre = H.before_tool_call({ toolName, params }, ctx); if (!pre?.block) H.after_tool_call({ toolName, params, result, toolCallId: Math.random().toString() }, ctx); return pre; };
  const py = (c) => call("exec", { command: c }, "ok");
  t("inspection runs 1-3 allowed", [1, 2, 3].every((i) => !py(`/env/bin/python -c "import anndata; print(${i})"`)?.block));
  const b = py("python3 run_hvg.py");
  t("4th interpreter run (analysis) blocked before research", b?.block === true && /research comes before analysis/.test(b.blockReason));
  t("shell reads and version checks are never held", !call("exec", { command: "ls -la data && head -3 notes.txt" }, "ok")?.block && !py(`/env/bin/python -c "import scanpy; print(scanpy.__version__)"`)?.block);
  t("python that fetches a URL (research) is not held", !py(`python3 -c "import urllib.request as u; u.urlopen('https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=hvg')"`)?.block);
  call("web_search", { query: "hvg selection sparse data" }, "results");
  call("web_fetch", { url: "https://a.org/x" }, "Real page content, long enough to not be an error page at all. ".repeat(5));
  call("web_fetch", { url: "https://b.org/y" }, "Another real page, long enough to not be an error page at all. ".repeat(5));
  const brief = `${WS}/briefs/hvg-${n}.md`;
  writeFileSync(brief, "# Brief\n## Map\n- D1 hvg method\n## Research\n- a https://a.org/x\n- b https://b.org/y\n");
  call("write", { path: `briefs/hvg-${n}.md`, content: "x" }, "ok");
  t("after research: analysis run allowed", !py("python3 run_hvg.py")?.block);
  t("gate opening logged", logFor(ctx).some((l) => /research-gate: open \(opened=2 searches=\d+\) after 3 inspection run\(s\), 1 block\(s\)/.test(l)));
}
{ // Research first: cap of 3 blocks, then allowed and warned.
  const H = load({ researchFirst: true, inspectRuns: 0 });
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  H.before_prompt_build({ currentUserMessage: "Find the top 2000 HVGs in this dataset." }, ctx);
  const tryRun = () => H.before_tool_call({ toolName: "exec", params: { command: "python3 run.py" } }, ctx);
  t("blocked 3 times", [1, 2, 3].every(() => tryRun()?.block === true));
  t("4th attempt allowed (cap)", !tryRun()?.block);
  t("violation logged", logFor(ctx).some((l) => /research-gate: cap reached → analysis allowed, research-first violated/.test(l)));
  H.before_agent_finalize({ lastAssistantMessage: "Answer.\n" + "x".repeat(200) }, ctx);
  t("violation warned in the saved answer", logFor(ctx).some((l) => /ungrounded \(.*brief/.test(l)));
}
{ // Research first off: interpreter runs never held.
  const H = load({});
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  H.before_prompt_build({ currentUserMessage: "Find the top 2000 HVGs in this dataset." }, ctx);
  t("off: analysis runs not held", [1, 2, 3, 4, 5].every(() => !H.before_tool_call({ toolName: "exec", params: { command: "python3 run.py" } }, ctx)?.block));
}
{ // Counter: 'SUPPORTED (3 opened sources)' cells are counted.
  const H = load({ claimCheck: true });
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  H.before_prompt_build({ currentUserMessage: "Find the top 2000 HVGs in this dataset." }, ctx);
  H.before_agent_finalize({ lastAssistantMessage: "| a | D1 | SUPPORTED (3 opened primary sources) | yes |\n| b | - | NOT COVERED as prediction | no |\n" + "x".repeat(200) }, ctx);
  t("annotated label cells counted", logFor(ctx).some((l) => /claim-check: table supported=1 contradicted=0 not-covered=1/.test(l)));
}
{ // Python fetches count as opened pages (and can open the research gate).
  const H = load({ researchFirst: true, inspectRuns: 0 });
  const ctx = { agentId: "gp", runId: `c${++n}`, sessionKey: `agent:gp:claims-${n}` };
  H.before_prompt_build({ currentUserMessage: "Find the top 2000 HVGs in this dataset." }, ctx);
  const call = (toolName, params, result) => { const pre = H.before_tool_call({ toolName, params }, ctx); if (!pre?.block) H.after_tool_call({ toolName, params, result, toolCallId: Math.random().toString() }, ctx); return pre; };
  for (const u of ["https://a.org/hvg", "https://b.org/sparse"]) call("exec", { command: `python3 -c "import urllib.request as r; print(r.urlopen('${u}').read()[:2000])"` }, "Real page body about HVG selection on sparse data, long enough. ".repeat(5));
  const brief = `${WS}/briefs/py-${n}.md`;
  writeFileSync(brief, "## Map\n- D1\n## Research\n- https://a.org/hvg\n- https://b.org/sparse\n");
  call("write", { path: `briefs/py-${n}.md`, content: "x" }, "ok");
  t("python-fetched pages count as opened; analysis allowed", !call("exec", { command: "python3 run_hvg.py" }, "ok")?.block);
}
console.log(`${ok} passed, ${bad} failed`);
process.exit(bad ? 1 : 0);
