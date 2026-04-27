/**
 * Top-level shell: handles auth (the one piece that's truly app-wide)
 * and dispatches the four routes:
 *
 *   /                  → HomePage    (project list)
 *   /projects/new      → ProjectAppShell with no project loaded
 *                        (the welcome screen with "describe what you want
 *                        to build" — same UX as before)
 *   /projects/:id      → ProjectAppShell with that project loaded
 *   /login             → LoginPage   (also rendered inline below when
 *                        the auth check resolves to null)
 *
 * Everything below this file (~900 LoC of session / IDE / browser glue)
 * lives in ProjectAppShell.tsx.
 */
import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import type { UserResponse } from "@polaris/shared-types";

import { getMe } from "./api";
import { LoginPage } from "./LoginPage";
import { HomePage } from "./HomePage";
import { ProjectAppShell } from "./ProjectAppShell";

// Re-export the types that used to live alongside the App component;
// historical consumers (ChatPane, hooks, chat/StatusBar) import from
// "./App", and this keeps that import surface stable.
export type { PaneMode, RightPaneTab, SessionStats } from "./ProjectAppShell";


export function App() {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    let alive = true;
    getMe()
      .then((authUser) => {
        if (alive) setUser(authUser);
      })
      .catch(() => {
        /* not logged in — fall through to LoginPage */
      })
      .finally(() => {
        if (alive) setAuthChecked(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const handleLogout = useCallback(() => {
    setUser(null);
  }, []);

  if (!authChecked) {
    return (
      <div className="flex h-dvh items-center justify-center bg-surface">
        <img src="/polaris.svg" alt="" className="h-10 w-10 animate-pulse" />
      </div>
    );
  }
  if (user === null) {
    return <LoginPage />;
  }

  return (
    <Routes>
      <Route path="/" element={<HomePage user={user} onLogout={handleLogout} />} />
      <Route
        path="/projects/new"
        element={<ProjectAppShell user={user} onLogout={handleLogout} />}
      />
      <Route
        path="/projects/:projectId"
        element={<ProjectAppShell user={user} onLogout={handleLogout} />}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
