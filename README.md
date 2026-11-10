# Paper Research Workspace

Open this folder in the Codex desktop app to discover new papers and read local PDFs within the same project.

| Skill | Purpose |
| --- | --- |
| `paper-discovery` | Find and screen papers based on your research interests, recording bibliographic details, sources, and access options. |
| `paper-reader` | Index local PDFs, inspect complete pages, and support paper explanations and comparisons with page numbers and figure references. |

Both skills look for settings and scripts relative to the project root. After extracting or cloning the project, preserve the `.agents/`, `.codex/`, `discovery/`, and `scripts/` directory structure. The package includes the files needed to run the workflows; each user builds their own paper library and page-level text index locally.

## Set up the workspace

1. Install Python 3.9 or later. Open the project root in Codex and, when required, trust the project to load its model settings. Model availability depends on your account and runtime.
2. Place PDFs you have access to inside the project, for example in `papers/`. Run the following commands to install the extraction dependency, build the index, and verify the files. The initial installation requires an internet connection. Rendering figures requires Poppler's `pdftoppm`; use the version bundled with Codex or install it separately.

   ```sh
   python3 scripts/bootstrap.py
   python3 scripts/papers.py index
   python3 scripts/papers.py check
   ```

3. Adjust `discovery/interests.json` as needed, then run `python3 scripts/discover.py check` to validate the settings. Start a new chat and try these prompts:

   - "Use `$paper-discovery` to find papers published in the past 30 days on mixed-layer mechanisms of North Atlantic marine heatwaves. Include each paper's DOI, publication status, and the evidence you verified."
   - "Use `$paper-reader` to explain the local paper [paper title]. Cite the PDF page number and section or figure for each main finding."

On Windows, you can use `py -3` instead of `python3`. Search results and indexes are stored locally and can be regenerated as needed. If `discovery/weekly-prompt.txt` is included, use it to configure a weekly search in your own Codex account.

Literature discovery requires access to the public APIs and RSS feeds configured in the project. The project settings enable network access in Codex's workspace sandbox, but device, organization, and runtime policies still apply. Search reports identify any sources that fail.

## Share or rebuild the package

Run `python3 scripts/package_research.py` from the project root. The exported folder and ZIP archive are saved under `output/packages/`. To publish this template to your own repository, follow the [GitHub upload guide](docs/GITHUB.md).
