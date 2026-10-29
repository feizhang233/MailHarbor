"""Generate CSS from DESIGN.md's JSON-compatible YAML frontmatter."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
tokens = json.loads((root / "DESIGN.md").read_text().split("---", 2)[1])
lines = ["/* Generated from DESIGN.md; run scripts/generate_ui_tokens.py. */", ":root {"]
for group, prefix in [("colors", "color"), ("rounded", "radius"), ("spacing", "space")]:
    for name, value in tokens[group].items():
        lines.append(f"  --{prefix}-{name}: {value};")
for name, value in tokens["typography"].items():
    for field, prefix in [("fontFamily", "font"), ("fontSize", "text"), ("lineHeight", "leading")]:
        lines.append(f"  --{prefix}-{name}: {value[field]};")
lines.append("}\n")
expected = "\n".join(lines)
target = root / "mailhub/static/tokens.css"
if "--check" in sys.argv:
    assert target.read_text() == expected, "Runtime tokens drifted from DESIGN.md"
    print("UI token mapping: passed")
else:
    target.write_text(expected)
    print("Generated mailhub/static/tokens.css")
