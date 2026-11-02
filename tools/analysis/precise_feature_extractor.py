#!/usr/bin/env python3
"""
precise_feature_extractor.py

基于 libclang 的函数级精准算法特征提取工具。

相比 Tree-sitter 粗筛工具，本工具提供：
  - 精确的 C/C++ AST 解析（支持模板、重载、类型推导）
  - 循环嵌套结构与边界表达式
  - 数组/指针访存模式与 stride 推断
  - 函数调用图与参数类型
  - 数据类型、局部变量、分支条件
  - 支持 compile_commands.json 和手动 include 路径

输出：每个函数一条 JSON（JSONL），结构如下：
  {
    "file": "/abs/path/to/file.cpp",
    "function": "Foo::Bar",
    "signature": "void Foo::Bar(int, float*)",
    "return_type": "void",
    "line": 42,
    "parameters": [...],
    "loops": [...],
    "memory_accesses": [...],
    "calls": [...],
    "types_used": [...],
    "variables": [...],
    "branches": {...}
  }

依赖：
    pip install libclang

Linux/macOS 注意：
    libclang 需要找到 libclang.so/dylib。如果报错，可设置：
        export LD_LIBRARY_PATH=/usr/lib/llvm-<N>/lib:$LD_LIBRARY_PATH   # Linux
        export DYLD_LIBRARY_PATH=/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib:$DYLD_LIBRARY_PATH  # macOS

用法：
    # 单文件（自动尝试常见 include 路径）
    python precise_feature_extractor.py --src file.cpp --out feats.jsonl

    # 使用 compile_commands.json
    python precise_feature_extractor.py --src file.cpp --out feats.jsonl \
        --compile-commands path/to/compile_commands.json

    # 手动指定 include 和宏
    python precise_feature_extractor.py --src file.cpp --out feats.jsonl \
        -I/path/to/include -DFOO=1 -std=c++17

    # 批量处理目录
    python precise_feature_extractor.py --src source_code/xxx --out feats.jsonl \
        --compile-commands path/to/compile_commands.json

作者：为 OpenHarmony 异构优化机会识别方法论实现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import clang.cindex
    from clang.cindex import Cursor, CursorKind, TypeKind, TranslationUnit
except ImportError as exc:  # pragma: no cover
    print(
        "ERROR: libclang Python bindings not found.\n"
        "Install: pip install libclang\n"
        "If libclang native library is missing, set LD_LIBRARY_PATH/DYLD_LIBRARY_PATH.",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Defaults and helpers
# ---------------------------------------------------------------------------

DEFAULT_CLANG_ARGS = [
    "-x", "c++",
    "-std=c++17",
    "-Wno-unknown-warning-option",
    "-Wno-unused-command-line-argument",
]

# Common OpenHarmony include roots (best-effort fallback)
COMMON_INCLUDES = [
    "foundation",
    "foundation/arkui",
    "base",
    "commonlibrary",
    "developtools",
    "third_party",
]


def _default_sysroot_args() -> List[str]:
    """Add platform-specific default include paths so libclang finds system headers."""
    extra: List[str] = []

    # clang builtin headers (stdarg.h, stddef.h, intrinsics, etc.)
    # Use -isystem so they are treated as system headers and do not shadow libc++.
    try:
        import subprocess
        resource_dir = subprocess.check_output(["clang", "-print-resource-dir"], text=True).strip()
        if resource_dir and Path(resource_dir).exists():
            extra.extend(["-isystem", str(Path(resource_dir) / "include")])
    except Exception:
        pass

    if sys.platform == "darwin":
        try:
            import subprocess
            sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
            if sdk and Path(sdk).exists():
                extra.append(f"-isysroot{sdk}")
                # libc++ ships inside the SDK; -isysroot lets clang find it.
                # We do NOT add it via -I to avoid shadowing the resource-dir builtins.
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        candidates = [
            "/usr/include/c++/11",
            "/usr/include/c++/12",
            "/usr/include/c++/13",
            "/usr/include/c++/14",
            "/usr/lib/llvm-14/lib/clang/14.0.0/include",
            "/usr/lib/llvm-15/lib/clang/15.0.0/include",
            "/usr/lib/llvm-16/lib/clang/16.0.0/include",
            "/usr/lib/llvm-17/lib/clang/17.0.0/include",
            "/usr/lib/llvm-18/lib/clang/18.0.0/include",
        ]
        for c in candidates:
            if Path(c).exists():
                extra.extend(["-I", c])
    return extra


def _get_text(cursor: Cursor) -> str:
    """Extract source text for a cursor's extent."""
    tokens = list(cursor.get_tokens())
    if not tokens:
        return cursor.spelling or cursor.displayname
    return "".join(t.spelling for t in tokens)


