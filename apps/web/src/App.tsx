import { type SyntheticEvent, useCallback, useEffect, useState } from "react";
import {
  createHome,
  getSession,
  type Home,
  listHomes,
  loginUrl,
  logout,
  type SessionInfo,
} from "./api";

type State =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "signed-in"; session: SessionInfo; homes: Home[] }
  | { status: "error"; message: string };

async function fetchState(): Promise<State> {
  try {
    const session = await getSession();
    if (!session) return { status: "anonymous" };
    return { status: "signed-in", session, homes: await listHomes() };
  } catch (error) {
    return { status: "error", message: String(error) };
  }
}

export function App() {
  const [state, setState] = useState<State>({ status: "loading" });
  const loginError = new URLSearchParams(window.location.search).get("login_error");

  const load = useCallback(async () => {
    setState(await fetchState());
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetchState().then((next) => {
      if (!cancelled) setState(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Smart Home Hub</h1>
      {loginError && <p role="alert">Sign-in failed ({loginError}). Please try again.</p>}
      {state.status === "loading" && <p>Loading…</p>}
      {state.status === "error" && <p role="alert">{state.message}</p>}
      {state.status === "anonymous" && (
        <a href={loginUrl(window.location.pathname)}>Sign in</a>
      )}
      {state.status === "signed-in" && (
        <SignedIn session={state.session} homes={state.homes} onChange={load} />
      )}
    </main>
  );
}

function SignedIn({
  session,
  homes,
  onChange,
}: {
  session: SessionInfo;
  homes: Home[];
  onChange: () => Promise<void>;
}) {
  const [name, setName] = useState("");

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    await createHome(session, name);
    setName("");
    await onChange();
  }

  async function signOut() {
    window.location.assign(await logout(session));
  }

  return (
    <section>
      <p>
        Signed in as <strong>{session.user.name ?? session.user.email}</strong>{" "}
        <button type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </p>
      <h2>Your homes</h2>
      {homes.length === 0 ? (
        <p>No homes yet.</p>
      ) : (
        <ul>
          {homes.map((home) => (
            <li key={home.id}>
              {home.name} <small>({home.role})</small>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={(event) => void submit(event)}>
        <label>
          New home{" "}
          <input value={name} onChange={(event) => { setName(event.target.value); }} required />
        </label>{" "}
        <button type="submit">Create</button>
      </form>
    </section>
  );
}
