import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type Notification, unwrap } from "../api/client";
import { ALERTS_CHANGED } from "../live/useLiveHome";

const POLL_MS = 60_000;

/** Unread count in the header, with the inbox in a popover. Refreshes on a timer and
 * whenever the live socket of the open home reports an alert. */
export function Bell() {
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<Notification[] | null>(null);
  const [open, setOpen] = useState(false);
  const [version, setVersion] = useState(0);
  const refresh = () => { setVersion((v) => v + 1); };

  useEffect(() => {
    let cancelled = false;
    unwrap(api.GET("/me/notifications", { params: { query: { limit: 20 } } })).then(
      (inbox) => {
        if (cancelled) return;
        setUnread(inbox.unread);
        setItems(inbox.items);
      },
      // Signed out or offline: keep what we have; the session banner says the rest.
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [version]);

  useEffect(() => {
    const timer = setInterval(refresh, POLL_MS);
    window.addEventListener(ALERTS_CHANGED, refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener(ALERTS_CHANGED, refresh);
    };
  }, []);

  async function read(item: Notification) {
    if (item.read_at) return;
    await api.POST("/me/notifications/{notification_id}/read", {
      params: { path: { notification_id: item.id } },
    });
    refresh();
  }

  async function readAll() {
    await api.POST("/me/notifications/read-all");
    refresh();
  }

  return (
    <div className="bell">
      <button
        type="button"
        className="ghost"
        aria-expanded={open}
        aria-label={`Notifications, ${String(unread)} unread`}
        onClick={() => { setOpen(!open); }}
      >
        🔔{unread > 0 && <span className="count">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="inbox" role="dialog" aria-label="Notifications">
          <div className="row">
            <strong className="grow">Notifications</strong>
            {unread > 0 && (
              <button type="button" className="ghost" onClick={() => void readAll()}>
                Mark all read
              </button>
            )}
          </div>
          {items === null || items.length === 0 ? (
            <p className="muted">Nothing yet.</p>
          ) : (
            <ul className="plain">
              {items.map((item) => (
                <li key={item.id} className={item.read_at ? "read" : "unread"}>
                  <Link
                    to={`/homes/${item.home_id}/alerts`}
                    onClick={() => {
                      void read(item);
                      setOpen(false);
                    }}
                  >
                    <span className={`dot severity-${item.severity}`} aria-hidden="true" />
                    <span className="grow">
                      {item.title}
                      <small className="muted"> · {new Date(item.created_at).toLocaleString()}</small>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
