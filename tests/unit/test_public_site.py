"""The public documentation export contains only explicitly allowed files."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools.site import build as site


class PublicSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / 'docs/assets').mkdir(parents=True)
        (self.root / 'README.md').write_text('# Demo\n[Install](docs/INSTALLATION.md)\n![demo](docs/assets/demo.png)\n', encoding='utf-8')
        (self.root / 'docs/INSTALLATION.md').write_text('# Install\nSynthetic instructions only.', encoding='utf-8')
        (self.root / 'docs/assets/demo.png').write_bytes(b'synthetic-image-fixture')
        for patcher in [patch.object(site, 'ROOT', self.root),
                        patch.object(site, 'PAGES', {'index': 'README.md', 'install': 'docs/INSTALLATION.md'}),
                        patch.object(site, 'ASSETS', ['demo.png'])]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_build_exports_only_curated_files_and_rewrites_links(self):
        (self.root / '.env').write_text('DO_NOT_EXPORT=synthetic', encoding='utf-8')
        output = site.build()
        names = {path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_file()}
        self.assertEqual(names, {'index.html', 'install.html', 'assets/demo.png',
                                 'style.css', 'manifest.json', '.nojekyll'})
        index = (output / 'index.html').read_text('utf-8')
        self.assertIn('href="install.html"', index)
        self.assertIn('src="assets/demo.png"', index)
        self.assertNotIn('DO_NOT_EXPORT', index)
        self.assertIn('Content-Security-Policy', index)

    def test_readme_html_wrappers_render_markdown_headings_and_images(self):
        (self.root / 'README.md').write_text(
            '<div align="center">\n\n# Wrapped heading\n\n</div>\n'
            '<details><summary>More</summary>\n\n![demo](docs/assets/demo.png)\n\n</details>',
            encoding='utf-8')
        output = site.build()
        page = (output / 'index.html').read_text('utf-8')
        self.assertIn('<h1', page)
        self.assertIn('src="assets/demo.png"', page)

    def test_existing_unapproved_export_file_blocks_publication(self):
        output = self.root / 'outputs/site'
        output.mkdir(parents=True)
        (output / 'private.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            site.build()

    def test_relative_link_cannot_escape_repo(self):
        with self.assertRaisesRegex(ValueError, 'escapes'):
            site.rewrite_links('[bad](../secret.md)', self.root / 'README.md')


if __name__ == '__main__':
    unittest.main()
