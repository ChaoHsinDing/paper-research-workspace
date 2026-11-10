# Evidence and reading-card contracts

An evidence packet is a JSON object with:

- `paper_id`: ID from manifest, stable for the exact PDF bytes.
- `sha256`: full source hash from manifest.
- `title`: verified paper title.
- `pdf_page`: 1-based PDF file page index.
- `section_or_figure`: verified section or figure label; use null if unknown.
- `quote`: exact contiguous excerpt from that page. Preserve numbers and qualifications.
- `supported_claim`: concise faithful paraphrase, with observations distinguished from simulations.
- `caveats`: qualifications, indirect/secondary evidence, incomplete extraction, or unread material.
- `pages_read`: actual page indexes inspected, and whether images were viewed.

For persistent cards, `library/annotations.json` is an object keyed by paper ID. Each value contains:

```json
{
  "sha256": "full source hash",
  "title": "verified original title",
  "authors": ["verified author name", "et al."],
  "year": null,
  "doi": null,
  "publication_status": "status verified from the PDF",
  "topics": ["English retrieval term"],
  "claims": [
    {
      "kind": "主要發現",
      "text": "繁體中文概述，保留研究條件。",
      "evidence": [
        {
          "pdf_page": 1,
          "section": "Abstract",
          "quote": "Exact contiguous excerpt from this page."
        }
      ]
    }
  ],
  "coverage_note": "State which pages/sections were read and which remain unread."
}
```

Do not use an acceptance date as a verified publication date. In-press papers may use their clearly documented article year, with status explicit. A DOI year alone is only a clue. Use `null` when uncertain. Do not invent limitations just to fill a template. An abstract-only card should say so and omit claims about methods/limitations not supported by the abstract.

Run `python3 scripts/papers.py index`, `python3 scripts/papers.py cards`, then `python3 scripts/papers.py check`. These checks establish file/version identity and excerpt location; the coordinator still evaluates whether the claim follows from its context.
