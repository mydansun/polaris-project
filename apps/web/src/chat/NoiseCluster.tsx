/** Collapsible cluster summarizing a run of adjacent "noise" events. */

import { Avatar, AvatarFallback, cn } from "@polaris/ui";

import { ChatBubble, type ChatMessage } from "../ChatBubble";
import { KIND_VISUALS } from "./itemVisuals";
import { countByKind, kindLabel } from "./messageGrouping";

export function NoiseCluster({ messages }: { messages: ChatMessage[] }) {
  const kindCounts = countByKind(messages);
  return (
    <details className="group">
      <summary className="flex w-full cursor-pointer select-none items-center gap-2">
        <Avatar className="h-7 w-7 shrink-0">
          <AvatarFallback className="bg-surface-alt">
            <span className="icon-[ph--stack-fill] text-sm text-text-muted" />
          </AvatarFallback>
        </Avatar>
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
          {kindCounts.map(({ kind, count }) => {
            const visual = KIND_VISUALS[kind] ?? KIND_VISUALS["codex:other"];
            const label = kindLabel(kind);
            return (
              <span
                key={kind}
                className="flex items-center gap-1.5 text-[12px] text-text-muted/50"
              >
                <span className={cn(visual.icon, "text-sm")} />
                <span>{label}</span>
                {count > 1 && (
                  <span className="flex items-center gap-0.5 text-[11px] opacity-70">
                    <span className="icon-[mdi--close-thick] text-[9px]" />
                    {count}
                  </span>
                )}
              </span>
            );
          })}
        </div>
        <span className="icon-[mdi--chevron-right] shrink-0 text-xs text-text-muted transition-transform group-open:rotate-90" />
      </summary>
      <div className="mt-1 flex flex-col gap-1">
        {messages.map((msg) => (
          <ChatBubble key={msg.id} message={msg} />
        ))}
      </div>
    </details>
  );
}
