"""Dependency-free markup and privacy contract checks; not browser/a11y proof."""
from html.parser import HTMLParser
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
html = (root / "mailhub/static/index.html").read_text()
js = (root / "mailhub/static/app.js").read_text()
css = (root / "mailhub/static/app.css").read_text()


class CheckHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.inputs = []
        self.labels = set()
        self.references = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "id" in data:
            assert data["id"] not in self.ids, f"Duplicate ID: {data['id']}"
            self.ids.add(data["id"])
        if tag == "form":
            assert "novalidate" in data, "Product forms require owned validation"
        if tag == "label" and "for" in data:
            self.labels.add(data["for"])
        if tag in {"input", "select"}:
            self.inputs.append(data)
        if tag in {"script", "link"}:
            target = data.get("src", data.get("href", ""))
            assert not target.startswith("https:"), "No external assets permitted"
        for key in ("aria-describedby", "aria-labelledby"):
            self.references.extend(data.get(key, "").split())


parser = CheckHTML()
parser.feed(html)
for input_ in parser.inputs:
    assert input_.get("id") in parser.labels or input_.get("aria-label"), f"Missing input label: {input_}"
for ref in parser.references:
    assert ref in parser.ids, f"Broken ARIA reference: {ref}"
for referenced in re.findall(r'\$\("([\w-]+)"\)', js):
    assert referenced in parser.ids, f"JavaScript references missing element: {referenced}"
for forbidden in [r"\.innerHTML\s*=", r"\b(?:alert|confirm|prompt)\s*\(", r"localStorage\.", r"sessionStorage\."]:
    assert not re.search(forbidden, js), f"Unsafe UI pattern: {forbidden}"
assert 'textContent = String(text)' in js
assert "AbortController" in js and "searchEpoch" in js
assert "compositionstart" in js and "compositionend" in js
assert "X-CSRF-Token" in js and 'credentials: "same-origin"' in js
assert "scrollbar-color" in css and "::-webkit-scrollbar" in css
assert "prefers-reduced-motion" in css and "forced-colors" in css
assert 'document.title =' in js
print(f"UI static contract: passed ({len(parser.ids)} IDs, {len(parser.inputs)} labeled fields)")
