"""Self-contained HTML report from a rea JSON result (repo, deps, company or diff).

One file, no external scripts or styles, every value escaped, source links kept.
Tables with a `sortable` class sort on header click (a short inline script).
Colours follow the viewer's light or dark preference.
"""

from __future__ import annotations

import html
import json
from urllib.parse import urlparse

CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#5c5c5c;--line:#d8d8d8;--head:#f3f3f3;--link:#0b57d0;--pro:#1e7f3a;--con:#b3261e;--note:#7a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#e6e6e6;--muted:#a3a3a3;--line:#333;--head:#1e1e1e;--link:#8ab4f8;--pro:#6ccf7e;--con:#ff8a80;--note:#e3c060}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin-inline:auto}
h1{font-size:1.6rem;margin:0 0 .25rem}h2{font-size:1.2rem;margin:2rem 0 .5rem;border-bottom:1px solid var(--line);padding-bottom:.25rem}
p.lead,p.note{color:var(--muted)}a{color:var(--link)}table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:.92rem}
th,td{border:1px solid var(--line);padding:.4rem .55rem;vertical-align:top;text-align:left}th{background:var(--head);position:sticky;top:0}
table.sortable th{cursor:pointer;user-select:none}table.sortable th::after{content:" \\2195";color:var(--muted);font-size:.8em}
.pro{color:var(--pro)}.con{color:var(--con)}.note{color:var(--note)}.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 .4em;font-size:.85em;margin-right:.25em}
ul.claims li{margin:.3rem 0}.src{font-size:.85em;white-space:nowrap}code{font-size:.9em;background:var(--head);padding:0 .25em;border-radius:3px}
@media (max-width:640px){table{display:block;overflow-x:auto}}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(function(t){
  t.querySelectorAll('th').forEach(function(th,i){th.addEventListener('click',function(){
    var rows=Array.from(t.tBodies[0].rows), asc=!(th.dataset.asc==='1');
    t.querySelectorAll('th').forEach(function(h){delete h.dataset.asc}); th.dataset.asc=asc?'1':'0';
    rows.sort(function(a,b){var x=a.cells[i].dataset.v||a.cells[i].textContent,y=b.cells[i].dataset.v||b.cells[i].textContent;
      var nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)&&/^\\s*-?[\\d.]+/.test(x)&&/^\\s*-?[\\d.]+/.test(y))return asc?nx-ny:ny-nx;
      return asc?x.localeCompare(y):y.localeCompare(x)});
    rows.forEach(function(r){t.tBodies[0].appendChild(r)})})})});
