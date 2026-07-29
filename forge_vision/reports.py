"""Site report export (FR-API-009, Appendix B outline).

The report is deliberately plain Markdown: readable without the application,
diffable, and easy to paste into a lab notebook. It follows the spec's
standard experiment report outline and refuses to state more than the
measurements support — limitations and alternative explanations are sections,
not footnotes.
"""

from __future__ import annotations

import time


def _fmt_conf(c: dict) -> str:
    return (f"lateral {c['lateral_position']} / depth {c['depth']} "
            f"→ overall **{c['overall']}**")


def site_report(site: dict, scan_results: list[dict], findings: list[dict],
                software_version: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# Site report — {site['name']}",
        "",
        f"Generated {ts} by Forge Vision {software_version}",
        "",
        "## 1. Objective and question being tested",
        "",
        "Identify subsurface responses that persist across independently "
        "registered scans of this site, and state how well their position and "
        "depth are actually constrained.",
        "",
        "## 2. Site and physical setup",
        "",
        f"- Site id: `{site['site_id']}`",
        f"- Coordinate system: {site['coordinate_system']}",
        f"- Units: {site.get('units', 'meters')}",
        f"- Registered scans: {len(site['scans'])}",
    ]
    if site.get("notes"):
        lines += ["", f"Notes: {site['notes']}"]

    lines += ["", "## 3. Scans and geometry", "",
              "| Scan | Experiment | Origin (x, y) | Heading | Points | Medium |",
              "|---|---|---|---|---|---|"]
    for res in scan_results:
        p = res["placement"]
        med = res.get("medium", {})
        lines.append(
            f"| {p.get('label', '')} | `{res['experiment_id']}` | "
            f"({p['origin_x_m']:.2f}, {p['origin_y_m']:.2f}) m | "
            f"{p['heading_deg']:.0f}° | {res.get('measured_columns', '?')} | "
            f"{med.get('name', '?')} (εr {med.get('epsilon_r', '?')}"
            f"±{med.get('epsilon_r_uncertainty', 0)}) |")

    lines += ["", "## 4. Processing", "",
              "Each B-scan was focused by diffraction-stack migration, which "
              "collapses the hyperbolic signature of a compact reflector to a "
              "point under the assumed propagation velocity. Focused maxima "
              "were transformed into site coordinates using each scan's "
              "registered origin and heading, then clustered across scans.",
              "", "## 5. Findings", ""]

    if not findings:
        lines += ["No responses met the detection threshold in any registered "
                  "scan. This is a statement about this survey's sensitivity, "
                  "not evidence that the subsurface is empty.", ""]
    else:
        lines += ["| # | Position (x, y) | Depth | Depth interval | Scans | "
                  "Contrast | Confidence |", "|---|---|---|---|---|---|---|"]
        for i, f in enumerate(findings, 1):
            lines.append(
                f"| {i} | ({f['site_x_m']:.2f}, {f['site_y_m']:.2f}) m | "
                f"{f['depth_m']:.2f} m | "
                f"{f['depth_interval_m'][0]:.2f}–{f['depth_interval_m'][1]:.2f} m | "
                f"{f['supporting_scans']} | {f['max_contrast_db']:.1f} dB | "
                f"{f['confidence']['overall']} |")
        lines.append("")
        for i, f in enumerate(findings, 1):
            lines += [
                f"### Finding {i} — {f['classification']}", "",
                f"- Position: ({f['site_x_m']:.2f}, {f['site_y_m']:.2f}) m, "
                f"spread across supporting scans {f['position_spread_m']:.2f} m",
                f"- Depth: {f['depth_m']:.2f} m "
                f"(interval {f['depth_interval_m'][0]:.2f}–"
                f"{f['depth_interval_m'][1]:.2f} m under the assumed medium)",
                f"- Supported by {f['supporting_scans']} scan(s), "
                f"{f['observations']} observation(s)",
                f"- Confidence: {_fmt_conf(f['confidence'])}",
                "",
                "Evidence:", ""]
            for e in f["evidence"]:
                lines.append(
                    f"  - `{e['experiment_id']}` ({e['label']}) at "
                    f"{e['scan_x_m']:.2f} m along scan, depth {e['depth_m']:.2f} m, "
                    f"contrast {e['contrast_db']:.1f} dB")
            lines += ["", f"- Observation: {f['epistemic']['observation']}",
                      f"- Inference: {f['epistemic']['inference']}",
                      f"- Unknown: {f['epistemic']['unknown']}", ""]

    lines += [
        "## 6. Alternative explanations and limitations", "",
        "- A response supported by a single scan may be a multipath artefact "
        "or a surface feature; it has not been confirmed from a second "
        "geometry.",
        "- Depth is derived from an assumed relative permittivity. Where that "
        "assumption carries uncertainty, the depth interval above widens "
        "accordingly and the true depth may lie anywhere within it.",
        "- Migration assumes a single homogeneous velocity. Layered or mixed "
        "ground will defocus and mislocate responses.",
        "- Lateral coverage exists only along the scan lines. Nothing is known "
        "about the volume between them.",
        "- No material or object class is determined by these measurements.",
        "",
        "## 7. Recommended next experiment", "",
    ]
    single = [f for f in findings if f["supporting_scans"] < 2]
    if single:
        f = single[0]
        lines.append(
            f"Run a scan crossing ({f['site_x_m']:.2f}, {f['site_y_m']:.2f}) m "
            "roughly perpendicular to the existing line. A response that "
            "persists from a second direction is materially stronger evidence "
            "than a repeat of the same geometry.")
    elif findings:
        lines.append(
            "Constrain depth rather than position: calibrate the propagation "
            "velocity against a target at known depth, or scan at a second "
            "frequency, so the depth interval narrows.")
    else:
        lines.append(
            "Increase sensitivity before concluding anything: verify "
            "calibration, capture a fresh background, and re-scan with finer "
            "position steps.")

    lines += ["", "## 8. Provenance", "",
              f"- Software version: {software_version}",
              f"- Site record: `{site['site_id']}`",
              "- Source experiments: "
              + ", ".join(f"`{r['experiment_id']}`" for r in scan_results),
              "- Every finding above links to the scans and scan positions "
              "that produced it; raw I/Q for each is preserved in its "
              "experiment package.", ""]
    return "\n".join(lines)
