import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  InputOTP,
  InputOTPGroup,
  InputOTPSlot,
  REGEXP_ONLY_DIGITS,
} from "@polaris/ui";
import { getAuthConfig, getDevLoginUrl, requestCode, verifyCode } from "./api";

type Step = "email" | "invite" | "code";

export function LoginPage() {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cooldown, setCooldown] = useState(0);

  // Dev Login button visibility is controlled by the backend:
  // POLARIS_DEV_USER_EMAIL unset → GET /auth/config returns
  // {dev_login_enabled: false} → we don't render the button.
  const [devLoginEnabled, setDevLoginEnabled] = useState(false);
  useEffect(() => {
    getAuthConfig()
      .then((cfg) => setDevLoginEnabled(cfg.dev_login_enabled))
      .catch(() => setDevLoginEnabled(false));
  }, []);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  async function handleRequestCode(e: React.FormEvent, invite?: string) {
    e.preventDefault();
    if (!email.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await requestCode(email.trim(), invite);
      if (!res.ok && res.reason === "invite_required") {
        setStep("invite");
      } else {
        setStep("code");
        setCooldown(60);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.errors.failedToSendCode"));
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmitInvite(e: React.FormEvent) {
    e.preventDefault();
    if (inviteCode.length !== 6) return;
    setLoading(true);
    setError(null);
    try {
      const res = await requestCode(email.trim(), inviteCode);
      if (!res.ok && res.reason === "invite_required") {
        setError(t("login.errors.invalidInviteCode"));
      } else {
        setStep("code");
        setCooldown(60);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.errors.invalidOrExpiredInvite"));
    } finally {
      setLoading(false);
    }
  }

  async function handleVerifyCode(e: React.FormEvent) {
    e.preventDefault();
    if (code.length !== 6) return;
    setLoading(true);
    setError(null);
    try {
      await verifyCode(email.trim(), code);
      window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.errors.verificationFailed"));
      setLoading(false);
    }
  }

  async function handleResend() {
    if (cooldown > 0) return;
    setLoading(true);
    setError(null);
    try {
      await requestCode(email.trim(), inviteCode || undefined);
      setCooldown(60);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.errors.failedToResendCode"));
    } finally {
      setLoading(false);
    }
  }

  const subtitle: Record<Step, string> = {
    email: t("login.signInWithEmail"),
    invite: t("login.enterInviteCode"),
    code: t("login.enterCodeSentTo", { email }),
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl font-bold">Polaris</CardTitle>
          <p className="text-sm text-text-muted">{subtitle[step]}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error !== null && (
            <div className="rounded-lg border-l-4 border-error bg-error-light px-3 py-2 text-xs text-error">
              {error}
            </div>
          )}

          {step === "email" && (
            <>
              <form onSubmit={(e) => handleRequestCode(e)} className="flex flex-col gap-3">
                <Input
                  type="email"
                  placeholder={t("login.emailPlaceholder")}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                  disabled={loading}
                />
                <Button type="submit" className="w-full" disabled={loading || !email.trim()}>
                  {loading ? t("login.checking") : t("login.continue")}
                </Button>
              </form>
              {devLoginEnabled ? (
                <Button
                  variant="outline"
                  onClick={() => { window.location.href = getDevLoginUrl(); }}
                  className="w-full gap-2"
                >
                  <span className="icon-[mdi--login] text-xl" />
                  {t("login.devLogin")}
                </Button>
              ) : null}
            </>
          )}

          {step === "invite" && (
            <>
              <form onSubmit={handleSubmitInvite} className="flex flex-col gap-3">
                <div className="flex justify-center">
                  <InputOTP
                    autoFocus
                    maxLength={6}
                    pattern={REGEXP_ONLY_DIGITS}
                    value={inviteCode}
                    onChange={setInviteCode}
                    disabled={loading}
                    aria-label={t("login.inviteCodePlaceholder")}
                  >
                    <InputOTPGroup>
                      {[0, 1, 2, 3, 4, 5].map((i) => (
                        <InputOTPSlot key={i} index={i} className="h-11 w-11 text-lg" />
                      ))}
                    </InputOTPGroup>
                  </InputOTP>
                </div>
                <Button type="submit" className="w-full" disabled={loading || inviteCode.length !== 6}>
                  {loading ? t("login.verifying") : t("login.continue")}
                </Button>
              </form>
              <button
                type="button"
                className="mx-auto cursor-pointer text-xs text-text-muted hover:text-accent"
                onClick={() => { setStep("email"); setInviteCode(""); setError(null); }}
              >
                {t("login.back")}
              </button>
            </>
          )}

          {step === "code" && (
            <>
              <form onSubmit={handleVerifyCode} className="flex flex-col gap-3">
                <div className="flex justify-center">
                  <InputOTP
                    autoFocus
                    maxLength={6}
                    pattern={REGEXP_ONLY_DIGITS}
                    value={code}
                    onChange={setCode}
                    disabled={loading}
                    aria-label={t("login.codePlaceholder")}
                  >
                    <InputOTPGroup>
                      {[0, 1, 2, 3, 4, 5].map((i) => (
                        <InputOTPSlot key={i} index={i} className="h-11 w-11 text-lg" />
                      ))}
                    </InputOTPGroup>
                  </InputOTP>
                </div>
                <Button type="submit" className="w-full" disabled={loading || code.length !== 6}>
                  {loading ? t("login.verifying") : t("login.verify")}
                </Button>
              </form>
              <div className="flex items-center justify-between text-xs text-text-muted">
                <button
                  type="button"
                  className="cursor-pointer hover:text-accent"
                  onClick={() => { setStep("email"); setCode(""); setError(null); }}
                >
                  {t("login.back")}
                </button>
                <button
                  type="button"
                  className={cooldown > 0 ? "opacity-50" : "cursor-pointer hover:text-accent"}
                  onClick={handleResend}
                  disabled={cooldown > 0}
                >
                  {cooldown > 0 ? t("login.resendIn", { seconds: cooldown }) : t("login.resendCode")}
                </button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
