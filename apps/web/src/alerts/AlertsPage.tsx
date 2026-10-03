import { useState } from "react";
import {
  api,
  type Alert,
  describe,
  type NotificationPreferences,
  unwrap,
  type WebhookSettings,
} from "../api/client";
import { useResource } from "../hooks/useResource";

type Severity = Alert["severity"];

const SEVERITY_TONE: Record<Alert["severity"], string> = {
  info: "muted",
  warning: "tone-text-active",
  critical: "tone-text-warn",
};

export function AlertsPage({
  homeId,
  version,
  isOwner,
  isGuest,
}: {
  homeId: string;
  /** Changes whenever the live socket reports an alert transition. */
  version: number;
  isOwner: boolean;
  isGuest: boolean;
}) {
  const [show, setShow] = useState<"open" | "resolved">("open");
  const { data: alerts, error, reload } = useResource(
    () =>
      unwrap(
        api.GET("/homes/{home_id}/alerts", {
          params: { path: { home_id: homeId }, query: { status: show } },
        }),
      ),
    `${homeId}:${show}:${String(version)}`,
  );
  const [failure, setFailure] = useState<string | null>(null);

  async function acknowledge(alert: Alert) {
    setFailure(null);
    try {
      await unwrap(
        api.POST("/homes/{home_id}/alerts/{alert_id}/acknowledge", {
          params: { path: { home_id: homeId, alert_id: alert.id } },
        }),
      );
      reload();
    } catch (problem) {
      setFailure(describe(problem));
    }
  }

  return (
    <div className="alerts">
      <div className="row">
        <h2 className="grow">Alerts</h2>
        <div role="group" aria-label="Show">
          <button type="button" className={show === "open" ? "" : "ghost"} onClick={() => { setShow("open"); }}>
            Open
          </button>{" "}
          <button
            type="button"
            className={show === "resolved" ? "" : "ghost"}
            onClick={() => { setShow("resolved"); }}
          >
            Resolved
          </button>
        </div>
      </div>
      {error !== null && <p role="alert">{describe(error)}</p>}
      {failure && <p role="alert">{failure}</p>}
      {alerts === null ? (
        <p>Loading…</p>
      ) : alerts.length === 0 ? (
        <p className="muted">{show === "open" ? "Nothing needs attention." : "No resolved alerts yet."}</p>
      ) : (
        <ul className="cards">
          {alerts.map((alert) => (
            <li key={alert.id}>
              <div className="grow">
                <strong>{alert.title}</strong>{" "}
                <span className={`badge ${SEVERITY_TONE[alert.severity]}`}>{alert.severity}</span>
                <div className="muted">
                  {alert.opened_at && `since ${new Date(alert.opened_at).toLocaleString()}`}
                  {alert.resolved_at && ` · resolved ${new Date(alert.resolved_at).toLocaleString()}`}
                  {alert.acknowledged_by && ` · ${alert.acknowledged_by} is on it`}
                </div>
              </div>
              {alert.status === "open" && !alert.acknowledged_by && !isGuest && (
                <button type="button" onClick={() => void acknowledge(alert)}>
                  I'm on it
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!isGuest && <PreferencesForm homeId={homeId} />}
      {isOwner && <WebhookForm homeId={homeId} />}
    </div>
  );
}

function PreferencesForm({ homeId }: { homeId: string }) {
  const { data: loaded } = useResource(
    () =>
      unwrap(api.GET("/homes/{home_id}/notification-preferences", { params: { path: { home_id: homeId } } })),
    homeId,
  );
  const [draft, setDraft] = useState<NotificationPreferences | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const prefs = draft ?? loaded;
  if (prefs === null) return null;
  const quiet = prefs.quiet_start !== null && prefs.quiet_end !== null;
  const update = (change: Partial<NotificationPreferences>) => { setDraft({ ...prefs, ...change }); };

  async function save() {
    if (prefs === null) return;
    try {
      await unwrap(
        api.PUT("/homes/{home_id}/notification-preferences", {
          params: { path: { home_id: homeId } },
          body: prefs,
        }),
      );
      setMessage("Saved.");
    } catch (problem) {
      setMessage(describe(problem));
    }
  }

  return (
    <form
      className="capture"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3>My notifications</h3>
      <p className="muted">Every alert appears in your inbox. Email is for the ones you choose here.</p>
      <div className="row">
        <label>
          <input
            type="checkbox"
            checked={prefs.email_enabled ?? true}
            onChange={(e) => { update({ email_enabled: e.target.checked }); }}
          />{" "}
          Email me
        </label>
        <label>
          for{" "}
          <select
            value={prefs.min_email_severity ?? "warning"}
            onChange={(e) => { update({ min_email_severity: e.target.value as Severity }); }}
          >
            <option value="info">everything</option>
            <option value="warning">warnings and critical</option>
            <option value="critical">critical only</option>
          </select>
        </label>
      </div>
      <div className="row">
        <label>
          <input
            type="checkbox"
            checked={quiet}
            onChange={(e) => { update(e.target.checked ? { quiet_start: 22, quiet_end: 7 } : { quiet_start: null, quiet_end: null }); }}
          />{" "}
          Quiet hours
        </label>
        {quiet && (
          <>
            <HourInput label="from" value={prefs.quiet_start ?? 22} onChange={(quiet_start) => { update({ quiet_start }); }} />
            <HourInput label="to" value={prefs.quiet_end ?? 7} onChange={(quiet_end) => { update({ quiet_end }); }} />
            <span className="muted">Critical alerts (locks, cameras) still come through.</span>
          </>
        )}
      </div>
      <div className="row">
        <button type="submit">Save</button>
        {message && <span role="status">{message}</span>}
      </div>
    </form>
  );
}

function HourInput({ label, value, onChange }: { label: string; value: number; onChange: (hour: number) => void }) {
  return (
    <label>
      {label}{" "}
      <select value={value} onChange={(e) => { onChange(Number(e.target.value)); }}>
        {Array.from({ length: 24 }, (_, h) => (
          <option key={h} value={h}>
            {String(h).padStart(2, "0")}:00
          </option>
        ))}
      </select>
    </label>
  );
}

async function loadWebhook(homeId: string): Promise<WebhookSettings | null> {
  const { data, response } = await api.GET("/homes/{home_id}/webhook", { params: { path: { home_id: homeId } } });
  return response.ok && data ? data : null;
}

function WebhookForm({ homeId }: { homeId: string }) {
  const { data: loaded, reload } = useResource(() => loadWebhook(homeId), homeId);
  const [url, setUrl] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const shown = url ?? loaded?.url ?? "";

  async function save(rotate: boolean) {
    setMessage(null);
    try {
      const saved = await unwrap(
        api.PUT("/homes/{home_id}/webhook", {
          params: { path: { home_id: homeId } },
          body: { url: shown, enabled: true, rotate_secret: rotate },
        }),
      );
      setSecret(saved.secret ?? null);
      setMessage("Saved.");
      reload();
    } catch (problem) {
      setMessage(describe(problem));
    }
  }

  async function test() {
    try {
      await unwrap(api.POST("/homes/{home_id}/webhook/test", { params: { path: { home_id: homeId } } }));
      setMessage("A signed ping is on its way.");
    } catch (problem) {
      setMessage(describe(problem));
    }
  }

  return (
    <form
      className="capture"
      onSubmit={(e) => {
        e.preventDefault();
        void save(false);
      }}
    >
      <h3>Webhook</h3>
      <p className="muted">
        Every alert that opens or resolves is POSTed here, signed with <code>X-Smarthome-Signature</code>.
      </p>
      <div className="row">
        <label className="grow">
          URL{" "}
          <input
            type="url"
            value={shown}
            onChange={(e) => { setUrl(e.target.value); }}
            placeholder="https://example.com/hooks/smarthome"
            required
            size={40}
          />
        </label>
        <button type="submit">Save</button>
        {loaded && (
          <>
            <button type="button" className="ghost" onClick={() => void save(true)}>
              Rotate secret
            </button>
            <button type="button" className="ghost" onClick={() => void test()}>
              Send test
            </button>
          </>
        )}
      </div>
      {secret && (
        <p role="status">
          Signing secret (shown once): <code>{secret}</code>
        </p>
      )}
      {message && <p role="status">{message}</p>}
    </form>
  );
}
