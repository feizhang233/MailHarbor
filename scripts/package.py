#!/usr/bin/env python3
"""Package reproducible source or a configured plugin, excluding runtime secrets."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))
from mailhub.config import normalize_origin
# Keep release contents explicit even when packaging an extracted copy without Git.
SOURCE_FILES = (
    ".dockerignore",
    ".env.example",
    ".github/workflows/ci.yml",
    ".gitignore",
    "CONTRACT.md",
    "DESIGN.md",
    "Dockerfile",
    "README.md",
    "UX-CONTRACT.md",
    "V2-CONTRACT.md",
    "compose.yaml",
    "deploy/Caddyfile",
    "docs/ai-connections.md",
    "docs/deployment.md",
    "docs/oauth.md",
    "integrations/claude-desktop.json.example",
    "integrations/codex.config.toml.example",
    "integrations/codex.oauth.toml.example",
    "integrations/cursor.bearer.json.example",
    "integrations/cursor.oauth.json.example",
    "integrations/deepseek-harness.cordis.yml.example",
    "integrations/rest_client.py",
    "mailhub/__init__.py",
    "mailhub/app.py",
    "mailhub/config.py",
    "mailhub/oauth_server.py",
    "mailhub/providers.py",
    "mailhub/replies.py",
    "mailhub/static/app.css",
    "mailhub/static/app.js",
    "mailhub/static/i18n.js",
    "mailhub/static/index.html",
    "mailhub/static/translations.js",
    "mailhub/static/tokens.css",
    "mailhub/stdio_bridge.py",
    "mailhub/store.py",
    "mailhub/sync.py",
    "plugin/README.md",
    "plugin/mcp.json",
    "plugin/plugin.json",
    "premium-ui.json",
    "requirements.txt",
    "scripts/check_ui.py",
    "scripts/ci_tests.py",
    "scripts/generate_ui_tokens.py",
    "scripts/package.py",
    "scripts/setup.py",
    "scripts/stdio_bridge.py",
    "tests/live_fixture.py",
    "tests/live_smoke.py",
    "tests/test_core.py",
    "tests/test_csrf_regression.py",
    "tests/test_iam.py",
    "tests/test_iam_store.py",
    "tests/test_integrations.py",
    "tests/test_oauth_server.py",
    "tests/test_providers.py",
    "tests/test_sync_regression.py",
    "tests/ui-verification.md",
    "tests/ui_logic.cjs",
    "tests/verification.md",
)


def source_files(root: Path):
    for filename in SOURCE_FILES:
        relative = Path(filename)
        path = root / relative
        if path.is_symlink() or any((root / parent).is_symlink() for parent in relative.parents):
            continue
        if path.is_file():
            yield path, relative


def make_source_zip(output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in source_files(ROOT):
            archive.write(path, Path("MailHarbor") / relative)
            count += 1
    return count


def plugin_url(origin: str) -> str:
    try:
        canonical = normalize_origin(origin, allow_loopback_http=False)
    except ValueError:
        raise ValueError("外掛需要不含帳密的 HTTPS 網址。") from None
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
