/* DOM-level test for the Stream tab (gui/stream.js).
 *
 * Loads the real index.html into jsdom, stubs fetch with a fake library, then
 * evaluates app.js and stream.js and drives the page: activate the tab, assert
 * the rails render, click a torrent folder, assert the folder listing renders
 * as kind-grouped rails, and assert that merely browsing never hits the
 * resolve endpoint.
 *
 * Run from the repo root:
 *   NODE_PATH=<node workspace>/node_modules node tests/test_stream_ui.js
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const GUI_DIR = path.resolve(__dirname, '..', 'gui');

let failures = 0;
function check(label, condition) {
    if (condition) {
        console.log('  ok   - ' + label);
    } else {
        failures += 1;
        console.log('  FAIL - ' + label);
    }
}

function fakeEntry(name, kind, isDir, sizeStr) {
    return {
        name,
        is_dir: isDir,
        size: isDir ? 0 : 1024,
        size_str: sizeStr || (isDir ? 'Unknown' : '1KB'),
        kind: isDir ? 'folder' : kind,
        playable: false,
    };
}

const LIBRARY = {
    '/': [fakeEntry('Torrent One', 'folder', true, '3GB')],
    '/Torrent One': [
        fakeEntry('Sub Dir', 'folder', true),
        fakeEntry('a.mkv', 'video', false, '1GB'),
        fakeEntry('cover.jpg', 'image', false, '2KB'),
        fakeEntry('notes.txt', 'other', false, '1KB'),
    ],
    '/Torrent One/Sub Dir': [fakeEntry('b.mkv', 'video', false, '2GB')],
};

function buildDom() {
    const html = fs.readFileSync(path.join(GUI_DIR, 'index.html'), 'utf8');
    const dom = new JSDOM(html, {
        runScripts: 'outside-only',
        url: 'http://localhost:8000/?api_key=test-key',
        pretendToBeVisual: true,
    });
    const { window } = dom;

    const calls = [];
    window.fetch = function (url, options) {
        calls.push({ url: String(url), options: options || {} });
        const body = options && options.body ? JSON.parse(options.body) : {};
        let payload = { success: true };

        if (String(url).includes('/api/stream/list') || String(url).includes('/api/stream/library')) {
            const p = body.path || '/';
            payload = { success: true, path: p, entries: LIBRARY[p] || [] };
        } else if (String(url).includes('/api/stream/resolve')) {
            payload = { success: true, url: 'https://cdn.test/x', kind: 'video', playable: true };
        } else if (String(url).includes('/api/status')) {
            payload = { status: 'idle', logs: [], queue: [], active_downloads: {} };
        }

        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(payload),
            text: () => Promise.resolve(JSON.stringify(payload)),
        });
    };

    window.eval(fs.readFileSync(path.join(GUI_DIR, 'app.js'), 'utf8'));
    window.eval(fs.readFileSync(path.join(GUI_DIR, 'stream.js'), 'utf8'));
    return { window, calls };
}

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 40));

(async function main() {
    const { window, calls } = buildDom();
    const doc = window.document;

    console.log('Stream tab DOM tests');

    check('StreamPage global is registered', typeof window.StreamPage === 'object');

    // --- activate -----------------------------------------------------------
    window.StreamPage.activate();
    await tick();

    check('library fetch was issued',
        calls.some((c) => c.url.includes('/api/stream/library') || c.url.includes('/api/stream/list')));
    check('no resolve call while browsing',
        !calls.some((c) => c.url.includes('/api/stream/resolve')));
    check('breadcrumb shows Library',
        doc.getElementById('stream-breadcrumb').textContent.includes('Library'));
    check('hero shows the library title',
        doc.getElementById('stream-hero-title').textContent === 'Your library');
    check('root rail renders the torrent card',
        doc.querySelectorAll('#stream-body .stream-card').length === 1);
    check('torrent card is marked as a folder',
        doc.querySelector('#stream-body .stream-card').getAttribute('data-kind') === 'folder');

    // --- drill into the torrent --------------------------------------------
    doc.querySelector('#stream-body .stream-card').click();
    await tick();

    const rails = Array.from(doc.querySelectorAll('#stream-body .stream-rail-head h3'))
        .map((el) => el.textContent);
    check('folder listing renders kind-grouped rails',
        rails.includes('Folders') && rails.includes('Videos') &&
        rails.includes('Images') && rails.includes('Other files'));
    check('no resolve call after drilling into a folder',
        !calls.some((c) => c.url.includes('/api/stream/resolve')));
    check('breadcrumb grew to include the torrent',
        doc.getElementById('stream-breadcrumb').textContent.includes('Torrent One'));
    check('back button is now visible',
        doc.getElementById('stream-back-btn').style.display !== 'none');
    check('video card carries the size',
        Array.from(doc.querySelectorAll('.stream-card-meta')).some((el) => el.textContent === '1GB'));

    // --- go up via the breadcrumb ------------------------------------------
    const rootCrumb = doc.querySelector('#stream-breadcrumb [data-nav="/"]');
    check('breadcrumb root is clickable', !!rootCrumb);
    rootCrumb.click();
    await tick();
    check('going up returns to the library rail',
        doc.querySelectorAll('#stream-body .stream-card').length === 1);

    // --- empty folder -------------------------------------------------------
    window.StreamPage.load('/Torrent One/Sub Dir');
    await tick();
    check('empty-rail state still renders the sub-folder file',
        doc.getElementById('stream-body').textContent.includes('b.mkv'));

    console.log(failures === 0 ? '\nALL DOM TESTS PASSED' : '\n' + failures + ' DOM TEST(S) FAILED');
    process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
    console.error('DOM test crashed:', err);
    process.exit(2);
});
