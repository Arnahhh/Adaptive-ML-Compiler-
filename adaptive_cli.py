"""Hybrid code-improvement pipeline shared by the terminal app and desktop UI."""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SUPPORTED = {".c": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".py": "Python", ".java": "Java"}
DEFAULT_GOAL = "automatic improvement"


def detect_language(source: str, filename: str = "") -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix in SUPPORTED:
        return SUPPORTED[suffix]
    text = source
    # Check C/C++ markers before generic class syntax so a C++ class is not
    # mistaken for Java merely because both languages use the keyword class.
    if re.search(r"#\s*include\s*<iostream>|\bstd::|\bcout\s*<<|\bcin\s*>>|\busing\s+namespace\s+std|\btemplate\s*<", text):
        return "C++"
    if re.search(r"#\s*include\s*[<\"](stdio|stdlib|string|math)\.h[>\"]|\bprintf\s*\(|\bscanf\s*\(", text):
        return "C"
    if re.search(r"\bSystem\.(out|in)\b|\bimport\s+java\.|\bpublic\s+(?:(?:abstract|final)\s+)*class\s+\w+", text) or re.search(r"^\s*package\s+[\w.]+\s*;", text, re.M):
        return "Java"
    if re.search(r"\b(def|elif|None|True|False)\b|\bprint\s*\(", text) or re.search(r"\bdef\s+\w+\s*\(|\bif\s+.*:\s*$", text, re.M):
        return "Python"
    if re.search(r"\bclass\s+\w+\s*\{|\bnew\s+[A-Z]\w*\s*\(", text):
        return "Java" if "System.out" in text else "C++"
    return None


def analyze(source: str, language: str) -> dict[str, Any]:
    lines = source.splitlines()
    code_lines = [line for line in lines if line.strip() and not line.lstrip().startswith(("#", "//", "/*", "*"))]
    features: dict[str, Any] = {
        "source_lines": len(lines),
        "lines_of_code": len(code_lines),
        "lines_of_code_approx": len(code_lines),
        "loops_approx": len(re.findall(r"\b(for|while|do)\b", source)),
        "branches_approx": len(re.findall(r"\b(if|else|switch|case|catch|elif)\b", source)),
        "arithmetic_operators_approx": len(re.findall(r"(?<![*/])[+*%]|(?<!/)/(?!/)|(?<!-)-(?!>)", source)),
        "functions_or_methods_approx": len(re.findall(r"\b(def|function|void|int|float|double|public|private|static)\s+[A-Za-z_$][\w$]*\s*\(", source)),
    }
    if language == "Python":
        try:
            tree = ast.parse(source)
            features["python_ast_nodes"] = sum(1 for _ in ast.walk(tree))
            features["python_functions"] = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree))
            features["python_classes"] = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
            features["python_syntax"] = "valid"
        except SyntaxError as exc:
            features["python_syntax"] = f"invalid at line {exc.lineno}: {exc.msg}"
    return features


