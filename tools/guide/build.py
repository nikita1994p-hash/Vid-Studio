"""Builds «Vid Studio — инструкция от А до Я.pdf» from guide.md with Chrome or Edge (headless print).

    python tools/guide/build.py

guide.md is light Markdown: '# ' section (new page), '## ' subsection, '- ' and '1. ' lists, '| a | b |' tables
(first row = header), ``` blocks, **bold**, `code`. A paragraph starting with ✅, ⚠ or 💡 becomes a note box.
"""
import html
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PDF = ROOT / "Vid Studio — инструкция от А до Я.pdf"
BROWSERS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1d1d1f; }
h1 { font-size: 19pt; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 3px solid #e50914; break-before: page; }
h2 { font-size: 13.5pt; margin: 18px 0 6px; color: #b00710; break-after: avoid; }
p { margin: 6px 0; }
ul, ol { margin: 6px 0 6px 22px; padding: 0; } li { margin: 3px 0; }
table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 9.6pt; break-inside: auto; }
tr { break-inside: avoid; }
th { background: #1d1d1f; color: #fff; text-align: left; padding: 5px 7px; }
td { border-bottom: 1px solid #ddd; padding: 5px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #f6f6f7; }
code { font-family: Consolas, monospace; font-size: 9.3pt; background: #f0f0f2; padding: 0 3px; border-radius: 3px; }
pre { font-family: Consolas, monospace; font-size: 9pt; background: #f0f0f2; padding: 8px 10px; border-radius: 6px; white-space: pre-wrap; }
.note { border-left: 4px solid; border-radius: 6px; padding: 7px 10px; margin: 9px 0; break-inside: avoid; }
.ok { border-color: #2ba640; background: #edf8ef; } .warn { border-color: #e8a400; background: #fdf6e3; }
.tip { border-color: #2f8fe0; background: #ebf4fc; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .logo { font-size: 40pt; font-weight: 700; } .cover .logo span { color: #e50914; }
.cover .sub { font-size: 16pt; margin-top: 8px; } .cover .meta { color: #666; margin-top: 30px; }
.toc { columns: 2; } .toc a { color: #1d1d1f; text-decoration: none; } .toc div { margin: 3px 0; }
"""


def inline(text):
    out = []
    for i, part in enumerate(re.split(r"(`[^`]+`)", text)):
        if i % 2:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(part, quote=False)))
    return "".join(out)


def convert(md):
    body, toc, lines, i = [], [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            body.append(f"<pre>{html.escape(chr(10).join(lines[i + 1:j]))}</pre>")
            i = j + 1
        elif line.startswith("# "):
            n = len(toc) + 1
            toc.append(f'<div><a href="#s{n}">{inline(line[2:])}</a></div>')
            body.append(f'<h1 id="s{n}">{inline(line[2:])}</h1>')
            i += 1
        elif line.startswith("## "):
            body.append(f"<h2>{inline(line[3:])}</h2>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head = "".join(f"<th>{inline(c)}</th>" for c in rows[0])
            rest = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rest}</tbody></table>")
        elif re.match(r"(- |\d+\. )", line):
            tag = "ul" if line.startswith("- ") else "ol"
            items = []
            while i < len(lines) and re.match(r"(- |\d+\. )", lines[i]):
                items.append(f"<li>{inline(re.sub(r'^(- |\d+\. )', '', lines[i]))}</li>")
                i += 1
            body.append(f"<{tag}>{''.join(items)}</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||- |\d+\. |```)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            text = " ".join(para)
            kind = {"✅": "ok", "⚠": "warn", "💡": "tip"}.get(text[:1])
            body.append(f'<div class="note {kind}">{inline(text)}</div>' if kind else f"<p>{inline(text)}</p>")
    return toc, "\n".join(body)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    md = (HERE / "guide.md").read_text(encoding="utf-8")
    meta, _, rest = md.partition("\n---\n")
    title, sub, *info = [x for x in meta.splitlines() if x.strip()]
    toc, body = convert(rest)
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body>
<div class="cover"><div class="logo"><span>▶</span> Vid Studio</div><div class="sub">{inline(sub)}</div>
<div class="meta">{"<br>".join(inline(x) for x in info)}</div></div>
<h1 style="border:0">Содержание</h1><div class="toc">{"".join(toc)}</div>
{body}</body></html>"""
    out = HERE / "guide.html"
    out.write_text(page, encoding="utf-8")
    browser = next((b for b in BROWSERS if pathlib.Path(b).exists()), None)
    if not browser:
        sys.exit("Chrome or Edge not found")
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={PDF}", out.as_uri()], check=True, capture_output=True, timeout=120)
    print(f"{PDF.name}: {PDF.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
