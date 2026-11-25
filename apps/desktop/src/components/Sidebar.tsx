import {
  AudioLines,
  FileUp,
  Hash,
  Info,
  ListChecks,
  ListVideo,
  MessagesSquare,
  NotebookText,
  Search,
  Settings,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { RecordButton } from "@/components/Recording";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type View =
  | { name: "meetings" }
  | { name: "meeting"; id: number; at?: number }
  | { name: "ask" }
  | { name: "actions" }
  | { name: "worklog" }
  | { name: "topics" }
  | { name: "people" }
  | { name: "search" }
  | { name: "settings" }
  | { name: "about" };

const NAV = [
  { name: "meetings", label: "Meetings", icon: ListVideo },
  { name: "ask", label: "Ask", icon: MessagesSquare },
  { name: "actions", label: "Action items", icon: ListChecks },
  { name: "worklog", label: "Work log", icon: NotebookText },
  { name: "topics", label: "Topics", icon: Hash },
  { name: "people", label: "People", icon: Users },
  { name: "search", label: "Search", icon: Search },
  { name: "settings", label: "Settings", icon: Settings },
] as const;

function NavItem({
  icon: Icon,
  active,
  onClick,
  children,
}: {
  icon: LucideIcon;
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors",
        active
          ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground",
      )}
    >
      <Icon className="size-4" />
      {children}
    </button>
  );
}

export function Sidebar({
  view,
  openActions,
  engineOk,
  onNavigate,
  onImport,
}: {
  view: View;
  openActions: number;
  engineOk: boolean;
  onNavigate: (view: View) => void;
  onImport: () => void;
}) {
  const active = view.name === "meeting" ? "meetings" : view.name;
  return (
    <aside className="flex w-60 shrink-0 select-none flex-col border-r bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-2.5 px-4 pt-4 pb-3">
        <div className="flex size-8 items-center justify-center rounded-lg bg-foreground text-background shadow-sm">
          <AudioLines className="size-4.5" />
        </div>
        <div className="leading-tight">
          <div className="font-heading text-[15px] font-semibold tracking-tight">ZachCap</div>
          <div className="text-[11px] text-muted-foreground">Meetings that remember who said what</div>
        </div>
      </div>
      <div className="space-y-1.5 px-3 pb-3">
        <RecordButton />
        <Button variant="outline" size="sm" className="w-full justify-start gap-2" onClick={onImport}>
          <FileUp className="size-4" />
          Import recording
        </Button>
      </div>
      <nav className="flex flex-col gap-0.5 px-2">
        {NAV.map(({ name, label, icon }) => (
          <NavItem key={name} icon={icon} active={active === name} onClick={() => onNavigate({ name })}>
            {label}
            {name === "actions" && openActions > 0 && (
              <span className="ml-auto rounded-full bg-foreground px-1.5 py-px text-[10px] font-medium text-background tabular-nums">
                {openActions}
              </span>
            )}
          </NavItem>
        ))}
      </nav>
      <div className="mt-auto flex flex-col px-2 pt-3">
        <NavItem icon={Info} active={active === "about"} onClick={() => onNavigate({ name: "about" })}>
          About
        </NavItem>
      </div>
      <div className="flex items-center gap-2 px-4 pt-2 pb-3 text-xs text-muted-foreground">
        <span className={cn("size-1.5 rounded-full", engineOk ? "bg-emerald-500" : "animate-pulse bg-red-500")} />
        {engineOk ? "Local engine running" : "Engine not responding"}
      </div>
    </aside>
  );
}
