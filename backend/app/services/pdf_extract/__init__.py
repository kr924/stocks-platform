"""
Quarterly-results extraction from a filed PDF.

Vendored from the pipeline worked out separately and documented in
`PIPELINE.html` alongside it. Two files, kept as close to upstream as possible
so a fix made there can be dropped straight back in — the only edit is turning
`import ocr_words` into a relative import now that they live in a package.

    quarterly_extract.extract(path, symbol, ocr_fallback)  the cascade
    ocr_words                                              renders a page and
                                                           returns words shaped
                                                           like PyMuPDF's

The cascade, each stage tried only when the one before fails validation:

    A   PDF text layer      PyMuPDF word coordinates      ~0.1s   ~65% of files
    B1  Tesseract           rendered at 150 DPI           ~1.2s
    B2  RapidOCR            rendered at 150 DPI           ~6.9s   better on
                                                                 ruled tables

Measured over 1,926 filings: a clean read is right about 94% of the time with a
median error of 0.01%; a flagged read is right about 67% of the time. Only
about a quarter of filings yield a clean read at all, and that ceiling is input
quality — dead links, scans, corrupted text layers — not the parser.

**Which is why this is the check and not the source.** Screener carries every
quarter already normalised, so the platform's figures come from there and the
filing is read independently to corroborate them. Agreement between two sources
that share no code is evidence; a single source repeated is not.

Deliberately not a vision-language model: on financial data a VLM will emit a
plausible, wrong number with complete confidence and no way to detect it. Words
with bounding boxes is all this needs, and OCR gives that deterministically.
"""
