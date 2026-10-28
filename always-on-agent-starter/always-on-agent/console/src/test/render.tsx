import { render } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import App from "../App";
import { mockFetch, type Overrides } from "./mockApi";

/** Render the whole console at a hash route against the fake API. */
export function renderAt(hash: string, overrides: Overrides = {}) {
  const calls = mockFetch(overrides);
  window.location.hash = hash;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const view = render(<App client={client} />);
  return { calls, client, ...view };
}