def native_validate(path: Path, language: str, source: str) -> tuple[bool | None, str]:
    suffix = path.suffix.lower()
    try:
        if language == "Python":
            try:
                compile(source, str(path), "exec")
                return True, "Python syntax check passed. This does not prove runtime correctness."
            except SyntaxError as exc:
                return False, f"Python syntax error at line {exc.lineno}: {exc.msg}"
        if language in ("C", "C++"):
            compiler_name = "gcc" if language == "C" else "g++"
            compiler = shutil.which(compiler_name)
            if not compiler:
                return None, f"Cannot validate {language}: {compiler_name} is not on PATH."
            with tempfile.TemporaryDirectory(prefix="amlc_validate_") as temp:
                temp_path = Path(temp) / ("candidate" + suffix)
                temp_path.write_text(source, encoding="utf-8", newline="")
                result = subprocess.run([compiler, "-fsyntax-only", str(temp_path)], capture_output=True, text=True, timeout=25)
            if result.returncode == 0:
                return True, f"{language} syntax/type check passed ({compiler_name} -fsyntax-only)."
            detail = (result.stderr or result.stdout).strip().splitlines()
            return False, "\n".join(detail[:10]) or f"{language} validation failed."
        if language == "Java":
            compiler = shutil.which("javac")
            if not compiler:
                return None, "Cannot validate Java: javac is not on PATH. Install a JDK to enable Java validation."
            with tempfile.TemporaryDirectory(prefix="amlc_validate_") as temp:
                temp_path = Path(temp) / path.name
                temp_path.write_text(source, encoding="utf-8", newline="")
                result = subprocess.run([compiler, "-encoding", "UTF-8", "-Xlint:none", str(temp_path)], cwd=temp, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return True, "Java compilation check passed (javac)."
            detail = (result.stderr or result.stdout).strip().splitlines()
            return False, "\n".join(detail[:10]) or "Java validation failed."
    except subprocess.TimeoutExpired:
        return None, f"Validation timed out for {language}; candidate was not fully validated."
    except OSError as exc:
        return None, f"Validation could not run: {exc}"
    return None, "No validator available for this language."


def conservative_rules(source: str, language: str, preference: str = DEFAULT_GOAL) -> tuple[str, list[str]]:
    """Apply low-risk formatting and isolated literal folds, avoiding expression chains."""
    original = source
    updated_lines = []
    changes: list[str] = []
    trailing_count = 0
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        clean = body.rstrip(" \t")
        trailing_count += clean != body
        updated_lines.append(clean + ending)
    updated = "".join(updated_lines)
    if trailing_count:
        changes.append(f"Removed trailing whitespace from {trailing_count} line(s).")
    if updated and not updated.endswith("\n"):
        updated += "\n"
        changes.append("Added a final newline.")

    # Fold only an entire simple assignment/return expression. Do not rewrite part
    # of a longer expression (e.g., 2 + 3 * 4) where precedence could change.
    simple_expression = re.compile(
        r"^(?P<prefix>\s*(?:(?:const\s+)?(?:(?:unsigned|signed|long|short)\s+)*(?:int|float|double|auto|var|int|long|short)\s+)?[A-Za-z_$][\w$]*\s*=\s*|\s*return\s+)"
        r"(?P<left>\d{1,5})\s*(?P<operator>[+*-])\s*(?P<right>\d{1,5})(?P<suffix>\s*;?\s*)$"
    )
    folded = 0
    lines_out = []
    for line in updated.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if any(mark in body for mark in ("'", '"', "//", "/*", "#")):
            lines_out.append(line)
            continue
        match = simple_expression.fullmatch(body)
        if not match:
            lines_out.append(line)
            continue
        left = int(match.group("left"))
        right = int(match.group("right"))
        op = match.group("operator")
        value = left + right if op == "+" else (left - right if op == "-" else left * right)
        if not (-2147483648 <= value <= 2147483647):
            lines_out.append(line)
            continue
        lines_out.append(match.group("prefix") + str(value) + match.group("suffix") + ending)
        folded += 1
    updated = "".join(lines_out)
    if language == "Python":
        print_expr = re.compile(r"^(?P<prefix>\s*print\s*\(\s*)(?P<left>\d{1,5})\s*(?P<operator>[+*-])\s*(?P<right>\d{1,5})(?P<suffix>\s*\)\s*)$")
        print_lines = []
        for line in updated.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            match = print_expr.fullmatch(body)
            if not match:
                print_lines.append(line)
                continue
            left = int(match.group("left"))
            right = int(match.group("right"))
            op = match.group("operator")
            value = left + right if op == "+" else (left - right if op == "-" else left * right)
            if -2147483648 <= value <= 2147483647:
                print_lines.append(match.group("prefix") + str(value) + match.group("suffix") + ending)
                folded += 1
            else:
                print_lines.append(line)
        updated = "".join(print_lines)
    if folded:
        changes.append(f"Folded {folded} isolated small-integer constant expression(s).")

    if language == "Python":
        try:
            ast.parse(updated)
        except SyntaxError:
            changes.append("Python AST check reported invalid syntax; the pipeline will validate or roll back the candidate.")
    if not changes:
        changes.append("No low-risk deterministic rewrite matched; analysis and validation still ran.")
    if updated == original and changes == ["No low-risk deterministic rewrite matched; analysis and validation still ran."]:
        changes = ["No source transformation was necessary; analysis and validation still ran."]
    return updated, changes


def extract_model_code(response: str) -> str:
    response = response.strip()
    match = re.search(r"```(?:[A-Za-z0-9_+.-]+)?\s*\n(.*?)```", response, re.S)
    if match:
        response = match.group(1).strip("\n")
    return response + ("\n" if response and not response.endswith("\n") else "")


def available_ollama_model(endpoint: str, preferred: str) -> tuple[str | None, str]:
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2.5) as response:
            data = json.loads(response.read().decode("utf-8"))
        names = [item.get("name", "") for item in data.get("models", []) if item.get("name")]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f" Server response: {detail[:1200]}" if detail else ""
        return None, f"Ollama model discovery failed with HTTP {exc.code} ({exc.reason}).{suffix}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"Local Ollama is unavailable ({exc.__class__.__name__}: {exc})."
    if not names:
        return None, "Ollama is running but has no downloaded models. Pull a coding model, e.g. `ollama pull qwen2.5-coder:7b`."
    for name in names:
        if name == preferred or name.split(":")[0] == preferred.split(":")[0]:
            return name, f"Using configured local model '{name}'."
    coding_names = [name for name in names if any(word in name.lower() for word in ("coder", "code", "starcoder", "deepseek-coder"))]
    selected = coding_names[0] if coding_names else names[0]
    return selected, f"Configured model '{preferred}' was not installed; automatically selected installed model '{selected}'."



