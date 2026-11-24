import { Lock } from "lucide-react";

import { Button } from "@/components/ui/button";

/** In place of what Claude would do with a private meeting. ``what`` ends the sentence that
 * says so. */
export function PrivateNote({ what, onAllow }: { what: string; onAllow: () => void }) {
  return (
    <div className="flex flex-col items-center py-14 text-center">
      <div className="flex size-12 items-center justify-center rounded-full bg-muted">
        <Lock className="size-5" />
      </div>
      <p className="mt-4 font-medium">This meeting is private</p>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">Nothing in it is sent to Claude, {what}</p>
      <Button variant="outline" className="mt-5" onClick={onAllow}>
        Let Claude read it
      </Button>
    </div>
  );
}