"""


def esc(value) -> str:
	return html.escape("" if value is None else str(value), quote=True)


def link(url: str, text: str | None = None) -> str:
	parsed = urlparse(str(url))
	if parsed.scheme not in ("http", "https"):
		return esc(url)
	return f'<a href="{esc(url)}" rel="noopener noreferrer">{esc(text or parsed.netloc + (parsed.path if len(parsed.path) < 40 else "/..."))}</a>'


def sources(evidence: list) -> str:
	urls = [e.get("url") if isinstance(e, dict) else e for e in evidence or []]
	return " ".join(f'<span class="src">[{link(u, str(i + 1))}]</span>' for i, u in enumerate(urls) if u)


def table(headers: list[str], rows: list[list[str]], sortable: bool = True) -> str:
	if not rows:
		return "<p class=\"note\">None.</p>"
	head = "".join(f"<th>{esc(h)}</th>" for h in headers)
	body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
	return f'<table class="{"sortable" if sortable else ""}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def ledger_html(ledger: dict) -> str:
	out = []
	titles = (("strength", "Pros", "pro"), ("weakness", "Cons", "con"), ("revenue_model", "How it makes money", ""), ("fact", "Facts", ""))
	for kind, title, cls in titles:
		items = [c for c in ledger.get("sourced", []) if c.get("kind") == kind]
		if items:
			out.append(f"<h2>{title}</h2><ul class=\"claims\">" + "".join(
				f'<li class="{cls}">{esc(c.get("text"))} {sources(c.get("evidence", []))}</li>' for c in items) + "</ul>")
	if ledger.get("unverified"):
		out.append("<h2>Unverified (not used in the matrix or the plan)</h2><ul class=\"claims\">" + "".join(
			f'<li><span class="tag">{esc(c.get("kind"))}</span>{esc(c.get("text"))}</li>' for c in ledger["unverified"]) + "</ul>")
	return "".join(out)


def plan_html(plan: list[dict]) -> str:
	rows = [[f'<span class="tag">{esc(p.get("action"))}</span>', esc(p.get("step")), esc(p.get("measure") or "define one"),
		esc(p.get("because")) + " " + sources(p.get("evidence", []))] for p in plan or []]
	return "<h2>Original build plan</h2>" + table(["Action", "Step", "Measure of done", "Because"], rows, sortable=False)


def figures_html(figures: list[dict]) -> str:
	if not figures:
		return ""
	rows = []
	for f in figures:
		value = f"${f['low'] / 1000:.2f}B" if f["low"] >= 1000 else f"${f['low']:.0f}M"
		if f["high"] != f["low"]:
			value += " to " + (f"${f['high'] / 1000:.2f}B" if f["high"] >= 1000 else f"${f['high']:.0f}M")
		flags = [x for x in (("UNVERIFIED" if not f.get("sources") else ""), ("DISPUTED" if f["high"] / f["low"] > 1.25 else "")) if x]
		rows.append([esc(f.get("entity")), esc(f.get("kind")), value, esc(f.get("as_of") or "unknown"), esc(", ".join(flags) or "ok"), sources(f.get("sources", []))])
	return "<h2>Figures</h2><p class=\"note\">Compare figures only within one metric kind.</p>" + table(["Entity", "Metric kind", "Figure", "As of", "Flags", "Sources"], rows)


def checks_html(checks: list[dict]) -> str:
	if not checks:
		return ""
	ok = sum(1 for c in checks if c.get("status") == "verified")
	rows = [[esc(c.get("status")), link(c.get("url")), esc((c.get("quote") or "")[:120]), esc(c.get("reason") or "")] for c in checks]
	return f"<h2>Quote check</h2><p class=\"note\">{ok} of {len(checks)} source quotes were found on their pages.</p>" + table(["Status", "Source", "Quote", "Detail"], rows)


def deps_html(d: dict) -> str:
	rows = []
	for dep in d.get("dependencies", []):
		lic = link(dep["license_source"], dep.get("license") or "not stated") if dep.get("license_source") else "not looked up"
		if dep.get("license_note"):
			lic += f' <span class="note">({esc(dep["license_note"])})</span>'
		spec = f"<code>{esc(dep.get('spec'))}</code>" + (f' <span class="note">({esc(dep["version_from"])})</span>' if dep.get("version_from") else "")
		rows.append([esc(dep.get("name")), spec, esc(dep.get("locked") or ""), esc(dep.get("ecosystem")), esc(dep.get("scope")), esc(dep.get("manifest")), lic,
			esc(dep.get("license_class") if dep.get("looked_up") else "n/a")])
	vulns = []
	for f in ((d.get("osv") or {}).get("findings") or []):
		for a in f.get("advisories", []):
			vulns.append([esc(f["name"]), esc(f["version"]), link("https://osv.dev/vulnerability/" + a["id"], a["id"]), esc(a.get("severity") or "not stated"), esc(a.get("summary") or "")])
	vuln_html = ("<h2>Known vulnerabilities (OSV.dev)</h2>" + table(["Dependency", "Version", "Advisory", "Severity", "Summary"], vulns)) if vulns else ""
	return vuln_html + "<h2>Dependency list</h2>" + table(["Name", "Version spec", "Locked", "Ecosystem", "Scope", "Manifest", "License", "Class"], rows)


def diff_html(r: dict) -> str:
	out = []
	u = r.get("unverified", {})
	if u.get("ours") or u.get("theirs"):
		out.append(f"<p class=\"note\">Unverified claims are not compared: {esc(u.get('ours'))} in ours, {esc(u.get('theirs'))} in theirs.</p>")
	if r.get("kind") == "repo":
		rows = [[esc(x["label"]), f'<span class="tag">{esc(x["verdict"])}</span>', esc(x.get("ours_text") or "not checked") + " " + sources(x.get("ours_evidence", [])),
			esc(x.get("theirs_text") or "not checked") + " " + sources(x.get("theirs_evidence", []))] for x in r.get("rows", [])]
		out.append("<h2>Check by check</h2>" + table(["Check", "Verdict", "Ours", "Theirs"], rows))
	else:
		rows = []
		for m in r.get("suggested_matches", []):
			rows.append([f'<span class="tag">{esc(m["meaning"])}</span>', esc(f"{m['score']:.2f}" if m.get("score") is not None else "-"),
				f'<span class="tag">{esc(m["ours"]["kind"])}</span>{esc(m["ours"]["text"])}', f'<span class="tag">{esc(m["theirs"]["kind"])}</span>{esc(m["theirs"]["text"])}',
				esc(", ".join(m.get("shared_words", []))), esc(", ".join(m.get("opposite_on", [])) or "-"),
				esc(m.get("found_by", "vocabulary")) + (f": {esc(m['model_reason'])}" if m.get("model_reason") else "")])
		out.append("<h2>Suggested matches</h2><p class=\"note\">Suggestions to confirm, never merged.</p>" + table(["Meaning", "Score", "Ours", "Theirs", "Shared", "Opposite on", "Found by"], rows))
		titles = {"strength": "Pros", "weakness": "Cons", "revenue_model": "How it makes money", "fact": "Facts"}
		for kind, sides in (r.get("side_by_side") or {}).items():
			if sides.get("ours") or sides.get("theirs"):
				out.append(f"<h2>{titles.get(kind, kind)}</h2>" + table(["Ours", "Theirs"], [[
					"<ul>" + "".join(f"<li>{esc(c['text'])}</li>" for c in sides["ours"]) + "</ul>" or "none sourced",
					"<ul>" + "".join(f"<li>{esc(c['text'])}</li>" for c in sides["theirs"]) + "</ul>" or "none sourced"]], sortable=False))
	return "".join(out)


def kind_of(data: dict) -> str:
	if "kind" in data and ("rows" in data or "side_by_side" in data):
		return "diff"
	if "dependencies" in data and "manifests" in data:
		return "deps"
	if "entity" in data and "ledger" in data:
		return "company"
	if "repo" in data and "ledger" in data:
		return "repo"
	raise ValueError("not a rea JSON result (expected the output of repo, deps, company or diff with --format json)")


def render(data: dict) -> str:
	kind = kind_of(data)
	if kind == "repo":
		title, lead = f"Teardown: {data['repo']}", f"Source: {link(data.get('html_url', ''))}. Every claim cites the GitHub API response it came from."
		body = ledger_html(data.get("ledger", {})) + plan_html(data.get("plan", []))
	elif kind == "deps":
		title, lead = f"Dependencies: {data['repo']}", "Licenses come from the package registry; each is linked to the answer it came from."
		body = ledger_html(data.get("ledger", {})) + deps_html(data)
	elif kind == "company":
		title, lead = f"Teardown: {data['entity']}", f"Dossier as of {esc(data.get('as_of') or 'unknown')}. Claims without a verified source are listed as unverified."
		body = checks_html(data.get("checks", [])) + ledger_html(data.get("ledger", {})) + figures_html(data.get("figures", [])) + plan_html(data.get("plan", []))
	else:
		title, lead = f"Diff: {data['ours']} (ours) vs {data['theirs']} (theirs)", "Only sourced claims are compared."
		body = diff_html(data)
	return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
		f"<title>{esc(title)}</title><style>{CSS}</style></head><body><h1>{esc(title)}</h1><p class=\"lead\">{lead}</p>{body}"
		f"<p class=\"note\">Generated by rea. Raw data: <code>{esc(len(json.dumps(data)))} bytes of JSON</code>.</p><script>{JS}</script></body></html>")