def ollama_transform(source: str, language: str, model: str, endpoint: str) -> str:
    """Ask Ollama for a concrete, behavior-preserving source improvement."""

    prompt = (
        "You are the optimization engine of an academic hybrid compiler.\n"
        f"Language: {language}\n\n"

        "TASK:\n"
        "Analyze the source and return a genuinely improved version when a "
        "safe, meaningful improvement exists. Do not merely reformat code.\n\n"

        "LOOK FOR THESE OPPORTUNITIES:\n"
        "1. Repeated calculations whose result can be reused.\n"
        "2. Redundant conditions or unnecessary branches.\n"
        "3. Inefficient loops or repeated work that can safely be avoided.\n"
        "4. Unnecessarily complicated expressions that can be simplified.\n"
        "5. Duplicated code that can be simplified without harming readability.\n"
        "6. Clearer variable usage and control flow.\n"
        "7. Simple constant expressions that can be evaluated at compile time.\n\n"

        "STRICT SAFETY RULES:\n"
        "- Preserve observable program behavior for valid inputs.\n"
        "- Preserve input/output behavior, prompts, and output formatting.\n"
        "- Preserve public function names, class names, signatures, and entry points.\n"
        "- Preserve required imports and dependencies; do not invent APIs.\n"
        "- Do not remove input operations, output operations, or error handling "
        "just to make the code shorter.\n"
        "- Do not change algorithms in ways that could alter edge cases, "
        "integer overflow behavior, ordering, or floating-point results.\n"
        "- Do not introduce concurrency or external dependencies.\n"
        "- Keep the code compatible with the original language and its syntax.\n"
        "- Prefer simple, explainable changes over clever or speculative changes.\n"
        "- If no meaningful safe improvement exists, return the original source.\n\n"

        "OUTPUT RULES:\n"
        "- Return the complete source file, not a fragment.\n"
        "- Return source code only: no Markdown fences, explanations, or headings.\n"
        "- Do not omit unchanged functions or declarations.\n\n"

        "SOURCE START\n"
        + source
        + "\nSOURCE END"
    )

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0}
    })

    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/generate",
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))

    candidate = extract_model_code(data.get("response", ""))

    if not candidate.strip():
        raise ValueError("Ollama returned an empty source candidate.")

    if len(candidate) > max(50000, len(source) * 4 + 5000):
        raise ValueError(
            "Ollama returned an implausibly large source candidate; it was rejected."
        )

    return candidate


def io_operations(source: str, language: str) -> set[str]:
    """Collect common observable input/output operations for a conservative AI guard.

    This is a best-effort source-level guard, not a full language parser. It prevents
    an AI candidate from silently removing the program's obvious input/output APIs.
    Runtime comparison is still needed to check behavior for particular inputs.
    """
    patterns: dict[str, tuple[str, ...]] = {
        "Python": (r"\binput\s*\(", r"\bprint\s*\(", r"\bsys\s*\.\s*stdin\b", r"\bsys\s*\.\s*stdout\b"),
        "C": (r"\bscanf\s*\(", r"\bprintf\s*\(", r"\bfscanf\s*\(", r"\bfprintf\s*\(", r"\bgetchar\s*\(", r"\bputchar\s*\(", r"\bgets\s*\(", r"\bputs\s*\("),
        "C++": (r"\bstd\s*::\s*cin\b", r"\bstd\s*::\s*cout\b", r"(?<![\w:])cin\s*>>", r"(?<![\w:])cout\s*<<", r"\bgetline\s*\(", r"\bscanf\s*\(", r"\bprintf\s*\("),
        "Java": (r"\bSystem\s*\.\s*out\s*\.\s*(?:print|println|printf)\s*\(", r"\bSystem\s*\.\s*err\s*\.", r"\bScanner\b", r"\b(?:nextInt|nextLine|nextDouble|next)\s*\(", r"\breadLine\s*\(", r"\bBufferedReader\b"),
    }
    source_patterns = patterns.get(language, ())
    return {pattern for pattern in source_patterns if re.search(pattern, source)}


