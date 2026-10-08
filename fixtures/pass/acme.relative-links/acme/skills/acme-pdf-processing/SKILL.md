---
name: acme-pdf-processing
description: Extract text and tables from PDF files and fill PDF forms. Use when a task reads or edits a PDF.
compatibility: Requires python3.
metadata:
  ai-tools-libs: python/pdftext
---

# PDF processing

Run `python3 scripts/extract.py <file>` to extract text, and
`dotnet run scripts/report.cs -- <file>` for a table report. Formats are
listed in `references/formats.md`.

The formats are [listed](references/formats.md#pdf) and [specified](https://pdfa.org/).
Back to [the top](#pdf-processing); a link to another file reads `[x](../other/SKILL.md)`.

[formats]: ./references/formats.md

```markdown
[y](../other/SKILL.md)
```
