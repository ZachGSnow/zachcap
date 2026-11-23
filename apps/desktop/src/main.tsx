import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { followTheme } from "@/lib/theme";
import "./index.css";

// A desktop app has no use for the browser's Back/Refresh/Print menu. Text fields keep theirs,
// and development builds keep it for Inspect.
document.addEventListener("contextmenu", (event) => {
  const target = event.target as HTMLElement;
  if (!import.meta.env.DEV && !target.closest("input, textarea")) event.preventDefault();
});

// Light or dark as chosen in Settings, or following Windows (the default).
followTheme();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <TooltipProvider>
      <App />
      <Toaster position="bottom-right" />
    </TooltipProvider>
  </React.StrictMode>,
);
