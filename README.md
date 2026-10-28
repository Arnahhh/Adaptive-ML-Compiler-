# Adaptive ML Compiler

**Hybrid AI-Assisted Source Code Optimization**

A Python-based project combining rule-based compiler optimizations with locally running AI models to transform source code and compare results.

## Features
- Hybrid rule-based and AI-assisted code optimization.
- Local AI inference using Ollama.
- Before-and-after source code comparison.
- Optimization reports and demonstration programs.
- Local inference without paid AI APIs.

## Technology Stack
Python, Ollama, C/C++, Git, GitHub.

## Workflow
1. Accept source code as input.
2. Apply available rule-based optimizations.
3. Use Ollama for AI-assisted transformations when configured.
4. Generate optimized code and comparison reports.

## Setup
Install Python and the dependencies listed in `requirements.txt`.
Install Ollama and download a compatible model for AI-assisted optimization.

Run the CLI or demo using the project's provided scripts and documentation.

## Main Files
- `adaptive_cli.py` — optimization workflow.
- `adaptive_runtime.py` — runtime support.
- `adaptive_app.py` — application interface.
- `optimizer.c` — compiler optimization implementation.
- `tests/` — test files.
- `examples/` — example programs.

## Limitations
AI-generated code requires validation. Semantic equivalence and performance improvements are not guaranteed.

## Future Enhancements
Syntax-aware transformations, automated correctness testing, and expanded language support.
