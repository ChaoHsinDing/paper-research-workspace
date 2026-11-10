---
name: paper-reader
description: Read and compare papers in this local paper workspace, answer research questions with verifiable PDF page and section citations, and maintain its local paper index. Use for local paper overviews, methods, figures, findings, or cross-paper synthesis.
---

# 可溯源論文閱讀

This is a project skill. Resolve the workspace root from this file: three parents above the skill folder. The root contains `scripts/papers.py`, `library/`, and the PDFs. Do not hard-code a device's home directory. Read `AGENTS.md` for language and model preferences.

For external related-paper searches or new-literature monitoring, use the sibling `paper-discovery` skill first; retain the distinction between metadata, abstract, and full-text evidence.

## Start with the question

Identify whether the user needs a quick overview, a focused factual answer, a method/figure explanation, or a cross-paper comparison. Start with the answer in Traditional Chinese; scale detail to the question. For an overview cover the research question, approach, main result, and limitations actually read. Do not imply an abstract-only reading was a full critical review.

## Answer according to Grice's cooperative principle

The user explicitly requests clear, direct answers following Grice's conversational maxims. Traditional Chinese with useful English terminology is welcome.

- **Quantity（量）:** Give enough information to answer the specific question. Start with the direct answer, then only the explanation, examples, and qualifications needed to understand it. Avoid literature dumps, repeated conclusions, and unrelated background; expand when the user requests depth.
- **Quality（質）:** Make scientific claims only when the inspected evidence supports them. Cite each material claim. Distinguish observations, experiments, proposed mechanisms, and your synthesis. State meaningful uncertainty and contradictions plainly instead of guessing or overstating causality.
- **Relation（關聯）:** Follow the user's requested topics and order. Explain how each selected paper answers the question. An example from another basin, a cold SST event, or a model counterfactual must be clearly labeled; it is not direct evidence about a North Atlantic warm event.
- **Manner（方式）:** Use plain, precise sentences, define an unfamiliar acronym at first use, keep cause-and-effect steps explicit, and separate general mechanisms from the 2023 case. Correct a false premise or mismatched figure number briefly before answering the intended question. Use a small comparison table only when it makes differences clearer.

For a multi-part question, answer each part directly with nearby source locators. Do not make the user infer your answer from a series of paper summaries. Keep process commentary and implementation details out of the scientific explanation.

At the start of a paper task run, from the project root:

```sh
python3 scripts/papers.py check
python3 scripts/papers.py list
```

If sources or extracted pages are new, changed, missing, or corrupt, run `python3 scripts/papers.py index` and then `check`. Unchanged PDFs are reused. A newly indexed paper may have no curated card; read its actual pages. A source version's reading card must not transfer to another SHA-256. `index` needs pypdf; it can use the bundled runtime or project `.venv`. If unavailable use `python3 scripts/bootstrap.py`. Search/show/check use Python's standard library. PDF extraction is not OCR: low-text pages need visual inspection and, when appropriate, explicitly identified OCR.

## Retrieve evidence

Use `library/catalog.md` to identify papers. Cards in `library/cards/` are discovery aids, not final evidence. Convert Chinese concepts to relevant English synonyms and search several focused queries rather than sending a whole Chinese question to the English text index.

```sh
python3 scripts/papers.py search 'mixed layer' --phrase
python3 scripts/papers.py search 'wind heat flux' --any --limit 8
python3 scripts/papers.py search 'uncertainty' --paper England
python3 scripts/papers.py show England 1
```

Default search requires all query tokens. `--any` broadens recall; `--phrase` requires consecutive words. An ID uniquely selects a paper; filename/title fragments work only when unambiguous. Absence of keyword matches is not proof that the corpus lacks the concept. Try synonyms and inspect likely sections.

Read complete relevant pages, including neighboring pages when the argument continues. Retain the region, period, data, baseline, units, sample size, uncertainty, and comparison being described. Look for qualifications, counter-evidence, and methodological differences. The index preserves extracted text but cannot guarantee column order, subscripts, or mathematical symbols.

