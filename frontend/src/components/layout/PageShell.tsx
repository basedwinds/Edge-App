import type { ReactNode } from "react";
import { TopBar } from "./TopBar";
import { WarmingBanner } from "./WarmingBanner";

export function PageShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col flex-1 min-w-0">
      <TopBar title={title} />
      {/* Every page goes through here, so the cold-start notice is wired once
          rather than remembered per page. It renders nothing once warm. */}
      <WarmingBanner />
      <div className="flex-1 overflow-auto p-6">{children}</div>
    </div>
  );
}
