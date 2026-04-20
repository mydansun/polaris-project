/**
 * ChatPane — left-column chat surface.
 *
 * Heavy logic split into sibling files under `chat/`:
 *   - messageGrouping.ts — noise-cluster grouping (pure utility)
 *   - NoiseCluster.tsx   — collapsed cluster summary
 *   - ChatInput.tsx      — input textarea + buttons
 *   - SessionStatusIcon.tsx — header status pill
 *
 * This file keeps: ChatPaneProps, scroll/auto-follow coordination, header
 * composition, and the main render tree.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import i18n from "./i18n";
import {
  Avatar,
  AvatarFallback,
  AvatarImage,
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ScrollArea,
  Separator,
} from "@polaris/ui";
import type {
  ClarificationRequest,
  ClarificationResponse,
  ProjectDetailResponse,
  SessionStatus,
  UserResponse,
} from "@polaris/shared-types";

import type { RightPaneTab, SessionStats } from "./App";
import { ChatBubble, type ChatMessage } from "./ChatBubble";
import { ClarificationCard } from "./ClarificationCard";
import { ChatInput } from "./chat/ChatInput";
import { ExampleProjectCards } from "./chat/ExampleProjectCards";
import { NoiseCluster } from "./chat/NoiseCluster";
import { StatusBar } from "./chat/StatusBar";
import { SessionStatusIcon, type SessionUiStatus } from "./chat/SessionStatusIcon";
import { groupMessages } from "./chat/messageGrouping";

type ChatPaneProps = {
  messages: ChatMessage[];
  project: ProjectDetailResponse | null;
  sessionStatus: SessionUiStatus;
  isSubmitting: boolean;
  error: string | null;
  ready: boolean;
  user: UserResponse;
  onSendMessage: (text: string) => void;
  /** Send via the design-intent pre-agent (mode="discover_then_build"). */
  onDiscover?: (text: string) => void;
  onNewProject: () => void;
  onOpenSwitcher: () => void;
  onLogout: () => void;
  onInterrupt: () => void;
  onRestartWorkspace: () => void;
  onOpenPublish: () => void;
  /** True while an agent_message is actively streaming tokens.  Flips
   *  auto-scroll from "smooth" to "instant" to avoid jitter. */
  isStreamingAgentMsg: boolean;
  rightPane: RightPaneTab;
  onRightPaneChange: (tab: RightPaneTab) => void;
  hasMoreSessions: boolean;
  isLoadingOlderSessions: boolean;
  onLoadOlderSessions: () => void;
  clarificationRequest: ClarificationRequest | null;
  onClarificationSubmit: (response: ClarificationResponse) => void;
  pendingPlanApproval: boolean;
  onProceedWithPlan: () => void;
  sessionStats: SessionStats;
};

const IDLE_STATUSES: SessionUiStatus[] = ["completed", "failed", "interrupted", "idle"];
const IN_FLIGHT_STATUSES: SessionStatus[] = ["queued", "running"];

// Within this many pixels of the bottom still counts as "at bottom" —
// tolerates sub-pixel scroll positions and browser-zoom drift.
const AT_BOTTOM_THRESHOLD = 50;

