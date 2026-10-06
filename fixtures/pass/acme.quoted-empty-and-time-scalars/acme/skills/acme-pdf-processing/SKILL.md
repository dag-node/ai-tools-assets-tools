---
name: acme-pdf-processing
description: Extract text and tables from PDF files and fill PDF forms. Use when a task reads or edits a PDF.
compatibility: Requires python3.
metadata:
  ai-tools-libs: ""
  ai-tools-reviewed: "2026-10-06"
  ai-tools-span: "1:20"
---

# PDF processing

Run `python3 scripts/extract.py <file>` to extract text, and
`dotnet run scripts/report.cs -- <file>` for a table report. Formats are
listed in `references/formats.md`.
