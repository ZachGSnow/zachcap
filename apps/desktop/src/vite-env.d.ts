/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ZACHCAP_PORT?: string;
  readonly VITE_ZACHCAP_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
