/// <reference types="vite/client" />

// VeloxQuant UI — typed build-time env (see src/api.ts).
interface ImportMetaEnv {
  /** Base URL of the `api` service. Default: http://localhost:8000. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
