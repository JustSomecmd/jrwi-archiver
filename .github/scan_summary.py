"""Sums up Grype's findings for the image on the build's page, and fails when a critical or high one has a fix.

Run by the scan job in .github/workflows/docker.yml with the path of Grype's JSON report. Uses only the standard
library, so nothing is installed for it."""

import json
import os
import sys
from collections import Counter

ORDER = ["Critical", "High", "Medium", "Low", "Negligible", "Unknown"]


def main(path: str) -> int:
    report = json.load(open(path, encoding="utf-8"))
    rows = []
    for m in report.get("matches", []):
        v, a = m.get("vulnerability", {}), m.get("artifact", {})
        fix = v.get("fix", {})
        rows.append({"id": v.get("id", "?"), "severity": v.get("severity") or "Unknown",
                     "package": a.get("name", "?"), "version": a.get("version", "?"), "type": a.get("type", "?"),
                     "fixed": ", ".join(fix.get("versions") or []) if fix.get("state") == "fixed" else ""})
    rows.sort(key=lambda r: (ORDER.index(r["severity"]) if r["severity"] in ORDER else 9, not r["fixed"],
                             r["package"], r["id"]))
    counts = Counter(r["severity"] for r in rows)
    fixable = [r for r in rows if r["fixed"]]
    urgent = [r for r in fixable if r["severity"] in ("Critical", "High")]
    distro = report.get("distro") or {}
    db = (report.get("descriptor") or {}).get("db") or {}
    out = [f"## Known vulnerabilities in the image",
           f"Scanned with Grype {report.get('descriptor', {}).get('version', '?')}"
           f" (database built {db.get('built') or (db.get('status') or {}).get('built') or '?'}),"
           f" on {distro.get('name', '?')} {distro.get('version', '')}.", ""]
    if not rows:
        out.append("No known vulnerabilities.")
    else:
        out.append(", ".join(f"{counts[s]} {s.lower()}" for s in ORDER if counts[s])
                   + f"; {len(fixable)} with a fix available.")
        out.append("")
        shown = [r for r in rows if r["fixed"] or r["severity"] in ("Critical", "High")][:80]
        if shown:
            out += ["| Severity | Vulnerability | Package | Installed | Fixed in |", "|---|---|---|---|---|"]
            out += [f"| {r['severity']} | {r['id']} | {r['package']} ({r['type']}) | {r['version']} | {r['fixed'] or 'no fix yet'} |"
                    for r in shown]
        rest = len(rows) - len(shown)
        if rest > 0:
            out += ["", f"{rest} more, of lower severity and without a fix yet, {'is' if rest == 1 else 'are'} in the "
                    "report (the build's artifacts)."]
    if urgent:
        out += ["", f"**{len(urgent)} critical or high with a fix available:** a fresh build should take in the fix "
                     "for Debian's packages; a Python package needs its newer version in requirements.txt."]
    text = "\n".join(out) + "\n"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 1 if urgent else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