def interface_symbols(source: str, language: str) -> set[tuple[str, int | None]]:
    """Best-effort guard against an AI rewrite deleting declared symbols/interfaces."""
    symbols: set[tuple[str, int | None]] = set()
    if language == "Python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return symbols
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                count = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
                count += int(args.vararg is not None) + int(args.kwarg is not None)
                symbols.add(("function:" + node.name, count))
            elif isinstance(node, ast.ClassDef):
                symbols.add(("class:" + node.name, None))
        return symbols
    for match in re.finditer(r"\b(class|struct|interface|enum)\s+([A-Za-z_$][\w$]*)", source):
        symbols.add((match.group(1) + ":" + match.group(2), None))
    # Protect main and function/method names from simple C-family/Java declarations.
    signature = re.compile(r"(?m)^\s*(?:[\w:<>,~*&\[\].]+\s+)+([A-Za-z_$][\w$]*)\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:\{|;)")
    for match in signature.finditer(source):
        name, args = match.group(1), match.group(2).strip()
        if name in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        if not args or args == "void":
            count = 0
        else:
            count = args.count(",") + 1
        symbols.add(("function:" + name, count))
    return symbols


def run_existing_compiler(path: Path, native_validation_ok: bool | None = None, native_validation_message: str = "") -> str:
    # The bundled legacy core's Python frontend is intentionally only a subset parser.
    # Do not run it against general Python source and misreport valid programs as syntax failures.
    if path.suffix.lower() == ".py":
        validation = native_validation_message or "Python's built-in parser is the language syntax validator."
        return (
            "SKIPPED — LEGACY PYTHON FRONTEND IS A LIMITED SUBSET PARSER.\n"
            f"Independent Python validation: {validation}\n"
            "The original compiler core is not a general-purpose Python compiler. For Python, the app uses "
            "Python AST-based profiling, conservative source transformations, Python syntax validation, and the "
            "Python interpreter for execution. No AST/IR/CFG/KNN result is claimed for this Python input.\n"
            "This is a core-language-coverage limitation, not a Python syntax error."
        )
    binary = ROOT / ("amlc.exe" if os.name == "nt" else "amlc")
    if not binary.exists():
        return "Original compiler analysis was not run because amlc is not built. Use Build Original Compiler Core or run .\\build.bat."
    try:
        with tempfile.TemporaryDirectory(prefix="amlc_internal_analysis_") as temp:
            isolated = Path(temp)
            binary_copy = isolated / binary.name
            shutil.copy2(binary, binary_copy)
            model_source = ROOT / "ml" / "model.csv"
            if model_source.is_file():
                (isolated / "ml").mkdir()
                shutil.copy2(model_source, isolated / "ml" / "model.csv")
            result = subprocess.run([str(binary_copy), str(path)], cwd=isolated, capture_output=True, text=True, timeout=20)
        output = (result.stdout or result.stderr).strip()
        if not output:
            output = f"Compiler exited with status {result.returncode}."
        # The embedded Python/Java frontends are subset parsers. If an independent
        # validator accepts the code, don't present a subset rejection as a language
        # syntax failure.
        subset_rejection = (
            "could not parse the supported subset" in output.lower()
            or "status : failed" in output.lower()
            or "status: failed" in output.lower()
        )
        if native_validation_ok is True and subset_rejection:
            return (
                "CORE FRONTEND LIMITATION ΓÇö NOT A LANGUAGE SYNTAX ERROR.\n"
                f"The installed language validator passed: {native_validation_message}\n"
                "The embedded compiler frontend rejected this program because its implemented language subset "
                "does not cover all valid constructs in this source. No reliable AST/IR/CFG/KNN result was "
                "produced for this input. This is a limitation of the academic core frontend, not proof that "
                "the source program is invalid.\n\n"
                "Raw subset-frontend diagnostic (for debugging):\n" + output[:8000]
            )
        return output[:12000]
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Original compiler analysis could not run: {exc}"


