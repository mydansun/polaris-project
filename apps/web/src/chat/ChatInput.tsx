/** Input textarea with send / stop / discover buttons.
 *
 * Kept free of ChatPane's scroll/state concerns — takes pure callbacks and
 * renders.  Used both in the empty-state hero block and the bottom-of-chat
 * row (ChatPane decides which to show).
 */

import { Button } from "@polaris/ui";
import i18n from "../i18n";

export type ChatInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  disabled: boolean;
  canSend: boolean;
  placeholder: string;
  onStop?: () => void;
  showStop?: boolean;
  /** When set, renders a "帮我构思 / Help me figure it out" button that
   *  submits the current text via the design-intent pre-agent. */
  onDiscover?: () => void;
  /** Label for the re-discover CTA when a design_intent is already active.
   *  Falls back to `chat.discoverIntent` when the project has no active intent. */
  discoverLabel?: string;
};

export function ChatInput({
  value,
  onChange,
  onSubmit,
  onKeyDown,
  disabled,
  canSend,
  placeholder,
  onStop,
  showStop,
  onDiscover,
  discoverLabel,
}: ChatInputProps) {
  return (
    <form onSubmit={onSubmit} className="w-full">
      {/* Min height ≈ three text lines; auto-grows up to 240px then scrolls.
          Send/Stop button is absolutely positioned bottom-right so it stays
          anchored regardless of textarea height. */}
      <div className="running-border relative rounded-2xl bg-white px-4 pt-3 pb-12 shadow-sm transition-shadow focus-within:shadow-md focus-within:ring-1 focus-within:ring-accent/30">
        <textarea
          rows={3}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder={placeholder}
          className="block min-h-[72px] w-full resize-none bg-transparent text-sm leading-6 text-text-primary placeholder:text-text-muted focus-visible:outline-none disabled:opacity-60"
          style={{ maxHeight: 240, overflowY: "auto" }}
          onInput={(e) => {
            const el = e.currentTarget;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
          }}
        />
        <span className="pointer-events-none absolute bottom-3 left-4 flex select-none items-center gap-1 text-[11px] text-text-muted/50">
          {typeof navigator !== "undefined" &&
          /Mac|iPhone|iPad/.test(navigator.platform) ? (
            <span className="icon-[mdi--apple-keyboard-command] text-xs" />
          ) : (
            <span className="icon-[mdi--keyboard-outline] text-xs" />
          )}
          <span className="icon-[mdi--keyboard-return] text-xs" />
          <span>{i18n.t("chat.toSend")}</span>
        </span>
        <div className="absolute bottom-2 right-2 flex items-center gap-2">
          {onDiscover !== undefined && showStop !== true ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canSend}
              onClick={onDiscover}
              className="h-8 rounded-full px-3 text-xs"
              title={discoverLabel ?? i18n.t("chat.discoverIntent")}
            >
              <span className="icon-[mdi--compass-outline] mr-1 text-sm" />
              {discoverLabel ?? i18n.t("chat.discoverIntent")}
            </Button>
          ) : null}
          {showStop === true && onStop !== undefined ? (
            <Button
              type="button"
              size="icon"
              variant="destructive"
              onClick={onStop}
              className="h-8 w-8 rounded-full"
              title="Stop current turn"
            >
              <span className="icon-[mdi--stop] text-base" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={!canSend}
              className="h-8 w-8 rounded-full"
            >
              <span className="icon-[mdi--send] text-base" />
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
