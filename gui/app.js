// Application State variables
let currentTab = 'dashboard';
let currentConsoleTab = 'currently';
let isRunning = false;
let pollingInterval = null;
let lastLogCount = 0;
let selectedTorrentName = null;
let tokenExpiryWarningShown = false;

// SVG icon paths for toast types
const TOAST_ICONS = {
    warning: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>',
    error: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>',
    success: '<svg class="toast-icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>'
};

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
        <button class="toast-close" onclick="this.parentElement.classList.add('toast-hiding'); setTimeout(() => this.parentElement.remove(), 300)">&times;</button>
    `;
    container.appendChild(toast);
    
    if (durationMs > 0) {
        setTimeout(() => {
            toast.classList.add('toast-hiding');
            setTimeout(() => toast.remove(), 300);
        }, durationMs);
    }
}

function checkTokenExpiry(tokenStr) {
    if (!tokenStr) return;
    try {
        const parsed = JSON.parse(tokenStr);
        const expiresAt = parsed.expires_at;
        if (expiresAt) {
            const nowSec = Math.floor(Date.now() / 1000);
            if (nowSec > expiresAt) {
                if (!tokenExpiryWarningShown) {
                    tokenExpiryWarningShown = true;
                    const expiredAgo = nowSec - expiresAt;
                    let agoText;
                    if (expiredAgo < 3600) agoText = `${Math.floor(expiredAgo / 60)} minutes ago`;
                    else agoText = `${Math.floor(expiredAgo / 3600)} hours ago`;
                    
                    showToast('warning', 'Session Token Expired',
                        `Your Torbox session token expired ${agoText}. Please log in again at torbox.app and paste a fresh token.`, 0);
                }
            } else {
                tokenExpiryWarningShown = false;
            }
        }
    } catch (e) {}
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
    if (tabName === 'completed') titleEl.textContent = 'Completed Downloads';
    if (tabName === 'logs') titleEl.textContent = 'Raw System Logs';
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
    fetch('/api/stop', { method: 'POST' }) // Stops and resets state log array
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
    
    const body = {
        token: token,
        dest_dir: dest_dir,
        delay: delay,
        max_parallel_files: max_parallel_files
    };
    
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
                const originalText = loginBtn.textContent;
                loginBtn.textContent = 'Saved!';
                loginBtn.style.backgroundColor = '#04BF8A';
                loginBtn.style.color = '#000000';
                loginBtn.style.borderColor = '#04BF8A';
                
                setTimeout(() => {
                    loginBtn.textContent = originalText;
                    loginBtn.style.backgroundColor = '';
                    loginBtn.style.color = '';
                    loginBtn.style.borderColor = '';
                }, 2000);
            }
            
            fetchStatus();
        }
    })
    .catch(err => console.error("Error saving settings:", err));
}

// Trigger Start/Stop queue execution
function toggleDownloadProcess() {
    const startBtn = document.getElementById('start-btn');
    const startText = document.getElementById('start-btn-text');
    const urlInput = "https://torbox.app/download";

    if (isRunning) {
        // Send Stop signal
        fetch('/api/stop', { method: 'POST' })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    isRunning = false;
                    startBtn.classList.remove('btn-secondary');
                    startBtn.classList.add('btn-primary');
                    startText.textContent = 'Start Download';
                }
            });
    } else {
        // Check if selection exists
        if (!selectedTorrentName) {
            alert("Please select a torrent from the queue list first by clicking on it.");
            return;
        }

        // Send Start signal for selected torrent
        fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                url: urlInput,
                torrent_name: selectedTorrentName
            })
        })
        .then(res => {
            if (res.status === 400) {
                alert("Downloader is already running!");
                return;
            }
            return res.json();
        })
        .then(data => {
            if (data && data.success) {
                isRunning = true;
                startBtn.classList.remove('btn-primary');
                startBtn.classList.add('btn-secondary');
                startText.textContent = 'Stop Download';
            }
        })
        .catch(err => console.error("Error starting downloads:", err));
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
function fetchStatus() {
    fetch('/api/status')
        .then(res => res.json())
        .then(state => {
            // 1. Sync settings fields if not focused
            const tokenInput = document.getElementById('session-token-input');
            if (document.activeElement !== tokenInput && state.token) {
                tokenInput.value = state.token;
            }
            
            // Check if the token has expired
            checkTokenExpiry(state.token);

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

            // Extract and display user email from token if available
            let displayEmail = "Account";
            if (state.token) {
                try {
                    const parsed = JSON.parse(state.token);
                    if (parsed.user && parsed.user.email) {
                        displayEmail = parsed.user.email;
                    } else if (parsed.email) {
                        displayEmail = parsed.email;
                    }
                } catch (e) {
                    try {
                        const parts = state.token.split('.');
                        if (parts.length === 3) {
                            const payload = JSON.parse(atob(parts[1]));
                            if (payload.email) {
                                displayEmail = payload.email;
                            }
                        }
                    } catch (jwtErr) {}
                }
            }
            const accountEl = document.getElementById('account-status');
            if (accountEl) {
                const span = accountEl.querySelector('span');
                if (span) {
                    span.textContent = displayEmail;
                    span.style.color = state.token ? '#04BF8A' : '';
                }
            }

            // Sync free space metric
            document.getElementById('free-space-val').textContent = state.free_space;

            // Sync running status state
            isRunning = (state.status === 'scanning' || state.status === 'downloading');
            const startBtn = document.getElementById('start-btn');
            const startText = document.getElementById('start-btn-text');
            if (isRunning) {
                startBtn.className = 'btn btn-secondary btn-large';
                startText.textContent = 'Stop Download';
            } else {
                startBtn.className = 'btn btn-primary btn-large';
                startText.textContent = state.status === 'finished' ? 'Start Download' : 'Start Download';
            }

            // Update badge queue count
            document.getElementById('queue-count').textContent = `${state.queue.length} TOTAL`;

            // 2. Render Torrent Queue
            renderQueue(state.queue);

            // 3. Render Logs Console
            renderLogs(state.logs, state.active_downloads);

            // 4. Update Completed Table
            renderCompleted(state.queue);
        })
        .catch(err => console.error("Error fetching state:", err));
}

// Render Torrent Queue List in UI
function renderQueue(queue) {
    const container = document.getElementById('queue-container');
    if (!container) return;

    if (queue.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" width="48" height="48" class="empty-icon"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
                <p>No torrents in queue.</p>
                <span>Paste a session token, then click "Start Download" to scan your Torbox downloads list and begin downloading completed torrent folders.</span>
            </div>
        `;
        return;
    }

    let html = '';
    queue.forEach(item => {
        const isDownloading = item.status === 'downloading';
        const progressClass = item.status === 'failed' ? 'failed' : (isDownloading ? 'downloading' : '');
        
        const isSelected = item.name === selectedTorrentName;
        const selectedClass = isSelected ? 'selected' : '';
        const escapedName = item.name.replace(/'/g, "\\'");

        html += `
            <div class="queue-item ${selectedClass}" onclick="selectTorrent('${escapedName}')" style="cursor: pointer;">
                <div class="queue-row">
                    <span class="torrent-name" title="${item.name}">${item.name}</span>
                    <span class="torrent-size">${item.size}</span>
                </div>
                <div class="queue-row">
                    <div class="torrent-status">
                        <span class="dot ${item.status === 'completed' ? 'emerald' : (item.status === 'failed' ? 'red' : 'blue')}"></span>
                        <span class="status-text ${item.status}">${item.status === 'downloading' ? `Downloading - ${item.speed}` : item.status}</span>
                    </div>
                    <span class="torrent-percent font-mono">${item.progress}%</span>
                </div>
                <div class="progress-container">
                    <div class="progress-bar ${progressClass}" style="width: ${item.progress}%"></div>
                </div>
            </div>
        `;
    });
    
    container.innerHTML = html;
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
        const titleEl = el.querySelector('.torrent-name');
        if (titleEl && titleEl.getAttribute('title') === selectedTorrentName) {
            el.classList.add('selected');
        } else {
            el.classList.remove('selected');
        }
    });
}

