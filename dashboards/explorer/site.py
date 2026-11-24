"""Write the static site: the template pages plus the data they load, or self-contained pages with the data
inlined (--inline, for a viewer that serves one file with no network)."""

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import idscan
from .fingerprint import SPEC_PATH

WEB = Path(__file__).resolve().parent / "web"
PAGES = ("index.html", "compare.html")
NAV = re.compile(r'\s*<nav class="views"[^>]*>.*?</nav>', re.S)


def _inline_text(text: str) -> str:
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def _inline_page(page: str, docs: List[Tuple[str, Dict[str, Any]]]) -> str:
    """One page with its stylesheets, scripts and data inlined; the view switch goes, it links other files."""
    page = NAV.sub("", page)
    for href in re.findall(r'<link rel="stylesheet" href="([\w/.-]+\.css)">', page):
        css = (WEB / href).read_text(encoding="utf-8")
        page = page.replace(
            f'<link rel="stylesheet" href="{href}">', f"<style>\n{css}</style>"
        )
    blobs = [
        f'<script type="application/json" class="vpb-data">{_inline_text(json.dumps(d, separators=(",", ":"), ensure_ascii=False))}</script>'
        for _, d in docs
    ]
    spec = SPEC_PATH.read_text(encoding="utf-8").strip()
    blobs.append(
        f'<script type="application/json" id="vpb-fingerprint-fields">{_inline_text(spec)}</script>'
    )
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
    docs: List[Tuple[str, Dict[str, Any]]], out: Path, inline: bool = False
) -> List[Path]:
    """docs: (file name, vpb-result/1 document). Returns the files written."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if inline:
        for p in PAGES:
            dst = out / p
            dst.write_text(
                _inline_page((WEB / p).read_text(encoding="utf-8"), docs),
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
    data = out / "data"
    data.mkdir(exist_ok=True)
    names = []
    for name, d in docs:
        (data / name).write_text(
            json.dumps(d, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        names.append(name)
        written.append(data / name)
    (data / "manifest.json").write_text(json.dumps({"files": names}, indent=2) + "\n")
    written.append(data / "manifest.json")
    return written


def scan(paths: List[Path]) -> List[str]:
    return [hit for p in paths for hit in idscan.scan(p)]
