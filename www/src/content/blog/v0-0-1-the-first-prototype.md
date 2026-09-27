---
title: "v0.0.1: the first Mah prototype"
date: 2025-02-07
description: "Where Mah started: a grammar-DSL-generated parser, a flat-array interpreter, and just enough syntax to find prime numbers."
tags: [changelog]
version: "0.0.1"
---

The earliest version of Mah wasn't hand-written at all: a small grammar
DSL fed a parser generator (`compiler-generator/`, kept today only as
history — see `docs/GRAMMAR_DSL.md`), producing a parser for a fairly
minimal language: numbers, strings, real booleans, `if`/`elif`/`else`,
`while`/`break`/`continue`, functions with `def`, `print`/`input`/`sin`/
`cos`, and a flat-array interpreter with static scoping for variables and
functions.

A few things that shipped in this first stretch:

- decimal numbers (via Python's `decimal`, still true today) instead of
  floats
- comments, function calls, the modulo operator, and better error
  messages with line numbers
- a first syntax-highlighting config, and a screenshot of it working in
  an editor

This era ran from November 2024 through early February 2025 and is where
the name and the motto ("ماه — moon in Persian") were picked. Everything
from here up through v0.1.0 rewrites and extends this prototype into what
the docs describe today — starting with v0.0.2, which throws out the
generated parser for a hand-written one and adds closures, structs, and
enums.
