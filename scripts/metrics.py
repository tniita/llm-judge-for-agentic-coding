"""PR の共通祖先と head から機械的な品質指標を計算し、差分（Δ）を出す。対象は Python。

使い方:
  python scripts/metrics.py --repo path/to/repo --base main --head agent-branch --out metrics.json

指標（テストコードは除外する）:
  循環的複雑度   McCabe（radon）の全関数の合計。ΔV
  重複コード     正規化した 6 行単位の重複行数と重複率
  結合度         パッケージ内の import グラフでの Ce（依存する数）/ Ca（依存される数）/ I = Ce/(Ca+Ce)
  変更の波及しやすさ  Propagation Cost（到達可能性行列の密度。MacCormack ら）。ΔPC
  モジュール性   パッケージ（ディレクトリ）単位で分割したときの Newman のモジュラリティ Q。ΔQ

ファイルごとの解析結果は git の blob SHA をキーに --cache-dir へ保存する。ベースコミットの解析結果は
キャッシュから取得され、今回の変更で中身が変わったファイルだけが再計算される。
"""

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict, deque
from pathlib import Path


ANALYZER_VERSION = "3"
WINDOW = 6
DEFAULT_EXCLUDES = ["tests/*", "*/tests/*", "test/*", "*/test/*", "test_*.py", "*/test_*.py", "*_test.py", "conftest.py", "*/conftest.py"]
COMMENT_RE = re.compile(r"^\s*#")


# ---------- git ----------

def git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, input=input).stdout


def comparison_refs(repo: Path, base: str, head: str) -> tuple[str, str]:
    head_sha = git(repo, "rev-parse", f"{head}^{{commit}}").decode().strip()
    try:
        base_sha = git(repo, "merge-base", base, head_sha).decode().strip()
    except subprocess.CalledProcessError:
        raise SystemExit(f"{base} と {head} に共通祖先がありません。PR の比較元を確認してください") from None
    return base_sha, head_sha


def list_python_blobs(repo: Path, rev: str, excludes: list[str]) -> dict[str, str]:
    """rev に含まれる .py ファイルの path → blob SHA（除外パターンに当たるものは除く）。"""
    out = git(repo, "ls-tree", "-r", "-z", rev).decode("utf-8")
    blobs = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        _, kind, sha = meta.split()
        if kind == "blob" and path.endswith(".py") and not any(fnmatch.fnmatch(path, p) for p in excludes):
            blobs[path] = sha
    return blobs


def read_blobs(repo: Path, shas: list[str]) -> dict[str, str]:
    if not shas:
        return {}
    out = git(repo, "cat-file", "--batch", input=("\n".join(shas) + "\n").encode())
    result, pos = {}, 0
    for sha in shas:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        start = header_end + 1
        result[sha] = out[start:start + size].decode("utf-8", errors="replace")
        pos = start + size + 1
    return result


# ---------- ファイル単位の解析（キャッシュ対象） ----------

