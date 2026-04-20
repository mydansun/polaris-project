import { useTranslation } from "react-i18next";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@polaris/ui";

import type { QuotaError } from "./api";

/** Pops up when the API returns 429 on session creation.  Distinguishes
 *  the two reasons (platform-wide vs per-user) via different body copy so
 *  the user knows whether it's "try again soon" or "finish another
 *  session first". */
export function QuotaDialog({
  error,
  onClose,
}: {
  error: QuotaError | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const open = error !== null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("quota.title")}</DialogTitle>
          <DialogDescription>
            {error?.reason === "user_quota"
              ? t("quota.userBody", { limit: error.limit })
              : t("quota.platformBody", { limit: error?.limit ?? 0 })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button onClick={onClose}>{t("quota.ok")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
