"""Sums up Grype's findings for the image on the build's page, and fails when a critical or high one has a fix.

Run by the scan job in .github/workflows/docker.yml with the path of Grype's JSON report. Uses only the standard
library, so nothing is installed for it. Findings that .grype.yaml leaves out are listed on their own."""

import json
import os
import sys
from collections import Counter, defaultdict

ORDER = ["Critical", "High", "Medium", "Low", "Negligible", "Unknown"]


def rows_of(matches: list) -> list[dict]:
    out = []
    for m in matches:
        v, a = m.get("vulnerability", {}), m.get("artifact", {})
        fix = v.get("fix", {})
        ups = [u.get("name") for u in a.get("upstreams") or [] if u.get("name")]
        out.append({"id": v.get("id", "?"), "severity": v.get("severity") or "Unknown",
                    "package": a.get("name", "?"), "source": ups[0] if ups else a.get("name", "?"),
                    "version": a.get("version", "?"), "type": a.get("type", "?"),
                    "where": ", ".join(sorted({l.get("path", "") for l in a.get("locations") or []}))[:120],
                    "fixed": ", ".join(fix.get("versions") or []) if fix.get("state") == "fixed" else ""})
    return out


def rank(severity: str) -> int:
    return ORDER.index(severity) if severity in ORDER else len(ORDER)


def once(rows: list[dict]) -> list[dict]:
    """Each vulnerability once per source package (Debian's ffmpeg covers libavcodec61, libavformat61 and more)."""
    return list({(r["source"], r["type"], r["id"]): r for r in rows}.values())


def by_source(rows: list[dict]) -> list[str]:
    groups = defaultdict(list)
    for r in once(rows):
        groups[(r["source"], r["type"])].append(r)
    lines = ["| Package | Critical | High | Medium | Lower | With a fix |", "|---|---|---|---|---|---|"]
    for (source, kind), rs in sorted(groups.items(),
                                     key=lambda g: tuple(-sum(x["severity"] == s for x in g[1]) for s in ORDER[:3])):
        c = Counter(r["severity"] for r in rs)
        lower = sum(n for s, n in c.items() if rank(s) > 2)
        lines.append(f"| {source} ({kind}) | {c['Critical']} | {c['High']} | {c['Medium']} | {lower}"
                     f" | {sum(1 for r in rs if r['fixed'])} |")
    return lines


def tally(rows: list[dict]) -> str:
    c = Counter(r["severity"] for r in once(rows))
    return ", ".join(f"{c[s]} {s.lower()}" for s in ORDER if c[s])


def main(path: str) -> int:
    report = json.load(open(path, encoding="utf-8"))
    rows = rows_of(report.get("matches", []))
    ignored = rows_of(report.get("ignoredMatches", []))
    fixable = sorted([r for r in rows if r["fixed"]], key=lambda r: (rank(r["severity"]), r["package"], r["id"]))
    urgent = [r for r in fixable if r["severity"] in ("Critical", "High")]
    distro = report.get("distro") or {}
    desc = report.get("descriptor") or {}
    db = desc.get("db") or {}
    built = db.get("built") or (db.get("status") or {}).get("built") or "?"
    out = ["## Known vulnerabilities in the image",
           f"Grype {desc.get('version', '?')}, database built {built}; {distro.get('name', '?')} {distro.get('version', '')}.",
           ""]
    if not rows:
        out.append("No known vulnerabilities.")
    else:
        out.append(f"{tally(rows)}, each counted once per package; {len(fixable)} findings with a fix available.")
    if fixable:
        out += ["", "### With a fix available", "",
                "| Severity | Vulnerability | Package | Installed | Fixed in |", "|---|---|---|---|---|"]
        out += [f"| {r['severity']} | {r['id']} | {r['package']} ({r['type']}) | {r['version']} | {r['fixed']} |"
                for r in fixable[:150]]
    if rows:
        out += ["", "### By package", ""] + by_source(rows)
    if ignored:
        where = sorted({r["where"] for r in ignored if r["where"]})
        out += ["", f"### Left out by .grype.yaml: {tally(ignored)}", "",
                "See .grype.yaml for why." + (" In " + "; ".join(where[:4]) + "." if where else "")]
    if urgent:
        out += ["", f"**{len(urgent)} critical or high with a fix available.** Debian's fixes come in with the weekly "
                     "build, which starts afresh; a Python package needs its newer version in requirements.txt."]
    text = "\n".join(out) + "\n"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 1 if urgent else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
