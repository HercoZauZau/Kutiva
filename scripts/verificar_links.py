#!/usr/bin/env python3
"""Verifica links do catálogo KUTIVA e actualiza apenas os ícones no README.

Sem dependências externas. Distingue indisponibilidade comprovada de bloqueios
anti-bot/erros temporários. Falhas HTTP 404/410 externas exigem duas execuções
separadas por pelo menos 24 horas para receberem o ícone de link quebrado.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

LINK_LINE = re.compile(
    r"^(?P<prefix>[ \t]*[*-][ \t]+):(?P<status>heavy_check_mark|x|warning):"
    r"(?P<space>[ \t]+)\[(?P<name>.+)\]\((?P<url>.+)\)(?P<suffix>[^\r\n]*)$"
)
HEADERS = {
    "User-Agent": "KUTIVA-LinkMonitor/1.0 (+GitHub Actions; public data catalogue)",
    "Accept": "text/html,application/json,*/*;q=0.8",
}
MISSING_CODES = {404, 410}
GOOD_MARK = "heavy_check_mark"
BAD_MARK = "x"
UNKNOWN_MARK = "warning"


def http_status(url: str, method: str, timeout: float) -> tuple[int | None, str]:
    """Devolve código HTTP, sem descarregar o conteúdo do conjunto de dados."""
    try:
        req = Request(url, headers=HEADERS, method=method)
        with urlopen(req, timeout=timeout) as response:
            return response.status, ""
    except HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return None, str(exc)[:150]


def inspect_link(url: str, root: Path, timeout: float) -> tuple[str, str]:
    """Resultado: 'ok', 'missing', 'uncertain' ou 'local_missing'."""
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"}:
        # Alguns servidores rejeitam HEAD, por isso GET serve de confirmação.
        status, reason = http_status(url, "HEAD", timeout)
        if status is not None and 200 <= status < 400:
            return "ok", f"HTTP {status}"
        status, reason = http_status(url, "GET", timeout)
        if status is not None and 200 <= status < 400:
            return "ok", f"HTTP {status}"
        if status in MISSING_CODES:
            return "missing", f"HTTP {status}"
        return "uncertain", reason or (f"HTTP {status}" if status else "Sem resposta")

    if parsed.scheme or parsed.netloc:
        return "uncertain", f"Esquema não suportado: {parsed.scheme or '//' }"

    path = unquote(parsed.path)
    if not path:  # Link só para uma âncora do próprio README.
        return "ok", "Âncora local (sem validação da âncora)"
    target = (root / path.lstrip("/")).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return "uncertain", "Caminho fora do repositório"
    return ("ok", "Ficheiro/pasta local") if target.exists() else ("local_missing", "Ficheiro/pasta inexistente")


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("pending"), dict):
        raise ValueError(f"Estado incompatível: {path}")
    return data["pending"]


def choose_mark(
    url: str,
    result: str,
    previous: dict,
    today: datetime,
) -> tuple[str, dict | None]:
    if result == "ok":
        return GOOD_MARK, None
    if result == "local_missing":
        return BAD_MARK, None
    if result == "uncertain":
        return UNKNOWN_MARK, None

    old = previous.get(url, {})
    first = old.get("first_missing")
    if first:
        try:
            first_day = datetime.fromisoformat(first)
            if first_day.tzinfo is None:
                first_day = first_day.replace(tzinfo=timezone.utc)
            if today - first_day >= timedelta(hours=24):
                return BAD_MARK, {"first_missing": first}
        except ValueError:
            pass
    return UNKNOWN_MARK, {"first_missing": today.isoformat(timespec="seconds")}


def run(readme: Path, state_path: Path, workers: int, timeout: float) -> int:
    if not readme.is_file():
        raise FileNotFoundError(f"README não encontrado: {readme}")
    root = readme.resolve().parent
    content = readme.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    matches = [(i, LINK_LINE.match(line.rstrip("\r\n"))) for i, line in enumerate(lines)]
    matches = [(i, m) for i, m in matches if m]
    if not matches:
        raise RuntimeError("Nenhum link com indicador encontrado. README não foi alterado.")

    links = {m.group("url") for _, m in matches}
    print(f"A verificar {len(links)} links únicos em {len(matches)} recursos...", flush=True)
    previous = read_state(state_path)
    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(inspect_link, url, root, timeout): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                results[url] = ("uncertain", f"Erro imprevisto: {str(exc)[:100]}")

    now = datetime.now(timezone.utc)
    pending: dict[str, dict] = {}
    stats = {GOOD_MARK: 0, UNKNOWN_MARK: 0, BAD_MARK: 0}
    changes = 0
    for idx, match in matches:
        url = match.group("url")
        outcome, reason = results[url]
        mark, new_state = choose_mark(url, outcome, previous, now)
        if new_state is not None:
            pending[url] = new_state
        stats[mark] += 1
        if mark != match.group("status"):
            original = lines[idx]
            # Substitui só o indicador, sem tocar nos nomes e URLs.
            start, end = match.span("status")
            lines[idx] = original[:start] + mark + original[end:]
            changes += 1
            print(f"  {match.group('status')} -> {mark}: {match.group('name')} ({reason})")

    updated = "".join(lines)
    if "* :warning: *Por confirmar / acesso bloqueado*" not in updated:
        updated = updated.replace(
            "* :x: *Link Quebrado*",
            "* :x: *Link Quebrado*\n* :warning: *Por confirmar / acesso bloqueado*",
            1,
        )
    if updated != content:
        readme.write_text(updated, encoding="utf-8")

    # Guarda apenas falhas definitivas pendentes/confirmadas. Não cria commits
    # por causa de verificações bem-sucedidas e repetidas sem mudanças.
    if pending != previous or state_path.exists():
        if pending:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"version": 1, "pending": dict(sorted(pending.items()))}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        elif state_path.exists():
            state_path.unlink()

    summary = (
        f"Links analisados: {len(matches)} | Disponíveis: {stats[GOOD_MARK]} | "
        f"Inconclusivos/por confirmar: {stats[UNKNOWN_MARK]} | "
        f"Quebrados confirmados: {stats[BAD_MARK]} | Ícones actualizados: {changes}"
    )
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fp:
            fp.write(f"## KUTIVA — Verificação semanal\n\n{summary}\n\n")
            fp.write("Um erro 403, 429, 5xx ou de ligação é inconclusivo. "
                     "Um 404/410 externo precisa de duas verificações separadas por 24 horas.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--state", type=Path, default=Path(".github/link-status.json"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0:
        parser.error("--workers e --timeout têm de ser positivos")
    return run(args.readme, args.state, args.workers, args.timeout)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
