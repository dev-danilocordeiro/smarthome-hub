import { useEffect, useRef, useState } from "react";

interface Resource<T> {
  data: T | null;
  error: unknown;
  reload: () => void;
}

/** Load data for `key` (refetched when the key changes or `reload` is called). A
 * response that arrives after the key changed is dropped. */
export function useResource<T>(load: () => Promise<T>, key: string): Resource<T> {
  const [state, setState] = useState<{ data: T | null; error: unknown }>({ data: null, error: null });
  const [version, setVersion] = useState(0);
  const loader = useRef(load);

  useEffect(() => {
    loader.current = load;
  });

  useEffect(() => {
    let cancelled = false;
    loader.current().then(
      (data) => {
        if (!cancelled) setState({ data, error: null });
      },
      (error: unknown) => {
        if (!cancelled) setState((current) => ({ ...current, error }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, version]);

  return { ...state, reload: () => { setVersion((v) => v + 1); } };
}
