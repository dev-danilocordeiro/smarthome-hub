import { useCallback, useState } from "react";
import { api, ApiProblem, type Command, describe, reauthenticationUrl, unwrap } from "../api/client";
import type { CommandUpdate } from "../live/model";

export const SETTLED = new Set(["acknowledged", "failed", "timed_out"]);

export interface CommandFeedback {
  kind: "error" | "reauth";
  message: string;
  href?: string;
}

/** Sends commands and tracks which devices have one in flight (until the live socket
 * reports its outcome). */
export function useCommands(homeId: string, updates: Record<string, CommandUpdate>) {
  const [issued, setIssued] = useState<Record<string, string>>({}); // command id -> device
  const [feedback, setFeedback] = useState<CommandFeedback | null>(null);

  const pending = new Set(
    Object.entries(issued)
      .filter(([id]) => !SETTLED.has(updates[id]?.status ?? "pending"))
      .map(([, device]) => device),
  );

  const send = useCallback(
    async (deviceId: string, desired: Record<string, unknown> | null, action = "set_state") => {
      setFeedback(null);
      try {
        const command: Command = await unwrap(
          api.POST("/homes/{home_id}/devices/{device_id}/commands", {
            params: { path: { home_id: homeId, device_id: deviceId } },
            body: { action: action as Command["action"], desired },
          }),
        );
        setIssued((current) => ({ ...current, [command.id]: deviceId }));
        return command;
      } catch (error) {
        if (error instanceof ApiProblem) {
          const href = reauthenticationUrl(error.problem, window.location.pathname);
          setFeedback(
            href
              ? { kind: "reauth", message: error.problem.title, href }
              : { kind: "error", message: error.message },
          );
        } else {
          setFeedback({ kind: "error", message: describe(error) });
        }
        return null;
      }
    },
    [homeId],
  );

  return { send, pending, feedback, clearFeedback: () => { setFeedback(null); } };
}
