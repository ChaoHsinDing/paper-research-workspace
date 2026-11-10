"""Integrity regression tests; uses tiny synthetic PDFs, never the user's originals."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import papers
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_pdf(path, lines):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    body = "BT /F1 12 Tf 50 700 Td 16 TL "
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        body += f"({safe}) Tj T* "
    stream.set_data((body + "ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "A synthetic paper"})
    with path.open("wb") as f:
        writer.write(f)


class LibraryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paper-library-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "example.pdf"
        make_pdf(self.source, ["A synthetic paper on ocean heat", "Air-sea heat flux warms a shallow mixed layer.",
                               "The response is conditional on the study region.", "A marine heat-", "wave lasts longer than one day."])
        self.index()

    def tearDown(self):
        self.temp.cleanup()

    def index(self):
        output = io.StringIO()
        with redirect_stdout(output):
            papers.build_index(self.root)
        return json.loads(output.getvalue())

    def paper(self):
        return papers.manifest(self.root)["papers"][0]

    def test_generated_reports_do_not_become_sources(self):
        for folder in ["output/pdf", "tmp/pdfs"]:
            target = self.root / folder / "generated.pdf"
            target.parent.mkdir(parents=True)
            shutil.copyfile(self.source, target)
        # A source collection may legitimately contain a nested folder named output.
        nested_source = self.root / "collection/output/other.pdf"
        nested_source.parent.mkdir(parents=True)
        make_pdf(nested_source, ["Another original paper."])
        self.assertEqual(papers.pdf_files(self.root), sorted([self.source, nested_source]))
        self.index()
        self.assertEqual(len(papers.manifest(self.root)["papers"]), 2)

    def test_incremental_rename_and_duplicate_identity(self):
        original_id = self.paper()["id"]
        original_hash = self.paper()["pages"][0]["text_sha256"]
        self.assertEqual(self.index()["reused"], 1)
        renamed = self.root / "renamed.pdf"
        self.source.rename(renamed)
        shutil.copyfile(renamed, self.root / "duplicate.pdf")
        result = self.index()
        self.assertEqual(result["extracted"], 0)
        self.assertEqual(result["duplicate_files"], 1)
        self.assertEqual(self.paper()["id"], original_id)
        self.assertEqual(self.paper()["pages"][0]["text_sha256"], original_hash)
        self.assertEqual(len(self.paper()["source_paths"]), 2)

    def test_source_change_blocks_stale_citations_and_search(self):
        old = self.paper()
        make_pdf(self.source, ["A different scientific conclusion."])
        with self.assertRaises(ValueError):
            papers.source_path(self.root, old)
        with self.assertRaises(ValueError):
            papers.require_fresh(self.root, papers.manifest(self.root))
        self.index()
        self.assertNotEqual(self.paper()["id"], old["id"])

    def test_corrupt_page_is_detected_and_repaired(self):
        page = self.paper()["pages"][0]
        (self.root / page["text_path"]).write_text("Incorrect substituted text")
        with self.assertRaises(ValueError):
            papers.page_text(self.root, self.paper(), 1)
        self.assertEqual(self.index()["extracted"], 1)
        self.assertIn("shallow mixed layer", papers.page_text(self.root, self.paper(), 1))

    def test_exact_quotes_and_invalid_pages(self):
        paper = self.paper()
        self.assertTrue(papers.locate_quote(self.root, paper, {"pdf_page": 1, "quote": "A marine heatwave lasts longer than one day."}))
        with self.assertRaises(ValueError):
            papers.locate_quote(self.root, paper, {"pdf_page": 1, "quote": "The paper proves all heatwaves are caused by ships."})
        for page in [0, 2]:
            with self.assertRaises(ValueError):
                papers.page_text(self.root, paper, page)

    def test_search_rebuilds_missing_database(self):
        (self.root / "library/search.sqlite3").unlink()
        args = SimpleNamespace(query="marine heatwave", phrase=True, any=False, paper=None, limit=5)
        out = io.StringIO()
        with redirect_stdout(out):
            papers.search(self.root, args)
        result = json.loads(out.getvalue())
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["pdf_page"], 1)
        args.query = "neverpresent"
        out = io.StringIO()
        with redirect_stdout(out):
            papers.search(self.root, args)
        self.assertEqual(json.loads(out.getvalue())["hits"], [])


if __name__ == "__main__":
    unittest.main()
