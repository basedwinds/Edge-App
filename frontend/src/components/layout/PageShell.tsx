import type { ReactNode } from "react";
import { TopBar } from "./TopBar";

export function PageShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col flex-1 min-w-0">
      <TopBar title={title} />
      <div className="flex-1 overflow-auto p-6">{children}</div>
    </div>
  );
}
