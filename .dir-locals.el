;; SPDX-License-Identifier: MIT
;; The column each file kind wraps its prose at, matching ai-tools-base's checkout: a config file
;; header at 72 (the RFC text width), a source comment at 120, a Markdown page a person reads at 80,
;; and a fixture skill an agent retrieves with grep at 120. formatters/fill-comments.sh fills through
;; these values.
((conf-mode . ((fill-column . 72)))
 (conf-unix-mode . ((fill-column . 72)))
 (sh-mode . ((fill-column . 120)))
 (python-mode . ((fill-column . 120)))
 (emacs-lisp-mode . ((fill-column . 120)))
 (markdown-mode . ((fill-column . 80)))
 ("fixtures/" . ((markdown-mode . ((fill-column . 120))))))