def analyze_source(source: str) -> dict:
    from radon.visitors import ComplexityVisitor  # make_row.py が format_delta だけ使うときは不要なので遅延 import

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"parse_error": f"{e.msg} (line {e.lineno})", "functions": [], "imports": [], "lines": []}

    functions = []

    def collect(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
            if not isinstance(node, ast.ClassDef):
                function = ComplexityVisitor.from_ast(node).functions[0]
                functions.append({"name": ".".join(scope), "line": node.lineno, "complexity": function.complexity})
        for child in ast.iter_child_nodes(node):
            collect(child, scope)

    collect(tree)

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [{"module": a.name, "names": [], "level": 0} for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append({"module": node.module or "", "names": [a.name for a in node.names], "level": node.level})

    # 重複検出用: 空行・コメント行・import 行を除き、空白を正規化した行（行番号付き）
    lines = []
    for no, raw in enumerate(source.splitlines(), start=1):
        text = " ".join(raw.split())
        if not text or COMMENT_RE.match(text) or text.startswith(("import ", "from ")):
            continue
        lines.append([no, hashlib.sha1(text.encode()).hexdigest()[:16]])
    return {"functions": functions, "imports": imports, "lines": lines}


class FileCache:
    def __init__(self, cache_dir: Path | None):
        self.dir = cache_dir
        self.hits = self.misses = 0
        self.memo: dict[str, dict] = {}
        self.used: set[str] = set()
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            for stale in self.dir.glob("v*-*.json"):
                if not stale.name.startswith(f"v{ANALYZER_VERSION}-"):
                    stale.unlink()

    def _path(self, sha: str) -> Path:
        return self.dir / f"v{ANALYZER_VERSION}-{sha}.json"

    def analyze(self, repo: Path, blobs: dict[str, str]) -> dict[str, dict]:
        result, missing = {}, []
        for path, sha in blobs.items():
            self.used.add(sha)
            cached = self._path(sha) if self.dir else None
            if sha in self.memo:
                result[path] = self.memo[sha]
            elif cached and cached.exists():
                result[path] = self.memo[sha] = json.loads(cached.read_text(encoding="utf-8"))
                self.hits += 1
            else:
                missing.append((path, sha))
        sources = read_blobs(repo, sorted({sha for _, sha in missing}))
        for path, sha in missing:
            if sha not in self.memo:
                self.memo[sha] = analyze_source(sources[sha])
                self.misses += 1
                if self.dir:
                    self._path(sha).write_text(json.dumps(self.memo[sha]), encoding="utf-8")
            result[path] = self.memo[sha]
        return result

    def prune(self) -> int:
        """今回の base / head で使わなかった解析結果を消す（CI のキャッシュが際限なく増えないように）。"""
        if not self.dir:
            return 0
        removed = 0
        for path in self.dir.glob(f"v{ANALYZER_VERSION}-*.json"):
            if path.stem.split("-", 1)[1] not in self.used:
                path.unlink()
                removed += 1
        return removed


# ---------- コード全体の指標 ----------

def module_names(paths: list[str]) -> dict[str, str]:
    """path → dotted module 名。__init__.py のあるディレクトリをパッケージとみなす。"""
    files = set(paths)
    names = {}
    for path in paths:
        parts = path[:-3].split("/")
        # パッケージのルートまで上がる（__init__.py の無いディレクトリで止める）
        start = len(parts) - 1
        while start > 0 and "/".join(parts[:start]) + "/__init__.py" in files:
            start -= 1
        mod = parts[start:]
        if mod[-1] == "__init__":
            mod = mod[:-1]
        names[path] = ".".join(mod) if mod else parts[-2] if len(parts) > 1 else parts[-1]
    return names


def resolve_import(imp: dict, current: str, is_package: bool, modules: set[str]) -> set[str]:
    if imp["level"]:
        base = current.split(".") if is_package else current.split(".")[:-1]
        base = base[: len(base) - (imp["level"] - 1)] if imp["level"] > 1 else base
        target = ".".join([*base, imp["module"]] if imp["module"] else base)
    else:
        target = imp["module"]
    found = set()
    candidates = [f"{target}.{n}" if target else n for n in imp["names"]] if imp["names"] else []
    for c in candidates:
        if c in modules:
            found.add(c)
    if not found:
        parts = target.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in modules:
                found.add(prefix)
                break
    found.discard(current)
    return found


def build_graph(analyses: dict[str, dict]) -> tuple[dict[str, str], dict[str, set[str]]]:
    names = module_names(sorted(analyses))
    modules = set(names.values())
    graph = {m: set() for m in modules}
    for path, a in analyses.items():
        current = names[path]
        is_package = path.endswith("__init__.py")
        for imp in a["imports"]:
            graph[current] |= resolve_import(imp, current, is_package, modules)
    return names, graph


def propagation_cost(graph: dict[str, set[str]]) -> float | None:
    n = len(graph)
    if not n:
        return None
    reachable = 0
    for start in graph:
        seen, queue = {start}, deque([start])
        while queue:
            for nxt in graph[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        reachable += len(seen)
    return reachable / (n * n)


def modularity(graph: dict[str, set[str]], is_package: set[str]) -> float | None:
    edges = {tuple(sorted((a, b))) for a, deps in graph.items() for b in deps if a != b}
    m = len(edges)
    if not m:
        return None

    def community(mod: str) -> str:
        return mod if mod in is_package else mod.rpartition(".")[0]

    inner, degree = Counter(), Counter()
    for a, b in edges:
        ca, cb = community(a), community(b)
        degree[ca] += 1
        degree[cb] += 1
        if ca == cb:
            inner[ca] += 1
    return sum(inner[c] / m - (degree[c] / (2 * m)) ** 2 for c in degree)


def duplication(analyses: dict[str, dict]) -> tuple[dict, dict[str, list[tuple[str, int]]]]:
    windows = defaultdict(list)
    for path, a in analyses.items():
        lines = a["lines"]
        for i in range(len(lines) - WINDOW + 1):
            key = hashlib.sha1("".join(h for _, h in lines[i:i + WINDOW]).encode()).hexdigest()
            windows[key].append((path, i))
    dup = set()
    for locations in windows.values():
        if len(locations) > 1:
            for path, i in locations:
                dup.update((path, j) for j in range(i, i + WINDOW))
    total = sum(len(a["lines"]) for a in analyses.values())
    locs = {k: [(p, analyses[p]["lines"][i][0]) for p, i in v] for k, v in windows.items() if len(v) > 1}
    return {"duplicated_lines": len(dup), "total_lines": total, "duplication_rate": len(dup) / total if total else 0.0}, locs


def snapshot(analyses: dict[str, dict]) -> dict:
    names, graph = build_graph(analyses)
    packages = {names[p] for p in analyses if p.endswith("__init__.py")}
    ca = Counter(dep for deps in graph.values() for dep in deps)
    coupling = {
        m: {"ce": len(graph[m]), "ca": ca[m],
            "i": len(graph[m]) / (len(graph[m]) + ca[m]) if graph[m] or ca[m] else 0.0}
        for m in graph
    }
    # 同じスコープの同名定義（property の getter/setter など）は 1 件にまとめる。
    # 出現順で区別すると、間に定義を足しただけで別の関数と対応づいてしまうため
    functions = {}
    for path, a in analyses.items():
        for f in a["functions"]:
            key = f"{path}::{f['name']}"
            if key in functions:
                functions[key]["complexity"] += f["complexity"]
                functions[key]["definitions"] += 1
            else:
                functions[key] = {"path": path, **f, "definitions": 1}
    dup, dup_locs = duplication(analyses)
    complexities = [f["complexity"] for a in analyses.values() for f in a["functions"]]
    summary = {
        "files": len(analyses),
        "modules": len(graph),
        "complexity_total": sum(complexities),
        "complexity_max": max(complexities, default=0),
        **dup,
        "coupling_edges": sum(len(d) for d in graph.values()),
        "mean_instability": sum(c["i"] for c in coupling.values()) / len(coupling) if coupling else 0.0,
        "propagation_cost": propagation_cost(graph),
        "modularity": modularity(graph, packages),
    }
    parse_errors = {p: a["parse_error"] for p, a in analyses.items() if a.get("parse_error")}
    if parse_errors:
        summary = {key: None for key in summary}
    return {"summary": summary, "functions": functions, "coupling": coupling, "dup_locs": dup_locs,
            "names": names, "parse_errors": parse_errors}


def diff_value(before, after):
    if before is None or after is None:
        return None
    return after - before


def compare(before: dict, after: dict, changed_paths: set[str]) -> dict:
    sb, sa = before["summary"], after["summary"]
    delta = {k: diff_value(sb[k], sa[k]) for k in sb}
    if before["parse_errors"] or after["parse_errors"]:
        return {"delta": delta, "functions": [], "modules": [], "new_duplicates": []}

    functions = []
    for key in sorted(set(before["functions"]) | set(after["functions"])):
        fb, fa = before["functions"].get(key), after["functions"].get(key)
        cb, ca_ = (fb or {}).get("complexity"), (fa or {}).get("complexity")
        if cb != ca_:
            ref = fa or fb
            functions.append({"path": ref["path"], "name": ref["name"], "line": ref["line"],
                              "before": cb, "after": ca_, "delta": (ca_ or 0) - (cb or 0)})
    functions.sort(key=lambda f: -abs(f["delta"]))

    changed_modules = {after["names"].get(p) or before["names"].get(p) for p in changed_paths} - {None}
    modules = []
    for m in sorted(set(before["coupling"]) | set(after["coupling"])):
        cb, ca_ = before["coupling"].get(m), after["coupling"].get(m)
        if cb == ca_ and m not in changed_modules:
            continue
        cb, ca_ = cb or {"ce": 0, "ca": 0, "i": 0.0}, ca_ or {"ce": 0, "ca": 0, "i": 0.0}
        d = {k: ca_[k] - cb[k] for k in ("ce", "ca", "i")}
        if any(d.values()):
            modules.append({"module": m, "before": cb, "after": ca_, "delta": d})

    new_dups = [locs for k, locs in after["dup_locs"].items() if k not in before["dup_locs"]]
    new_dups = [sorted(set(l)) for l in new_dups if any(p in changed_paths for p, _ in l)]
    # 連続するウィンドウは同じ重複ブロックなので、先頭だけ残す
    seen, blocks = set(), []
    for locs in sorted(new_dups):
        sig = tuple(p for p, _ in locs)
        if any(sig == s and all(abs(a[1] - b[1]) <= WINDOW for a, b in zip(locs, prev)) for s, prev in seen):
            continue
        seen.add((sig, tuple(locs)))
        blocks.append([{"path": p, "line": n} for p, n in locs])

    return {
        "delta": delta,
        "functions": functions,
        "modules": modules,
        "new_duplicates": blocks,
    }


def compute(repo: Path, base: str, head: str, excludes: list[str], cache_dir: Path | None,
            prune_cache: bool = False) -> dict:
    base_sha, head_sha = comparison_refs(repo, base, head)
    cache = FileCache(cache_dir)
    blobs_b = list_python_blobs(repo, base_sha, excludes)
    blobs_a = list_python_blobs(repo, head_sha, excludes)
    before = snapshot(cache.analyze(repo, blobs_b))
    after = snapshot(cache.analyze(repo, blobs_a))
    changed = {p for p in set(blobs_b) | set(blobs_a) if blobs_b.get(p) != blobs_a.get(p)}
    result = compare(before, after, changed)
    pruned = cache.prune() if prune_cache else 0
    return {
        "base": base_sha,
        "head": head_sha,
        "excludes": excludes,
        "changed_files": sorted(changed),
        "before": before["summary"],
        "after": after["summary"],
        **result,
        "status": "unavailable" if before["parse_errors"] or after["parse_errors"] else "completed",
        "parse_errors": {side: errors for side, errors in
                         (("base", before["parse_errors"]), ("head", after["parse_errors"])) if errors},
        "cache": {"hits": cache.hits, "misses": cache.misses, "pruned": pruned},
    }


# ---------- Judge に渡すテキスト（Δ のみ） ----------

def _signed(v, digits=0, pt=False) -> str:
    if v is None:
        return "計算不可"
    if pt:
        return f"{v * 100:+.{digits}f}pt"
    return f"{v:+.{digits}f}" if digits else f"{v:+d}" if isinstance(v, int) else f"{v:+g}"


def format_delta(metrics: dict, top: int = 5) -> str:
    if metrics.get("status") == "unavailable" or metrics.get("parse_errors"):
        return ("機械的な品質指標は計測不能です。増減や改善を判断せず、人間レビューで確認してください。\n"
                + json.dumps(metrics.get("parse_errors", {}), ensure_ascii=False))
    d = metrics["delta"]
    lines = [
        "（CI が PR の共通祖先との比較で機械的に計算した指標の差分。静的解析による参考値）",
        f"- 循環的複雑度（McCabe）の合計: ΔV = {_signed(d['complexity_total'])}（関数ごとの最大値 {_signed(d['complexity_max'])}）",
    ]
    for f in metrics["functions"][:top]:
        before = "新規" if f["before"] is None else f["before"]
        after = "削除" if f["after"] is None else f["after"]
        lines.append(f"  - {f['path']}:{f['line']} {f['name']}: {before} → {after}（{_signed(f['delta'])}）")
    lines.append(f"- 重複コード: 重複行 {_signed(d['duplicated_lines'])}（重複率 {_signed(d['duplication_rate'], 1, pt=True)}）")
    for block in metrics["new_duplicates"][:top]:
        lines.append("  - 新たな重複: " + " ↔ ".join(f"{b['path']}:{b['line']}" for b in block))
    lines.append(f"- 結合度: モジュール間の依存 {_signed(d['coupling_edges'])} 本、不安定度 I の平均 {_signed(d['mean_instability'], 3)}")
    for m in metrics["modules"][:top]:
        md = m["delta"]
        lines.append(f"  - {m['module']}: ΔCe {_signed(md['ce'])}, ΔCa {_signed(md['ca'])}, ΔI {_signed(md['i'], 2)}")
    lines.append(f"- 変更の波及しやすさ: ΔPC = {_signed(d['propagation_cost'], 3)}")
    lines.append(f"- モジュール性: ΔQ = {_signed(d['modularity'], 3)}")
    if metrics.get("parse_errors"):
        lines.append("- 構文解析できなかったファイル（指標から除外）: " + ", ".join(metrics["parse_errors"]))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--exclude", action="append", default=[], help="除外する path の glob（複数指定可）。既定でテストは除外")
    p.add_argument("--cache-dir", type=Path, default=Path(".metrics-cache"))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--prune-cache", action="store_true", help="今回使わなかった解析結果をキャッシュから消す（CI 向け）")
    p.add_argument("--out", type=Path, default=Path("metrics.json"))
    args = p.parse_args()

    metrics = compute(args.repo, args.base, args.head, DEFAULT_EXCLUDES + args.exclude,
                      None if args.no_cache else args.cache_dir, args.prune_cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_delta(metrics))
    print(f"→ {args.out}（キャッシュ: 再利用 {metrics['cache']['hits']} / 再計算 {metrics['cache']['misses']} ファイル）")


if __name__ == "__main__":
    main()
