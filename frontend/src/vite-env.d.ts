/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Optional API base override, baked in at build time.
   *
   * Declared optional because it is never actually defined: no `.env` file
   * sets it, the Dockerfile does not pass it, and CI does not either — the
   * SPA is served from the same origin as the API and talks to the relative
   * `/api/v1`. Typed as a required `string` this was a value TypeScript
   * believed could never be `undefined` while it was `undefined` on every
   * build, which also made the `?? '/api/v1'` fallbacks at both read sites
   * look like dead code to a reader (and to a linter) when they are in fact
   * the only thing supplying the value.
   *
   * Read in exactly two places, both defaulting to `/api/v1`:
   * `src/api/client.ts` and `src/hooks/useJobStream.ts`.
   */
  readonly VITE_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
