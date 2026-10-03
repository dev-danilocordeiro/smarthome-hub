import { useRef, useState } from "react";
import { api, ApiProblem, type Automation, describe, type DryRun, type Problem, unwrap } from "../api/client";
import type { LiveDevice } from "../live/model";
import { parseDefinition, pretty, templates } from "./templates";

const DRY_RUN_HOURS = 24;

export function AutomationEditor({
  homeId,
  automation,
  devices,
  onSaved,
  onCancel,
}: {
  homeId: string;
  automation: Automation | null;
  devices: LiveDevice[];
  onSaved: (saved: Automation) => void;
  onCancel: () => void;
}) {
  const options = templates(devices);
  const [name, setName] = useState(automation?.name ?? options[0]?.name ?? "");
  const [enabled, setEnabled] = useState(automation?.enabled ?? true);
  const [text, setText] = useState(pretty(automation?.definition ?? options[0]?.definition ?? {}));
  const [problem, setProblem] = useState<Problem | string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [dryRun, setDryRun] = useState<DryRun | null>(null);
  const [busy, setBusy] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);

  function insert(snippet: string) {
    const area = editor.current;
    if (!area) return;
    const { selectionStart, selectionEnd } = area;
    setText(`${text.slice(0, selectionStart)}${snippet}${text.slice(selectionEnd)}`);
  }

  async function run<T>(work: (definition: Record<string, unknown>) => Promise<T>): Promise<T | null> {
    const parsed = parseDefinition(text);
    if ("error" in parsed) {
      setProblem(parsed.error);
      return null;
    }
    setBusy(true);
    setProblem(null);
    try {
      return await work(parsed.value);
    } catch (error) {
      setProblem(error instanceof ApiProblem ? error.problem : describe(error));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    const saved = await run(async (definition) => {
      const body = { name, enabled, definition };
      if (automation === null) {
        return unwrap(api.POST("/homes/{home_id}/automations", { params: { path: { home_id: homeId } }, body }));
      }
      return unwrap(
        api.PUT("/homes/{home_id}/automations/{automation_id}", {
          params: {
            path: { home_id: homeId, automation_id: automation.id },
            header: { "if-match": `"${String(automation.version)}"` },
          },
          body,
        }),
      );
    });
    if (saved) {
      setWarnings(saved.warnings ?? []);
      onSaved(saved);
    }
  }

  async function simulate() {
    const result = await run((definition) =>
      unwrap(
        api.POST("/homes/{home_id}/automations/dry-run", {
          params: { path: { home_id: homeId } },
          body: { definition, from: new Date(Date.now() - DRY_RUN_HOURS * 3_600_000).toISOString() },
        }),
      ),
    );
    if (result) setDryRun(result);
  }

  return (
    <section className="editor" aria-label="Automation editor">
      <div className="row">
        <label className="grow">
          Name <input value={name} onChange={(e) => { setName(e.target.value); }} required maxLength={80} />
        </label>
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => { setEnabled(e.target.checked); }} /> Enabled
        </label>
        {automation === null && (
          <label>
            Template{" "}
            <select
              onChange={(e) => {
                const chosen = options[Number(e.target.value)];
                if (chosen) {
                  setName(chosen.name);
                  setText(pretty(chosen.definition));
                }
              }}
            >
              {options.map((t, i) => (
                <option key={t.name} value={i}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="editor-body">
        <label className="grow">
          Definition (DSL v1)
          <textarea
            ref={editor}
            value={text}
            spellCheck={false}
            rows={22}
            onChange={(e) => { setText(e.target.value); }}
          />
        </label>
        <div className="reference">
          <h4>Devices</h4>
          <p className="muted">Click to insert the id at the cursor.</p>
          <ul className="plain">
            {devices.map((d) => (
              <li key={d.id}>
                <button type="button" className="link" onClick={() => { insert(`"${d.id}"`); }}>
                  {d.name}
                </button>{" "}
                <small className="muted">{d.kind}</small>
              </li>
            ))}
          </ul>
          <p>
            <a href="/api/automations/schema" target="_blank" rel="noreferrer">
              JSON Schema
            </a>
          </p>
        </div>
      </div>
      {problem !== null && (
        <div role="alert" className="problem">
          {typeof problem === "string" ? (
            problem
          ) : (
            <>
              <strong>{problem.title}</strong>
              {problem.detail && <>: {problem.detail}</>}
              {problem.status === 412 && <> Someone else saved a newer version; reload it first.</>}
              {problem.path && (
                <div>
                  at <code>{problem.path}</code>
                </div>
              )}
            </>
          )}
        </div>
      )}
      {warnings.length > 0 && (
        <ul className="warnings">
          {warnings.map((w) => (
            <li key={w}>⚠ {w}</li>
          ))}
        </ul>
      )}
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void save()}>
          {automation === null ? "Create" : `Save version ${String(automation.version + 1)}`}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={() => void simulate()}>
          Dry run (last {DRY_RUN_HOURS} h)
        </button>
        <button type="button" className="ghost" onClick={onCancel}>
          Close
        </button>
      </div>
      {dryRun && <DryRunResult result={dryRun} />}
    </section>
  );
}

function DryRunResult({ result }: { result: DryRun }) {
  const ran = result.runs.filter((r) => r.outcome === "would_run").length;
  return (
    <section aria-label="Dry run result">
      <h4>
        Would have run {ran} time{ran === 1 ? "" : "s"} in the last {DRY_RUN_HOURS} h
        {result.truncated && " (first 500 shown)"}
      </h4>
      {result.warnings.map((w) => (
        <p key={w} className="muted">
          {w}
        </p>
      ))}
      <ul className="plain">
        {result.runs.slice(0, 50).map((r) => (
          <li key={`${r.at}-${String(r.trigger_index)}`}>
            <span className={`badge outcome-${r.outcome}`}>{r.outcome.replace("_", " ")}</span>{" "}
            {new Date(r.at).toLocaleString()} <small className="muted">trigger {r.trigger_index}</small>
            {r.reason && <small className="muted"> · {r.reason}</small>}
            {r.failed_conditions.length > 0 && (
              <small className="muted"> · condition {r.failed_conditions.join(", ")} failed</small>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
