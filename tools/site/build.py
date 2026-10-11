"""Build an allowlisted, static documentation site; never copy runtime directories."""
from __future__ import annotations
import html
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit, unquote
import markdown

ROOT = Path(__file__).resolve().parents[2]
PAGES = {'index': 'README.md', 'install': 'docs/INSTALLATION.md',
         'develop': 'docs/DEVELOPMENT.md', 'architecture': 'docs/ARCHITECTURE.md',
         'automation': 'docs/maintainers/AUTOMATION.md', 'releases': 'docs/maintainers/RELEASES.md',
         'security': 'SECURITY.md'}
ASSETS = ['mjobs-pipeline-demo.png', 'mjobs-sources-demo.png', 'mjobs-dashboard-fixture.png']
REPO = 'https://github.com/MulattoTech/MJobs'
LINK = re.compile(r'([!]?(?:\[[^\]\n]*\]))\(([^)\n]+)\)')
STYLE = '''
:root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.6}
body{margin:0;background:#0e1720;color:#e2ebf0}a{color:#66d4ce}a:hover{text-decoration:none}
header{border-bottom:1px solid #33434f;padding:1rem max(1rem,calc((100vw - 1060px)/2));background:#15242e}
nav{display:flex;flex-wrap:wrap;gap:1.3rem;font-size:.95rem}nav a{text-decoration:none}
main{max-width:1060px;margin:0 auto;padding:2rem 1.3rem 4rem}h1,h2,h3{line-height:1.2;color:#f4f8fa}
h1{font-size:2.9rem}h2{margin-top:2.6rem}img{max-width:100%;height:auto;border-radius:8px}
pre{background:#091017;border:1px solid #33434f;border-radius:8px;padding:1rem;overflow:auto}
code{font-family:ui-monospace,monospace;font-size:.9em}blockquote{border-left:4px solid #d6b062;margin:1rem 0;padding:.8rem 1rem;background:#23303a}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}td,th{padding:.7rem;border-bottom:1px solid #33434f;text-align:left}
summary{cursor:pointer;padding:.7rem 0;font-weight:600}footer{font-size:.85rem;color:#a5b9c6;margin-top:3rem}
'''


def rewrite_links(text: str, source: Path) -> str:
    reverse = {(ROOT / value).resolve(): name for name, value in PAGES.items()}
    def replace(match):
        label, target = match.groups()
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not parts.path:
            return match.group(0)
        path = (source.parent / unquote(parts.path)).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('Documentation link escapes source tree')
        suffix = '#' + parts.fragment if parts.fragment else ''
        if path in reverse:
            link = reverse[path] + '.html' + suffix
        elif path.parent == (ROOT / 'docs/assets') and path.name in ASSETS:
            link = 'assets/' + path.name
        else:
            link = REPO + '/blob/main/' + path.relative_to(ROOT).as_posix() + suffix
        return label + '(' + link + ')'
    return LINK.sub(replace, text)


def build() -> Path:
    output = ROOT / 'outputs/site'
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or not output.resolve().is_relative_to(ROOT / 'outputs'):
        raise ValueError('Unsafe site output directory')
    allowed = {name + '.html' for name in PAGES} | {'assets', 'style.css', '.nojekyll', 'manifest.json'}
    unexpected = {p.name for p in output.iterdir()} - allowed
    if unexpected:
        raise ValueError('Refusing to publish unexpected files: ' + str(sorted(unexpected)))
    (output / 'assets').mkdir(exist_ok=True)
    for name in ASSETS:
        source = ROOT / 'docs/assets' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Invalid public screenshot: ' + name)
        shutil.copyfile(source, output / 'assets' / name)
    (output / 'style.css').write_text(STYLE, encoding='utf-8')
    links = ' '.join(f'<a href="{name}.html">{html.escape(name.title())}</a>' for name in PAGES)
    for name, relative in PAGES.items():
        source = ROOT / relative
        text = rewrite_links(source.read_text('utf-8'), source)
        # GitHub parses Markdown inside common README wrappers; Python-Markdown
        # needs an explicit md_in_html opt-in to render their headings/images.
        text = re.sub(r'<(div|details)([^>]*)>',
                      lambda match: '<' + match[1] + match[2] + ' markdown="1">'
                      if 'markdown=' not in match[2] else match[0], text)
        body = markdown.markdown(text, extensions=['fenced_code', 'tables', 'toc', 'md_in_html'])
        title = 'MJobs / ' + name.title()
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' https: data:; style-src \'self\'; base-uri \'none\'">'
                f'<title>{html.escape(title)}</title><link rel="stylesheet" href="style.css"></head>'
                f'<body><header><nav>{links}</nav></header><main>{body}'
                '<footer>MJobs developer preview. External application submission is off. '
                f'<a href="{REPO}">Source and exact CI results</a>. No analytics or applicant data.</footer>'
                '</main></body></html>')
        (output / (name + '.html')).write_text(page, encoding='utf-8')
    (output / '.nojekyll').write_text('', encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({'pages': PAGES, 'assets': ASSETS}, indent=2) + '\n', encoding='utf-8')
    actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
    expected = {name+'.html' for name in PAGES} | {'style.css', '.nojekyll', 'manifest.json'} | {'assets/'+name for name in ASSETS}
    if actual != expected:
        raise ValueError('Site export differs from the explicit allowlist')
    print(f'Static site: {len(PAGES)} pages, {len(ASSETS)} reviewed screenshots, no runtime export.')
    return output


if __name__ == '__main__':
    build()
