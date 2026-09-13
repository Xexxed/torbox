/* Stream tab — a lazy media browser over the TorBox library.
 *
 * Rendering a folder NEVER resolves a link: /api/stream/list returns only
 * already-scraped metadata (name, size, kind). A file's CDN link is resolved
 * on click only, through /api/stream/resolve, and playback is proxied
 * same-origin through /api/stream/content.
 *
 * Depends on globals from app.js: escapeHtml(), escapeAttr(), showToast().
 */
(function () {
    'use strict';

    const state = {
        path: '/',
        entries: [],
        error: null,
        loading: false,
        activated: false,
        cache: new Map(),        // path -> entries
        pending: new Set(),      // paths with a resolve in flight
    };

    const GLYPHS = {
        folder: '<path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/>',
        video: '<path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/>',
        image: '<path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/>',
        other: '<path d="M6 2c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6H6zm7 7V3.5L18.5 9H13z"/>',
    };

    const RAILS = [
        { key: 'folder', title: 'Folders' },
        { key: 'video', title: 'Videos' },
        { key: 'image', title: 'Images' },
        { key: 'other', title: 'Other files' },
    ];

    function $(id) {
        return document.getElementById(id);
    }

    function parentPath(path) {
        const parts = String(path).split('/').filter(Boolean);
        parts.pop();
        return parts.length ? '/' + parts.join('/') : '/';
    }

    function baseName(path) {
        const parts = String(path).split('/').filter(Boolean);
        return parts.length ? parts[parts.length - 1] : 'Library';
    }

    function glyph(kind) {
        return GLYPHS[kind] || GLYPHS.other;
    }

    async function fetchListing(path) {
        const res = await fetch('/api/stream/list', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
        let data = null;
        try {
            data = await res.json();
        } catch (e) {
            throw new Error('The server returned an unreadable response.');
        }
        if (!res.ok || !data.success) {
            throw new Error((data && data.error) || ('Request failed (' + res.status + ')'));
        }
        return data;
    }

    function renderBreadcrumb() {
        const nav = $('stream-breadcrumb');
        if (!nav) return;
        const parts = String(state.path).split('/').filter(Boolean);
        const crumbs = [{ label: 'Library', path: '/' }];
        let acc = '';
        parts.forEach((part) => {
            acc += '/' + part;
            crumbs.push({ label: part, path: acc });
        });

        nav.innerHTML = crumbs.map((c, i) => {
            const last = i === crumbs.length - 1;
            const label = escapeHtml(c.label);
            if (last) {
                return '<span class="stream-crumb stream-crumb--current">' + label + '</span>';
            }
            return '<button class="stream-crumb" data-nav="' + escapeAttr(c.path) + '">' + label + '</button>' +
                '<span class="stream-crumb-sep" aria-hidden="true">/</span>';
        }).join('');

        const back = $('stream-back-btn');
        if (back) back.style.display = state.path === '/' ? 'none' : '';
    }

    function renderHero() {
        const titleEl = $('stream-hero-title');
        const subEl = $('stream-hero-sub');
        if (!titleEl || !subEl) return;

        const files = state.entries.filter((e) => !e.is_dir);
        const folders = state.entries.length - files.length;
        const videos = files.filter((e) => e.kind === 'video').length;
        const images = files.filter((e) => e.kind === 'image').length;

        titleEl.textContent = state.path === '/' ? 'Your library' : baseName(state.path);

        if (state.loading) {
            subEl.textContent = 'Reading folder…';
            return;
        }
        if (state.path === '/') {
            subEl.textContent = state.entries.length
                ? state.entries.length + ' torrent' + (state.entries.length === 1 ? '' : 's') + '. Pick one to look inside.'
                : 'No torrents found yet. Sign in and refresh.';
            return;
        }
        const bits = [];
        if (folders) bits.push(folders + ' folder' + (folders === 1 ? '' : 's'));
        if (videos) bits.push(videos + ' video' + (videos === 1 ? '' : 's'));
        if (images) bits.push(images + ' image' + (images === 1 ? '' : 's'));
        subEl.textContent = bits.length ? bits.join(' · ') : 'This folder is empty.';
    }

    function cardMarkup(entry) {
        const kind = entry.is_dir ? 'folder' : (entry.kind || 'other');
        const meta = entry.is_dir ? 'Folder' : (entry.size_str && entry.size_str !== 'Unknown' ? entry.size_str : '');
        return (
            '<button class="stream-card stream-card--' + escapeAttr(kind) + '"' +
            ' data-path="' + escapeAttr(entry.name) + '"' +
            ' data-dir="' + (entry.is_dir ? '1' : '0') + '"' +
            ' data-kind="' + escapeAttr(kind) + '"' +
            ' title="' + escapeAttr(entry.name) + '">' +
            '<span class="stream-card-poster">' +
            '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">' + glyph(kind) + '</svg>' +
            (kind === 'video' || kind === 'image'
                ? '<span class="stream-card-badge">' + escapeHtml(kind) + '</span>'
                : '') +
            '</span>' +
            '<span class="stream-card-name">' + escapeHtml(entry.name) + '</span>' +
            '<span class="stream-card-meta">' + escapeHtml(meta) + '</span>' +
            '</button>'
        );
    }

    function railMarkup(title, entries) {
        return (
            '<section class="stream-rail">' +
            '<div class="stream-rail-head"><h3>' + escapeHtml(title) + '</h3>' +
            '<span class="stream-rail-count">' + entries.length + '</span></div>' +
            '<div class="stream-rail-track">' + entries.map(cardMarkup).join('') + '</div>' +
            '</section>'
        );
    }

    function skeletonMarkup() {
        let cards = '';
        for (let i = 0; i < 6; i++) {
            cards += '<div class="stream-card stream-card--skeleton"><span class="stream-card-poster"></span>' +
                '<span class="stream-skeleton-line"></span><span class="stream-skeleton-line short"></span></div>';
        }
        return '<section class="stream-rail"><div class="stream-rail-head"><h3>Loading</h3></div>' +
            '<div class="stream-rail-track">' + cards + '</div></section>';
    }

    function render() {
        renderBreadcrumb();
        renderHero();

        const body = $('stream-body');
        if (!body) return;

        if (state.loading) {
            body.innerHTML = skeletonMarkup();
            return;
        }

        if (state.error) {
            body.innerHTML =
                '<div class="stream-message stream-message--error">' +
                '<p>' + escapeHtml(state.error) + '</p>' +
                '<button class="btn btn-secondary btn-sm" data-action="retry">Retry</button>' +
                '</div>';
            return;
        }

        if (!state.entries.length) {
            body.innerHTML =
                '<div class="stream-message">' +
                '<p>' + (state.path === '/' ? 'No torrents found in your library.' : 'This folder is empty.') + '</p>' +
                '</div>';
            return;
        }

        const groups = { folder: [], video: [], image: [], other: [] };
        state.entries.forEach((entry) => {
            const key = entry.is_dir ? 'folder' : (groups[entry.kind] ? entry.kind : 'other');
            groups[key].push(entry);
        });

        body.innerHTML = RAILS
            .filter((rail) => groups[rail.key].length)
            .map((rail) => railMarkup(rail.title, groups[rail.key]))
            .join('');
    }

    function childPath(name) {
        return (state.path === '/' ? '' : state.path) + '/' + name;
    }

    async function load(path, options) {
        const opts = options || {};
        state.path = path;
        state.error = null;

        if (!opts.force && state.cache.has(path)) {
            state.entries = state.cache.get(path);
            state.loading = false;
            render();
            return;
        }

        state.loading = true;
        state.entries = [];
        render();

        try {
            const data = await fetchListing(path);
            state.entries = data.entries || [];
            state.cache.set(path, state.entries);
        } catch (e) {
            state.entries = [];
            state.error = (e && e.message) || String(e);
        } finally {
            state.loading = false;
            render();
        }
    }

    function onBodyClick(event) {
        const actionEl = event.target.closest('[data-action="retry"]');
        if (actionEl) {
            load(state.path, { force: true });
            return;
        }

        const card = event.target.closest('.stream-card');
        if (!card || card.classList.contains('stream-card--skeleton')) return;

        const name = card.getAttribute('data-path');
        const path = childPath(name);
        if (card.getAttribute('data-dir') === '1') {
            load(path);
            return;
        }
        // Images get a lightbox with prev/next over the images in this folder.
        const images = state.entries.filter((e) => !e.is_dir && e.kind === 'image');
        openFile(path, name, {
            siblings: images.map((e) => ({ name: e.name, path: childPath(e.name) })),
            index: images.findIndex((e) => e.name === name),
        });
    }

    /* ---- Playback ---------------------------------------------------------
     * This is the only place a file's link is ever resolved, and only because
     * the user clicked that exact file. Playback itself is proxied same-origin
     * through /api/stream/content so the browser gets Range support and the
     * CDN link never has to be exposed to the media element.
     */

    const links = new Map();   // path -> { url, kind, playable, expires_at }

    const player = {
        path: null,
        name: null,
        kind: null,
        siblings: null,        // [{ name, path }] for the image lightbox
        index: -1,
        retried: false,
    };

    function apiKey() {
        try {
            const fromUrl = new URLSearchParams(window.location.search).get('api_key');
            return fromUrl || window.localStorage.getItem('torbox_api_key') || '';
        } catch (e) {
            return '';
        }
    }

    // Encode per segment so the path stays readable in logs and proxies.
    function encodeVfsPath(path) {
        return String(path).split('/').map(encodeURIComponent).join('/');
    }

    function contentUrl(path, options) {
        const opts = options || {};
        let url = '/api/stream/content?path=' + encodeVfsPath(path);
        const key = apiKey();
        if (key) url += '&api_key=' + encodeURIComponent(key);
        if (opts.download) url += '&download=1';
        return url;
    }

    async function resolveLink(path, force) {
        if (!force) {
            const cached = links.get(path);
            // Treat a link as stale 30s before the server says it expires.
            if (cached && (!cached.expires_at || cached.expires_at * 1000 > Date.now() + 30000)) {
                return cached;
            }
        }
        const res = await fetch('/api/stream/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
        let data = null;
        try {
            data = await res.json();
        } catch (e) {
            throw new Error('The server returned an unreadable response.');
        }
        if (!res.ok || !data.success) {
            throw new Error((data && data.error) || ('Could not resolve this file (' + res.status + ')'));
        }
        const record = {
            url: data.url,
            kind: data.kind || 'other',
            // 'direct'   -- the browser plays the file as-is (seekable)
            // 'remux'    -- Matroska-style container, relayed through FFmpeg
            //               as fragmented MP4 (not seekable)
            // 'external' -- neither; offer copy/download/open instead
            mode: data.mode || (data.playable ? 'direct' : 'external'),
            // Why the mode was chosen, so the panel can name the real cause:
            // 'native' | 'remuxable' | 'codec' | 'no-ffmpeg' | 'unprobeable'
            reason: data.reason || null,
            playable: !!data.playable,
            duration: typeof data.duration === 'number' ? data.duration : null,
            video_codec: data.video_codec || null,
            audio_codec: data.audio_codec || null,
            expires_at: data.expires_at || 0,
        };
        links.set(path, record);
        return record;
    }

    function playerEl() { return $('stream-player'); }
    function stageEl() { return $('stream-player-stage'); }

    function setActions(show) {
        ['stream-player-copy', 'stream-player-open', 'stream-player-download'].forEach((id) => {
            const el = $(id);
            if (el) el.hidden = !show;
        });
    }

    function openOverlay() {
        const overlay = playerEl();
        if (!overlay) return;
        overlay.style.display = 'flex';
        overlay.classList.add('active');
    }

    function showOverlay(name, message) {
        const title = $('stream-player-title');
        const kind = $('stream-player-kind');
        if (title) title.textContent = name || 'File';
        if (kind) kind.textContent = 'Resolving';
        setActions(false);
        const stage = stageEl();
        if (stage) {
            stage.innerHTML =
                '<div class="stream-player-note"><div class="spinner"></div><p>' +
                escapeHtml(message || 'Resolving link…') + '</p></div>';
        }
        openOverlay();
    }

    function showOverlayError(name, message) {
        const title = $('stream-player-title');
        const kind = $('stream-player-kind');
        if (title) title.textContent = name || 'File';
        if (kind) kind.textContent = 'Error';
        setActions(false);
        const stage = stageEl();
        if (stage) {
            stage.innerHTML =
                '<div class="stream-player-note stream-player-note--error">' +
                '<p>' + escapeHtml(message) + '</p>' +
                '<button class="btn btn-secondary btn-sm" data-action="player-retry">Try again</button>' +
                '</div>';
        }
        openOverlay();
    }

    function teardownMedia() {
        const stage = stageEl();
        if (!stage) return;
        const video = stage.querySelector('video');
        if (video) {
            try { video.pause(); } catch (e) { /* never started */ }
            video.removeAttribute('src');
            try { video.load(); } catch (e) { /* already detached */ }
        }
        const img = stage.querySelector('img');
        if (img) img.removeAttribute('src');
        stage.innerHTML = '';
    }

    function attachRetry(media, path) {
        media.addEventListener('error', () => {
            if (player.retried) return;
            player.retried = true;
            // The proxy regenerates expired CDN links on its own; this covers
            // the case where the link cached for this path has gone dead.
            resolveLink(path, true)
                .then(() => {
                    media.src = contentUrl(path) + '&retry=' + Date.now();
                    if (media.tagName === 'VIDEO') {
                        try { media.play(); } catch (e) { /* autoplay blocked */ }
                    }
                })
                .catch(() => { /* surface nothing: the error state stands */ });
        });
    }

    function formatDuration(seconds) {
        if (!seconds || seconds <= 0) return '';
        const total = Math.round(seconds);
        const pad = (n) => (n < 10 ? '0' + n : String(n));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        return h ? h + ':' + pad(m) + ':' + pad(s) : m + ':' + pad(s);
    }

    function remuxNotice(record) {
        const codecs = [record.video_codec, record.audio_codec]
            .filter(Boolean)
            .map((c) => escapeHtml(String(c).toUpperCase()))
            .join(' + ');
        const parts = [
            'Remuxed to MP4 on the fly' +
            (codecs ? ' (' + codecs + ', copied without re-encoding)' : '') + '.',
            'Seeking is unavailable, because a live conversion has no fixed length.',
        ];
        const duration = formatDuration(record.duration);
        if (duration) parts.push('Runs about ' + duration + '.');
        return parts.join(' ');
    }

    function renderVideo(path, name, record, options) {
        const stage = stageEl();
        if (!stage) return;
        const opts = options || {};

        const wrap = document.createElement('div');
        wrap.className = 'stream-player-video';

        const video = document.createElement('video');
        video.className = 'stream-player-media';
        video.controls = true;
        video.autoplay = true;
        video.playsInline = true;
        video.preload = 'metadata';
        video.setAttribute('controlsList', 'nodownload');
        attachRetry(video, path);
        video.src = contentUrl(path);
        wrap.appendChild(video);

        if (opts.notice) {
            const note = document.createElement('p');
            note.className = 'stream-player-notice';
            note.innerHTML = opts.notice;
            wrap.appendChild(note);
        }

        stage.innerHTML = '';
        stage.appendChild(wrap);
    }

    function navButton(dir) {
        const btn = document.createElement('button');
        btn.className = 'stream-lightbox-nav stream-lightbox-nav--' + dir;
        btn.setAttribute('data-nav-dir', dir);
        btn.setAttribute('aria-label', dir === 'prev' ? 'Previous image' : 'Next image');
        btn.innerHTML = dir === 'prev'
            ? '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>'
            : '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M8.59 16.59 10 18l6-6-6-6-1.41 1.41L13.17 12z"/></svg>';
        return btn;
    }

    function renderImage(path, name, record) {
        const stage = stageEl();
        if (!stage) return;
        const wrap = document.createElement('div');
        wrap.className = 'stream-lightbox';

        const img = document.createElement('img');
        img.className = 'stream-player-media stream-player-media--image';
        img.alt = name;
        attachRetry(img, path);
        img.src = contentUrl(path);
        wrap.appendChild(img);

        const total = player.siblings ? player.siblings.length : 0;
        if (total > 1 && player.index >= 0) {
            wrap.appendChild(navButton('prev'));
            wrap.appendChild(navButton('next'));
            const counter = document.createElement('div');
            counter.className = 'stream-lightbox-counter';
            counter.textContent = (player.index + 1) + ' / ' + total;
            wrap.appendChild(counter);
        }

        stage.innerHTML = '';
        stage.appendChild(wrap);
    }

    function renderUnsupported(path, name, record) {
        const stage = stageEl();
        if (!stage) return;
        const isVideo = record.kind === 'video';
        const ext = (String(name).split('.').pop() || '').toLowerCase();
        const icon = '<svg viewBox="0 0 24 24" width="34" height="34" aria-hidden="true">' +
            glyph(isVideo ? 'video' : 'other') + '</svg>';

        const codec = record.video_codec ? String(record.video_codec).toUpperCase() : '';
        const audio = record.audio_codec ? String(record.audio_codec).toUpperCase() : '';
        // The server tells us *why* this file landed on the external panel.
        // Falling back to "we have a codec" keeps an older server readable.
        const reason = record.reason || (codec ? 'codec' : null);
        const container = escapeHtml(ext ? ext.toUpperCase() : 'This format');
        let heading;
        let detail;

        if (!isVideo) {
            heading = 'No inline preview for this file type';
            detail = 'Download it, or open the direct link in an app that handles this format.';
        } else if (reason === 'codec' && codec) {
            // The probe ran, so name the real culprit: it is the codec, not the
            // container. Swapping containers would not help, and saying "MKV
            // needs re-encoding" would point the user at the wrong thing.
            const named = codec === 'HEVC' ? 'HEVC (H.265)' : escapeHtml(codec);
            heading = 'This video\'s codec cannot play in the browser';
            detail = 'The video track is ' + escapeHtml(codec) +
                (audio ? ' with ' + escapeHtml(audio) + ' audio' : '') +
                ', and browsers cannot decode ' + named + '. Remuxing copies the codecs ' +
                'unchanged, so it cannot rescue this file — playing it would need a full ' +
                're-encode, which this server deliberately does not do. ' +
                'Download it and open it in a player like VLC.';
        } else if (reason === 'no-ffmpeg') {
            heading = 'FFmpeg is not available';
            detail = container + ' files have to be remuxed before a browser will take them, ' +
                'and the server could not find <code>ffmpeg</code> and <code>ffprobe</code> on ' +
                'its PATH. Install FFmpeg and restart the app, or download the file and open ' +
                'it in a player like VLC.';
        } else if (reason === 'unprobeable') {
            heading = 'Could not read this file';
            detail = 'The server could not inspect ' + container + '\'s streams, so it cannot ' +
                'tell whether they are browser-safe. That usually means a stale link or an ' +
                'unusual container. Download it and open it in a player like VLC.';
        } else if (reason === 'codec') {
            heading = 'This video\'s codec cannot play in the browser';
            detail = container + ' holds a codec browsers cannot decode. Playing it would need ' +
                'a full re-encode, which this server deliberately does not do. ' +
                'Download it and open it in a player like VLC.';
        } else {
            heading = 'This container cannot play in the browser';
            detail = 'Browsers only decode MP4, WebM, MOV and OGG natively. ' + container +
                ' needs re-encoding, which this server deliberately does not do. ' +
                'Download it and open it in a player like VLC.';
        }

        stage.innerHTML =
            '<div class="stream-player-note">' +
            '<div class="stream-player-note-icon">' + icon + '</div>' +
            '<h4>' + escapeHtml(heading) + '</h4>' +
            '<p>' + detail + '</p>' +
            '<div class="stream-player-note-actions">' +
            '<button class="btn btn-secondary btn-sm" data-action="player-copy">Copy link</button>' +
            '<a class="btn btn-secondary btn-sm" data-action="player-open" target="_blank" rel="noopener noreferrer">Open in new tab</a>' +
            '<a class="btn btn-primary btn-sm" data-action="player-download">Download</a>' +
            '</div>' +
            '</div>';

        const openEl = stage.querySelector('[data-action="player-open"]');
        const downloadEl = stage.querySelector('[data-action="player-download"]');
        if (openEl) openEl.href = record.url;
        if (downloadEl) downloadEl.href = contentUrl(path, { download: true });
    }

    function renderMedia(path, name, record) {
        const title = $('stream-player-title');
        const kindEl = $('stream-player-kind');
        if (title) title.textContent = name;
        if (kindEl) {
            // When a file cannot play inline, naming the codec is far more
            // useful than a generic "external" badge.
            kindEl.textContent = record.kind === 'image'
                ? 'Image'
                : record.kind === 'video'
                    ? (record.mode === 'direct' ? 'Video'
                        : record.mode === 'remux' ? 'Video · remuxed'
                            : record.video_codec
                                ? 'Video · ' + String(record.video_codec).toUpperCase()
                                : 'Video · external')
                    : 'File';
        }

        const openEl = $('stream-player-open');
        const downloadEl = $('stream-player-download');
        const copyEl = $('stream-player-copy');
        if (openEl) openEl.href = record.url;
        if (downloadEl) downloadEl.href = contentUrl(path, { download: true });
        if (copyEl) copyEl.setAttribute('data-link', record.url);
        setActions(true);

        if (record.kind === 'image') {
            renderImage(path, name, record);
        } else if (record.mode === 'remux') {
            // Playable, but only as a live conversion: no seeking.
            renderVideo(path, name, record, { notice: remuxNotice(record) });
        } else if (record.kind === 'video' && record.mode === 'direct') {
            renderVideo(path, name, record);
        } else {
            renderUnsupported(path, name, record);
        }

        openOverlay();
    }

    async function openFile(path, name, options) {
        const opts = options || {};
        if (state.pending.has(path)) return;      // a double-click coalesces
        state.pending.add(path);

        player.path = path;
        player.name = name;
        player.kind = null;
        player.siblings = opts.siblings || null;
        player.index = typeof opts.index === 'number' ? opts.index : -1;
        player.retried = false;

        showOverlay(name, 'Resolving link…');
        try {
            const record = await resolveLink(path);
            if (player.path !== path) return;     // user moved on while resolving
            player.kind = record.kind;
            renderMedia(path, name, record);
        } catch (e) {
            if (player.path !== path) return;
            showOverlayError(name, (e && e.message) || String(e));
        } finally {
            state.pending.delete(path);
        }
    }

    function closePlayer() {
        teardownMedia();
        const overlay = playerEl();
        if (overlay) {
            overlay.classList.remove('active');
            overlay.style.display = 'none';
        }
        player.path = null;
        player.name = null;
        player.kind = null;
        player.siblings = null;
        player.index = -1;
    }

    function navigate(delta) {
        const list = player.siblings;
        if (!list || list.length < 2 || player.index < 0) return;
        const next = (player.index + delta + list.length) % list.length;
        const entry = list[next];
        openFile(entry.path, entry.name, { siblings: list, index: next });
    }

    function copyLink() {
        const record = player.path ? links.get(player.path) : null;
        if (!record) return;
        const done = () => {
            if (window.showToast) {
                window.showToast('success', 'Link copied', 'The direct link expires after about 30 minutes.');
            }
        };
        const fail = () => {
            if (window.showToast) {
                window.showToast('warning', 'Copy failed', 'Select the link manually instead.');
            }
        };

        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(record.url).then(done).catch(fail);
                return;
            }
        } catch (e) { /* fall through to the legacy path */ }

        try {
            const ta = document.createElement('textarea');
            ta.value = record.url;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand && document.execCommand('copy');
            document.body.removeChild(ta);
            ok ? done() : fail();
        } catch (e) {
            fail();
        }
    }

    function onPlayerClick(event) {
        const navBtn = event.target.closest('[data-nav-dir]');
        if (navBtn) {
            navigate(navBtn.getAttribute('data-nav-dir') === 'prev' ? -1 : 1);
            return;
        }

        const actionEl = event.target.closest('[data-action]');
        if (actionEl) {
            const action = actionEl.getAttribute('data-action');
            if (action === 'player-copy') {
                copyLink();
                event.preventDefault();
            } else if (action === 'player-retry') {
                if (player.path) {
                    openFile(player.path, player.name, { siblings: player.siblings, index: player.index });
                }
            }
            // player-open / player-download are anchors: let them navigate.
            return;
        }

        if (event.target === playerEl()) closePlayer();   // backdrop click
    }

    function bind() {
        const body = $('stream-body');
        if (body) body.addEventListener('click', onBodyClick);

        const nav = $('stream-breadcrumb');
        if (nav) {
            nav.addEventListener('click', (event) => {
                const btn = event.target.closest('[data-nav]');
                if (btn) load(btn.getAttribute('data-nav'));
            });
        }

        const overlay = playerEl();
        if (overlay) overlay.addEventListener('click', onPlayerClick);

        const closeBtn = $('stream-player-close');
        if (closeBtn) closeBtn.addEventListener('click', closePlayer);

        document.addEventListener('keydown', (event) => {
            if (!player.path) return;                 // overlay is closed
            if (event.key === 'Escape') {
                closePlayer();
                return;
            }
            if (player.kind === 'image') {
                if (event.key === 'ArrowLeft') navigate(-1);
                if (event.key === 'ArrowRight') navigate(1);
            }
        });
    }

    const StreamPage = {
        activate() {
            if (!state.activated) {
                state.activated = true;
                bind();
                load('/');
            }
        },

        goUp() {
            load(parentPath(state.path));
        },

        refresh() {
            state.cache.clear();
            links.clear();       // every resolved link may now be stale
            fetch('/api/stream/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
                .catch(() => {})
                .finally(() => load(state.path, { force: true }));
        },

        closePlayer,

        // Exposed for the playback tests and for debugging.
        load,
        openFile,
        resolveLink,
        links,
        player,
        state,
    };

    window.StreamPage = StreamPage;
})();
