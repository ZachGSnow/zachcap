import { Component, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

type State = { error: Error | null };

/** Keeps a page that fails to render from blanking the whole window: says what broke and
 * offers a way back. Keyed by the page, so navigating away resets it. */
export class ErrorBoundary extends Component<{ children: ReactNode; onReset: () => void }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
        <p className="font-medium">This page ran into a problem</p>
        <p className="max-w-md text-sm text-muted-foreground">{this.state.error.message}</p>
        <Button
          size="sm"
          onClick={() => {
            this.setState({ error: null });
            this.props.onReset();
          }}
        >
          Back to meetings
        </Button>
      </div>
    );
  }
}
