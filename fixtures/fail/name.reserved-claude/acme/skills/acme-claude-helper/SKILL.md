---
name: acme-claude-helper
description: Extract text and tables from PDF files and fill PDF forms. Use when a task reads or edits a PDF.
compatibility: Requires python3.
metadata:
  ai-tools-libs: python/pdftext
---

# PDF processing

Run `python3 scripts/extract.py <file>` to extract text, and
`dotnet run scripts/report.cs -- <file>` for a table report. Formats are
listed in `references/formats.md`.
