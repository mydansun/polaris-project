/**
 * HomePage E2E — runs against the live compose stack with seed data
 * loaded.  See `scripts/seed.py load` to populate.
 *
 *   docker compose -f compose.dev.yaml up -d
 *   ./scripts/seed.py load
 *   pnpm --filter @polaris/web exec playwright test
 *
 * If the seed snapshot isn't there, the LIVE assertions skip.
 */
import { test, expect } from "@playwright/test";


test.describe("/ HomePage", () => {
  test.beforeEach(async ({ page }) => {
    // Dev-login — same path the React app exposes for headless workflows.
    // Lands on /, the React router takes over from there.
    await page.goto("/api/auth/dev-login");
    await page.waitForURL("/", { timeout: 10_000 });
  });

  test("renders project cards with status badges", async ({ page }) => {
    await expect(page.getByRole("heading", { name: "Projects", level: 2 })).toBeVisible();
    const cards = page.getByRole("listitem");
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(1);

    // Every card has exactly one of the five status labels.
    for (let i = 0; i < count; i += 1) {
      const card = cards.nth(i);
      await expect(
        card.getByText(/^(Live|Draft|Publishing|Failed|Rolled back)$/),
      ).toBeVisible();
    }
  });

  test("cards without a screenshot or mood-board show the placeholder tile", async ({ page }) => {
    // Create a fresh project via the API — it has neither
    // ``latest_deployment`` nor ``mood_board_url`` yet, so the card
    // must render the placeholder instead of leaving a blank gap.
    const apiRequest = page.context().request;
    const name = `polaris-spec-placeholder-${Math.random().toString(36).slice(2, 8)}`;
    const created = await (
      await apiRequest.post("/api/projects", {
        data: { name },
        headers: { "content-type": "application/json" },
      })
    ).json();
    try {
      await page.goto("/");
      const card = page
        .getByRole("listitem")
        .filter({ hasText: name });
      await expect(card).toBeVisible();
      // The placeholder is the only thing in the card with this testid.
      await expect(card.getByTestId("project-card-placeholder")).toBeVisible();
    } finally {
      try { await apiRequest.delete(`/api/projects/${created.id}`); } catch { /* ignore */ }
    }
  });

  test("LIVE seeded projects render a deployment screenshot", async ({ page }) => {
    // After seed.py load + the one-shot screenshot backfill, every
    // LIVE seeded project's card has an <img src="…s3.<domain>/static
    // /images/deployments/<id>.png">.  We don't load the bytes (cross-
    // origin to MinIO + bucket policy isn't part of the assertion);
    // just verify the URL shape.
    await expect(page.getByRole("listitem").first()).toBeVisible();
    const liveCards = page.getByRole("listitem").filter({ hasText: "Live" });
    if ((await liveCards.count()) === 0) {
      test.skip(true, "no LIVE seeded projects");
    }
    const firstImg = liveCards.first().locator("img").first();
    await expect(firstImg).toHaveAttribute(
      "src",
      /\/static\/images\/deployments\/[0-9a-f-]{36}\.png$/,
    );
  });

  test("LIVE seeded projects have a hash button pointing at prod URL", async ({ page }) => {
    // Wait for cards to render before counting (the React app fetches
    // /api/projects asynchronously; .count() is a snapshot, so we
    // need the explicit wait).
    await expect(page.getByRole("listitem").first()).toBeVisible();

    const liveCards = page.getByRole("listitem").filter({ hasText: "Live" });
    const liveCount = await liveCards.count();
    if (liveCount === 0) {
      test.skip(true, "no LIVE seeded projects — run scripts/seed.py load");
    }
    const firstButton = liveCards.first().locator("[data-prod-url]");
    await expect(firstButton).toHaveAttribute(
      "data-prod-url",
      /^https:\/\/[0-9a-f-]{36}\.prod\.[a-z0-9.-]+\/?$/,
    );
  });

  test("+ New project button navigates to /projects/new", async ({ page }) => {
    await page.getByRole("button", { name: /new project/i }).click();
    await page.waitForURL(/\/projects\/new$/);
    // Welcome state is the existing first-message UI; verify a textarea
    // and the example-prompt buttons render.
    await expect(page.getByRole("textbox")).toBeVisible();
  });

  test("clicking a project card navigates to /projects/:id", async ({ page }) => {
    const firstCard = page.getByRole("listitem").first();
    await firstCard.click();
    await page.waitForURL(/\/projects\/[0-9a-f-]{36}$/);
  });

  test("project list polls on a 10s interval", async ({ page }) => {
    // Capture network calls for /api/projects.  We expect at least 2
    // within ~12s (initial + first poll).
    const calls: number[] = [];
    page.on("response", (r) => {
      if (r.url().includes("/api/projects") && r.request().method() === "GET") {
        calls.push(Date.now());
      }
    });
    await page.waitForTimeout(11_500);
    expect(calls.length).toBeGreaterThanOrEqual(2);
  });
});


test.describe("empty-state redirect", () => {
  // Skipped by default — flipping the user's project list to empty
  // requires DB intervention.  Placeholder so the test exists once
  // we add a fixture toggle.
  test.skip("user with no projects is redirected to /projects/new", async () => {
    /* todo: requires a fixture that resets the user's projects, then
       we'd assert the URL becomes /projects/new on first load. */
  });
});
