/**
 * End-to-end replay test for the golf-landing-page scenario.
 *
 * Drives the full flow against the live compose stack with
 * POLARIS_REPLAY pointing at tests/fixtures/replay/raw/golf-landing-page.json.gz
 * — same path the smoke test exercised manually via Playwright MCP.
 * No real LLM / Pinterest / image-gen calls fire; the recorded
 * fixture supplies every codex frame and design-intent node output.
 *
 * Why opt-in via POLARIS_E2E_REPLAY=1:
 *   - The dev stack must be restarted with POLARIS_REPLAY set.  This
 *     test can't transparently flip env on a running stack, so we
 *     gate it behind an explicit run flag rather than skipping at
 *     test time and confusing the reporter.
 *
 * Operator workflow:
 *
 *   # one-time env setup
 *   echo 'POLARIS_REPLAY=/home/sun/projects/polaris-project/tests/fixtures/replay/raw/golf-landing-page.json.gz' >> .env
 *   docker compose -f compose.dev.yaml up -d --force-recreate api worker
 *
 *   # run
 *   POLARIS_E2E_REPLAY=1 pnpm --filter @polaris/web exec playwright test replay-golf
 *
 *   # cleanup
 *   sed -i '/POLARIS_REPLAY=/d' .env
 *   docker compose -f compose.dev.yaml up -d --force-recreate api worker
 */

import { test, expect, type Page } from "@playwright/test";


// Choices are pinned to the recording — the worker's ReplayCodexSession
// reads codex frames from the fixture, not from anything the user
// types, so picking different choices wouldn't change the codex stream.
// We assert the question titles too, so a re-recording with different
// wording fails the test loudly instead of silently miss-matching.
type Round = {
  name: string;
  questions: Array<{
    title_starts_with: string;
    choice_testid: string;
    choice_label_substring: string;
  }>;
};

const ROUNDS: Round[] = [
  {
    name: "round 1: audience / visual / goal",
    questions: [
      {
        title_starts_with: "Who is this page for",
        choice_testid: "clarification-choice-members",
        choice_label_substring: "Prospective members",
      },
      {
        title_starts_with: "What visual direction",
        choice_testid: "clarification-choice-heritage_premium",
        choice_label_substring: "Heritage premium",
      },
      {
        title_starts_with: "What should the page drive",
        choice_testid: "clarification-choice-membership_inquiry",
        choice_label_substring: "Membership inquiry",
      },
    ],
  },
  {
    name: "round 2: color / flow",
    questions: [
      {
        title_starts_with: "Pick a primary color",
        choice_testid: "clarification-choice-fairway_green",
        choice_label_substring: "Fairway Green",
      },
      {
        title_starts_with: "Choose the page flow",
        choice_testid: "clarification-choice-hero_story_membership",
        choice_label_substring: "Hero",
      },
    ],
  },
  {
    name: "round 3: surface / action",
    questions: [
      {
        title_starts_with: "Which page surface",
        choice_testid: "clarification-choice-private_club_landing",
        choice_label_substring: "Private golf club",
      },
      {
        title_starts_with: "What exact action",
        choice_testid: "clarification-choice-request_membership_info",
        choice_label_substring: "Request membership",
      },
    ],
  },
];


// One question's worth of UI work: assert the title, click the choice,
// click advance.  Card may transition immediately to the next
// question OR disappear (between rounds + on round 3 final).  Caller
// waits on the appropriate thing afterward.
async function answerOne(
  page: Page,
  q: Round["questions"][number],
): Promise<void> {
  // Wait for the card to show this specific question.  Cards persist
  // across question transitions within a round, so the title is the
  // distinguishing waypoint.
  await expect(page.getByText(new RegExp(q.title_starts_with, "i"))).toBeVisible({
    timeout: 60_000,
  });
  // Sanity-check the recorded choice is what's on screen.  Catches
  // a re-recording with a renamed choice id before the click goes
  // through into the wrong option.
  const choice = page.getByTestId(q.choice_testid);
  await expect(choice).toBeVisible();
  await expect(choice).toHaveAttribute(
    "data-choice-label",
    new RegExp(q.choice_label_substring, "i"),
  );
  await choice.click();
  await page.getByTestId("clarification-advance").click();
}


async function answerRound(page: Page, round: Round): Promise<void> {
  for (const q of round.questions) {
    await answerOne(page, q);
  }
}


