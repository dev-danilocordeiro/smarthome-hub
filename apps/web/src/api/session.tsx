import { createContext, type ReactNode, use, useEffect, useState } from "react";
import { api, ApiProblem, describe, type Session, setCsrfToken, unwrap } from "./client";

type SessionState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "signed-in"; session: Session }
  | { status: "error"; message: string };

const SessionContext = createContext<SessionState>({ status: "loading" });

async function loadSession(): Promise<SessionState> {
  try {
    const session = await unwrap(api.GET("/auth/session"));
    setCsrfToken(session.csrf_token);
    return { status: "signed-in", session };
  } catch (error) {
    if (error instanceof ApiProblem && error.status === 401) return { status: "anonymous" };
    return { status: "error", message: describe(error) };
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    void loadSession().then((next) => {
      if (!cancelled) setState(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return <SessionContext value={state}>{children}</SessionContext>;
}

export function useSession(): SessionState {
  return use(SessionContext);
}

export async function signOut(): Promise<void> {
  const { logout_url } = await unwrap(api.POST("/auth/logout"));
  window.location.assign(logout_url);
}
