// Extract api_key from URL query parameters and store it
const urlParams = new URLSearchParams(window.location.search);
const apiKey = urlParams.get('api_key') || localStorage.getItem('torbox_api_key') || '';
if (urlParams.get('api_key')) {
    localStorage.setItem('torbox_api_key', urlParams.get('api_key'));
}

// Redefine window.fetch to automatically inject X-API-Key header
const originalFetch = window.fetch;
window.fetch = function (resource, options) {
    if (apiKey) {
        options = options || {};
        options.headers = options.headers || {};
        if (options.headers instanceof Headers) {
            options.headers.set('X-API-Key', apiKey);
        } else if (Array.isArray(options.headers)) {
            options.headers.push(['X-API-Key', apiKey]);
        } else {
            options.headers['X-API-Key'] = apiKey;
        }
    }
    return originalFetch(resource, options);
};

// Application State variables
let currentTab = 'dashboard';
let currentConsoleTab = 'downloading';
let isRunning = false;
let pollingInterval = null;
let lastLogCount = 0;
let selectedTorrentName = null;
let tokenExpiryWarningShown = false;
let currentTorrentFiles = [];

// SVG icon paths for toast types
const TOAST_ICONS = {
    warning: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>',
    error: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>',
    success: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>'
};

// Escape user-provided strings before injecting into innerHTML templates
function escapeHtml(text) {
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function showToast(type, title, message, durationMs = 8000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        ${TOAST_ICONS[type] || TOAST_ICONS.warning}
        <div class="toast-body">
            <div class="toast-title">${title}</div>
            <div class="toast-message">${message}</div>
        </div>
        <button class="toast-close" aria-label="Dismiss" onclick="this.parentElement.classList.add('toast-hiding'); setTimeout(() => this.parentElement.remove(), 220)">&times;</button>
    `;
    container.appendChild(toast);

    if (durationMs > 0) {
        setTimeout(() => {
            toast.classList.add('toast-hiding');
            setTimeout(() => toast.remove(), 220);
        }, durationMs);
    }
}

function checkTokenExpiry(state) {
    if (!state) return;
    // If the backend has actively confirmed the token is expired/invalid (because redirect to login occurred), show a toast warning
    if (state.status === 'error' && state.error_message && state.error_message.includes('expired or invalid')) {
        if (!tokenExpiryWarningShown) {
            tokenExpiryWarningShown = true;
            showToast('warning', 'Session Token Expired / Invalid', state.error_message, 0);
        }
    } else if (state.status !== 'error') {
        tokenExpiryWarningShown = false;
    }
}

// Initialize the Dashboard
document.addEventListener('DOMContentLoaded', () => {
    // Fetch initial status and populate inputs
    fetchStatus();
    
    // Start continuous polling of backend API
    pollingInterval = setInterval(fetchStatus, 800);
});

// Switch Main Menu Tabs
function switchTab(tabName) {
    currentTab = tabName;
    
    // Manage active menu highlights
    document.querySelectorAll('.sidebar-menu .menu-item').forEach(el => {
        el.classList.remove('active');
    });
    event.currentTarget.classList.add('active');

    // Manage visibility of content panels
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.add('hidden');
    });
    
    const targetView = document.getElementById(`${tabName}-view`);
    if (targetView) {
        targetView.classList.remove('hidden');
    }

    // Dynamic header title update
    const titleEl = document.getElementById('page-title');
    if (tabName === 'dashboard') titleEl.textContent = 'Torbox Downloader';
    if (tabName === 'stream') titleEl.textContent = 'Stream';
    if (tabName === 'completed') titleEl.textContent = 'Completed Downloads';
    if (tabName === 'logs') titleEl.textContent = 'Raw System Logs';

    // Let the Stream page lazy-load its library the first time it is opened.
    if (tabName === 'stream' && window.StreamPage) window.StreamPage.activate();
}

// Switch Console View Tabs
function switchConsoleTab(tabName) {
    currentConsoleTab = tabName;
    document.querySelectorAll('.tabs-sub .sub-tab').forEach(el => {
        el.classList.remove('active');
    });
    document.getElementById(`sub-tab-${tabName}`).classList.add('active');
    
    // Refresh log viewer instantly
    fetchStatus();
}

// Clear UI Console log rows
function clearConsoleLogs() {
    fetch('/api/clear_logs', { method: 'POST' }) // Resets state log array without stopping downloads
        .then(() => {
            const consoleEl = document.getElementById('console-logs');
            if (consoleEl) consoleEl.innerHTML = '';
            const rawEl = document.getElementById('full-raw-logs');
            if (rawEl) rawEl.innerHTML = '';
        });
}

// Save parameters (Token, Path, Delay, Concurrency) to Backend
function saveSettings() {
    tokenExpiryWarningShown = false; // Reset so expiry re-checks against the new token
    const token = document.getElementById('session-token-input').value;
    const dest_dir = document.getElementById('download-path-input').value;
    const delay = parseInt(document.getElementById('delay-input').value) || 5;
    const max_parallel_files = parseInt(document.getElementById('parallel-downloads-input').value) || 1;
    const parallel_connections = document.getElementById('parallel-connections-input').checked;
    const disk_profile = (document.getElementById('disk-profile-input') || {}).value || 'auto';
    const write_buffer_mb = parseFloat((document.getElementById('write-buffer-input') || {}).value) || 0;
    const watch_mode = document.getElementById('watch-mode-input').checked;
    const watch_interval = parseInt(document.getElementById('watch-interval-input').value) || 60;
    const driveLetterSel = document.getElementById('drive-letter-input');
    const mount_drive_letter = driveLetterSel ? (driveLetterSel.value || '') : undefined;
    
    const body = {
        token: token,
        dest_dir: dest_dir,
        delay: delay,
        max_parallel_files: max_parallel_files,
        parallel_connections: parallel_connections,
        disk_profile: disk_profile,
        write_buffer_mb: write_buffer_mb,
        watch_mode: watch_mode,
        watch_interval: watch_interval
    };
    if (mount_drive_letter !== undefined) {
        body.mount_drive_letter = mount_drive_letter === '' ? 'AUTO' : mount_drive_letter;
    }
    
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            console.log("Settings successfully updated!");

            // Provide immediate visual feedback on the Login button
            const loginBtn = document.getElementById('login-btn');
            if (loginBtn) {
                loginBtn.classList.add('btn-success-flash');
                loginBtn.textContent = 'Saved';
                setTimeout(() => {
                    loginBtn.classList.remove('btn-success-flash');
                    loginBtn.textContent = 'Sign in';
                }, 1600);
            }

            fetchStatus();
        } else if (data.error) {
            showToast('error', 'Could not save settings', data.error);
        }
    })
    .catch(err => console.error("Error saving settings:", err));
}

// Trigger Start/Stop queue execution
// One source of truth for the start/stop button's look (color, label, icon)
function setStartButtonVisual(running) {
    const startBtn = document.getElementById('start-btn');
    if (!startBtn) return;
    const startText = document.getElementById('start-btn-text');
    const iconEl = document.getElementById('start-btn-icon');
    startBtn.className = `btn ${running ? 'btn-danger' : 'btn-primary'} btn-large`;
    if (startText) startText.textContent = running ? 'Stop download' : 'Start download';
    if (iconEl) iconEl.innerHTML = `<path d="${running ? 'M6 6h12v12H6z' : 'M8 5v14l11-7z'}"/>`;
}

// Trigger Start/Stop queue execution
function toggleDownloadProcess() {
    if (isRunning) {
        // Send Stop signal
        fetch('/api/stop', { method: 'POST' })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    isRunning = false;
                    setStartButtonVisual(false);
                }
            });
    } else {
        if (!selectedTorrentName) {
            showToast('warning', 'No torrent selected', 'Click a torrent in the queue list to select it before starting.');
            return;
        }

        openFilesModal();
        fetchTorrentStructure(selectedTorrentName);
    }
}

// Files Modal Handling functions
function openFilesModal() {
    const modal = document.getElementById('files-modal');
    if (modal) {
        modal.style.display = 'flex';
        setTimeout(() => {
            modal.classList.add('active');
        }, 10);
    }
}

function closeFilesModal() {
    const modal = document.getElementById('files-modal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => {
            modal.style.display = 'none';
        }, 300);
    }
}

function retryFetchStructure() {
    if (selectedTorrentName) {
        fetchTorrentStructure(selectedTorrentName);
    }
}

function fetchTorrentStructure(name) {
    const urlInput = "https://torbox.app/download";
    
    // Set loading state
    document.getElementById('modal-loading-state').style.display = 'flex';
    document.getElementById('modal-error-state').style.display = 'none';
    document.getElementById('modal-tree-content').style.display = 'none';
    document.getElementById('modal-download-btn').style.display = 'none';
    const genBtn = document.getElementById('modal-generate-links-btn');
    if (genBtn) genBtn.style.display = 'none';
    
    document.getElementById('modal-torrent-name').textContent = name;
    
    fetch('/api/fetch_structure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            url: urlInput,
            torrent_name: name
        })
    })
    .then(res => {
        if (res.status === 400) {
            throw new Error("Bad request. Please verify connection and token.");
        }
        if (res.status === 429) {
            throw new Error("Another torrent is being scanned. Please wait and try again.");
        }
        return res.json();
    })
    .then(data => {
        if (data && data.success && data.files) {
            currentTorrentFiles = data.files;
            document.getElementById('modal-loading-state').style.display = 'none';
            document.getElementById('modal-tree-content').style.display = 'block';
            document.getElementById('modal-download-btn').style.display = 'inline-flex';
            const genBtn = document.getElementById('modal-generate-links-btn');
            if (genBtn) genBtn.style.display = 'inline-flex';
            
            renderFilesTree(data.files);
        } else {
            throw new Error(data ? data.error : "Failed to load structure.");
        }
    })
    .catch(err => {
        document.getElementById('modal-loading-state').style.display = 'none';
        document.getElementById('modal-error-state').style.display = 'flex';
        document.getElementById('modal-error-msg').textContent = err.message || "Failed to fetch files structure.";
    });
}

function renderFilesTree(files) {
    const container = document.getElementById('modal-files-tree');
    if (!container) return;
    
    if (!files || files.length === 0) {
        container.innerHTML = '<div class="text-center text-muted p-4">No files detected in this torrent.</div>';
        return;
    }
    
    const tree = buildTreeFromFlatList(files);
    let html = '';
    const keys = Object.keys(tree.children);
    
    // Sort keys: folders first, then files alphabetically
    keys.sort((a, b) => {
        const typeA = tree.children[a].type;
        const typeB = tree.children[b].type;
        if (typeA !== typeB) {
            return typeA === 'folder' ? -1 : 1;
        }
        return a.localeCompare(b);
    });
    
    keys.forEach(key => {
        html += renderTreeNode(tree.children[key], key, 0);
    });
    
    container.innerHTML = html;
    updateSelectAllState();
}

function buildTreeFromFlatList(files) {
    const root = { type: 'folder', children: {} };
    files.forEach(file => {
        let current = root;
        const pathSegments = file.path || [];
        pathSegments.forEach(segment => {
            if (!current.children[segment]) {
                current.children[segment] = {
                    type: 'folder',
                    name: segment,
                    children: {}
                };
            }
            current = current.children[segment];
        });
        
        const relPath = pathSegments.concat([file.name]).join('/');
        current.children[file.name] = {
            type: 'file',
            name: file.name,
            size: file.size,
            id: relPath
        };
    });
    return root;
}

function renderTreeNode(node, name, level) {
    if (node.type === 'file') {
        return `
            <div class="tree-node file-node">
                <div class="tree-row" style="padding-left: ${level * 20 + 8}px;">
                    <div class="tree-toggle-icon"></div>
                    <input type="checkbox" class="tree-checkbox file-checkbox" data-id="${node.id}" checked onchange="onFileCheckboxChange(this)" />
                    <div class="tree-item-icon file">
                        <svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>
                    </div>
                    <span class="tree-node-name">${node.name}</span>
                    <span class="tree-node-size">${node.size}</span>
                </div>
            </div>
        `;
    }
    
    const keys = Object.keys(node.children || {});
    keys.sort((a, b) => {
        const typeA = node.children[a].type;
        const typeB = node.children[b].type;
        if (typeA !== typeB) {
            return typeA === 'folder' ? -1 : 1;
        }
        return a.localeCompare(b);
    });
    
    let childrenHTML = '';
    keys.forEach(key => {
        childrenHTML += renderTreeNode(node.children[key], key, level + 1);
    });
    
    const toggleIcon = keys.length > 0 ? '▾' : '';
    const hasChildren = keys.length > 0;
    
    return `
        <div class="tree-node folder-node">
            <div class="tree-row folder-row" onclick="toggleFolderCollapse(this)" style="padding-left: ${level * 20 + 8}px;">
                <div class="tree-toggle-icon ${hasChildren ? 'expanded' : ''}">${toggleIcon}</div>
                <input type="checkbox" class="tree-checkbox folder-checkbox" checked onclick="event.stopPropagation()" onchange="onFolderCheckboxChange(this)" />
                <div class="tree-item-icon folder">
                    <svg viewBox="0 0 24 24"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
                </div>
                <span class="tree-node-name font-semibold">${name}</span>
            </div>
            <div class="tree-children">
                ${childrenHTML}
            </div>
        </div>
    `;
}

function toggleFolderCollapse(row) {
    const node = row.parentElement;
    const toggleIcon = row.querySelector('.tree-toggle-icon');
    const childrenContainer = node.querySelector('.tree-children');
    
    if (childrenContainer) {
        childrenContainer.classList.toggle('hidden');
        if (toggleIcon) {
            toggleIcon.classList.toggle('expanded');
            toggleIcon.textContent = childrenContainer.classList.contains('hidden') ? '▸' : '▾';
        }
    }
}

function onFolderCheckboxChange(checkbox) {
    const folderNode = checkbox.closest('.folder-node');
    const childCheckboxes = folderNode.querySelectorAll('.tree-checkbox');
    childCheckboxes.forEach(cb => {
        cb.checked = checkbox.checked;
    });
    updateParentFolderCheckbox(folderNode);
    updateSelectAllState();
}

function onFileCheckboxChange(checkbox) {
    updateParentFolderCheckbox(checkbox);
    updateSelectAllState();
}

function updateParentFolderCheckbox(element) {
    const parentChildrenContainer = element.closest('.tree-children');
    if (!parentChildrenContainer) return;
    const folderNode = parentChildrenContainer.closest('.folder-node');
    if (!folderNode) return;
    
    const folderRow = folderNode.querySelector('.folder-row');
    const folderCheckbox = folderRow ? folderRow.querySelector('.folder-checkbox') : null;
    if (folderCheckbox) {
        const siblingCheckboxes = parentChildrenContainer.querySelectorAll('.tree-row > .tree-checkbox');
        let allChecked = true;
        for (let cb of siblingCheckboxes) {
            if (!cb.checked) {
                allChecked = false;
                break;
            }
        }
        folderCheckbox.checked = allChecked;
        updateParentFolderCheckbox(folderNode);
    }
}

function toggleSelectAllFiles(checkbox) {
    const allCheckboxes = document.querySelectorAll('#modal-files-tree .tree-checkbox');
    allCheckboxes.forEach(cb => {
        cb.checked = checkbox.checked;
    });
    updateSelectedCount();
}

function updateSelectAllState() {
    const allFileCheckboxes = document.querySelectorAll('#modal-files-tree .file-checkbox');
    const selectAllCheckbox = document.getElementById('modal-select-all');
    if (allFileCheckboxes.length === 0 || !selectAllCheckbox) return;
    
    let allChecked = true;
    for (let cb of allFileCheckboxes) {
        if (!cb.checked) {
            allChecked = false;
            break;
        }
    }
    selectAllCheckbox.checked = allChecked;
    updateSelectedCount();
}

function updateSelectedCount() {
    const allFiles = document.querySelectorAll('#modal-files-tree .file-checkbox');
    const checkedFiles = document.querySelectorAll('#modal-files-tree .file-checkbox:checked');
    const countSpan = document.getElementById('modal-selected-count');
    if (countSpan) {
        countSpan.textContent = `(${checkedFiles.length} of ${allFiles.length} selected)`;
    }
}

function startSelectedDownload() {
    const checkedFiles = [];
    const checkboxes = document.querySelectorAll('#modal-files-tree .file-checkbox:checked');
    checkboxes.forEach(cb => {
        checkedFiles.push(cb.getAttribute('data-id'));
    });
    
    if (checkedFiles.length === 0) {
        showToast('warning', 'Nothing selected', 'Select at least one file to download.');
        return;
    }

    closeFilesModal();

    const urlInput = "https://torbox.app/download";

    fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            url: urlInput,
            torrent_name: selectedTorrentName,
            selected_files: checkedFiles
        })
    })
    .then(res => {
        if (res.status === 400) {
            showToast('warning', 'Already running', 'The downloader is already running.');
            return;
        }
        return res.json();
    })
    .then(data => {
        if (data && data.success) {
            isRunning = true;
            setStartButtonVisual(true);
            showToast("success", "Queue started", "Downloading the selected files and folders.");
        }
    })
    .catch(err => console.error("Error starting selected downloads:", err));
}

// Generate Links Modal Handling functions
let isGeneratingLinks = false;
let generateLinksAbortController = null;

function openGenerateLinksModal() {
    const checkedFileCheckboxes = document.querySelectorAll('#modal-files-tree .file-checkbox:checked');
    if (checkedFileCheckboxes.length === 0) {
        alert("Please select at least one file to generate links for.");
        return;
    }
    
    // Show setup UI and hide progress / results
    document.getElementById('gen-links-setup').style.display = 'block';
    document.getElementById('gen-links-progress-container').style.display = 'none';
    document.getElementById('gen-links-result-container').style.display = 'none';
    
    document.getElementById('gen-links-selected-count').textContent = checkedFileCheckboxes.length;
    
    // Reset progress bar
    document.getElementById('gen-links-progress-bar').style.transform = 'scaleX(0)';
    document.getElementById('gen-links-progress-status').textContent = `Generating: 0 / ${checkedFileCheckboxes.length} completed...`;
    
    const modal = document.getElementById('generate-links-modal');
    if (modal) {
        modal.style.display = 'flex';
        setTimeout(() => {
            modal.classList.add('active');
        }, 10);
    }
}

function closeGenerateLinksModal() {
    if (isGeneratingLinks) {
        if (!confirm("Are you sure you want to stop generating links?")) {
            return;
        }
        if (generateLinksAbortController) {
            generateLinksAbortController.abort();
        }
        isGeneratingLinks = false;
    }
    
    const modal = document.getElementById('generate-links-modal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => {
            modal.style.display = 'none';
        }, 300);
    }
}

async function startGeneratingLinks() {
    const checkedCheckboxes = document.querySelectorAll('#modal-files-tree .file-checkbox:checked');
    const totalFiles = checkedCheckboxes.length;
    if (totalFiles === 0) return;
    
    isGeneratingLinks = true;
    generateLinksAbortController = new AbortController();
    
    // Hide setup, show progress
    document.getElementById('gen-links-setup').style.display = 'none';
    document.getElementById('gen-links-progress-container').style.display = 'flex';
    document.getElementById('gen-links-result-container').style.display = 'none';
    
    const formatOption = document.querySelector('input[name="link-format"]:checked').value;
    const generatedItems = [];
    
    let completed = 0;
    
    for (let i = 0; i < totalFiles; i++) {
        if (!isGeneratingLinks) break;
        
        const cb = checkedCheckboxes[i];
        const fileId = cb.getAttribute('data-id');
        
        const fileObj = currentTorrentFiles.find(f => {
            const relPath = (f.path || []).concat([f.name]).join('/');
            return relPath === fileId;
        });
        
        if (!fileObj) {
            console.error("Could not find file object for ID:", fileId);
            continue;
        }
        
        document.getElementById('gen-links-progress-status').textContent = `Generating link [${i+1}/${totalFiles}]: ${fileObj.name}...`;
        
        try {
            const response = await fetch('/api/generate_link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    torrent_name: selectedTorrentName,
                    name: fileObj.name,
                    size: fileObj.size,
                    selector: fileObj.selector
                }),
                signal: generateLinksAbortController.signal
            });
            
            const data = await response.json();
            if (data && data.success && data.download_url) {
                if (formatOption === 'link-title') {
                    generatedItems.push(`${data.download_url}:${fileObj.name}`);
                } else {
                    generatedItems.push(data.download_url);
                }
            } else {
                console.error("Failed to generate link for file", fileObj.name, data ? data.error : "Unknown error");
                generatedItems.push(`FAILED_TO_GENERATE_LINK:${fileObj.name}`);
            }
        } catch (err) {
            if (err.name === 'AbortError') {
                console.log('Link generation aborted by user.');
                break;
            }
            console.error("Error calling /api/generate_link for file", fileObj.name, err);
            generatedItems.push(`ERROR_TO_GENERATE_LINK:${fileObj.name}`);
        }
        
        completed++;
        const percent = Math.round((completed / totalFiles) * 100);
        document.getElementById('gen-links-progress-bar').style.transform = `scaleX(${(percent / 100).toFixed(4)})`;
        document.getElementById('gen-links-progress-status').textContent = `Generating: ${completed} / ${totalFiles} completed...`;
    }
    
    isGeneratingLinks = false;
    
    // Show textarea and results
    document.getElementById('gen-links-progress-container').style.display = 'none';
    document.getElementById('gen-links-result-container').style.display = 'flex';
    
    const textarea = document.getElementById('gen-links-textarea');
    textarea.value = generatedItems.join('\n');
}

function copyGeneratedLinks() {
    const textarea = document.getElementById('gen-links-textarea');
    if (!textarea || !textarea.value) return;
    
    textarea.select();
    try {
        navigator.clipboard.writeText(textarea.value);
        showToast('success', 'Copied to Clipboard', 'All generated download links successfully copied.');
    } catch (err) {
        showToast('error', 'Copy Failed', 'Please manually select and copy the text.');
    }
}

// Clear session storage and log out
function triggerLogout() {
    if (confirm("Are you sure you want to log out and clear your session token?")) {
        fetch('/api/logout', { method: 'POST' })
            .then(() => {
                document.getElementById('session-token-input').value = '';
                fetchStatus();
            });
    }
}

// Fetch general AppState status from backend APIs
// --- Account profiles (named Torbox accounts) ------------------------------

let profilePollTimer = null;

/** POST a JSON body and resolve with the parsed response. */
function postForm(url, body) {
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    }).then(res => res.json());
}

/**
 * Keep the account picker in step with the server.
 *
 * The dropdown is only rebuilt when the set of accounts actually changes,
 * so an open picker is not yanked away by the 500 ms status poll.
 */
function syncProfiles(state) {
    const select = document.getElementById('profile-select');
    if (!select) return;
    const profiles = state.profiles || [];
    const active = state.active || '';

    const signature = profiles
        .map(p => `${p.name}:${p.email || ''}:${p.has_login ? 1 : 0}`)
        .join('|');
    if (select.dataset.signature !== signature) {
        select.dataset.signature = signature;
        select.innerHTML = profiles.map(p => {
            const label = p.email
                ? `${p.name} — ${p.email}`
                : `${p.name}${p.has_login ? '' : ' (not signed in)'}`;
            return `<option value="${escapeAttr(p.name)}"${p.name === active ? ' selected' : ''}>${escapeHtml(label)}</option>`;
        }).join('');
    }
    if (document.activeElement !== select) {
        select.value = active;
    }

    const hint = document.querySelector('.account-hint');
    if (hint) {
        const current = profiles.find(p => p.name === active);
        hint.textContent = current && current.email ? current.email : 'Not signed in';
    }

    // The header Remove button only makes sense when an account is selected.
    const removeBtn = document.getElementById('remove-profile-btn-header');
    if (removeBtn) {
        const visible = !!active;
        removeBtn.hidden = !visible;
    }

    if (state.login) {
        updateAddProfileProgress(state.login);
    }
}

function onProfileChange(name) {
    if (!name) return;
    postForm('/api/profiles/select', { name })
        .then(data => {
            if (!data.success) {
                showToast('error', 'Could not switch account', data.error || 'Unknown error');
            } else {
                showToast('success', 'Account switched', 'Now using “' + name + '”.');
            }
            fetchStatus();
        })
        .catch(err => showToast('error', 'Could not switch account', String(err)));
}

/**
 * Manual rescan of the Torbox torrent list. Triggered by the dashboard
 * refresh button. The actual scan keeps running on the server after the
 * request returns; the spinner just covers the request, then the status
 * poll will show the queue repopulating.
 */
function refreshTorrents() {
    const btn = document.getElementById('queue-refresh-btn');
    if (!btn || btn.disabled) return;
    btn.classList.add('is-spinning');
    btn.disabled = true;
    const start = Date.now();
    postForm('/api/refresh_torrents', {})
        .then(data => {
            if (!data || !data.success) {
                showToast('error', 'Could not refresh', (data && data.error) || 'Unknown error');
            }
        })
        .catch(err => showToast('error', 'Could not refresh', String(err)))
        .finally(() => {
            // Hold the spinner briefly so a fast response still feels intentional.
            const wait = Math.max(0, 600 - (Date.now() - start));
            setTimeout(() => {
                btn.classList.remove('is-spinning');
                btn.disabled = false;
            }, wait);
        });
}

function openAddProfileModal() {
    const modal = document.getElementById('add-profile-modal');
    if (!modal) return;
    document.getElementById('add-profile-setup').style.display = '';
    document.getElementById('add-progress-container').style.display = 'none';
    resetAddProfileProgress();
    const input = document.getElementById('profile-name-input');
    if (input) input.value = '';
    // The overlay is hidden via opacity + pointer-events until `.active`
    // is added (same pattern as the other modals). It also starts with
    // an inline `display: none` in the markup, so that has to be cleared
    // first or the .active class has nothing to make visible.
    modal.style.display = 'flex';
    setTimeout(() => modal.classList.add('active'), 10);
    if (input) input.focus();
}

/** Clear any previous failure state before a fresh attempt. */
function resetAddProfileProgress() {
    const status = document.getElementById('add-profile-status');
    if (status) {
        status.classList.remove('login-failed');
        status.textContent = 'Waiting for sign-in…';
    }
    const bar = document.getElementById('add-profile-progress-bar');
    if (bar) bar.classList.add('indeterminate');
    const retry = document.getElementById('add-profile-retry-btn');
    if (retry) retry.style.display = 'none';
}

function closeAddProfileModal() {
    const modal = document.getElementById('add-profile-modal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => { modal.style.display = 'none'; }, 300);
    }
    if (profilePollTimer) { clearInterval(profilePollTimer); profilePollTimer = null; }
}

function startAddProfile() {
    const input = document.getElementById('profile-name-input');
    const name = ((input && input.value) || '').trim();
    if (!name) {
        showToast('error', 'Name required', 'Give the account a short name before signing in.');
        return;
    }
    postForm('/api/profiles/add', { name })
        .then(data => {
            if (!data.success) {
                showToast('error', 'Could not start sign-in', data.error || 'Unknown error');
                return;
            }
            document.getElementById('add-profile-setup').style.display = 'none';
            document.getElementById('add-progress-container').style.display = 'block';
            resetAddProfileProgress();
            document.getElementById('add-profile-status').textContent = 'Opening a browser window…';
            if (profilePollTimer) clearInterval(profilePollTimer);
            profilePollTimer = setInterval(pollAddProfile, 1000);
        })
        .catch(err => showToast('error', 'Could not start sign-in', String(err)));
}

function pollAddProfile() {
    fetch('/api/profiles')
        .then(res => res.json())
        .then(data => {
            if (data.login) updateAddProfileProgress(data.login);
            if (data.login && !data.login.active) {
                if (profilePollTimer) { clearInterval(profilePollTimer); profilePollTimer = null; }
                if (data.login.ok) {
                    showToast('success', 'Account added', data.login.message || '');
                    closeAddProfileModal();
                    fetchStatus();
                } else {
                    // Leave the modal open so the failure is readable and the
                    // account can be retried without re-typing its name.
                    showAddProfileFailure(data.login.message || 'Sign-in did not finish.');
                    fetchStatus();
                }
            }
        })
        .catch(() => {});
}

function showAddProfileFailure(message) {
    const status = document.getElementById('add-profile-status');
    if (status) {
        status.textContent = message;
        status.classList.add('login-failed');
    }
    const bar = document.getElementById('add-profile-progress-bar');
    if (bar) bar.classList.remove('indeterminate');
    const retry = document.getElementById('add-profile-retry-btn');
    if (retry) retry.style.display = '';
}

function updateAddProfileProgress(login) {
    const status = document.getElementById('add-profile-status');
    if (status && login.message) status.textContent = login.message;
}

function cancelAddProfile() {
    postForm('/api/profiles/cancel_login', {}).then(() => fetchStatus());
}

function removeActiveProfile() {
    const select = document.getElementById('profile-select');
    const name = select && select.value;
    if (!name) {
        showToast('error', 'No account selected', 'Pick an account first or use the Remove button inside the Add dialog.');
        return;
    }
    if (!confirm('Remove the account “' + name + '”? Its saved session is deleted. You can add it again later.')) return;
    postForm('/api/profiles/remove', { name })
        .then(data => {
            if (!data.success) {
                showToast('error', 'Could not remove account', data.error || 'Unknown error');
            } else {
                showToast('success', 'Account removed', '“' + name + '” is gone.');
                closeAddProfileModal();
            }
            fetchStatus();
        })
        .catch(err => showToast('error', 'Could not remove account', String(err)));
}

function fetchStatus() {
    fetch('/api/status')
        .then(res => res.json())
        .then(state => {
            // 1. Sync settings fields if not focused
            const tokenInput = document.getElementById('session-token-input');
            if (document.activeElement !== tokenInput) {
                if (state.token_set) {
                    // Only overwrite if it doesn't already show a masked value or actual value
                    if (!tokenInput.value) {
                        tokenInput.value = '••••••••••••••••••••';
                    }
                } else {
                    tokenInput.value = '';
                }
            }
            
            // Check if the token has expired
            checkTokenExpiry(state);

            // Account profiles (named Torbox accounts)
            if (state.profiles) {
                syncProfiles(state);
            }

            const pathInput = document.getElementById('download-path-input');
            if (document.activeElement !== pathInput && state.dest_dir) {
                pathInput.value = state.dest_dir;
            }

            const parallelInput = document.getElementById('parallel-downloads-input');
            if (document.activeElement !== parallelInput && state.max_parallel_files !== undefined) {
                parallelInput.value = state.max_parallel_files;
            }

            const delayInput = document.getElementById('delay-input');
            if (document.activeElement !== delayInput && state.delay !== undefined) {
                delayInput.value = state.delay;
            }

            const diskProfileInput = document.getElementById('disk-profile-input');
            if (diskProfileInput && document.activeElement !== diskProfileInput && state.disk_profile !== undefined) {
                diskProfileInput.value = state.disk_profile;
            }

            const writeBufferInput = document.getElementById('write-buffer-input');
            if (writeBufferInput && document.activeElement !== writeBufferInput && state.write_buffer_mb !== undefined) {
                writeBufferInput.value = state.write_buffer_mb;
            }

            const parallelConnsInput = document.getElementById('parallel-connections-input');
            if (parallelConnsInput && document.activeElement !== parallelConnsInput && state.parallel_connections !== undefined) {
                parallelConnsInput.checked = state.parallel_connections;
            }

            const watchModeInput = document.getElementById('watch-mode-input');
            if (watchModeInput && document.activeElement !== watchModeInput && state.watch_mode !== undefined) {
                watchModeInput.checked = state.watch_mode;
            }

            const watchIntervalInput = document.getElementById('watch-interval-input');
            if (watchIntervalInput && document.activeElement !== watchIntervalInput && state.watch_interval !== undefined) {
                watchIntervalInput.value = state.watch_interval;
            }

            // Extract and display user email from backend state
            let displayEmail = state.email || "Account";
            const accountEl = document.getElementById('account-status');
            if (accountEl) {
                const span = accountEl.querySelector('.account-meta span');
                const hint = accountEl.querySelector('.account-hint');
                if (span) span.textContent = displayEmail;
                if (hint) hint.textContent = state.token_set ? 'Connected' : 'Not signed in';
                accountEl.classList.toggle('connected', !!state.token_set);
            }

            // Sync free space metric
            document.getElementById('free-space-val').textContent = state.free_space;

            // Sync virtual drive card (mount status / backend / letter)
            renderDriveState(state.mount, state.mount_drive_letter);

            // Sync running status state
            isRunning = (state.status === 'scanning' || state.status === 'downloading');
            setStartButtonVisual(isRunning);

            // Update badge queue count
            document.getElementById('queue-count').textContent = `${state.queue.length} total`;

            // 2. Render Torrent Queue
            renderQueue(state.queue, state.token_set);

            // 3. Render Logs Console
            renderLogs(state.logs, state.active_downloads);

            // 4. Update Completed Table
            renderCompleted(state.queue);
        })
        .catch(err => console.error("Error fetching state:", err));
}

// ===== Virtual Drive (RaiDrive-style mount) =====

// Mount the TorBox library as a local read-only drive.
function mountDrive() {
    const btn = document.getElementById('mount-btn');
    btn.disabled = true;
    fetch('/api/mount_drive', { method: 'POST' })
        .then(res => res.json().then(d => ({ ok: res.ok, d })))
        .then(({ ok, d }) => {
            if (!ok || d.success === false) {
                showToast('error', 'Mount Failed', d.error || 'Unknown error');
                btn.disabled = false;
            } else {
                showToast('success', 'Mounting Drive', 'Preparing your virtual drive...');
            }
        })
        .catch(err => {
            showToast('error', 'Mount Failed', String(err));
            btn.disabled = false;
        });
}

// Unmount the drive (removes mapping, stops server).
function unmountDrive() {
    const btn = document.getElementById('unmount-btn');
    btn.disabled = true;
    fetch('/api/unmount_drive', { method: 'POST' })
        .then(res => res.json())
        .then(d => {
            if (!d.success) showToast('error', 'Unmount Failed', d.error || 'Unknown error');
        })
        .catch(err => showToast('error', 'Unmount Failed', String(err)));
}

// Open the mounted drive in Explorer.
function openDrive() {
    fetch('/api/open_drive', { method: 'POST' })
        .then(res => res.json())
        .then(d => {
            if (!d.success) showToast('error', 'Cannot Open Drive', d.error || 'Unknown error');
        })
        .catch(err => showToast('error', 'Cannot Open Drive', String(err)));
}

// Render the virtual-drive card from backend state.
function renderDriveState(mount, preferred) {
    if (!mount) return;

    const badge = document.getElementById('drive-status-badge');
    const mountBtn = document.getElementById('mount-btn');
    const unmountBtn = document.getElementById('unmount-btn');
    const openBtn = document.getElementById('open-drive-btn');
    const backendEl = document.getElementById('drive-backend-val');
    const letterEl = document.getElementById('drive-letter-val');
    const hintEl = document.getElementById('drive-hint-text');

    // Sync drive-letter preference unless the user is editing it
    const sel = document.getElementById('drive-letter-input');
    if (sel && document.activeElement !== sel && preferred !== undefined) {
        sel.value = preferred || '';
    }

    const st = mount.status || 'idle';
    const mounted = st === 'mounted';
    const busy = st === 'mounting' || st === 'unmounting';

    if (badge) {
        badge.textContent = st.charAt(0).toUpperCase() + st.slice(1);
        badge.className = 'badge' + (mounted ? ' badge-success' : (st === 'error' ? ' badge-error' : (busy ? ' badge-live' : '')));
    }
    if (backendEl) backendEl.textContent = mounted ? (mount.backend === 'winfsp' ? 'WinFsp (native)' : 'WebDAV (net use)') : '-';
    if (letterEl) letterEl.textContent = mounted && mount.path ? mount.path : '-';

    if (mountBtn) {
        mountBtn.style.display = (mounted || busy) ? 'none' : '';
        mountBtn.disabled = false;
        mountBtn.textContent = st === 'mounting' ? 'Mounting...' : 'Mount Drive';
    }
    if (unmountBtn) {
        unmountBtn.style.display = (mounted || busy) ? '' : 'none';
        unmountBtn.disabled = st === 'unmounting';
        unmountBtn.textContent = st === 'unmounting' ? 'Unmounting...' : 'Unmount';
    }
    if (openBtn) openBtn.style.display = mounted ? '' : 'none';

    if (hintEl) {
        hintEl.textContent = mounted
            ? (mount.backend === 'webdav'
                ? "Mounted via WebDAV ('net use'). For a native Explorer drive install WinFsp (https://winfsp.dev/) and remount."
                : "Streaming live from TorBox CDN - opening plays instantly; copy-paste downloads to the paste target. Nothing is stored on disk.")
            : (st === 'error'
                ? (mount.error || 'Mount failed.') + (mount.hint ? ' ' + mount.hint : '')
                : 'Stream your cached torrents as a local drive. Opening plays straight from the cloud; copy-paste downloads to the paste target. Nothing is stored on disk.');
    }
}

// Render Torrent Queue List in UI.
// The backend is polled every 800ms, so the list updates in place when the
// composition is unchanged — progress bars transition instead of resetting,
// and entrance stagger only plays when the queue actually changes.
// Empty-state copy for when the user has a saved session but the queue
// hasn't been populated yet (e.g. before the first scan completes).
const QUEUE_EMPTY_SIGNED_IN_HTML = `
    <div class="empty-state">
        <div class="empty-icon-tile">
            <svg viewBox="0 0 24 24" width="26" height="26" class="empty-icon"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
        </div>
        <p>Queue is empty</p>
        <div class="empty-steps">
            <div class="empty-step"><span class="empty-step-key">01</span><span>Click the refresh button or Start download to scan your Torbox library.</span></div>
            <div class="empty-step"><span class="empty-step-key">02</span><span>Select a torrent here, pick its files, and start.</span></div>
        </div>
    </div>`;

const QUEUE_EMPTY_HTML = `
    <div class="empty-state">
        <div class="empty-icon-tile">
            <svg viewBox="0 0 24 24" width="26" height="26" class="empty-icon"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
        </div>
        <p>Queue is empty</p>
        <div class="empty-steps">
            <div class="empty-step"><span class="empty-step-key">01</span><span>Paste your Torbox session token above and sign in.</span></div>
            <div class="empty-step"><span class="empty-step-key">02</span><span>Click "Start download" to scan your Torbox downloads list.</span></div>
            <div class="empty-step"><span class="empty-step-key">03</span><span>Select a torrent here, pick its files, and start.</span></div>
        </div>
    </div>`;

function queueDotClass(status) {
    if (status === 'completed') return 'emerald';
    if (status === 'failed') return 'red';
    if (status === 'queued') return 'yellow';
    return 'blue';
}

function formatEta(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return '';
    const s = Math.round(seconds);
    if (s <= 0) return '';
    if (s < 60) return `${s}s left`;
    if (s < 3600) return `${Math.round(s / 60)}m left`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m left`;
    return `${Math.round(s / 86400)}d left`;
}

// TorBox reports a server-side state per torrent (cached, downloading,
// expired, incomplete). Surfaced next to the local queue status.
function serverStateLabel(state) {
    if (!state) return '';
    const known = {
        cached: 'cached',
        downloading: 'downloading',
        uploading: 'seeding',
        completed: 'ready',
        expired: 'expired',
        incomplete: 'incomplete',
        checking: 'checking',
    };
    return known[state] || state;
}

function queueItemHtml(item, index) {
    const isDownloading = item.status === 'downloading';
    const progressClass = item.status === 'failed' ? 'failed' : (isDownloading ? 'downloading' : '');
    const isSelected = item.name === selectedTorrentName;
    const serverState = serverStateLabel(item.download_state);
    const eta = formatEta(item.eta);
    let statusLabel = isDownloading ? `Downloading · ${item.speed || ''}` : item.status;
    if (serverState && !isDownloading) statusLabel = `${statusLabel} · ${serverState}`;
    if (eta && !isDownloading) statusLabel = `${statusLabel} · ${eta}`;
    const onclickName = item.name.replace(/'/g, "\\'");

    return `
        <div class="queue-item ${isSelected ? 'selected' : ''}" data-name="${escapeAttr(item.name)}" onclick="selectTorrent('${onclickName}')" style="--i: ${index};">
            <div class="queue-row">
                <span class="torrent-name" title="${escapeAttr(item.name)}">${escapeHtml(item.name)}</span>
                <span class="torrent-size">${escapeHtml(item.size)}</span>
            </div>
            <div class="queue-row">
                <div class="torrent-status">
                    <span class="dot ${queueDotClass(item.status)}${isDownloading ? ' pulse' : ''}"></span>
                    <span class="status-text ${item.status}">${escapeHtml(statusLabel)}</span>
                </div>
                <span class="torrent-percent">${item.progress}%</span>
            </div>
            <div class="progress-container">
                <div class="progress-bar ${progressClass}" style="transform: scaleX(${(item.progress / 100).toFixed(4)})"></div>
            </div>
        </div>
    `;
}

function updateQueueItem(el, item) {
    const isDownloading = item.status === 'downloading';
    const progressClass = item.status === 'failed' ? 'failed' : (isDownloading ? 'downloading' : '');
    const serverState = serverStateLabel(item.download_state);
    const eta = formatEta(item.eta);
    let statusLabel = isDownloading ? `Downloading · ${item.speed || ''}` : item.status;
    if (serverState && !isDownloading) statusLabel = `${statusLabel} · ${serverState}`;
    if (eta && !isDownloading) statusLabel = `${statusLabel} · ${eta}`;

    el.classList.toggle('selected', item.name === selectedTorrentName);

    const sizeEl = el.querySelector('.torrent-size');
    if (sizeEl) sizeEl.textContent = item.size;

    const percentEl = el.querySelector('.torrent-percent');
    if (percentEl) percentEl.textContent = `${item.progress}%`;

    const dotEl = el.querySelector('.torrent-status .dot');
    if (dotEl) dotEl.className = `dot ${queueDotClass(item.status)}${isDownloading ? ' pulse' : ''}`;

    const statusEl = el.querySelector('.status-text');
    if (statusEl) {
        statusEl.className = `status-text ${item.status}`;
        statusEl.textContent = statusLabel;
    }

    const barEl = el.querySelector('.progress-bar');
    if (barEl) {
        barEl.className = `progress-bar ${progressClass}`;
        barEl.style.transform = `scaleX(${(item.progress / 100).toFixed(4)})`;
    }
}

function renderQueue(queue, signedIn) {
    const container = document.getElementById('queue-container');
    if (!container) return;

    if (!queue || queue.length === 0) {
        const want = signedIn ? QUEUE_EMPTY_SIGNED_IN_HTML : QUEUE_EMPTY_HTML;
        if (!container.querySelector('.empty-state')) {
            container.innerHTML = want;
        }
        return;
    }

    const existing = new Map();
    container.querySelectorAll('.queue-item').forEach(el => existing.set(el.dataset.name, el));
    const sameSet = queue.length === existing.size && queue.every(item => existing.has(item.name));

    if (sameSet) {
        // Poll refresh: mutate rows in place so bars and dots animate smoothly
        queue.forEach(item => updateQueueItem(existing.get(item.name), item));
        return;
    }

    // Composition changed: rebuild with a short entrance stagger
    container.innerHTML = queue.map((item, i) => queueItemHtml(item, i)).join('');
    container.classList.add('stagger');
    clearTimeout(container._staggerTimer);
    container._staggerTimer = setTimeout(() => container.classList.remove('stagger'), 800);
}

// Select a torrent in the UI list
function selectTorrent(name) {
    if (selectedTorrentName === name) {
        selectedTorrentName = null;
    } else {
        selectedTorrentName = name;
    }

    // Quick local visual update for active borders
    const items = document.querySelectorAll('.queue-item');
    items.forEach(el => {
        if (el.dataset.name === selectedTorrentName) {
            el.classList.add('selected');
        } else {
            el.classList.remove('selected');
        }
    });
}

// Wrap a raw log line's timestamp and level in styled spans
function formatConsoleLine(line) {
    const match = String(line).match(/^(\[[^\]]*\])\s*([A-Za-z]+):?\s*(.*)$/);
    if (match) {
        const level = match[2].toUpperCase();
        const levelClass = level === 'ERROR' ? 'level-error'
            : (level === 'WARN' || level === 'WARNING') ? 'level-warn'
            : level === 'INFO' ? 'level-info'
            : 'level-debug';
        return `<div class="console-line"><span class="log-ts">${escapeHtml(match[1])}</span> <span class="log-level ${levelClass}">${escapeHtml(match[2])}:</span> ${escapeHtml(match[3])}</div>`;
    }
    return `<div class="console-line">${escapeHtml(line)}</div>`;
}

// Build one active-download row for the console tabs.
// mode: 'downloading' | 'finished' | 'failed'
function dlItemHtml(filename, fileState, mode) {
    const progress = fileState.progress || 0;
    const speed = fileState.speed || '0 KB/s';
    const downloadedStr = formatBytes(fileState.downloaded);
    const totalStr = formatBytes(fileState.total);

    let headRight = '';
    if (mode === 'failed') {
        const onclickName = filename.replace(/'/g, "\\'");
        headRight = `
            <div style="display: flex; align-items: center; gap: 8px;">
                <button class="btn-mini" onclick="retryFailedFile('${onclickName}')">Retry</button>
                <span class="dl-state failed">Failed</span>
            </div>`;
    } else if (mode === 'finished') {
        headRight = '<span class="dl-state success">Completed</span>';
    } else {
        headRight = `<span class="dl-speed">${escapeHtml(speed)}</span>`;
    }

    const barClass = mode === 'failed' ? 'failed' : (mode === 'downloading' ? 'downloading' : '');

    return `
    <div class="dl-item">
        <div class="dl-head">
            <span class="dl-name" title="${escapeAttr(filename)}">${escapeHtml(filename)}</span>
            ${headRight}
        </div>
        <div class="progress-container">
            <div class="progress-bar ${barClass}" style="transform: scaleX(${(progress / 100).toFixed(4)})"></div>
        </div>
        <div class="dl-meta">
            <span>${progress}%</span>
            <span>${downloadedStr} / ${totalStr}</span>
        </div>
    </div>`;
}

// Render console log entries and active downloads dynamically
function renderLogs(logs, activeDownloads) {
    const consoleEl = document.getElementById('console-logs');
    const rawEl = document.getElementById('full-raw-logs');

    if (!consoleEl || !rawEl) return;

    const entries = Object.entries(activeDownloads || {});
    const activeCount = entries.filter(([, s]) => s.status === 'downloading').length;

    // Live chip reflects real download activity
    const chip = document.getElementById('console-live-chip');
    const chipText = document.getElementById('console-live-text');
    if (chip && chipText) {
        chip.classList.toggle('live', activeCount > 0);
        chipText.textContent = activeCount > 0 ? `${activeCount} active` : 'Idle';
    }

    const systemHtml = (logs || []).map(formatConsoleLine).join('');

    // Populate Console views based on selected console tab
    if (currentConsoleTab === 'downloading') {
        const items = entries.filter(([, state]) => state.status === 'downloading');
        consoleEl.innerHTML = items.length > 0
            ? items.map(([filename, fileState]) => dlItemHtml(filename, fileState, 'downloading')).join('')
            : '<div class="console-line">No files downloading right now. Select a torrent and click Start download.</div>';
    } else if (currentConsoleTab === 'finished') {
        const items = entries.filter(([, state]) => state.status === 'completed');
        consoleEl.innerHTML = items.length > 0
            ? items.map(([filename, fileState]) => dlItemHtml(filename, fileState, 'finished')).join('')
            : '<div class="console-line">No finished downloads in this session.</div>';
    } else if (currentConsoleTab === 'failed') {
        const items = entries.filter(([, state]) => state.status === 'failed');
        consoleEl.innerHTML = items.length > 0
            ? items.map(([filename, fileState]) => dlItemHtml(filename, fileState, 'failed')).join('')
            : '<div class="console-line">No failed downloads in this session.</div>';
    } else {
        consoleEl.innerHTML = systemHtml || '<div class="console-line">System idle. Handshake complete.</div>';
    }

    // Populate raw full logs view tab
    rawEl.innerHTML = (logs || []).map(formatConsoleLine).join('')
        || '<div class="console-line">No raw logs collected yet.</div>';

    // Auto-scroll to bottom of logs if logs tab is open and logs have increased
    if (currentConsoleTab === 'logs' && logs.length > lastLogCount) {
        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
    if (logs.length > lastLogCount) {
        rawEl.scrollTop = rawEl.scrollHeight;
        lastLogCount = logs.length;
    }
}

// Format bytes into human readable format
function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

// Render Completed list table elements
function renderCompleted(queue) {
    const tbody = document.getElementById('completed-table-body');
    if (!tbody) return;

    const completedItems = (queue || []).filter(item => item.status === 'completed');
    if (completedItems.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="4" class="text-center text-muted">No completed downloads in this session yet.</td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = completedItems.map(item => `
        <tr>
            <td class="font-semibold">${escapeHtml(item.name)}</td>
            <td class="text-muted cell-mono">${escapeHtml(item.path || 'Root')}</td>
            <td class="cell-mono">${escapeHtml(item.size)}</td>
            <td><span class="badge badge-success">Success</span></td>
        </tr>
    `).join('');
}

// Retry a single failed download file
function retryFailedFile(filename) {
    // Set running UI state immediately
    setStartButtonVisual(true);
    isRunning = true;

    fetch('/api/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: filename })
    })
    .then(res => res.json())
    .then(data => {
        if (data && data.success) {
            showToast("success", "Retrying download", filename);
            // Auto switch to Downloading tab
            switchConsoleTab('downloading');
        } else {
            showToast('error', 'Retry failed', (data && data.error) || 'Could not retry this file.');
            isRunning = false;
            fetchStatus();
        }
    })
    .catch(err => {
        console.error("Error retrying file:", err);
        isRunning = false;
        fetchStatus();
    });
}