test.describe("replay: golf-landing-page", () => {
  // Opt-in gate: POLARIS_E2E_REPLAY=1 signals the operator has
  // restarted the worker with POLARIS_REPLAY set.
  test.skip(
    !process.env.POLARIS_E2E_REPLAY,
    "opt-in: requires worker started with POLARIS_REPLAY",
  );

  test.beforeEach(async ({ page }) => {
    await page.goto("/api/auth/dev-login");
    await page.waitForURL("/", { timeout: 10_000 });

    // Quota hygiene: a previous failed test may have left a session
    // stuck in `running` / `queued` state, which holds a slot under
    // the per-user concurrency cap.  POST /sessions on the next run
    // would 429 silently and the test would fail at the very first
    // navigation with no useful diagnostic.  Belt-and-suspenders:
    // interrupt any non-terminal sessions for the dev user before
    // we kick off a fresh one.  Uses page.context().request so the
    // auth cookie set by dev-login is attached automatically;
    // Playwright's bare `request` fixture has its own cookie jar.
    const auth = page.context().request;
    const projects = (await (
      await auth.get("/api/projects")
    ).json()) as Array<{ id: string }>;
    for (const p of projects) {
      const sessions = (await (
        await auth.get(`/api/projects/${p.id}/sessions`)
      ).json()) as Array<{ id: string; status: string }>;
      for (const s of sessions) {
        if (s.status === "running" || s.status === "queued") {
          try {
            await auth.post(`/api/sessions/${s.id}/interrupt`);
          } catch {
            /* best-effort */
          }
        }
      }
    }
  });

  test("full flow drives clarifications → mood board → plan → build", async ({
    page,
  }) => {
    const auth = page.context().request;
    // ── 1. New project from the golf example card ──────────────────
    await page.getByRole("button", { name: /new project/i }).click();
    await page.waitForURL(/\/projects\/new$/);
    await page.getByTestId("example-card-golf").click();
    await page.waitForURL(/\/projects\/[0-9a-f-]{36}$/);
    const projectId = page.url().match(/\/projects\/([0-9a-f-]{36})$/)![1];

    try {
      // ── 2. Three clarification rounds back-to-back ────────────────
      // The SSE snapshot fix guarantees the first card appears
      // without a reload; rounds 2/3 arrive live as the previous
      // round's interrupt resumes through the replay runner.
      for (const round of ROUNDS) {
        await test.step(round.name, async () => {
          await answerRound(page, round);
        });
      }

      // ── 3. Discovery products ─────────────────────────────────────
      // Mood board (uploaded from recorded b64 PNG → MinIO)
      await expect(page.getByTestId("mood-board-img")).toBeVisible({
        timeout: 60_000,
      });
      // Plan card (rendered from recorded codex:plan item).  In replay
      // mode only the technical tab exists (Phase 3.5 limitation —
      // text_plain isn't captured); single-tab layout is fine.
      await expect(page.getByTestId("plan-card")).toBeVisible();
      // The Proceed/Approve button gates the build turn.
      await expect(page.getByTestId("plan-approve-button")).toBeVisible();

      // ── 4. Approve the plan, kick off build_direct ───────────────
      await page.getByTestId("plan-approve-button").click();

      // ── 5. Final session reaches completed ───────────────────────
      // Build replays in ~0.1 s wall time (was 339 s live), so a
      // generous 30 s timeout has huge headroom.
      await expect(page.getByTestId("session-status-completed")).toBeVisible({
        timeout: 30_000,
      });

      // ── 6. Invariants assertable at the chat-pane level ──────────
      // File counter ticked: codex's recorded fileChange items each
      // bump the StatusBar via the replay-mode sink wiring.  Golf
      // recording has 7 paths across 3 fileChange items.
      await expect(page.locator("text=File changes").first()).toBeVisible();
      // Playwright counter: codex called the playwright MCP 8 times
      // across the recorded build turn.
      await expect(page.locator("text=Test calls").first()).toBeVisible();

      // ── 7. Persisted clarifications visible to chat replay ───────
      // GET /sessions/{id} returns clarifications[] populated with
      // each round's selected_choice — that's what feeds the
      // chat-pane bubble rendering on a fresh page load.  Folded
      // into the same flow rather than a separate test to avoid
      // duplicating the slow scenario drive (~20 s) and the
      // cross-test workspace-container churn that adds.
      const sessions = await (
        await auth.get(`/api/projects/${projectId}/sessions`)
      ).json();
      const discoverSession = sessions.find(
        (s: { mode: string }) => s.mode === "discover_then_build",
      );
      expect(discoverSession).toBeDefined();
      const detail = await (
        await auth.get(`/api/sessions/${discoverSession.id}`)
      ).json();
      // Three rounds = three Clarification rows persisted with
      // status='answered' and at least one selected_choice each.
      expect(detail.clarifications.length).toBe(3);
      for (const c of detail.clarifications) {
        expect(c.status).toBe("answered");
        const answers = Object.values(c.answers) as Array<{
          selected_choice: string | null;
        }>;
        expect(answers.some((a) => a.selected_choice !== null)).toBe(true);
      }
    } finally {
      // Belt-and-suspenders cleanup — the test creates a real project
      // row even in replay mode (the orchestrator + DB are real).
      // Let test-database accumulation get out of hand fast in CI.
      try {
        await auth.delete(`/api/projects/${projectId}`);
      } catch {
        /* ignore — cleanup is best-effort */
      }
    }
  });

});
