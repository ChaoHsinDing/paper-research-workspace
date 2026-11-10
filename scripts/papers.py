#!/usr/bin/env python3
"""Local, page-addressable paper library. No model calls or network access."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_VERSION = 1
SKIP_DIRS = {".git", ".venv", "library", "__pycache__", "node_modules"}
# Generated deliverables and their scratch files must not become research sources.
GENERATED_ROOTS = {"output", "tmp"}


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize(text):
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path leaves library root: {relative}")
    return path


def pdf_files(root):
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() == ".pdf"
                  and p.relative_to(root).parts[0] not in GENERATED_ROOTS
                  and not any(x in SKIP_DIRS for x in p.relative_to(root).parts)
                  and p.resolve().is_relative_to(root.resolve()))


def manifest(root):
    result = read_json(root / "library/manifest.json")
    if result is None:
        raise ValueError("Index missing. Run: python3 scripts/papers.py index")
    return result


def pick(data, selector):
    exact = [p for p in data["papers"] if selector == p["id"]]
    candidates = exact or [p for p in data["papers"] if selector.casefold() in
                           (p["id"] + " " + p["title"] + " " + " ".join(p["source_paths"])).casefold()]
    if len(candidates) != 1:
        raise ValueError(f"Paper selector matched {len(candidates)} papers; use an ID from list.")
    return candidates[0]


def page_text(root, paper, number):
    if not 1 <= number <= paper["page_count"]:
        raise ValueError(f"PDF page must be 1..{paper['page_count']}")
    page = paper["pages"][number - 1]
    path = safe_path(root, page["text_path"])
    if not path.is_file() or digest(path) != page["text_sha256"]:
        raise ValueError(f"Missing/modified extracted page: {page['text_path']}. Run index to repair.")
    return path.read_text(encoding="utf-8")


def source_path(root, paper):
    for rel in paper["source_paths"]:
        path = safe_path(root, rel)
        if path.is_file() and digest(path) == paper["sha256"]:
            return path
    raise ValueError(f"Original PDF missing or changed for {paper['id']}; run index before citing.")


def freshness(root, data):
    current = {p.relative_to(root).as_posix(): digest(p) for p in pdf_files(root)}
    expected = {rel: p["sha256"] for p in data["papers"] for rel in p["source_paths"]}
    return {
        "new": sorted(current.keys() - expected.keys()),
        "missing": sorted(expected.keys() - current.keys()),
        "changed": sorted(k for k in current.keys() & expected.keys() if current[k] != expected[k]),
    }


def require_fresh(root, data):
    result = freshness(root, data)
    if any(result.values()):
        raise ValueError("Index out of date; run index before answering: " + json.dumps(result, ensure_ascii=False))


def pypdf_import():
    if importlib.util.find_spec("pypdf"):
        import pypdf
        return pypdf
    candidates = [ROOT / ".venv/bin/python", ROOT / ".venv/Scripts/python.exe",
                  Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"]
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve() != Path(sys.executable).resolve():
            check = subprocess.run([str(candidate), "-c", "import pypdf"], capture_output=True)
            if check.returncode == 0:
                os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise ValueError("PDF extractor missing. Run: python3 scripts/bootstrap.py")


def build_index(root, force=False):
    pypdf = pypdf_import()
    previous = read_json(root / "library/manifest.json", {"papers": []})
    old = {p["sha256"]: p for p in previous["papers"]}
    annotations = read_json(root / "library/annotations.json", {})
    papers, by_hash = [], {}
    stats = {"extracted": 0, "reused": 0, "duplicate_files": 0}
    for source in pdf_files(root):
        sha = digest(source)
        rel = source.relative_to(root).as_posix()
        if sha in by_hash:
            by_hash[sha]["source_paths"].append(rel)
            stats["duplicate_files"] += 1
            continue
        paper_id = "P" + sha[:12]
        prior = old.get(sha)
        reusable = prior and not force and prior.get("extraction_version") == EXTRACTION_VERSION
        if reusable:
            reusable = all((root / p["text_path"]).is_file() and digest(root / p["text_path"]) == p["text_sha256"]
                           for p in prior["pages"])
        if reusable:
            paper = dict(prior)
            stats["reused"] += 1
        else:
            reader = pypdf.PdfReader(source)
            raw_meta = reader.metadata or {}
            pages = []
            has_labels = "/PageLabels" in reader.trailer["/Root"]
            labels = reader.page_labels if has_labels else [None] * len(reader.pages)
            first = ""
            for n, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
                if n == 1:
                    first = text
                text_rel = f"library/pages/{paper_id}/p{n:04d}.txt"
                atomic_text(root / text_rel, text)
                pages.append({"pdf_page": n, "pdf_page_label": labels[n - 1],
                              "text_path": text_rel, "text_sha256": digest(root / text_rel),
                              "characters": len(text.strip()), "lines": len(text.splitlines()),
                              "needs_visual_or_ocr": len(re.sub(r"\s", "", text)) < 80})
            doi = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", first, re.I)
            paper = {"id": paper_id, "sha256": sha,
                     "title": normalize(str(raw_meta.get("/Title", ""))) or source.stem,
                     "doi": doi.group(0).rstrip(".,;)") if doi else None,
                     "authors": [], "year": None,
                     "metadata_status": "automatic; verify against first page",
                     "page_count": len(pages), "pages": pages,
                     "extractor": f"pypdf {pypdf.__version__} plain text",
                     "extraction_version": EXTRACTION_VERSION}
            stats["extracted"] += 1
        paper["source_paths"] = [rel]
        if paper_id in annotations:
            annotation = annotations[paper_id]
            if annotation.get("sha256") != sha:
                raise ValueError(f"Annotation hash mismatch: {paper_id}")
            for key in ("title", "authors", "year", "doi", "publication_status", "topics"):
                if key in annotation:
                    paper[key] = annotation[key]
            paper["metadata_status"] = "curated from PDF; see reading card"
        papers.append(paper)
        by_hash[sha] = paper
    if not papers:
        raise ValueError("No PDFs found below the workspace root.")
    data = {"schema_version": 1, "extraction_version": EXTRACTION_VERSION,
            "page_numbering": "1-based PDF page index; PDF page labels are not verified printed page numbers",
            "papers": papers}
    write_json(root / "library/manifest.json", data)
    rebuild_database(root, data)
    write_catalog(root, data)
    print(json.dumps({**stats, "papers": len(papers), "pages": sum(p["page_count"] for p in papers)}, ensure_ascii=False))


def rebuild_database(root, data):
    path = root / "library/search.sqlite3"
    temporary = path.with_suffix(".building.sqlite3")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute("CREATE VIRTUAL TABLE page_search USING fts5(paper_id UNINDEXED, pdf_page UNINDEXED, title, text, tokenize='unicode61 remove_diacritics 2')")
            for paper in data["papers"]:
                for page in paper["pages"]:
                    text = page_text(root, paper, page["pdf_page"])
                    db.execute("INSERT INTO page_search VALUES (?,?,?,?)",
                               (paper["id"], page["pdf_page"], paper["title"], normalize(text)))
            db.execute("CREATE TABLE state (manifest_sha TEXT NOT NULL)")
            db.execute("INSERT INTO state VALUES (?)", (digest(root / "library/manifest.json"),))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_catalog(root, data):
    rows = ["# 論文目錄", "", "頁數為 PDF 檔案頁序。閱讀卡是檢索入口；回答前仍須回讀原文。", "",
            "| ID | 論文 | 年份 | PDF 頁數 | 閱讀卡 |", "|---|---|---:|---:|---|"]
    for paper in data["papers"]:
        source = quote("../" + paper["source_paths"][0], safe="/")
        title = paper["title"].replace("|", "\\|")
        card = f"[閱讀卡](cards/{paper['id']}.md)" if (root / f"library/cards/{paper['id']}.md").exists() else "待建立"
        rows.append(f"| {paper['id']} | [{title}]({source}) | {paper.get('year') or '待核對'} | {paper['page_count']} | {card} |")
    rows.extend(["", "抽取文字保存於 `pages/`；`manifest.json` 記錄原文及逐頁文字雜湊。", ""])
    atomic_text(root / "library/catalog.md", "\n".join(rows))


def database(root, data):
    path = root / "library/search.sqlite3"
    rebuild = not path.exists()
    if not rebuild:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                rebuild = db.execute("SELECT manifest_sha FROM state").fetchone()[0] != digest(root / "library/manifest.json")
        except (sqlite3.Error, TypeError):
            rebuild = True
    if rebuild:
        rebuild_database(root, data)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def search(root, args):
    data = manifest(root)
    require_fresh(root, data)
    terms = re.findall(r"\w+", normalize(args.query), re.UNICODE)
    if not terms:
        raise ValueError("Provide at least one search term.")
    if args.phrase:
        query = '"' + " ".join(terms) + '"'
    else:
        query = (" OR " if args.any else " AND ").join('"' + t + '"' for t in terms)
    selected = pick(data, args.paper) if args.paper else None
    sql = "SELECT paper_id, pdf_page, snippet(page_search, 3, '[', ']', ' … ', 55), bm25(page_search,0,0,0.5,1) FROM page_search WHERE page_search MATCH ?"
    params = [query]
    if selected:
        sql += " AND paper_id = ?"
        params.append(selected["id"])
    sql += " ORDER BY bm25(page_search,0,0,0.5,1) LIMIT ?"
    params.append(args.limit)
    by_id = {p["id"]: p for p in data["papers"]}
    with database(root, data) as db:
        hits = db.execute(sql, params).fetchall()
    results = []
    for paper_id, number, excerpt, score in hits:
        paper = by_id[paper_id]
        page_text(root, paper, int(number))
        results.append({"paper_id": paper_id, "title": paper["title"], "pdf_page": int(number),
                        "source": str(root / paper["source_paths"][0]),
                        "text_file": str(root / paper["pages"][int(number) - 1]["text_path"]),
                        "snippet": excerpt})
    print(json.dumps({"query": args.query, "match": "phrase" if args.phrase else "any" if args.any else "all",
                      "hits": results, "note": "Snippets are discovery only. Read complete relevant pages before answering."}, ensure_ascii=False, indent=2))


def show(root, args):
    paper = pick(manifest(root), args.paper)
    source = source_path(root, paper)
    text = page_text(root, paper, args.page)
    print(f"{paper['id']} | {paper['title']}\nPDF page {args.page}/{paper['page_count']}\nSource: {source}\nText: {root / paper['pages'][args.page - 1]['text_path']}\n")
    for n, line in enumerate(text.splitlines(), 1):
        print(f"{n:04d}  {line}")


def locate_quote(root, paper, evidence):
    text = page_text(root, paper, int(evidence["pdf_page"]))
    quote_text = evidence.get("quote", "").strip()
    if len(quote_text) < 10:
        raise ValueError("Evidence quote is too short to identify reliably.")
    if normalize(quote_text) not in normalize(text):
        raise ValueError(f"Quote not found: {paper['id']} p{evidence['pdf_page']}: {quote_text[:100]}")
    return True


def validate_annotations(root, data):
    annotations = read_json(root / "library/annotations.json", {})
    errors, checked, used = [], 0, set()
    for paper in data["papers"]:
        annotation = annotations.get(paper["id"])
        if annotation is None:
            continue
        used.add(paper["id"])
        if annotation.get("sha256") != paper["sha256"]:
            errors.append(f"Annotation hash mismatch: {paper['id']}")
        for claim in annotation.get("claims", []):
            if not claim.get("evidence"):
                errors.append(f"Claim lacks evidence: {paper['id']}")
            for evidence in claim.get("evidence", []):
                try:
                    locate_quote(root, paper, evidence)
                    checked += 1
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append(str(exc))
    return errors, checked, sorted(set(annotations) - used)


def cards(root):
    data = manifest(root)
    require_fresh(root, data)
    errors, checked, orphaned = validate_annotations(root, data)
    if errors:
        raise ValueError("\n".join(errors))
    annotations = read_json(root / "library/annotations.json", {})
    for paper in data["papers"]:
        annotation = annotations.get(paper["id"])
        if annotation is None:
            continue
        source = quote("../../" + paper["source_paths"][0], safe="/")
        lines = [f"# {paper['title']}", "", f"- ID：`{paper['id']}`",
                 f"- 作者：{'; '.join(paper.get('authors', []))}", f"- 年份：{paper.get('year') or '待核對'}",
                 f"- 版本：{paper.get('publication_status', '請核對原文')}",
                 f"- 原文：[PDF]({source})（{paper['page_count']} 頁）",
                 f"- SHA-256：`{paper['sha256']}`",
                 f"- DOI：{paper.get('doi') or '未確認'}", "",
                 "本卡為初步閱讀導覽，不能代替原文。摘錄位置已經程式核對；語意與圖表仍須依問題回查。", ""]
        for claim in annotation.get("claims", []):
            lines.extend([f"## {claim['kind']}", "", claim["text"], ""])
            for ev in claim["evidence"]:
                n = ev["pdf_page"]
                link = f"../pages/{paper['id']}/p{n:04d}.txt"
                lines.extend([f"來源：[PDF 第 {n} 頁抽取文字]({link})；{ev.get('section', '請回查頁面')}。", "",
                              "> " + normalize(ev["quote"]), ""])
        lines.extend(["## 閱讀範圍", "", annotation.get("coverage_note", "初步導讀；完整方法、限制與圖表應按提問回讀。"), ""])
        atomic_text(root / f"library/cards/{paper['id']}.md", "\n".join(lines))
    write_catalog(root, data)
    print(json.dumps({"cards": len(set(annotations) & {p['id'] for p in data['papers']}),
                      "verified_quotes": checked, "inactive_annotations": orphaned}, ensure_ascii=False))


def check(root):
    data = manifest(root)
    state = freshness(root, data)
    errors = []
    for paper in data["papers"]:
        for page in paper["pages"]:
            try:
                page_text(root, paper, page["pdf_page"])
            except ValueError as exc:
                errors.append(str(exc))
    annotation_errors, quotes, orphaned = validate_annotations(root, data)
    errors.extend(annotation_errors)
    result = {"papers": len(data["papers"]), "pages": sum(p["page_count"] for p in data["papers"]),
              "freshness": state, "verified_quotes": quotes, "errors": errors,
              "inactive_annotations": orphaned,
              "pages_needing_visual_or_ocr": [{"paper_id": p["id"], "pdf_page": page["pdf_page"]}
                                            for p in data["papers"] for page in p["pages"] if page["needs_visual_or_ocr"]]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors or any(state.values()) else 0


def render(root, args):
    paper = pick(manifest(root), args.paper)
    source = source_path(root, paper)
    page_text(root, paper, args.page)
    binary = shutil.which("pdftoppm")
    if binary is None:
        raise ValueError("Rendering requires Poppler (pdftoppm). Use the Codex bundled runtime or install Poppler.")
    output = root / f"library/previews/{paper['id']}-p{args.page:04d}"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([binary, "-f", str(args.page), "-l", str(args.page), "-singlefile", "-scale-to", "2000", "-png", str(source), str(output)], check=True)
    print(str(output.with_suffix(".png")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Library root (default: script's project root)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index", help="Incrementally extract PDFs, rebuild search and catalog")
    p.add_argument("--force", action="store_true")
    sub.add_parser("list", help="Print compact paper catalog")
    sub.add_parser("check", help="Verify originals, pages, and reading-card quotes")
    sub.add_parser("cards", help="Validate annotations and generate reading cards")
    p = sub.add_parser("search", help="Search English keywords; translate Chinese concepts before searching")
    p.add_argument("query")
    p.add_argument("--paper")
    p.add_argument("--limit", type=int, default=8)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--any", action="store_true", help="Any query token; default requires all tokens")
    group.add_argument("--phrase", action="store_true")
    for command in ("show", "render"):
        p = sub.add_parser(command)
        p.add_argument("paper")
        p.add_argument("page", type=int, help="1-based PDF page index")
    p = sub.add_parser("verify-quote", help="Check a quote against an indexed page")
    p.add_argument("paper")
    p.add_argument("page", type=int)
    p.add_argument("quote")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "index":
            build_index(root, args.force)
        elif args.command == "list":
            for paper in manifest(root)["papers"]:
                print(f"{paper['id']}\t{paper.get('year') or '?'}\t{paper['page_count']} pages\t{paper['title']}")
        elif args.command == "search":
            if not 1 <= args.limit <= 100:
                raise ValueError("limit must be 1..100")
            search(root, args)
        elif args.command == "show":
            show(root, args)
        elif args.command == "render":
            render(root, args)
        elif args.command == "cards":
            cards(root)
        elif args.command == "check":
            return check(root)
        elif args.command == "verify-quote":
            paper = pick(manifest(root), args.paper)
            source_path(root, paper)
            locate_quote(root, paper, {"pdf_page": args.page, "quote": args.quote})
            print("Quote matches indexed source page. This verifies location, not interpretation.")
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
