import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./index.css";

// A desktop app has no use for the browser's Back/Refresh/Print menu. Text fields keep theirs,
// and development builds keep it for Inspect.
document.addEventListener("contextmenu", (event) => {
  const target = event.target as HTMLElement;
  if (!import.meta.env.DEV && !target.closest("input, textarea")) event.preventDefault();
});

// Follow the Windows light/dark setting.
const dark = window.matchMedia("(prefers-color-scheme: dark)");
const applyTheme = () => document.documentElement.classList.toggle("dark", dark.matches);
applyTheme();
dark.addEventListener("change", applyTheme);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <TooltipProvider>
      <App />
      <Toaster position="bottom-right" />
    </TooltipProvider>
  </React.StrictMode>,
);
