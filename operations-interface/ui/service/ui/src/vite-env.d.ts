/// <reference types="vite/client" />

// Coinext UI — typed build-time env (see src/api.ts).
interface ImportMetaEnv {
  /** Base URL of the `api` service. Default: same-origin /api proxy. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
