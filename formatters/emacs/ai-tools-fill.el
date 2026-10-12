;;; ai-tools-fill.el --- fill comment prose without ending a line on a tie word  -*- lexical-binding: t -*-
;; SPDX-License-Identifier: AGPL-3.0-only
;;
;; The formatter for the wrap rule the writing standard states: a comment is read as written, so
;; each file kind wraps at its own column, and no comment line ends on an article, a conjunction,
;; a preposition, or a wh-word. Where a line BREAKS is this file's rule alone -- `prose-check.py'
;; measures the width and does not read where a line ends. The column is `fill-column', which
;; `.dir-locals.el' at the repository root sets per mode. Emacs's paragraph filler already knows
;; every language's comment syntax, so the rule is a `fill-nobreak-predicate' hook.
;;
;; Interactive use: load this file, then `M-q' on a comment block. Batch use over whole files:
;; `bash formatters/fill-comments.sh <file>...', which calls `ai-tools-fill-comments-file'.
;;
;; The batch filler is conservative on purpose. It fills a run of consecutive lines carrying the
;; same comment prefix, one space and text, and leaves every other shape as it finds it: a run
;; one of whose lines is indented deeper (an example command), a run drawing a table or a diagram
;; (two lines carrying a vertical rule, or a ` -- ' column, at the same column -- the vertical rule
;; is the reading `formatters/align-tables.py' states, and that tool puts such a table in order),
;; a line inside a string, inside a CDATA or `<pre>' region, or inside a fenced block the comment
;; carries (the commands a header shows), and every line `ai-tools-fill--skip-line' names.
;; A docstring is not a comment and is not read.
;;
;; Three shapes are filled by rules of their own, each keeping its words in order:
;;
;; - A HANGING PARAGRAPH: a doc comment's contract head (`name <args> : text', or `-- text') and
;;   the lines under it indented by one more run of spaces. It is filled as one paragraph whose
;;   first line keeps the head's prefix and whose other lines take the continuation's.
;; - A COLUMN TABLE: rows of a first column, a gap of two spaces or more, and a second column, with
;;   any continuation of a row's second column indented to it. A table of two columns whose rows
;;   were aligned at one column and no longer are -- a first column renamed to another width -- is
;;   re-padded to the column most of its rows sit at, or wider where a first column needs it, and
;;   a row whose last column runs past the fill column is wrapped at that column's indent.
;; - A DECORATED HEADER, `# ── title ───': the trailing rule is shortened to bring the line inside
;;   the fill column, and the line is not wrapped. A rule line with no title stays as it is.

(defconst ai-tools-tie-words
  '("a" "an" "the" "and" "or" "nor" "but" "so" "yet"
    "of" "to" "in" "on" "at" "by" "for" "with" "from" "into" "onto" "upon" "over" "under"
    "above" "below" "between" "among" "through" "during" "before" "after" "about" "against"
    "along" "across" "around" "near" "off" "out" "up" "down" "via" "per" "as"
    "what" "which" "who" "whom" "whose" "that" "when" "where" "why" "how")
  "The words a line does not end on: they tie to the word after them.
ai-tools-base's `msg.lib.sh' keeps the same list as `_AI_TOOLS_MSG_TIES' for its runtime. `formatters/fill-markdown.py'
reads this list at run time, so the two formatters share it.")

(defun ai-tools-no-break-after-tie ()
  "Non-nil when the word before point is a tie word, so the filler does not break here.
A word closing a sentence is not a tie; trailing punctuation around the word is ignored."
  (save-excursion
    (skip-chars-backward " \t")
    (let ((end (point)))
      (skip-chars-backward "^ \t\n")
      (let ((word (buffer-substring-no-properties (point) end)))
        (and (not (string-match-p "[.!?]\\'" word))
             (member (downcase (string-trim-right word "[,;:)\"'`]+")) ai-tools-tie-words))))))

(add-hook 'fill-nobreak-predicate #'ai-tools-no-break-after-tie)

(defconst ai-tools-fill--code-span "\\(`+\\)[^`]*?\\1"
  "An inline code span: a backtick run, content without a backtick, and a matching run.
`formatters/fill-markdown.py' reads the checker's `BACKTICK_SPAN' for the same rule, which elisp
cannot read, so this states it again -- narrower in one way, since a span whose content holds a
backtick run of another length is not matched.")

(defun ai-tools-no-break-in-code-span ()
  "Non-nil when point stands inside a code span, so the filler does not break there.
A span holds a literal a reader searches for whole -- a command line, an owner and mode, a flag
with its operand -- and `grep' finds one only while its span is on one line. A span wider than
the column runs the line over instead, since a wider line is still a line."
  (save-match-data
    (let ((break (point))
          (inside nil))
      (save-excursion
        (goto-char (line-beginning-position))
        (let ((limit (line-end-position)))
          (while (and (not inside)
                      (re-search-forward ai-tools-fill--code-span limit t))
            (when (and (< (match-beginning 0) break) (> (match-end 0) break))
              (setq inside t)))))
      inside)))

(add-hook 'fill-nobreak-predicate #'ai-tools-no-break-in-code-span)

(defconst ai-tools-fill--prose-line "^\\([ \t]*\\(?:#\\|//\\)\\) \\([^ \t].*\\)$"
  "A comment line the batch filler may fill: prefix, one space, text.")

(defconst ai-tools-fill--verbatim-open "<!\\[CDATA\\[\\|<pre\\b"
  "What opens a region a reader gets byte for byte: a CDATA section, a `<pre>' block.")

(defconst ai-tools-fill--verbatim-close "\\]\\]>\\|</pre>"
  "What closes the region `ai-tools-fill--verbatim-open' opens.")

(defconst ai-tools-fill--skip-line
  (concat "^[ \t]*\\(?:#\\|//\\)[ \t]*"
          "\\(?:!\\|shellcheck\\b\\|noqa\\b\\|pylint:\\|type:\\|pragma\\b\\|SPDX-"
          "\\|ref-index:\\|prose-check:\\|ai-tools-admin-[a-z-]*:"
          "\\|args:\\|stdout:\\|stderr:\\|returns?:\\|\\$[0-9]+[ \t]\\|Values:\\|Defaults?:"
          ;; The column rule excludes the newline from both sides: `[^ ]' matches one, so the
          ;; unanchored form read a line as holding a column whenever the NEXT line was indented
          ;; three spaces or more -- every comment block inside a function body. Two spaces after
          ;; a sentence end are sentence spacing, not a column.
          "\\|[A-Za-z_][A-Za-z0-9_]*=\\|[^ \t\n]+$\\|.*[^ \t\n.!?] \\{2,\\}[^ \t\n]"
          "\\|.*\\(?:" ai-tools-fill--verbatim-open "\\|" ai-tools-fill--verbatim-close "\\)"
          "\\|.*[-=_*─━]\\{3,\\}\\)")
  "A comment line the batch filler leaves alone, and that ends the run before it: a shebang,
a linter directive, an SPDX header, a checker marker (`ref-index: ignore-file', `prose-check:
ignore'), a declaration another tool reads (`# ai-tools-admin-verbs: …', which `ai-tools-admin'
parses out of a contributed command's header), a commented default, a lone token (a path, a URL,
a name on a line of its own), a rule or banner line, a doc comment's contract line (`args:',
`stdout:', `$1 path'), a config header's field line (`Values:', `Default:'), and a line holding
a column. The last two are code rather than prose -- a signature,
a parameter table, an example rule -- and a fill reads them as a sentence and wraps the columns
away; a field line states one value, which does not wrap, and a field joined into the description
above it stops being a field. A marker joined into the paragraph above it stops marking. A column
is a gap of two spaces or more that does not follow a sentence end: `read_user <user>  -- …' over
`read_here         -- …' is a table whose first row a fill would otherwise join to the prose above.")

(defconst ai-tools-fill--vertical-rule "[|│┃║]\\| -- "
  "What draws a column in a comment: a vertical rule character in an ASCII diagram or a table,
and the ` -- ' that separates a name from its description in a two-column list.")

(defun ai-tools-fill--vertical-columns (line)
  "The columns LINE carries a vertical rule or a ` -- ' at.
`string-match' sets the match data, so the caller's own match is saved around it."
  (save-match-data
    (let ((columns nil) (start 0))
      (while (string-match ai-tools-fill--vertical-rule line start)
        (push (match-beginning 0) columns)
        (setq start (match-end 0)))
      columns)))

(defun ai-tools-fill--join-run (beg end)
  "Join the lines between BEG and END into one, dropping the `fill-prefix' from each.
The fill is handed one line, which does two things. `fill-delete-newlines' adds a space after a
sentence that ended a line -- unconditionally, `sentence-end-double-space' deciding only whether
`canonically-space-region' takes it back, and that pass is the one NOSQUEEZE turns off -- so a
run with no line end inside it is never given the second space. And every pass reads the same
input, so where the breaks fall does not depend on where they fell last time. NOSQUEEZE itself
stays on, since it is what keeps an aligned fragment's own column spacing."
  (save-excursion
    (goto-char beg)
    (while (re-search-forward (concat "\n" (regexp-quote fill-prefix)) end t)
      (replace-match " "))))

(defvar ai-tools-fill--verbatim-present nil
  "Non-nil while the buffer being filled holds a verbatim opener.
`ai-tools-fill-comments' binds it, so the scan behind `ai-tools-fill--in-verbatim-p' runs only
over a buffer that has one.")

(defun ai-tools-fill--in-verbatim-p ()
  "Non-nil when the line at point sits inside a CDATA section or a `<pre>' block.
Both hold text a reader gets byte for byte -- an XML payload, preformatted output -- so a line
break inside one is content, whatever comment marker the line carries."
  (and ai-tools-fill--verbatim-present
       (save-match-data
         (save-excursion
           (let ((limit (line-beginning-position))
                 (depth 0)
                 (either (concat ai-tools-fill--verbatim-open "\\|"
                                 ai-tools-fill--verbatim-close)))
             (goto-char (point-min))
             (while (re-search-forward either limit t)
               (setq depth (if (string-match-p ai-tools-fill--verbatim-close (match-string 0))
                               (max 0 (1- depth))
                             (1+ depth))))
             (> depth 0))))))

(defconst ai-tools-fill--comment-fence "^[ \t]*\\(?:#\\|//\\)[ \t]*\\(?:```\\|~~~\\)"
  "A fence marker on a comment line: what a header shows a command between.")

(defvar ai-tools-fill--fence-present nil
  "Non-nil while the buffer being filled holds a fence marker on a comment line.
`ai-tools-fill-comments' binds it, so the scan behind `ai-tools-fill--in-comment-fence-p' runs
only over a buffer that has one.")

(defun ai-tools-fill--in-comment-fence-p ()
  "Non-nil when the line at point sits inside a fenced block a comment carries.
A header shows a command, or several, between two fence markers, and each line of it is one a
reader copies whole: a fill that read them as a paragraph would join two commands into one, or
wrap one that runs past the column. The checker skips the same lines. A marker on a line of its
own is a lone token, which ends a run, so this decides only where a run may start."
  (and ai-tools-fill--fence-present
       (save-match-data
         (save-excursion
           (let ((limit (line-beginning-position))
                 (count 0))
             (goto-char (point-min))
             (while (re-search-forward ai-tools-fill--comment-fence limit t)
               (setq count (1+ count)))
             (= 1 (% count 2)))))))

(defun ai-tools-fill--in-string-p ()
  "Non-nil when the line at point sits inside a string, which the mode's syntax decides.
A heredoc body is the case that matters: the text is data this file writes or feeds elsewhere --
a seeded config header, a fixture, an embedded script -- so a comment marker in it belongs to
that text rather than to this file, and filling it rewrites what the file emits.
`syntax-ppss' searches, so the caller's `looking-at' match is saved around it."
  (save-match-data (nth 3 (syntax-ppss (line-beginning-position)))))

(defun ai-tools-fill--range-markers (ranges)
  "RANGES, a list of (FIRST . LAST) line-number pairs, as (START . END) marker pairs.
Filling a paragraph shortens the buffer, so a line number read after the first fill names a line
the caller did not ask for. The numbers are resolved once, before any fill, and the markers then
move with the text they cover."
  (mapcar (lambda (range)
            (cons (save-excursion (goto-char (point-min))
                                  (forward-line (1- (car range)))
                                  (point-marker))
                  (save-excursion (goto-char (point-min))
                                  (forward-line (cdr range))
                                  (point-marker))))
          ranges))

(defun ai-tools-fill--run-in-ranges (beg end ranges)
  "Non-nil when the region from BEG to END meets a range in RANGES.
RANGES is the marker list `ai-tools-fill--range-markers' builds; nil means every run."
  (or (null ranges)
      (seq-some (lambda (range) (and (< (marker-position (car range)) end)
                                     (> (marker-position (cdr range)) beg)))
                ranges)))

(defconst ai-tools-fill--contract-head
  "\\`[A-Za-z_][^ \t]*\\(?: [^ \t]+\\)*? \\(?::\\|--\\) [^ \t]"
  "The text of a doc comment's contract head, `name <args> : text' or `name <args> -- text'.
A hanging paragraph opens on one. A line introducing an indented example (`Usage:', `Run it
as:') does not, so the commands under it are not joined to it.")

(defvar ai-tools-fill--cell-column nil
  "The column a column table's second column starts at, while one of its cells is filled.
`ai-tools-no-break-before-cell' reads it, so the fill does not break inside the first column or
the gap after it.")

(defun ai-tools-no-break-before-cell ()
  "Non-nil when a column table's cell is being filled and point stands left of its column."
  (and ai-tools-fill--cell-column (< (current-column) ai-tools-fill--cell-column)))

(add-hook 'fill-nobreak-predicate #'ai-tools-no-break-before-cell)

(defconst ai-tools-fill--comment-line "^\\([ \t]*\\(?:#\\|//\\)\\)\\( *\\)\\([^ \t\n].*\\)$"
  "A comment line read as a column table reads it: the marker, the spaces after it, the text.")

(defconst ai-tools-fill--not-a-row
  (concat "[|│┃║]\\|[-=_*─━]\\{3,\\}"
          "\\|\\`\\(?:!\\|shellcheck\\b\\|noqa\\b\\|pylint:\\|type:\\|pragma\\b\\|SPDX-"
          "\\|ref-index:\\|prose-check:\\|ai-tools-admin-[a-z-]*:\\|[A-Za-z_][A-Za-z0-9_]*="
          "\\|args:\\|stdout:\\|stderr:\\|returns?:\\|\\$[0-9]+[ \t]\\|Values:\\|Defaults?:\\)")
  "Comment text that is not a column table's row although it may hold a gap: a pipe table or a
diagram, which `formatters/align-tables.py' aligns, a rule or banner line, a directive or a
commented default, whose spacing another tool reads, and a doc comment's contract line or a
config header's field line, which `ai-tools-fill--skip-line' leaves as written.")

(defconst ai-tools-fill--column-gap "[^ \t.!?]\\( \\{2,\\}\\)[^ \t]"
  "The gap that ends a column table's first column; group 1 is the gap.")

(defun ai-tools-fill--table-line ()
  "The comment line at point as a column table reads it, or nil where it is not a comment line.
A list (MARKER INDENT TEXT KEY-END TEXT-START LAST-START): INDENT counts the spaces after
MARKER; on a row, KEY-END and TEXT-START index TEXT at the first column's end and the second
column's start, and LAST-START at the start of the last column, which differs from TEXT-START
on a row of three columns or more. All three are nil on any other line."
  (when (looking-at ai-tools-fill--comment-line)
    (let ((marker (match-string-no-properties 1))
          (indent (length (match-string-no-properties 2)))
          (text (match-string-no-properties 3)))
      (save-match-data
        (if (and (not (string-match-p ai-tools-fill--not-a-row text))
                 (string-match ai-tools-fill--column-gap text))
            (let ((key-end (match-beginning 1))
                  (start (match-end 1))
                  (last (match-end 1)))
              (while (string-match ai-tools-fill--column-gap text last)
                (setq last (match-end 1)))
              (list marker indent text key-end start last))
          (list marker indent text nil nil nil))))))

(defun ai-tools-fill--line-column (line)
  "The column LINE, an entry of `ai-tools-fill--table-line', puts its second column at."
  (+ (length (nth 0 line)) (nth 1 line) (or (nth 4 line) 0)))

(defun ai-tools-fill--majority (columns)
  "The most frequent of COLUMNS, the smallest on a tie, and how often it occurs, as a cons."
  (let ((best nil) (best-count 0))
    (dolist (column (sort (copy-sequence columns) #'<))
      (let ((count (seq-count (lambda (other) (= other column)) columns)))
        (when (> count best-count) (setq best column best-count count))))
    (cons best best-count)))

(defun ai-tools-fill--replace-line (text)
  "Replace the text of the line at point with TEXT, leaving point at its start."
  (let ((start (line-beginning-position)))
    (delete-region start (line-end-position))
    (goto-char start)
    (insert text)
    (goto-char start)))

(defun ai-tools-fill--table-at-point ()
  "The column table starting at the row at point, as a list of (MARKER-POSITION . LINE), or nil.
A table is a row and the rows and continuation lines after it under the same marker and indent:
a continuation sits deeper than the rows and no further left than the leftmost second column so
far, and is not a fence marker. It takes two rows, or a row with a continuation, since one row
alone is as likely an example command. Point ends on the line after the last line read."
  (let* ((first (ai-tools-fill--table-line))
         (marker (nth 0 first))
         (indent (nth 1 first))
         (leftmost (ai-tools-fill--line-column first))
         (lines nil)
         (rows 0))
    (catch 'done
      (while (not (eobp))
        (let ((line (ai-tools-fill--table-line)))
          (cond ((or (null line) (not (equal (nth 0 line) marker)))
                 (throw 'done nil))
                ((and (= (nth 1 line) indent) (nth 3 line))
                 (setq rows (1+ rows)
                       leftmost (min leftmost (ai-tools-fill--line-column line)))
                 (push (cons (point-marker) line) lines))
                ((and (> (nth 1 line) indent)
                      (>= (+ (length marker) (nth 1 line)) leftmost)
                      (not (looking-at ai-tools-fill--comment-fence)))
                 (push (cons (point-marker) (list marker (nth 1 line) (nth 2 line) nil nil nil)) lines))
                (t (throw 'done nil))))
        (forward-line 1)))
    (and (or (>= rows 2) (> (length lines) rows)) (nreverse lines))))

(defun ai-tools-fill--full-line (line)
  "LINE, an entry of `ai-tools-fill--table-line', as the text of the buffer line it was read from."
  (concat (nth 0 line) (make-string (nth 1 line) ?\s) (nth 2 line)))

(defun ai-tools-fill--starts-word-at (line column)
  "Non-nil when a word of LINE, an entry of `ai-tools-fill--table-line', starts at COLUMN."
  (let ((full (ai-tools-fill--full-line line)))
    (and (< column (length full))
         (eq (aref full (1- column)) ?\s)
         (not (eq (aref full column) ?\s)))))

(defun ai-tools-fill--last-column (line)
  "The column LINE, a row read by `ai-tools-fill--table-line', puts its last column at."
  (+ (length (nth 0 line)) (nth 1 line) (nth 5 line)))

(defun ai-tools-fill--realign-table (lines)
  "Re-pad LINES, a table from `ai-tools-fill--table-at-point', when a row has left its column.
Only a table of two columns is re-padded: where a row holds a second wide gap, which gap ends the
first column is a guess -- `name = value  -- text' pads the name with one space or several -- and
a wrong guess moves a column the author placed. The table's column is the one most rows start
their second column at, and it is read only where two rows agree on it, since rows that agree on
no column were not aligned to begin with. A row a word of which starts at that column is aligned.
A row that is not aligned is one whose first column was renamed to another width: every row is
re-padded to the table's column, widened where such a row's first column needs it plus a gap of
two, and a continuation line at or right of the column moves by the same amount. Returns
\(ROW-MARKER . COLUMN) for each row, COLUMN being where its last column starts."
  (let* ((rows (seq-filter (lambda (entry) (nth 3 (cdr entry))) lines))
         (majority (ai-tools-fill--majority
                    (mapcar (lambda (entry) (ai-tools-fill--line-column (cdr entry))) rows)))
         (two-columns (seq-every-p (lambda (entry) (= (nth 4 (cdr entry)) (nth 5 (cdr entry)))) rows))
         (column (and two-columns (>= (cdr majority) 2) (car majority)))
         (broken (and column (seq-remove (lambda (entry)
                                           (ai-tools-fill--starts-word-at (cdr entry) column))
                                         rows)))
         (target column))
    (when broken
      (dolist (entry broken)
        (let ((line (cdr entry)))
          (setq target (max target (+ 2 (length (nth 0 line)) (nth 1 line) (nth 3 line))))))
      (dolist (entry lines)
        (let* ((line (cdr entry))
               (full (ai-tools-fill--full-line line))
               (marker (nth 0 line)))
          (goto-char (car entry))
          (cond ((memq entry broken)
                 (let ((lead (substring full 0 (+ (length marker) (nth 1 line) (nth 3 line)))))
                   (ai-tools-fill--replace-line
                    (concat lead (make-string (- target (length lead)) ?\s)
                            (substring full (+ (length marker) (nth 1 line) (nth 4 line)))))))
                ((nth 3 line)
                 (let ((lead (string-trim-right (substring full 0 column))))
                   (ai-tools-fill--replace-line
                    (concat lead (make-string (- target (length lead)) ?\s) (substring full column)))))
                ((>= (ai-tools-fill--line-column line) column)
                 (ai-tools-fill--replace-line
                  (concat marker (make-string (+ (nth 1 line) (- target column)) ?\s) (nth 2 line))))))))
    (mapcar (lambda (entry)
              (let ((line (cdr entry)))
                (cons (car entry)
                      (cond (broken target)
                            ((and column (ai-tools-fill--starts-word-at line column)) column)
                            (t (ai-tools-fill--last-column line))))))
            rows)))

(defun ai-tools-fill--wrap-cells (lines cells end)
  "Fill each cell of a column table whose lines run past `fill-column'.
LINES is the table from `ai-tools-fill--table-at-point', CELLS the (ROW-MARKER . COLUMN) pairs
`ai-tools-fill--realign-table' returns, and END a marker after the table's last line. A cell is
its row and the continuation lines up to the next row; it is joined into one line and filled
with the indent of its first continuation line, or of its last column where it has none, as the
prefix of every line after the first."
  (let ((filled 0)
        (remaining cells))
    (while remaining
      (let* ((row (car (car remaining)))
             (column (cdr (car remaining)))
             (next (if (cdr remaining) (car (car (cdr remaining))) end))
             (marker (nth 0 (cdr (assq row lines))))
             (over nil))
        (save-excursion
          (goto-char row)
          (while (< (point) next)
            (end-of-line)
            (when (> (current-column) fill-column) (setq over t))
            (forward-line 1)))
        (when over
          ;; The cell stops before the newline that ends its last line, or at the buffer's end. A cell
          ;; that already has a continuation line keeps its indent, which may hang under a later word
          ;; than the second column's first.
          (let* ((stop (copy-marker (if (eq (char-before next) ?\n) (1- next) next)))
                 (indent (save-excursion
                           (goto-char row)
                           (forward-line 1)
                           (if (and (< (point) next)
                                    (looking-at (concat (regexp-quote marker) "\\( *\\)[^ \t\n]")))
                               (+ (length marker) (length (match-string 1)))
                             column)))
                 (fill-prefix (concat marker (make-string (- indent (length marker)) ?\s)))
                 (ai-tools-fill--cell-column column))
            (save-excursion
              (goto-char row)
              (while (re-search-forward (concat "\n" (regexp-quote marker) " *") stop t)
                (replace-match " ")))
            (fill-region row stop nil t)
            (setq filled (1+ filled))))
        (setq remaining (cdr remaining))))
    filled))

(defun ai-tools-fill--tables (ranges)
  "Realign and wrap every column table in the buffer that meets RANGES; returns the count."
  (let ((count 0))
    (goto-char (point-min))
    (while (not (eobp))
      (let ((first (ai-tools-fill--table-line)))
        (if (and first (nth 3 first)
                 (not (ai-tools-fill--in-string-p))
                 (not (ai-tools-fill--in-verbatim-p))
                 (not (ai-tools-fill--in-comment-fence-p)))
            (let* ((beg (point))
                   (lines (ai-tools-fill--table-at-point))
                   (end (point-marker)))
              (if (not (and lines (ai-tools-fill--run-in-ranges beg end ranges)))
                  (unless lines (goto-char beg) (forward-line 1))
                (let ((cells (ai-tools-fill--realign-table lines)))
                  (ai-tools-fill--wrap-cells lines cells end)
                  (setq count (1+ count)))
                (goto-char end)))
          (forward-line 1))))
    count))

(defconst ai-tools-fill--decorated-header
  "^[ \t]*\\(?:#\\|//\\)[ \t]+\\([-=_*─━]\\)\\1+ [^ \t\n].*[^ \t\n] \\1\\{3,\\}[ \t]*$"
  "A decorated header: a rule, a title, and a rule of the same character to the line end.")

(defun ai-tools-fill--trim-headers (ranges)
  "Shorten the trailing rule of every decorated header past `fill-column' that meets RANGES.
The rule keeps three characters at least, so a title too long for the column leaves the line
over it, which the closing width report names. The line is never wrapped."
  (let ((count 0))
    (goto-char (point-min))
    (while (not (eobp))
      (when (and (looking-at ai-tools-fill--decorated-header)
                 (not (ai-tools-fill--in-string-p))
                 (not (ai-tools-fill--in-comment-fence-p))
                 (ai-tools-fill--run-in-ranges (point) (line-end-position) ranges))
        (let ((rule (match-string 1)))
          (end-of-line)
          (skip-chars-backward " \t")
          (let* ((rule-end (point))
                 (rule-start (progn (skip-chars-backward rule) (point)))
                 (excess (- (save-excursion (goto-char rule-end) (current-column)) fill-column))
                 (removable (- rule-end rule-start 3)))
            (when (and (> excess 0) (> removable 0))
              (delete-region (- rule-end (min excess removable)) rule-end)
              (setq count (1+ count))))))
      (forward-line 1))
    count))

(defun ai-tools-fill-comments (&optional ranges)
  "Fill every comment paragraph, column table and decorated header in the buffer at `fill-column'.
With RANGES, a list of (FIRST . LAST) line-number pairs, fill only a paragraph meeting one,
which is how `formatters/format.sh' fills what a diff touched. Returns the number of paragraphs,
tables and headers filled. See the file header for what is left alone and for the shapes filled
by rules of their own."
  (let ((filled 0)
        (sentence-end-double-space nil)
        (colon-double-space nil)
        (ranges (and ranges (ai-tools-fill--range-markers ranges)))
        (ai-tools-fill--verbatim-present
         (save-excursion (goto-char (point-min))
                         (re-search-forward ai-tools-fill--verbatim-open nil t)))
        (ai-tools-fill--fence-present
         (save-excursion (goto-char (point-min))
                         (re-search-forward ai-tools-fill--comment-fence nil t))))
    (save-excursion
      (setq filled (+ (ai-tools-fill--tables ranges) (ai-tools-fill--trim-headers ranges)))
      (goto-char (point-min))
      (while (not (eobp))
        (if (and (looking-at ai-tools-fill--prose-line)
                 (not (looking-at ai-tools-fill--skip-line))
                 (not (ai-tools-fill--in-string-p))
                 (not (ai-tools-fill--in-verbatim-p))
                 (not (ai-tools-fill--in-comment-fence-p)))
            (let* ((prefix (match-string 1))
                   (head-text (match-string 2))
                   (beg (point))
                   (lines 0)
                   (plain t)
                   (hang nil)
                   (drawn nil)
                   (verticals nil)
                   (run-re (concat "^" (regexp-quote prefix) " "))
                   (line-re (concat "^" (regexp-quote prefix) " [^ \t]"))
                   (deeper-re (concat "^" (regexp-quote prefix) "\\( \\{2,\\}\\)[^ \t]")))
              (while (and (not (eobp))
                          (looking-at run-re)
                          (not (looking-at ai-tools-fill--skip-line)))
                (setq lines (1+ lines))
                (cond ((looking-at line-re)
                       (when (> lines 1) (setq hang 'no)))
                      ((and (> lines 1) (looking-at deeper-re)
                            (member hang (list nil (match-string 1))))
                       (setq plain nil hang (if (eq hang 'no) 'no (match-string 1))))
                      (t (setq plain nil hang 'no)))
                (let ((columns (ai-tools-fill--vertical-columns
                                (buffer-substring-no-properties (point) (line-end-position)))))
                  (when (seq-intersection columns verticals) (setq drawn t))
                  (setq verticals columns))
                (forward-line 1))
              ;; A hanging paragraph is a contract head and continuation lines that all share one
              ;; deeper indent; any other run holding a deeper line is left as written.
              (let ((continuation
                     (cond (plain " ")
                           ((and (stringp hang)
                                 (save-match-data
                                   (string-match-p ai-tools-fill--contract-head head-text)))
                            hang))))
                (when (and continuation (not drawn)
                           (ai-tools-fill--run-in-ranges beg (point) ranges))
                  (let ((end (point-marker))
                        (fill-prefix (concat prefix continuation)))
                    (ai-tools-fill--join-run beg end)
                    (fill-region beg end nil t)
                    (goto-char end)
                    (setq filled (1+ filled))))))
          (forward-line 1))))
    filled))

(defun ai-tools-fill-comments-file (file &optional width ranges)
  "Fill the comment paragraphs, column tables and decorated headers of FILE in place and save it.
The mode and `fill-column' come from the file's extension and the repository's .dir-locals.el;
WIDTH overrides the column, and RANGES confines the fill as in `ai-tools-fill-comments'.
Writes no backup and no lock file. FILE is read and written as UTF-8 with Unix line ends, so
every byte a fill does not touch comes back as it was. A file-local variable is applied only
where Emacs marks it safe and an `eval:' form never is: the text a formatter reads is not a place
it takes instructions from. A symlink, or anything but a regular file, is an error rather than a
fill, since the write would land where the link points."
  (unless (and (file-regular-p file) (not (file-symlink-p file)))
    (error "%s: not a regular file, or a symlink" file))
  (let ((create-lockfiles nil)
        (make-backup-files nil)
        (enable-local-variables :safe)
        (enable-local-eval nil)
        (coding-system-for-read 'utf-8-unix)
        (coding-system-for-write 'utf-8-unix))
    (with-current-buffer (find-file-noselect file)
      (let ((fill-column (or width fill-column))
            (require-final-newline nil))
        (let ((count (ai-tools-fill-comments ranges)))
          (when (buffer-modified-p)
            (save-buffer))
          (message "%s: %d comment paragraph(s) filled at %d columns" file count fill-column))))))

(provide 'ai-tools-fill)
;;; ai-tools-fill.el ends here
