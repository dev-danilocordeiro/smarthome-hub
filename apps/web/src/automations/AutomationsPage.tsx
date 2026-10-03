import { useEffect, useState } from "react";
import { api, type Automation, describe, type Run, unwrap } from "../api/client";
import { useResource } from "../hooks/useResource";
import type { LiveDevice } from "../live/model";
import { AutomationEditor } from "./AutomationEditor";

function Runs({ homeId, automation }: { homeId: string; automation: Automation }) {
  const [runs, setRuns] = useState<Run[]>([]);
  useEffect(() => {
    let cancelled = false;
    void unwrap(
      api.GET("/homes/{home_id}/automations/{automation_id}/runs", {
        params: { path: { home_id: homeId, automation_id: automation.id }, query: { limit: 20 } },
      }),
    ).then((found) => {
      if (!cancelled) setRuns(found);
    }, () => undefined);
    return () => {
      cancelled = true;
    };
  }, [homeId, automation.id, automation.version]);

  return (
    <section aria-label="Recent runs">
      <h3>Recent runs</h3>
      {runs.length === 0 ? (
        <p className="muted">It has not run yet.</p>
      ) : (
        <ul className="plain">
          {runs.map((run) => (
            <li key={run.id}>
              <span className={`badge run-${run.status}`}>{run.status}</span>{" "}
              {new Date(run.started_at).toLocaleString()}{" "}
              <small className="muted">
                trigger {run.trigger_index} · v{run.automation_version}
                {run.reason && ` · ${run.reason}`}
                {run.outcomes.length > 0 &&
                  ` · ${String(run.outcomes.filter((o) => o.command_id).length)} command(s)`}
              </small>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function AutomationsPage({
  homeId,
  devices,
  canManage,
}: {
  homeId: string;
  devices: LiveDevice[];
  canManage: boolean;
}) {
  const { data: automations, error: loadError, reload } = useResource(
    () => unwrap(api.GET("/homes/{home_id}/automations", { params: { path: { home_id: homeId } } })),
    homeId,
  );
  const [failure, setFailure] = useState<string | null>(null);
  const [editing, setEditing] = useState<Automation | "new" | null>(null);
  const error = failure ?? (loadError === null ? null : describe(loadError));

  async function toggle(automation: Automation) {
    const params = { path: { home_id: homeId, automation_id: automation.id } };
    try {
      await unwrap(
        automation.enabled && automation.status === "active"
          ? api.POST("/homes/{home_id}/automations/{automation_id}/disable", { params })
          : api.POST("/homes/{home_id}/automations/{automation_id}/enable", { params }),
      );
    } catch (problem) {
      setFailure(describe(problem));
    }
    reload();
  }

  if (error) return <p role="alert">{error}</p>;
  if (automations === null) return <p>Loading…</p>;
  const selected = editing === "new" || editing === null ? null : automations.find((a) => a.id === editing.id) ?? null;

  return (
    <div className="automations">
      <div className="row">
        <h2 className="grow">Automations</h2>
        {canManage && (
          <button type="button" onClick={() => { setEditing("new"); }}>
            New automation
          </button>
        )}
      </div>
      {automations.length === 0 && <p className="muted">No automations yet.</p>}
      <ul className="cards">
        {automations.map((a) => (
          <li key={a.id} className={a.id === selected?.id ? "selected" : ""}>
            <div className="grow">
              <button type="button" className="link" onClick={() => { setEditing(a); }}>
                <strong>{a.name}</strong>
              </button>
              <div>
                <span className={`badge ${a.status === "suspended" ? "run-failed" : a.enabled ? "run-completed" : ""}`}>
                  {a.status === "suspended" ? "suspended" : a.enabled ? "on" : "off"}
                </span>{" "}
                <small className="muted">
                  v{a.version} · edited {new Date(a.updated_at).toLocaleDateString()}
                </small>
              </div>
              {a.status_reason && <small className="warn-text">{a.status_reason}</small>}
            </div>
            {canManage && (
              <button type="button" className="secondary" onClick={() => void toggle(a)}>
                {a.enabled && a.status === "active" ? "Disable" : "Enable"}
              </button>
            )}
          </li>
        ))}
      </ul>
      {editing !== null && canManage && (
        <AutomationEditor
          key={editing === "new" ? "new" : `${editing.id}-${String(selected?.version ?? 0)}`}
          homeId={homeId}
          automation={selected}
          devices={devices}
          onSaved={(saved) => {
            setEditing(saved);
            reload();
          }}
          onCancel={() => { setEditing(null); }}
        />
      )}
      {selected && <Runs homeId={homeId} automation={selected} />}
    </div>
  );
}