def _get_location(cursor: Cursor) -> Optional[Dict[str, Any]]:
    loc = cursor.location
    if loc is None or not loc.file:
        return None
    return {
        "file": str(loc.file),
        "line": loc.line,
        "column": loc.column,
        "offset": loc.offset,
    }


def _get_extent(cursor: Cursor) -> Optional[Dict[str, int]]:
    """Return the source line span needed to bind a trace marker to one AST node."""

    try:
        start = cursor.extent.start
        end = cursor.extent.end
        if not start.file or not end.file:
            return None
        return {
            "start_line": start.line,
            "end_line": end.line,
            "start_column": start.column,
            "end_column": end.column,
        }
    except Exception:
        return None


def _qualified_name(cursor: Cursor) -> str:
    """Return fully qualified name for a function/method cursor."""
    if cursor.kind == CursorKind.TRANSLATION_UNIT:
        return ""
    parts = []
    c: Optional[Cursor] = cursor
    while c and c.kind != CursorKind.TRANSLATION_UNIT:
        name = c.spelling or c.displayname
        if name:
            parts.append(name)
        c = c.semantic_parent
    return "::".join(reversed(parts))


def _type_spelling(t: Any) -> str:
    return t.spelling if t else ""


def _cursor_type_spelling(cursor: Cursor) -> str:
    try:
        return cursor.type.spelling
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Compile args discovery
# ---------------------------------------------------------------------------

