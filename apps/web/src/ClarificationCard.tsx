import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Input } from "@polaris/ui";
import type {
  ClarificationAnswer,
  ClarificationRequest,
  ClarificationResponse,
} from "@polaris/shared-types";

type ClarificationCardProps = {
  request: ClarificationRequest;
  onSubmit: (response: ClarificationResponse) => void;
  disabled?: boolean;
};

/**
 * One-question-at-a-time walkthrough of the clarifier's question batch.
 * Local state collects answers across steps; only the final step's button
 * submits to the server.  Max width kept bounded so the card doesn't
 * sprawl when the chat column is wide.
 */
export function ClarificationCard({
  request,
  onSubmit,
  disabled,
}: ClarificationCardProps) {
  const { t } = useTranslation();
  const total = request.questions.length;
  const [currentIndex, setCurrentIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, ClarificationAnswer>>(
    () => {
      const init: Record<string, ClarificationAnswer> = {};
      for (const q of request.questions) {
        init[q.id] = { selected_choice: null, override_text: null };
      }
      return init;
    },
  );

  const q = request.questions[currentIndex];
  const isLast = currentIndex === total - 1;
  const currentAnswer = answers[q.id];
  // Color-question treatment: when every option carries a swatch the
  // question is effectively "pick a color", so we scale the swatch up
  // from a 16px dot to a 24px chip with a soft shadow to make the
  // choice feel deliberate.
  const isColorQuestion =
    q.choices.length > 0 && q.choices.every((c) => typeof c.swatch === "string" && c.swatch);
  const canAdvance = useMemo(() => {
    const a = answers[q.id];
    if (!a) return false;
    return (
      a.selected_choice !== null ||
      (a.override_text !== null && a.override_text.trim().length > 0)
    );
  }, [answers, q.id]);

  function setChoice(choiceId: string) {
    setAnswers((prev) => ({
      ...prev,
      [q.id]: { ...prev[q.id], selected_choice: choiceId },
    }));
  }

  function setOverride(text: string) {
    setAnswers((prev) => ({
      ...prev,
      [q.id]: { ...prev[q.id], override_text: text || null },
    }));
  }

  function advance() {
    if (!canAdvance || disabled) return;
    if (isLast) {
      onSubmit({ request_id: request.request_id, answers });
      return;
    }
    setCurrentIndex((i) => Math.min(total - 1, i + 1));
  }

  function goBack() {
    if (currentIndex === 0 || disabled) return;
    setCurrentIndex((i) => Math.max(0, i - 1));
  }

  const progressPct = ((currentIndex + 1) / total) * 100;

  return (
    <div className="running-border flex w-full max-w-2xl flex-col gap-4 rounded-2xl p-5">
      {/* Progress header */}
      <div className="flex items-center gap-3 text-xs text-text-muted">
        <span className="shrink-0 tabular-nums">
          {currentIndex + 1} / {total}
        </span>
        <div className="relative h-1 flex-1 overflow-hidden rounded-full bg-border-light">
          <div
            className="absolute inset-y-0 left-0 bg-accent transition-all duration-200"
            style={{ width: `${progressPct}%` }}
          />
        </div>
        {currentIndex > 0 ? (
          <button
            type="button"
            onClick={goBack}
            disabled={disabled}
            className="flex shrink-0 cursor-pointer items-center gap-0.5 text-text-muted hover:text-text-primary disabled:opacity-50"
          >
            <span className="icon-[mdi--chevron-left] text-sm" />
            {t("clarification.back")}
          </button>
        ) : null}
      </div>

      {/* Current question */}
      <div className="flex flex-col gap-2">
        <p className="text-sm font-semibold text-text-primary">{q.title}</p>
        {q.description ? (
          <p className="text-xs text-text-muted">{q.description}</p>
        ) : null}
        <div className="flex flex-col gap-1.5">
          {q.choices.map((c) => {
            const selected = currentAnswer?.selected_choice === c.id;
            return (
              <button
                key={c.id}
                type="button"
                disabled={disabled}
                onClick={() => setChoice(c.id)}
                className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                  selected
                    ? "border-accent bg-accent/10 text-text-primary"
                    : "border-border-light bg-white text-text-muted hover:border-accent/40 hover:bg-accent/5"
                }`}
              >
                {c.swatch ? (
                  <span
                    aria-hidden
                    className={
                      "inline-block shrink-0 rounded-full border border-black/10 " +
                      (isColorQuestion ? "h-6 w-6 shadow-sm" : "h-4 w-4")
                    }
                    style={{ backgroundColor: c.swatch }}
                  />
                ) : null}
                <span className="font-medium">{c.label}</span>
                {c.summary ? (
                  <span className="ml-1.5 text-xs text-text-muted">{c.summary}</span>
                ) : null}
              </button>
            );
          })}
        </div>
        {q.allow_override_text ? (
          <Input
            type="text"
            placeholder={q.override_label ?? t("clarification.overrideLabel")}
            value={currentAnswer?.override_text ?? ""}
            onChange={(e) => setOverride(e.target.value)}
            disabled={disabled}
            className="text-sm"
          />
        ) : null}
      </div>

      <Button
        onClick={advance}
        disabled={disabled || !canAdvance}
        className="w-full"
      >
        {isLast ? t("clarification.continue") : t("clarification.next")}
      </Button>
    </div>
  );
}
