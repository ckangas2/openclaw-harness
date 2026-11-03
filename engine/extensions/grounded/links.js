// Shared link rules for grounded and audit-gate. The two copies must stay identical
// (engine/tests checks this). A link counts as a SOURCE only if its page was READ in
// the session: fetched successfully (not an error or "not found" page) or downloaded
// with curl/wget. Links merely SEEN (search results, links inside other pages) prove
// the page exists, not that its content was used.

export const URL_RE = /https?:\/\/[^\s<>"'`)\]]+/gi;
// Links written without a scheme, e.g. "openai.com/index/harness-engineering/".
// A path is required, so bare names like "scanpy.io" in prose are not counted.
const TLDS = "com|org|net|io|ai|dev|edu|gov|bio|science|app|co|uk|de|ch|fr|nl|jp|cn|info|me|xyz|int|eu";
export const BARE_URL_RE = new RegExp(
  String.raw`(?<![@\w/.:-])((?:[a-z0-9-]+\.)+(?:${TLDS})\/[^\s<>"'\`)\]]*)`,
  "gi",
);

// Canonical form: host + path, lowercase, no www, no trailing slash, no query/fragment,
// with known mirrors and variants mapped to one address.
export function canonUrl(u) {
  let x;
  try {
    const raw = String(u).trim().replace(/[.,;:!?]+$/, "");
    x = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
  } catch {
    return null;
  }
  let host = x.host.toLowerCase().replace(/^www\./, "");
  if (/^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$/.test(host)) return null;
  let path = decodeURIComponent(x.pathname).replace(/\/+$/, "");
  // arXiv: abs / pdf / html, any version, and the alphaxiv mirror are the same paper
  let m = (host + path).match(/^(?:arxiv\.org|alphaxiv\.org|export\.arxiv\.org)\/(?:abs|pdf|html)\/([a-z.-]*\/?\d{4}\.\d{4,5}|[a-z.-]+\/\d{7})(?:v\d+)?(?:\.pdf)?$/i);
  if (m) return `arxiv.org/abs/${m[1].toLowerCase()}`;
  // DOI resolvers
  if (host === "dx.doi.org") host = "doi.org";
  // PubMed Central moved hosts
  m = (host + path).match(/^(?:ncbi\.nlm\.nih\.gov\/pmc|pmc\.ncbi\.nlm\.nih\.gov)\/articles\/(pmc\d+)$/i);
  if (m) return `pmc.ncbi.nlm.nih.gov/articles/${m[1].toLowerCase()}`;
  // A repository's README read raw counts as reading the repository page
  m = (host + path).match(/^raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/[^/]+\/readme(\.md|\.rst|\.txt)?$/i);
  if (m) return `github.com/${m[1]}/${m[2]}`.toLowerCase();
  m = (host + path).match(/^github\.com\/([^/]+)\/([^/]+)(?:\/(?:tree|blob)\/[^/]+(?:\/readme(\.md|\.rst)?)?)?$/i);
  if (m) return `github.com/${m[1]}/${m[2]}`.toLowerCase();
  return (host + path).toLowerCase();
}

function textOf(v) {
  if (typeof v === "string") return v;
  try { return JSON.stringify(v ?? ""); } catch { return String(v ?? ""); }
}

// Every URL mentioned in a tool result (search hits, links inside fetched pages, output).
export function urlsIn(value) {
  return (textOf(value).match(URL_RE) || []).map(canonUrl).filter(Boolean);
}

// Did a fetch come back as an error page? Tools often return 404 pages as content.
export function looksLikeErrorPage(result) {
  const t = textOf(result);
  if (/\\?"status(?:Code)?\\?"\s*:\s*\\?"?(4\d\d|5\d\d)\b/.test(t)) return true;
  if (/\bHTTP\/[\d.]+\s+[45]\d\d\b/.test(t)) return true;
  // Short bodies that are just a not-found message
  const body = t.replace(/SECURITY NOTICE[\s\S]{0,600}?(?:\n\n|$)/i, "");
  if (body.length < 4000 && /\b(404|page not found|not found|no such (page|file)|does not exist)\b/i.test(body)) return true;
  return false;
}

// URLs a shell command downloaded (curl/wget). Counted as read only if the command succeeded
// and the output is not an error page.
export function downloadedUrls(command) {
  const cmd = String(command || "");
  // Shell downloaders, and Python fetches (urllib/requests/httpx) run through the shell.
  if (!/\b(curl|wget|http(ie)?|urlopen|urllib|requests\.(get|post)|httpx)\b/.test(cmd)) return [];
  return (cmd.match(URL_RE) || []).map(canonUrl).filter(Boolean);
}

// Every link cited in an answer, scheme or not.
export function citedLinks(text) {
  const t = String(text || "");
  const out = [];
  for (const raw of t.match(URL_RE) || []) out.push(raw.replace(/[.,;:!?)\]]+$/, ""));
  const rest = t.replace(URL_RE, " ");
  for (const m of rest.matchAll(BARE_URL_RE)) out.push(m[1].replace(/[.,;:!?)\]]+$/, ""));
  return [...new Set(out)];
}

// Sort the cited links into read / seen-only / unknown. A seen-only link marked
// [UNVERIFIED] on its own line is honest and not counted against the answer.
export function classifyLinks(text, read, seen) {
  const lines = String(text || "").split("\n");
  const res = { read: [], seenOnly: [], unknown: [], marked: [] };
  for (const raw of citedLinks(text)) {
    const c = canonUrl(raw);
    if (!c) continue;
    if (read.has(c)) { res.read.push(raw); continue; }
    // An abbreviated link ("host/path/...pdf") matches an opened page that starts with the shown part,
    // as long as the shown part goes past the domain (no matching a whole site from "host/...").
    const cut = raw.split(/\.\.\.|\u2026/)[0];
    if (cut !== raw) {
      const pre = canonUrl(cut);
      if (pre && pre.includes("/") && [...read].some((r) => r.startsWith(pre))) { res.read.push(raw); continue; }
    }
    const line = lines.find((l) => l.includes(raw)) || "";
    const marked = /\bunverified\b|not (opened|read|verified)|(didn'?t|did not|never) (open|read)|snippet only|not opened/i.test(line);
    if (seen.has(c)) (marked ? res.marked : res.seenOnly).push(raw);
    else (marked ? res.marked : res.unknown).push(raw);
  }
  return res;
}