def build_report(source_path: Path, output_path: Path, language: str, features: dict[str, Any], ai_note: str,
                 before_message: str, after_message: str, changes: list[str], original_compiler: str,
                 original: str, candidate: str) -> str:
    diff = list(difflib.unified_diff(original.splitlines(), candidate.splitlines(), fromfile="original", tofile="improved", lineterm=""))
    changed_lines = sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    lines = [
        "ADAPTIVE ML COMPILER ΓÇö AUTOMATIC IMPROVEMENT REPORT",
        f"Input: {source_path}", f"Output: {output_path}", f"Language: {language}",
        f"AI status: {ai_note}", f"Input validation: {before_message}", f"Output validation: {after_message}",
        f"Changed source lines (approx): {changed_lines}",
        f"Substantive source change: {'NO ΓÇö only whitespace/newline normalization' if original.rstrip() == candidate.rstrip() else 'YES ΓÇö inspect the source diff and test behavior'}",
        "Behavioral equivalence: NOT PROVEN. Run tests and compare outputs before relying on a changed program.",
        "Performance improvement: NOT BENCHMARKED. Compiler-valid code is not automatically faster.",
        "Static code profile:",
    ]
    lines.extend(f"- {key.replace('_', ' ')}: {value}" for key, value in features.items())
    lines.append("Changes:")
    lines.extend(f"- {item}" for item in changes)
    if diff:
        lines.extend(["", "SOURCE DIFF (first 160 lines):", *diff[:160]])
    lines.extend(["", "Original compiler analysis:", original_compiler])
    return "\n".join(lines) + "\n"


