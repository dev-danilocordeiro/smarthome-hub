// HTTP read load on the BFF as one signed-in member (k6).
//
//   make loadtest-api            (VUS=20 SECONDS=120)
//
// Each iteration does what an open app does: list devices, read the latest readings of
// one device, a day of history for one metric, the energy report, the inbox count.
// Thresholds encode the budget: no errors, p95 under 300 ms (history under 500 ms).

import http from "k6/http";
import { check } from "k6";

const session = JSON.parse(open("/loadtest/session.json"));
const BASE = __ENV.API || "http://localhost:8000";
const homes = session.homes.filter((h) => h.devices.length > 0);

export const options = {
  scenarios: {
    members: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 20),
      duration: `${__ENV.SECONDS || 120}s`,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{endpoint:devices}": ["p(95)<300"],
    "http_req_duration{endpoint:latest}": ["p(95)<300"],
    "http_req_duration{endpoint:history}": ["p(95)<500"],
    "http_req_duration{endpoint:energy}": ["p(95)<500"],
    "http_req_duration{endpoint:inbox}": ["p(95)<300"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

const params = (endpoint) => ({ headers: { cookie: session.cookie }, tags: { endpoint } });

export default function () {
  const home = homes[Math.floor(Math.random() * homes.length)];
  const device = home.devices[Math.floor(Math.random() * home.devices.length)];
  const now = new Date();
  const dayAgo = new Date(now.getTime() - 86_400_000).toISOString();

  const devices = http.get(`${BASE}/homes/${home.id}/devices`, params("devices"));
  check(devices, { "devices 200": (r) => r.status === 200 });
  const latest = http.get(`${BASE}/homes/${home.id}/devices/${device}/readings/latest`, params("latest"));
  check(latest, { "latest 200": (r) => r.status === 200 });
  const history = http.get(
    `${BASE}/homes/${home.id}/devices/${device}/telemetry?metric=rssi_dbm&from=${encodeURIComponent(dayAgo)}`,
    params("history"),
  );
  check(history, { "history 200": (r) => r.status === 200 });
  const energy = http.get(
    `${BASE}/homes/${home.id}/energy/usage?from=${encodeURIComponent(dayAgo)}&to=${encodeURIComponent(now.toISOString())}&bucket=hour`,
    params("energy"),
  );
  check(energy, { "energy 200": (r) => r.status === 200 });
  const inbox = http.get(`${BASE}/me/notifications/unread-count`, params("inbox"));
  check(inbox, { "inbox 200": (r) => r.status === 200 });
}
