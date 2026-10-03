import { useEffect, useReducer, useState } from "react";
import { BASE } from "../api/client";
import { applyLive, EMPTY, type LiveMessage } from "./model";

export type Connection = "connecting" | "live" | "reconnecting" | "signed-out" | "forbidden";

const MAX_BACKOFF_MS = 30_000;
export const ALERTS_CHANGED = "smarthome:alerts-changed";
// Close codes the server uses (see identity/api/dependencies.py).
const UNAUTHORIZED = 4401;
const FORBIDDEN = 4403;

export function liveUrl(homeId: string, location: Location = window.location): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}${BASE}/homes/${homeId}/live`;
}

/** Live state of a home: a snapshot on every (re)connect, then device events. */
export function useLiveHome(homeId: string) {
  const [state, dispatch] = useReducer(applyLive, EMPTY);
  const [connection, setConnection] = useState<Connection>("connecting");

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let backoff = 1000;
    let stopped = false;

    function open() {
      socket = new WebSocket(liveUrl(homeId));
      socket.onmessage = (event: MessageEvent<string>) => {
        const message = JSON.parse(event.data) as LiveMessage;
        if (message.type === "snapshot") {
          backoff = 1000;
          setConnection("live");
        }
        dispatch(message);
        if (message.type === "alert") {
          // The notification bell lives outside the home view; it listens for this.
          window.dispatchEvent(new CustomEvent(ALERTS_CHANGED));
        }
      };
      socket.onclose = (event) => {
        if (stopped) return;
        if (event.code === UNAUTHORIZED || event.code === FORBIDDEN) {
          setConnection(event.code === UNAUTHORIZED ? "signed-out" : "forbidden");
          return;
        }
        // Dropped, server restarting, or closed for being too slow (1013): try again.
        setConnection("reconnecting");
        retry = setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      };
    }

    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      socket?.close();
    };
  }, [homeId]);

  return { ...state, connection };
}