export function ChatPane({
  messages,
  project,
  sessionStatus,
  isSubmitting,
  error,
  ready,
  user,
  onSendMessage,
  onDiscover,
  onNewProject,
  onOpenSwitcher,
  onLogout,
  onInterrupt,
  onRestartWorkspace,
  onOpenPublish,
  isStreamingAgentMsg,
  rightPane,
  onRightPaneChange,
  hasMoreSessions,
  isLoadingOlderSessions,
  onLoadOlderSessions,
  clarificationRequest,
  onClarificationSubmit,
  pendingPlanApproval,
  onProceedWithPlan,
  sessionStats,
}: ChatPaneProps) {
  const { t } = useTranslation();
  const [inputText, setInputText] = useState("");
  const [restartDialogOpen, setRestartDialogOpen] = useState(false);

  // Ref to the Radix ScrollArea root; we query for the inner viewport
  // ([data-radix-scroll-area-viewport]) after mount to get the actual
  // scrollable element.
  const scrollRootRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLElement | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);

  // "User is at / near the bottom" — when true, new content auto-scrolls.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // Timestamp until which scroll events from the viewport should be
  // ignored.  Set on programmatic smooth scrolls so the mid-animation
  // scroll events don't flip isAtBottom back to false.
  const ignoreScrollUntilRef = useRef<number>(0);

  const isIdle = IDLE_STATUSES.includes(sessionStatus);
  const isInFlight = (IN_FLIGHT_STATUSES as SessionUiStatus[]).includes(sessionStatus);
  const canSend = inputText.trim().length > 0 && !isSubmitting && isIdle;
  const isEmpty = messages.length === 0;

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const vp = viewportRef.current;
    if (vp === null) return;
    if (behavior === "smooth") {
      ignoreScrollUntilRef.current = performance.now() + 700;
    }
    vp.scrollTo({ top: vp.scrollHeight, behavior });
    setIsAtBottom(true);
  }, []);

  // Resolve the inner viewport element once the ScrollArea mounts.
  useEffect(() => {
    if (scrollRootRef.current === null) return;
    viewportRef.current = scrollRootRef.current.querySelector<HTMLElement>(
      "[data-radix-scroll-area-viewport]",
    );
  }, [isEmpty]);

  // Scroll listener — track isAtBottom, trigger older-session loading near top.
  useEffect(() => {
    const vp = viewportRef.current;
    if (vp === null) return;
    const onScroll = () => {
      if (performance.now() < ignoreScrollUntilRef.current) return;
      const distanceFromBottom = vp.scrollHeight - vp.scrollTop - vp.clientHeight;
      setIsAtBottom(distanceFromBottom < AT_BOTTOM_THRESHOLD);
      if (vp.scrollTop < AT_BOTTOM_THRESHOLD && hasMoreSessions && !isLoadingOlderSessions) {
        onLoadOlderSessions();
      }
    };
    vp.addEventListener("scroll", onScroll, { passive: true });
    return () => vp.removeEventListener("scroll", onScroll);
  }, [isEmpty, hasMoreSessions, isLoadingOlderSessions, onLoadOlderSessions]);

  // Mirror the streaming flag into a ref so the RO callback below can
  // read the latest value without re-creating the observer.
  const isStreamingRef = useRef(isStreamingAgentMsg);
  useEffect(() => {
    isStreamingRef.current = isStreamingAgentMsg;
  }, [isStreamingAgentMsg]);

  // Preserve scroll position when older sessions are prepended.  Snapshot
  // scrollHeight before the DOM updates from older messages, then after
  // React renders, adjust scrollTop by the delta.
  const prevScrollHeightRef = useRef<number | null>(null);
  const prevMessageCountRef = useRef<number>(0);
  if (
    messages.length > prevMessageCountRef.current &&
    prevMessageCountRef.current > 0
  ) {
    const vp = viewportRef.current;
    if (vp && vp.scrollTop < AT_BOTTOM_THRESHOLD) {
      prevScrollHeightRef.current = vp.scrollHeight;
    }
  }
  prevMessageCountRef.current = messages.length;
  useEffect(() => {
    const vp = viewportRef.current;
    const prev = prevScrollHeightRef.current;
    if (vp && prev !== null) {
      vp.scrollTop += vp.scrollHeight - prev;
      prevScrollHeightRef.current = null;
    }
  });

  // Auto-scroll on content growth when user is following.  Use instant
  // scroll during streaming delta bursts to avoid smooth-scroll jitter.
  const initialLoadRef = useRef(true);
  useEffect(() => {
    if (isEmpty) initialLoadRef.current = true;
  }, [isEmpty]);
  useEffect(() => {
    if (contentRef.current === null || viewportRef.current === null) return;
    const ro = new ResizeObserver(() => {
      if (isAtBottom) {
        if (initialLoadRef.current) {
          scrollToBottom("auto");
          initialLoadRef.current = false;
        } else {
          scrollToBottom(isStreamingRef.current ? "auto" : "smooth");
        }
      }
    });
    ro.observe(contentRef.current);
    return () => ro.disconnect();
  }, [isAtBottom, isEmpty, scrollToBottom]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSend) return;
    onSendMessage(inputText.trim());
    setInputText("");
    requestAnimationFrame(() => scrollToBottom("smooth"));
  }

  function handleDiscover() {
    if (!canSend || onDiscover === undefined) return;
    onDiscover(inputText.trim());
    setInputText("");
    requestAnimationFrame(() => scrollToBottom("smooth"));
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (canSend) {
        onSendMessage(inputText.trim());
        setInputText("");
        requestAnimationFrame(() => scrollToBottom("smooth"));
      }
    }
  }

  const placeholder =
    project === null
      ? t("chat.describeWhatToBuild")
      : isInFlight
        ? t("chat.typeNextMessage")
        : t("chat.sendMessage");

  return (
    <section
      className="flex h-full flex-col overflow-hidden bg-surface"
      aria-label="AI console"
    >
      <div className="flex items-center justify-between gap-3 border-b border-border-light px-5 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <Button
            variant="ghost"
            size="icon"
            onClick={onOpenSwitcher}
            className="h-8 w-8 shrink-0 text-text-muted"
            title="Switch project"
            aria-label="Switch project"
          >
            <span className="icon-[mdi--menu] text-lg" />
          </Button>
          <img src="/polaris.svg" alt="Polaris" className="h-6 w-6 shrink-0" />
          <h1 className="min-w-0 truncate text-lg font-bold">
            {project?.name ?? t("chat.newProject")}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {!ready ? <Badge variant="destructive">{t("chat.apiOffline")}</Badge> : null}
          <SessionStatusIcon status={sessionStatus} />
          <Avatar className="h-7 w-7 shrink-0">
            {user.avatar_url !== null ? (
              <AvatarImage src={user.avatar_url} alt={user.name} />
            ) : null}
            <AvatarFallback className="bg-accent/20 text-accent text-xs">
              {user.name.charAt(0).toUpperCase()}
            </AvatarFallback>
          </Avatar>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0 text-text-muted"
                aria-label="More actions"
              >
                <span className="icon-[mdi--dots-vertical] text-lg" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              <DropdownMenuItem onClick={onNewProject}>
                <span className="icon-[mdi--plus-box] text-base" />
                {t("chat.menu.newProject")}
              </DropdownMenuItem>
              {project !== null ? (
                <DropdownMenuItem onClick={() => setRestartDialogOpen(true)}>
                  <span className="icon-[mdi--restart] text-base" />
                  {t("chat.menu.restartWorkspace")}
                </DropdownMenuItem>
              ) : null}
              <DropdownMenuItem
                onClick={() => {
                  const next = i18n.language === "zh" ? "en" : "zh";
                  i18n.changeLanguage(next);
                  localStorage.setItem("polaris-lang", next);
                }}
              >
                <span className="icon-[mdi--translate] text-base" />
                {t("language.switchTo")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onLogout}>
                <span className="icon-[mdi--logout] text-base" />
                {t("chat.menu.signOut")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {project !== null ? (
            <Button
              variant="ghost"
              size="icon"
              onClick={onOpenPublish}
              className="h-8 w-8 shrink-0 hover:opacity-80"
              title={t("chat.menu.deployments")}
            >
              <span className="icon-[emojione-v1--rocket] text-lg" />
            </Button>
          ) : null}
          <div className="flex items-center rounded-md border border-border-light bg-white p-0.5">
            {(["browser", "ide", "none"] as const).map((tab) => {
              const active = rightPane === tab;
              const icons: Record<string, string> = {
                browser: "icon-[mdi--web]",
                ide: "icon-[mdi--code-braces]",
                none: "icon-[mdi--eye-off-outline]",
              };
              const titles: Record<string, string> = {
                browser: t("chat.tabs.browser"),
                ide: t("chat.tabs.ide"),
                none: t("chat.tabs.hide"),
              };
              return (
                <button
                  key={tab}
                  type="button"
                  onClick={() => onRightPaneChange(tab)}
                  className={`flex h-7 w-7 items-center justify-center rounded p-0 cursor-pointer ${
                    active ? "bg-accent/15 text-accent" : "text-text-muted hover:bg-surface"
                  }`}
                  title={titles[tab]}
                >
                  <span className={`${icons[tab]} text-sm`} />
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {isEmpty ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-6 px-6 pb-8">
          <div className="flex flex-col items-center gap-3">
            <img src="/polaris.svg" alt="Polaris" className="h-12 w-12" />
            <p className="text-2xl font-bold text-text-primary">Polaris</p>
            <p className="text-sm text-text-muted">{t("chat.welcomeSubtitle")}</p>
          </div>
          {error !== null ? (
            <div className="w-full max-w-2xl rounded-lg border-l-4 border-error bg-error-light px-3 py-2 text-xs text-error">
              {error}
            </div>
          ) : null}
          <ExampleProjectCards
            onSelect={(msg) => onSendMessage(msg)}
            disabled={isSubmitting}
          />
          <div className="w-full max-w-2xl">
            <ChatInput
              value={inputText}
              onChange={setInputText}
              onSubmit={handleSubmit}
              onKeyDown={handleKeyDown}
              disabled={isSubmitting}
              canSend={canSend}
              placeholder={placeholder}
              onStop={onInterrupt}
              showStop={isInFlight}
              onDiscover={onDiscover !== undefined ? handleDiscover : undefined}
              discoverLabel={t("chat.discoverIntent")}
            />
          </div>
        </div>
      ) : (
        <>
          <div className="relative min-h-0 flex-1">
            <ScrollArea className="h-full" ref={scrollRootRef}>
              <div ref={contentRef} className="flex flex-col gap-3 p-4">
                {isLoadingOlderSessions && (
                  <div className="flex justify-center py-2">
                    <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-accent/30 border-t-accent" />
                  </div>
                )}
                {hasMoreSessions && !isLoadingOlderSessions && (
                  <button
                    type="button"
                    onClick={onLoadOlderSessions}
                    className="mx-auto cursor-pointer text-xs text-text-muted hover:text-accent"
                  >
                    {t("chat.loadEarlierMessages")}
                  </button>
                )}
                {groupMessages(messages).map((group, gi) =>
                  group.type === "single" ? (
                    <ChatBubble key={group.message.id} message={group.message} />
                  ) : (
                    <NoiseCluster key={`cluster-${gi}`} messages={group.messages} />
                  ),
                )}
                {clarificationRequest && (
                  <ClarificationCard
                    request={clarificationRequest}
                    onSubmit={onClarificationSubmit}
                  />
                )}
                {pendingPlanApproval && !isInFlight && (
                  <div className="flex justify-center py-4">
                    <div className="running-border flex items-center gap-3 rounded-2xl px-6 py-4">
                      <Button onClick={onProceedWithPlan} className="gap-2">
                        <span className="icon-[mdi--play] text-base" />
                        {t("chat.proceed")}
                      </Button>
                      <span className="text-xs text-text-muted">
                        {t("chat.orTypeAdjustments")}
                      </span>
                    </div>
                  </div>
                )}
                {isInFlight && !clarificationRequest ? (
                  <div className="flex justify-center py-4">
                    <div className="running-border flex items-center gap-2.5 rounded-full px-4 py-2">
                      <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-accent/30 border-t-accent" />
                      <span className="text-xs font-medium tracking-wide text-accent">
                        {t("chat.agentIsWorking")}
                      </span>
                      <span className="flex gap-0.5">
                        <span
                          className="h-1 w-1 animate-bounce rounded-full bg-accent/60"
                          style={{ animationDelay: "0ms" }}
                        />
                        <span
                          className="h-1 w-1 animate-bounce rounded-full bg-accent/60"
                          style={{ animationDelay: "150ms" }}
                        />
                        <span
                          className="h-1 w-1 animate-bounce rounded-full bg-accent/60"
                          style={{ animationDelay: "300ms" }}
                        />
                      </span>
                    </div>
                  </div>
                ) : null}
              </div>
            </ScrollArea>
            {/* Jump-to-bottom FAB — shown only when scrolled away from bottom. */}
            {!isAtBottom ? (
              <Button
                type="button"
                size="icon"
                variant="outline"
                onClick={() => scrollToBottom("smooth")}
                className="absolute bottom-3 left-1/2 h-9 w-9 -translate-x-1/2 rounded-full border border-border-light bg-white/95 shadow-md backdrop-blur"
                aria-label="Jump to latest"
                title="Jump to latest"
              >
                <span className="icon-[mdi--arrow-down] text-base text-text-primary" />
              </Button>
            ) : null}
          </div>

          {error !== null ? (
            <div className="mx-4 mb-2 rounded-lg border-l-4 border-error bg-error-light px-3 py-2 text-xs text-error">
              {error}
            </div>
          ) : null}

          <Separator />

          <div className="p-3">
            <ChatInput
              value={inputText}
              onChange={setInputText}
              onSubmit={handleSubmit}
              onKeyDown={handleKeyDown}
              disabled={isSubmitting}
              canSend={canSend}
              placeholder={placeholder}
              onStop={onInterrupt}
              showStop={isInFlight}
              onDiscover={onDiscover !== undefined ? handleDiscover : undefined}
              discoverLabel={t("chat.rediscoverIntent")}
            />
          </div>
          <StatusBar stats={sessionStats} />
        </>
      )}
      <Dialog open={restartDialogOpen} onOpenChange={setRestartDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("chat.restartConfirm.title")}</DialogTitle>
            <DialogDescription>{t("chat.restartConfirm.body")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRestartDialogOpen(false)}>
              {t("chat.restartConfirm.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setRestartDialogOpen(false);
                onRestartWorkspace();
              }}
            >
              {t("chat.restartConfirm.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
