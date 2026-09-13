/* DOM-level test for Stream playback (gui/stream.js + gui/index.html).
 *
 * Loads the real index.html into jsdom with a fake library and a fake resolve
 * endpoint, then drives the player: assert nothing is resolved while browsing,
 * that clicking a file resolves exactly that file, that a natively playable
 * video gets a proxied <video>, that a remuxable MKV also gets a <video> but
 * with the no-seeking notice, that an unplayable codec gets the panel instead,
 * that the image lightbox pages lazily, and that closing tears media down.
 *
 * Run from the repo root:
 *   NODE_PATH=<node workspace>/node_modules node tests/test_stream_player.js
 */

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

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

const IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'];
const PLAYABLE_EXTS = ['mp4', 'm4v', 'webm', 'mov', 'ogg'];

function classify(name) {
    const ext = String(name).split('.').pop().toLowerCase();
    if (IMAGE_EXTS.includes(ext)) {
        return { kind: 'image', playable: true, mode: 'direct' };
    }
    if (PLAYABLE_EXTS.includes(ext)) {
        return { kind: 'video', playable: true, mode: 'direct' };
    }
    if (ext === 'txt') {
        return { kind: 'other', playable: false, mode: 'direct' };
    }
    if (name === 'hevc.mkv') {
        // Container is fine to swap, but the browser cannot decode HEVC.
        return { kind: 'video', playable: false, mode: 'external',
                 video_codec: 'hevc', audio_codec: 'dts' };
    }
    if (ext === 'mkv') {
        // H.264 + AAC: remuxable, so it plays as a live conversion.
        return { kind: 'video', playable: false, mode: 'remux',
                 video_codec: 'h264', audio_codec: 'aac', duration: 5400 };
    }
    return { kind: 'video', playable: false, mode: 'external' };
}

function fakeEntry(name, isDir) {
    const cls = isDir ? { kind: 'folder', playable: false, mode: 'direct' } : classify(name);
    return {
        name,
        is_dir: !!isDir,
        size: isDir ? 0 : 1024,
        size_str: isDir ? 'Unknown' : '1KB',
        kind: isDir ? 'folder' : cls.kind,
        playable: cls.playable,
    };
}

const LIBRARY = {
    '/': [fakeEntry('Torrent One', true)],
    '/Torrent One': [
        fakeEntry('movie.mp4', false),
        fakeEntry('show.mkv', false),
        fakeEntry('hevc.mkv', false),
        fakeEntry('a.jpg', false),
        fakeEntry('b.jpg', false),
        fakeEntry('c.jpg', false),
        fakeEntry('notes.txt', false),
    ],
};

function buildDom() {
    const html = fs.readFileSync(path.join(GUI_DIR, 'index.html'), 'utf8');
    const virtualConsole = new VirtualConsole();
    // jsdom has no media pipeline: swallow the "not implemented" noise.
    virtualConsole.on('jsdomError', (err) => {
        if (!/not implemented/i.test(String(err && err.message))) {
            console.error(err);
        }
    });

    const dom = new JSDOM(html, {
        runScripts: 'outside-only',
        url: 'http://localhost:8000/?api_key=test-key',
        pretendToBeVisual: true,
        virtualConsole,
    });
    const { window } = dom;

    const calls = [];
    const resolvePaths = [];

    window.fetch = function (url, options) {
        const target = String(url);
        calls.push({ url: target, options: options || {} });
        const body = options && options.body ? JSON.parse(options.body) : {};
        let payload = { success: true };

        if (target.includes('/api/stream/list') || target.includes('/api/stream/library')) {
            const p = body.path || '/';
            payload = { success: true, path: p, entries: LIBRARY[p] || [] };
        } else if (target.includes('/api/stream/resolve')) {
            resolvePaths.push(body.path);
            const name = String(body.path).split('/').pop();
            const cls = classify(name);
            payload = {
                success: true,
                url: 'https://cdn.test/' + encodeURIComponent(name),
                kind: cls.kind,
                playable: cls.playable,
                mode: cls.mode,
                duration: cls.duration || null,
                video_codec: cls.video_codec || null,
                audio_codec: cls.audio_codec || null,
                expires_at: Math.floor(Date.now() / 1000) + 1800,
            };
        } else if (target.includes('/api/status')) {
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
    return { window, calls, resolvePaths };
}

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 40));

function cardByName(doc, name) {
    return Array.from(doc.querySelectorAll('#stream-body .stream-card'))
        .find((el) => el.getAttribute('data-path') === name);
}

function key(window, doc, keyName) {
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: keyName, bubbles: true }));
}

