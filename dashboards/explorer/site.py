"""Write the static site: the template pages, the metric definitions, a manifest of the runs and one data file
per run, or self-contained pages with all of it inlined (--inline, for a viewer that serves one file with no
network)."""

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from . import idscan
from .fingerprint import SPEC_PATH

WEB = Path(__file__).resolve().parent / "web"
PAGES = ("index.html", "compare.html")
NAV = re.compile(r'\s*<nav class="views"[^>]*>.*?</nav>', re.S)


def manifest_entry(doc: Dict[str, Any]) -> Dict[str, Any]:
    """What the pages need to list and pick a run without loading its file."""
    run, fp = doc["run"], doc["run"]["fingerprint"]
    return {
        "id": run["id"],
        "file": f"{run['id']}.json",
        "data_set": run["data_set"],
        "families": sorted({c["family"] for c in doc["cells"]}),
        "mixes": sorted({c["mix"] for c in doc["cells"]}),
        "build": {k: run["build"][k] for k in ("repo", "ref", "sha", "time")},
        "started": run["started"],
        "cells": len(doc["cells"]),
        "fingerprint": {
            "hash": fp["hash"],
            "io_threads": fp["io_threads"],
            "cores": fp["cores"],
            "server": fp["server"]["instance_type"],
            "client": fp["client"]["instance_type"],
            "network_path": fp["network_path"],
        },
    }


def _json(d: Any) -> str:
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def _inline_text(text: str) -> str:
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def _inline_page(page: str, docs: List[Dict[str, Any]], metrics: Dict[str, Any]) -> str:
    """One page with its stylesheets, scripts and data inlined; the view switch goes, it links other files."""
    page = NAV.sub("", page)
    for href in re.findall(r'<link rel="stylesheet" href="([\w/.-]+\.css)">', page):
        css = (WEB / href).read_text(encoding="utf-8")
        page = page.replace(
            f'<link rel="stylesheet" href="{href}">', f"<style>\n{css}</style>"
        )
    tag = '<script type="application/json" {}>{}</script>'
    blobs = [tag.format('class="vpb-run"', _inline_text(_json(d))) for d in docs]
    blobs.append(
        tag.format('id="vpb-metrics"', _inline_text(_json({"metrics": metrics})))
    )
    spec = SPEC_PATH.read_text(encoding="utf-8").strip()
    blobs.append(tag.format('id="vpb-fingerprint-fields"', _inline_text(spec)))
    srcs = re.findall(r'<script src="([\w/.-]+\.js)"></script>', page)
    for i, src in enumerate(srcs):
        js = (WEB / src).read_text(encoding="utf-8")
        if "</script" in js:
            raise ValueError(f"{src} must not contain a closing script tag")
        page = page.replace(
            f'<script src="{src}"></script>',
            ("\n".join(blobs) + "\n" if i == 0 else "") + f"<script>\n{js}</script>",
        )
    return page


def write_site(
    docs: List[Dict[str, Any]],
    out: Path,
    metrics: Dict[str, Any],
    inline: bool = False,
) -> List[Path]:
    """docs: vpb-result run files; metrics: the metric definitions. Returns the files written."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if inline:
        for p in PAGES:
            dst = out / p
            dst.write_text(
                _inline_page((WEB / p).read_text(encoding="utf-8"), docs, metrics),
                encoding="utf-8",
            )
            written.append(dst)
        return written
    for src in WEB.rglob("*"):
        if src.is_file():
            dst = out / src.relative_to(WEB)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            written.append(dst)
    shutil.copyfile(SPEC_PATH, out / SPEC_PATH.name)
    written.append(out / SPEC_PATH.name)
    (out / "metrics.json").write_text(
        json.dumps({"schema": "vpb-metrics", "metrics": metrics}, indent=1) + "\n"
    )
    written.append(out / "metrics.json")
    data = out / "data"
    data.mkdir(exist_ok=True)
    for d in docs:
        dst = data / f"{d['run']['id']}.json"
        dst.write_text(_json(d) + "\n", encoding="utf-8")
        written.append(dst)
    manifest = {"runs": [manifest_entry(d) for d in docs]}
    (data / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    written.append(data / "manifest.json")
    return written


def scan(paths: List[Path]) -> List[str]:
    return [hit for p in paths for hit in idscan.scan(p)]
