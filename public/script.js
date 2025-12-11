const grid = document.getElementById("ban-grid");
const searchInput = document.getElementById("search");
const clearSearchBtn = document.getElementById("clear-search");
const refreshBtn = document.getElementById("refresh");
const refreshCountdown = document.getElementById("refresh-countdown");
const activeCountEl = document.getElementById("active-count");
const totalCountEl = document.getElementById("total-count");
const lastUpdatedEl = document.getElementById("last-updated");

const REFRESH_MS = 20000;
const avatarPlaceholder =
  "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160' viewBox='0 0 160 160'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0%' stop-color='%233ed4c2'/><stop offset='100%' stop-color='%235b8dfd'/></linearGradient></defs><rect width='160' height='160' rx='20' fill='url(%23g)'/><text x='50%' y='54%' text-anchor='middle' font-family='Arial' font-size='64' fill='%23041020'>RBX</text></svg>";

let bans = [];
const profileCache = new Map();
let refreshTimer = null;
let countdownTimer = null;
let nextRefreshAt = null;

function escapeHtml(str = "") {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatTime(iso) {
  if (!iso) return "Unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Unknown";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relativeTime(iso) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(diff)) return "";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

async function fetchProfile(userId) {
  if (!userId) {
    return { username: "Unknown", displayName: "Unknown user", avatar: avatarPlaceholder };
  }
  if (profileCache.has(userId)) {
    return profileCache.get(userId);
  }

  const profile = { username: `User ${userId}`, displayName: "", avatar: avatarPlaceholder };

  try {
    const res = await fetch(`https://users.roblox.com/v1/users/${userId}`);
    if (res.ok) {
      const data = await res.json();
      profile.username = data.name || profile.username;
      profile.displayName = data.displayName || profile.username;
    }
  } catch (err) {
    console.warn("Failed to load user profile", userId, err);
  }

  try {
    const res = await fetch(
      `https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds=${userId}&size=150x150&format=Png&isCircular=true`
    );
    if (res.ok) {
      const data = await res.json();
      const thumb = (data.data && data.data[0]) || {};
      if (thumb.imageUrl) {
        profile.avatar = thumb.imageUrl;
      }
    }
  } catch (err) {
    console.warn("Failed to load avatar", userId, err);
  }

  profileCache.set(userId, profile);
  return profile;
}

async function enrichLogs(logs) {
  const withProfiles = await Promise.all(
    logs.map(async (log) => {
      const profile = await fetchProfile(log.userId);
      return { ...log, profile };
    })
  );

  return withProfiles.sort((a, b) => new Date(b.startTime || b.createTime).getTime() - new Date(a.startTime || a.createTime).getTime());
}

function formatReason(text = "") {
  if (!text.trim()) return "No public reason provided.";
  const clean = escapeHtml(text).replace(/\n+/g, "<br>");
  return clean;
}

function statusPill(active) {
  return `<span class="status-pill ${active ? "live" : "ended"}">${active ? "Active" : "Ended"}</span>`;
}

function reasonPill(shortReason = "") {
  const key = shortReason.toLowerCase().replace(/\s+/g, "-") || "other";
  return `<span class="pill reason ${key}">${escapeHtml(shortReason || "Other")}</span>`;
}

function renderBans(list) {
  if (!list.length) {
    grid.innerHTML = `<div class="empty">No bans found for that search. Try another keyword.</div>`;
    return;
  }

  grid.innerHTML = list
    .map((log) => {
      const profile = log.profile || {};
      const name = escapeHtml(profile.displayName || profile.username || `User ${log.userId}`);
      const username = escapeHtml(profile.username || "unknown");
      const mod = log.moderatorId ? `Moderator ${escapeHtml(log.moderatorId)}` : "Unknown moderator";
      const started = formatTime(log.startTime || log.createTime);
      const relative = relativeTime(log.startTime || log.createTime);

      return `
        <article class="ban-card ${log.active ? "active" : "expired"}">
          <div class="card-head">
            <div class="avatar">
              <img src="${profile.avatar || avatarPlaceholder}" alt="Avatar for ${name}" loading="lazy">
            </div>
            <div class="identity">
              <div class="name-row">
                <h4>${name}</h4>
                ${reasonPill(log.shortReason)}
              </div>
              <p class="muted">@${username} • ID ${log.userId || "?"}</p>
            </div>
            ${statusPill(log.active)}
          </div>
          <div class="card-body">
            <div class="pair">
              <span class="label">Moderator</span>
              <span class="value">${mod}</span>
            </div>
            <div class="pair">
              <span class="label">Started</span>
              <span class="value">${started}${relative ? ` • ${relative}` : ""}</span>
            </div>
            <div class="pair">
              <span class="label">Public reason</span>
              <p class="reason" title="${escapeHtml(log.displayReason || log.privateReason || "No reason")}">${formatReason(
        log.displayReason || log.privateReason || ""
      )}</p>
            </div>
            <div class="pair meta">
              <span class="label">Flags</span>
              <span class="chips">
                <span class="chip">${log.excludeAltAccounts ? "Exclude alts" : "Applies to alts"}</span>
                <span class="chip subtle">Ref ${escapeHtml(log.userPath || "users")}</span>
              </span>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderCounts(list) {
  const active = list.filter((b) => b.active).length;
  activeCountEl.textContent = active;
  totalCountEl.textContent = list.length;
  if (lastUpdatedEl.dataset.ts) {
    lastUpdatedEl.textContent = formatTime(lastUpdatedEl.dataset.ts);
  }
}

function applySearch() {
  const q = (searchInput.value || "").trim().toLowerCase();
  if (!q) {
    renderBans(bans);
    renderCounts(bans);
    return;
  }

  const filtered = bans.filter((log) => {
    const profile = log.profile || {};
    const haystack = [
      profile.displayName,
      profile.username,
      log.userId,
      log.moderatorId,
      log.shortReason,
      log.displayReason,
      log.privateReason,
    ]
      .join(" ")
      .toLowerCase();

    return haystack.includes(q);
  });

  renderBans(filtered);
  renderCounts(filtered);
}

async function fetchBans() {
  try {
    const res = await fetch("/api/bans");
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "Failed to fetch ban logs");
    }

    bans = await enrichLogs(data.logs || []);
    lastUpdatedEl.dataset.ts = new Date().toISOString();
    lastUpdatedEl.textContent = formatTime(lastUpdatedEl.dataset.ts);
    renderCounts(bans);
    applySearch();
  } catch (err) {
    console.error(err);
    grid.innerHTML = `<div class="error">Could not load ban logs. ${escapeHtml(err.message)}</div>`;
  } finally {
    nextRefreshAt = Date.now() + REFRESH_MS;
  }
}

function startAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
  }
  refreshTimer = setInterval(fetchBans, REFRESH_MS);
}

function startCountdown() {
  if (countdownTimer) {
    clearInterval(countdownTimer);
  }
  countdownTimer = setInterval(() => {
    if (!nextRefreshAt) {
      refreshCountdown.textContent = "—";
      return;
    }
    const diff = nextRefreshAt - Date.now();
    if (diff <= 0) {
      refreshCountdown.textContent = "now";
      return;
    }
    const seconds = Math.ceil(diff / 1000);
    refreshCountdown.textContent = `${seconds}s`;
  }, 500);
}

function bindEvents() {
  searchInput.addEventListener("input", applySearch);
  clearSearchBtn.addEventListener("click", () => {
    searchInput.value = "";
    applySearch();
  });
  refreshBtn.addEventListener("click", () => {
    nextRefreshAt = Date.now() + REFRESH_MS;
    fetchBans();
  });
}

async function init() {
  bindEvents();
  startCountdown();
  await fetchBans();
  startAutoRefresh();
}

init();