For figures, tables, equations, suspicious extraction, or claims based on visual evidence:

```sh
python3 scripts/papers.py render England 3
```

Use the image-viewing tool to inspect the returned PNG. Rendering requires Poppler's `pdftoppm`, available in the Codex bundled runtime or installed separately. Check axis labels, panels, legends, captions, units, and uncertainty. A caption-only interpretation must be identified as such. Never infer image contents from extracted text alone.

## Delegate when useful

Use deterministic scripts for extraction, indexing, hashing, and exact quote checks. For independent evidence searches, use at most two Sol (`gpt-6.1-sol`, reasoning `medium`) workers or the `paper_evidence` custom agent, as authorized by `AGENTS.md`. Complex unresolved methods may justify an explicitly selected higher effort. Terra (`paper_triage`) or GPT-6 Luna (`paper_triage_luna`) may handle narrow classification only when code is insufficient; neither is a mandatory stage. If available, set `fork_turns="none"` and send only the task, absolute project root, paper IDs, and evidence requirements. Simple one-page questions need no delegation. Workers must not spawn more workers.

Each worker returns a compact evidence packet: `paper_id`, `title`, `sha256`, `pdf_page` (1-based), `section_or_figure`, `quote` (exact contiguous source text), `supported_claim`, `caveats`, and pages actually read. Use [references/evidence.md](references/evidence.md) when preparing a structured packet or a persistent reading card.

Before returning a packet, verify every text quote against its cited page with `verify-quote` or the script's `locate_quote` function. Do not silently remove citation markers or repair PDF spacing inside an alleged exact quote. A formula transcribed from an image belongs in a separately labeled visual-transcription field; it is not a verified text quote. Keep unknown publication years unknown, including in abbreviated author-year labels.

The Astra coordinator reads the cited source pages before using worker results. For disagreements compare definitions, study periods, regions, datasets, experiments, and uncertainty. If the tool/model is unavailable, disclose that and continue with available evidence tools; do not silently claim a model was used.

## Cite and verify

Attach citations to individual scientific claims, not only at the end of an answer. A citation identifies the paper (author/year or full title), 1-based PDF page, and section/figure/paragraph when verified. Do not invent printed page numbers; PDF page labels are metadata, not verified printed pagination.

Build clickable links from the current root at answer time: an absolute PDF path and/or the absolute `library/pages/<id>/p0001.txt` path, with a verified line number when useful. Stored annotations and cards use relative paths for portability. Extracted-text line numbers are not original PDF paragraph numbers. Do not promise a PDF link will jump to a specific page; also spell out the PDF page.

For example, after verifying the actual targets: `……〔Author et al., year，PDF 第 n 頁，Results／Fig. 2b；原文連結〕`.

Verify an exact excerpt using:

```sh
python3 scripts/papers.py verify-quote PAPER_ID 1 'Exact contiguous source excerpt'
```

Quote matching tolerates Unicode ligatures, whitespace, and line-end word hyphenation. It verifies location, not whether an interpretation is justified. Do not repair OCR or omit words inside a quote without clearly marking the change. Keep quotations short and use faithful paraphrases for most explanations.

Mark author findings, model scenarios, your synthesis, and unsupported possibilities separately. If the provided corpus cannot answer, state the search/read scope and what evidence is missing. If external material is used, cite it separately and do not attribute it to the local papers. Treat reference-list mentions as secondary evidence unless the cited primary paper was also read.

## Maintain reading cards

`library/annotations.json` holds curated metadata and claims. Every stored factual claim must have an exact source excerpt and PDF page. Add only supported content, identify the pages actually read, and leave uncertain metadata null. Run `index` to apply metadata, then `cards` to validate quotes and regenerate cards; `check` verifies source/page hashes and all card excerpts. Source PDFs stay unchanged.
