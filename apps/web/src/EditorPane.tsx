import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ProjectDetailResponse } from "@polaris/shared-types";
type EditorPaneProps = {
  project: ProjectDetailResponse | null;
  ideUrl: string | null;
  projectRoot: string | null;
  sessionInFlight: boolean;
  folder: string;
};

export function EditorPane({
  project,
  ideUrl,
  projectRoot,
  sessionInFlight,
  folder,
}: EditorPaneProps) {
  const { t } = useTranslation();
  const workspace = project?.workspace ?? null;
  const [iframeLoaded, setIframeLoaded] = useState(false);
  const ideReady = workspace?.ide_status === "ready";

  useEffect(() => {
    setIframeLoaded(false);
  }, [folder]);

  // Pre-project: nothing to configure yet.
  if (workspace === null) {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-alt" aria-label="Online code editor">
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-text-muted">
          <span className="icon-[mdi--code] text-5xl" />
          <p className="text-sm">{t("editor.waitingForProject")}</p>
        </div>
      </section>
    );
  }

  // Project exists but agent hasn't declared project_root → API withholds
  // ide_url (and the POST /ide/session endpoint 409s).  Show a quiet
  // placeholder; SSE project_root_changed + 30s poller will bring URL in.
  if (projectRoot === null) {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-alt" aria-label="Online code editor">
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-text-muted">
          <span className="icon-[mdi--code] text-5xl" />
          <p className="text-sm">{t("editor.waitingForAgent")}</p>
        </div>
      </section>
    );
  }

  if (ideUrl !== null && ideReady) {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-alt" aria-label="Online code editor">
        <iframe
          key={folder}
          className="block h-full w-full bg-white"
          title="Code editor workspace"
          src={ideUrl}
          onLoad={() => setIframeLoaded(true)}
          sandbox="allow-downloads allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
          allow="clipboard-read; clipboard-write"
        />
        {!iframeLoaded ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-surface text-xs text-text-muted">
            <span className="icon-[mdi--code] text-3xl" />
            <div className="animate-pulse">{t("editor.loading")}</div>
          </div>
        ) : null}
      </section>
    );
  }

  if (workspace.ide_status === "failed") {
    return (
      <section className="relative h-full min-w-0 overflow-hidden bg-surface-alt" aria-label="Online code editor">
        <div className="flex h-full items-center justify-center p-6 text-center text-sm text-text-muted">
          <p className="text-text-primary">{t("editor.failed")}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="relative h-full min-w-0 overflow-hidden bg-surface-alt" aria-label="Online code editor">
      <div className="absolute inset-0 animate-pulse bg-surface" />
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-xs text-text-muted">
        <span className="icon-[mdi--code] text-3xl" />
        <div className="animate-pulse">{t("editor.starting")}</div>
      </div>
    </section>
  );
}
