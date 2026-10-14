"""Production UI appearance, persistence, keyboard and cross-tab acceptance."""
from pathlib import Path
from copy import deepcopy
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from tests.browser import test_source_relationship_graph as graph_fixture


class ThemeBrowserTests(unittest.TestCase):
    def test_appearance_survives_reload_and_navigation_and_tracks_system(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-theme-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'theme.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(dict(PROFILE))
            store.save_profile({**deepcopy(PROFILE), 'id': 'other', 'name': 'Other synthetic profile',
                                'email': 'other@example.test'})
            graph_fixture.SourceRelationshipGraphBrowserTests._seed_graph(store)
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(create_app(
                store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0',
                operation_mode='PERSONAL'), log_level='critical'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            external = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated theme API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(color_scheme='light', viewport={'width': 1400, 'height': 1000})

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route('**/*', local_only)
                        page = context.new_page()
                        page.goto(origin)
                        select = page.get_by_label('Appearance', exact=True)
                        expect(select).to_have_value('system')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                        page.emulate_media(color_scheme='dark')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                        select.select_option('light')
                        page.emulate_media(color_scheme='dark')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                        select.focus()
                        select.press('End')
                        select.press('Enter')
                        expect(select).to_have_value('dark')
                        expect(select).to_be_focused()
                        page.reload()
                        expect(select).to_have_value('dark')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                        sibling = context.new_page()
                        sibling.goto(origin)
                        expect(sibling.get_by_label('Appearance', exact=True)).to_have_value('dark')
                        select.select_option('light')
                        expect(sibling.locator('html')).to_have_attribute('data-theme', 'light')
                        for theme in ('light', 'dark'):
                            select.select_option(theme)
                            page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                            page.reload()
                            for tab in ('Profile', 'Sources', 'Pipeline', 'Activity', 'AI Settings'):
                                page.get_by_role('navigation', name='Main navigation').get_by_role('button', name=tab, exact=True).click()
                                expect(page.locator('html')).to_have_attribute('data-theme', theme)
                                self.assert_readable(page, 'main')
                                self.assert_readable(page, '.sidebar nav button[aria-current]')
                                if page.locator('.side-note p').is_visible():
                                    self.assert_readable(page, '.side-note p')
                                for node in page.locator('main .panel, main input, main select').all()[:8]:
                                    if node.is_visible():
                                        self.assert_readable_node(node)
                                for node in page.locator('main label, main p, main small, main .status').all()[:35]:
                                    if node.is_visible():
                                        self.assert_readable_node(node)
                                if tab == 'Sources':
                                    graph = page.get_by_role('region', name='Source relationship graph')
                                    expect(graph).to_contain_text('Synthetic Graph Employer')
                                    for node in graph.locator('svg text').all():
                                        self.assert_readable_node(node, foreground='fill')
                                    graph.locator('svg [role="button"]').first.focus()
                                    expect(graph.locator('svg [role="button"]').first).to_be_focused()
                                    self.assertEqual(graph.locator('svg [role="button"] rect').first.evaluate(
                                        "node => getComputedStyle(node).strokeWidth"), '3px')
                            Path('outputs').mkdir(exist_ok=True)
                            page.screenshot(path=f'outputs/theme-{theme}-desktop.png', full_page=True)
                            page.set_viewport_size({'width': 390, 'height': 844})
                            expect(select).to_be_visible()
                            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                            page.screenshot(path=f'outputs/theme-{theme}-mobile.png', full_page=True)
                            page.set_viewport_size({'width': 1400, 'height': 1000})
                        # Invalid saved values and storage removal recover to system.
                        page.evaluate("localStorage.setItem('mjobs-theme','invalid')")
                        page.reload()
                        expect(select).to_have_value('system')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                        sibling.evaluate('localStorage.clear()')
                        expect(select).to_have_value('system')
                        # Quota/privacy failures keep the control usable without
                        # claiming persistence. Other application storage is untouched.
                        page.evaluate("""() => {const original = Storage.prototype.setItem;
                            Storage.prototype.setItem = function(key,value) {
                              if(key==='mjobs-theme') throw new Error('synthetic quota');
                              return original.call(this,key,value);
                            };}""")
                        select.select_option('light')
                        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                        expect(page.get_by_role('status').filter(has_text='this visit')).to_be_visible()
                        self.assertEqual(external, [])
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), 'Theme API leaked')

    def assert_readable(self, page, selector):
        self.assert_readable_node(page.locator(selector))

    def assert_readable_node(self, node, foreground='color'):
        colors = node.evaluate("""(element,foreground) => {
            const fg=getComputedStyle(element)[foreground];
            let bg='rgb(255, 255, 255)';
            for(let current=element;current;current=current.parentElement) {
              const value=getComputedStyle(current).backgroundColor;
              if(value!=='rgba(0, 0, 0, 0)'&&value!=='transparent'){bg=value;break;}
            }
            if(element.tagName==='text') {
              const rect=element.parentElement.querySelector('rect');
              if(rect) bg=getComputedStyle(rect).fill;
            }
            return [fg,bg];
        }""", foreground)
        def luminance(color):
            channels = [int(value) / 255 for value in re.findall(r'\d+', color)[:3]]
            linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in channels]
            return sum(x * weight for x, weight in zip(linear, [.2126, .7152, .0722]))
        values = sorted(map(luminance, colors))
        self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 4.5, colors)
