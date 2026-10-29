#!/usr/bin/env python3
"""Package reproducible source or a configured plugin, excluding runtime secrets."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mailhub.config import normalize_origin
ROOT_FILES = {
    "README.md", "CONTRACT.md", "V2-CONTRACT.md", "DESIGN.md", "UX-CONTRACT.md", "premium-ui.json",
    "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore", ".env.example",
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "pytest.ini",
}
SOURCE_DIRS = {"mailhub", "scripts", "deploy", "integrations", "plugin", "tests", "docs", ".github"}
ALLOWED_SUFFIXES = {".py", ".html", ".css", ".js", ".mjs", ".cjs", ".json", ".md", ".toml", ".yaml", ".yml", ".example", ".svg", ".txt"}


def source_files(root: Path):
    files = []
    for filename in ROOT_FILES:
        path = root / filename
        if path.is_file() and not path.is_symlink():
            files.append((path, path.relative_to(root)))
    for directory in SOURCE_DIRS:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for current, subdirs, names in os.walk(base, followlinks=False):
            subdirs[:] = sorted(name for name in subdirs if not name.startswith(".") and
                name not in {"__pycache__", "node_modules", "data", "dist", "build"} and
                not (Path(current) / name).is_symlink())
            for name in names:
                path = Path(current) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                if path.suffix not in ALLOWED_SUFFIXES and path.name != "Caddyfile":
                    continue
                files.append((path, path.relative_to(root)))
    yield from sorted(files, key=lambda item: str(item[1]))


def make_source_zip(output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in source_files(ROOT):
            archive.write(path, Path("MailHarbor") / relative)
            count += 1
    return count


def plugin_url(origin: str) -> str:
    canonical = normalize_origin(origin)
    if not canonical.startswith("https://"):
        raise ValueError("外掛需要不含帳密的 HTTPS 網址。")
    return canonical + "/mcp"


def make_plugin(output: Path, origin: str) -> None:
    endpoint = plugin_url(origin)
    if output.exists():
        raise ValueError("輸出目錄已存在，未覆寫。請選擇新的輸出目錄。")
    output.mkdir(parents=True)
    shutil.copyfile(ROOT / "plugin/plugin.json", output / "plugin.json")
    manifest = json.loads((ROOT / "plugin/mcp.json").read_text(encoding="utf-8"))
    manifest["mcpServers"]["mailharbor"]["url"] = endpoint
    manifest["mcpServers"]["mailharbor"]["extensions"]["com.openai"]["auth"]["resource"] = endpoint
    (output / "mcp.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(ROOT / "plugin/README.md", output / "README.md")
    # Standard Codex config remains a separate, directly usable connection path.
    for name in ("codex.oauth.toml.example", "codex.config.toml.example"):
        config = (ROOT / "integrations" / name).read_text(encoding="utf-8")
        config = config.replace('"https://mail.example.com/mcp"', json.dumps(endpoint))
        (output / name).write_text(config, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/MailHarbor.zip")
    parser.add_argument("--plugin-url", help="Generate a plugin folder for this HTTPS origin instead of a source ZIP")
    args = parser.parse_args()
    try:
        if args.plugin_url:
            if args.output.suffix == ".zip":
                raise ValueError("產生外掛時，--output 請指定目錄，例如 dist/mailharbor-plugin。")
            make_plugin(args.output, args.plugin_url)
            print(f"外掛範本已產生：{args.output}；未安裝、未上架。")
        else:
            count = make_source_zip(args.output)
            print(f"已封裝 {count} 個來源檔案：{args.output}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
