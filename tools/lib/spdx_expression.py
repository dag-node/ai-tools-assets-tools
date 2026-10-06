# SPDX-License-Identifier: MIT
"""SPDX licence expressions, parsed before they are judged, and judged against an allowlist.

An expression is identifiers joined by `AND` and `OR`, with parentheses. The policy is the conservative one the format
states: every identifier under `AND` and under `OR` is on the list in force, so `MIT OR GPL-3.0-only` is refused despite
its MIT option. `WITH` exceptions, `LicenseRef-` and `DocumentRef-` references, a `+` suffix, `NONE`, `NOASSERTION`, an
empty value and a malformed expression are refused; which of these it is, is reported apart from a policy refusal, so a
publisher reads whether the expression is wrong or merely outside the list. The grammar is bounded before it is parsed:
an expression has at most `MAX_TOKENS` tokens and nests at most `MAX_NESTING` parentheses deep, so the recursive parser
runs on a bounded input and a longer expression is a syntax finding, not a recursion limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional

TOKEN = re.compile(r"\s*(?:(?P<open>\()|(?P<close>\))|(?P<word>[A-Za-z0-9.+:-]+))")
IDENTIFIER = re.compile(r"^[A-Za-z0-9.-]+$")
OPERATORS = ("AND", "OR", "WITH")
MAX_TOKENS = 64
MAX_NESTING = 8


@dataclass
class SpdxEvaluation:
    """What the parser and the policy said about one expression."""

    expression: str
    identifiers: List[str] = field(default_factory=list)
    syntax_error: Optional[str] = None
    unsupported: List[str] = field(default_factory=list)
    outside_allowlist: List[str] = field(default_factory=list)

    @property
    def is_well_formed(self) -> bool:
        return self.syntax_error is None and not self.unsupported

    @property
    def is_allowed(self) -> bool:
        return self.is_well_formed and not self.outside_allowlist


class _Parser:
    def __init__(self, tokens: List[str]) -> None:
        self.tokens = tokens
        self.position = 0
        self.depth = 0
        self.identifiers: List[str] = []
        self.unsupported: List[str] = []

    def peek(self) -> Optional[str]:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def parse_expression(self) -> None:
        self.parse_operand()
        while self.peek() in ("AND", "OR"):
            self.take()
            self.parse_operand()

    def parse_operand(self) -> None:
        token = self.peek()
        if token is None:
            raise ValueError("an operator has no right-hand side")
        if token == "(":
            self.take()
            self.depth += 1
            if self.depth > MAX_NESTING:
                raise ValueError(f"nests more than {MAX_NESTING} parentheses deep")
            self.parse_expression()
            if self.peek() != ")":
                raise ValueError("a `(` is not closed")
            self.take()
            self.depth -= 1
        elif token in OPERATORS or token == ")":
            raise ValueError(f"`{token}` where a licence identifier is expected")
        else:
            self.take()
            self.read_identifier(token)
            if self.peek() == "WITH":
                self.take()
                exception = self.peek()
                if exception is None or exception in OPERATORS or exception in ("(", ")"):
                    raise ValueError("`WITH` has no exception after it")
                self.take()
                self.unsupported.append(f"`{token} WITH {exception}`: an exception is not supported")

    def read_identifier(self, token: str) -> None:
        if token.startswith("LicenseRef-") or token.startswith("DocumentRef-"):
            self.unsupported.append(f"`{token}`: a licence reference is not accepted")
        elif token in ("NONE", "NOASSERTION"):
            self.unsupported.append(f"`{token}` names no licence")
        elif token.endswith("+"):
            self.unsupported.append(f"`{token}`: the `+` suffix is not accepted; name the `-or-later` identifier")
        elif not IDENTIFIER.match(token):
            raise ValueError(f"`{token}` is not a licence identifier")
        self.identifiers.append(token)


def tokenize(expression: str) -> List[str]:
    tokens: List[str] = []
    position = 0
    while position < len(expression):
        match = TOKEN.match(expression, position)
        if match is None or match.end() == position:
            raise ValueError(f"unexpected character `{expression[position]}` at column {position + 1}")
        position = match.end()
        token = match.group("open") or match.group("close") or match.group("word")
        if token:
            tokens.append(token)
    return tokens


def evaluate_expression(expression: str, allowlist: Iterable[str]) -> SpdxEvaluation:
    """Parse `expression` and judge every identifier against `allowlist`."""
    evaluation = SpdxEvaluation(expression=expression)
    stripped = expression.strip()
    if not stripped:
        evaluation.syntax_error = "the licence is empty"
        return evaluation
    try:
        tokens = tokenize(stripped)
        if len(tokens) > MAX_TOKENS:
            raise ValueError(f"has {len(tokens)} tokens; an expression has at most {MAX_TOKENS}")
        parser = _Parser(tokens)
        parser.parse_expression()
        if parser.peek() is not None:
            raise ValueError(f"`{parser.peek()}` follows a complete expression")
    except ValueError as error:
        evaluation.syntax_error = str(error)
        return evaluation
    evaluation.identifiers = parser.identifiers
    evaluation.unsupported = parser.unsupported
    allowed: FrozenSet[str] = frozenset(allowlist)
    evaluation.outside_allowlist = [identifier for identifier in parser.identifiers if identifier not in allowed]
    return evaluation
