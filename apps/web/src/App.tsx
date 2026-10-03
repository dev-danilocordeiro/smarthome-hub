import { BrowserRouter, Route, Routes } from "react-router";
import { loginUrl } from "./api/client";
import { SessionProvider, signOut, useSession } from "./api/session";
import { HomePage } from "./pages/HomePage";
import { HomesPage } from "./pages/HomesPage";

function Shell() {
  const state = useSession();
  const loginError = new URLSearchParams(window.location.search).get("login_error");

  return (
    <>
      <header className="app-header">
        <h1>Smart Home Hub</h1>
        {state.status === "signed-in" && (
          <p>
            <strong>{state.session.user.name ?? state.session.user.email}</strong>{" "}
            <button type="button" className="ghost" onClick={() => void signOut()}>
              Sign out
            </button>
          </p>
        )}
      </header>
      <main>
        {loginError && <p role="alert">Sign-in failed ({loginError}). Please try again.</p>}
        {state.status === "loading" && <p>Loading…</p>}
        {state.status === "error" && <p role="alert">{state.message}</p>}
        {state.status === "anonymous" && (
          <p>
            <a className="button" href={loginUrl(window.location.pathname)}>
              Sign in
            </a>
          </p>
        )}
        {state.status === "signed-in" && (
          <Routes>
            <Route path="/" element={<HomesPage />} />
            <Route path="/homes/:homeId/*" element={<HomePage />} />
          </Routes>
        )}
      </main>
    </>
  );
}

export function App() {
  return (
    <SessionProvider>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </SessionProvider>
  );
}
