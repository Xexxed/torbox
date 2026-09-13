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
        } else {
            openFile(path, name);
        }
    }

    function openFile(path, name) {
        // Playback and the image viewer are wired up in the next stage.
        if (window.showToast) {
            window.showToast('warning', 'Playback not wired yet', 'Resolving "' + name + '" arrives with the player.');
        }
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
            fetch('/api/stream/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            })
                .catch(() => {})
                .finally(() => load(state.path, { force: true }));
        },

        // Exposed for the playback stage and for debugging.
        load,
        state,
    };

    window.StreamPage = StreamPage;
})();