def _load_compile_commands(path: Path) -> Dict[str, List[str]]:
    """Load compile_commands.json and map file path -> args list."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: failed to load {path}: {exc}", file=sys.stderr)
        return {}

    mapping: Dict[str, List[str]] = {}
    for entry in data:
        file_path = entry.get("file")
        cmd = entry.get("command") or entry.get("arguments", "")
        if not file_path or not cmd:
            continue
        if isinstance(cmd, list):
            tokens = cmd
        else:
            # naive shell split; good enough for typical compile_commands
            tokens = cmd.split()
        # Strip compiler executable and output/file args, keep flags and includes
        flags: List[str] = []
        skip_next = False
        for i, tok in enumerate(tokens):
            if skip_next:
                skip_next = False
                continue
            if i == 0 and (tok.endswith("clang") or tok.endswith("clang++") or tok.endswith("g++") or tok.endswith("gcc")):
                continue
            if tok in ("-o", "-c"):
                skip_next = True
                continue
            if tok.startswith("-") or tok.endswith(".cpp") or tok.endswith(".cc") or tok.endswith(".c"):
                flags.append(tok)
        mapping[str(Path(file_path).resolve())] = flags
    return mapping


def _build_clang_args(
    src: Path,
    compile_commands: Optional[Path],
    extra_includes: List[str],
    extra_defines: List[str],
    std: str,
) -> List[str]:
    args = list(DEFAULT_CLANG_ARGS)
    args[3] = std  # replace -std=c++17
    args.extend(_default_sysroot_args())

    # compile_commands.json
    if compile_commands:
        cc_map = _load_compile_commands(compile_commands)
        abs_src = str(src.resolve())
        if abs_src in cc_map:
            args.extend(cc_map[abs_src])
        else:
            print(f"WARNING: {src} not found in compile_commands.json; using fallback args", file=sys.stderr)

    # manual includes
    for inc in extra_includes:
        args.extend(["-I", inc])

    # manual defines
    for d in extra_defines:
        args.extend(["-D", d])

    return args


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

class FunctionFeatureExtractor:
    def __init__(self, cursor: Cursor, tu: TranslationUnit):
        self.cursor = cursor
        self.tu = tu
        self.source_path = str(cursor.location.file) if cursor.location and cursor.location.file else ""
        self.result: Dict[str, Any] = {}

    def extract(self) -> Dict[str, Any]:
        c = self.cursor
        self.result = {
            "file": self.source_path,
            "function": _qualified_name(c),
            "display_name": c.displayname,
            "signature": c.type.spelling or c.displayname,
            "return_type": _type_spelling(c.result_type),
            "location": _get_location(c),
            "extent": _get_extent(c),
            "parameters": self._extract_parameters(),
            "loops": [],
            "memory_accesses": [],
            "calls": [],
            "types_used": sorted(set()),
            "variables": [],
            "branches": {"if": 0, "switch": 0, "ternary": 0, "conditions": []},
            "operators": [],
            "attributes": self._extract_attributes(),
        }

        types_used: Set[str] = set()
        operators_seen: Set[str] = set()
        self._walk(c, types_used, operators_seen, loop_stack=[])
        self.result["types_used"] = sorted(t for t in types_used if t)
        self.result["operators"] = sorted(operators_seen)
        return self.result

    def _extract_parameters(self) -> List[Dict[str, Any]]:
        params = []
        for child in self.cursor.get_arguments():
            params.append({
                "name": child.spelling,
                "type": _cursor_type_spelling(child),
                "location": _get_location(child),
            })
        return params

    def _extract_operator(self, cursor: Cursor) -> Optional[str]:
        """Extract operator symbol from a binary/unary/compound-assignment cursor."""
        tokens = list(cursor.get_tokens())
        # Filter out child-extent tokens; operators are the remaining punctuation tokens.
        child_ranges = set()
        for child in cursor.get_children():
            try:
                child_ranges.add((child.extent.start.offset, child.extent.end.offset))
            except Exception:
                pass

        candidate_tokens: List[str] = []
        for tok in tokens:
            tok_spelling = tok.spelling
            # Skip tokens that belong to child cursors
            tok_start = tok.extent.start.offset
            tok_end = tok.extent.end.offset
            if any(tok_start >= s and tok_end <= e for s, e in child_ranges):
                continue
            candidate_tokens.append(tok_spelling)

        # Longest-match against known operators
        known_ops = [
            "<<=", ">>=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
            "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "++", "--",
            "+", "-", "*", "/", "%", "<", ">", "!", "~", "&", "|", "^",
        ]
        for op in known_ops:
            if op in candidate_tokens:
                return op
        # Fallback: return the first non-identifier token
        for tok in candidate_tokens:
            if not tok.isidentifier():
                return tok
        return None

    def _extract_attributes(self) -> List[str]:
        """Best-effort attribute/annotation extraction (e.g., [[clang::optnone]])."""
        attrs = []
        try:
            for child in self.cursor.get_children():
                if child.kind == CursorKind.ANNOTATE_ATTR:
                    attrs.append(child.displayname)
        except Exception:
            pass
        return attrs

    def _walk(
        self,
        cursor: Cursor,
        types_used: Set[str],
        operators_seen: Set[str],
        loop_stack: List[Dict[str, Any]],
    ) -> None:
        """Recursive AST walk collecting features."""
        kind = cursor.kind

        # Collect referenced types
        t = cursor.type
        if t and t.spelling:
            types_used.add(t.spelling)
        rt = cursor.referenced
        if rt and rt.type and rt.type.spelling:
            types_used.add(rt.type.spelling)

        # Operators
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR, CursorKind.UNARY_OPERATOR):
            op = self._extract_operator(cursor)
            if op:
                operators_seen.add(op)

        # Loops
        if kind in (CursorKind.FOR_STMT, CursorKind.WHILE_STMT, CursorKind.DO_STMT):
            loop_info = self._extract_loop(cursor, kind, len(loop_stack))
            self.result["loops"].append(loop_info)
            loop_stack.append(loop_info)

        # Memory accesses
        if kind == CursorKind.ARRAY_SUBSCRIPT_EXPR:
            self.result["memory_accesses"].append(self._extract_array_access(cursor))
        if kind == CursorKind.UNARY_OPERATOR:
            op = _get_text(cursor)
            if op.startswith("*"):
                self.result["memory_accesses"].append(self._extract_pointer_deref(cursor))

        # Calls
        if kind == CursorKind.CALL_EXPR:
            self.result["calls"].append(self._extract_call(cursor))

        # Branches
        if kind == CursorKind.IF_STMT:
            self.result["branches"]["if"] += 1
            cond = self._extract_condition(cursor)
            if cond:
                self.result["branches"]["conditions"].append(cond)
        if kind == CursorKind.SWITCH_STMT:
            self.result["branches"]["switch"] += 1
        if kind == CursorKind.CONDITIONAL_OPERATOR:
            self.result["branches"]["ternary"] += 1

        # Variables
        if kind == CursorKind.VAR_DECL:
            self.result["variables"].append({
                "name": cursor.spelling,
                "type": _cursor_type_spelling(cursor),
                "location": _get_location(cursor),
            })

        # Recurse
        for child in cursor.get_children():
            self._walk(child, types_used, operators_seen, loop_stack)

        # Pop loop stack when leaving
        if kind in (CursorKind.FOR_STMT, CursorKind.WHILE_STMT, CursorKind.DO_STMT):
            if loop_stack:
                loop_stack.pop()

    def _extract_loop(self, cursor: Cursor, kind: CursorKind, depth: int) -> Dict[str, Any]:
        children = list(cursor.get_children())

        loop_type = "for"
        if kind == CursorKind.WHILE_STMT:
            loop_type = "while"
        elif kind == CursorKind.DO_STMT:
            loop_type = "do_while"

        # For `for(init; cond; inc) body`, children are [init, cond, inc, body]
        # For while/do, children are [cond, body]
        cond_cursor = None
        body_cursor = None
        init_cursor = None
        inc_cursor = None

        if kind == CursorKind.FOR_STMT and len(children) >= 4:
            init_cursor, cond_cursor, inc_cursor, body_cursor = children[0], children[1], children[2], children[3]
        elif kind == CursorKind.FOR_STMT and len(children) == 3:
            # missing init
            cond_cursor, inc_cursor, body_cursor = children[0], children[1], children[2]
        elif kind in (CursorKind.WHILE_STMT, CursorKind.DO_STMT) and len(children) >= 2:
            cond_cursor, body_cursor = children[0], children[1]
        elif children:
            body_cursor = children[-1]

        induction_vars: List[str] = []
        if init_cursor:
            induction_vars.extend(self._find_induction_variables(init_cursor))
        if inc_cursor:
            induction_vars.extend(self._find_induction_variables(inc_cursor))
        induction_vars = sorted(set(induction_vars))

        return {
            "type": loop_type,
            "depth": depth,
            "line": cursor.location.line if cursor.location else 0,
            "condition": _get_text(cond_cursor) if cond_cursor else "",
            "increment": _get_text(inc_cursor) if inc_cursor else "",
            "induction_variables": induction_vars,
            "body_lines": self._estimate_lines(body_cursor),
        }

    def _find_induction_variables(self, cursor: Cursor) -> List[str]:
        """Find variables that look like loop counters (declared in init/inc or assigned)."""
        names: List[str] = []
        for child in cursor.walk_preorder():
            if child.kind == CursorKind.DECL_REF_EXPR:
                names.append(child.spelling)
            if child.kind == CursorKind.VAR_DECL:
                names.append(child.spelling)
        return names

    def _estimate_lines(self, cursor: Optional[Cursor]) -> int:
        if cursor is None:
            return 0
        try:
            start = cursor.extent.start.line
            end = cursor.extent.end.line
            return max(0, end - start + 1)
        except Exception:
            return 0

    def _extract_array_access(self, cursor: Cursor) -> Dict[str, Any]:
        children = list(cursor.get_children())
        base = children[0] if children else None
        index = children[1] if len(children) > 1 else None

        base_name = _qualified_name(base) if base else ""
        if not base_name:
            base_name = _get_text(base) if base else ""
        index_text = _get_text(index) if index else ""

        return {
            "kind": "array_subscript",
            "base": base_name,
            "index": index_text,
            "base_type": _cursor_type_spelling(base) if base else "",
            "element_type": _cursor_type_spelling(cursor),
            "location": _get_location(cursor),
            "stride_hint": self._infer_stride(index_text),
        }

    def _extract_pointer_deref(self, cursor: Cursor) -> Dict[str, Any]:
        child = next(iter(cursor.get_children()), None)
        expr_text = _get_text(child) if child else _get_text(cursor)
        return {
            "kind": "pointer_deref",
            "expression": expr_text,
            "type": _cursor_type_spelling(cursor),
            "location": _get_location(cursor),
            "stride_hint": "unknown",
        }

    def _infer_stride(self, index_text: str) -> str:
        """Infer stride pattern from index expression text."""
        s = index_text.strip()
        if not s:
            return "constant"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
            return "unit_stride"
        if re.search(r"\*\s*[A-Za-z_][A-Za-z0-9_]*\s*\+", s) or re.search(r"\+\s*[A-Za-z_][A-Za-z0-9_]*\s*\*", s):
            return "row_major_2d"
        if "*" in s and "+" in s:
            return "strided_2d"
        if "+" in s or "-" in s:
            return "linear_combination"
        return "complex"

    def _extract_call(self, cursor: Cursor) -> Dict[str, Any]:
        callee = cursor.referenced
        callee_name = _qualified_name(callee) if callee else _get_text(cursor)
        args = []
        for arg in cursor.get_arguments():
            args.append({
                "text": _get_text(arg),
                "type": _cursor_type_spelling(arg),
            })
        return {
            "callee": callee_name,
            "location": _get_location(cursor),
            "arg_count": len(args),
            "args": args,
        }

    def _extract_condition(self, cursor: Cursor) -> Optional[Dict[str, Any]]:
        """Extract the condition expression of an if statement."""
        children = list(cursor.get_children())
        if not children:
            return None
        # if(cond) body [else else_clause]
        cond = children[0]
        return {
            "text": _get_text(cond),
            "location": _get_location(cond),
        }


# ---------------------------------------------------------------------------
# File/directory processing
# ---------------------------------------------------------------------------

def _collect_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    exts = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"}
    return sorted(p for p in root.rglob("*") if p.suffix in exts)


def _extract_from_file(path: Path, clang_args: List[str], verbose: bool = False) -> List[Dict[str, Any]]:
    index = clang.cindex.Index.create()
    try:
        tu = index.parse(str(path), clang_args)
    except clang.cindex.TranslationUnitLoadError as exc:
        raise RuntimeError(f"libclang failed to parse {path}: {exc}") from exc

    # Diagnostics: by default only report fatal errors to avoid noise from system headers.
    # With --verbose, report warnings and above.
    threshold = clang.cindex.Diagnostic.Warning if verbose else clang.cindex.Diagnostic.Fatal
    diags = [d for d in tu.diagnostics if d.severity >= threshold]
    if diags:
        for d in diags[:3]:
            print(f"DIAG {path}: {d}", file=sys.stderr)

    results: List[Dict[str, Any]] = []
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind == CursorKind.FUNCTION_DECL and cursor.is_definition():
            # Only top-level definitions in the requested file
            loc = cursor.location
            if loc and loc.file and Path(loc.file.name).resolve() == path.resolve():
                extractor = FunctionFeatureExtractor(cursor, tu)
                results.append(extractor.extract())
        elif cursor.kind in (CursorKind.CXX_METHOD, CursorKind.FUNCTION_TEMPLATE):
            if cursor.is_definition():
                loc = cursor.location
                if loc and loc.file and Path(loc.file.name).resolve() == path.resolve():
                    extractor = FunctionFeatureExtractor(cursor, tu)
                    results.append(extractor.extract())
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract precise algorithmic features from C/C++ sources using libclang."
    )
    ap.add_argument("--src", required=True, help="Source file or directory")
    ap.add_argument("--out", required=True, help="Output JSONL file")
    ap.add_argument("--compile-commands", help="Path to compile_commands.json")
    ap.add_argument("-I", "--include", dest="includes", action="append", default=[], help="Additional include directory")
    ap.add_argument("-D", "--define", dest="defines", action="append", default=[], help="Additional macro definition")
    ap.add_argument("--std", default="-std=c++17", help="C++ standard flag (default: -std=c++17)")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show libclang diagnostics (warnings and above)")
    args = ap.parse_args()

    root = Path(args.src)
    if not root.exists():
        print(f"ERROR: path does not exist: {root}", file=sys.stderr)
        return 1

    files = _collect_files(root)
    if not files:
        print(f"WARNING: no C/C++ source files found under {root}", file=sys.stderr)
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cc_path = Path(args.compile_commands) if args.compile_commands else None

    all_results: List[Dict[str, Any]] = []
    total_functions = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for src in files:
            clang_args = _build_clang_args(src, cc_path, args.includes, args.defines, args.std)
            try:
                feats = _extract_from_file(src, clang_args, verbose=args.verbose)
            except Exception as exc:
                print(f"SKIP {src}: {exc}", file=sys.stderr)
                continue
            total_functions += len(feats)
            all_results.extend(feats)
            if not args.pretty:
                for f in feats:
                    fh.write(json.dumps(f, ensure_ascii=False) + "\n")

        if args.pretty:
            json.dump(all_results, fh, ensure_ascii=False, indent=2)

    print(f"Extracted {total_functions} precise function features from {len(files)} files -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
