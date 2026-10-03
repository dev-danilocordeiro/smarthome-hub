import { type SyntheticEvent, useState } from "react";
import { Link } from "react-router";
import { api, describe, unwrap } from "../api/client";
import { useResource } from "../hooks/useResource";

export function HomesPage() {
  const { data: homes, error, reload } = useResource(() => unwrap(api.GET("/homes")), "homes");
  const [name, setName] = useState("");

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    await unwrap(api.POST("/homes", { body: { name, timezone } }));
    setName("");
    reload();
  }

  return (
    <section>
      <h2>Your homes</h2>
      {error !== null && <p role="alert">{describe(error)}</p>}
      {homes === null ? (
        <p>Loading…</p>
      ) : homes.length === 0 ? (
        <p className="muted">No homes yet.</p>
      ) : (
        <ul className="cards">
          {homes.map((home) => (
            <li key={home.id}>
              <Link to={`/homes/${home.id}`} className="grow">
                <strong>{home.name}</strong>
              </Link>
              <small className="muted">
                {home.role} · {home.timezone}
              </small>
            </li>
          ))}
        </ul>
      )}
      <form className="row" onSubmit={(event) => void submit(event)}>
        <label>
          New home <input value={name} onChange={(e) => { setName(e.target.value); }} required />
        </label>
        <button type="submit">Create</button>
      </form>
    </section>
  );
}
