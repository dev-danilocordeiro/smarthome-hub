import { useState } from "react";
import { api, ApiProblem, describe, reauthenticationUrl, type Scene, type Schemas, unwrap } from "../api/client";
import { useResource } from "../hooks/useResource";
import type { LiveDevice } from "../live/model";

type Outcome = Schemas["ActionOutcomeOut"];
const CONTROLLABLE = new Set(["light", "plug", "lock", "thermostat", "camera"]);

function problemText(failure: unknown): { message: string; href: string | null } {
  if (failure instanceof ApiProblem) {
    return {
      message: failure.message,
      href: reauthenticationUrl(failure.problem, window.location.pathname),
    };
  }
  return { message: describe(failure), href: null };
}

export function ScenesPage({
  homeId,
  devices,
  canManage,
}: {
  homeId: string;
  devices: LiveDevice[];
  canManage: boolean;
}) {
  const { data: scenes, error, reload } = useResource(
    () => unwrap(api.GET("/homes/{home_id}/scenes", { params: { path: { home_id: homeId } } })),
    homeId,
  );
  const [outcomes, setOutcomes] = useState<Record<string, Outcome[]>>({});
  const [failure, setFailure] = useState<{ message: string; href: string | null } | null>(null);
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());

  async function activate(scene: Scene) {
    setFailure(null);
    try {
      const result = await unwrap(
        api.POST("/homes/{home_id}/scenes/{scene_id}/activate", {
          params: { path: { home_id: homeId, scene_id: scene.id } },
        }),
      );
      setOutcomes((current) => ({ ...current, [scene.id]: result.outcomes }));
    } catch (error) {
      setFailure(problemText(error));
    }
  }

  async function capture() {
    // A scene from what the chosen devices report right now.
    const states = devices
      .filter((d) => chosen.has(d.id) && Object.keys(d.reported).length > 0)
      .map((d) => ({ device_id: d.id, desired: d.reported }));
    setFailure(null);
    try {
      await unwrap(
        api.POST("/homes/{home_id}/scenes", { params: { path: { home_id: homeId } }, body: { name, states } }),
      );
      setName("");
      setChosen(new Set());
      reload();
    } catch (error) {
      setFailure(problemText(error));
    }
  }

  const byId = Object.fromEntries(devices.map((d) => [d.id, d]));
  const candidates = devices.filter((d) => CONTROLLABLE.has(d.kind) && Object.keys(d.reported).length > 0);

  return (
    <div className="scenes">
      <h2>Scenes</h2>
      {error !== null && <p role="alert">{problemText(error).message}</p>}
      {failure && (
        <p role="alert">
          {failure.message} {failure.href && <a href={failure.href}>Sign in again</a>}
        </p>
      )}
      {scenes === null ? (
        <p>Loading…</p>
      ) : scenes.length === 0 ? (
        <p className="muted">No scenes yet.</p>
      ) : (
        <ul className="cards">
          {scenes.map((scene) => (
            <li key={scene.id}>
              <div className="grow">
                <strong>{scene.name}</strong>
                <div className="muted">
                  {scene.states.map((s) => byId[s.device_id]?.name ?? s.device_id).join(", ")}
                </div>
                {outcomes[scene.id] && (
                  <small>
                    {outcomes[scene.id]?.filter((o) => o.command_id).length} sent
                    {outcomes[scene.id]?.some((o) => o.error) &&
                      `, failed: ${outcomes[scene.id]?.filter((o) => o.error).map((o) => o.device_id).join(", ") ?? ""}`}
                  </small>
                )}
              </div>
              <button type="button" onClick={() => void activate(scene)}>
                Activate
              </button>
            </li>
          ))}
        </ul>
      )}
      {canManage && candidates.length > 0 && (
        <form
          className="capture"
          onSubmit={(e) => {
            e.preventDefault();
            void capture();
          }}
        >
          <h3>New scene from the current state</h3>
          <label>
            Name <input value={name} onChange={(e) => { setName(e.target.value); }} required maxLength={80} />
          </label>
          <fieldset>
            <legend>Devices</legend>
            {candidates.map((d) => (
              <label key={d.id} className="check">
                <input
                  type="checkbox"
                  checked={chosen.has(d.id)}
                  onChange={(e) => {
                    const next = new Set(chosen);
                    if (e.target.checked) next.add(d.id);
                    else next.delete(d.id);
                    setChosen(next);
                  }}
                />{" "}
                {d.name} <small className="muted">{JSON.stringify(d.reported)}</small>
              </label>
            ))}
          </fieldset>
          <button type="submit" disabled={chosen.size === 0}>
            Save scene
          </button>
        </form>
      )}
    </div>
  );
}
