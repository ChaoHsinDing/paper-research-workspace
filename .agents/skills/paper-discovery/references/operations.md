# Discovery operations

All commands run from the project root, using Python 3.9+ standard library. Global `--root PATH` and `--profile PATH` go before the subcommand. No OpenAI API calls are made.

```sh
python3 scripts/discover.py check
python3 scripts/discover.py search --query 'North Atlantic marine heatwave mixed layer' --days 730 --limit 5
python3 scripts/discover.py scan
python3 scripts/discover.py inbox --limit 5
python3 scripts/discover.py status
python3 scripts/discover.py mark RECORD_ID --status recommended
python3 scripts/discover.py mark RECORD_ID --status read
python3 scripts/discover.py mark RECORD_ID --status dismissed
```

`search` is an on-demand query and does not advance monitor watermarks. `scan` persists source records/checkpoints and a JSON run report; `inbox` returns pending candidates without consuming them. Consult `--help` for current optional bounds. Inspect the returned `report_path` for full coverage, and use returned IDs exactly.

## Profile and state

`discovery/interests.json` holds topic queries, ranking terms, region terms, seed DOIs, enabled sources and request limits. These are not a guarantee of exhaustive recall. Initial scan looks back 30 days; later scans overlap successful checkpoints by 7 days. Crossref broad search, when explicitly enabled, uses deposit/creation chronology; it is disabled in the default profile because broad keyword matches are noisy. The default instead refreshes configured seed DOIs with Crossref. Publication dates remain separate from first discovery. OpenAlex's rolling publication-date search and RSS are supplementary, not an exhaustive update stream. Cursor caps or API restrictions must be disclosed. Provider schema, API access or indexing lag can change coverage.

Private runtime data lives under `discovery/state.sqlite3`, `discovery/cache/`, and `discovery/runs/`. The local library manifest is used to identify papers already owned. Runtime data is excluded from the public export. `OPENALEX_API_KEY` is optional for basic access; a free verified key may be needed for paging. Never echo or save its value in reports. Do not create accounts, send verification emails or purchase access without a separate user request. No key is required for Crossref basic metadata access.

After a script network failure, do not replace the report with a successful claim. In an interactive run, the environment may permit an approved network-enabled retry. In unattended runs, report the blocked source and retain its checkpoint. A manual web search can provide useful extra leads, but it does not certify that the scripted monitor completed.

## Candidate interpretation

Programmatic ranks match words, not scientific merit. A record can lack an abstract. Do not exclude a plausible important record solely because fields are missing. Verify shortlisted records on original publisher or repository pages. Separate an old paper newly indexed, a changed version, and a genuinely new publication. A full-paper claim needs paper-reader's page-level checks; use the original page/figure when layout matters.

## Sources

- Crossref API: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- Incremental dates and caching: https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/
- OpenAlex searching: https://help.openalex.org/api/searching/
- OpenAlex authentication: https://help.openalex.org/api/authentication/
- Scheduled tasks: https://learn.chatgpt.com/docs/automations
