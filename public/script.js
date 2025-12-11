const services = [
  {
    name: "Edge Network",
    status: "operational",
    uptime: "99.99%",
    latency: 18,
    trend: [14, 16, 15, 18, 17, 16, 18, 19, 17, 16],
  },
  {
    name: "API Gateway",
    status: "operational",
    uptime: "99.98%",
    latency: 32,
    trend: [28, 30, 29, 32, 33, 31, 30, 34, 33, 32],
  },
  {
    name: "Authentication",
    status: "degraded",
    uptime: "99.4%",
    latency: 86,
    trend: [60, 65, 70, 74, 78, 80, 84, 86, 88, 86],
  },
  {
    name: "Realtime Channels",
    status: "operational",
    uptime: "99.97%",
    latency: 42,
    trend: [40, 38, 39, 42, 45, 44, 42, 41, 40, 42],
  },
  {
    name: "Data Platform",
    status: "operational",
    uptime: "99.92%",
    latency: 58,
    trend: [52, 54, 56, 58, 60, 59, 58, 57, 58, 58],
  },
  {
    name: "Payments",
    status: "partial",
    uptime: "99.1%",
    latency: 122,
    trend: [80, 90, 95, 100, 120, 125, 130, 128, 124, 122],
  },
];

const incidents = [
  {
    title: "Auth token refresh latency",
    severity: "minor",
    time: "Today, 11:12 UTC",
    detail: "Elevated latency for token refresh in EU region. Mitigation in place while cache warms.",
  },
  {
    title: "Payments webhook retries",
    severity: "major",
    time: "Yesterday, 22:04 UTC",
    detail: "Intermittent 5xx responses from third-party provider. Automatic retries limited impact.",
  },
  {
    title: "Edge routing update",
    severity: "info",
    time: "Yesterday, 08:40 UTC",
    detail: "New routing table deployed to reduce cross-region hops and improve cold starts.",
  },
];

const maintenance = [
  {
    title: "Database cluster maintenance",
    window: "Dec 15, 01:00–02:30 UTC",
    note: "Failover testing with automatic retries for write operations.",
  },
  {
    title: "Realtime broker upgrade",
    window: "Dec 17, 04:00–04:20 UTC",
    note: "Rolling restart; expected sub-200 ms reconnect for active sockets.",
  },
];

const statusMap = {
  operational: { label: "Operational", cls: "ok" },
  degraded: { label: "Degraded", cls: "warn" },
  partial: { label: "Partial", cls: "warn" },
  down: { label: "Down", cls: "down" },
};

const servicesEl = document.getElementById("services");
const incidentsEl = document.getElementById("incidents");
const maintenanceEl = document.getElementById("maintenance");

function sparklinePath(values, width, height) {
  const max = Math.max(...values);
  const min = Math.min(...values);
  const norm = values.map((v) => {
    const range = max - min || 1;
    return height - ((v - min) / range) * height;
  });

  return norm
    .map((v, i) => {
      const x = (i / (values.length - 1)) * width;
      return `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${v.toFixed(1)}`;
    })
    .join(" ");
}

function renderSpark(values, width = 120, height = 40) {
  const path = sparklinePath(values, width, height);
  return `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="spark-gradient" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="rgba(62, 212, 194, 0.6)"/>
          <stop offset="100%" stop-color="rgba(91, 141, 253, 0.0)"/>
        </linearGradient>
      </defs>
      <path d="${path}" stroke="rgba(62, 212, 194, 0.9)" stroke-width="2" fill="none" stroke-linecap="round"/>
      <path d="${path} L ${width} ${height} L 0 ${height} Z" fill="url(#spark-gradient)" opacity="0.4"/>
    </svg>
  `;
}

function renderServices() {
  servicesEl.innerHTML = services
    .map((s) => {
      const status = statusMap[s.status] || statusMap.operational;
      return `
        <article class="service-card">
          <div class="title">
            <div>
              <p class="eyebrow">${s.uptime} uptime</p>
              <h4>${s.name}</h4>
            </div>
            <span class="badge ${status.cls}">
              <span class="dot"></span>
              ${status.label}
            </span>
          </div>
          <div class="service-metrics">
            <div class="metric">
              <span class="label">Latency</span>
              <span class="value">${s.latency} ms</span>
            </div>
            <div class="metric">
              <span class="label">Trend</span>
              <div class="mini-spark">${renderSpark(s.trend)}</div>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderIncidents() {
  incidentsEl.innerHTML = incidents
    .map(
      (i) => `
      <article class="timeline-item">
        <div class="summary">
          <div>
            <p class="eyebrow">${i.time}</p>
            <h4>${i.title}</h4>
          </div>
          <span class="pill ${i.severity}">${i.severity}</span>
        </div>
        <p>${i.detail}</p>
      </article>
    `
    )
    .join("");
}

function renderMaintenance() {
  maintenanceEl.innerHTML = maintenance
    .map(
      (m) => `
      <article class="maintenance-card">
        <h4>${m.title}</h4>
        <p class="time">${m.window}</p>
        <p class="muted">${m.note}</p>
      </article>
    `
    )
    .join("");
}

function updateSummary() {
  const degraded = services.some((s) => s.status === "degraded" || s.status === "partial");
  const down = services.some((s) => s.status === "down");
  const overall = document.getElementById("overall-status");
  const pill = overall.closest(".status-pill");

  if (down) {
    pill.className = "status-pill danger";
    overall.textContent = "Service disruption";
  } else if (degraded) {
    pill.className = "status-pill warn";
    overall.textContent = "Minor degradation";
  } else {
    pill.className = "status-pill success";
    overall.textContent = "All systems operational";
  }

  document.getElementById("updated-at").textContent = new Date().toUTCString();
  document.getElementById("incidents-count").textContent = incidents.length;

  const avgLatency = Math.round(
    services.reduce((sum, s) => sum + s.latency, 0) / services.length
  );
  document.getElementById("latency").textContent = `${avgLatency} ms`;
}

function renderHeroSparkline() {
  const target = document.getElementById("hero-sparkline");
  const values = Array.from({ length: 12 }, () => 40 + Math.random() * 30);
  const width = 320;
  const height = 220;
  const path = sparklinePath(values, width, height);

  target.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="gradient" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="rgba(62, 212, 194, 0.65)"/>
          <stop offset="100%" stop-color="rgba(91, 141, 253, 0.0)"/>
        </linearGradient>
      </defs>
      <path d="${path}" stroke="rgba(255,255,255,0.5)" stroke-width="2.5" fill="none" stroke-linecap="round"/>
      <path d="${path} L ${width} ${height} L 0 ${height} Z" fill="url(#gradient)" />
    </svg>
  `;
}

function simulateLive() {
  services.forEach((s) => {
    const jitter = Math.random() * 6 - 3;
    s.latency = Math.max(12, Math.round(s.latency + jitter));
    s.trend.shift();
    s.trend.push(Math.max(12, s.latency + Math.random() * 6 - 3));
  });

  renderServices();
  updateSummary();
}

document.getElementById("refresh").addEventListener("click", simulateLive);

renderServices();
renderIncidents();
renderMaintenance();
renderHeroSparkline();
updateSummary();
setInterval(simulateLive, 8000);