def improve_source(source: str, filename: str = "user_code.py", allow_ai: bool = True) -> dict[str, Any]:
    """Improve a source string and return the candidate, language, validation and report."""
    language = detect_language(source, filename)
    if language is None:
        raise ValueError("Could not detect C, C++, Java, or Python. Open a supported source file or paste a complete source program.")
    suffix = next((ext for ext, lang in SUPPORTED.items() if lang == language), ".py")
    stem = Path(filename).stem or "user_code"
    if language == "Java":
        match = re.search(r"(?m)^\s*public\s+(?:(?:abstract|final|strictfp)\s+)*class\s+([A-Za-z_$][\w$]*)", source)
        if not match:
            match = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", source)
        if match:
            stem = match.group(1)
    with tempfile.TemporaryDirectory(prefix="adaptive_ml_improve_") as temp_dir:
        temp_root = Path(temp_dir)
        source_path = temp_root / (stem + suffix)
        source_path.write_text(source, encoding="utf-8", newline="")
        features = analyze(source, language)
        before_ok, before_message = native_validate(source_path, language, source)
        candidate, changes = conservative_rules(source, language)
        model = os.environ.get("AMLC_OLLAMA_MODEL", "qwen2.5-coder:7b")
        endpoint = os.environ.get("AMLC_OLLAMA_URL", "http://127.0.0.1:11434")
        ai_note = "AI disabled by configuration; conservative deterministic improvements were used."
        if allow_ai and os.environ.get("AMLC_DISABLE_OLLAMA", "0").lower() not in {"1", "true", "yes"}:
            selected_model, discovery_note = available_ollama_model(endpoint, model)
            if not selected_model:
                ai_note = discovery_note + " Deterministic improvements were used."
            else:
                ai_note = discovery_note
                try:
                    ai_candidate = ollama_transform(source, language, selected_model, endpoint)
                    before_symbols = interface_symbols(source, language)
                    candidate_symbols = interface_symbols(ai_candidate, language)
                    lost_symbols = before_symbols - candidate_symbols
                    before_io = io_operations(source, language)
                    candidate_io = io_operations(ai_candidate, language)
                    lost_io = before_io - candidate_io
                    if lost_symbols:
                        labels = ", ".join(f"{name}/{count}" if count is not None else name for name, count in sorted(lost_symbols))
                        ai_note += f" AI candidate rejected because it removed/changed declared interfaces: {labels}."
                    elif lost_io:
                        labels = ", ".join(sorted(lost_io))
                        ai_note += f" AI candidate rejected because it removed an observable input/output operation: {labels}."
                    else:
                        ai_ok, ai_validation = native_validate(source_path, language, ai_candidate)
                        if ai_candidate != source and ai_ok is True:
                            candidate = ai_candidate
                            changes = [f"Local Ollama model '{selected_model}' proposed a non-identical candidate; native validation and interface/I-O guards passed."]
                            ai_note += " AI candidate accepted after syntax/compile validation and interface/I-O checks; compare original and improved runs before trusting behavior."
                        elif ai_candidate == source:
                            ai_note += " Model found no source change worth applying; deterministic changes were retained."
                        elif ai_ok is False:
                            ai_note += f" AI candidate rejected by native validation: {ai_validation}"
                        else:
                            ai_note += " AI candidate not accepted because native validation was unavailable."
                except urllib.error.HTTPError as exc:
                    try:
                        detail = exc.read().decode("utf-8", errors="replace").strip()
                    except OSError:
                        detail = ""
                    detail = detail[:1200] if detail else "The server did not include an error body."
                    ai_note += f" AI transform failed with HTTP {exc.code} ({exc.reason}): {detail} Deterministic fallback was used; if the source diff shows only whitespace/newline changes, no substantive optimization was produced."
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    ai_note += f" AI transform failed ({exc.__class__.__name__}: {exc}). Deterministic fallback was used; if the source diff shows only whitespace/newline changes, no substantive optimization was produced."
        else:
            ai_note = "AI disabled for this run; conservative deterministic improvements were used."

        after_ok, after_message = native_validate(source_path, language, candidate)
        if after_ok is False:
            candidate = source
            changes.append("Candidate rolled back because final syntax/compile validation failed.")
            after_ok, after_message = native_validate(source_path, language, candidate)
        if after_ok is None:
            after_message += " Validation was unavailable or incomplete; inspect and test the code before use."
        output_path = (temp_root / "adapted" / source_path.name) if language == "Java" else (temp_root / (source_path.stem + "_improved" + suffix))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(candidate, encoding="utf-8", newline="")
        compiler_analysis = run_existing_compiler(source_path, before_ok, before_message)
        shown_input = Path(filename).name if filename else "Pasted source code"
        compiler_analysis = compiler_analysis.replace(str(source_path), shown_input)
        report = build_report(source_path, output_path, language, features, ai_note, before_message, after_message, changes, compiler_analysis, source, candidate)
        report = report.replace(f"Input: {source_path}", f"Input: {shown_input}")
        report = report.replace(f"Output: {output_path}", "Output: candidate displayed in the Improved Code panel; use Save Improved Code to persist it")
        return {
            "language": language,
            "stem": stem,
            "suffix": suffix,
            "code": candidate,
            "report": report,
            "ai_status": ai_note,
            "input_validation": before_message,
            "output_validation": after_message,
            "changes": changes,
            "analysis": features,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatically improve and validate C, C++, Java, and Python source code")
    parser.add_argument("source", nargs="?", help="Path to a .c, .cpp, .py, or .java source file")
    parser.add_argument("--no-ai", action="store_true", help="Skip the local Ollama improvement step")
    args = parser.parse_args()
    source_arg = args.source
    if not source_arg:
        source_arg = input("Code file path: ").strip().strip('"')
    if not source_arg:
        print("ERROR: Provide a source file path.")
        return 2
    source_path = Path(source_arg).expanduser().resolve()
    if not source_path.is_file():
        print(f"ERROR: Source file not found: {source_path}")
        return 2
    try:
        original = source_path.read_text(encoding="utf-8")
        result = improve_source(original, source_path.name, allow_ai=not args.no_ai)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    suffix = result["suffix"]
    if result["language"] == "Java":
        output_path = source_path.parent / "adapted" / (result["stem"] + suffix)
    else:
        output_path = source_path.with_name(source_path.stem + "_improved" + suffix)
    if output_path.resolve() == source_path.resolve():
        print("ERROR: Output path would overwrite the input; refusing to write.")
        return 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result["code"], encoding="utf-8", newline="")
    report_path = source_path.with_name(source_path.stem + "_improvement_report.txt")
    report = result["report"]
    report = report.replace("Input: " + (source_path.name), "Input: " + str(source_path))
    report = report.replace("Output: candidate displayed in the Improved Code panel; use Save Improved Code to persist it", "Output: " + str(output_path))
    report_path.write_text(report, encoding="utf-8")
    print(f"Language: {result['language']}")
    print(f"AI status: {result['ai_status']}")
    print(f"Output validation: {result['output_validation']}")
    print(f"Improved code saved to: {output_path}")
    print(f"Report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