// Render console log entries and active downloads dynamically
function renderLogs(logs, activeDownloads) {
    const consoleEl = document.getElementById('console-logs');
    const rawEl = document.getElementById('full-raw-logs');
    
    if (!consoleEl || !rawEl) return;

    // Build system logs HTML
    let systemHtml = '';
    logs.forEach(line => {
        let colorClass = 'text-muted';
        if (line.includes('ERROR')) {
            colorClass = 'accent-red';
        } else if (line.includes('WARN')) {
            colorClass = 'accent-yellow';
        } else if (line.includes('INFO') || line.includes('Successfully') || line.includes('Finished')) {
            colorClass = '';
        }
        systemHtml += `<div class="console-line ${colorClass}">${line}</div>`;
    });

    // Populate Console views based on selected console tab
    if (currentConsoleTab === 'currently') {
        // Render active downloads progress bars
        let currentlyHtml = '';
        if (activeDownloads && Object.keys(activeDownloads).length > 0) {
            Object.entries(activeDownloads).forEach(([filename, fileState]) => {
                const progress = fileState.progress || 0;
                const speed = fileState.speed || '0 KB/s';
                const downloadedStr = formatBytes(fileState.downloaded);
                const totalStr = formatBytes(fileState.total);
                
                let barColor = 'var(--emerald-color)';
                let statusLabel = speed;
                if (fileState.status === 'completed') {
                    barColor = '#04BF8A';
                    statusLabel = '<span style="color: #04BF8A; font-weight: bold;">Completed</span>';
                } else if (fileState.status === 'failed') {
                    barColor = '#ff5f56';
                    statusLabel = '<span style="color: #ff5f56; font-weight: bold;">Failed</span>';
                }
                
                currentlyHtml += `
                <div class="active-download-item" style="margin-bottom: 14px; border-bottom: 1px solid var(--border-color); padding-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 6px; align-items: center;">
                        <span class="file-name" style="color: #ffffff; font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 70%;" title="${filename}">${filename}</span>
                        <span class="file-speed" style="font-size: 11px; font-family: var(--font-mono);">${statusLabel}</span>
                    </div>
                    <div class="progress-bar-container" style="background: #1a1a1a; height: 6px; border-radius: 3px; overflow: hidden; width: 100%; border: 1px solid #2a2a2a;">
                        <div class="progress-bar-fill" style="background: ${barColor}; width: ${progress}%; height: 100%; transition: width 0.2s ease;"></div>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 11px; margin-top: 6px; color: var(--text-muted); font-family: var(--font-mono);">
                        <span>${progress}%</span>
                        <span>${downloadedStr} / ${totalStr}</span>
                    </div>
                </div>`;
            });
        } else {
            currentlyHtml = '<div class="console-line text-muted">No files are currently downloading. Select a torrent and click Start Download.</div>';
        }
        consoleEl.innerHTML = currentlyHtml;
    } else {
        consoleEl.innerHTML = systemHtml || '<div class="console-line text-muted">System idle. Handshake complete.</div>';
    }

    // Populate raw full logs view tab
    let rawHtml = '';
    logs.forEach(line => {
        rawHtml += `<div class="console-line">${line}</div>`;
    });
    rawEl.innerHTML = rawHtml || '<div class="console-line text-muted">No raw logs collected yet.</div>';

    // Auto-scroll to bottom of logs if system tab is open and logs have increased
    if (currentConsoleTab === 'system' && logs.length > lastLogCount) {
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

    const completedItems = queue.filter(item => item.status === 'completed');
    if (completedItems.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="4" class="text-center text-muted">No completed downloads in this session yet.</td>
            </tr>
        `;
        return;
    }

    let html = '';
    completedItems.forEach(item => {
        html += `
            <tr>
                <td class="font-semibold">${item.name}</td>
                <td class="text-muted font-mono">${item.path || 'Root'}</td>
                <td>${item.size}</td>
                <td class="text-emerald font-semibold">SUCCESS</td>
            </tr>
        `;
    });
    tbody.innerHTML = html;
}
