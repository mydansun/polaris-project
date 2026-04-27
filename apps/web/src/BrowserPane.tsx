import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@polaris/ui";
import type { BrowserSessionResponse } from "@polaris/shared-types";
type BrowserPaneProps = {
  browserSession: BrowserSessionResponse | null;
  isOpeningBrowser: boolean;
  hasWorkspace: boolean;
  /** True when a project exists but the agent hasn't declared
   * project_root yet.  The API returns 404 in this state and we show a
   * "waiting for the agent" placeholder instead of the loading spinner. */
  awaitingAgent: boolean;
  onOpenBrowser: () => void;
  mcpToolCallActive: boolean;
};

export function BrowserPane({
  browserSession,
  isOpeningBrowser,
  hasWorkspace,
  awaitingAgent,
  onOpenBrowser,
  mcpToolCallActive,
}: BrowserPaneProps) {
  const { t } = useTranslation();
  const [iframeLoaded, setIframeLoaded] = useState(false);
  const browserUrl = browserSession?.vnc_url ?? null;
  const status = browserSession?.status ?? null;

  // Pre-project: needs a project before anything happens.
  if (!hasWorkspace) {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-accent" aria-label="Browser VNC">
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-text-muted">
          <span className="icon-[openmoji--chromium] text-5xl" />
          <p className="text-sm">{t("browser.waitingForProject")}</p>
        </div>
      </section>
    );
  }

  // Project exists but agent hasn't declared project_root → awaiting.
  // The API withholds vnc_url in this state (404), so browserSession
  // is null here, but we don't want to show "starting…" forever.
  if (awaitingAgent) {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-accent" aria-label="Browser VNC">
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-text-muted">
          <span className="icon-[openmoji--chromium] text-5xl" />
          <p className="text-sm">{t("browser.waitingForAgent")}</p>
        </div>
      </section>
    );
  }

  if (browserUrl !== null && status === "ready") {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-accent" aria-label="Browser VNC">
        <iframe
          className="block h-full w-full bg-white"
          title="Chromium browser session"
          src={browserUrl}
          onLoad={() => setIframeLoaded(true)}
          sandbox="allow-downloads allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
          allow="clipboard-read; clipboard-write"
        />
        {!iframeLoaded ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-surface text-xs text-text-muted">
            <span className="icon-[openmoji--chromium] text-3xl" />
            <div className="animate-pulse">{t("browser.loading")}</div>
          </div>
        ) : null}
        {mcpToolCallActive && (
          <div
            className="absolute inset-0 z-[5] flex items-center justify-center bg-black/25 backdrop-blur-[1px]"
            aria-label="Browser interaction blocked"
          >
            <div className="running-border flex flex-col items-center gap-3 rounded-2xl px-8 py-6 shadow-lg">
              <span className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-accent/30 border-t-accent" />
              <span className="text-sm font-medium text-text-primary">
                {t("browser.agentTesting")}
              </span>
            </div>
          </div>
        )}
      </section>
    );
  }

  if (browserSession === null || status === "starting") {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-accent" aria-label="Browser VNC">
        <div className="absolute inset-0 animate-pulse bg-surface" />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-xs text-text-muted">
          <span className="icon-[openmoji--chromium] text-3xl" />
          <div>{t("browser.starting")}</div>
        </div>
      </section>
    );
  }

  return (
    <section className="relative h-full min-w-0 overflow-hidden bg-surface-accent" aria-label="Browser VNC">
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-text-muted">
        <p>{t("browser.sessionIs", { status: status ?? t("browser.unavailable") })}</p>
        <Button variant="outline" size="sm" disabled={isOpeningBrowser} onClick={onOpenBrowser}>
          {isOpeningBrowser ? t("browser.opening") : t("browser.openBrowser")}
        </Button>
      </div>
    </section>
  );
}
