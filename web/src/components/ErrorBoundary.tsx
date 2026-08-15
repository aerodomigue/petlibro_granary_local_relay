import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public state: ErrorBoundaryState = { hasError: false };

  public static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("React dashboard error", error, errorInfo);
  }

  public render(): ReactNode {
    if (this.state.hasError) {
      return <section className="error-boundary" role="alert"><h1>Something went wrong</h1><p>This screen can be safely retried without changing the feeder.</p><button type="button" onClick={() => this.setState({ hasError: false })}>Try again</button></section>;
    }
    return this.props.children;
  }
}
