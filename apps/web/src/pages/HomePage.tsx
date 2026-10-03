import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useParams } from "react-router";
import { api, type Home, unwrap } from "../api/client";
import { AlertsPage } from "../alerts/AlertsPage";
import { AutomationsPage } from "../automations/AutomationsPage";
import { useCommands } from "../devices/commands";
import { DeviceDrawer } from "../devices/DeviceDrawer";
import { EnergyPage } from "../energy/EnergyPage";
import { FloorPlan } from "../floorplan/FloorPlan";
import { type Connection, useLiveHome } from "../live/useLiveHome";
import { ScenesPage } from "../scenes/ScenesPage";

const CONNECTION_LABEL: Record<Connection, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  "signed-out": "Signed out",
  forbidden: "No access",
};

export function HomePage() {
  const { homeId = "" } = useParams();
  const [home, setHome] = useState<Home | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const live = useLiveHome(homeId);
  const commands = useCommands(homeId, live.commands);

  useEffect(() => {
    let cancelled = false;
    void unwrap(api.GET("/homes/{home_id}", { params: { path: { home_id: homeId } } })).then(
      (found) => {
        if (!cancelled) setHome(found);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [homeId]);

  const devices = Object.values(live.devices);
  const device = selected ? live.devices[selected] : undefined;
  const role = home?.role;
  const canControl = role !== undefined && role !== "viewer";
  const canManage = role === "owner" || role === "resident";

  return (
    <section className="home">
      <header className="home-header">
        <div className="grow">
          <Link to="/" className="muted">
            ← Homes
          </Link>
          <h2>{home?.name ?? "…"}</h2>
        </div>
        <span className={`connection connection-${live.connection}`} role="status">
          {CONNECTION_LABEL[live.connection]}
        </span>
      </header>
      <nav className="tabs" aria-label="Home sections">
        <NavLink to={`/homes/${homeId}`} end>
          Floor plan
        </NavLink>
        {role !== "guest" && <NavLink to={`/homes/${homeId}/energy`}>Energy</NavLink>}
        <NavLink to={`/homes/${homeId}/alerts`}>Alerts</NavLink>
        {role !== "guest" && <NavLink to={`/homes/${homeId}/automations`}>Automations</NavLink>}
        <NavLink to={`/homes/${homeId}/scenes`}>Scenes</NavLink>
      </nav>
      {live.connection === "signed-out" && (
        <p role="alert">
          Your session ended. <a href="/api/auth/login">Sign in again</a>.
        </p>
      )}
      <Routes>
        <Route
          index
          element={
            <div className="plan-layout">
              {devices.length === 0 && live.connection === "live" ? (
                <p className="muted">No devices yet. Pair one with a pairing code.</p>
              ) : (
                <FloorPlan
                  devices={devices}
                  selected={selected}
                  pending={commands.pending}
                  onSelect={(id) => {
                    commands.clearFeedback();
                    setSelected(id === selected ? null : id);
                  }}
                />
              )}
              {device && (
                <DeviceDrawer
                  homeId={homeId}
                  device={device}
                  pending={commands.pending.has(device.id)}
                  feedback={commands.feedback}
                  updates={live.commands}
                  canControl={canControl}
                  onCommand={(desired) => void commands.send(device.id, desired)}
                  onClose={() => { setSelected(null); }}
                />
              )}
            </div>
          }
        />
        <Route path="energy" element={<EnergyPage homeId={homeId} canManage={canManage} />} />
        <Route
          path="alerts"
          element={
            <AlertsPage homeId={homeId} version={live.alerts} isOwner={role === "owner"} isGuest={role === "guest"} />
          }
        />
        <Route
          path="automations"
          element={<AutomationsPage homeId={homeId} devices={devices} canManage={canManage} />}
        />
        <Route path="scenes" element={<ScenesPage homeId={homeId} devices={devices} canManage={canManage} />} />
      </Routes>
    </section>
  );
}