(async function main() {
    const { window, resolvePaths } = buildDom();
    const doc = window.document;
    const overlay = doc.getElementById('stream-player');
    const stage = doc.getElementById('stream-player-stage');

    console.log('Stream player DOM tests');

    check('player overlay exists in the markup', !!overlay && !!stage);

    window.StreamPage.activate();
    await tick();
    doc.querySelector('#stream-body .stream-card').click();     // into the torrent
    await tick();

    // --- browsing resolves nothing -----------------------------------------
    check('drilling into a folder resolves nothing', resolvePaths.length === 0);
    check('overlay stays hidden while browsing', overlay.style.display === 'none');

    // --- natively playable video -------------------------------------------
    let before = resolvePaths.length;
    cardByName(doc, 'movie.mp4').click();
    await tick();

    check('clicking a video resolves exactly that path',
        resolvePaths.length === before + 1 &&
        resolvePaths[resolvePaths.length - 1] === '/Torrent One/movie.mp4');

    let video = stage.querySelector('video');
    check('a playable video mounts a <video> element', !!video);

    let videoSrc = video ? video.getAttribute('src') : '';
    check('video src is the same-origin content proxy',
        videoSrc.indexOf('/api/stream/content') === 0);
    check('video src carries the vfs path',
        decodeURIComponent(videoSrc).includes('path=/Torrent One/movie.mp4'));
    check('video src carries the api key', videoSrc.includes('api_key=test-key'));
    check('video src points at the CDN directly nowhere',
        videoSrc.indexOf('cdn.test') === -1);
    check('overlay is visible during playback', overlay.style.display === 'flex');
    check('kind chip says Video',
        doc.getElementById('stream-player-kind').textContent === 'Video');
    check('title shows the file name',
        doc.getElementById('stream-player-title').textContent === 'movie.mp4');
    check('a directly playable video shows no notice',
        stage.querySelector('.stream-player-notice') === null);

    // --- link actions -------------------------------------------------------
    check('Open in new tab points at the direct CDN link',
        doc.getElementById('stream-player-open').getAttribute('href') === 'https://cdn.test/movie.mp4');
    const dlHref = doc.getElementById('stream-player-download').getAttribute('href');
    check('Download goes through the proxy with download=1',
        dlHref.includes('/api/stream/content') && dlHref.includes('download=1'));

    // --- close tears the stream down ---------------------------------------
    key(window, doc, 'Escape');
    await tick();

    check('Escape closes the overlay', overlay.style.display === 'none');
    check('closing clears the media element', stage.querySelector('video') === null);
    check('closing leaves no src behind', stage.innerHTML === '');

    // --- same file again is served from the link cache ----------------------
    before = resolvePaths.length;
    cardByName(doc, 'movie.mp4').click();
    await tick();
    check('re-opening the same file does not re-resolve', resolvePaths.length === before);
    doc.getElementById('stream-player-close').click();
    await tick();
    check('the close button also closes the overlay', overlay.style.display === 'none');

    // --- remuxable MKV ------------------------------------------------------
    before = resolvePaths.length;
    cardByName(doc, 'show.mkv').click();
    await tick();

    check('clicking the .mkv resolves it', resolvePaths.length === before + 1);
    video = stage.querySelector('video');
    check('a remuxable .mkv DOES mount a <video>', !!video);
    check('a remuxable .mkv does not show the unsupported panel',
        stage.querySelector('.stream-player-note') === null);
    check('kind chip flags the remux',
        doc.getElementById('stream-player-kind').textContent === 'Video · remuxed');

    videoSrc = video ? video.getAttribute('src') : '';
    check('the remuxed video uses the content proxy',
        videoSrc.indexOf('/api/stream/content') === 0);
    check('the remuxed video does NOT ask for raw mode',
        !videoSrc.includes('raw='), 'the server decides the mode, not the client');
    check('the remuxed video does not point at the CDN',
        videoSrc.indexOf('cdn.test') === -1);

    const notice = stage.querySelector('.stream-player-notice');
    check('a remuxed video shows a notice', !!notice);
    check('the notice explains the remux',
        (notice ? notice.textContent : '').includes('Remuxed to MP4'));
    check('the notice warns that seeking is unavailable',
        (notice ? notice.textContent : '').toLowerCase().includes('seeking is unavailable'));
    check('the notice names the codecs',
        (notice ? notice.textContent : '').includes('H264') &&
        (notice ? notice.textContent : '').includes('AAC'));
    check('the notice reports the duration',
        (notice ? notice.textContent : '').includes('1:30:00'));
    check('the remuxed video still offers a download',
        doc.getElementById('stream-player-download')
            .getAttribute('href').includes('download=1'));

    key(window, doc, 'Escape');
    await tick();

    // --- unplayable codec ---------------------------------------------------
    before = resolvePaths.length;
    cardByName(doc, 'hevc.mkv').click();
    await tick();

    check('clicking the HEVC file resolves it', resolvePaths.length === before + 1);
    check('an HEVC file does NOT mount a <video>', stage.querySelector('video') === null);
    check('an HEVC file shows the panel', !!stage.querySelector('.stream-player-note'));
    check('the panel names the offending codec',
        stage.textContent.includes('HEVC'));
    check('the panel explains the codec is the problem',
        stage.textContent.includes('codec cannot play'));
    check('the panel offers copy / open / download actions',
        !!stage.querySelector('[data-action="player-copy"]') &&
        !!stage.querySelector('[data-action="player-open"]') &&
        !!stage.querySelector('[data-action="player-download"]'));
    check('the panel download link uses the proxy with download=1',
        stage.querySelector('[data-action="player-download"]')
            .getAttribute('href').includes('download=1'));
    check('kind chip flags the external file',
        doc.getElementById('stream-player-kind').textContent === 'Video · external');

    key(window, doc, 'Escape');
    await tick();

    // --- image lightbox -----------------------------------------------------
    before = resolvePaths.length;
    cardByName(doc, 'b.jpg').click();
    await tick();

    const img = stage.querySelector('img');
    check('clicking an image mounts an <img>', !!img);
    check('the image src is the content proxy',
        (img ? img.getAttribute('src') : '').indexOf('/api/stream/content') === 0);
    check('the image src carries the vfs path',
        decodeURIComponent(img.getAttribute('src')).includes('path=/Torrent One/b.jpg'));
    check('only the clicked image was resolved',
        resolvePaths.length === before + 1 &&
        resolvePaths[resolvePaths.length - 1] === '/Torrent One/b.jpg');
    check('lightbox shows the counter for 3 images',
        (stage.querySelector('.stream-lightbox-counter') || {}).textContent === '2 / 3');
    check('lightbox renders prev and next buttons',
        !!stage.querySelector('[data-nav-dir="prev"]') &&
        !!stage.querySelector('[data-nav-dir="next"]'));

    // --- lazy paging --------------------------------------------------------
    before = resolvePaths.length;
    stage.querySelector('[data-nav-dir="next"]').click();
    await tick();
    check('next resolves only the next image, lazily',
        resolvePaths.length === before + 1 &&
        resolvePaths[resolvePaths.length - 1] === '/Torrent One/c.jpg');
    check('next advanced the counter',
        stage.querySelector('.stream-lightbox-counter').textContent === '3 / 3');
    check('next swapped the image',
        decodeURIComponent(stage.querySelector('img').getAttribute('src'))
            .includes('path=/Torrent One/c.jpg'));

    key(window, doc, 'ArrowRight');
    await tick();
    check('arrow key wraps around to the first image',
        decodeURIComponent(stage.querySelector('img').getAttribute('src'))
            .includes('path=/Torrent One/a.jpg'));

    key(window, doc, 'ArrowLeft');
    await tick();
    check('arrow key wraps backwards to the last image',
        stage.querySelector('.stream-lightbox-counter').textContent === '3 / 3');
    check('an image never mounts a <video>', stage.querySelector('video') === null);

    key(window, doc, 'Escape');
    await tick();
    check('Escape closes the lightbox', overlay.style.display === 'none');

    // --- other files --------------------------------------------------------
    cardByName(doc, 'notes.txt').click();
    await tick();
    check('a plain file shows the no-preview panel',
        stage.textContent.includes('No inline preview'));
    check('a plain file still offers a download link',
        !!stage.querySelector('[data-action="player-download"]'));

    key(window, doc, 'Escape');
    await tick();

    // --- resolve failure ----------------------------------------------------
    // Drop the cached link so the next click has to hit the (failing) endpoint.
    window.StreamPage.links.clear();
    const originalFetch = window.fetch;
    window.fetch = function (url, options) {
        if (String(url).includes('/api/stream/resolve')) {
            return Promise.resolve({
                ok: true,
                status: 200,
                json: () => Promise.resolve({ success: false, error: 'Link generation failed.' }),
                text: () => Promise.resolve(''),
            });
        }
        return originalFetch(url, options);
    };

    cardByName(doc, 'movie.mp4').click();
    await tick();

    check('a failed resolve surfaces the error state',
        stage.textContent.includes('Link generation failed.'));
    check('the error state offers a retry',
        !!stage.querySelector('[data-action="player-retry"]'));
    check('a failed resolve mounts no media', stage.querySelector('video') === null);

    window.fetch = originalFetch;
    stage.querySelector('[data-action="player-retry"]').click();
    await tick();
    check('retry recovers and mounts the player', !!stage.querySelector('video'));

    key(window, doc, 'Escape');
    await tick();

    // --- backdrop click -----------------------------------------------------
    cardByName(doc, 'movie.mp4').click();
    await tick();
    check('overlay is open before the backdrop click', overlay.style.display === 'flex');
    overlay.click();
    await tick();
    check('clicking the backdrop closes the overlay', overlay.style.display === 'none');

    console.log(failures === 0 ? '\nALL PLAYER TESTS PASSED' : '\n' + failures + ' PLAYER TEST(S) FAILED');
    process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
    console.error('Player DOM test crashed:', err);
    process.exit(2);
});
