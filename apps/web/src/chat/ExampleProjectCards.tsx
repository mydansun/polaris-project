/** Four example-project cards shown on the first-visit welcome screen.
 *
 *  Clicking a card fires `onSelect(message)` with a localized prompt so
 *  the agent starts building that archetype immediately.  Images ship in
 *  the frontend bundle at apps/web/public/examples/ — served relative to
 *  the site root, no domain coupling.
 */

import { useTranslation } from "react-i18next";

type ExampleKey = "golf" | "todo" | "blog" | "estate";
type Example = { key: ExampleKey; image: string };

const EXAMPLES: Example[] = [
  { key: "golf", image: "/examples/golf.jpg" },
  { key: "todo", image: "/examples/to-do-list.jpg" },
  { key: "blog", image: "/examples/blog.jpg" },
  { key: "estate", image: "/examples/estate.jpg" },
];

export function ExampleProjectCards({
  onSelect,
  disabled,
}: {
  onSelect: (message: string) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    // 4 cards in a single horizontal row so the ChatInput below stays
    // near the vertical center.  Images use a short 5:3 aspect so the
    // total welcome-area footprint drops from ~400px (2×2) to ~130px.
    // Falls back to 2 columns on very narrow screens (sub-`sm`) to keep
    // card width usable on phones.
    <div className="grid w-full max-w-2xl grid-cols-2 gap-2.5 sm:grid-cols-4">
      {EXAMPLES.map((ex) => {
        const title = t(`examples.${ex.key}.title`);
        const message = t(`examples.${ex.key}.message`);
        return (
          <button
            key={ex.key}
            type="button"
            disabled={disabled}
            onClick={() => onSelect(message)}
            className={
              "group flex cursor-pointer flex-col overflow-hidden rounded-lg border " +
              "border-border-light bg-white text-left shadow-sm transition " +
              "hover:-translate-y-0.5 hover:border-accent/60 hover:shadow-md " +
              "disabled:cursor-not-allowed disabled:opacity-60"
            }
          >
            <img
              src={ex.image}
              alt=""
              loading="lazy"
              className="aspect-[5/3] w-full object-cover"
            />
            <div className="flex items-center justify-between gap-1 px-2 py-1.5">
              <span className="truncate text-[12px] font-medium text-text-primary">
                {title}
              </span>
              <span className="icon-[mdi--arrow-right] shrink-0 text-xs text-text-muted transition-colors group-hover:text-accent" />
            </div>
          </button>
        );
      })}
    </div>
  );
}
